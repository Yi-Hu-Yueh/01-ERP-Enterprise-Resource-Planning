"""Fresh artifact database only: purchase/return baseline, two BOMs and routing references."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.tests.test_foundation import payload
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
from backend.tests.test_bom import header as bom_header,add as bom_add
path=Path(sys.argv[1]).resolve();out=Path(sys.argv[2]).resolve()
assert out.is_relative_to(ROOT/'artifacts') and path.parent==out and not path.exists()
out.mkdir(parents=True,exist_ok=True)
with TestClient(create_app(path)) as client:
 r=po.__wrapped__((client,client.app.state.db,path));inquiry.__wrapped__(returns.__wrapped__(r))
 category=client.get('/api/categories').json()[0];unit=client.get('/api/units').json()[0]
 other=client.post('/api/finished-goods',json=payload(category,unit,'QA-ROUTE-FG2')).json()
 r['products']=[r['finished'],other]
 a,b=bom_header(r,revision='MATERIAL-A',output_basis_qty='1'),bom_header(r,1,revision='MATERIAL-B',output_basis_qty='1')
 bom_add(r,a,qty='2.75');bom_add(r,a,1,'0.5');bom_add(r,b,qty='1.25')
 operations=[client.post('/api/operation-definitions',json={'code':code,'name':name,'instruction':'隔離測試工法','is_active':True}).json() for code,name in [('CUT','裁紙'),('PRINT','印刷'),('BIND','裝訂')]]
 centers=[client.post('/api/work-centers',json={'code':'WC-ROUTE-'+str(i),'name':'中心'+str(i),'description':None,'is_active':True}).json() for i in range(2)]
 assert all('id' in x for x in operations+centers)
 result={'products':r['products'],'operations':operations,'centers':centers,'boms':client.get('/api/boms').json(),
         'stock':client.get('/api/stock-positions').json(),'inquiry':client.get('/api/purchase-outstanding').json()}
 (out/'routing-browser-fixtures.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print('Isolated routing references and accepted-style BOM/purchase/stock baseline created; Owner database unused.')
