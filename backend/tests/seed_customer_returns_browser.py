"""Fresh synthetic shipment/return sources; never copies Owner business rows."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from backend.app.local_demo import create_local_demo_app
from backend.tests.seed_sales_shipments_browser import seed as seed_shipment_sources
from backend.tests.test_sales_shipments import create as create_shipment, post as post_shipment
from backend.tests.test_mrp import preview

PORT, UI_PORT = 8038, 5201


def seed(path):
    path = Path(path).resolve()
    assert path.is_relative_to(ROOT / "artifacts") and not path.exists()
    metadata = seed_shipment_sources(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000)) as client:
        r = dict(metadata, client=client, app=app, db=app.state.db, path=path)
        draft = create_shipment(r, "SHIP-QA-RETURN-SOURCE", quantity="0.75")
        response = post_shipment(r, draft)
        assert response.status_code == 201, response.text
        shipment = response.json()
        assert shipment["posting_state"] == "POSTED"
        assert shipment["movements"][0]["qty_base_delta"] == "-0.75"
        positions = client.get("/api/stock-positions").json()
        position = next(p for p in positions if p["id"] == metadata["position"]["id"])
        assert position["on_hand_qty_base"] == "0.5"
        raw = next(p for p in positions if p["item_id"] == metadata["raws"][0]["id"])
        assert raw["on_hand_qty_base"] == "3"
        resources = list(metadata["baseline"]) + ["sales-shipments"]
        metadata.update(shipment=shipment, shipments=[shipment], position=position, positions=positions,
            baseline={name: client.get("/api/" + name).json() for name in resources},
            mrp=preview(r), database=str(path))
        assert client.get("/api/customer-returns").json() == []
    return metadata


if __name__ == "__main__":
    metadata = seed(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print("Fresh synthetic return QA: shipped0.75/FGstock0.5/raw3; no customer return created.")
