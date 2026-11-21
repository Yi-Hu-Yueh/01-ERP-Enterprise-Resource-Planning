"""Focused current Owner database comparison; never enumerates historical files."""
import argparse
import json
from pathlib import Path
import sqlite3
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]

def snapshot(path):
    with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows = {n: db.execute('SELECT * FROM "'+n.replace('"','""')+'" ORDER BY rowid').fetchall() for n in names}
        return dict(rows=rows, schema=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
                    integrity=db.execute("PRAGMA integrity_check").fetchall(), foreign_keys=db.execute("PRAGMA foreign_key_check").fetchall())

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("stage", choices=["before","after"])
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    out=args.output.resolve()
    assert out.is_relative_to(ROOT/"artifacts")
    out.mkdir(parents=True,exist_ok=True)
    path=ROOT/"data"/"erp.sqlite"
    result=snapshot(path)
    result["api"]={resource:json.load(urlopen("http://127.0.0.1:8000/api/"+resource)) for resource in [
        "work-orders","boms","routings","operation-definitions","purchase-orders","purchase-outstanding",
        "purchase-receipts","supplier-returns","stock-positions","warehouses"]}
    file=out/("owner-"+args.stage+".json")
    file.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    if args.stage=="before":
        backup=out/"owner-before.sqlite"
        assert not backup.exists()
        with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as src, sqlite3.connect(backup) as dest:
            src.backup(dest)
        assert snapshot(backup)=={k:v for k,v in result.items() if k!="api"}
    else:
        assert json.loads(json.dumps(result))==json.loads((out/"owner-before.json").read_text(encoding="utf8"))
    print(json.dumps(dict(stage=args.stage,tables=len(result["rows"]),rows=sum(len(v) for k,v in result["rows"].items() if k!="alembic_version"),
                         head=result["rows"]["alembic_version"],work_orders=result["api"]["work-orders"],
                         stock=result["api"]["stock-positions"]),ensure_ascii=False))

if __name__=="__main__":
    main()
