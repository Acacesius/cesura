"""End-to-end run of a fake-mode cesura (tests/fakeserver.py) in Chromium, via Playwright.

    docker run --rm --network host -v "$PWD:/src" -w /src mcr.microsoft.com/playwright/python:v1.55.0-noble \
        python tests/e2e.py --base http://127.0.0.1:18765 --code TQWXZ --key test-host-key --shots docs/screenshots

Drives a host dashboard and several phones through: device identity (two tabs,
one row), fair queue, vote-skip, co-host powers, kick, ban, code rotation,
lock, host library, pasted playlists, lyrics, pocket mode and the reconnect
banner. Fails on any page error. With --shots, saves the README screenshots.
"""
import argparse
import sys

from playwright.sync_api import expect, sync_playwright

PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True, "has_touch": True,
         "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"}

results: list[tuple[str, bool, str]] = []


def check(label, fn):
    try:
        fn()
        results.append((label, True, ""))
        print("PASS ", label, flush=True)
    except Exception as e:
        results.append((label, False, str(e).splitlines()[0][:200]))
        print("FAIL ", label, "::", str(e).splitlines()[0][:200], flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--shots")
    ap.add_argument("--restart", help="container name: restart it to test the reconnect banner")
    a = ap.parse_args()
    base, code = a.base, a.code
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        # Pocket mode runs on WebKit (Safari's engine): Playwright's Chromium is built without AAC.
        webkit = p.webkit.launch()

        def page_for(ctx, label):
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(f"{label}: {e}"))
            pg.on("dialog", lambda d: d.accept())
            return pg

        host_ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
        host = page_for(host_ctx, "host")
        host.goto(f"{base}/host?key={a.key}")
        check("host dashboard connects", lambda: expect(host.locator("#p-live")).to_contain_text("Live", timeout=10000))

        def join(name, label=None, engine=None):
            ctx = (engine or browser).new_context(**PHONE)
            pg = page_for(ctx, label or name)
            cur = host.locator("#codebox").inner_text().replace("\n", "") or code  # the test rotates it
            pg.goto(f"{base}/?code={cur}")
            pg.fill("#name-input", name)
            pg.click("#join-btn")
            expect(pg.locator("#room")).to_be_visible(timeout=10000)
            expect(pg.locator("#sync")).to_be_visible(timeout=10000)
            return ctx, pg

        maya_ctx, maya = join("Maya")
        theo_ctx, theo = join("Theo")
        jun_ctx, jun = join("Jun", engine=webkit)
        check("three guests show as three listener rows", lambda: expect(host.locator("#listeners .lrow")).to_have_count(3, timeout=8000))

        # Same device, second tab: still one row, with a ×2 badge.
        maya2 = page_for(maya_ctx, "maya-tab2")
        maya2.goto(f"{base}/")
        check("second tab on the same device: one row, ×2 badge", lambda: (
            expect(host.locator("#listeners .lrow")).to_have_count(3, timeout=8000),
            expect(host.locator("#listeners .lrow", has_text="Maya").locator(".tag", has_text="×2")).to_be_visible(timeout=8000)))
        maya2.close()

        # Search + queue: Maya adds 3, Theo adds 1 -> Theo's goes second (fair queue).
        def search_add(pg, q, n):
            pg.click("nav.tabbar [data-tab=search]")
            pg.fill("#q", q)
            pg.press("#q", "Enter")
            expect(pg.locator("#results .trow").first).to_be_visible(timeout=20000)
            for i in range(n):
                pg.locator("#results .trow [aria-label^='Add']").nth(i).click()
                pg.wait_for_timeout(250)

        search_add(maya, "bohemian rhapsody", 3)
        search_add(theo, "dancing queen", 1)
        check("fair queue: Theo's song is up next, ahead of Maya's backlog", lambda: (
            expect(host.locator("#queue .qrow").first.locator(".who")).to_contain_text("Theo", timeout=8000)))

        # Pocket mode on Jun.
        jun.click("nav.tabbar [data-tab=playing]")
        jun.click("#unlock") if jun.locator("#unlock").is_visible() else None
        jun.locator("#pocket-row").click()
        check("pocket mode: audio plays the shared stream", lambda: (
            expect(jun.locator("#sync")).to_contain_text("Pocket mode", timeout=5000),
            jun.wait_for_function("() => document.getElementById('audio').src.includes('/pocket?') && document.getElementById('audio').currentTime > 1", timeout=20000)))
        check("pocket shows as Pocket on the host and the station is on", lambda: (
            expect(host.locator("#listeners .lrow", has_text="Jun")).to_contain_text("Pocket", timeout=8000),
            expect(host.locator("#e-pocket")).to_contain_text("On", timeout=8000)))

        # Vote skip: 2 active non-pocket listeners (Maya, Theo) -> 1 vote needed at 1/2.
        for pg in (maya, theo):
            pg.click("nav.tabbar [data-tab=playing]")
            if pg.locator("#unlock").is_visible():
                pg.click("#unlock")
        maya.wait_for_timeout(1500)
        host.click("#settings")
        host.locator("#pop-votes button", has_text="All").click()
        host.keyboard.press("Escape")
        host.mouse.click(5, 500)
        first = host.locator("#np-title").inner_text()
        maya.wait_for_function("() => !document.getElementById('skip').disabled", timeout=15000)
        maya.click("#skip")
        check("vote skip: 1 of 2 votes shows on the button, song keeps playing", lambda: (
            expect(maya.locator("#skip .count")).to_have_text("1/2", timeout=5000),
            expect(host.locator("#np-title")).to_have_text(first)))
        theo.click("#skip")
        check("vote skip: second vote skips; votes reset", lambda: (
            expect(host.locator("#np-title")).not_to_have_text(first, timeout=8000),
            expect(maya.locator("#skip .count")).to_have_count(0, timeout=5000)))
        host.click("#settings")
        host.locator("#pop-votes button", has_text="½").click()
        host.mouse.click(5, 500)

        # Attribution: pause by Theo shows on now playing and in the feed.
        theo.wait_for_function("() => !document.getElementById('pause').disabled", timeout=15000)
        theo.click("#pause")
        check("attribution: 'Paused by Theo' on now playing and in Hands on it", lambda: (
            expect(maya.locator("#np-hands")).to_have_text("Paused by Theo", timeout=5000),
            expect(host.locator("#feed")).to_contain_text("Theo paused")))
        theo.click("#pause")

        # Co-host: promote Theo; Theo sees the roster and can remove Jun.
        def row_action(name, action):
            host.locator("#listeners .lrow", has_text=name).locator(".lmenu > button").click()
            host.locator("#listeners .lrow", has_text=name).locator(".popover button", has_text=action).click()

        row_action("Theo", "Make co-host")
        check("co-host badge on the host", lambda: expect(host.locator("#listeners .lrow", has_text="Theo").locator(".tag", has_text="CO-HOST")).to_be_visible(timeout=5000))
        theo.click("nav.tabbar [data-tab=queue]")
        theo.click("#q-count")
        check("co-host sees Remove buttons in the room sheet", lambda: expect(theo.locator("#people-list .prow", has_text="Jun").locator("button", has_text="Remove")).to_be_visible(timeout=5000))
        theo.locator("#people-list .prow", has_text="Jun").locator("button", has_text="Remove").click()
        check("co-host kick: Jun is out with 'removed' message", lambda: (
            expect(jun.locator("#join")).to_be_visible(timeout=8000),
            expect(jun.locator("#join-err")).to_contain_text("removed you")))
        theo.locator("#people-sheet [data-close]").click()
        check("co-host can't hit host-only endpoints", lambda: (
            theo.evaluate("async () => (await fetch('/api/host/code', {method:'POST'})).status") == 403 or (_ for _ in ()).throw(AssertionError("expected 403"))))

        # Jun rejoins (kick isn't a ban), then gets banned.
        jun.fill("#name-input", "Jun")
        jun.click("#join-btn")
        check("kicked guest can rejoin", lambda: expect(host.locator("#listeners .lrow", has_text="Jun")).to_be_visible(timeout=8000))
        row_action("Jun", "Ban")
        check("ban: Jun is out and can't rejoin", lambda: (
            expect(jun.locator("#join")).to_be_visible(timeout=8000),
            jun.click("#join-btn"),
            expect(jun.locator("#join-err")).to_contain_text("removed you", timeout=5000)))

        # Rotate the code: Maya stays connected and gets the new code.
        old = host.locator("#codebox").inner_text().replace("\n", "")
        host.click("#rotate-code")
        check("rotate code: new code on the dashboard, Maya stays in with it", lambda: (
            expect(host.locator("#code-note")).to_be_visible(timeout=5000),
            expect(maya.locator("[data-code]").first).not_to_have_text(old, timeout=5000),
            expect(maya.locator("#room")).to_be_visible()))
        new = host.locator("#codebox").inner_text().replace("\n", "")

        # Lock: a new phone is refused with a clear message.
        host.click("#lock-room")
        lea_ctx = browser.new_context(**PHONE)
        lea = page_for(lea_ctx, "lea")
        lea.goto(f"{base}/?code={new}")
        lea.fill("#name-input", "Lea")
        lea.click("#join-btn")
        check("locked room refuses a new device", lambda: expect(lea.locator("#join-err")).to_contain_text("locked", timeout=5000))
        host.click("#lock-room")

        # Library: off by default; host shares it; Maya browses and queues.
        maya.click("nav.tabbar [data-tab=library]")
        check("library: off by default, clean empty state", lambda: expect(maya.locator("#host-lib")).to_contain_text("isn't sharing", timeout=5000))
        host.click("#settings")
        host.click("#pop-library")
        host.mouse.click(5, 500)
        check("library: host's playlists appear when shared", lambda: expect(maya.locator("#host-lib .lrow-btn")).to_have_count(4, timeout=10000))
        if a.shots:
            maya.wait_for_timeout(3000)  # let the "new room code" toast clear
            maya.screenshot(path=f"{a.shots}/guest-library.png")
        maya.locator("#host-lib .lrow-btn", has_text="Road trip").click()
        check("library playlist sheet with Queue all (shuffled)", lambda: (
            expect(maya.locator("#lib-tracks .trow").first).to_be_visible(timeout=20000),
            expect(maya.locator("#lib-add")).to_contain_text("shuffled")))
        before = len(host.locator("#queue .qrow").all())
        maya.click("#lib-add")
        check("queue all goes through fair insertion", lambda: (
            expect(maya.locator("#lib-add")).to_contain_text("Added", timeout=8000),
            host.wait_for_function(f"() => document.querySelectorAll('#queue .qrow').length > {before}", timeout=8000)))
        maya.click("#lib-back")
        maya.fill("#paste-url", "https://example.com/not-a-playlist")
        maya.click("#paste-go")
        check("paste: rejects non-playlist links", lambda: expect(maya.locator("#paste-hint")).to_contain_text("isn't a playlist", timeout=8000))

        # Lyrics.
        maya.click("nav.tabbar [data-tab=playing]")
        maya.click("#lyrics-btn")
        check("lyrics sheet opens with lyrics or a clean empty state", lambda: expect(maya.locator("#lyrics-body .lyr, #lyrics-body .emptystate")).to_be_visible(timeout=20000))
        maya.locator("#lyrics-sheet [data-close]").click()

        # Export (fake library: no real playlist is created).
        host.click("#settings")
        host.click("#pop-export")
        check("session export shows the playlist link", lambda: expect(host.locator("#export-link")).to_contain_text("music.youtube.com/playlist?list=", timeout=10000))
        host.locator("#export-sheet [data-close]").click()

        # Lock screen metadata follows the track.
        check("media session metadata matches now playing", lambda: maya.wait_for_function(
            "() => navigator.mediaSession.metadata && navigator.mediaSession.metadata.title === document.getElementById('np-title').textContent", timeout=5000))

        # Paste a real public playlist (found via search).
        pl = maya.evaluate("async () => (await (await fetch('/api/search?q=road%20trip%20hits&kind=playlists&code=' + localStorage.getItem('cesura.code'))).json()).results[0].id")
        maya.click("nav.tabbar [data-tab=library]")
        maya.fill("#paste-url", f"https://music.youtube.com/playlist?list={pl}")
        maya.click("#paste-go")
        check("paste: a public playlist opens as a sheet", lambda: (
            expect(maya.locator("#lib-tracks .trow").first).to_be_visible(timeout=20000),
            expect(maya.locator("#lib-add")).to_contain_text("shuffled")))
        maya.click("#lib-back")

        # Touch gestures for co-hosts: swipe left removes, long-press + drag reorders (pins).
        theo.click("nav.tabbar [data-tab=queue]")
        theo.wait_for_timeout(500)
        def touch(pg, sel, steps, hold=0):
            pg.evaluate("""([sel, steps, hold]) => new Promise((done) => {
                const row = document.querySelector(sel), b = row.getBoundingClientRect();
                const x0 = b.left + b.width / 2, y0 = b.top + b.height / 2;
                const ev = (type, dx, dy) => row.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerId: 7, pointerType: "touch", isPrimary: true, clientX: x0 + dx, clientY: y0 + dy }));
                ev("pointerdown", 0, 0);
                setTimeout(() => {
                    let i = 0;
                    const t = setInterval(() => { const [dx, dy] = steps[i++]; ev("pointermove", dx, dy);
                        if (i === steps.length) { clearInterval(t); ev("pointerup", dx, dy); setTimeout(done, 300); } }, 30);
                }, hold);
            })""", [sel, steps, hold])
        n0 = theo.locator("#queue .trow").count()
        last_title = theo.locator("#queue .trow").nth(n0 - 1).locator(".t").inner_text()
        touch(theo, "#queue .trow:nth-child(2)", [[-20, 0], [-60, 0], [-120, 0], [-160, 0]])
        check("co-host swipe-to-remove", lambda: expect(theo.locator("#queue .trow")).to_have_count(n0 - 1, timeout=5000))
        last_title = theo.locator("#queue .trow").last.locator(".t").inner_text()
        touch(theo, "#queue .trow:last-child", [[0, -20], [0, -120], [0, -400], [0, -2000]], hold=600)
        check("co-host long-press reorder pins the song near the top", lambda: (
            expect(theo.locator("#queue .trow").nth(0).locator(".t")).to_have_text(last_title, timeout=5000),
            expect(host.locator("#queue .qrow").first.locator(".tag", has_text="PINNED")).to_be_visible(timeout=5000)))

        # Drag to reorder with the grip: a guest moves their own songs by touch (real touch events),
        # the host by mouse and by keyboard. The server decides; every client redraws from the broadcast.
        # Start from a short queue so every row is on screen: the host empties it, Maya adds 3, Theo 1.
        host.evaluate("""async () => { for (const t of state.queue) await fetch('/api/remove', { method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Host-Key': localStorage.getItem('cesura.hostkey') },
            body: JSON.stringify({ code: state.code, qid: t.qid }) }); }""")
        search_add(maya, "abba", 3)
        search_add(theo, "abba gimme", 1)
        maya.click("nav.tabbar [data-tab=queue]")
        maya.wait_for_timeout(800)
        own = maya.locator("#queue .trow:has(.grip)")
        check("guest sees grips on their own songs only", lambda: maya.wait_for_function(
            """() => [...document.querySelectorAll('#queue .trow')].every((r, i) => !!r.querySelector('.grip') === (mine(state.queue[i]) && !state.queue[i].pinned))
                && document.querySelectorAll('#queue .grip').length === 3""", timeout=5000))
        n_own = own.count()
        moved = own.nth(n_own - 1).locator(".t").inner_text()
        g, top = own.nth(n_own - 1).locator(".grip").bounding_box(), own.nth(0).bounding_box()
        cdp = maya_ctx.new_cdp_session(maya)
        x, y = g["x"] + g["width"] / 2, g["y"] + g["height"] / 2
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for k in range(1, 11):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y + (top["y"] - 10 - y) * k / 10}]})
            maya.wait_for_timeout(30)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        check("guest touch-drags their own song ahead of their others", lambda: (
            expect(maya.locator("#queue .trow:has(.grip)").first.locator(".t")).to_have_text(moved, timeout=5000)))

        rows = host.locator("#queue .qrow")
        last = rows.last.locator(".t").inner_text()
        g, first_box = rows.last.locator(".grip").bounding_box(), rows.first.bounding_box()
        host.mouse.move(g["x"] + g["width"] / 2, g["y"] + g["height"] / 2)
        host.mouse.down()
        for k in range(1, 13):
            host.mouse.move(g["x"] + g["width"] / 2, g["y"] + (first_box["y"] + 5 - g["y"]) * k / 12)
            host.wait_for_timeout(25)
        host.mouse.up()
        check("host mouse-drags the last song to the top; guests follow", lambda: (
            expect(rows.first.locator(".t")).to_have_text(last, timeout=5000),
            expect(maya.locator("#queue .trow").first.locator(".t")).to_have_text(last, timeout=5000)))
        second = rows.nth(1).locator(".t").inner_text()
        rows.nth(1).locator(".grip").focus()
        host.keyboard.press("ArrowUp")
        check("keyboard: ArrowUp on a grip moves it up and keeps focus", lambda: (
            expect(rows.first.locator(".t")).to_have_text(second, timeout=5000),
            host.wait_for_function("t => document.activeElement.classList.contains('grip') && document.activeElement.closest('.qrow').querySelector('.t').textContent === t", arg=second, timeout=3000)))

        # Tap targets: everything interactive on the phone screens is at least 44px in both directions (or wraps a 44px area).
        def small_targets(pg):
            return pg.evaluate("""() => [...document.querySelectorAll('button, a, input:not([type=checkbox]):not([type=range]), label.switch, [role=button]')]
                .filter(e => e.offsetParent && !e.closest('.hidden'))
                .map(e => [e, e.getBoundingClientRect()]).filter(([e, r]) => r.width && (r.height < 43.5 || r.width < 43.5))
                .map(([e, r]) => (e.id || e.className || e.tagName) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height))""")
        bad = []
        for tab in ("playing", "queue", "search", "library"):
            maya.click(f"nav.tabbar [data-tab={tab}]")
            maya.wait_for_timeout(300)
            bad += [f"{tab}: {x}" for x in small_targets(maya)]
        check("tap targets ≥ 44px on the guest screens", lambda: not bad or (_ for _ in ()).throw(AssertionError("; ".join(sorted(set(bad))[:12]))))

        # Reaping: a phone that goes away shows "Reconnecting", then drops off after the grace period.
        sam_ctx, sam = join("Sam")
        expect(host.locator("#listeners .lrow", has_text="Sam")).to_be_visible(timeout=8000)
        sam_ctx.close()
        check("gone device: 'Reconnecting' during grace, then dropped after ~30 s", lambda: (
            expect(host.locator("#listeners .lrow", has_text="Sam")).to_contain_text("Reconnecting", timeout=8000),
            expect(host.locator("#listeners .lrow", has_text="Sam")).to_have_count(0, timeout=45000)))

        if a.shots:
            take_shots(browser, host, maya, theo, base, a.shots)

        if a.restart:
            # Reconnect banner: the server goes away and comes back.
            import subprocess
            subprocess.run(["docker", "restart", "-t", "1", a.restart], check=True, capture_output=True)
            check("reconnecting banner while the server is down, then it clears", lambda: (
                expect(maya.locator("#conn")).to_be_visible(timeout=15000),
                expect(host.locator("#conn")).to_be_visible(timeout=15000),
                expect(maya.locator("#conn")).to_be_hidden(timeout=40000),
                expect(host.locator("#p-live")).to_contain_text("Live", timeout=20000)))
        for c in (maya_ctx, theo_ctx, jun_ctx, lea_ctx, host_ctx):
            c.close()
        browser.close()
        webkit.close()

    check("no page errors", lambda: not errors or (_ for _ in ()).throw(AssertionError("; ".join(errors[:5]))))
    failed = [r for r in results if not r[1]]
    print(f"\nE2E {'FAILED' if failed else 'PASSED'}: {len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


def take_shots(browser, host, maya, theo, base, out):
    host.reload()
    host.wait_for_timeout(2500)
    # Open one listener's action menu so the new row actions are visible.
    host.locator("#listeners .lrow", has_text="Theo").locator(".lmenu > button").click()
    host.wait_for_timeout(300)
    host.screenshot(path=f"{out}/host.png", full_page=True)
    host.mouse.click(5, 500)
    maya.click("nav.tabbar [data-tab=playing]")
    maya.wait_for_timeout(1200)
    maya.screenshot(path=f"{out}/guest-now-playing.png")
    maya.locator("#pocket-row").click()
    maya.wait_for_function("() => document.getElementById('sync').textContent.includes('Pocket')", timeout=10000)
    maya.wait_for_timeout(800)
    maya.screenshot(path=f"{out}/guest-pocket.png")
    maya.locator("#pocket-row").click()
    maya.click("nav.tabbar [data-tab=queue]")
    maya.wait_for_timeout(600)
    maya.screenshot(path=f"{out}/guest-queue.png")
    maya.click("nav.tabbar [data-tab=search]")
    maya.fill("#q", "bohemian rhapsody")
    maya.press("#q", "Enter")
    maya.locator("#results .trow").first.wait_for(timeout=20000)
    maya.wait_for_timeout(800)
    maya.screenshot(path=f"{out}/guest-search.png")
    ctx = browser.new_context(**PHONE)
    pg = ctx.new_page()
    pg.goto(f"{base}/")
    pg.evaluate("localStorage.clear()")
    pg.goto(f"{base}/")
    pg.wait_for_timeout(600)
    pg.screenshot(path=f"{out}/guest-join.png")
    ctx.close()


if __name__ == "__main__":
    sys.exit(main())
