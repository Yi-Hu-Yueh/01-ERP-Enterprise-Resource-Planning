"""Run both loopback-only services. Never kills unrelated processes or resets a database."""
import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def port_available(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def stop(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        process.terminate()
    process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "erp.sqlite")
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix:
        raise SystemExit("Use the project .venv Python: .venv\\Scripts\\python.exe scripts\\dev.py")
    for port in [args.backend_port, args.frontend_port]:
        if not port_available(port):
            raise SystemExit(f"127.0.0.1:{port} is occupied; no process was stopped.")
    node = shutil.which("node")
    vite = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    if not node or not vite.exists():
        raise SystemExit("Install frontend dependencies with npm ci first.")
    environment = {**os.environ, "ERP_DB_PATH": str(args.database.resolve()),
                   "ERP_API_TARGET": f"http://127.0.0.1:{args.backend_port}"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    children = []
    try:
        children.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app.main:app",
                          "--host", "127.0.0.1", "--port", str(args.backend_port)], cwd=ROOT,
                          env=environment, creationflags=flags))
        children.append(subprocess.Popen([node, str(vite), "--host", "127.0.0.1",
                          "--port", str(args.frontend_port), "--strictPort"], cwd=ROOT / "frontend",
                          env=environment, creationflags=flags))
        print(f"UI: http://127.0.0.1:{args.frontend_port}", flush=True)
        print(f"API: http://127.0.0.1:{args.backend_port}/docs", flush=True)
        print("LOCAL DEMO MODE — NO VERIFIED LOGIN. Ctrl+C stops both owned services.", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
        if any(p.returncode for p in children if p.returncode is not None):
            raise SystemExit("A service exited; inspect its error above. Existing data was not reset.")
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(children):
            stop(process)


if __name__ == "__main__":
    main()

