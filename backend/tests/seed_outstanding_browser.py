import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.tests.test_purchasing import po,create_header,add_line,get
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
from backend.tests.test_receiving import header,line,post
path=Path(sys.argv[1]).resolve();out=Path(sys.argv[2]).resolve()
assert out.is_relative_to(ROOT/'artifacts') and path.parent==out and not path.exists()
out.mkdir(parents=True,exist_ok=True)
with TestClient(create_app(path)) as client:
 r=po.__wrapped__((client,client.app.state.db,path));r=returns.__wrapped__(r);inquiry.__wrapped__(r)
 parent=create_header(r,'MULTI',order_date='2026-11-20');src=[add_line(r,parent,'2','3'),add_line(r,parent,'4','5',item=1)]
 rr={**r,'order':get(r,parent),'sources':src};doc=header(rr,'MULTI-R');line(rr,doc,'0.5',warehouse_id=r['warehouse2']['id'],lot_code='OTHER-LOT');line(rr,doc,'1',source=1,lot_code=None);assert post(rr,doc).status_code==201
 other=create_header(r,'SUPPLIER-OTHER',supplier_id=r['suppliers'][1]['id']);add_line(r,other,'3','2')
 result={'supplier':r['suppliers'][0],'otherSupplier':r['suppliers'][1],'raw':r['raws'][0],'otherRaw':r['raws'][1],'orders':client.get('/api/purchase-orders').json(),'inquiry':client.get('/api/purchase-outstanding').json()}
 (out/'browser-fixtures.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print('Isolated accepted-baseline and multi-line inquiry fixtures created.')
