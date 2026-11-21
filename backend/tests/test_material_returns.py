from backend.tests.finished_goods_migration_assertions import assert_finished_goods_upgrade_objects
"""Operation18 invariants against fresh synthetic SQLite databases only."""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT, DEMO_ACTOR_ID, make_engine, migrate
from backend.app.decimal_text import exact_difference, exact_sum
from backend.app.main import create_app
from backend.tests.material_return_qa_runtime import (
    seed_material_return_qa_database, create_material_return_qa_app,
    new_material_return_qa_application,
)
from backend.tests.material_return_seed_fixture import clone_material_return_seed
from backend.tests.test_material_issues import (
    balance, header as issue_header, add as issue_add, post as issue_post,
    current as issue_current, line_data as issue_line_data,
)
from backend.tests.test_supplier_offers import snapshot
from backend.tests.test_supplier_returns import (
    header as supplier_header, line as supplier_line, post as supplier_post,
)
from backend.tests.test_receiving import (
    header as receipt_header, line as receipt_line, post as receipt_post,
)
from backend.tests.test_mrp import preview, material

URL = "/api/material-returns"
TABLES = ["material_return", "material_return_line", "material_return_posting", "material_return_entry"]


def parent_directory(tmp_path):
    if tmp_path.resolve().is_relative_to((ROOT / "artifacts").resolve()):
        return tmp_path
    parent = ROOT / "artifacts" / "round16-pytest"
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="test-", dir=parent))


@pytest.fixture
def qa(tmp_path, material_return_sources):
    path = parent_directory(tmp_path) / "material-return-qa.sqlite"
    metadata = clone_material_return_seed(material_return_sources, path)
    app = create_material_return_qa_app(path)
    with TestClient(app, base_url="http://127.0.0.1:8026", client=("127.0.0.1", 50000)) as client:
        yield dict(metadata, client=client, app=app, db=app.state.db, path=path)


@pytest.fixture
def ordinary(tmp_path, material_return_sources):
    path = parent_directory(tmp_path) / "ordinary.sqlite"
    metadata = clone_material_return_seed(material_return_sources, path)
    app = create_app(path)
    with TestClient(app) as client:
        yield dict(metadata, client=client, app=app, db=app.state.db, path=path)


def current(r, document):
    response = r["client"].get(URL + "/" + document["id"])
    assert response.status_code == 200, response.text
    return response.json()


def header_data(r, number="MRET-TEST-001", order=0, **patch):
    work = r["orders"][order]
    return {**dict(request_key=str(uuid4()), document_no=number, work_order_id=work["id"],
        work_order_version=work["row_version"], business_date="2026-11-20", note="實際未用材料退回"), **patch}


def header(r, number="MRET-TEST-001", order=0, **patch):
    response = r["client"].post(URL, json=header_data(r, number, order, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def header_edit(document, **patch):
    return {**{key: document[key] for key in ["document_no", "work_order_id", "work_order_version", "business_date", "note", "row_version"]}, **patch}


def line_data(r, document, quantity="0.75", **patch):
    origin = r["issue"]["lines"][0]
    return {**dict(request_key=str(uuid4()), document_version=current(r, document)["row_version"],
        origin_line_id=origin["id"], origin_line_version=origin["row_version"], quantity=quantity, note="原領料未用餘量"), **patch}


def add(r, document, quantity="0.75", **patch):
    response = r["client"].post(URL + "/" + document["id"] + "/lines", json=line_data(r, document, quantity, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def line_edit(r, document, line, **patch):
    source = line["source"]
    return {**dict(origin_line_id=source["origin_line_id"], origin_line_version=source["origin_line_version"],
        quantity=line["quantity"], note=line["note"], row_version=line["row_version"],
        document_version=current(r, document)["row_version"]), **patch}


def review(r, document):
    response = r["client"].get(URL + "/" + document["id"] + "/posting-review")
    assert response.status_code == 200, response.text
    return response.json()


def post_data(r, document, **patch):
    reviewed = review(r, document)
    parent = reviewed["material_return"]
    return {**dict(request_key=str(uuid4()), row_version=parent["row_version"],
        work_order_version=parent["work_order_version"], confirmation_token=reviewed["confirmation_token"]), **patch}


def post(r, document, body=None):
    return r["client"].post(URL + "/" + document["id"] + "/post", json=body if body is not None else post_data(r, document))


def sources(r, **params):
    response = r["client"].get(URL + "/sources", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def counts(r):
    with r["db"].read() as session:
        return {table: session.connection().exec_driver_sql("SELECT count(*) FROM " + table).scalar() for table in TABLES}


def test_draft_header_line_crud_retries_search_audit_and_persistence(ordinary):
    r = ordinary; c = r["client"]
    before = snapshot(r["path"]); original_mrp = preview(r); purchasing = c.get("/api/purchase-outstanding").json()
    body = header_data(r); response = c.post(URL, json=body)
    assert response.status_code == 201, response.text
    h = response.json()
    assert h["id"] != h["document_no"] and h["posting_state"] == "DRAFT" and h["posting_id"] is None
    assert not h["policy"]["posting_allowed"] and h["created_by"] == DEMO_ACTOR_ID
    assert c.post(URL, json=body).status_code == 200
    assert c.post(URL, json={**body, "note": "different retry"}).status_code == 409
    assert c.post(URL, json=header_data(r)).status_code == 409
    changed = c.put(URL + "/" + h["id"], json=header_edit(h, document_no="MRET-EDITED", note="更新草稿"))
    assert changed.status_code == 200, changed.text
    h = changed.json(); assert h["row_version"] == 2 and h["created_at"] == response.json()["created_at"]
    first = add(r, h); second = add(r, h, "0.25")
    edited = c.put(URL + "/" + h["id"] + "/lines/" + first["id"], json=line_edit(r, h, first, quantity="0.5"))
    assert edited.status_code == 200, edited.text
    first = edited.json(); assert first["row_version"] == 2 and first["quantity"] == "0.5"
    assert first["source"]["issue_no"] == "ISSUE-TEST-001"
    assert c.get(URL, params={"q": "MRET-EDITED"}).json()[0]["id"] == h["id"]
    assert c.get(URL, params={"q": "WO-TEST-001"}).json()[0]["id"] == h["id"]
    assert c.get(URL, params={"q": "%"}).json() == []
    assert len(c.get(URL + "/" + h["id"] + "/lines").json()) == 2
    saved = current(r, h)
    with TestClient(create_app(r["path"])) as reopened:
        assert reopened.get(URL + "/" + h["id"]).json() == saved
    assert c.delete(URL + "/" + h["id"] + "/lines/" + second["id"], params={"row_version": second["row_version"], "document_version": saved["row_version"]}).status_code == 204
    assert c.delete(URL + "/" + h["id"], params={"row_version": current(r, h)["row_version"]}).status_code == 204
    assert c.post(URL, json=body).status_code == 410
    assert balance(r) == "2.25" and preview(r)["materials"] == original_mrp["materials"]
    assert c.get("/api/purchase-outstanding").json() == purchasing
    after = snapshot(r["path"])
    assert all(after["rows"][table] == rows for table, rows in before["rows"].items() if table != "stock_create_receipt")


@pytest.mark.parametrize("quantity", ["0", "-0.75", "0." + "0" * 100 + "12345678901234567890123456789", "9" * 5000])
def test_drafts_preserve_signed_zero_tiny_large_exact_values_without_moving_stock(ordinary, quantity):
    r = ordinary; h = header(r); line = add(r, h, quantity)
    assert line["quantity"] == quantity and balance(r) == "2.25"
    before = snapshot(r["path"])
    state = current(r, h)
    response = post(r, h, dict(request_key=str(uuid4()), row_version=state["row_version"], work_order_version=state["work_order_version"]))
    assert response.status_code == 403 and snapshot(r["path"]) == before


def test_source_options_only_genuine_posted_issue_and_exact_provenance(qa):
    r = qa; before = snapshot(r["path"])
    options = sources(r, work_order_id=r["orders"][0]["id"])
    assert len(options) == 1
    source = options[0]; origin = r["issue"]["lines"][0]
    assert (source["origin_line_id"], source["issue_id"], source["issue_posting_id"]) == (origin["id"], r["issue"]["id"], r["issue"]["posting_id"])
    assert (source["issued_qty_base"], source["returned_qty_base"], source["returnable_qty_base"]) == ("1.75", "0", "1.75")
    assert source["position"]["id"] == origin["position_id"] and source["position"]["lot_code"] == "LOT-R8-001"
    assert len(source["position"]["receipt_origins"]) == 2
    assert sources(r, work_order_id=r["orders"][1]["id"]) == []
    assert sources(r, q="%") == [] and snapshot(r["path"]) == before


@pytest.mark.parametrize("patch", [
    {"origin_line_id": str(uuid4())}, {"origin_line_version": 0}, {"quantity": 0.75}, {"quantity": "NaN"},
    {"material_id": str(uuid4())}, {"warehouse_id": str(uuid4())}, {"position_id": str(uuid4())}, {"lot_code": "OTHER-LOT"},
])
def test_invalid_or_client_substituted_sources_reject_atomically(qa, patch):
    r = qa; h = header(r); before = snapshot(r["path"])
    response = r["client"].post(URL + "/" + h["id"] + "/lines", json=line_data(r, h, **patch))
    assert response.status_code in {409, 422}, response.text
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


def test_wrong_work_order_and_unposted_issue_line_are_not_return_sources(qa):
    r = qa; wrong = header(r, order=1)
    draft = issue_header(r, "ISSUE-NOT-POSTED")
    unposted = issue_add(r, draft, "0.25")
    before = snapshot(r["path"])
    assert r["client"].post(URL + "/" + wrong["id"] + "/lines", json=line_data(r, wrong)).status_code == 422
    own = header(r, "MRET-INVALID-SOURCE")
    before = snapshot(r["path"])
    response = r["client"].post(URL + "/" + own["id"] + "/lines", json=line_data(r, own, origin_line_id=unposted["id"], origin_line_version=unposted["row_version"]))
    assert response.status_code == 422 and snapshot(r["path"]) == before


def test_stale_header_line_source_and_cross_parent_edits_preserve_saved_draft(qa):
    r = qa; c = r["client"]; h = header(r); line = add(r, h); other = header(r, "MRET-OTHER")
    before = snapshot(r["path"])
    assert c.put(URL + "/" + h["id"], json=header_edit(h, note="stale")).status_code == 409
    assert c.put(URL + "/" + h["id"] + "/lines/" + line["id"], json=line_edit(r, h, line, row_version=0)).status_code == 409
    assert c.put(URL + "/" + h["id"] + "/lines/" + line["id"], json=line_edit(r, h, line, origin_line_version=0)).status_code == 409
    assert c.put(URL + "/" + other["id"] + "/lines/" + line["id"], json=line_edit(r, other, line)).status_code == 404
    assert c.put(URL + "/" + h["id"], json=header_edit(current(r, h), work_order_id=r["orders"][1]["id"], work_order_version=r["orders"][1]["row_version"])).status_code == 409
    assert snapshot(r["path"]) == before


def test_review_readonly_actual_plus075_stock3_mrp_preliminary_and_original_issue_unchanged(qa):
    r = qa; c = r["client"]; h = header(r); line = add(r, h)
    before = snapshot(r["path"]); buying = c.get("/api/purchase-outstanding").json(); original_issue = issue_current(r, r["issue"])
    data = review(r, h)
    assert snapshot(r["path"]) == before and balance(r) == "2.25"
    movement = data["expected_movements"][0]
    assert movement["return_line_id"] == line["id"] and movement["qty_base_delta"] == "0.75"
    assert (movement["current_stock_qty_base"], movement["expected_stock_qty_base"]) == ("2.25", "3")
    assert data["confirmation_token"] and data["material_return"]["policy"]["authorization_basis"] == "CONTROLLED_QA_ONLY"
    response = post(r, h); assert response.status_code == 201, response.text
    posted = response.json(); assert posted["posting_state"] == "POSTED" and balance(r) == "3"
    assert posted["movements"][0]["qty_base_delta"] == "0.75" and posted["movements"][0]["return_line_id"] == line["id"]
    source = sources(r)[0]
    assert (source["issued_qty_base"], source["returned_qty_base"], source["returnable_qty_base"]) == ("1.75", "0.75", "1")
    assert exact_difference(source["issued_qty_base"], source["returned_qty_base"]) == "1"
    planning = preview(r); raw = material(planning, r)
    assert planning["preliminary"] and (raw["gross_demand_qty_base"], raw["physical_stock_qty_base"], raw["shortage_qty_base"]) == ("11.5625", "3", "8.5625")
    assert issue_current(r, r["issue"])["movements"] == original_issue["movements"]
    assert c.get("/api/purchase-outstanding").json() == buying
    with r["db"].read() as session:
        connection = session.connection()
        assert connection.exec_driver_sql("SELECT qty_base_delta FROM material_issue_entry").scalar() == "-1.75"
        assert connection.exec_driver_sql("SELECT count(*) FROM material_return_entry").scalar() == 1
        assert connection.exec_driver_sql("SELECT typeof(qty_base_delta) FROM material_return_entry").scalar() == "text"
        assert connection.exec_driver_sql("SELECT count(*) FROM stock_posting").scalar() == 2
        assert sorted(connection.exec_driver_sql("SELECT qty_base_delta FROM stock_entry").scalars()) == ["4", "6"]
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []


def test_repeated_partial_returns_count_posted_only_and_aggregate_sources(qa):
    r = qa; draft = header(r, "MRET-SAVED-NOT-COUNTED"); add(r, draft, "1.75")
    assert sources(r)[0]["returned_qty_base"] == "0"
    first = header(r, "MRET-PART1"); add(r, first, "0.25"); add(r, first, "0.5")
    reviewed = review(r, first)
    assert {m["expected_stock_qty_base"] for m in reviewed["expected_movements"]} == {"3"}
    assert post(r, first).status_code == 201 and balance(r) == "3"
    second = header(r, "MRET-PART2"); add(r, second, "0.125")
    assert post(r, second).status_code == 201 and balance(r) == "3.125"
    result = sources(r)[0]
    assert (result["returned_qty_base"], result["returnable_qty_base"]) == ("0.875", "0.875")
    assert current(r, draft)["posting_state"] == "DRAFT" and counts(r)["material_return_entry"] == 3


@pytest.mark.parametrize("quantities", [["1", "1"], ["1.750000000000000000000000000000000001"]])
def test_overreturn_rejection_is_atomic_for_combined_lines_and_exact_fraction(qa, quantities):
    r = qa; h = header(r)
    for quantity in quantities: add(r, h, quantity)
    before = snapshot(r["path"])
    response = r["client"].get(URL + "/" + h["id"] + "/posting-review")
    if response.status_code == 200:
        reviewed = response.json(); parent = reviewed["material_return"]
        response = post(r, h, dict(request_key=str(uuid4()), row_version=parent["row_version"], work_order_version=parent["work_order_version"], confirmation_token=reviewed["confirmation_token"]))
    assert response.status_code == 422, response.text
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


@pytest.mark.parametrize("quantity", ["0", "-0.75"])
def test_actual_inbound_direction_rejects_zero_negative_without_restricting_draft(qa, quantity):
    r = qa; h = header(r); saved = add(r, h, quantity); before = snapshot(r["path"])
    assert saved["quantity"] == quantity
    response = r["client"].get(URL + "/" + h["id"] + "/posting-review")
    assert response.status_code == 422 and snapshot(r["path"]) == before


def test_small_exact_positive_post_preserves_every_digit_without_rounding(qa):
    r = qa; quantity = "0." + "0" * 90 + "12345678901234567890123456789"
    h = header(r); add(r, h, quantity); response = post(r, h)
    assert response.status_code == 201, response.text
    assert response.json()["movements"][0]["qty_base_delta"] == quantity
    assert balance(r) == exact_sum(["2.25", quantity])
    assert sources(r)[0]["returnable_qty_base"] == exact_difference("1.75", quantity)


def test_same_original_request_retries_conflicts_and_normal_restart_only_report_history(qa):
    r = qa; h = header(r); add(r, h); body = post_data(r, h)
    response = post(r, h, body); assert response.status_code == 201, response.text
    before = snapshot(r["path"])
    assert post(r, h, body).status_code == 200
    for changed in [{"request_key": str(uuid4())}, {"row_version": 0}, {"confirmation_token": "different"}]:
        assert post(r, h, {**body, **changed}).status_code == 409
    assert snapshot(r["path"]) == before and balance(r) == "3"
    with TestClient(create_app(r["path"])) as restarted:
        assert restarted.get(URL + "/policy").json()["posting_allowed"] is False
        replay = restarted.post(URL + "/" + h["id"] + "/post", json=body)
        assert replay.status_code == 200 and replay.json()["posting_id"] == response.json()["posting_id"]
    assert snapshot(r["path"]) == before
    other = header(r, "MRET-CONFLICTING-KEY"); add(r, other, "0.25")
    before = snapshot(r["path"])
    assert post(r, other, post_data(r, other, request_key=body["request_key"])).status_code == 409
    assert snapshot(r["path"]) == before and balance(r) == "3"


def test_same_document_concurrent_original_request_commits_exactly_once(qa):
    r = qa; h = header(r); add(r, h); body = post_data(r, h)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(r, h, body), range(2)))
    assert sorted(result.status_code for result in responses) == [200, 201], [result.text for result in responses]
    assert len({result.json()["posting_id"] for result in responses}) == 1
    assert counts(r)["material_return_entry"] == 1 and balance(r) == "3"


def test_concurrent_distinct_returns_cannot_jointly_exceed_one_issue_source(qa):
    r = qa; documents = [header(r, "MRET-COMPETE-" + str(index)) for index in range(2)]
    for document in documents: add(r, document, "1")
    bodies = [post_data(r, document) for document in documents]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pair: post(r, pair[0], pair[1]), zip(documents, bodies)))
    assert sorted(result.status_code for result in responses) in [[201, 409], [201, 422]], [result.text for result in responses]
    assert balance(r) == "3.25" and counts(r)["material_return_entry"] == 1
    assert (sources(r)[0]["returned_qty_base"], sources(r)[0]["returnable_qty_base"]) == ("1", "0.75")


def test_readonly_default_review_does_not_authorize_return(ordinary):
    r = ordinary; h = header(r); add(r, h); before = snapshot(r["path"])
    result = review(r, h)
    assert result["confirmation_token"] is None and not result["material_return"]["policy"]["posting_allowed"]
    assert post(r, h).status_code == 403 and snapshot(r["path"]) == before
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


@pytest.mark.parametrize("attack", ["query", "headers", "body", "environment"])
def test_http_flags_environment_and_ordinary_database_writes_cannot_enable_live_gate(ordinary, monkeypatch, attack):
    r = ordinary; h = header(r); add(r, h); body = post_data(r, h); before = snapshot(r["path"])
    suffix = ""; headers = {}
    if attack == "query": suffix = "?authorized=true&qa=true&local_demo=true&operation=material-return"
    if attack == "headers": headers = {"X-QA-Authorized": "true", "X-ERP-Return-Posting": "1"}
    if attack == "body": body["authorized"] = True
    if attack == "environment":
        monkeypatch.setenv("ERP_RETURN_AUTHORIZED", "1"); monkeypatch.setenv("ERP_QA_POSTING", "true")
    response = r["client"].post(URL + "/" + h["id"] + "/post" + suffix, json=body, headers=headers)
    assert response.status_code == (422 if attack == "body" else 403)
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("stage", ["second_entry", "finalize"])
def test_injected_mid_and_final_failure_rolls_back_stock_versions_idempotency_audit_and_grant(qa, stage):
    r = qa; h = header(r); add(r, h, "0.25"); last = add(r, h, "0.5"); body = post_data(r, h)
    with r["db"].write() as session:
        if stage == "second_entry":
            session.connection().exec_driver_sql("CREATE TRIGGER qa_return_fail BEFORE INSERT ON material_return_entry WHEN NEW.return_line_id='" + last["id"] + "' BEGIN SELECT RAISE(ABORT,'injected return second-entry failure'); END")
        else:
            session.connection().exec_driver_sql("CREATE TRIGGER qa_return_fail BEFORE UPDATE ON material_return_posting WHEN NEW.posting_state='POSTED' BEGIN SELECT RAISE(ABORT,'injected return finalization failure'); END")
    before = snapshot(r["path"])
    response = post(r, h, body)
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before and balance(r) == "2.25"
    with r["db"].read() as session:
        connection = session.connection()
        assert connection.exec_driver_sql("SELECT count(*) FROM material_return_posting WHERE posting_state='PREPARING'").scalar() == 0
        assert connection.exec_driver_sql("SELECT count(*) FROM material_return_posting WHERE confirmation_digest IS NOT NULL").scalar() == 0
        values = connection.exec_driver_sql("SELECT erp_material_return_authorized(),erp_material_return_basis(),erp_material_return_document(),erp_material_return_digest(),erp_material_return_snapshot(),erp_material_return_time()").one()
        assert tuple(values) == (0, None, None, None, None, None)


def test_posted_return_and_issue_history_resist_api_sql_update_delete_and_replace(qa):
    r = qa; c = r["client"]; h = header(r); add(r, h); assert post(r, h).status_code == 201
    saved = current(r, h); line = saved["lines"][0]; before = snapshot(r["path"])
    assert c.put(URL + "/" + h["id"], json=header_edit(saved, note="forbidden")).status_code == 409
    assert c.delete(URL + "/" + h["id"], params={"row_version": saved["row_version"]}).status_code == 409
    assert c.put(URL + "/" + h["id"] + "/lines/" + line["id"], json=line_edit(r, h, line, quantity="99")).status_code == 409
    assert c.delete(URL + "/" + h["id"] + "/lines/" + line["id"], params={"row_version": line["row_version"], "document_version": saved["row_version"]}).status_code == 409
    for table in TABLES + ["material_issue", "material_issue_line", "material_issue_posting", "material_issue_entry"]:
        for verb in ["UPDATE", "DELETE", "REPLACE"]:
            with pytest.raises(IntegrityError):
                with r["db"].write() as session:
                    connection = session.connection()
                    if verb == "UPDATE": sql = "UPDATE " + table + " SET created_by=created_by"
                    elif verb == "DELETE": sql = "DELETE FROM " + table
                    else:
                        names = ",".join(row[1] for row in connection.exec_driver_sql("PRAGMA table_info(" + table + ")"))
                        sql = "INSERT OR REPLACE INTO " + table + " (" + names + ") SELECT " + names + " FROM " + table + " LIMIT 1"
                    connection.exec_driver_sql(sql)
    origin = r["issue"]["lines"][0]
    for table, identity in [("work_order", saved["work_order_id"]), ("item", origin["material_id"]), ("warehouse", origin["warehouse_id"]), ("stock_position", origin["position_id"]), ("inventory_lot", origin["position"]["lot_id"])]:
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:
                session.connection().exec_driver_sql("DELETE FROM " + table + " WHERE id=?", (identity,))
    assert snapshot(r["path"]) == before and balance(r) == "3"


def test_qa_capability_rejects_owner_nonartifact_and_existing_seed(tmp_path, qa):
    with pytest.raises(ValueError): create_material_return_qa_app(ROOT / "data/erp.sqlite")
    with pytest.raises(ValueError): create_material_return_qa_app(ROOT / "backend/nonartifact.sqlite")
    with pytest.raises(ValueError): seed_material_return_qa_database(qa["path"])
    with pytest.raises(ValueError): create_material_return_qa_app(qa["path"], bind_host="0.0.0.0")


@pytest.mark.parametrize("change", ["line", "parent", "material_source", "physical_stock", "already_returned"])
def test_signed_review_detects_actual_draft_source_stock_and_cumulative_return_changes(qa, change):
    r = qa; c = r["client"]; h = header(r); line = add(r, h); body = post_data(r, h)
    if change == "line":
        response = c.put(URL + "/" + h["id"] + "/lines/" + line["id"], json=line_edit(r, h, line, quantity="0.5"))
    elif change == "parent":
        response = c.put(URL + "/" + h["id"], json=header_edit(current(r, h), note="new saved note"))
    elif change == "material_source":
        raw = c.get("/api/raw-materials/" + r["raws"][0]["id"]).json()
        values = {key: raw[key] for key in ["code", "name", "specification", "category_id", "base_unit_id", "lot_tracking", "is_active", "row_version"]}
        response = c.put("/api/raw-materials/" + raw["id"], json={**values, "name": "已重新核對材料名稱"})
    elif change == "physical_stock":
        supplier = supplier_header(r, "SRET-AFTER-REVIEW"); supplier_line(r, supplier, "0.25", 1)
        response = supplier_post(r, supplier)
    else:
        other = header(r, "MRET-AFTER-REVIEW"); add(r, other, "0.25"); response = post(r, other)
    assert response.status_code in {200, 201}, response.text
    before = snapshot(r["path"])
    assert post(r, h, body).status_code == 409 and snapshot(r["path"]) == before
    assert current(r, h)["posting_state"] == "DRAFT"


def test_signed_confirmation_requires_matching_runtime_host_origin_and_untampered_token(qa):
    r = qa; h = header(r); add(r, h); body = post_data(r, h); before = snapshot(r["path"])
    for token in [None, "forged", body["confirmation_token"] + "tampered"]:
        response = post(r, h, {**body, "confirmation_token": token})
        assert response.status_code == 403, response.text
    for origin in ["https://foreign.example", "http://127.0.0.1:9999", "null"]:
        response = r["client"].post(URL + "/" + h["id"] + "/post", json=body, headers={"Origin": origin})
        assert response.status_code == 403, response.text
    for base, peer in [("http://127.0.0.1:9999", ("127.0.0.1", 50001)), ("http://foreign.example:8026", ("127.0.0.1", 50001)), ("http://127.0.0.1:8026", ("198.51.100.24", 50001))]:
        client = TestClient(r["app"], base_url=base, client=peer)
        try:
            assert client.post(URL + "/" + h["id"] + "/post", json=body).status_code in {400, 403}
        finally: client.close()
    fresh = create_material_return_qa_app(r["path"])
    with TestClient(fresh, base_url="http://127.0.0.1:8026", client=("127.0.0.1", 50002)) as client:
        assert client.post(URL + "/" + h["id"] + "/post", json=body).status_code == 403
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


def test_operation_specific_grant_is_exact_connection_and_document_and_clears_after_commit(qa):
    import json
    r = qa; h = header(r); add(r, h); body = post_data(r, h); observations = []; checking = False

    def on_insert(connection, cursor, statement, parameters, context, executemany):
        nonlocal checking
        if checking or not statement.lstrip().upper().startswith("INSERT INTO MATERIAL_RETURN_POSTING"):
            return
        checking = True
        try:
            same = connection.exec_driver_sql("SELECT erp_material_return_authorized(),erp_material_return_document(),erp_material_return_snapshot()").one()
            assert same[1] == h["id"] and h["id"] in same[2] and r["issue"]["posting_id"] in same[2]
            assert isinstance(json.loads(same[2]), dict)
            with r["db"].engine.connect() as separate:
                assert separate.connection.driver_connection is not connection.connection.driver_connection
                other = separate.exec_driver_sql("SELECT erp_material_return_authorized()").scalar()
            observations.append((same[0], other, connection.exec_driver_sql("SELECT erp_material_return_authorized()").scalar()))
        finally: checking = False

    event.listen(r["db"].engine, "before_cursor_execute", on_insert)
    try: response = post(r, h, body)
    finally: event.remove(r["db"].engine, "before_cursor_execute", on_insert)
    assert response.status_code == 201, response.text
    assert observations == [(1, 0, 1)]
    with r["db"].read() as session:
        values = session.connection().exec_driver_sql("SELECT erp_material_return_authorized(),erp_material_return_basis(),erp_material_return_document(),erp_material_return_digest(),erp_material_return_snapshot(),erp_material_return_time()").one()
        assert tuple(values) == (0, None, None, None, None, None)


@pytest.mark.parametrize("runtime", ["qa", "ordinary"])
def test_direct_sql_posting_cannot_borrow_runtime_or_write_authorization_values(request, runtime):
    from backend.app.material_return_models import MaterialReturn, MaterialReturnPosting
    from backend.app.models import now_utc
    r = request.getfixturevalue(runtime); h = header(r); add(r, h); h = current(r, h); before = snapshot(r["path"])
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:
            parent = session.get(MaterialReturn, h["id"]); parent.row_version += 1; session.flush()
            session.add(MaterialReturnPosting(material_return_id=h["id"], idempotency_key=str(uuid4()),
                request_fingerprint="ordinary database write", source_version=h["row_version"], work_order_version=h["work_order_version"],
                posting_state="PREPARING", authorization_basis="CONTROLLED_QA_ONLY", confirmation_digest="f" * 64,
                confirmation_snapshot="{}", confirmation_received_at=now_utc(), created_by=DEMO_ACTOR_ID, posted_by=DEMO_ACTOR_ID))
            session.flush()
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


def test_cross_domain_keys_cannot_create_return_or_spend_posted_return_key(qa):
    r = qa; h = header(r); add(r, h)
    with r["db"].read() as session:
        keys = [session.connection().exec_driver_sql("SELECT idempotency_key FROM " + table + " LIMIT 1").scalar() for table in ["stock_posting", "supplier_return_posting", "material_issue_posting"]]
    before = snapshot(r["path"])
    for key in keys:
        assert post(r, h, post_data(r, h, request_key=key)).status_code == 409
    assert snapshot(r["path"]) == before
    body = post_data(r, h); assert post(r, h, body).status_code == 201
    supplier = supplier_header(r, "SRET-RETURN-KEY"); supplier_line(r, supplier, "0.25", 1)
    receipt = receipt_header(r, "RCV-RETURN-KEY"); receipt_line(r, receipt, "0.25", lot_code="LOT-R8-001", supplier_lot_code="SUPPLIER-REAL-LOT")
    before = snapshot(r["path"])
    supplier_state = r["client"].get("/api/supplier-returns/" + supplier["id"]).json()
    assert supplier_post(r, supplier, dict(request_key=body["request_key"], row_version=supplier_state["row_version"])).status_code == 409
    receipt_state = r["client"].get("/api/purchase-receipts/" + receipt["id"]).json()
    response = r["client"].post("/api/purchase-receipts/" + receipt["id"] + "/post", json=dict(request_key=body["request_key"], row_version=receipt_state["row_version"]))
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before and balance(r) == "3"


def test_concurrent_unique_header_and_line_create_retries_have_one_identity(qa):
    r = qa; c = r["client"]; body = header_data(r)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: c.post(URL, json=body), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert len({response.json()["id"] for response in responses}) == 1
    h = responses[0].json(); line_body = line_data(r, h)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: c.post(URL + "/" + h["id"] + "/lines", json=line_body), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert len({response.json()["id"] for response in responses}) == 1
    before = snapshot(r["path"])
    assert c.post(URL + "/" + h["id"] + "/lines", json={**line_body, "quantity": "0.5"}).status_code == 409
    assert c.post(URL, json=header_data(r)).status_code == 409
    assert snapshot(r["path"]) == before and balance(r) == "2.25"


def test_same_material_distinct_lots_issue_lines_and_work_orders_keep_separate_returnable_pools(tmp_path):
    extra = {}
    def prepare(r):
        receipt = receipt_header(r, "RCV-SECOND-LOT")
        receipt_line(r, receipt, "1.5", lot_code="LOT-DISTINCT")
        response = receipt_post(r, receipt); assert response.status_code == 201, response.text
        positions = r["client"].get("/api/material-issues/stock-options").json()
        position = next(row for row in positions if row["lot_code"] == "LOT-DISTINCT")
        issue = issue_header(r, "ISSUE-DISTINCT-LOT")
        issue_add(r, issue, "0.5", position_id=position["id"])
        response = issue_post(r, issue); assert response.status_code == 201, response.text
        extra["lot_issue"] = issue_current(r, issue)
        issue = issue_header(r, "ISSUE-OTHER-WORK-ORDER", order=1)
        issue_add(r, issue, "0.25")
        response = issue_post(r, issue); assert response.status_code == 201, response.text
        extra["other_work_issue"] = issue_current(r, issue)
    app, path, metadata = new_material_return_qa_application(parent_directory(tmp_path), prepare=prepare)
    with TestClient(app, base_url="http://127.0.0.1:8026", client=("127.0.0.1", 50000)) as client:
        r = dict(metadata, client=client, app=app, db=app.state.db, path=path)
        h = header(r); add(r, h, "0.25")
        lot_source = extra["lot_issue"]["lines"][0]; wrong_work_source = extra["other_work_issue"]["lines"][0]
        add(r, h, "0.125", origin_line_id=lot_source["id"], origin_line_version=lot_source["row_version"])
        before = snapshot(path)
        response = client.post(URL + "/" + h["id"] + "/lines", json=line_data(r, h, "0.125", origin_line_id=wrong_work_source["id"], origin_line_version=wrong_work_source["row_version"]))
        assert response.status_code == 422 and snapshot(path) == before
        response = post(r, h); assert response.status_code == 201, response.text
        assert balance(r) == "2.25"
        distinct = next(row for row in client.get("/api/stock-positions").json() if row["lot_code"] == "LOT-DISTINCT")
        assert distinct["on_hand_qty_base"] == "1.125"
        contexts = {row["origin_line_id"]: row for row in sources(r)}
        assert (contexts[r["issue"]["lines"][0]["id"]]["returned_qty_base"], contexts[r["issue"]["lines"][0]["id"]]["returnable_qty_base"]) == ("0.25", "1.5")
        assert (contexts[lot_source["id"]]["returned_qty_base"], contexts[lot_source["id"]]["returnable_qty_base"]) == ("0.125", "0.375")
        assert (contexts[wrong_work_source["id"]]["returned_qty_base"], contexts[wrong_work_source["id"]]["returnable_qty_base"]) == ("0", "0.25")


def test_supplier_return_guards_consume_restored_physical_stock_once_and_preserve_purchase_sources(qa):
    from backend.app.return_models import SupplierReturn, SupplierReturnPosting
    r = qa; h = header(r); add(r, h); assert post(r, h).status_code == 201
    buying = r["client"].get("/api/purchase-outstanding").json()
    assert [order["total_amount"] for order in buying["orders"]] == ["156", "36.25"]
    assert buying["quantity_totals"][0]["outstanding_qty_base"] == "7"
    doc = supplier_header(r, "SRET-RESTORED-STOCK"); supplier_line(r, doc, "3", 1)
    assert supplier_post(r, doc).status_code == 201 and balance(r) == "0"
    # No purchase receipt or original issue/return provenance is rewritten.
    with r["db"].read() as session:
        connection = session.connection()
        assert connection.exec_driver_sql("SELECT count(*) FROM material_return_entry").scalar() == 1
        assert connection.exec_driver_sql("SELECT qty_base_delta FROM material_return_entry").scalar() == "0.75"
        assert connection.exec_driver_sql("SELECT qty_base_delta FROM material_issue_entry").scalar() == "-1.75"
        assert sorted(connection.exec_driver_sql("SELECT qty_base_delta FROM stock_entry").scalars()) == ["4", "6"]
    # A source with receipt remainder cannot authorize spending absent stock.
    excess = supplier_header(r, "SRET-NO-DOUBLE-RESTORE"); supplier_line(r, excess, "0.125", 0)
    before = snapshot(r["path"])
    assert supplier_post(r, excess).status_code == 422 and snapshot(r["path"]) == before
    with pytest.raises(IntegrityError, match="physical stock insufficient"):
        with r["db"].write() as session:
            parent = session.get(SupplierReturn, excess["id"]); version = parent.row_version; parent.row_version += 1; session.flush()
            session.add(SupplierReturnPosting(supplier_return_id=parent.id, idempotency_key=str(uuid4()),
                request_fingerprint="database physical-stock probe", source_version=version,
                posting_state="PREPARING", created_by=DEMO_ACTOR_ID, posted_by=DEMO_ACTOR_ID))
            session.flush()
    assert snapshot(r["path"]) == before


def test_additive_0012_migration_preserves_old_rows_and_rolls_back_late_failure(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from backend.app.db import Database
    from backend.app import main
    from backend.tests.test_foundation import resources, payload
    path = parent_directory(tmp_path) / "genuine0011.sqlite"
    engine = make_engine(path)
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    with engine.connect().execution_options(erp_write=True) as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0011_local_demo_issues")
    engine.dispose()
    # Create real old-schema rows through existing APIs while pinning startup
    # to that verified historical head. No source posting is fabricated here.
    with monkeypatch.context() as patch:
        patch.setattr(main, "migrate", lambda _: None)
        with TestClient(create_app(path)) as client:
            category, unit = resources(client)
            response = client.post("/api/raw-materials", json=payload(category, unit, "PRE0012-RAW"))
            assert response.status_code == 201, response.text

    def complete_snapshot():
        connection = sqlite3.connect(path)
        try:
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return dict(rows={name: connection.execute('SELECT rowid,* FROM "' + name + '" ORDER BY rowid').fetchall() for name in names},
                objects=connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall())
        finally: connection.close()

    before = complete_snapshot()
    assert before["rows"]["alembic_version"][0][1] == "0011_local_demo_issues" and len(before["rows"]) == 35
    module = import_module("backend.migrations.versions.0012_material_returns")
    original = module.op.execute; reached = []
    def late_failure(statement, *args, **kwargs):
        result = original(statement, *args, **kwargs)
        if "CREATE TRIGGER material_return_posting_finalize" in str(statement):
            connection = module.op.get_bind()
            assert connection.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name='material_return_entry'").scalar() == 1
            assert "MATERIAL_RETURN" in connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='physical_stock_ledger'").scalar()
            reached.append(True)
            raise RuntimeError("injected late material-return migration failure")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(module.op, "execute", late_failure)
        with pytest.raises(RuntimeError, match="late material-return"):
            migrate(path)
    assert reached and complete_snapshot() == before
    migrate(path); migrate(path)
    after = complete_snapshot()
    assert len(after["rows"]) == 63 and set(after["rows"]) - set(before["rows"]) == set(TABLES + ["production_completion", "finished_goods_receipt", "finished_goods_posting", "finished_goods_entry", "sales_order", "sales_order_line", "sales_create_receipt", "sales_shipment", "sales_shipment_posting", "sales_shipment_entry", "customer_return", "customer_return_finalization", "customer_return_entry", "stocktake", "stocktake_line", "stocktake_exception", "stocktake_posting", "stocktake_entry", "inventory_adjustment", "inventory_adjustment_posting", "inventory_adjustment_entry", "inventory_transfer", "inventory_transfer_posting", "inventory_transfer_entry"])
    assert all(after["rows"][name] == rows for name, rows in before["rows"].items() if name != "alembic_version")
    assert_finished_goods_upgrade_objects(before, after)
    database = Database(path)
    try:
        with database.read() as session:
            connection = session.connection()
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0020_inventory_transfers"
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []
            assert connection.exec_driver_sql("SELECT erp_material_return_authorized()").scalar() == 0
    finally: database.engine.dispose()
