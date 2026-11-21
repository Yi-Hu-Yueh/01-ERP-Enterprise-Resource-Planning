"""Independent demand arithmetic and integrity checks on fresh isolated SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from fractions import Fraction
from uuid import uuid4
import sqlite3
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from backend.app.main import create_app
from backend.tests.test_purchasing import po
from backend.tests.test_foundation import payload, setup
from backend.tests.test_supplier_offers import snapshot

URL='/api/sales-orders'


@pytest.fixture
def so(po):
    c=po['client']
    po['customers']=[c.post('/api/customers',json=dict(code='QA-C'+str(i),name='客戶'+str(i),currency_id=po['currency']['id'],is_active=True)).json() for i in range(2)]
    category=c.get('/api/categories').json()[0]
    po['products']=[po['finished'],c.post('/api/finished-goods',json=payload(category,po['unit'],'QA-FG2')).json()]
    return po


def data(r,no='SO-QA-A',**patch):
    return {**dict(request_key=str(uuid4()),document_no=no,customer_id=r['customers'][0]['id'],order_date='2026-11-20',note='銷售驗收'),**patch}


def create(r,no='SO-QA-A',**patch):
    response=r['client'].post(URL,json=data(r,no,**patch));assert response.status_code==201,response.text
    return response.json()


def get(r,o):return r['client'].get(URL+'/'+o['id']).json()


def header_edit(o,**patch):
    return {**{k:o[k] for k in ['document_no','customer_id','order_date','note','row_version']},**patch}


def line_data(r,o,qty='1.25',price='40',index=0,**patch):
    return {**dict(request_key=str(uuid4()),order_version=get(r,o)['row_version'],item_id=r['products'][index]['id'],unit_id=r['unit']['id'],description_snapshot='原始成品說明',ordered_qty=qty,unit_price=price),**patch}


def add(r,o,qty='1.25',price='40',index=0):
    response=r['client'].post(URL+'/'+o['id']+'/lines',json=line_data(r,o,qty,price,index));assert response.status_code==201,response.text
    return response.json()


def line_edit(r,o,l,**patch):
    return {**{k:l[k] for k in ['item_id','unit_id','description_snapshot','ordered_qty','unit_price','row_version']},'order_version':get(r,o)['row_version'],**patch}


def delete_line(r,o,l):
    return r['client'].delete(URL+'/'+o['id']+'/lines/'+l['id'],params=dict(order_version=get(r,o)['row_version'],row_version=l['row_version']))


def test_68_125_price_73_125_delete_55_quantity_and_order_isolation(so):
    r=so;c=r['client'];a=create(r);b=create(r,'SO-QA-B',customer_id=r['customers'][1]['id'])
    assert a['total_amount']==b['total_amount']=='0' and a['state']=='DRAFT'
    one=add(r,a);two=add(r,a,'2.5','7.25',1);add(r,b,'7','-0.125');other=get(r,b)
    assert one['line_amount']=='50' and two['line_amount']=='18.125' and get(r,a)['total_amount']=='68.125'
    response=c.put(URL+'/'+a['id']+'/lines/'+one['id'],json=line_edit(r,a,one,unit_price='44'))
    assert response.status_code==200;one=response.json()
    assert one['line_amount']=='55' and get(r,a)['total_amount']=='73.125'
    assert delete_line(r,a,two).status_code==204 and get(r,a)['total_amount']=='55'
    response=c.put(URL+'/'+a['id']+'/lines/'+one['id'],json=line_edit(r,a,one,ordered_qty='2.5'))
    assert response.status_code==200 and response.json()['line_amount']=='110' and get(r,a)['total_amount']=='110'
    assert get(r,b)==other
    saved=get(r,a)
    with TestClient(create_app(r['path'])) as reopened:assert reopened.get(URL+'/'+a['id']).json()==saved
    with r['db'].read() as session:
        con=session.connection()
        assert con.exec_driver_sql('SELECT typeof(ordered_qty),typeof(unit_price) FROM sales_order_line').all()==[('text','text'),('text','text')]
        assert 'total_amount' not in [x[1] for x in con.exec_driver_sql('PRAGMA table_info(sales_order)')]
        assert 'line_amount' not in [x[1] for x in con.exec_driver_sql('PRAGMA table_info(sales_order_line)')]


@pytest.mark.parametrize('qty,price',[('0','-4'),('-1.25','-7.25'),('0.0000000000000000000000000000000000001','1.123456789012345678901234567890123456789'),('1234567890123456789012345678901234567890.125','999999999999999999999999999.999')])
def test_independent_fraction_arithmetic_no_rounding_or_unsigned_rule(so,qty,price):
    a=create(so);line=add(so,a,qty,price);add(so,a,'0.1','0.2')
    assert Fraction(line['line_amount'])==Fraction(qty)*Fraction(price)
    assert Fraction(get(so,a)['total_amount'])==Fraction(qty)*Fraction(price)+Fraction(1,50)


def test_5000_digits_repeated_products_and_zero_stock_demand(so):
    a=create(so);huge='9'*5000
    assert add(so,a,huge,'10')['line_amount']==huge+'0'
    add(so,a,'-'+huge,'10');add(so,a,'2.5','7.25');add(so,a,'2.5','7.25')
    assert get(so,a)['total_amount']=='36.25' and get(so,a)['line_count']==4
    assert so['client'].get('/api/stock-positions').json()==[]


@pytest.mark.parametrize('patch',[{'customer_id':str(uuid4())},{'document_no':' '},{'order_date':'2026-02-30'},{'total_amount':'100'},{'state':'SHIPPED'},{'currency_id':str(uuid4())}])
def test_invalid_header_or_unapproved_overrides_leave_rows_unchanged(so,patch):
    a=create(so);before=snapshot(so['path']);body=data(so,'INVALID');body.update(patch)
    assert so['client'].post(URL,json=body).status_code==422
    assert so['client'].put(URL+'/'+a['id'],json=header_edit(a,**patch)).status_code==422
    assert snapshot(so['path'])==before


@pytest.mark.parametrize('patch',[{'item_id':str(uuid4())},{'unit_id':str(uuid4())},{'ordered_qty':1.25},{'unit_price':'NaN'},{'line_amount':'999'},{'sales_order_id':str(uuid4())},{'ordered_qty':'1e4'}])
def test_invalid_line_and_client_amount_or_reparent_rejected_atomically(so,patch):
    a=create(so);l=add(so,a);before=snapshot(so['path']);body=line_data(so,a);body.update(patch)
    assert so['client'].post(URL+'/'+a['id']+'/lines',json=body).status_code==422
    assert so['client'].put(URL+'/'+a['id']+'/lines/'+l['id'],json=line_edit(so,a,l,**patch)).status_code==422
    assert snapshot(so['path'])==before


def test_raw_wrong_base_unit_parent_ownership_unique_and_literal_search(so):
    r=so;c=r['client'];a=create(r);b=create(r,'SO-B');l=add(r,a);before=snapshot(r['path'])
    for patch in [dict(item_id=r['raws'][0]['id']),dict(unit_id=c.post('/api/units',json=dict(code='OTHER',name='其他')).json()['id'])]:
        body=line_data(r,a);body.update(patch);assert c.post(URL+'/'+a['id']+'/lines',json=body).status_code==422
    assert c.post(URL,json=data(r)).status_code==409
    assert c.get(URL+'/'+b['id']+'/lines/'+l['id']).status_code==404
    assert c.put(URL+'/'+b['id']+'/lines/'+l['id'],json=line_edit(r,b,l)).status_code==404
    assert c.delete(URL+'/'+b['id']+'/lines/'+l['id'],params=dict(order_version=1,row_version=1)).status_code==404
    assert len(c.get(URL,params=dict(q='客戶0')).json())==2
    assert len(c.get(URL,params=dict(q='SO-QA')).json())==1
    assert c.get(URL,params=dict(q='%')).json()==[]
    assert get(r,a)['lines'][0]==l


def test_header_customer_update_and_reference_delete_protection(so):
    r=so;c=r['client'];a=create(r);l=add(r,a)
    assert c.delete('/api/customers/'+a['customer_id'],params=dict(row_version=1)).status_code==409
    assert c.delete('/api/finished-goods/'+l['item_id'],params=dict(row_version=1)).status_code==409
    assert c.delete(URL+'/'+a['id'],params=dict(row_version=get(r,a)['row_version'])).status_code==409
    changed=c.put(URL+'/'+a['id'],json=header_edit(get(r,a),customer_id=r['customers'][1]['id'],note='更新'))
    assert changed.status_code==200 and changed.json()['customer_code']=='QA-C1' and changed.json()['total_amount']=='50'
    assert delete_line(r,a,l).status_code==204
    assert c.delete(URL+'/'+a['id'],params=dict(row_version=get(r,a)['row_version'])).status_code==204
    assert c.get(URL+'/'+a['id']).status_code==404


def test_durable_retries_conflicts_tombstones_and_restart(so):
    r=so;c=r['client'];body=data(r);a=c.post(URL,json=body).json()
    assert c.post(URL,json=body).status_code==200
    assert c.post(URL,json={**body,'document_no':'OTHER'}).status_code==409
    lb=line_data(r,a);l=c.post(URL+'/'+a['id']+'/lines',json=lb).json()
    assert c.post(URL+'/'+a['id']+'/lines',json=lb).status_code==200
    with TestClient(create_app(r['path'])) as reopened:assert reopened.post(URL+'/'+a['id']+'/lines',json=lb).json()==l
    assert delete_line(r,a,l).status_code==204
    assert c.post(URL+'/'+a['id']+'/lines',json=lb).status_code==410
    assert get(r,a)['total_amount']=='0'


def test_concurrent_line_create_edit_stale_delete_one_winner(so):
    r=so;c=r['client'];a=create(r);body=line_data(r,a)
    with ThreadPoolExecutor(2) as pool:assert sorted(pool.map(lambda _:c.post(URL+'/'+a['id']+'/lines',json=body).status_code,range(2)))==[200,201]
    a=get(r,a);l=a['lines'][0];b=line_edit(r,a,l,unit_price='44')
    with ThreadPoolExecutor(2) as pool:assert sorted(pool.map(lambda _:c.put(URL+'/'+a['id']+'/lines/'+l['id'],json=b).status_code,range(2)))==[200,409]
    assert get(r,a)['total_amount']=='55'
    assert c.delete(URL+'/'+a['id']+'/lines/'+l['id'],params=dict(order_version=a['row_version'],row_version=1)).status_code==409
    old=get(r,a);body=header_edit(old,note='同時更新')
    with ThreadPoolExecutor(2) as pool:assert sorted(pool.map(lambda _:c.put(URL+'/'+a['id'],json=body).status_code,range(2)))==[200,409]


def test_sql_type_unit_reparent_guards_and_future_actual_fk_protection(so):
    r=so;c=r['client'];a=create(r);b=create(r,'SO-B');l=add(r,a)
    for sql,args in [('UPDATE sales_order_line SET sales_order_id=? WHERE id=?',(b['id'],l['id'])),('UPDATE sales_order_line SET item_id=? WHERE id=?',(r['raws'][0]['id'],l['id'])),("UPDATE item SET item_kind='RAW' WHERE id=?",(l['item_id'],)),("UPDATE sales_order SET state='SHIPPED' WHERE id=?",(a['id'],))]:
        before=snapshot(r['path'])
        with pytest.raises(IntegrityError),r['db'].write() as s:s.connection().exec_driver_sql(sql,args)
        assert snapshot(r['path'])==before
    with r['db'].write() as s:
        s.connection().exec_driver_sql('CREATE TABLE qa_future_source(id TEXT PRIMARY KEY,line_id TEXT REFERENCES sales_order_line(id) ON DELETE RESTRICT ON UPDATE RESTRICT)')
        s.connection().exec_driver_sql('INSERT INTO qa_future_source VALUES (?,?)',(str(uuid4()),l['id']))
    before=snapshot(r['path'])
    assert delete_line(r,a,l).status_code==409
    assert c.put(URL+'/'+a['id']+'/lines/'+l['id'],json=line_edit(r,a,l,unit_price='44')).status_code==409
    assert snapshot(r['path'])==before


@pytest.mark.parametrize('stage',['create','edit','delete'])
def test_injected_late_failure_rolls_back_rows_versions_keys_and_sum(so,stage):
    r=so;c=r['client'];a=create(r);l=add(r,a)
    sql={'create':"CREATE TRIGGER qa_fail BEFORE INSERT ON sales_create_receipt BEGIN SELECT RAISE(ABORT,'qa fail'); END",'edit':"CREATE TRIGGER qa_fail BEFORE UPDATE OF row_version ON sales_order BEGIN SELECT RAISE(ABORT,'qa fail'); END",'delete':"CREATE TRIGGER qa_fail BEFORE UPDATE OF row_version ON sales_order BEGIN SELECT RAISE(ABORT,'qa fail'); END"}[stage]
    with r['db'].write() as s:s.connection().exec_driver_sql(sql)
    before=snapshot(r['path'])
    response=(c.post(URL+'/'+a['id']+'/lines',json=line_data(r,a)) if stage=='create' else c.put(URL+'/'+a['id']+'/lines/'+l['id'],json=line_edit(r,a,l,unit_price='44')) if stage=='edit' else delete_line(r,a,l))
    assert response.status_code==409,response.text
    assert snapshot(r['path'])==before and get(r,a)['total_amount']=='50'


def test_sales_workflow_changes_only_demand_tables_not_stock_or_purchasing(so):
    from backend.tests.test_purchasing import create_header,add_line as purchase_add
    r=so;c=r['client'];poa=create_header(r);purchase_add(r,poa,'12','13')
    a=create(r);before=snapshot(r['path']);stock=c.get('/api/stock-positions').json();purchase=c.get('/api/purchase-orders').json()
    l=add(r,a);add(r,a,'2.5','7.25',1)
    c.put(URL+'/'+a['id']+'/lines/'+l['id'],json=line_edit(r,a,l,unit_price='44'))
    after=snapshot(r['path'])
    for table,rows in before['rows'].items():
        if not table.startswith('sales_'):assert after['rows'][table]==rows,table
    assert c.get('/api/stock-positions').json()==stock and c.get('/api/purchase-orders').json()==purchase
    # Operation22 legitimately adds shipment tables. Demand entry must still
    # create no shipment documents, postings or physical movements.
    for table in ['sales_shipment','sales_shipment_posting','sales_shipment_entry']:
        assert before['rows'][table] == after['rows'][table] == []
