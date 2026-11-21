from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.db import migrate

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "erp.sqlite")
    args = parser.parse_args()
    migrate(args.database)
    print(f"Migrations complete; existing records preserved: {args.database.resolve()}")

