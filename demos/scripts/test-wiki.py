#!/usr/bin/env python3
"""Checks for wiki.py that a screenshot cannot make.

This panel can draw a beautiful, confident, wrong picture in five ways, and
none of them look wrong on the wall:

  1. **The vertical axis can be upside down.** A field of strokes is a field of
     strokes; one with deletions rising and additions falling is exactly as
     pretty and says the opposite thing about whether the encyclopedia is
     growing.
  2. **The bot dimming can be backwards, or absent.** Then the brightest thing
     on the panel is a category bot, and the one fact it is trying to
     communicate -- that most edits are made by software -- is inverted.
  3. **The time axis can be a queue rather than a clock.** If the strokes are
     laid down evenly instead of at their arrival times, the burstiness is gone
     and the panel is a texture rather than a recording. It still looks fine.
  4. **A non-Latin title can reach the crawl** and draw a row of blank boxes,
     or worse, a row of the wrong letters.
  5. **A username could reach the record.** This is the one that matters, and
     it is checked against the *live* cached record rather than a synthetic
     one, because it is ftdata.py's fetcher being tested and not this demo.

So the drawing is asserted **in pixels** against synthetic windows whose answers
cannot be argued with -- one edit, in a known direction, at a known millisecond
-- and the privacy rule is asserted against whatever is actually in the cache.

`ftdata.CACHE_DIR` binds at import, so the three data states a demo must handle
-- fresh, stale, absent -- each get a **separate process** with FT_DATA_CACHE
set, at the bottom of this file.

    $ python3 scripts/test-wiki.py                     # uses the live cache
    $ python3 scripts/test-wiki.py --cache-dir /tmp/c  # or a pointed one

The live cache is only needed for the checks against real data; everything else
builds its own cache directory and needs nothing. Populate it with
`python3 ftdata.py --once --only wiki-stream`.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                         # noqa: E402
import ftdata                                                  # noqa: E402
import wiki                                                    # noqa: E402

FAILED = []
PASSED = [0]

SECS = 40.0


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(wiki, **kw)


def frames(args, n=4):
    """Render `n` frames in order from a fresh build."""
    r = wiki.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


# --------------------------------------------------------------------------
# A synthetic window, so that every question has an arithmetic answer.
# --------------------------------------------------------------------------

def synthetic(cache_dir, events=None, titles=None, names=None,
              fetched_ago=60.0, secs=SECS, **extra):
    """Write a wiki-stream record. events: (ms, delta, project_index, flags)."""
    os.makedirs(cache_dir, exist_ok=True)
    if events is None:
        # Twelve human additions, spread a second apart, all from project 0.
        events = [(i * 1000, 100, 0, 0) for i in range(12)]
    names = names or ["enwiki", "commonswiki", "frwiki"]
    payload = {
        "secs": secs, "n": len(events), "n_all": len(events) * 2,
        "per_s": len(events) / (secs or 1.0),
        "all_per_s": 2 * len(events) / (secs or 1.0),
        "bot_pct": 100.0 * sum(1 for e in events if e[3] & 1) / max(1, len(events)),
        "n_new": sum(1 for e in events if e[3] & 2),
        "add_bytes": sum(e[1] for e in events if e[1] > 0),
        "del_bytes": -sum(e[1] for e in events if e[1] < 0),
        "n_projects": len(names), "pnames": names,
        "projects": [[n, 1] for n in names],
        "ms": [e[0] for e in events], "d": [e[1] for e in events],
        "pi": [e[2] for e in events], "f": [e[3] for e in events],
        "titles": titles or [],
        "n_titles_seen": len(titles or []),
        "title_note": "namespace 0, Latin-script only",
        "source": "synthetic",
    }
    payload.update(extra)
    rec = {"name": "wiki-stream", "fetched_at": time.time() - fetched_ago,
           "source": "synthetic", "ttl": ftdata.ttl_for("wiki-stream"),
           "payload": payload}
    with open(os.path.join(cache_dir, "wiki-stream.json"), "w") as fh:
        json.dump(rec, fh)
    return cache_dir


def contains_text(frame, s, thresh=80):
    """Is this string drawn anywhere on the frame, at either size?

    Reading the words back off the panel is the only way to be sure the honest
    message reached it rather than merely being computed. The counters between
    the strokes have to be dark too, or a solid lit block matches every string
    in the language -- which cost four false passes in the caiso version of this
    function before anybody noticed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in (1, 2):
        m = wiki.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                win = row[:, x:x + gw]
                if not np.array_equal(win & m, m):
                    continue
                if (win & ~m).mean() <= 0.2:
                    return True
    return False


# --------------------------------------------------------------------------
# 1. The axis. Up is added, down is removed, and nothing else uses the axis.
# --------------------------------------------------------------------------

def test_direction():
    print("\nthe vertical axis")
    tmp = tempfile.mkdtemp(prefix="wiki-dir")
    try:
        # One big addition at t=0 and one big deletion a second later, nothing
        # else in the window. Both are far past full scale so both are as tall
        # as their side of the axis goes.
        synthetic(tmp, events=[(0, 50000, 0, 0), (1000, -50000, 0, 0)])
        r, _ = frames(opts(cache_dir=tmp), 1)
        lay, tile = r.layout, r.state["tile"]
        mid = lay.mid - lay.band_y
        speed = ds.options(wiki).speed
        c_add, c_del = 0, int(1000 * speed / 1000.0)

        above = tile[:mid, c_add].max(), tile[mid + 1:, c_add].max()
        below = tile[:mid, c_del].max(), tile[mid + 1:, c_del].max()
        check("an addition draws above the zero line and not below",
              above[0] > 40 and above[1] == 0, "above %d below %d" % above)
        check("a deletion draws below the zero line and not above",
              below[1] > 40 and below[0] == 0, "above %d below %d" % below)
        check("...and both reach their full height",
              int((tile[:mid, c_add].max(axis=1) > 0).sum()) == lay.up_rows
              and int((tile[mid + 1:, c_del].max(axis=1) > 0).sum()) == lay.dn_rows,
              "%d up rows, %d down rows" % (lay.up_rows, lay.dn_rows))

        # And the axis is symmetric: the same number of bytes either way is the
        # same number of pixels, so a deletion is never made to look bigger
        # than the addition that undid it.
        synthetic(tmp, events=[(0, 400, 0, 0), (2000, -400, 0, 0)])
        r, _ = frames(opts(cache_dir=tmp), 1)
        tile, mid = r.state["tile"], r.layout.mid - r.layout.band_y
        cu, cd = 0, int(2000 * speed / 1000.0)
        hu = int((tile[:mid, cu].max(axis=1) > 0).sum())
        hd = int((tile[mid + 1:, cd].max(axis=1) > 0).sum())
        check("+400 and -400 bytes are the same height", abs(hu - hd) <= 1,
              "%d up, %d down" % (hu, hd))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_magnitude():
    print("\nthe height scale")
    tmp = tempfile.mkdtemp(prefix="wiki-mag")
    try:
        sizes = [10, 100, 900, 9000]
        synthetic(tmp, events=[(i * 2000, v, 0, 0) for i, v in enumerate(sizes)])
        r, _ = frames(opts(cache_dir=tmp), 1)
        lay, tile = r.layout, r.state["tile"]
        mid = lay.mid - lay.band_y
        speed = ds.options(wiki).speed
        hs = []
        for i in range(len(sizes)):
            c = int(i * 2000 * speed / 1000.0)
            hs.append(int((tile[:mid, c].max(axis=1) > 0).sum()))
        check("bigger edits are taller, monotonically",
              all(hs[i] <= hs[i + 1] for i in range(len(hs) - 1)),
              " ".join("%db=%dpx" % (s, h) for s, h in zip(sizes, hs)))
        check("a ten-byte typo is still visible", hs[0] >= 1, "%d px" % hs[0])
        check("past full scale clips rather than overflowing",
              hs[-1] == lay.up_rows, "%d px of %d" % (hs[-1], lay.up_rows))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bot_dimming():
    print("\nbots are dimmer than people, and by how much")
    tmp = tempfile.mkdtemp(prefix="wiki-bot")
    try:
        # The same edit twice: one made by a person, one by a bot.
        synthetic(tmp, events=[(0, 5000, 0, 0), (2000, 5000, 0, 1)])
        r, _ = frames(opts(cache_dir=tmp), 1)
        tile, mid = r.state["tile"], r.layout.mid - r.layout.band_y
        speed = ds.options(wiki).speed
        human = int(tile[mid - 1, 0].max())
        bot = int(tile[mid - 1, int(2000 * speed / 1000.0)].max())
        check("a bot edit is dimmer than the same edit by a person",
              0 < bot < human, "human %d, bot %d" % (human, bot))
        check("...by about the declared factor",
              abs(bot / max(1.0, float(human)) - wiki.BOT_DIM) < 0.06,
              "ratio %.2f, BOT_DIM %.2f" % (bot / max(1.0, float(human)),
                                            wiki.BOT_DIM))
        check("...and both are the same height, because size is bytes",
              int((tile[:mid, 0].max(axis=1) > 0).sum())
              == int((tile[:mid, int(2000 * speed / 1000.0)].max(axis=1) > 0).sum()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2. The time axis. A stroke's column is when the edit happened, and this is
#    the check that catches the whole thing quietly becoming a queue.
# --------------------------------------------------------------------------

def test_time_axis():
    print("\nthe time axis is a clock, not a queue")
    tmp = tempfile.mkdtemp(prefix="wiki-time")
    try:
        # Three edits in the first 200 ms and one twenty seconds later. If the
        # layout is even rather than temporal, the burst spreads out.
        burst = [(0, 5000, 0, 0), (60, 5000, 0, 0), (140, 5000, 0, 0),
                 (20000, 5000, 0, 0)]
        synthetic(tmp, events=burst)
        r, _ = frames(opts(cache_dir=tmp), 1)
        lay, tile = r.layout, r.state["tile"]
        mid = lay.mid - lay.band_y
        # Only the tile's first pass: it carries a screen of its own beginning
        # stitched onto the end, and matching the burst there would prove
        # nothing about where the strip put it.
        lit = np.where(tile[:mid, :r.state["strip_w"]].max(axis=(0, 2)) > 0)[0]
        speed = ds.options(wiki).speed
        check("a 140 ms burst lands inside a few columns",
              len(lit) and lit[2] - lit[0] <= max(3, int(0.2 * speed) + 1),
              "columns %s" % lit[:4].tolist())
        check("an edit 20 s later lands 20 s away",
              abs(int(lit[-1]) - int(20 * speed)) <= 1,
              "column %d, expected %d" % (lit[-1], 20 * speed))
        check("the strip is the window at the declared speed",
              abs(r.state["strip_w"] - SECS * speed) <= 1,
              "%d px for %.0f s" % (r.state["strip_w"], SECS))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_motion_and_purity():
    print("\nmotion, and render() being a pure function of t")
    tmp = tempfile.mkdtemp(prefix="wiki-move")
    try:
        synthetic(tmp, events=[(i * 300, 400 + 7 * i, i % 3, i % 2)
                               for i in range(120)])
        a = opts(cache_dir=tmp)
        r = wiki.build(a)
        f0 = r(0.0, 0).copy()
        f1 = r(1.0 / 20.0, 1).copy()
        check("consecutive frames differ", not np.array_equal(f0, f1))
        check("the head does not move",
              np.array_equal(f0[:r.layout.band_y], f1[:r.layout.band_y]))
        # At 20 px/s and 20 fps the strip advances exactly one pixel a frame,
        # which is the whole reason those are the defaults.
        band0, band1 = f0[r.layout.band_y:], f1[r.layout.band_y:]
        check("the band advances exactly one pixel a frame at the defaults",
              np.array_equal(band0[:, 1:], band1[:, :-1]))

        cold = wiki.build(a)(3.0, 60).copy()
        warm = None
        rr = wiki.build(a)
        for i in range(61):
            warm = rr(i / 20.0, i)
        check("a cold render(3.0) equals the same t reached frame by frame",
              np.array_equal(cold, warm))

        # And it loops: the window is a tile, so t and t + secs are the same
        # picture. A seam here would be a visible jolt once a pass.
        loop = wiki.build(a)
        period = r.state["strip_w"] / a.speed
        check("the loop closes with no seam",
              np.array_equal(loop(1.0, 20), loop(1.0 + period, 20)),
              "period %.1f s" % period)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 3. Titles: the ones that can be drawn are drawn, and nothing else is.
# --------------------------------------------------------------------------

def test_titles():
    print("\nthe title crawl")
    tmp = tempfile.mkdtemp(prefix="wiki-title")
    try:
        titles = [[500, 0, 120, "PACIFIC TYPHOON SEASON"],
                  [9000, 2, -40, "BRASSICA ELONGATA"]]
        synthetic(tmp, titles=titles)
        r, _ = frames(opts(cache_dir=tmp), 1)
        check("both titles are packed into the strip", r.state["drawn"] == 2,
              "%d drawn" % r.state["drawn"])
        band = r.state["tile"][:, :r.state["strip_w"]]
        check("a title is legible in the strip",
              contains_text(band, "PACIFIC TYPHOON SEASON"))
        check("...and so is the second one",
              contains_text(band, "BRASSICA ELONGATA"))

        # An article name with the punctuation that titles are actually full
        # of. Dropping the apostrophe and the parentheses would be a different
        # title, so the font has to carry them.
        synthetic(tmp, titles=[[500, 0, 10, "ST. MICHAEL'S ABBEY (ORANGE, CA)"]])
        r, _ = frames(opts(cache_dir=tmp), 1)
        check("apostrophes, commas and brackets survive",
              contains_text(r.state["tile"][:, :r.state["strip_w"]],
                            "ST. MICHAEL'S ABBEY (ORANGE, CA)"))

        # Titles must never overlap each other -- including across the seam,
        # where a title running off the right-hand end comes back at column
        # zero and used to land on top of the lane's first title.
        many = [[int(i * SECS * 1000 / 40.0), i % 3, 50,
                 "TITLE NUMBER %d IS QUITE LONG INDEED" % i] for i in range(40)]
        synthetic(tmp, titles=many)
        r, _ = frames(opts(cache_dir=tmp), 1)
        lay, tile = r.layout, r.state["tile"]
        overlap = 0
        for lane in range(lay.lanes):
            y = lay.lane_y(lane) - lay.band_y
            row = tile[y:y + 5, :r.state["strip_w"]].max(axis=(0, 2)) > 0
            # A lane is a run of lit columns per title; two titles on top of
            # each other make a run longer than any single title can be.
            run = best = 0
            for v in row:
                run = run + 1 if v else 0
                best = max(best, run)
            longest = 4 + wiki.text_width("TITLE NUMBER 39 IS QUITE LONG INDEED")
            if best > longest + 4:
                overlap += 1
        check("no two titles collide in a lane, seam included", overlap == 0,
              "%d lanes with a run too long" % overlap)

        # A window in which nothing was renderable. The crawl is empty and the
        # panel still draws -- the strokes are the demo, the titles are the
        # delight, and losing the delight is not losing the panel.
        synthetic(tmp, titles=[])
        r, f = frames(opts(cache_dir=tmp), 4)
        check("a window with no Latin titles still draws its strokes",
              r.state["drawn"] == 0 and f.max() > 0)
        check("...and drops the LATIN TITLES note rather than lying",
              not contains_text(f, "LATIN TITLES"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_numbers():
    print("\nthe numbers on the panel")
    tmp = tempfile.mkdtemp(prefix="wiki-num")
    try:
        events = [(i * 200, 1000, 0, 1 if i % 4 else 0) for i in range(200)]
        synthetic(tmp, events=events,
                  titles=[[500, 0, 12, "CARL COX"]])
        r, f = frames(opts(cache_dir=tmp), 4)
        rec = r.state["rec"]
        check("the rate is on the panel",
              contains_text(f, "%d EDITS/S" % int(round(rec["per_s"]))),
              "%.1f/s" % rec["per_s"])
        check("the bot share is on the panel",
              contains_text(f, "BOT %d%%" % int(round(rec["bot_pct"]))),
              "%.0f%% bots" % rec["bot_pct"])
        check("the bytes added are on the panel",
              contains_text(f, "+%s" % wiki.kilo(rec["add_bytes"])),
              "+%s" % wiki.kilo(rec["add_bytes"]))
        check("it says which project the colours belong to",
              contains_text(f, "EN") and contains_text(f, "COMMONS"))
        check("and it names itself", contains_text(f, "WIKIMEDIA"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_degraded():
    print("\nrecords that are wrong rather than merely old")
    tmp = tempfile.mkdtemp(prefix="wiki-bad")
    try:
        for label, extra in (
                ("truncated columns", {"d": [1, 2]}),
                ("no events at all", {"ms": [], "d": [], "pi": [], "f": []}),
                ("a zero-length window", {"secs": 0.0}),
                ("no project names", {"pnames": []})):
            d = os.path.join(tmp, label.replace(" ", "-"))
            synthetic(d, **extra)
            r, f = frames(opts(cache_dir=d), 4)
            check("%s gets the no-data card" % label,
                  r.state["rec"] is None and contains_text(f, "NO WIKI DATA"),
                  str(r.state["problem"])[:44])

        # A project index pointing past the end of the name table: draw the
        # rest rather than losing the panel to one bad row.
        d = os.path.join(tmp, "bad-title-index")
        synthetic(d, titles=[[100, 99, 5, "SHOULD BE DROPPED"],
                             [4000, 0, 5, "SHOULD BE DRAWN"]])
        r, _ = frames(opts(cache_dir=d), 1)
        check("a title naming a project that is not there is dropped",
              r.state["drawn"] == 1, "%d drawn" % r.state["drawn"])

        # Old, but still a true picture of the encyclopedia.
        d = os.path.join(tmp, "stale")
        synthetic(d, fetched_ago=ftdata.ttl_for("wiki-stream") + 600.0)
        r, f = frames(opts(cache_dir=d), 4)
        check("a record past its TTL still draws",
              r.state["rec"] is not None and not contains_text(f, "NO WIKI DATA"))
        check("...and says STALE on the panel",
              contains_text(f, "STALE") and r.state["stale"],
              ftdata.describe_age(r.state["rec"]["age"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. Privacy. Asserted against the real cache, because the rule lives in
#    ftdata.py's fetcher and a synthetic record proves nothing about it.
# --------------------------------------------------------------------------

BANNED_KEYS = ("user", "user_hash", "users", "comment", "parsedcomment",
               "revision", "notify_url", "ip", "editor", "id", "meta")


def test_privacy(cache_dir):
    print("\nprivacy: no editor identity may be in the record")
    got = ftdata.load("wiki-stream", cache_dir)
    if got is None:
        check("a live record is present to check", False,
              "run: python3 ftdata.py --once --only wiki-stream")
        return
    payload, _age = got

    def walk(node, path=""):
        out = []
        if isinstance(node, dict):
            for k, v in node.items():
                out.append((path + "/" + str(k), k, v))
                out.extend(walk(v, path + "/" + str(k)))
        elif isinstance(node, list):
            for v in node[:200]:
                out.extend(walk(v, path + "[]"))
        return out

    nodes = walk(payload)
    bad = [p for p, k, _ in nodes if str(k).lower() in BANNED_KEYS]
    check("no field named after an editor or a revision", not bad,
          ",".join(bad[:4]))

    # Nothing that looks like an IPv4 or IPv6 address, anywhere in the record,
    # including inside the titles -- an anonymous editor's "username" is their
    # address and a talk page about one carries it in its title.
    blob = json.dumps(payload)
    import re
    v4 = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", blob)
    v6 = re.findall(r"\b(?:[0-9A-Fa-f]{1,4}:){4,}[0-9A-Fa-f]{0,4}\b", blob)
    check("no IP address anywhere in the record", not v4 and not v6,
          ",".join((v4 + v6)[:3]))

    # And no title from a namespace where a title is a person's name.
    ns = [t[3] for t in (payload.get("titles") or [])
          if isinstance(t, list) and len(t) > 3
          and str(t[3]).split(":")[0] in
          ("USER", "USER TALK", "TALK", "SPECIAL", "USUARIO", "UTILISATEUR")]
    check("no user-namespace titles", not ns, ",".join(ns[:2]))
    check("the record says what it filtered titles down to",
          bool(payload.get("title_note")), str(payload.get("title_note")))


def test_live(cache_dir):
    print("\nthe live cached record")
    got = ftdata.load("wiki-stream", cache_dir)
    if got is None:
        check("a live record is present", False,
              "run: python3 ftdata.py --once --only wiki-stream")
        return
    payload, age = got
    path = ftdata.record_path("wiki-stream", cache_dir)
    size = os.path.getsize(path) if path else 0
    check("the record is small enough for the Pi's cache", size < 64000,
          "%d bytes, %d events, %d titles" % (size, len(payload.get("ms") or []),
                                              len(payload.get("titles") or [])))
    check("the window is roughly the length it claims",
          abs(float(payload["secs"]) - ftdata.WIKI_WINDOW) < 15.0,
          "%.1f s" % payload["secs"])
    check("bots are a large share of it, as advertised",
          10.0 < float(payload["bot_pct"]) < 95.0,
          "%.0f%% of %d edits" % (payload["bot_pct"], payload["n"]))
    check("many projects contributed", int(payload["n_projects"]) >= 5,
          "%d wikis" % payload["n_projects"])
    r, f = frames(opts(cache_dir=cache_dir), 8)
    check("the live record draws", r.state["rec"] is not None and f.max() > 0,
          "%d titles in the crawl, age %s"
          % (r.state["drawn"], ftdata.describe_age(age)))


# --------------------------------------------------------------------------
# 5. Fresh, stale and absent, one process each.
# --------------------------------------------------------------------------

def _one_state(state, cache_dir):
    args = opts()                       # note: no cache_dir, so CACHE_DIR wins
    r = wiki.build(args)
    out = None
    for i in range(8):
        out = r(i / 20.0, i)
    card = contains_text(out, "NO WIKI DATA")
    stale = contains_text(out, "STALE")
    drew = r.state["rec"] is not None
    verdict = {
        "fresh": (drew and not card and not stale, "drew the stream"),
        "stale": (drew and not card and stale, "drew it with STALE on it"),
        "absent": (not drew and card, "drew the no-data card"),
    }[state]
    print("RESULT %s %s cache=%s drew=%s card=%s stale=%s"
          % (state, "ok" if verdict[0] else "FAIL",
             os.path.basename(cache_dir), drew, card, stale))
    return 0 if verdict[0] else 1


def test_states_in_separate_processes():
    print("\nfresh, stale and absent -- one process each, FT_DATA_CACHE set")
    tmp = tempfile.mkdtemp(prefix="wiki-proc")
    try:
        fresh = synthetic(os.path.join(tmp, "fresh"), fetched_ago=120.0)
        stale = synthetic(os.path.join(tmp, "stale"),
                          fetched_ago=ftdata.ttl_for("wiki-stream") + 1800.0)
        absent = os.path.join(tmp, "absent")
        os.makedirs(absent)
        for state, d in (("fresh", fresh), ("stale", stale), ("absent", absent)):
            env = dict(os.environ, FT_DATA_CACHE=d, FT_DATA_BLOBS=d)
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--state", state, "--cache-dir", d],
                env=env, capture_output=True, text=True, timeout=120)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT")]
            check("%s cache, in its own process" % state,
                  proc.returncode == 0 and bool(line),
                  line[0][7:] if line else
                  (proc.stderr.strip().splitlines() or ["no output"])[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="wiki-size")
    try:
        synthetic(tmp, events=[(i * 250, 300 - 9 * i, i % 3, i % 2)
                               for i in range(150)],
                  titles=[[i * 2200, i % 3, 40, "SOME ARTICLE %d" % i]
                          for i in range(15)])
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, f = frames(opts(cache_dir=tmp, width=w, height=h), 40)
                lay = r.layout
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                detail = "stream %d rows, %d lanes" % (lay.stream_h, lay.lanes)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("wiki-stream", tempfile.mkdtemp(prefix="wiki-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "wiki.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in src]
    check("wiki.py does not import one either", not imported, ",".join(imported))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--state", default="",
                    choices=("", "fresh", "stale", "absent"),
                    help="internal: run one data state and print RESULT")
    a = ap.parse_args()
    if a.state:
        return _one_state(a.state, a.cache_dir)

    print("cache: %s" % a.cache_dir)
    test_no_network()
    test_direction()
    test_magnitude()
    test_bot_dimming()
    test_time_axis()
    test_motion_and_purity()
    test_titles()
    test_numbers()
    test_degraded()
    test_states_in_separate_processes()
    test_sizes()
    test_privacy(a.cache_dir)
    test_live(a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
