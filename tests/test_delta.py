"""The queue edits in patches reproduce the server's queue, whatever changed."""
import random

from app.delta import Sync, queue_ops


def apply(old, ops):
    """What a client does with a patch's "q" (the same steps as static/ds/core.js applyQueue)."""
    by = {t["qid"]: t for t in old}
    gone = set(ops["rm"]) | {p[1] if isinstance(p[1], str) else p[1]["qid"] for p in ops["put"]}
    out = [t for t in old if t["qid"] not in gone]
    for i, x in ops["put"]:
        out.insert(i, by[x] if isinstance(x, str) else x)
    return out


def item(q, v=0):
    return {"qid": q, "title": f"song {q}", "v": v}


def test_single_move_is_one_entry():
    old = [item(c) for c in "abcdef"]
    new = [old[4]] + old[:4] + old[5:]
    ops = queue_ops(old, new)
    assert ops == {"rm": [], "put": [[0, "e"]]}
    assert apply(old, ops) == new


def test_add_and_remove_send_only_the_new_item():
    old = [item(c) for c in "abc"]
    new = [old[0], item("x"), old[2]]
    ops = queue_ops(old, new)
    assert ops == {"rm": ["b"], "put": [[1, item("x")]]}


def test_changed_item_is_resent():
    old = [item("a"), item("b")]
    new = [item("a"), item("b", 1)]
    assert queue_ops(old, new)["put"] == [[1, item("b", 1)]]


def test_random_edits_round_trip():
    rnd = random.Random(7)
    q = [item(str(i)) for i in range(30)]
    n = 30
    for _ in range(500):
        new = list(q)
        for _ in range(rnd.randint(1, 4)):
            r = rnd.random()
            if r < 0.3 and new:
                new.pop(rnd.randrange(len(new)))
            elif r < 0.6:
                n += 1
                new.insert(rnd.randint(0, len(new)), item(str(n)))
            elif new:
                t = new.pop(rnd.randrange(len(new)))
                new.insert(rnd.randint(0, len(new)), t)
        rnd.shuffle(new) if rnd.random() < 0.05 else None
        assert apply(q, queue_ops(q, new)) == new
        q = new


def test_sync_sends_full_state_then_patches_then_resync():
    s = Sync()
    ws = object()
    s.set_queue([item("a")])
    first = s.message(ws, {"status": "playing", "x": 1})
    assert first["type"] == "state" and first["queue"] == [item("a")]
    assert s.message(ws, {"status": "playing", "x": 1}) is None  # nothing changed
    s.set_queue([item("a"), item("b")])
    p = s.message(ws, {"status": "paused", "x": 1})
    assert p == {"type": "patch", "qver": 2, "set": {"status": "paused"}, "q": {"base": 1, "rm": [], "put": [[1, item("b")]]}}
    s.set_queue([item("b")])
    s.set_queue([item("c")])  # this socket missed a version: full state again
    assert s.message(ws, {"status": "paused", "x": 1})["type"] == "state"
