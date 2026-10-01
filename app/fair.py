"""The fair queue: everyone who adds songs takes turns.

Each owner (a guest device, or "host") has their own sequence of tracks, and
the queue plays round-robin across owners: one from whoever is up, then one
from the next, and so on. So one person adding 30 songs doesn't bury everyone
else's.

`turns` is the order owners take their turns in. When an owner's track plays,
they move to the back of the line. Someone new joins the line just ahead of
whoever played most recently (so a newcomer doesn't wait behind the person
whose song is on right now). Owners with nothing queued drop out of it.

Pinned items sit outside the rotation at a fixed index. This is what a host or
co-host drag does ("this one goes 3rd"), and what host/co-host "play next" does
(index 0). Removing something ahead of a pinned item moves it up by one, like
any list. Pinning something ahead of it moves it down by one. Ordinary adds
never move it.
"""


class FairQueue:
    def __init__(self):
        self.items: list[dict] = []
        self.turns: list[str] = []
        self.last: str | None = None  # owner whose turn came most recently

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def clear(self):
        self.items.clear()
        self.turns.clear()
        self.last = None

    def find(self, qid: str) -> dict | None:
        return next((t for t in self.items if t["qid"] == qid), None)

    # ---- mutations
    def add(self, items: list[dict], owner: str, how: str = "end"):
        """how: "end" = back of my turns, "mine_first" = front of my turns, "next" = pinned at the very front."""
        for t in items:
            t["owner"] = owner
            t["pin"] = None
        if how == "next":
            for i, t in enumerate(items):
                self._shift_pins(i, +1)
                t["pin"] = i
            self.items[0:0] = items
            self._arrange()
            return
        seqs = self._seqs()
        mine = seqs.setdefault(owner, [])
        if how == "mine_first":
            mine[0:0] = items
        else:
            mine.extend(items)
        if owner not in self.turns:
            at = self.turns.index(self.last) if self.last in self.turns else len(self.turns)
            self.turns.insert(at, owner)
        self._arrange(seqs)

    def remove(self, qid: str) -> dict | None:
        t = self.find(qid)
        if t is None:
            return None
        self._drop(t)
        self._arrange()
        return t

    def pop(self) -> dict | None:
        """Take the head to play it. An unpinned head means its owner just had their turn."""
        if not self.items:
            return None
        t = self.items[0]
        self._drop(t)
        owner = t.get("owner")
        if t.get("pin") is None:
            self.last = owner
            if owner in self.turns:
                self.turns.remove(owner)
                self.turns.append(owner)
        self._arrange()
        return t

    def move(self, qid: str, index: int) -> bool:
        """Manual reorder: pin the item at `index`, overriding the rotation for it."""
        t = self.find(qid)
        if t is None:
            return False
        self._drop(t)
        index = max(0, min(index, len(self.items)))
        self._shift_pins(index, +1)
        t["pin"] = index
        self.items.insert(index, t)
        self._arrange()
        return True

    def retain(self, keep) -> list[dict]:
        """Drop every item where keep(item) is false (e.g. a kicked guest's tracks). Returns what was dropped."""
        gone = [t for t in self.items if not keep(t)]
        for t in gone:
            self._drop(t)
        self._arrange()
        return gone

    # ---- internals
    def _drop(self, t: dict):
        j = self.items.index(t)
        self.items.pop(j)
        for o in self.items:
            if o.get("pin") is not None and o["pin"] > j:
                o["pin"] -= 1

    def _shift_pins(self, at: int, by: int):
        for o in self.items:
            if o.get("pin") is not None and o["pin"] >= at:
                o["pin"] += by

    def _seqs(self) -> dict[str, list[dict]]:
        seqs: dict[str, list[dict]] = {}
        for t in self.items:
            if t.get("pin") is None:
                seqs.setdefault(t.get("owner") or "", []).append(t)
        return seqs

    def _arrange(self, seqs: dict[str, list[dict]] | None = None):
        seqs = self._seqs() if seqs is None else seqs
        self.turns = [o for o in self.turns if seqs.get(o)]
        self.turns += [o for o in seqs if seqs[o] and o not in self.turns]
        out: list[dict] = []
        r = 0
        while True:
            row = [seqs[o][r] for o in self.turns if r < len(seqs[o])]
            if not row:
                break
            out += row
            r += 1
        for t in sorted((t for t in self.items if t.get("pin") is not None), key=lambda t: t["pin"]):
            out.insert(min(t["pin"], len(out)), t)
        for i, t in enumerate(out):  # a pin past the end lands at the end; record where it really is
            if t.get("pin") is not None:
                t["pin"] = i
        self.items = out
