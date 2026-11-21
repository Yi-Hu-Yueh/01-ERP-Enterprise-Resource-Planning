"""Fresh real manufacturing/stock/sales QA lineage, never copied Owner rows."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.local_demo import create_local_demo_app
from backend.tests.seed_sales_browser import seed as seed_finished_stock
from backend.tests.test_sales import create as create_order, add as add_sales_line, get as get_order
from backend.tests.test_mrp import preview
from fastapi.testclient import TestClient

PORT, UI_PORT = 8036, 5199


def seed(path):
    path = Path(path).resolve()
    assert path.is_relative_to(ROOT / "artifacts") and not path.exists()
    metadata = seed_finished_stock(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        # The inherited manufacturing metadata's `unit` is the RAW sheet unit;
        # demand lines must use the actual finished-product base-unit identity.
        units = client.get("/api/units").json()
        sales_unit = next(u for u in units if u["id"] == metadata["products"][0]["base_unit_id"])
        metadata.update(raw_unit=metadata.get("unit"), unit=sales_unit, sales_unit=sales_unit)
        r = dict(metadata, client=client, app=app, db=app.state.db, path=path)
        order = create_order(r, "SO-QA-SHIPMENT")
        add_sales_line(r, order, "1.25", "40", 0)
        add_sales_line(r, order, "2.5", "7.25", 1)
        order = get_order(r, order)
        assert order["total_amount"] == "68.125"
        positions = client.get("/api/stock-positions").json()
        position = next(p for p in positions if p["item_id"] == metadata["products"][0]["id"])
        assert position["on_hand_qty_base"] == "1.25"
        resources = ["customers", "finished-goods", "units", "sales-orders", "work-orders", "boms", "routings",
            "operation-definitions", "purchase-orders", "purchase-outstanding", "purchase-receipts", "supplier-returns",
            "stock-positions", "material-issues", "material-returns", "production-completions", "finished-goods-receipts"]
        metadata.update(order=order, sales_order=order, sales_lines=order["lines"],
            position=position, positions=positions, baseline={name: client.get("/api/" + name).json() for name in resources},
            mrp=preview(r), database=str(path))
        assert client.get("/api/sales-shipments").json() == []
    return metadata


if __name__ == "__main__":
    metadata = seed(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print("Fresh synthetic shipment QA sources ready: ordered1.25/stock1.25/raw3; no shipment created.")
