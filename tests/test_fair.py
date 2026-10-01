from app.fair import FairQueue


def mk(name):
    return {"qid": name, "title": name}


def order(fq):
    return [t["qid"] for t in fq]


def test_round_robin_interleaves_owners():
    fq = FairQueue()
    fq.add([mk(f"a{i}") for i in range(1, 5)], "A")
    fq.add([mk("b1"), mk("b2")], "B")
    fq.add([mk("c1")], "C")
    assert order(fq) == ["a1", "b1", "c1", "a2", "b2", "a3", "a4"]


def test_one_persons_30_tracks_dont_bury_others():
    fq = FairQueue()
    fq.add([mk(f"a{i}") for i in range(30)], "A")
    fq.add([mk("b1")], "B")
    assert order(fq).index("b1") == 1


def test_owner_goes_to_back_of_line_after_their_turn():
    fq = FairQueue()
    fq.add([mk("a1"), mk("a2")], "A")
    fq.add([mk("b1"), mk("b2")], "B")
    assert fq.pop()["qid"] == "a1"
    assert order(fq) == ["b1", "a2", "b2"]
    fq.add([mk("c1")], "C")  # newcomer goes ahead of A, whose song is playing now
    assert order(fq) == ["b1", "c1", "a2", "b2"]


def test_newcomer_is_next_after_a_solo_owner():
    fq = FairQueue()
    fq.add([mk("a1"), mk("a2"), mk("a3")], "A")
    fq.pop()                               # a1 is playing
    fq.add([mk("b1")], "B")
    assert order(fq) == ["b1", "a2", "a3"]


def test_adding_never_reorders_existing_items():
    fq = FairQueue()
    fq.add([mk("a1"), mk("a2")], "A")
    fq.add([mk("b1")], "B")
    before = order(fq)
    fq.add([mk("a3")], "A")
    assert [q for q in order(fq) if q != "a3"] == before


def test_mine_first_is_front_of_my_turns_only():
    fq = FairQueue()
    fq.add([mk("a1"), mk("a2")], "A")
    fq.add([mk("b1"), mk("b2")], "B")
    fq.add([mk("b0")], "B", how="mine_first")
    # B's new track takes B's next turn; A still goes first.
    assert order(fq) == ["a1", "b0", "a2", "b1", "b2"]


def test_play_next_pins_to_front():
    fq = FairQueue()
    fq.add([mk("a1"), mk("a2")], "A")
    fq.add([mk("h1")], "host", how="next")
    assert order(fq)[0] == "h1"
    fq.add([mk("h2")], "host", how="next")
    assert order(fq)[:2] == ["h2", "h1"]


def test_manual_move_pins_position_against_new_adds():
    fq = FairQueue()
    fq.add([mk("a1"), mk("a2"), mk("a3")], "A")
    fq.move("a3", 1)
    assert order(fq) == ["a1", "a3", "a2"]
    fq.add([mk("b1"), mk("b2")], "B")
    assert order(fq)[1] == "a3"            # stays 2nd
    assert order(fq) == ["a1", "a3", "b1", "a2", "b2"]


def test_pinned_item_moves_up_as_queue_plays():
    fq = FairQueue()
    fq.add([mk("a1"), mk("a2"), mk("a3")], "A")
    fq.move("a3", 2)
    fq.add([mk("b1")], "B")
    assert order(fq) == ["a1", "b1", "a3", "a2"]
    fq.pop()
    assert order(fq) == ["b1", "a3", "a2"]
    assert fq.find("a3")["pin"] == 1


def test_pinned_head_does_not_rotate_turns():
    fq = FairQueue()
    fq.add([mk("a1")], "A")
    fq.add([mk("b1")], "B")
    fq.add([mk("h")], "host", how="next")
    fq.pop()
    assert order(fq) == ["a1", "b1"]


def test_remove_and_retain():
    fq = FairQueue()
    fq.add([mk("a1"), mk("a2")], "A")
    fq.add([mk("b1")], "B")
    fq.remove("a1")
    assert order(fq) == ["a2", "b1"]
    fq.retain(lambda t: t["owner"] != "A")
    assert order(fq) == ["b1"]
    assert fq.turns == ["B"]
