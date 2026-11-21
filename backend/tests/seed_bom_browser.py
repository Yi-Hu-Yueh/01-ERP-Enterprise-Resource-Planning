"""Create master and accepted purchase/return baseline only in a new QA artifact DB."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.tests.test_foundation import payload
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
path=Path(sys.argv[1]).resolve();out=Path(sys.argv[2]).resolve()
assert out.is_relative_to(ROOT/'artifacts') and path.parent==out and not path.exists()
out.mkdir(parents=True,exist_ok=True)
with TestClient(create_app(path)) as client:
 r=po.__wrapped__((client,client.app.state.db,path));inquiry.__wrapped__(returns.__wrapped__(r))
 unit=client.post('/api/units',json={'code':'BOOK','name':'本'}).json()
 category=client.get('/api/categories').json()[0]
 product2=client.post('/api/finished-goods',json=payload(category,unit,'QA-FG2')).json()
 result={'products':[r['finished'],product2],'raws':r['raws'],'inquiry':client.get('/api/purchase-outstanding').json(),'stock':client.get('/api/stock-positions').json()}
 (out/'bom-browser-fixtures.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print('New isolated BOM browser database seeded; no Owner database used.')
