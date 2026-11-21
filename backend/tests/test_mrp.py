"""MRP invariants against isolated SQLite, posted ledgers and coherent snapshots."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from backend.app import mrp
from backend.tests.test_foundation import setup, resources, payload
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
from backend.tests.test_supplier_offers import snapshot
from backend.tests.test_work_orders import work, accepted_sources, create, get, edit
from backend.tests.test_bom import header as bom_header, add as bom_add, get as bom_get, edit_header as bom_edit
from backend.tests.test_receiving import header as receipt_header, line as receipt_line, post as receipt_post

URL="/api/mrp/preview"


def seed(r):
    accepted_sources(r)
    r["orders"]=[create(r,"WO-TEST-001",bom=0,routing=0,planned_qty_base="2.5"),
                 create(r,"WO-TEST-002",1,1,planned_qty_base="3.75")]
    return r


@pytest.fixture
def planning(inquiry):
    return seed(inquiry)


def body(r,orders=None,warehouses=None,**extra):
    return dict(work_order_ids=[x["id"] for x in (r["orders"] if orders is None else orders)],
                warehouse_ids=[x["id"] for x in ([r["warehouse"]] if warehouses is None else warehouses)],**extra)


def preview(r,**kwargs):
    response=r["client"].post(URL,json=body(r,**kwargs))
    assert response.status_code==200,response.text
    return response.json()


def material(data,r,index=0):
    return next(m for m in data["materials"] if m["material"]["id"]==r["raws"][index]["id"])


@pytest.mark.parametrize("planned,usage,basis,expected",[
    ("2.5","2.75","1","6.875"),("3.75","1.25","1","4.6875"),
    ("7.5","0.125","2.5","0.375"),("1","1","0.0005","2000"),
    ("0","7","3","0"),("7","0","3","0"),
    ("0."+"0"*100+"12345678901234567890123456789","2","1",None),
    ("9"*5000,"2","1",None),("1","1","2"+ "0"*100,None)])
def test_rational_exactness_without_float_context_rounding_or_digit_caps(planned,usage,basis,expected):
    result=mrp.exact_requirement(planned,usage,basis)
    assert Fraction(Decimal(result))==Fraction(Decimal(planned))*Fraction(Decimal(usage))/Fraction(Decimal(basis))
    if expected is not None:
        assert result==expected


@pytest.mark.parametrize("planned,usage,basis",[
    ("-1","2","1"),("2","-1","1"),("-2","-3","1"),("0","-1","1"),
    ("1","1","3"),("1","1","0"),("1","1","-2")])
def test_unapproved_signed_and_nonterminating_results_are_not_supply(planned,usage,basis):
    with pytest.raises(ValueError):
        mrp.exact_requirement(planned,usage,basis)


def test_two_order_reference_shared_stock_once_returns_and_zero_mutations(planning):
    r=planning;c=r["client"];before=snapshot(r["path"])
    inquiry_before=c.get("/api/purchase-outstanding").json()
    data=preview(r);a=material(data,r);b=material(data,r,1)
    assert [(x["document_no"],[l["required_qty_base"] for l in x["components"]]) for x in data["orders"]]==[
        ("WO-TEST-001",["6.875","1.25"]),("WO-TEST-002",["4.6875"])]
    assert (a["gross_demand_qty_base"],a["physical_stock_qty_base"],a["shortage_qty_base"])==("11.5625","4","7.5625")
    assert (b["gross_demand_qty_base"],b["physical_stock_qty_base"],b["suggested_replenishment_qty_base"])==("1.25","0","1.25")
    assert len(a["positions"])==1 and a["positions"][0]["lot_code"]=="LOT-R8-001"
    assert data["complete"] and data["preliminary"] and data["label"]==mrp.DRAFT_LABEL
    assert all(o["approval_state"]==o["bom"]["release_state"]=="DRAFT" for o in data["orders"])
    assert mrp.DRAFT_BOM in data["orders"][0]["warnings"]
    assert any("未選取途程" in w for w in data["orders"][1]["warnings"])
    assert all("shortage_qty_base" not in o and "allocated_qty" not in o for o in data["orders"])
    assert c.get("/api/purchase-outstanding").json()==inquiry_before
    assert snapshot(r["path"])==before


def test_one_order_duplicate_ids_and_deterministic_normalization(planning):
    r=planning
    one=preview(r,orders=r["orders"][:1])
    assert (material(one,r)["gross_demand_qty_base"],material(one,r)["shortage_qty_base"])==("6.875","2.875")
    both=preview(r)
    duplicates=preview(r,orders=[r["orders"][1],r["orders"][0],r["orders"][1]],warehouses=[r["warehouse"]]*3)
    assert duplicates["materials"]==both["materials"] and duplicates["orders"]==both["orders"]
    assert duplicates["normalized_work_order_ids"]==sorted(x["id"] for x in r["orders"])
    assert (duplicates["duplicate_work_order_ids_removed"],duplicates["duplicate_warehouse_ids_removed"])==(1,2)


def test_same_bom_many_orders_and_legitimate_repeated_material_purposes(work):
    r=work;c=r["client"]
    b=bom_header(r,revision="PURPOSES",output_basis_qty="2")
    first=bom_add(r,b,qty="0.25");second=bom_add(r,b,qty="0.75")
    from backend.tests.test_bom import edit_component
    assert c.put("/api/boms/"+b["id"]+"/components/"+second["id"],
                 json=edit_component(r,b,second,purpose_note="封面用紙")).status_code==200
    r["boms"].append(bom_get(r,b))
    r["orders"]=[create(r,"SAME-"+str(i),bom=2,planned_qty_base=str(i+1)) for i in range(25)]
    r["warehouse"]=c.post("/api/warehouses",json=dict(code="W",name="倉",is_active=True,mrp_included=True)).json()
    data=preview(r)
    assert len(data["orders"])==25 and all(len(o["components"])==2 for o in data["orders"])
    assert {l["purpose_note"] for l in data["orders"][0]["components"]}=={"內頁","封面用紙"}
    assert material(data,r)["gross_demand_qty_base"]=="162.5"
    assert first["id"]!=second["id"]


def test_multiple_lots_warehouses_explicit_scope_no_fanout_and_not_mrp_flag(planning):
    r=planning
    doc=receipt_header(r,"EXTRA-WH")
    receipt_line(r,doc,"1.25",warehouse_id=r["warehouse2"]["id"],lot_code="LOT-W2")
    receipt_line(r,doc,"0.75",lot_code="LOT-ANOTHER")
    assert receipt_post(r,doc).status_code==201
    one=preview(r);two=preview(r,warehouses=[r["warehouse2"]]);combined=preview(r,warehouses=[r["warehouse2"],r["warehouse"],r["warehouse2"]])
    assert material(one,r)["physical_stock_qty_base"]=="4.75"
    assert material(two,r)["physical_stock_qty_base"]=="1.25"
    result=material(combined,r)
    assert (result["physical_stock_qty_base"],result["shortage_qty_base"])==("6","5.5625")
    assert len(result["positions"])==3 and len({p["position_id"] for p in result["positions"]})==3
    assert len(result["warehouses"])==2 and not next(w for w in combined["warehouses"] if w["id"]==r["warehouse2"]["id"])["mrp_included"]


def test_distinct_material_units_have_no_mixed_grand_total(planning):
    r=planning;c=r["client"]
    category=c.get("/api/categories").json()[0]
    unit=c.post("/api/units",json={"code":"KG","name":"公斤"}).json()
    raw=c.post("/api/raw-materials",json=payload(category,unit,"RAW-KG")).json()
    r["raws"].append(raw)
    b=bom_header(r,revision="MIXED",output_basis_qty="1")
    bom_add(r,b,qty="2");bom_add(r,b,2,"3.5");r["boms"].append(bom_get(r,b))
    row=create(r,"MIXED-ORDER",bom=2,planned_qty_base="2")
    data=preview(r,orders=[row])
    assert {(m["material"]["id"],m["material"]["unit_code"],m["gross_demand_qty_base"]) for m in data["materials"]}=={
        (r["raws"][0]["id"],next(u["code"] for u in c.get("/api/units").json() if u["id"]==r["raws"][0]["base_unit_id"]),"4"),
        (raw["id"],"KG","7")}
    assert "total_demand" not in data


def test_missing_bom_and_empty_bom_are_unknown_never_false_zero(planning):
    r=planning
    missing=create(r,"MISSING",planned_qty_base="0")
    data=preview(r,orders=[*r["orders"],missing])
    assert not data["complete"] and not next(o for o in data["orders"] if o["id"]==missing["id"])["complete"]
    assert all(m["gross_demand_qty_base"] is None and m["shortage_qty_base"] is None for m in data["materials"])
    only=preview(r,orders=[missing])
    assert only["materials"]==[] and not only["complete"]
    b=bom_header(r,revision="EMPTY");r["boms"].append(b)
    empty=create(r,"EMPTY",bom=2)
    assert not preview(r,orders=[empty])["orders"][0]["complete"]


@pytest.mark.parametrize("planned,usage,basis",[
    ("-2","3","1"),("2","-3","1"),("-2","-3","1"),("1","1","3"),("0","0","1")])
def test_unusual_drafts_preserve_values_and_exclude_unresolved_shortages(planning,planned,usage,basis):
    r=planning;b=bom_header(r,revision="UNUSUAL",output_basis_qty=basis)
    bom_add(r,b,qty=usage);r["boms"].append(bom_get(r,b))
    row=create(r,"UNUSUAL",bom=2,planned_qty_base=planned)
    before=snapshot(r["path"]);data=preview(r,orders=[row]);m=material(data,r)
    assert data["orders"][0]["planned_qty_base"]==planned
    assert data["orders"][0]["components"][0]["usage_qty_base"]==usage
    if planned==usage=="0":
        assert m["gross_demand_qty_base"]==m["shortage_qty_base"]=="0"
    else:
        assert not data["complete"] and m["gross_demand_qty_base"] is None and m["shortage_qty_base"] is None
    assert snapshot(r["path"])==before


def test_negative_position_is_visible_and_not_silently_clamped(planning,monkeypatch):
    # Current immutable posting guards prevent negative stock creation; inject the
    # read boundary to verify the preview fails safely if a future/imported ledger has it.
    r=planning;original=mrp.scoped_stock
    def negative(*args):
        rows=[dict(p) for p in original(*args)]
        rows[0]["on_hand_qty_base"]="-4"
        return rows
    monkeypatch.setattr(mrp,"scoped_stock",negative)
    before=snapshot(r["path"]);data=preview(r);m=material(data,r)
    assert m["physical_stock_qty_base"]=="-4" and m["positions"][0]["on_hand_qty_base"]=="-4"
    assert m["planning_supply_qty_base"] is None and m["shortage_qty_base"] is None and not data["complete"]
    assert snapshot(r["path"])==before


@pytest.mark.parametrize("patch",[
    {"work_order_ids":[]},{"warehouse_ids":[]},{"work_order_ids":[str(uuid4())]},
    {"warehouse_ids":[str(uuid4())]},{"work_order_ids":["bad"]},
    {"expected_work_order_versions":{str(uuid4()):1}},{"expected_bom_versions":{str(uuid4()):1}},
    {"expected_work_order_versions":{},"surprise":"not-allowed"}])
def test_api_incomplete_unknown_and_extra_selection_rejected_without_writes(planning,patch):
    r=planning;before=snapshot(r["path"])
    assert r["client"].post(URL,json={**body(r),**patch}).status_code==422
    assert snapshot(r["path"])==before


def test_changed_work_order_bom_recompute_and_stale_guards(planning):
    r=planning;c=r["client"];old=r["orders"][0]
    updated=c.put("/api/work-orders/"+old["id"],json=edit(old,planned_qty_base="5")).json()
    assert updated["row_version"]==old["row_version"]+1
    assert c.post(URL,json=body(r,expected_work_order_versions={old["id"]:old["row_version"]})).status_code==409
    r["orders"][0]=updated
    assert material(preview(r),r)["gross_demand_qty_base"]=="18.4375"
    # Explicit unlink is required by existing source holds, even in QA.
    unlinked=c.put("/api/work-orders/"+updated["id"],json=edit(updated,bom_id=None,bom_version=None)).json()
    b=bom_get(r,r["boms"][0]);changed=c.put("/api/boms/"+b["id"],json=bom_edit(b,output_basis_qty="2")).json()
    relinked=c.put("/api/work-orders/"+unlinked["id"],json=edit(unlinked,bom_id=b["id"],bom_version=changed["row_version"])).json()
    r["orders"][0]=relinked
    assert c.post(URL,json=body(r,expected_bom_versions={b["id"]:b["row_version"]})).status_code==409
    data=preview(r)
    assert material(data,r)["gross_demand_qty_base"]=="11.5625"
    assert data["orders"][0]["bom"]["row_version"]==changed["row_version"]


def test_query_only_failure_rolls_back_and_does_not_leak_pool_setting(planning,monkeypatch):
    r=planning;before=snapshot(r["path"])
    original=mrp.scoped_stock
    def fail(session,*args):
        session.connection().exec_driver_sql("UPDATE work_order SET note='forbidden'")
        return original(session,*args)
    monkeypatch.setattr(mrp,"scoped_stock",fail)
    response=r["client"].post(URL,json=body(r))
    assert response.status_code==500
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("PRAGMA query_only").scalar()==0
    assert snapshot(r["path"])==before
    # Same pool can still perform the explicitly authorized, unrelated QA edit.
    old=r["orders"][0]
    assert r["client"].put("/api/work-orders/"+old["id"],json=edit(old,note="QA valid write after failure")).status_code==200


def test_coherent_wal_snapshot_across_simultaneous_quantity_and_stock_changes(planning):
    r=planning;db=r["db"];c=r["client"];old=r["orders"][0]
    pool=ThreadPoolExecutor(max_workers=1);fired=[]
    def write():
        with db.write() as session:
            session.connection().exec_driver_sql("UPDATE work_order SET planned_qty_base='5',row_version=row_version+1 WHERE id=?",(old["id"],))
        doc=receipt_header(r,"CONCURRENT")
        receipt_line(r,doc,"1",lot_code="LOT-CONCURRENT")
        assert receipt_post(r,doc).status_code==201
    def after(connection,cursor,statement,parameters,context,executemany):
        if not fired and "FROM work_order" in statement:
            fired.append(True)
            pool.submit(write).result(timeout=15)
    event.listen(db.engine,"after_cursor_execute",after)
    try:
        first=preview(r)
    finally:
        event.remove(db.engine,"after_cursor_execute",after);pool.shutdown()
    second=preview(r)
    assert material(first,r)["gross_demand_qty_base"]=="11.5625"
    assert material(first,r)["physical_stock_qty_base"]=="4"
    assert material(second,r)["gross_demand_qty_base"]=="18.4375"
    assert material(second,r)["physical_stock_qty_base"]=="5"


def test_bulk_source_and_stock_reads_do_not_scale_with_order_count(work):
    r=work;c=r["client"];r["orders"]=[create(r,"MANY-"+str(i),bom=0,planned_qty_base="1") for i in range(40)]
    r["warehouse"]=c.post("/api/warehouses",json=dict(code="W",name="倉",is_active=True,mrp_included=True)).json()
    statements=[]
    def before(connection,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith(("SELECT","WITH")):
            statements.append(statement)
    event.listen(r["db"].engine,"before_cursor_execute",before)
    try:
        data=preview(r)
    finally:
        event.remove(r["db"].engine,"before_cursor_execute",before)
    assert material(data,r)["gross_demand_qty_base"]=="110"
    assert len(statements)==8 and sum("WITH scoped AS" in s for s in statements)==1
