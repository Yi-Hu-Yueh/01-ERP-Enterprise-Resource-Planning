"""Build only immutable Windows runtime; exports supply a fresh live snapshot.

Run after the one production frontend build. This fixed build never reads an
Owner database and never invokes npm or network installers.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "standalone-runtime" / "TIGER-ERP"
WORK = ROOT / "artifacts" / "standalone-build"
RUNTIME_DISTRIBUTIONS = ["fastapi", "starlette", "uvicorn", "SQLAlchemy", "alembic",
    "pydantic", "pydantic_core", "annotated-types", "annotated-doc", "typing_extensions",
    "typing-inspection", "anyio", "idna", "click", "colorama", "h11", "Mako", "MarkupSafe",
    "opentelemetry-api", "packaging", "Pygments", "setuptools"]


def _third_party_notices():
    parts = ["TIGER-ERP bundled runtime: third-party notices\n",
             "Python " + sys.version + "\n"]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python runtime license is missing")
    parts.append(python_license.read_text(encoding="utf-8"))
    versions = {}
    for name in RUNTIME_DISTRIBUTIONS:
        distribution = importlib.metadata.distribution(name)
        versions[name] = distribution.version
        parts.append(f"\n\n{'=' * 72}\n{name} {distribution.version}\n")
        found = False
        for file in distribution.files or []:
            normalized = str(file).replace("\\", "/")
            if ".dist-info/" in normalized and any(
                word in Path(normalized).name.lower() for word in ("license", "copying", "notice")):
                path = distribution.locate_file(file)
                if path.is_file():
                    parts.append(path.read_text(encoding="utf-8", errors="replace"))
                    found = True
        if not found:
            value = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
            parts.append(str(value or "See package metadata and upstream distribution for license terms."))
    # These are the production JS runtime and the CSS generator represented in
    # the compiled frontend, not the development/test dependency tree.
    for name in ("react", "react-dom", "scheduler", "tailwindcss"):
        directory = ROOT / "frontend" / "node_modules" / name
        package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        versions["npm/" + name] = package["version"]
        parts.append(f"\n\n{'=' * 72}\nnpm/{name} {package['version']}\n")
        license_files = [path for path in directory.iterdir() if path.is_file()
                         and path.name.lower().startswith(("license", "copying", "notice"))]
        if not license_files:
            raise RuntimeError(f"Bundled frontend package license is missing: {name}")
        for path in sorted(license_files):
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts), versions


def _source_fingerprints():
    sources = {ROOT / "backend" / "alembic.ini", ROOT / "backend" / "standalone.py",
        ROOT / "scripts" / "build_standalone.py", ROOT / "scripts" / "TIGER-ERP.spec",
        ROOT / "scripts" / "standalone-README.txt", ROOT / "scripts" / "packaging-requirements.txt",
        ROOT / "frontend" / "index.html", ROOT / "frontend" / "package.json",
        ROOT / "frontend" / "package-lock.json"}
    for directory in (ROOT / "backend" / "app", ROOT / "backend" / "migrations"):
        sources.update(directory.rglob("*.py"))
    sources.update(path for path in (ROOT / "frontend" / "dist").rglob("*") if path.is_file())
    sources.update(path for path in (ROOT / "frontend" / "src").rglob("*") if path.is_file())
    sources.update((ROOT / "frontend").glob("tsconfig*.json"))
    sources.update((ROOT / "frontend").glob("vite.config.*"))
    sources.update((ROOT / "backend").glob("requirements*.txt"))
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(sources)}


def _manifest(source_sha256):
    files = []
    for path in sorted(RUNTIME.rglob("*")):
        if not path.is_file() or path.name == "runtime-manifest.json":
            continue
        relative = path.relative_to(RUNTIME).as_posix()
        if path.is_symlink() or path.resolve() != path.absolute():
            raise RuntimeError(f"Runtime contains a linked file: {relative}")
        if relative.split("/", 1)[0] in {"data", "seed"}:
            raise RuntimeError("Immutable runtime must never include a database")
        if relative not in {"TIGER-ERP.exe", "README.txt", "THIRD-PARTY-LICENSES.txt"} and not relative.startswith("_internal/"):
            raise RuntimeError(f"Unexpected runtime output: {relative}")
        files.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "size": path.stat().st_size})
    if not (RUNTIME / "TIGER-ERP.exe").is_file() or not any(f["path"].startswith("_internal/") for f in files):
        raise RuntimeError("Build did not create a complete executable distribution")
    content = {"version": 1, "files": files, "source_sha256": source_sha256}
    (RUNTIME / "runtime-manifest.json").write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=WORK,
                        help="Task-owned directory beneath this project's artifacts; build cache is reused separately")
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    if not evidence.is_relative_to((ROOT / "artifacts").resolve()):
        raise SystemExit("Build evidence must stay beneath this project's artifacts directory.")
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise SystemExit("Build on Windows x64 with this project's .venv Python.")
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise SystemExit("Use the project .venv Python; no global installation is modified.")
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise SystemExit("Build the final production frontend once before building runtime.")
    if importlib.metadata.version("pyinstaller") != "6.22.3":
        raise SystemExit("Install scripts/packaging-requirements.txt in the project .venv first.")
    if RUNTIME.exists() and (RUNTIME / "data").exists():
        raise SystemExit("Refusing to rebuild a runtime folder containing active data.")
    if RUNTIME.exists() and (RUNTIME / "seed").exists():
        raise SystemExit("Refusing to rebuild a runtime folder containing a seed snapshot.")
    WORK.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    sources = _source_fingerprints()
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "PyInstaller", "--noconfirm",
        "--distpath", str(RUNTIME.parent), "--workpath", str(WORK / "pyinstaller"),
        str(ROOT / "scripts" / "TIGER-ERP.spec")], cwd=ROOT, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    if sources != _source_fingerprints():
        raise SystemExit("Application sources changed during compilation; runtime was not published as valid.")
    shutil.copyfile(ROOT / "scripts" / "standalone-README.txt", RUNTIME / "README.txt")
    notices, versions = _third_party_notices()
    (RUNTIME / "THIRD-PARTY-LICENSES.txt").write_text(notices, encoding="utf-8")
    manifest = _manifest(sources)
    report = {"runtime": str(RUNTIME), "files": len(manifest["files"]),
        "bytes": sum(f["size"] for f in manifest["files"]),
        "seconds": round(time.monotonic() - started, 3),
        "python": sys.version, "pyinstaller": importlib.metadata.version("pyinstaller"),
        "hooks": importlib.metadata.version("pyinstaller-hooks-contrib"), "dependencies": versions}
    (evidence / "build-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
