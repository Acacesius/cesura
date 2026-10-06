"""State over the socket: the full state on connect (or when a client asks to resync), then patches.

A patch carries only what changed since that socket's last message:

    {"type": "patch", "qver": 12,
     "set": {"status": "paused", ...},          top-level keys whose value changed
     "del": ["roster"],                         keys that went away
     "q": {"base": 11, "rm": [qid, ...],        queue edits, from version `base` to `qver`
           "put": [[index, qid | item], ...]}}

Queue edits: drop the `rm` qids and every qid named in `put`, then insert the
`put` entries in order (ascending index). An entry is a bare qid when the item
moved but is unchanged (the client reuses its copy), or the whole item when it is
new or its contents changed. Items that keep their relative order are not
mentioned at all, so one drag is one entry and one add is one item.

A client whose `qver` isn't `base` (it missed a message) asks for a resync and
gets the full state again.
"""
import json


def _stable(seq: list[int]) -> set[int]:
    """Indexes (into seq) of a longest increasing subsequence: the items that can stay put."""
    n = len(seq)
    best, prev = [1] * n, [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    i = max(range(n), key=best.__getitem__, default=-1)
    keep = set()
    while i >= 0:
        keep.add(i)
        i = prev[i]
    return keep


def queue_ops(old: list[dict], new: list[dict]) -> dict:
    """The edits that turn `old` into `new` (lists of items with a "qid")."""
    old_at = {t["qid"]: i for i, t in enumerate(old)}
    old_by = {t["qid"]: t for t in old}
    new_ids = {t["qid"] for t in new}
    kept = [t for t in new if t["qid"] in old_at]
    keep = {kept[i]["qid"] for i in _stable([old_at[t["qid"]] for t in kept])}
    put = []
    for i, t in enumerate(new):
        q = t["qid"]
        if q not in old_at or old_by[q] != t:
            put.append([i, t])
        elif q not in keep:
            put.append([i, q])
    return {"rm": [t["qid"] for t in old if t["qid"] not in new_ids], "put": put}


class Sync:
    """What each socket was last sent, so the next message can be a patch."""

    def __init__(self):
        self.qver = 0
        self.queue: list[dict] = []
        self.ops: dict | None = None   # edits from qver - 1 to qver
        self.sent: dict = {}           # socket -> (state it has, its qver)

    def set_queue(self, items: list[dict]) -> bool:
        if items == self.queue:
            return False
        self.ops = {"base": self.qver, **queue_ops(self.queue, items)}
        self.qver += 1
        self.queue = items
        return True

    def message(self, ws, state: dict) -> dict | None:
        """The message that brings this socket up to `state` (without its "queue"), or None if it's current."""
        prev = self.sent.get(ws)
        if prev is None or prev[1] not in (self.qver, self.qver - 1):
            self.sent[ws] = (state, self.qver)
            return {"type": "state", "qver": self.qver, **state, "queue": self.queue}
        old, qver = prev
        msg: dict = {"type": "patch", "qver": self.qver}
        if changed := {k: v for k, v in state.items() if old.get(k) != v}:
            msg["set"] = changed
        if gone := [k for k in old if k not in state]:
            msg["del"] = gone
        if qver != self.qver:
            msg["q"] = self.ops
        if len(msg) == 2:
            return None
        self.sent[ws] = (state, self.qver)
        return msg

    def forget(self, ws):
        self.sent.pop(ws, None)


def dumps(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
