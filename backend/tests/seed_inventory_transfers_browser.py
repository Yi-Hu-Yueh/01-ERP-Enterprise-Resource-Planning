"""Fresh synthetic accepted-style transfer sources; never an Owner database copy."""
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from backend.app.local_demo import create_local_demo_app
from backend.tests.seed_inventory_adjustments_browser import seed as seed_adjustment_sources
from backend.tests.test_finished_goods_receipts import snapshot

PORT, UI_PORT = 8045, 5208
NEW_TABLES = {"inventory_transfer", "inventory_transfer_posting", "inventory_transfer_entry"}


def seed(path):
    path = Path(path).resolve()
    assert path.is_relative_to(ROOT / "artifacts") and not path.exists()
    metadata = seed_adjustment_sources(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/inventory-adjustments", json=dict(request_key=str(uuid4()),
            document_no="ADJ-QA-TRANSFER-BASELINE", business_date="2026-11-21",
            position_id=metadata["raw_position"]["id"], direction="DECREASE", quantity="0.25",
            reason="本機展示庫存調整驗收，非實際損耗", note="Synthetic prior accepted operation"))
        assert response.status_code == 201, response.text
        row = response.json()
        reviewed = client.get("/api/inventory-adjustments/" + row["id"] + "/posting-review")
        assert reviewed.status_code == 200, reviewed.text
        response = client.post("/api/inventory-adjustments/" + row["id"] + "/post", json=dict(
            request_key=str(uuid4()), row_version=row["row_version"],
            confirmation_token=reviewed.json()["confirmation_token"]))
        assert response.status_code == 201, response.text
        adjustment = response.json()
        assert adjustment["state"] == "POSTED" and len(adjustment["movements"]) == 1
        assert adjustment["movements"][0]["qty_base_delta"] == "-0.25"
        positions = client.get("/api/stock-positions").json()
        for key, expected in [("raw_position", "2.75"), ("finished_position", "0.75"),
                              ("zero_position", "0"), ("other_position", "0")]:
            metadata[key] = next(p for p in positions if p["id"] == metadata[key]["id"])
            assert metadata[key]["on_hand_qty_base"] == expected
        fg = metadata["finished_position"]
        assert not any(p["item_id"] == fg["item_id"] and p["lot_id"] == fg["lot_id"]
            and p["warehouse_id"] == metadata["warehouse2"]["id"] for p in positions)
        historical = client.get("/api/stocktakes/" + metadata["stocktake"]["id"]).json()
        assert historical["state"] == "CLOSED" and historical["movements"] == [] and not historical["freeze_active"]
        assert next(l for l in historical["lines"] if l["position_id"] == metadata["raw_position"]["id"])["book_qty"] == "3"
        assert client.get("/api/inventory-transfers").json() == []
        resources = list(metadata["baseline"]) + ["inventory-adjustments"]
        metadata.update(database=str(path), positions=positions, adjustment=adjustment,
            stocktake=historical, baseline={name: client.get("/api/" + name).json() for name in dict.fromkeys(resources)})
    metadata["baseline_rows"] = {name: rows for name, rows in snapshot(path)["rows"].items()
        if name not in NEW_TABLES | {"stock_create_receipt"}}
    return metadata


if __name__ == "__main__":
    result = seed(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print("Fresh transfer QA: RAW2.75/FINISHED0.75; historical stocktake CLOSED; prior adjustment POSTED; no transfers.")
