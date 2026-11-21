"""Full accepted-style chain on a NEW QA file, never a copied Owner database."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from backend.app.local_demo import create_local_demo_app
from backend.tests.test_finished_goods_receipts import seed_finished_goods_receipt_qa_database,client_for,create,post
from backend.tests.test_mrp import preview
from backend.tests.test_supplier_offers import snapshot


def seed(path):
    path=Path(path).resolve()
    assert path.is_relative_to(ROOT/'artifacts') and not path.exists()
    metadata=seed_finished_goods_receipt_qa_database(path)
    app=create_local_demo_app(path,backend_port=8032,frontend_port=5195)
    with client_for(app) as client:
        r=dict(metadata,client=client,app=app,db=app.state.db,path=path)
        doc=create(r,'FG-QA-BASELINE',lot_code='LOT-QA-FG-BASELINE')
        result=post(r,doc);assert result.status_code==201,result.text
        currency=client.get('/api/currencies').json()[0]
        customers=[client.post('/api/customers',json=dict(code='QA-CUSTOMER-'+str(i),name='獨立驗收客戶'+str(i),currency_id=currency['id'],is_active=True)).json() for i in range(2)]
        resources=['work-orders','boms','routings','operation-definitions','purchase-orders','purchase-outstanding','purchase-receipts','supplier-returns','stock-positions','material-issues','material-returns','production-completions','finished-goods-receipts']
        original={k:client.get('/api/'+k).json() for k in resources}
        metadata.update(customers=customers,baseline=original,mrp=preview(r),database=str(path))
        assert next(p for p in original['stock-positions'] if p['item_id']==metadata['products'][0]['id'])['on_hand_qty_base']=='1.25'
    return metadata


if __name__=='__main__':
    metadata=seed(sys.argv[1]);Path(sys.argv[2]).write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('Fresh QA demand fixtures ready, real FG stock1.25/raw3; Owner untouched.')
