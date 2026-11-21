"""Fresh synthetic inquiry data, never copied Owner records."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from backend.app.local_demo import create_local_demo_app
from backend.tests.seed_customer_returns_browser import seed as sources
from backend.tests.test_customer_returns import create,finalize
from backend.tests.test_sales import create as create_order
def seed(path):
    path=Path(path).resolve();metadata=sources(path)
    app=create_local_demo_app(path,backend_port=8038,frontend_port=5201)
    with TestClient(app,base_url='http://127.0.0.1:8038',client=('127.0.0.1',50000)) as client:
        r=dict(metadata,client=client,db=app.state.db,app=app,path=path)
        order=client.get('/api/sales-orders/'+metadata['order']['id']).json()
        changed=client.put('/api/sales-orders/'+order['id'],json={k:order[k] for k in ['document_no','customer_id','order_date','note','row_version']}|dict(document_no='SO-TEST-001 '))
        assert changed.status_code==200,changed.text
        metadata['order']=changed.json()
        ret=create(r,'CRET-INQUIRY-QA',quantity='0.25')
        response=finalize(r,ret);assert response.status_code==201,response.text
        empty=create_order(r,'EMPTY-INQUIRY',customer_id=metadata['customers'][1]['id'])
        metadata.update(customer_return=response.json(),empty_order=empty)
        assert client.get('/api/outstanding-sales').json()['total_orders']==2
        assert next(p for p in client.get('/api/stock-positions').json() if p['id']==metadata['position']['id'])['on_hand_qty_base']=='0.75'
    return metadata
if __name__=='__main__':
    result=seed(sys.argv[1]);Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
