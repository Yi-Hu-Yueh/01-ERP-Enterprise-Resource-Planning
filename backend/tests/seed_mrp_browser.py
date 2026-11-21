"""Create accepted-style MRP scenarios only in a fresh project-local QA database."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
from backend.tests.test_mrp import seed
from backend.tests.test_work_orders import create

path=Path(sys.argv[1]).resolve()
out=Path(sys.argv[2]).resolve()
assert out.is_relative_to(ROOT/"artifacts") and path.parent==out and not path.exists()
out.mkdir(parents=True,exist_ok=True)
with TestClient(create_app(path)) as client:
    r=po.__wrapped__((client,client.app.state.db,path))
    seed(inquiry.__wrapped__(returns.__wrapped__(r)))
    missing=create(r,"WO-MISSING-BOM",planned_qty_base="0")
    result={k:r[k] for k in ["orders","products","raws","boms","warehouse","warehouse2"]}
    result["missing"]=missing
    (out/"mrp-browser-fixtures.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
print("Isolated MRP QA database seeded; Owner data untouched.")
