"""Read-only inquiry: exact independent origins, filters, totals and bounded bulk reads."""
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import event

from backend.tests.test_foundation import setup, payload
from backend.tests.test_purchasing import po, create_header, add_line, get, header_edit
from backend.tests.test_receiving import header as receipt_header, line as receipt_line, post as receipt_post
from backend.tests.test_supplier_returns import returns, header, line, post, balance
from backend.tests.test_supplier_offers import snapshot

URL='/api/purchase-outstanding'


@pytest.fixture
def inquiry(returns):
    r=returns
    for no,qty,origin in [('SRET-TEST-001','3',0),('SRET-TEST-002','2',0),('SRET-TEST-004','1',1)]:
        doc=header(r,no);line(r,doc,qty,origin);response=post(r,doc)
        assert response.status_code==201,response.text
    draft=header(r,'SRET-TEST-003');line(r,draft,'2')
    assert post(r,draft).status_code==422
    assert balance(r)=='4'
    return r


def query(r,**params):
    response=r['client'].get(URL,params=params)
    assert response.status_code==200,response.text
    return response.json()


def order(result,no):
    return next(row for row in result['orders'] if row['document_no']==no)


def test_owner_like_gross_returns_retained_original_sum_no_fanout_and_readonly(inquiry):
    r=inquiry;before=snapshot(r['path']);data=query(r)
    a,b=order(data,'PO-TEST-001'),order(data,'PO-TEST-002')
    fact=a['lines'][0]
    assert (fact['ordered_qty_base'],fact['gross_received_qty_base'],fact['outstanding_qty_base'],fact['posted_return_qty_base'],fact['net_retained_qty_base'])==('12','10','2','6','4')
    assert a['total_amount']=='156' and b['total_amount']=='36.25'
    assert (b['lines'][0]['gross_received_qty_base'],b['lines'][0]['outstanding_qty_base'])==('0','5')
    assert [(s['quantity_base'],s['posted_return_qty_base']) for s in fact['receipts']]==[('6','5'),('4','1')]
    assert data['total_orders']==2 and data['total_lines']==2
    totals=data['quantity_totals'][0]
    assert tuple(totals[k] for k in ['ordered_qty_base','gross_received_qty_base','outstanding_qty_base','posted_return_qty_base','net_retained_qty_base'])==('17','10','7','6','4')
    assert data['currency_totals'][0]['total_amount']=='192.25'
    assert query(r,outstanding_only=True)['total_orders']==2
    for method in ['POST','PUT','DELETE','PATCH']:
        assert r['client'].request(method,URL,json={}).status_code==405
    assert snapshot(r['path'])==before and balance(r)=='4'


def test_multiple_lines_same_material_different_orders_suppliers_and_drafts(inquiry):
    r=inquiry;a=r['order']
    extra=add_line(r,a,'3','2',item=1)
    other=create_header(r,'OTHER-SUPPLIER',supplier_id=r['suppliers'][1]['id'])
    otherline=add_line(r,other,'99','7')
    # One atomic multi-line receipt, unrelated warehouse/real lot; its order attribution stays exact.
    r2={**r,'order':get(r,a),'sources':[r['sources'][0],extra]}
    doc=receipt_header(r2,'MULTI');receipt_line(r2,doc,'0.5',lot_code='OTHER-REAL',warehouse_id=r['warehouse2']['id'])
    receipt_line(r2,doc,'1',source=1,lot_code=None)
    assert receipt_post(r2,doc).status_code==201
    draft=receipt_header(r2,'DRAFT');receipt_line(r2,draft,'999',lot_code='DRAFT-REAL')
    other_r={**r,'order':get(r,other),'sources':[otherline]}
    unrelated=receipt_header(other_r,'UNRELATED');receipt_line(other_r,unrelated,'8',lot_code='SUPPLIER2-LOT')
    assert receipt_post(other_r,unrelated).status_code==201
    data=query(r);first=order(data,'PO-TEST-001');second=order(data,'OTHER-SUPPLIER')
    assert len(first['lines'])==2 and first['total_amount']=='162'
    assert [x['gross_received_qty_base'] for x in first['lines']]==['10.5','1']
    assert [x['posted_return_qty_base'] for x in first['lines']]==['6','0']
    assert second['lines'][0]['gross_received_qty_base']=='8' and second['lines'][0]['posted_return_qty_base']=='0'
    assert len(first['lines'][0]['receipts'])==3
    filtered=query(r,item_id=r['raws'][0]['id'])
    assert len(order(filtered,'PO-TEST-001')['lines'])==2  # membership filter, full order details/SUM
    assert query(r,supplier_id=r['suppliers'][1]['id'])['total_orders']==1


@pytest.mark.parametrize('amount,received',[('0','0'),('1','1'),('1','2'),('0.000000000000000000000000000001','0.0000000000000000000000000000009'),('1234567890123456789012345678901234567890.1234567890123456789','0.1234567890123456789'),('9'*5000,'0')])
def test_exact_signed_difference_no_clamp_or_scale_cap(returns,amount,received):
    r=returns;parent=create_header(r,'EXACT');src=add_line(r,parent,amount,'1')
    rr={**r,'order':get(r,parent),'sources':[src]}
    if received!='0':
        doc=receipt_header(rr,'EXACT-R');receipt_line(rr,doc,received,lot_code='EXACT-LOT');assert receipt_post(rr,doc).status_code==201
    fact=order(query(r,document_no='EXACT'),'EXACT')['lines'][0]
    from backend.app.decimal_text import exact_difference
    assert fact['outstanding_qty_base']==exact_difference(amount,received)
    assert fact['gross_received_qty_base']==received
    assert query(r,document_no='EXACT',outstanding_only=True)['total_orders']==(1 if fact['has_outstanding'] else 0)
    if amount=='1' and received=='2':assert fact['outstanding_qty_base']=='-1'


def test_zero_negative_lines_any_positive_filter_and_unit_currency_groups(returns):
    r=returns
    unit=r['client'].post('/api/units',json={'code':'OTHER-U','name':'另一單位'}).json()
    cat=r['client'].get('/api/categories').json()[0]
    material=r['client'].post('/api/raw-materials',json=payload(cat,unit,'OTHER-ITEM')).json()
    currency=r['client'].post('/api/currencies',json={'code':'OTHER-CUR','name':'另一幣別'}).json()
    r2={**r,'raws':[material], 'unit':unit}
    parent=create_header(r,'GROUPS',currency_id=currency['id'])
    add_line(r,parent,'-100','1')
    add_line(r,parent,'1','1',item=1)
    add_line(r2,parent,'2','3')
    data=query(r,document_no='GROUPS',outstanding_only=True)
    assert data['total_orders']==1 and order(data,'GROUPS')['has_outstanding']
    assert len(data['quantity_totals'])==2
    assert sorted(t['outstanding_qty_base'] for t in data['quantity_totals'])==['-99','2']
    assert data['currency_totals'][0]['currency_code']=='OTHER-CUR' and data['currency_totals'][0]['total_amount']=='-93'
    assert len(query(r)['currency_totals'])==2


def test_filters_date_boundaries_search_literal_combinations_and_empty(inquiry):
    r=inquiry
    parent=get(r,r['other_order']);assert r['client'].put('/api/purchase-orders/'+parent['id'],json=header_edit(parent,order_date='2026-11-20')).status_code==200
    assert query(r,q='PO-TEST-001')['total_orders']==1
    assert query(r,q=r['suppliers'][0]['code'])['total_orders']==2
    assert query(r,q=r['raws'][1]['code'])['total_orders']==1
    assert query(r,q='%')['total_orders']==0
    assert query(r,q='_')['total_orders']==0
    assert query(r,document_no='001',supplier_id=r['suppliers'][0]['id'],item_id=r['raws'][0]['id'],date_from='2026-11-19',date_to='2026-11-19',outstanding_only=True)['total_orders']==1
    assert query(r,date_from='2026-11-20',date_to='2026-11-20')['total_orders']==1
    assert query(r,date_to='2026-11-18')['orders']==[]
    empty=query(r,supplier_id=str(uuid4()));assert empty['total_orders']==0 and empty['quantity_totals']==[] and empty['currency_totals']==[]
    assert query(r)['total_orders']==2


@pytest.mark.parametrize('params',[{'date_from':'2026-02-30'},{'date_to':'2026-1-01'},{'date_from':'2026-11-20','date_to':'2026-11-19'},{'supplier_id':'invalid'},{'item_id':'invalid'},{'page':0},{'page_size':0},{'outstanding_only':'nonsense'}])
def test_invalid_query_inputs_never_write(inquiry,params):
    r=inquiry;before=snapshot(r['path'])
    assert r['client'].get(URL,params=params).status_code==422
    assert snapshot(r['path'])==before


def test_stable_pagination_full_filtered_totals_and_constant_bulk_statement_count(inquiry):
    r=inquiry
    for i in range(23):
        parent=create_header(r,'PAGE-'+str(i).zfill(2));add_line(r,parent,'0.125','2')
    before=snapshot(r['path']);statements=[]
    def trace(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(r['db'].engine,'before_cursor_execute',trace)
    try:
        first=query(r,page_size=3);next_page=query(r,page=2,page_size=3);large=query(r,page_size=5001)
    finally:event.remove(r['db'].engine,'before_cursor_execute',trace)
    assert len([s for s in statements if s.startswith('WITH ')])==18  # six bounded statements per request
    assert all(not s.startswith(('INSERT','UPDATE','DELETE','CREATE','DROP','BEGIN IMMEDIATE')) for s in statements)
    assert first['total_orders']==25 and len(first['orders'])==3 and first['has_next']
    assert first['quantity_totals']==next_page['quantity_totals']==large['quantity_totals']
    assert first['currency_totals']==large['currency_totals']
    assert [o['id'] for o in first['orders']+next_page['orders']]==[o['id'] for o in large['orders'][:6]]
    assert len(large['orders'])==25
    assert query(r,page=999)['orders']==[]
    assert snapshot(r['path'])==before


def test_unposted_incomplete_receipts_and_returns_do_not_count(returns):
    r=returns;doc=receipt_header(r,'DRAFT-ONLY');receipt_line(r,doc,'999',lot_code='NOT-POSTED')
    pending=header(r,'UNPOSTED');line(r,pending,'3')
    # Genuine persisted PREPARING fault probes, not application-generated postings.
    from backend.tests.test_supplier_returns import direct_prepare
    with r['db'].write() as session:direct_prepare(session,r['client'].get('/api/supplier-returns/'+pending['id']).json(),str(uuid4()))
    result=order(query(r),'PO-TEST-001')['lines'][0]
    assert result['gross_received_qty_base']=='10' and result['posted_return_qty_base']=='0'
    assert result['outstanding_qty_base']=='2' and result['net_retained_qty_base']=='10'
