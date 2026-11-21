from backend.tests.finished_goods_migration_assertions import assert_finished_goods_upgrade_objects
"""Noninventory completion events against fresh isolated SQLite databases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from importlib import import_module
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from backend.app.db import ROOT, Database, migrate
from backend.app.main import create_app
from backend.app.local_demo import create_local_demo_app
from backend.app.completion_models import ProductionCompletion
from backend.app.completions import progress_values
from backend.tests.test_foundation import resources, payload
from backend.tests.test_supplier_offers import snapshot

URL = "/api/production-completions"


def seed_small(client):
    cat, unit = resources(client)
    product = client.post("/api/finished-goods", json=payload(cat, unit, "FG-QA")).json()
    orders = []
    for no, qty in [("QA-WO-A", "2.5"), ("QA-WO-B", "3.75")]:
        response = client.post("/api/work-orders", json=dict(request_key=str(uuid4()), document_no=no,
            product_id=product["id"], planned_qty_base=qty, planned_start="2026-11-20", planned_finish="2026-11-21"))
        assert response.status_code == 201, response.text
        orders.append(response.json())
    return dict(product=product, orders=orders)


@pytest.fixture
def completion(tmp_path):
    parent = tmp_path.resolve()
    if not parent.is_relative_to(ROOT / "artifacts"):
        base = ROOT / "artifacts" / "round17-targeted"
        base.mkdir(exist_ok=True)
        parent = Path(tempfile.mkdtemp(dir=base))
    path = parent / "completion.sqlite"
    app = create_local_demo_app(path, backend_port=8030, frontend_port=5193)
    with TestClient(app, base_url="http://127.0.0.1:8030", client=("127.0.0.1", 50000)) as client:
        yield dict(client=client, app=app, db=app.state.db, path=path, **seed_small(client))


def body(r, number="QA-COMP", order=0, **patch):
    w = r["orders"][order]
    return dict(request_key=str(uuid4()), document_no=number, work_order_id=w["id"], work_order_version=w["row_version"],
        report_date="2026-11-20", good_qty_base="1.25", defective_qty_base="0.25", note="獨立完工回報 QA", **patch)


def create(r, number="QA-COMP", order=0, **patch):
    data = body(r, number, order)
    data.update(patch)
    response = r["client"].post(URL, json=data)
    assert response.status_code == 201, response.text
    return response.json()


def edit(row, **patch):
    return dict({k: row[k] for k in ["document_no", "work_order_id", "report_date", "good_qty_base", "defective_qty_base", "note", "row_version"]},
        work_order_version=row["work_order"]["row_version"], **patch)


def confirm_body(r, row):
    review = r["client"].get(URL + "/" + row["id"] + "/confirmation-review")
    assert review.status_code == 200, review.text
    return dict(request_key=str(uuid4()), row_version=row["row_version"], confirmation_token=review.json()["confirmation_token"])


def confirm(r, row, data=None):
    return r["client"].post(URL + "/" + row["id"] + "/confirm", json=data or confirm_body(r, row))


def progress(r, order=0):
    return r["client"].get(URL + "/work-orders/" + r["orders"][order]["id"] + "/progress").json()


def test_draft_crud_exact_signed_source_identity_uniqueness_and_replay(completion):
    r = completion; c = r["client"]
    b = body(r); b.update(good_qty_base="-0.0000000000000000000000000000000000001", defective_qty_base="0")
    h = c.post(URL, json=b).json()
    assert h["good_qty_base"] == b["good_qty_base"] and h["state"] == "DRAFT"
    assert h["product_id"] == r["product"]["id"] and h["unit_id"] == r["product"]["base_unit_id"]
    assert c.post(URL, json=b).status_code == 200
    assert c.post(URL, json={**b, "note": "different"}).status_code == 409
    assert c.post(URL, json={**b, "request_key": str(uuid4())}).status_code == 409
    assert c.get(URL + "?q=獨立").json()[0]["id"] == h["id"]
    assert c.get(URL + "?q=%").json() == []
    assert progress(r)["confirmed_good_qty_base"] == "0"
    updated = c.put(URL + "/" + h["id"], json=edit(h, good_qty_base="0.123456789012345678901234567890123456789")).json()
    assert updated["row_version"] == 2 and updated["created_at"] == h["created_at"]
    assert c.put(URL + "/" + h["id"], json=edit(h)).status_code == 409
    assert c.delete(URL + "/" + h["id"] + "?row_version=1").status_code == 409
    assert c.delete(URL + "/" + h["id"] + "?row_version=2").status_code == 204
    assert c.post(URL, json=b).status_code == 410
    assert progress(r)["confirmed_report_count"] == 0


def test_confirmation_example_atomic_audit_retry_and_immutability(completion):
    r = completion; c = r["client"]; h = create(r); b = confirm_body(r, h)
    before = snapshot(r["path"])
    assert c.get(URL + "/" + h["id"] + "/confirmation-review").status_code == 200
    assert snapshot(r["path"]) == before
    response = confirm(r, h, b); assert response.status_code == 201, response.text
    posted = response.json()
    assert posted["state"] == "CONFIRMED" and posted["confirmation_id"] == b["request_key"]
    assert posted["authorization_basis"] == "LOCAL_DEMO_MANUAL" and posted["confirmed_at"] and posted["confirmed_by"]
    p = progress(r)
    assert [p[k] for k in ["confirmed_good_qty_base", "confirmed_defective_qty_base", "total_reported_qty_base", "progress_percent", "remaining_planned_good_qty_base"]] == ["1.25", "0.25", "1.5", "50", "1.25"]
    after = snapshot(r["path"])
    assert confirm(r, h, b).status_code == 200 and snapshot(r["path"]) == after
    assert confirm(r, h, {**b, "row_version": 99}).status_code == 409
    assert confirm(r, h, {**b, "request_key": str(uuid4())}).status_code == 409
    assert c.put(URL + "/" + h["id"], json=edit(posted)).status_code == 409
    assert c.delete(URL + "/" + h["id"] + "?row_version=2").status_code == 409
    with r["db"].write() as session:
        session.connection().exec_driver_sql("CREATE TABLE qa_ref(id TEXT PRIMARY KEY,source TEXT REFERENCES production_completion(id) ON DELETE RESTRICT)")
        session.connection().exec_driver_sql("INSERT INTO qa_ref VALUES(?,?)", (str(uuid4()), h["id"]))
    assert c.get("/api/work-orders/" + h["work_order_id"]).json()["approval_state"] == "DRAFT"


def test_partial_reports_exact_sum_equal_and_over_plan_not_clamped(completion):
    r = completion
    for i, (g, d, percent) in enumerate([("1.25", "0.25", "50"), ("1.25", "0", "100"), ("0.125", "0.5", "105")]):
        h = create(r, "PART-" + str(i), good_qty_base=g, defective_qty_base=d, report_date="2026-11-2" + str(i))
        assert confirm(r, h).status_code == 201
        assert progress(r)["progress_percent"] == percent
    p = progress(r)
    assert (p["confirmed_good_qty_base"], p["confirmed_defective_qty_base"], p["total_reported_qty_base"], p["remaining_planned_good_qty_base"], p["confirmed_report_count"], p["over_plan"]) == ("2.625", "0.75", "3.375", "-0.125", 3, True)
    h = create(r, "NO-ROUTING", order=1)
    assert h["work_order"]["routing_id"] is None and confirm(r, h).status_code == 201


@pytest.mark.parametrize("good,defective", [("-1", "2"), ("1", "-0.1"), ("0", "0")])
def test_signed_drafts_but_invalid_confirmation_no_mutation(completion, good, defective):
    r = completion; h = create(r, good_qty_base=good, defective_qty_base=defective); before = snapshot(r["path"])
    assert r["client"].get(URL + "/" + h["id"] + "/confirmation-review").status_code == 422
    assert confirm(r, h, dict(request_key=str(uuid4()), row_version=1, confirmation_token="bad")).status_code == 422
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("planned,good,percent,fraction", [("0", "1", None, None), ("-2", "1", None, None),
    ("3", "1", None, "100/3"), ("2.5", "1.25", "50", "50/1"),
    ("0." + "0"*100 + "1", "1", "1"+"0"*103, "1"+"0"*103+"/1")])
def test_progress_denominators_recurring_and_extreme_precision(planned, good, percent, fraction):
    p = progress_values(planned, good, "0.25", 1)
    assert p["progress_percent"] == percent and p["progress_percent_fraction"] == fraction
    assert p["planned_qty_base"] == planned


def test_defect_only_confirmation_and_high_precision_no_binary_float(completion):
    r = completion
    h = create(r, good_qty_base="0", defective_qty_base="0.000000000000000000000000000001")
    assert confirm(r, h).status_code == 201
    assert progress(r)["confirmed_defective_qty_base"] == h["defective_qty_base"]
    huge = "9" * 5000 + ".123456789012345678901234567891"
    h = create(r, "HUGE", good_qty_base=huge, defective_qty_base="0")
    assert confirm(r, h).status_code == 201 and progress(r)["confirmed_good_qty_base"] == huge
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT typeof(good_qty_base) FROM production_completion WHERE id=?", (h["id"],)).scalar() == "text"


def test_source_validation_missing_stale_and_input_derived_identity_rejected(completion):
    r = completion; c = r["client"]
    b = body(r)
    for patch in [dict(work_order_id=str(uuid4())), dict(work_order_version=99), dict(product_id=str(uuid4())), dict(unit_id=str(uuid4())), dict(good_qty_base=1.25), dict(report_date="2026-02-30")]:
        assert c.post(URL, json={**b, **patch}).status_code in {409, 422}
    h = create(r)
    assert c.delete("/api/work-orders/" + h["work_order_id"] + "?row_version=1").status_code == 409
    with pytest.raises(IntegrityError), r["db"].write() as session:
        session.connection().exec_driver_sql("UPDATE work_order SET planned_qty_base='9',row_version=row_version+1 WHERE id=?", (h["work_order_id"],))
    with pytest.raises(IntegrityError), r["db"].write() as session:
        session.connection().exec_driver_sql("UPDATE production_completion SET product_id=?,row_version=2 WHERE id=?", (str(uuid4()), h["id"]))


def test_concurrent_same_confirmation_once_and_cross_report_request_conflict(completion):
    r = completion; a = create(r, "A"); b = confirm_body(r, a)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: confirm(r, a, b), range(2)))
    assert sorted(x.status_code for x in responses) == [200, 201]
    assert progress(r)["confirmed_report_count"] == 1
    other = create(r, "B")
    assert confirm(r, other, b).status_code == 409


def test_concurrent_reports_require_fresh_review_then_sum_without_lost_output(completion):
    r = completion; a = create(r, "A"); other = create(r, "B")
    data = [(a, confirm_body(r, a)), (other, confirm_body(r, other))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pair: confirm(r, *pair), data))
    assert sorted(x.status_code for x in responses) == [201, 409]
    loser = data[next(i for i, response in enumerate(responses) if response.status_code == 409)][0]
    assert confirm(r, loser).status_code == 201
    assert progress(r)["confirmed_good_qty_base"] == "2.5" and progress(r)["confirmed_report_count"] == 2


@pytest.mark.parametrize("stage", ["after_update", "before_commit"])
def test_injected_late_failure_rolls_back_confirmation_and_all_audits(completion, stage):
    r = completion; h = create(r); b = confirm_body(r, h); before = snapshot(r["path"])
    if stage == "after_update":
        def injected(mapper, connection, target):
            if target.id == h["id"] and target.state == "CONFIRMED":
                raise RuntimeError("injected after confirmation UPDATE")
        event.listen(ProductionCompletion, "after_update", injected)
    else:
        def injected(session):
            raise RuntimeError("injected before COMMIT")
        from sqlalchemy.orm import Session
        event.listen(Session, "before_commit", injected)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            confirm(r, h, b)
    finally:
        event.remove(ProductionCompletion if stage == "after_update" else Session,
                     "after_update" if stage == "after_update" else "before_commit", injected)
    assert snapshot(r["path"]) == before
    assert confirm(r, h, b).status_code == 201


def test_confirmed_sql_update_delete_replace_and_unauthorized_transition_denied(completion):
    r = completion; h = create(r); assert confirm(r, h).status_code == 201
    for sql, params in [("UPDATE production_completion SET note='changed' WHERE id=?", (h["id"],)),
                        ("DELETE FROM production_completion WHERE id=?", (h["id"],)),
                        ("INSERT OR REPLACE INTO production_completion SELECT * FROM production_completion WHERE id=?", (h["id"],))]:
        before = snapshot(r["path"])
        with pytest.raises(IntegrityError), r["db"].write() as session:
            session.connection().exec_driver_sql(sql, params)
        assert snapshot(r["path"]) == before
    draft = create(r, "DRAFT")
    with pytest.raises(IntegrityError), r["db"].write() as session:
        session.connection().exec_driver_sql("""UPDATE production_completion SET state='CONFIRMED',row_version=2,
          confirmation_id=?,confirmation_fingerprint=?,confirmation_snapshot='{}',confirmed_at=?,confirmed_by=?,authorization_basis='LOCAL_DEMO_MANUAL' WHERE id=?""",
          (str(uuid4()), "a"*64, "2026-11-20T00:00:00.000000Z", h["created_by"], draft["id"]))
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT erp_completion_document(),erp_completion_fingerprint(),erp_issue_manual_authorized(),erp_material_return_authorized()").one() == (None, None, 0, 0)


def test_ordinary_runtime_denial_and_restart_exact_retry_durable(completion):
    r = completion; h = create(r); b = confirm_body(r, h); assert confirm(r, h, b).status_code == 201
    draft = create(r, "PERSIST-DRAFT"); before = snapshot(r["path"])
    with TestClient(create_app(r["path"])) as ordinary:
        assert not ordinary.get(URL + "/policy").json()["confirmation_allowed"]
        assert ordinary.post(URL + "/" + h["id"] + "/confirm", json=b).status_code == 200
        assert ordinary.post(URL + "/" + draft["id"] + "/confirm", json={**b,"request_key":str(uuid4())}).status_code == 403
        assert ordinary.get(URL + "/" + draft["id"]).json()["state"] == "DRAFT"
    assert snapshot(r["path"]) == before


def test_additive_migration_and_representative_late_ddl_failure(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from backend.app.db import make_engine
    path = tmp_path / "old.sqlite"
    engine = make_engine(path); config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    with engine.connect().execution_options(erp_write=True) as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0012_material_returns")
    engine.dispose(); before = snapshot(path)
    module = import_module("backend.migrations.versions.0013_production_completions")
    original = module.op.execute; reached = []
    def failure(statement, *args, **kwargs):
        result = original(statement, *args, **kwargs)
        if "CREATE TRIGGER completion_work_order_hold" in str(statement):
            reached.append(True)
            raise RuntimeError("injected late completion DDL failure")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(module.op, "execute", failure)
        with pytest.raises(RuntimeError, match="late completion"):
            migrate(path)
    assert reached and snapshot(path) == before
    migrate(path); migrate(path); after = snapshot(path)
    assert len(after["rows"]) == 62 and set(after["rows"])-set(before["rows"]) == {"production_completion", "finished_goods_receipt", "finished_goods_posting", "finished_goods_entry", "sales_order", "sales_order_line", "sales_create_receipt", "sales_shipment", "sales_shipment_posting", "sales_shipment_entry", "customer_return", "customer_return_finalization", "customer_return_entry", "stocktake", "stocktake_line", "stocktake_exception", "stocktake_posting", "stocktake_entry", "inventory_adjustment", "inventory_adjustment_posting", "inventory_adjustment_entry", "inventory_transfer", "inventory_transfer_posting", "inventory_transfer_entry"}
    assert all(after["rows"][t] == rows for t, rows in before["rows"].items())
    assert_finished_goods_upgrade_objects(before, after)
    with closing(sqlite3.connect(path)) as db:
        from backend.app.stock_ledger import register_exact_sql
        register_exact_sql(db)
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0020_inventory_transfers"


def test_stale_review_and_foreign_or_tampered_token_reject_without_grant(completion):
    r = completion; a = create(r); b = confirm_body(r, a)
    updated = r["client"].put(URL + "/" + a["id"], json=edit(a, good_qty_base="0.5")).json()
    assert confirm(r, updated, {**b, "row_version": 2}).status_code == 409
    for token in ["invalid", b["confirmation_token"][:-1]+("a" if b["confirmation_token"][-1]!="a" else "b")]:
        assert confirm(r, updated, {**b, "row_version": 2, "confirmation_token": token}).status_code == 409
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT erp_completion_document()").scalar() is None


def test_concurrent_draft_updates_only_one_version_wins(completion):
    r = completion; a = create(r)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda qty: r["client"].put(URL + "/" + a["id"], json=edit(a, good_qty_base=qty)).status_code, ["1", "2"]))
    assert sorted(results) == [200, 409]


@pytest.mark.parametrize("planned", ["0", "-2.5"])
def test_actual_zero_or_signed_work_order_allows_factual_report_but_no_percent(completion, planned):
    r = completion; c = r["client"]; wo = r["orders"][0]
    from backend.tests.test_work_orders import edit as order_edit
    changed = c.put("/api/work-orders/" + wo["id"], json=order_edit(wo, planned_qty_base=planned))
    assert changed.status_code == 200, changed.text
    r["orders"][0] = changed.json()
    h = create(r)
    assert confirm(r, h).status_code == 201
    p = progress(r)
    assert p["planned_qty_base"] == planned and p["progress_percent"] is None and p["progress_percent_fraction"] is None


def test_loopback_scope_foreign_review_and_restart_draft_token_denied(completion):
    r = completion; c = r["client"]; a = create(r); b = confirm_body(r, a); before = snapshot(r["path"])
    for headers in [{"Origin":"https://example.invalid"}, {"Host":"example.invalid"}]:
        assert not c.get(URL + "/policy", headers=headers).json()["confirmation_allowed"]
        assert c.post(URL + "/" + a["id"] + "/confirm", json=b, headers=headers).status_code == 403
    other_app = create_local_demo_app(r["path"], backend_port=8030, frontend_port=5193)
    with TestClient(other_app, base_url="http://127.0.0.1:8030", client=("127.0.0.1",50000)) as other:
        assert other.post(URL + "/" + a["id"] + "/confirm", json=b).status_code == 409
    second = create(r, "SECOND")
    assert confirm(r, second, b).status_code == 409
    after_by_id = {row[0]: row for row in snapshot(r["path"])["rows"]["production_completion"]}
    assert all(after_by_id[row[0]] == row for row in before["rows"]["production_completion"])


def seed_completion_qa_database(path):
    """Fresh purchasing/issue/return facts, never copied Owner business records."""
    from backend.tests.material_return_qa_runtime import seed_material_return_qa_database
    from backend.tests.test_material_returns import header, add, post, current
    metadata = seed_material_return_qa_database(path)
    app = create_local_demo_app(path, backend_port=8030, frontend_port=5193)
    with TestClient(app, base_url="http://127.0.0.1:8030", client=("127.0.0.1",50000)) as client:
        r = dict(metadata, client=client, app=app, db=app.state.db, path=path)
        h = header(r); add(r, h); response = post(r, h)
        assert response.status_code == 201, response.text
        metadata["material_return"] = current(r, h)
    return metadata


def test_real_issue_return_purchase_mrp_and_all_physical_tables_unchanged(tmp_path):
    path = tmp_path / "full-synthetic.sqlite"
    if not path.resolve().is_relative_to(ROOT / "artifacts"):
        base = ROOT / "artifacts" / "round17-targeted";base.mkdir(exist_ok=True)
        path = Path(tempfile.mkdtemp(dir=base)) / "full-synthetic.sqlite"
    metadata = seed_completion_qa_database(path)
    app = create_local_demo_app(path, backend_port=8030, frontend_port=5193)
    with TestClient(app, base_url="http://127.0.0.1:8030", client=("127.0.0.1",50000)) as client:
        r = dict(metadata, client=client, app=app, db=app.state.db, path=path)
        resources = ["stock-positions", "material-issues", "material-returns", "purchase-orders", "purchase-outstanding", "purchase-receipts", "supplier-returns", "work-orders", "boms", "routings", "operation-definitions"]
        projections = {name: client.get("/api/" + name).json() for name in resources}
        from backend.tests.test_mrp import preview
        old_mrp = preview(r)
        before = snapshot(path)
        for i in range(2):
            h = create(r, "COMP-TEST-001" if i == 0 else "COMP-OTHER", order=i)
            assert confirm(r, h).status_code == 201
        after = snapshot(path)
        assert all(after["rows"][t] == rows for t, rows in before["rows"].items() if t not in {"production_completion", "stock_create_receipt"})
        assert all(client.get("/api/" + name).json() == value for name, value in projections.items())
        new_mrp = preview(r)
        assert new_mrp["materials"] == old_mrp["materials"] and new_mrp["orders"] == old_mrp["orders"]
        material = next(p for p in projections["stock-positions"] if p["item_id"] == metadata["raws"][0]["id"])
        assert material["on_hand_qty_base"] == "3"
        assert next(m for m in new_mrp["materials"] if m["material"]["id"] == material["item_id"])["gross_demand_qty_base"] == "11.5625"
