"""Server behaviour through the real FastAPI app: identity, tokens, moderation, votes, fair adds.

No YouTube: there are no cookies, so tracks never load, and radio is off.
"""
import os
import tempfile

os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp())
os.environ["HOST_KEY"] = "test-host-key"

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main

H = {"X-Host-Key": "test-host-key"}
OPEN = []


@pytest.fixture()
def client():
    main.room.__init__()
    main.room.radio = False
    main.fail_log.clear()
    for i in range(40):
        vid = f"vid{i:08d}"
        main.search_cache[vid] = {"videoId": vid, "title": f"Song {i}", "artists": "X", "album": None, "duration": 200, "thumb": None}
    with TestClient(main.app) as c:
        yield c
        while OPEN:
            try:
                OPEN.pop().__exit__(None, None, None)
            except Exception:
                pass


def connect(c, device, code=None, token=""):
    ws = c.websocket_connect(f"/ws?code={code or main.room.code}&device={device}&token={token}")
    try:
        s = ws.__enter__()
    except Exception:
        raise
    OPEN.append(ws)
    welcome = s.receive_json()
    assert welcome["type"] == "welcome"
    s.receive_json()  # first state
    return s, welcome["token"]


def post(c, path, token="", headers=None, **body):
    return c.post(path, json={"code": main.room.code, "token": token, **body}, headers=headers or {})


def ids(n, start=0):
    return [f"vid{i:08d}" for i in range(start, start + n)]


def test_same_device_two_sockets_is_one_listener(client):
    a1, tok = connect(client, "device-aaaa")
    a2, tok2 = connect(client, "device-aaaa", token=tok)
    assert tok2 == tok
    rows = main.room.listener_rows()
    assert len(rows) == 1 and rows[0]["sockets"] == 2


def test_wrong_token_for_existing_device_is_refused(client):
    connect(client, "device-aaaa")
    with pytest.raises(WebSocketDisconnect) as e:
        connect(client, "device-aaaa", token="nope")
    assert e.value.code == 4009


def test_stream_needs_session_token(client):
    _, tok = connect(client, "device-aaaa")
    assert client.get(f"/stream/vid00000000?code={main.room.code}").status_code == 401
    # With a token it gets past auth (409: YouTube isn't linked in tests).
    assert client.get(f"/stream/vid00000000?code={main.room.code}&token={tok}").status_code == 409
    assert client.get(f"/pocket?code={main.room.code}").status_code == 401
    assert client.get(f"/stream/vid00000000?code=WRONG&token={tok}").status_code == 403


def test_actions_need_token_or_host_key(client):
    assert post(client, "/api/add", videoIds=ids(1)).status_code == 401
    assert post(client, "/api/add", headers=H, videoIds=ids(1)).status_code == 200
    assert post(client, "/api/add", headers={"X-Host-Key": "bad"}, videoIds=ids(1)).status_code == 403


def test_kick_closes_sockets_revokes_token_and_allows_rejoin(client):
    s, tok = connect(client, "device-aaaa")
    post(client, "/api/add", tok, videoIds=ids(1))
    pid = main.room.people.devices["device-aaaa"].pid
    assert post(client, "/api/mod/kick", headers=H, pid=pid).json()["ok"]
    while True:
        try:
            s.receive_json()
        except WebSocketDisconnect as e:
            assert e.code == 4004 and e.reason == "removed by host"
            break
    assert post(client, "/api/add", tok, videoIds=ids(1, 1)).status_code == 401
    connect(client, "device-aaaa")  # kick isn't a ban


def test_ban_blocks_rejoin_and_drops_their_songs(client):
    _, tok = connect(client, "device-aaaa")
    post(client, "/api/add", headers=H, videoIds=ids(1))
    post(client, "/api/add", tok, videoIds=ids(3, 1))
    assert len(main.room.queue) == 3
    pid = main.room.people.devices["device-aaaa"].pid
    client.post("/api/host/ban", json={"pid": pid}, headers=H)
    assert len(main.room.queue) == 0
    r = client.post("/api/join", json={"code": main.room.code, "device": "device-aaaa"})
    assert r.status_code == 409 and "removed" in r.json()["detail"]
    with pytest.raises(WebSocketDisconnect) as e:
        connect(client, "device-aaaa")
    assert e.value.code == 4005


def test_rotate_code_pushes_new_code_and_old_code_stops_working(client):
    s, tok = connect(client, "device-aaaa")
    old = main.room.code
    new = client.post("/api/host/code", headers=H).json()["code"]
    assert new != old
    msgs = [s.receive_json() for _ in range(3)]
    assert {"type": "code", "code": new} in msgs
    assert client.post("/api/join", json={"code": old}).status_code == 403
    # Someone in the room reconnects with their token even though their code is stale.
    connect(client, "device-aaaa", code=old, token=tok)


def test_lock_rejects_new_devices_only(client):
    connect(client, "device-aaaa")
    client.post("/api/host/lock", json={"on": True}, headers=H)
    r = client.post("/api/join", json={"code": main.room.code, "device": "device-bbbb"})
    assert r.status_code == 409 and "locked" in r.json()["detail"]
    with pytest.raises(WebSocketDisconnect) as e:
        connect(client, "device-bbbb")
    assert e.value.code == 4006


def test_cohost_powers_are_enforced_server_side(client):
    _, ctok = connect(client, "device-cohost")
    _, gtok = connect(client, "device-guest1")
    _, g2 = connect(client, "device-guest2")
    cpid = main.room.people.devices["device-cohost"].pid
    gpid = main.room.people.devices["device-guest2"].pid
    # A guest can't do mod things.
    assert post(client, "/api/mod/kick", gtok, pid=gpid).status_code == 403
    client.post("/api/host/cohost", json={"pid": cpid, "on": True}, headers=H)
    post(client, "/api/add", gtok, videoIds=ids(4))   # one starts playing, three queued
    q = main.room.queue
    assert post(client, "/api/mod/move", ctok, qid=q[2]["qid"], index=0).json()["ok"]
    assert post(client, "/api/remove", ctok, qid=main.room.queue[1]["qid"]).json()["ok"]
    assert post(client, "/api/mod/kick", ctok, pid=gpid).json()["ok"]
    # ...but not host-only things, whatever they send.
    for path in ("/api/host/ban", "/api/host/code", "/api/host/lock", "/api/host/settings", "/api/host/cohost",
                 "/api/new-session", "/api/host/export"):
        r = client.post(path, json={"code": main.room.code, "token": ctok, "pid": gpid, "on": True})
        assert r.status_code == 403, path
    assert client.post("/api/setup/verify", headers={"X-Setup-Token": ctok}).status_code in (403, 410)


def test_cohost_skips_instantly_guest_votes(client):
    _, ctok = connect(client, "device-cohost")
    _, g1 = connect(client, "device-guest1")
    _, g2 = connect(client, "device-guest2")
    _, g3 = connect(client, "device-guest3")
    for d in main.room.people.devices.values():
        d.listening = True
    post(client, "/api/add", headers=H, videoIds=ids(5))
    # 4 active listeners -> half -> 2 votes.
    assert main.room.votes_needed() == 2
    now = main.room.now["qid"]
    post(client, "/api/skip", g1, qid=now)
    assert main.room.now["qid"] == now and main.room.votes == {"device-guest1"}
    post(client, "/api/skip", g1, qid=now)  # same device twice counts once
    assert len(main.room.votes) == 1
    post(client, "/api/skip", g2, qid=now)
    assert main.room.now["qid"] != now and not main.room.votes  # votes reset on track change
    main.room.people.devices["device-cohost"].role = "cohost"
    now = main.room.now["qid"]
    post(client, "/api/skip", ctok, qid=now)
    assert main.room.now["qid"] != now
    assert "skipped to this" in main.room.hands["text"]


def test_vote_threshold_excludes_pocket_and_is_configurable(client):
    for i in range(5):
        connect(client, f"device-guest{i}")
    for d in main.room.people.devices.values():
        d.listening = True
    main.room.people.devices["device-guest4"].pocket = True
    assert main.room.votes_needed() == 2           # ceil(4 * 0.5)
    client.post("/api/host/settings?vote_fraction=0.75", headers=H)
    assert main.room.votes_needed() == 3
    client.post("/api/host/settings?vote_fraction=0.25", headers=H)
    assert main.room.votes_needed() == 1
    assert client.post("/api/host/settings?vote_fraction=0.1", headers=H).status_code == 400
    for d in main.room.people.devices.values():
        d.listening = False
    assert main.room.votes_needed() == 1           # minimum 1


def test_guest_play_next_is_front_of_own_turns_host_is_true_next(client):
    _, a = connect(client, "device-aaaa")
    _, b = connect(client, "device-bbbb")
    post(client, "/api/add", a, videoIds=ids(4))           # first one starts playing
    post(client, "/api/add", b, videoIds=ids(2, 10))
    titles = lambda: [t["videoId"] for t in main.room.queue]
    # A's first song is playing, so B is up next, then turns alternate.
    assert titles() == ["vid00000010", "vid00000001", "vid00000011", "vid00000002", "vid00000003"]
    post(client, "/api/add", a, videoIds=ids(1, 20), next=True)
    assert titles()[:2] == ["vid00000010", "vid00000020"]   # A's next turn, not ahead of B
    post(client, "/api/add", headers=H, videoIds=ids(1, 30), next=True)
    assert titles()[0] == "vid00000030"


def test_attribution_feed_and_queue_items(client):
    s, a = connect(client, "device-aaaa")
    s.send_json({"type": "hello", "name": "Sam"})
    s.receive_json()
    post(client, "/api/add", a, videoIds=ids(2))
    post(client, "/api/pause", a, paused=True)
    post(client, "/api/pause", headers=H, paused=False)
    feed = main.room.host_panel()["feed"]
    assert [(e["who"], e["kind"]) for e in feed[:3]] == [("Host", "resume"), ("Sam", "pause"), ("Sam", "queue")]
    assert main.room.queue[0]["by"] == "Sam"
    snap = main.room.snapshot(main.room.people.devices["device-aaaa"])
    assert snap["queue"][0]["mine"] and "owner" not in snap["queue"][0]
    assert snap["hands"]["text"] == "Resumed by Host"


def test_library_is_off_by_default_and_needs_code(client):
    assert client.get(f"/api/library?code={main.room.code}").status_code == 409
    assert client.get("/api/library?code=WRONG").status_code == 403
    assert client.get(f"/api/library/playlist?id=LM&code={main.room.code}").status_code == 409


def test_pasted_playlist_rejects_non_playlists(client):
    for bad in ("https://evil.example/?list=PLxxxxxxxxxxxxxx", "hello", "https://music.youtube.com/watch?v=abcdefghijk",
                "https://music.youtube.com/playlist?list=LM"):
        assert client.get("/api/playlist", params={"url": bad, "code": main.room.code}).status_code == 400, bad


def test_heartbeat_reaps_silent_socket(client, monkeypatch):
    s, tok = connect(client, "device-aaaa")
    p = main.room.people
    ws = next(iter(p.devices["device-aaaa"].sockets))
    p.missed[ws] = 3
    _, dead = p.heartbeat()
    assert dead == [ws]
