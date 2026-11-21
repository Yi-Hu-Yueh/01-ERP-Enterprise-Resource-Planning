"""Explicit Owner-approved loopback-only local-demo launcher; no QA factory."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uvicorn
from backend.app.db import ROOT
from backend.app.local_demo import create_local_demo_app


def main():
    parser=argparse.ArgumentParser(description="Standing Owner-approved LOCAL_DEMO_MANUAL inventory confirmation, not enterprise authorization")
    parser.add_argument("--database",type=Path,default=ROOT/"data/erp.sqlite")
    parser.add_argument("--backend-port",type=int,default=8000)
    parser.add_argument("--frontend-port",type=int,default=5173)
    args=parser.parse_args()
    if sys.prefix==sys.base_prefix:
        raise SystemExit("Use the project .venv Python")
    from scripts.dev import port_available,stop
    for port in [args.backend_port,args.frontend_port]:
        if not port_available(port):
            raise SystemExit("Loopback port is occupied; no process was stopped")
    app=create_local_demo_app(args.database,backend_port=args.backend_port,frontend_port=args.frontend_port)
    environment={**os.environ,"ERP_API_TARGET":"http://127.0.0.1:"+str(args.backend_port)}
    node=shutil.which("node");vite=ROOT/"frontend/node_modules/vite/bin/vite.js"
    if not node or not vite.exists():
        raise SystemExit("Existing frontend runtime is unavailable")
    flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
    frontend=subprocess.Popen([node,str(vite),"--host","127.0.0.1","--port",str(args.frontend_port),"--strictPort"],
        cwd=ROOT/"frontend",env=environment,creationflags=flags)
    print("LOCAL_DEMO_MANUAL: standing Owner approval; registered material_issue/material_return/finished_goods_receipt/sales_shipment/customer_return/stocktake/inventory_adjustment/inventory_transfer; NO VERIFIED ENTERPRISE IDENTITY",flush=True)
    print("Database: "+str(args.database.resolve())+"; API/UI bind127.0.0.1 only",flush=True)
    try:
        uvicorn.run(app,host="127.0.0.1",port=args.backend_port,proxy_headers=False)
    finally:
        stop(frontend)


if __name__=="__main__":
    main()
