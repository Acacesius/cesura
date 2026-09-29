#!/usr/bin/env python3
"""Smoke test for a cesura image. Starts a throwaway container with an empty
config, checks it, removes it. Used by CI before publishing; runnable locally:

    python3 tests/smoke.py --image cesura:dev

Checks: healthcheck goes healthy; runs as non-root; the setup token is printed
and /setup is gated by it; /host redirects to /setup; the in-image selftest
(ytmusicapi search + yt-dlp stream resolution) passes.
"""
import argparse
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def sh(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def request(url, method="GET", headers=None):
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(urllib.request.Request(url, method=method, headers=headers or {}), timeout=10)
        return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    args = ap.parse_args()
    port, cfg = free_port(), tempfile.mkdtemp(prefix="cesura-smoke-")
    import os
    os.chmod(cfg, 0o777)  # the container user must be able to write it, whatever the runner's uid
    name = f"cesura-smoke-{port}"
    failures = []

    def check(label, ok, extra=""):
        print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")
        if not ok:
            failures.append(label)

    sh("docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8765", "-v", f"{cfg}:/app/config", "--tmpfs", "/tmp", args.image)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            health = sh("docker", "inspect", "-f", "{{.State.Health.Status}}", name, check=False).stdout.strip()
            if health == "healthy":
                break
            time.sleep(2)
        check("healthcheck reports healthy", health == "healthy", health)
        uid = sh("docker", "exec", name, "id", "-u").stdout.strip()
        check("runs as non-root", uid != "0", f"uid {uid}")
        logs = sh("docker", "logs", name).stdout + sh("docker", "logs", name).stderr
        m = re.search(r"setup token:\s+([A-Za-z0-9_-]{20,})", logs)
        check("setup token printed in logs", bool(m))
        token = m.group(1) if m else ""
        status, headers = request(f"{base}/host")
        check("/host redirects to /setup before setup", status == 303 and headers.get("location") == "/setup", str(status))
        check("/setup is served", request(f"{base}/setup")[0] == 200)
        check("setup API rejects missing token", request(f"{base}/api/setup/verify", "POST")[0] == 403)
        check("setup API rejects wrong token", request(f"{base}/api/setup/verify", "POST", {"X-Setup-Token": "wrong"})[0] == 403)
        check("setup API accepts the printed token", request(f"{base}/api/setup/verify", "POST", {"X-Setup-Token": token})[0] == 200)
        st = sh("docker", "exec", name, "python", "-m", "app.selftest", check=False)
        try:
            res = json.loads(st.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            res = {"result": "fail", "detail": st.stderr[-300:]}
        check("selftest: deno + ytmusicapi + yt-dlp", res.get("result") == "pass",
              f"stream={res.get('stream')} search={res.get('search')} {res.get('detail') or ''}".strip())
    finally:
        sh("docker", "rm", "-f", name, check=False)
        sh("docker", "run", "--rm", "-v", f"{cfg}:/c", "--entrypoint", "sh", args.image, "-c", "rm -rf /c/* /c/.[!.]*", check=False)
    print("\nSMOKE", "FAILED: " + ", ".join(failures) if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
