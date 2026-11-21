"""Explicit isolated QA runtime; accepts no database override or authorization flag."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import uvicorn
from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.tests.issue_qa_runtime import new_qa_application
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
from backend.tests.test_mrp import seed


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--backend-port",type=int,default=8023)
    parser.add_argument("--frontend-port",type=int,default=5186)
    args=parser.parse_args()
    out=args.output.resolve()
    assert out.is_relative_to(ROOT/"artifacts")
    app,path=new_qa_application(out)
    with TestClient(create_app(path)) as client:
        r=po.__wrapped__((client,client.app.state.db,path))
        seed(inquiry.__wrapped__(returns.__wrapped__(r)))
        descriptor=dict(database=str(path),**{k:r[k] for k in ["orders","raws","boms","warehouse","warehouse2"]})
    (out/"authorized-qa.json").write_text(json.dumps(descriptor,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    environment={**os.environ,"ERP_API_TARGET":"http://127.0.0.1:"+str(args.backend_port)}
    frontend=subprocess.Popen([shutil.which("node"),str(ROOT/"frontend/node_modules/vite/bin/vite.js"),
        "--host","127.0.0.1","--port",str(args.frontend_port),"--strictPort"],cwd=ROOT/"frontend",env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    try:
        uvicorn.run(app,host="127.0.0.1",port=args.backend_port,log_level="info")
    finally:
        if frontend.poll() is None:
            if os.name=="nt":
                subprocess.run(["taskkill","/PID",str(frontend.pid),"/T","/F"],stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            else:frontend.terminate()
        frontend.wait(timeout=15)

if __name__=="__main__":main()
