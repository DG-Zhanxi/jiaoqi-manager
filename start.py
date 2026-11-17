"""Cross-platform launcher: python start.py --port 8788."""
from pathlib import Path
import argparse
import os
import sys

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description="交期管家")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    os.environ["JIAOQI_ROOT"] = str(ROOT)
    os.environ.setdefault("JIAOQI_ALLOWED_ORIGINS", f"http://127.0.0.1:{args.port},http://localhost:{args.port}")
    for name in ["app/var", "runtime", "secrets"]:
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "app"))
    import uvicorn
    uvicorn.run("backend.server:app", host=args.host, port=args.port)

if __name__ == "__main__":
    main()
