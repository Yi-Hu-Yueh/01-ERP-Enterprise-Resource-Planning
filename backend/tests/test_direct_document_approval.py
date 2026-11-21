"""Focused direct-submit contracts; all writes use independent synthetic DBs."""
from uuid import uuid4
import pytest
from backend.tests.test_approvals import (qa, approval_template, initialize, submit_body,
    csrf, read_sql, current, advance, tasks, act)
from backend.tests.test_purchasing import header_edit as purchase_edit
from backend.tests.test_sales import header_edit as sales_edit
from backend.tests.test_work_orders import edit as work_edit
from backend.app.approvals_engine import SubmitInput
from backend.app.approvals_common import digest

@pytest.mark.parametrize('kind,route,edit',[
    ('PURCHASE_ORDER','purchase-orders',purchase_edit),
    ('WORK_ORDER','work-orders',work_edit),
    ('SALES_ORDER','sales-orders',sales_edit)])
def test_preview_cas_duplicate_resubmit_and_parallel_signing(qa,kind,route,edit):
    r=initialize(qa);c=r['client'];source_id=r['metadata']['sources'][kind]['id']
    def preview():
        response=c.get('/api/approval/sources',params=dict(employee_id=r['requester'],type=kind,source_id=source_id))
        assert response.status_code==200
        assert len(response.json())==1
        return response.json()[0]
    def count(table):return read_sql(r['path'],'SELECT count(*) n FROM '+table)[0]['n']
    before=preview();source=c.get('/api/'+route+'/'+source_id).json()
    changed=c.put('/api/'+route+'/'+source_id,json=edit(source,note='Persisted change after dialog opened'))
    assert changed.status_code==200,changed.text
    body={**submit_body(r,kind),'expected_fingerprint':before['fingerprint']}
    receipts=count('approval_operation_receipt')
    assert c.post('/api/approval/requests',json=body,headers=csrf(c)).status_code==409
    assert count('approval_request')==0 and count('approval_operation_receipt')==receipts
    body['expected_fingerprint']=preview()['fingerprint']
    response=c.post('/api/approval/requests',json=body,headers=csrf(c));assert response.status_code==200,response.text
    request=response.json();assert request['fingerprint']==body['expected_fingerprint']
    assert c.post('/api/approval/requests',json=body,headers=csrf(c)).json()==request
    assert c.post('/api/approval/requests',json={**body,'request_key':str(uuid4())},headers=csrf(c)).status_code==409
    listing=c.get('/api/approval/requests',params=dict(employee_id=r['requester'],source_type=kind,source_id=source_id)).json()
    assert [x['id'] for x in listing]==[request['id']]
    assert c.get('/api/approval/requests',params=dict(employee_id=r['requester'],source_id=str(uuid4()))).json()==[]
    # Existing source editing stays permitted, while the old reviewed snapshot remains immutable.
    changed=c.put('/api/'+route+'/'+source_id,json=edit(changed.json(),note='Needs a new review attempt'))
    assert changed.status_code==200,changed.text
    old_events=current(r,request)['events'];old_attempts=count('approval_attempt')
    retry=dict(employee_id=r['requester'],request_key=str(uuid4()),action='resubmit',row_version=request['row_version'],cycle=request['cycle'],expected_fingerprint=body['expected_fingerprint'])
    assert c.post('/api/approval/requests/'+request['id']+'/actions',json=retry,headers=csrf(c)).status_code==409
    assert count('approval_attempt')==old_attempts
    retry['expected_fingerprint']=preview()['fingerprint']
    response=c.post('/api/approval/requests/'+request['id']+'/actions',json=retry,headers=csrf(c));assert response.status_code==200,response.text
    request=response.json();assert request['attempt']==2 and request['events'][:len(old_events)]==old_events
    request=advance(r,advance(r,request));co=tasks(r,request);assert len(co)==2
    request=act(r,request,task=co[0]);assert request['current_stage']==3 and not tasks(r,request,stage=4)
    request=act(r,request,task=co[1]);assert request['current_stage']==4

def test_legacy_receipt_hash_and_optional_fingerprint_compatibility(qa):
    r=initialize(qa);body=submit_body(r)
    response=r['client'].post('/api/approval/requests',json=body,headers=csrf(r['client']));assert response.status_code==200
    payload=SubmitInput(**body).model_dump(mode='json',exclude={'expected_fingerprint'})
    stored=read_sql(r['path'],'SELECT payload_hash FROM approval_operation_receipt WHERE request_key=?',(body['request_key'],))[0]
    assert stored['payload_hash']==digest(dict(operation='submit',payload=payload))
    repeat=r['client'].post('/api/approval/requests',json={**body,'expected_fingerprint':None},headers=csrf(r['client']))
    assert repeat.status_code==200 and repeat.json()==response.json()

def test_ambiguous_role_requires_explicit_candidate(qa):
    r=initialize(qa);c=r['client'];role=next(x for x in c.get('/api/approval/roles').json() if x['code']=='DEMO-FINANCE')
    original=next(x for x in c.get('/api/employees').json() if role['id'] in x['role_ids'])
    second=c.post('/api/employees',json=dict(code='DIRECT-SECOND-FINANCE',name='Synthetic second finance',department_id=original['department_id'],role_ids=[role['id']]),headers=csrf(c))
    assert second.status_code==201
    body=submit_body(r)
    assert c.post('/api/approval/requests',json=body,headers=csrf(c)).status_code==422
    body['assignments']={'3.1':second.json()['id']}
    response=c.post('/api/approval/requests',json=body,headers=csrf(c));assert response.status_code==200,response.text
