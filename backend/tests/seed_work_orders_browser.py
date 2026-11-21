"""Seed only a new, project-local QA database; never manufacture live Owner orders."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
from backend.tests.test_work_orders import accepted_sources

path = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2]).resolve()
assert out.is_relative_to(ROOT / "artifacts") and path.parent == out and not path.exists()
out.mkdir(parents=True, exist_ok=True)

with TestClient(create_app(path)) as client:
    r = po.__wrapped__((client, client.app.state.db, path))
    inquiry.__wrapped__(returns.__wrapped__(r))
    accepted_sources(r)
    assert client.get("/api/work-orders").status_code == 200
    assert client.get("/api/work-orders").json() == []
    result = {
        "products": r["products"], "raws": r["raws"], "boms": r["boms"], "routings": r["routings"],
        "operations": r["operations"], "centers": r["centers"],
        "stock": client.get("/api/stock-positions").json(),
        "inquiry": client.get("/api/purchase-outstanding").json(),
        "masterResponses": {resource: client.get("/api/"+resource).json() for resource in [
            "finished-goods", "raw-materials", "boms", "routings", "operation-definitions", "work-centers"]}}
    (out / "work-order-browser-fixtures.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf8")

print("Fresh isolated work-order browser references seeded; Owner database untouched; no work orders pre-created.")
