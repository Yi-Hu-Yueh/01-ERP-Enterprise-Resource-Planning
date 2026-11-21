"""Fresh synthetic adjustment sources and closed stocktake; never Owner copies."""
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from backend.app.db import DEMO_ACTOR_ID
from backend.app.local_demo import create_local_demo_app
from backend.app.receiving_models import StockPosition
from backend.tests.seed_stocktakes_browser import seed as seed_stocktake_sources
from backend.tests.test_finished_goods_receipts import snapshot

PORT, UI_PORT = 8043, 5206
NEW_TABLES = {"inventory_adjustment", "inventory_adjustment_posting", "inventory_adjustment_entry"}


def seed(path):
    path = Path(path).resolve()
    assert path.is_relative_to(ROOT / "artifacts") and not path.exists()
    metadata = seed_stocktake_sources(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        with app.state.db.write() as session:
            other = StockPosition(item_id=metadata["raw_position"]["item_id"],
                warehouse_id=metadata["warehouse2"]["id"], lot_id=metadata["raw_position"]["lot_id"],
                created_by=DEMO_ACTOR_ID)
            session.add(other)
            session.flush()
            metadata["other_position_id"] = other.id
        response = client.post("/api/stocktakes", json=dict(request_key=str(uuid4()),
            document_no="STK-QA-ADJUSTMENT-BASELINE", warehouse_id=metadata["warehouse"]["id"],
            business_date="2026-11-21", note="Fresh isolated zero-variance historical stocktake"))
        assert response.status_code == 201, response.text
        row = response.json()
        response = client.post("/api/stocktakes/" + row["id"] + "/start",
            json=dict(request_key=str(uuid4()), row_version=row["row_version"]))
        assert response.status_code == 201, response.text
        row = response.json()
        for line in list(row["lines"]):
            response = client.put("/api/stocktakes/" + row["id"] + "/lines/" + line["id"],
                json=dict(row_version=line["row_version"], stocktake_version=row["row_version"],
                    counted_qty=line["book_qty"], note="Explicit synthetic QA count"))
            assert response.status_code == 200, response.text
            row = response.json()
        review = client.get("/api/stocktakes/" + row["id"] + "/closing-review")
        assert review.status_code == 200, review.text
        response = client.post("/api/stocktakes/" + row["id"] + "/close", json=dict(
            request_key=str(uuid4()), row_version=row["row_version"],
            confirmation_token=review.json()["confirmation_token"]))
        assert response.status_code == 201, response.text
        historical = response.json()
        assert historical["state"] == "CLOSED" and not historical["freeze_active"]
        assert historical["movements"] == []
        positions = client.get("/api/stock-positions").json()
        metadata["other_position"] = next(p for p in positions if p["id"] == metadata["other_position_id"])
        assert metadata["other_position"]["on_hand_qty_base"] == "0"
        for key, expected in [("raw_position", "3"), ("finished_position", "0.75"), ("zero_position", "0")]:
            metadata[key] = next(p for p in positions if p["id"] == metadata[key]["id"])
            assert metadata[key]["on_hand_qty_base"] == expected
        assert client.get("/api/inventory-adjustments").json() == []
        resources = list(metadata["baseline"]) + ["stocktakes"]
        metadata.update(database=str(path), positions=positions, stocktake=historical,
            baseline={name: client.get("/api/" + name).json() for name in dict.fromkeys(resources)})
    metadata["baseline_rows"] = {name: rows for name, rows in snapshot(path)["rows"].items()
        if name not in NEW_TABLES | {"stock_create_receipt"}}
    return metadata


if __name__ == "__main__":
    result = seed(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print("Fresh adjustment QA: RAW3/FINISHED0.75/registered zero; historical stocktake CLOSED; no adjustment.")
