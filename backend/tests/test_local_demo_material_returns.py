"""Standing local-demo return authority against fresh synthetic databases only.

The existing return QA suite retains the complete source/numeric/SQL defense
matrix. These tests exercise the actual dedicated Owner-style demo factory and
the operation-specific grant that factory supplies; no live Owner copy is used.
"""
from base64 import urlsafe_b64decode
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT, Database, DEMO_ACTOR_ID
from backend.app.local_demo import DISCLOSURE, canonical, create_local_demo_app
from backend.app.main import create_app
from backend.app.material_return_models import MaterialReturn, MaterialReturnPosting
from backend.app.models import now_utc
from backend.tests.material_return_qa_runtime import (
    create_material_return_qa_app, seed_material_return_qa_database,
)
from backend.tests.material_return_seed_fixture import clone_material_return_seed
from backend.tests.test_material_returns import (
    URL, TABLES, add, counts, current, header, header_edit, line_edit,
    post, post_data, review, sources,
)
from backend.tests.test_material_issues import (
    balance, header as issue_header, add as issue_add, current as issue_current,
)
from backend.tests.test_local_demo_issues import confirmed_body, execute as issue_execute
from backend.tests.test_mrp import material, preview
from backend.tests.test_supplier_offers import snapshot
from backend.tests.test_supplier_returns import (
    header as supplier_header, line as supplier_line, post as supplier_post,
)


@pytest.fixture
def demo_return(tmp_path, material_return_sources):
    parent = tmp_path.resolve()
    if not parent.is_relative_to((ROOT / "artifacts").resolve()):
        base = ROOT / "artifacts" / "round16b-pytest"
        base.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(prefix="local-demo-", dir=base))
    path = parent / "local-demo-return.sqlite"
    metadata = clone_material_return_seed(material_return_sources, path)
    app = create_local_demo_app(path, backend_port=8026, frontend_port=5189)
    with TestClient(app, base_url="http://127.0.0.1:8026", client=("127.0.0.1", 50000)) as client:
        yield dict(metadata, client=client, app=app, db=app.state.db, path=path)


def assert_no_grant(r):
    with r["db"].read() as session:
        values = session.connection().exec_driver_sql(
            "SELECT erp_material_return_authorized(),erp_material_return_basis(),"
            "erp_material_return_document(),erp_material_return_digest(),"
            "erp_material_return_snapshot(),erp_material_return_time()"
        ).one()
        assert tuple(values) == (0, None, None, None, None, None)
        assert session.connection().exec_driver_sql("SELECT erp_issue_manual_authorized()").scalar() == 0


def copy_synthetic_database(source, destination):
    assert Path(source).resolve().is_relative_to((ROOT / "artifacts").resolve())
    assert Path(destination).resolve().is_relative_to((ROOT / "artifacts").resolve())
    original = sqlite3.connect(source); copy = sqlite3.connect(destination)
    try:
        original.backup(copy)
    finally:
        copy.close(); original.close()


def closed_file_snapshot(path):
    """Release every SQLite handle before testing real Windows file replacement."""
    connection = sqlite3.connect(path)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'")]
        return dict(rows={table: connection.execute('SELECT * FROM "' + table + '" ORDER BY 1').fetchall() for table in tables},
                    objects=connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall())
    finally:
        connection.close()


def test_demo_review_is_readonly_and_confirmed_return_preserves_issue_exact_stock_and_audit(demo_return):
    r = demo_return; c = r["client"]; h = header(r); line = add(r, h)
    for endpoint in ["/api/material-issues/policy", URL + "/policy"]:
        policy = c.get(endpoint).json()
        assert policy["posting_allowed"] and policy["authorization_basis"] == "LOCAL_DEMO_MANUAL"
        assert DISCLOSURE in policy["message"]
    before = snapshot(r["path"])
    buying = c.get("/api/purchase-outstanding").json()
    original_issue = issue_current(r, r["issue"])
    reviewed = review(r, h)
    assert snapshot(r["path"]) == before and balance(r) == "2.25"
    parent = reviewed["material_return"]; saved = parent["lines"][0]; origin = saved["source"]
    assert (parent["document_no"], origin["issue_no"], origin["issue_line_no"], origin["work_order_no"]) == (
        "MRET-TEST-001", "ISSUE-TEST-001", 1, "WO-TEST-001")
    assert (origin["issued_qty_base"], origin["returned_qty_base"], origin["returnable_qty_base"],
            saved["quantity"], saved["remaining_after_proposal_qty_base"]) == ("1.75", "0", "1.75", "0.75", "1")
    assert origin["position"]["id"] == r["issue"]["lines"][0]["position_id"]
    assert (origin["position"]["material_code"], origin["position"]["warehouse_code"], origin["position"]["lot_code"]) == (
        r["raws"][0]["code"], r["warehouse"]["code"], "LOT-R8-001")
    movement = reviewed["expected_movements"][0]
    assert (movement["return_line_id"], movement["origin_line_id"], movement["qty_base_delta"],
            movement["current_stock_qty_base"], movement["expected_stock_qty_base"], movement["unit_code"]) == (
        line["id"], origin["origin_line_id"], "0.75", "2.25", "3", r["unit"]["code"])
    body = dict(request_key=str(uuid4()), row_version=parent["row_version"],
                work_order_version=parent["work_order_version"], confirmation_token=reviewed["confirmation_token"])
    response = post(r, h, body)
    assert response.status_code == 201, response.text
    posted = response.json()
    assert posted["posting_state"] == "POSTED" and posted["authorization_basis"] == "LOCAL_DEMO_MANUAL"
    assert posted["movements"][0]["qty_base_delta"] == "0.75" and balance(r) == "3"
    assert (sources(r)[0]["returned_qty_base"], sources(r)[0]["returnable_qty_base"]) == ("0.75", "1")
    assert issue_current(r, r["issue"])["movements"] == original_issue["movements"]
    assert c.get("/api/purchase-outstanding").json() == buying
    planned = material(preview(r), r)
    assert (planned["gross_demand_qty_base"], planned["physical_stock_qty_base"], planned["shortage_qty_base"]) == (
        "11.5625", "3", "8.5625")
    encoded = body["confirmation_token"].split(".", 1)[0]
    payload = json.loads(urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    with r["db"].read() as session:
        connection = session.connection()
        audit = connection.exec_driver_sql(
            "SELECT authorization_basis,confirmation_digest,confirmation_snapshot,confirmation_received_at "
            "FROM material_return_posting").one()
        assert audit[0] == "LOCAL_DEMO_MANUAL" and audit[1] == hashlib.sha256(audit[2].encode()).hexdigest()
        assert audit[2] == canonical(payload) and audit[3]
        assert payload["operation"] == "manufacturing-material-return"
        assert payload["snapshot"]["material_return"]["id"] == h["id"]
        source_audit = payload["snapshot"]["origins"][origin["origin_line_id"]]
        assert source_audit["posting"]["id"] == r["issue"]["posting_id"]
        assert source_audit["event"]["qty_base_delta"] == "-1.75"
        assert source_audit["position"]["id"] == movement["position_id"]
        assert connection.exec_driver_sql("SELECT qty_base_delta,typeof(qty_base_delta) FROM material_return_entry").one() == ("0.75", "text")
        assert connection.exec_driver_sql("SELECT qty_base_delta FROM material_issue_entry").scalar() == "-1.75"
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []
    after = snapshot(r["path"])
    for table in ["material_issue", "material_issue_line", "material_issue_posting", "material_issue_entry"]:
        assert after["rows"][table] == before["rows"][table]
    assert_no_grant(r)


def test_demo_requires_explicit_untampered_confirmation_and_current_versions(demo_return):
    r = demo_return; h = header(r); add(r, h); body = post_data(r, h); before = snapshot(r["path"])
    for token in [None, "", "forged", body["confirmation_token"] + "changed"]:
        response = post(r, h, {**body, "confirmation_token": token})
        assert response.status_code == (422 if token == "" else 403), response.text
    missing = {key: value for key, value in body.items() if key != "confirmation_token"}
    assert post(r, h, missing).status_code == 403
    for patch in [{"row_version": 0}, {"work_order_version": 0}]:
        assert post(r, h, {**body, **patch}).status_code == 409
    assert post(r, h, {**body, "authorized": True, "local_demo": True}).status_code == 422
    assert snapshot(r["path"]) == before and balance(r) == "2.25"
    assert_no_grant(r)


def test_wrong_host_origin_peer_and_port_cannot_borrow_demo_authority(demo_return):
    r = demo_return; h = header(r); add(r, h); body = post_data(r, h); before = snapshot(r["path"])
    response = r["client"].post(URL + "/" + h["id"] + "/post", json=body,
                                headers={"Origin": "https://foreign.example"})
    assert response.status_code == 403, response.text
    for base, peer, headers in [
        ("http://127.0.0.1:8026", ("198.51.100.24", 50001), {}),
        ("http://127.0.0.1:9999", ("127.0.0.1", 50001), {}),
        ("http://127.0.0.1:8026", ("127.0.0.1", 50001), {"Host": "foreign.example:8026"}),
    ]:
        client = TestClient(r["app"], base_url=base, client=peer)
        try:
            assert client.post(URL + "/" + h["id"] + "/post", json=body, headers=headers).status_code in {400, 403}
        finally:
            client.close()
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


def test_default_runtime_flags_and_separate_qa_or_new_runtime_do_not_accept_demo_confirmation(demo_return, monkeypatch):
    r = demo_return; h = header(r); add(r, h); body = post_data(r, h); before = snapshot(r["path"])
    monkeypatch.setenv("ERP_RETURN_AUTHORIZED", "1")
    monkeypatch.setenv("ERP_QA_POSTING", "true")
    with TestClient(create_app(r["path"])) as ordinary:
        assert not ordinary.get(URL + "/policy").json()["posting_allowed"]
        assert not ordinary.get("/api/material-issues/policy").json()["posting_allowed"]
        for suffix, headers in [("?authorized=true&qa=true&local_demo=true", {}),
                                ("", {"X-QA-Authorized": "true", "X-ERP-Return-Posting": "1"})]:
            assert ordinary.post(URL + "/" + h["id"] + "/post" + suffix, json=body, headers=headers).status_code == 403
    for app, basis in [
        (create_material_return_qa_app(r["path"]), "CONTROLLED_QA_ONLY"),
        (create_local_demo_app(r["path"], backend_port=8026, frontend_port=5189), "LOCAL_DEMO_MANUAL"),
    ]:
        with TestClient(app, base_url="http://127.0.0.1:8026", client=("127.0.0.1", 50002)) as client:
            assert client.get(URL + "/policy").json()["authorization_basis"] == basis
            assert client.post(URL + "/" + h["id"] + "/post", json=body).status_code == 403
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


def test_demo_rejects_changed_database_instance_and_nonloopback_binding(demo_return):
    r = demo_return; h = header(r); add(r, h); body = post_data(r, h)
    with pytest.raises(ValueError):
        create_local_demo_app(r["path"], bind_host="0.0.0.0")
    before = snapshot(r["path"]); other_path = r["path"].parent / "other-synthetic.sqlite"
    copy_synthetic_database(r["path"], other_path)
    replacement = Database(other_path); original = r["app"].state.db
    r["app"].state.db = replacement
    try:
        assert post(r, h, body).status_code == 403
    finally:
        r["app"].state.db = original
        replacement.engine.dispose()
    assert snapshot(r["path"]) == before and snapshot(other_path) == before


def test_demo_rejects_a_replaced_database_file_even_at_the_same_path(demo_return):
    r = demo_return; h = header(r); add(r, h); body = post_data(r, h); before = closed_file_snapshot(r["path"])
    replacement = r["path"].parent / "replacement-synthetic.sqlite"
    copy_synthetic_database(r["path"], replacement)
    r["db"].engine.dispose()
    retained = r["path"].with_name("original-synthetic.sqlite")
    r["path"].rename(retained)
    replacement.rename(r["path"])
    assert post(r, h, body).status_code == 403
    assert not r["client"].get(URL + "/policy").json()["posting_allowed"]
    assert closed_file_snapshot(r["path"]) == before and closed_file_snapshot(retained) == before
    assert_no_grant(r)


@pytest.mark.parametrize("change", ["draft", "source", "stock"])
def test_demo_confirmation_becomes_stale_after_saved_draft_source_or_stock_change(demo_return, change):
    r = demo_return; c = r["client"]; h = header(r); line = add(r, h); body = post_data(r, h)
    if change == "draft":
        response = c.put(URL + "/" + h["id"] + "/lines/" + line["id"], json=line_edit(r, h, line, quantity="0.5"))
    elif change == "source":
        raw = c.get("/api/raw-materials/" + r["raws"][0]["id"]).json()
        values = {key: raw[key] for key in ["code", "name", "specification", "category_id", "base_unit_id", "lot_tracking", "is_active", "row_version"]}
        response = c.put("/api/raw-materials/" + raw["id"], json={**values, "name": "Reviewed source renamed"})
    else:
        supplier = supplier_header(r, "SRET-DEMO-STOCK-CHANGE")
        supplier_line(r, supplier, "0.25", 1)
        response = supplier_post(r, supplier)
    assert response.status_code in {200, 201}, response.text
    before = snapshot(r["path"])
    assert post(r, h, body).status_code == 409
    assert snapshot(r["path"]) == before and current(r, h)["posting_state"] == "DRAFT"
    assert_no_grant(r)


def test_demo_same_key_replay_different_key_conflicts_and_ordinary_restart_only_reads_committed_history(demo_return):
    r = demo_return; h = header(r); add(r, h); body = post_data(r, h)
    first = post(r, h, body); assert first.status_code == 201, first.text
    before = snapshot(r["path"])
    replay = post(r, h, body)
    assert replay.status_code == 200 and replay.json()["posting_id"] == first.json()["posting_id"]
    assert post(r, h, {**body, "request_key": str(uuid4())}).status_code == 409
    assert post(r, h, {**body, "confirmation_token": "changed"}).status_code == 409
    with TestClient(create_app(r["path"])) as ordinary:
        assert not ordinary.get(URL + "/policy").json()["posting_allowed"]
        replay = ordinary.post(URL + "/" + h["id"] + "/post", json=body)
        assert replay.status_code == 200 and replay.json()["posting_id"] == first.json()["posting_id"]
    assert snapshot(r["path"]) == before and balance(r) == "3"


def test_demo_duplicate_concurrent_confirmations_commit_one_return(demo_return):
    r = demo_return; h = header(r); add(r, h); body = post_data(r, h)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(r, h, body), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201], [response.text for response in responses]
    assert len({response.json()["posting_id"] for response in responses}) == 1
    assert counts(r)["material_return_entry"] == 1 and balance(r) == "3"
    assert_no_grant(r)


def test_demo_concurrent_distinct_returns_share_one_committed_returnable_pool(demo_return):
    r = demo_return; documents = [header(r, "MRET-DEMO-COMPETE-" + str(index)) for index in range(2)]
    for document in documents:
        add(r, document, "1")
    bodies = [post_data(r, document) for document in documents]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pair: post(r, pair[0], pair[1]), zip(documents, bodies)))
    assert sorted(response.status_code for response in responses) in [[201, 409], [201, 422]], [response.text for response in responses]
    assert counts(r)["material_return_entry"] == 1 and balance(r) == "3.25"
    assert (sources(r)[0]["returned_qty_base"], sources(r)[0]["returnable_qty_base"]) == ("1", "0.75")


def test_demo_duplicate_source_lines_cannot_independently_exceed_remaining_returnable(demo_return):
    r = demo_return; h = header(r); add(r, h, "1"); add(r, h, "1")
    before = snapshot(r["path"])
    response = r["client"].get(URL + "/" + h["id"] + "/posting-review")
    assert response.status_code == 422, response.text
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


@pytest.mark.parametrize("stage", ["later_entry", "finalize"])
def test_demo_failure_rolls_back_stock_document_request_audit_and_grant(demo_return, stage):
    r = demo_return; h = header(r); add(r, h, "0.25"); last = add(r, h, "0.5"); body = post_data(r, h)
    with r["db"].write() as session:
        if stage == "later_entry":
            session.connection().exec_driver_sql(
                "CREATE TRIGGER demo_return_fail BEFORE INSERT ON material_return_entry WHEN NEW.return_line_id='" +
                last["id"] + "' BEGIN SELECT RAISE(ABORT,'injected demo return later-entry failure'); END")
        else:
            session.connection().exec_driver_sql(
                "CREATE TRIGGER demo_return_fail BEFORE UPDATE ON material_return_posting WHEN NEW.posting_state='POSTED' "
                "BEGIN SELECT RAISE(ABORT,'injected demo return finalization failure'); END")
    before = snapshot(r["path"])
    response = post(r, h, body)
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before and balance(r) == "2.25"
    assert counts(r)["material_return_posting"] == 0 and counts(r)["material_return_entry"] == 0
    assert_no_grant(r)


def test_demo_grant_has_exact_operation_document_audit_and_physical_connection_and_clears(demo_return):
    r = demo_return; h = header(r); add(r, h); body = post_data(r, h); observations = []; checking = False

    def on_insert(connection, cursor, statement, parameters, context, executemany):
        nonlocal checking
        if checking or not statement.lstrip().upper().startswith("INSERT INTO MATERIAL_RETURN_POSTING"):
            return
        checking = True
        try:
            same = connection.exec_driver_sql(
                "SELECT erp_material_return_authorized(),erp_material_return_basis(),erp_material_return_document(),"
                "erp_material_return_digest(),erp_material_return_snapshot(),erp_material_return_time()"
            ).one()
            assert same[:3] == (1, "LOCAL_DEMO_MANUAL", h["id"])
            assert same[3] == hashlib.sha256(same[4].encode()).hexdigest() and same[5]
            assert json.loads(same[4])["snapshot"]["material_return"]["id"] == h["id"]
            assert r["issue"]["posting_id"] in same[4]
            assert connection.exec_driver_sql("SELECT erp_issue_manual_authorized()").scalar() == 0
            with r["db"].engine.connect() as separate:
                assert separate.connection.driver_connection is not connection.connection.driver_connection
                other = separate.exec_driver_sql("SELECT erp_material_return_authorized()").scalar()
            observations.append((same[0], other, connection.exec_driver_sql("SELECT erp_material_return_authorized()").scalar()))
        finally:
            checking = False

    event.listen(r["db"].engine, "before_cursor_execute", on_insert)
    try:
        response = post(r, h, body)
    finally:
        event.remove(r["db"].engine, "before_cursor_execute", on_insert)
    assert response.status_code == 201, response.text
    assert observations == [(1, 0, 1)] and balance(r) == "3"
    assert_no_grant(r)


def test_demo_database_write_cannot_forge_either_local_or_qa_return_grant(demo_return):
    r = demo_return; h = header(r); add(r, h); h = current(r, h); before = snapshot(r["path"])
    for basis in ["LOCAL_DEMO_MANUAL", "CONTROLLED_QA_ONLY"]:
        with pytest.raises(IntegrityError, match="authorization context denied"):
            with r["db"].write() as session:
                parent = session.get(MaterialReturn, h["id"]); parent.row_version += 1; session.flush()
                session.add(MaterialReturnPosting(material_return_id=h["id"], idempotency_key=str(uuid4()),
                    request_fingerprint="unconfirmed direct write", source_version=h["row_version"],
                    work_order_version=h["work_order_version"], posting_state="PREPARING", authorization_basis=basis,
                    confirmation_digest="f" * 64, confirmation_snapshot="{}", confirmation_received_at=now_utc(),
                    created_by=DEMO_ACTOR_ID, posted_by=DEMO_ACTOR_ID))
                session.flush()
    assert snapshot(r["path"]) == before and balance(r) == "2.25"
    assert_no_grant(r)


def test_demo_posted_return_and_original_issue_remain_immutable(demo_return):
    r = demo_return; c = r["client"]; h = header(r); add(r, h); assert post(r, h).status_code == 201
    saved = current(r, h); line = saved["lines"][0]; before = snapshot(r["path"])
    assert c.put(URL + "/" + h["id"], json=header_edit(saved, note="forbidden")).status_code == 409
    assert c.delete(URL + "/" + h["id"], params={"row_version": saved["row_version"]}).status_code == 409
    assert c.put(URL + "/" + h["id"] + "/lines/" + line["id"], json=line_edit(r, h, line, quantity="99")).status_code == 409
    assert c.delete(URL + "/" + h["id"] + "/lines/" + line["id"], params={"row_version": line["row_version"], "document_version": saved["row_version"]}).status_code == 409
    for table in TABLES + ["material_issue_posting", "material_issue_entry"]:
        for statement in ["UPDATE " + table + " SET created_by=created_by", "DELETE FROM " + table]:
            with pytest.raises(IntegrityError):
                with r["db"].write() as session:
                    session.connection().exec_driver_sql(statement)
    assert snapshot(r["path"]) == before and balance(r) == "3"


def test_existing_manual_issue_and_return_authorities_remain_operation_specific(demo_return):
    r = demo_return; issue = issue_header(r, "ISSUE-DEMO-AFTER-RETURN-ACTIVATION"); issue_add(r, issue, "0.25")
    response = issue_execute(r, issue, confirmed_body(r, issue))
    assert response.status_code == 201, response.text
    assert response.json()["authorization_basis"] == "LOCAL_DEMO_MANUAL" and balance(r) == "2"
    h = header(r); add(r, h); assert post(r, h).status_code == 201 and balance(r) == "2.75"
    with r["db"].read() as session:
        connection = session.connection()
        assert sorted(connection.exec_driver_sql("SELECT qty_base_delta FROM material_issue_entry").scalars()) == ["-0.25", "-1.75"]
        assert connection.exec_driver_sql("SELECT qty_base_delta FROM material_return_entry").scalar() == "0.75"
    assert_no_grant(r)
