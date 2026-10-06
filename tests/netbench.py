"""Network benchmark: what clients receive per queue change, and what the server fetches.

Run against a fake-mode server with the real download path (tests/fakeserver.py, FAKE_UPSTREAM=1):

    docker run -d --name cesura-bench --network host -v "$PWD/tests:/app/tests:ro" -v "$CFG:/app/config" \\
        -e HOST_KEY=bench -e FAKE_UPSTREAM=1 cesura:dev python -m tests.fakeserver
    docker run --rm --network host -v "$PWD/tests:/app/tests:ro" cesura:dev \\
        python -m tests.netbench --base http://127.0.0.1:8765 --key bench

Connects a host dashboard and GUESTS phones, then: one bulk add (30 songs), 10
single adds, 10 removes of the head of the queue (a person clearing songs one by
one), 5 moves, a pause and a resume. Prints the bytes every client received per
step, and the server's totals from /api/host/net (pushes, fetches, peak download
concurrency and inbound bandwidth).
"""
import argparse
import json
import threading
import time
import urllib.parse
import urllib.request

from websockets.sync.client import connect

GUESTS = 4


class Client:
    def __init__(self, url: str, answer_hb=True):
        self.ws = connect(url, max_size=None)
        self.bytes = self.msgs = 0
        self.last_state = None
        self.token = ""
        self.answer_hb = answer_hb
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        try:
            for raw in self.ws:
                self.bytes += len(raw)
                self.msgs += 1
                m = json.loads(raw)
                if m.get("type") == "welcome":
                    self.token = m["token"]
                elif m.get("type") == "hb" and self.answer_hb:
                    self.ws.send('{"type":"hb"}')
                elif m.get("type") == "state":
                    self.last_state = m
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--key", default="bench")
    args = ap.parse_args()
    H = {"X-Host-Key": args.key, "Content-Type": "application/json"}

    def http(path, body=None, method=None):
        req = urllib.request.Request(args.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=H, method=method or ("POST" if body is not None else "GET"))
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")

    ws_base = args.base.replace("http", "ws", 1)
    host = Client(f"{ws_base}/ws/host?key={args.key}")
    for _ in range(50):
        if host.last_state:
            break
        time.sleep(0.1)
    code = host.last_state["code"]
    guests = [Client(f"{ws_base}/ws?code={code}&device=bench-guest-{i:04d}") for i in range(GUESTS)]
    time.sleep(1)
    ids = []
    for q in ("lorien testard", "daft punk", "radiohead", "nina simone"):
        r = http(f"/api/search?q={urllib.parse.quote(q)}&kind=songs&code={code}")
        ids += [x["videoId"] for x in r["results"] if x["videoId"] not in ids]
    print(f"{len(ids)} songs from search; {GUESTS} guests + host connected")
    clients = [host] + guests

    def state_queue():
        return host.last_state["queue"]  # the host dashboard's live view

    rows = []

    def step(name, fn, changes):
        net0 = http("/api/host/net")
        b0 = [c.bytes for c in clients]
        m0 = [c.msgs for c in clients]
        fn()
        time.sleep(2)
        net1 = http("/api/host/net")
        got = [c.bytes - b for c, b in zip(clients, b0)]
        msgs = [c.msgs - m for c, m in zip(clients, m0)]
        wire = sum(v["wire"] for v in net1["pushes"].values()) - sum(v["wire"] for v in net0["pushes"].values())
        rows.append((name, changes, sum(got), sum(msgs), wire))
        print(f"{name:<22} {changes:>3} changes  {sum(msgs):>4} msgs  {sum(got) / 1024:>8.1f} KB raw  "
              f"{wire / 1024:>7.1f} KB deflated  {sum(got) / max(1, changes) / len(clients) / 1024:>6.2f} KB/change/client")

    step("bulk add 30", lambda: http("/api/add", {"code": code, "videoIds": ids[:30]}), 1)
    step("10 single adds", lambda: [http("/api/add", {"code": code, "videoIds": [v]}) for v in ids[30:40]], 10)
    time.sleep(3)

    def removes():
        for _ in range(10):
            http("/api/remove", {"code": code, "qid": state_queue()[0]["qid"]})
            time.sleep(0.3)
    step("10 removes (head)", removes, 10)

    def moves():
        for i in range(5):
            q = state_queue()
            http("/api/mod/move", {"code": code, "qid": q[-1 - i]["qid"], "index": 0})
            time.sleep(0.3)
    step("5 moves to top", moves, 5)
    step("pause + resume", lambda: (http("/api/pause", {"code": code, "paused": True}),
                                    http("/api/pause", {"code": code, "paused": False})), 2)
    time.sleep(15)  # let downloads finish
    net = http("/api/host/net")
    print()
    print("server pushes:", json.dumps({k: {"msgs": v["msgs"], "KB": round(v["bytes"] / 1024, 1), "KB deflated": round(v["wire"] / 1024, 1)} for k, v in net["pushes"].items()}))
    print("server fetches:", json.dumps({k: {"n": v["n"], "MB": round(v["bytes"] / 1048576, 1)} for k, v in net["fetches"].items()}))
    print("peak concurrency:", net["peak_concurrency"], " peak inbound Mbit/s:", net["peak_inbound_mbit"])
    for c in clients:
        c.ws.close()


if __name__ == "__main__":
    main()
