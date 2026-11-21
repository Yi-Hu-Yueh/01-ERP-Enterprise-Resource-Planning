"""Fresh synthetic whole-warehouse QA sources; never an Owner database copy."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from backend.app.db import DEMO_ACTOR_ID
from backend.app.local_demo import create_local_demo_app
from backend.app.receiving_models import InventoryLot, StockPosition
from backend.tests.seed_outstanding_sales_browser import seed as source_seed

PORT, UI_PORT = 8041, 5204


def seed(path):
    path = Path(path).resolve()
    assert path.is_relative_to(ROOT / "artifacts") and not path.exists()
    metadata = source_seed(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        # Registration alone is a genuine zero-balance position, not an opening
        # stock entry. A second RAW lot checks whole-warehouse lot coverage.
        with app.state.db.write() as session:
            lot = InventoryLot(item_id=metadata["raws"][0]["id"], lot_code="LOT-STK-ZERO-QA",
                origin_kind="PURCHASED", supplier_id=metadata["suppliers"][0]["id"],
                created_by=DEMO_ACTOR_ID)
            session.add(lot); session.flush()
            session.add(StockPosition(item_id=metadata["raws"][0]["id"],
                warehouse_id=metadata["warehouse"]["id"], lot_id=lot.id, created_by=DEMO_ACTOR_ID))
        positions = client.get("/api/stock-positions").json()
        raw = next(p for p in positions if p["item_id"] == metadata["raws"][0]["id"] and p["lot_code"] == "LOT-R8-001")
        finished = next(p for p in positions if p["id"] == metadata["position"]["id"])
        zero = next(p for p in positions if p["lot_code"] == "LOT-STK-ZERO-QA")
        assert (raw["on_hand_qty_base"], finished["on_hand_qty_base"], zero["on_hand_qty_base"]) == ("3", "0.75", "0")
        resources = list(metadata["baseline"]) + ["customer-returns"]
        metadata.update(database=str(path), positions=positions, raw_position=raw,
            finished_position=finished, zero_position=zero,
            baseline={name: client.get("/api/" + name).json() for name in dict.fromkeys(resources)})
        assert client.get("/api/stocktakes").json() == []
    return metadata


if __name__ == "__main__":
    result = seed(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print("Fresh isolated stocktake sources: RAW3, FINISHED0.75, registered zero; no stocktake created.")
