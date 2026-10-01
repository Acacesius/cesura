from app.library import TTLCache, normalize_playlist, session_tracks

PL = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def test_normalize_accepts_links_and_ids():
    for text in (PL, f"https://music.youtube.com/playlist?list={PL}", f"https://www.youtube.com/playlist?list={PL}&si=abc",
                 f"https://youtube.com/watch?v=dQw4w9WgXcQ&list={PL}&index=3", f"music.youtube.com/playlist?list={PL}",
                 f"https://music.youtube.com/browse/VL{PL}", f"VL{PL}"):
        assert normalize_playlist(text) == PL, text
    assert normalize_playlist("https://music.youtube.com/playlist?list=OLAK5uy_kq2tQ7jaJhx6PlNuz4PxhxzM0aYpKFAQo") \
        == "OLAK5uy_kq2tQ7jaJhx6PlNuz4PxhxzM0aYpKFAQo"


def test_normalize_rejects_everything_else():
    for text in ("", "LM", "WL", "https://music.youtube.com/playlist?list=LM", f"https://evil.example/playlist?list={PL}",
                 "javascript:alert(1)", f"ftp://youtube.com/?list={PL}", "https://youtu.be/dQw4w9WgXcQ",
                 PL + "<script>", "PL123"):
        assert normalize_playlist(text) is None, text


def test_session_tracks_dedupes_in_order():
    assert session_tracks([{"videoId": "a"}, {"videoId": "b"}, {"videoId": "a"}, {"videoId": "c"}]) == ["a", "b", "c"]


def test_ttl_cache_expires():
    t = [0.0]
    c = TTLCache(600, 2, clock=lambda: t[0])
    c.put("x", 1)
    t[0] = 599
    assert c.get("x") == 1
    t[0] = 1300
    assert c.get("x") is None
