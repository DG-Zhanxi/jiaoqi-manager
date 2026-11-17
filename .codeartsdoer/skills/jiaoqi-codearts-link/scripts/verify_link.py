"""Verify a public CodeArts mirror without reading credentials."""
import argparse
import datetime
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

CLOUD_REPO = "https://codehub.devcloud.cn-southwest-2.huaweicloud.com/e7989a0bb95d41038306ce4b0a94fcf7/jiaoqi-manager.git"

def verify(url, expected):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "codehub.devcloud.cn-southwest-2.huaweicloud.com" or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Expected a public Huawei Cloud CodeArts HTTPS repository")
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ValueError("Expected a full 40-character commit SHA")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.interactive=never", "ls-remote", url, "refs/heads/main"], capture_output=True, text=True, timeout=40, env=env)
    if result.returncode:
        raise RuntimeError(f"Public Git verification failed with exit {result.returncode}")
    rows = [line.split() for line in result.stdout.splitlines()]
    actual = next((row[0] for row in rows if len(row)==2 and row[1]=="refs/heads/main"), None)
    if actual != expected:
        raise RuntimeError("Cloud mirror commit differs from the expected release")
    return {"service":"Huawei Cloud CodeArts Repo", "repository":url, "commit":actual, "anonymous_read":True, "verified_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--repository",default=CLOUD_REPO)
    parser.add_argument("--expected-commit",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=verify(args.repository,args.expected_commit)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))
