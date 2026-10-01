from app import presence
from app.presence import Presence


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_reconnect_updates_same_device_and_counts_sockets():
    p = Presence(Clock())
    d, st = p.attach("ws1", "dev-aaaaaaaa")
    assert st == "ok"
    d2, st = p.attach("ws2", "dev-aaaaaaaa", d.token)
    assert d2 is d and len(d.sockets) == 2 and len(p.devices) == 1


def test_claiming_a_device_without_its_token_is_refused():
    p = Presence(Clock())
    p.attach("ws1", "dev-aaaaaaaa")
    d, st = p.attach("ws2", "dev-aaaaaaaa", "guess")
    assert d is None and st == "mismatch"


def test_heartbeat_closes_socket_after_three_missed_pongs():
    p = Presence(Clock())
    p.attach("ws1", "dev-aaaaaaaa")
    p.attach("ws2", "dev-bbbbbbbb")
    for _ in range(3):
        ping, dead = p.heartbeat()
        assert dead == []
        p.pong("ws2")
    ping, dead = p.heartbeat()
    assert dead == ["ws1"] and ping == ["ws2"]


def test_grace_period_before_drop_and_reconnect_cancels_it():
    c = Clock()
    p = Presence(c)
    d, _ = p.attach("ws1", "dev-aaaaaaaa")
    p.detach("ws1")
    c.t += presence.GRACE - 1
    assert p.reap() == []
    p.attach("ws9", "dev-aaaaaaaa", d.token)   # brief reconnect: no flicker
    p.detach("ws9")
    c.t += presence.GRACE - 1
    assert p.reap() == []
    c.t += 2
    assert p.reap() == [d] and not p.devices
    assert p.by_token(d.token) is None


def test_open_pocket_stream_keeps_device_alive_without_sockets():
    c = Clock()
    p = Presence(c)
    d, _ = p.attach("ws1", "dev-aaaaaaaa")
    p.stream_opened(d)
    p.detach("ws1")          # iOS froze the page; the socket got reaped
    c.t += 120
    assert p.reap() == [] and d.in_pocket
    p.stream_closed(d)
    c.t += presence.GRACE
    assert p.reap() == [d]


def test_kick_revokes_token_but_allows_rejoin_with_new_token():
    p = Presence(Clock())
    d, _ = p.attach("ws1", "dev-aaaaaaaa")
    old = d.token
    assert p.kick("dev-aaaaaaaa") is d
    assert p.by_token(old) is None
    d2, st = p.attach("ws2", "dev-aaaaaaaa")
    assert st == "ok" and d2.token != old


def test_ban_blocks_rejoin_and_survives_new_session():
    p = Presence(Clock())
    p.attach("ws1", "dev-aaaaaaaa")
    p.ban("dev-aaaaaaaa")
    assert p.attach("ws2", "dev-aaaaaaaa") == (None, "banned")
    p.reset()
    assert p.attach("ws3", "dev-aaaaaaaa") == (None, "banned")


def test_lock_rejects_new_devices_but_not_known_ones():
    c = Clock()
    p = Presence(c)
    d, _ = p.attach("ws1", "dev-aaaaaaaa")
    p.locked = True
    assert p.attach("ws2", "dev-bbbbbbbb") == (None, "locked")
    p.detach("ws1")
    c.t += presence.GRACE + 1
    p.reap()
    d2, st = p.attach("ws3", "dev-aaaaaaaa")   # known this session: may come back
    assert st == "ok"
    p.kick("dev-aaaaaaaa")                    # kicked while locked: can't
    assert p.attach("ws4", "dev-aaaaaaaa") == (None, "locked")


def test_voters_exclude_pocket_and_idle_devices():
    p = Presence(Clock())
    a, _ = p.attach("ws1", "dev-aaaaaaaa")
    b, _ = p.attach("ws2", "dev-bbbbbbbb")
    c, _ = p.attach("ws3", "dev-cccccccc")
    a.listening = b.listening = True
    b.pocket = True
    assert p.voters() == [a]
    assert set(p.listening()) == {a, b}
