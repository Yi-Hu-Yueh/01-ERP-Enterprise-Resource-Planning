"""Independent sales fulfillment expectations; all creation is isolated QA."""
from fractions import Fraction
from uuid import uuid4
import pytest
from sqlalchemy import event
from backend.tests.test_customer_returns import returning, create as return_create, finalize
from backend.tests.test_sales import create as order_create, add, get, line_data
from backend.tests.test_sales_shipments import create as shipment_create, post as ship
from backend.tests.test_finished_goods_receipts import snapshot

URL = '/api/outstanding-sales'


@pytest.fixture
def inquiry(returning):
    r = returning
    ret = return_create(r, 'CRET-INQUIRY', quantity='0.25')
    assert finalize(r, ret).status_code == 201
    r['return'] = ret
    return r


def read(r, **filters):
    response = r['client'].get(URL, params=filters)
    assert response.status_code == 200, response.text
    return response.json()


def order(result, identifier):
    return next(o for o in result['orders'] if o['id'] == identifier)


def test_actual_baseline_exact_summary_sources_and_readonly(inquiry):
    r = inquiry; before = snapshot(r['path']); sql = []
    def observe(connection, cursor, statement, parameters, context, many): sql.append(statement.strip().split()[0].upper())
    event.listen(r['db'].engine, 'before_cursor_execute', observe)
    try:
        for _ in range(3): result = read(r)
    finally: event.remove(r['db'].engine, 'before_cursor_execute', observe)
    assert set(sql) <= {'BEGIN', 'SELECT'}
    assert snapshot(r['path']) == before
    o = order(result, r['order']['id']); a, b = o['lines']
    assert o['state'] == 'DRAFT' and o['line_count'] == 2 and o['total_amount'] == '68.125'
    assert [(l['ordered_qty_base'],l['gross_shipped_qty_base'],l['outstanding_qty_base'],l['finalized_returned_qty_base']) for l in [a,b]] == [('1.25','0.75','0.5','0.25'),('2.5','0','2.5','0')]
    t = o['quantity_totals'][0]
    assert [t[k] for k in ['ordered_qty_base','gross_shipped_qty_base','outstanding_qty_base','finalized_returned_qty_base']] == ['3.75','0.75','3','0.25']
    assert a['id'] == r['sales_lines'][0]['id'] and a['shipments'][0]['id'] == r['shipment']['id']
    assert a['shipments'][0]['returns'][0]['id'] == r['return']['id']
    assert a['shipments'][0]['returns'][0]['finalization_state'] == 'POSTED'
    assert result['diagnostics'] == []


def test_partials_drafts_both_return_dispositions_and_fully_shipped_line_stays(inquiry):
    r = inquiry
    draft_ship = shipment_create(r, 'DRAFT-IGNORED', quantity='0.5')
    draft_return = return_create(r, 'DRAFT-RETURN-IGNORED', quantity='0.1')
    record = return_create(r, 'RECORD-ONLY', quantity='0.1', restock_eligible=False)
    assert finalize(r, record).status_code == 201
    partial = shipment_create(r, 'PARTIAL-SECOND', quantity='0.5')
    assert ship(r, partial).status_code == 201
    before = snapshot(r['path']); result = read(r, outstanding_only=True)
    o = order(result, r['order']['id']); assert len(o['lines']) == 2
    a = o['lines'][0]
    assert (a['gross_shipped_qty_base'],a['outstanding_qty_base'],a['finalized_returned_qty_base']) == ('1.25','0','0.35')
    assert {s['id'] for s in a['shipments']} == {r['shipment']['id'],partial['id']}
    assert {ret['id'] for s in a['shipments'] for ret in s['returns']} == {r['return']['id'],record['id']}
    assert draft_ship['id'] not in str(a) and draft_return['id'] not in str(a)
    assert o['has_outstanding'] and snapshot(r['path']) == before


def test_repeated_product_lines_and_separate_orders_do_not_mix(inquiry):
    r = inquiry; extra = add(r, r['order'], '4', '2')
    other = order_create(r, 'OTHER-ORDER', customer_id=r['customers'][1]['id']); add(r, other, '7', '3')
    result = read(r, product_id=r['products'][0]['id']); a = order(result,r['order']['id']); b = order(result,other['id'])
    assert len(a['lines']) == 3 and a['total_amount'] == '76.125'
    assert a['lines'][1]['matches_product_filter'] is False
    repeated = next(l for l in a['lines'] if l['id'] == extra['id'])
    assert (repeated['gross_shipped_qty_base'],repeated['outstanding_qty_base']) == ('0','4')
    assert (b['lines'][0]['gross_shipped_qty_base'],b['lines'][0]['outstanding_qty_base']) == ('0','7')


def test_empty_header_and_mixed_unit_ids_have_no_unitless_grand_total(inquiry):
    r = inquiry; c = r['client']; empty = order_create(r, 'EMPTY')
    unit = c.post('/api/units',json=dict(code='QA-OTHER-UNIT',name='另一基本單位')).json()
    product = c.post('/api/finished-goods',json=dict(code='QA-OTHER-PRODUCT',name='另一成品',category_id=r['products'][0]['category_id'],base_unit_id=unit['id'],lot_tracking=False,is_active=True)).json()
    response = c.post('/api/sales-orders/'+r['order']['id']+'/lines',json=line_data(r,r['order'],'0.125','8',item_id=product['id'],unit_id=unit['id']))
    assert response.status_code == 201,response.text
    result = read(r); o = order(result,r['order']['id']); e = order(result,empty['id'])
    assert o['total_amount'] == '69.125' and len(o['quantity_totals']) == 2
    groups = {g['unit_id']:g for g in o['quantity_totals']}
    assert groups[r['unit']['id']]['ordered_qty_base'] == '3.75'
    assert groups[unit['id']]['ordered_qty_base'] == '0.125'
    assert e['line_count'] == 0 and e['lines'] == [] and e['quantity_totals'] == [] and e['total_amount'] == '0'
    assert not e['has_outstanding'] and empty['id'] not in {o['id'] for o in read(r,outstanding_only=True)['orders']}
    assert 'total_amount' not in result and 'ordered_qty_base' not in result


def test_search_whitespace_literal_patterns_customer_dates_and_full_order_filter_semantics(inquiry):
    r = inquiry; c = r['client']; o = get(r,r['order'])
    changed = c.put('/api/sales-orders/'+o['id'],json={k:o[k] for k in ['document_no','customer_id','order_date','note','row_version']}|dict(document_no='SO-TEST-001 '))
    assert changed.status_code == 200
    assert read(r,document_no='  SO-TEST-001  ')['orders'][0]['document_no'] == 'SO-TEST-001 '
    assert read(r,document_no='   ')['total_orders'] == 1
    for term in ['%', '_', "' OR 1=1 --"]: assert read(r,document_no=term)['total_orders'] == 0
    assert read(r,customer_id=r['customers'][1]['id'])['total_orders'] == 0
    assert read(r,customer_id=r['customers'][0]['id'],date_from='2026-11-20',date_to='2026-11-20')['total_orders'] == 1
    assert read(r,date_from='2026-11-21')['total_orders'] == 0
    p = read(r,product_id=r['products'][0]['id'])['orders'][0]
    assert p['line_count'] == 2 and p['total_amount'] == '68.125' and p['quantity_totals'][0]['outstanding_qty_base'] == '3'


@pytest.mark.parametrize('filters',[{'date_from':'2026-02-30'},{'date_from':'2026-11-21','date_to':'2026-11-20'},{'customer_id':'bad'},{'product_id':'bad'},{'customer_id':str(uuid4())},{'product_id':str(uuid4())}])
def test_invalid_filters_reject_without_writes(inquiry,filters):
    r = inquiry; before = snapshot(r['path'])
    assert r['client'].get(URL,params=filters).status_code == 422
    assert snapshot(r['path']) == before


def test_long_exact_decimals_signed_values_and_no_row_cap(inquiry):
    r = inquiry; q = '0.'+'0'*60+'125'; a = order_create(r,'EXACT')
    add(r,a,q,'0.125'); add(r,a,'-0.5','2')
    result = order(read(r),a['id']); x,y = result['lines']
    assert Fraction(x['ordered_qty_base']) == Fraction(q) and Fraction(x['line_amount']) == Fraction(q)*Fraction('0.125')
    assert y['outstanding_qty_base'] == '-0.5' and y['diagnostic']
    assert result['quantity_totals'][0]['outstanding_qty_base'] == '-0.'+'4'+'9'*59+'875'
    # More than the purchasing page size: no inherited arbitrary sales row cap.
    for n in range(21): order_create(r,'EXTRA-'+str(n))
    assert read(r)['total_orders'] == 23


def test_inconsistent_historical_quantity_is_negative_and_diagnosed_not_clamped(inquiry):
    r = inquiry
    with r['db'].write() as session:
        session.connection().exec_driver_sql('DROP TRIGGER sales_shipment_protect_line')
        session.connection().exec_driver_sql("UPDATE sales_order_line SET ordered_qty='0.25',row_version=row_version+1 WHERE id=?",(r['sales_lines'][0]['id'],))
    before = snapshot(r['path']); line = order(read(r),r['order']['id'])['lines'][0]
    assert (line['gross_shipped_qty_base'],line['outstanding_qty_base']) == ('0.75','-0.5')
    assert line['diagnostic'] and snapshot(r['path']) == before


def test_no_mutation_methods_or_new_posting_capability(inquiry):
    r = inquiry; before = snapshot(r['path'])
    for method in ['post','put','patch','delete']:
        assert getattr(r['client'],method)(URL).status_code == 405
    from backend.app.local_demo import REGISTERED_LOCAL_DEMO_POSTINGS
    assert 'outstanding_sales' not in REGISTERED_LOCAL_DEMO_POSTINGS
    assert snapshot(r['path']) == before
