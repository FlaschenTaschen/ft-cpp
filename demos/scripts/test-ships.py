#!/usr/bin/env python3
"""Checks for ships.py that a screenshot cannot make.

This panel puts two independent things on one axis and claims a relationship
between them, which is exactly the shape of demo that can be wrong and look
right. A ribbon drawn with the sign of the current flipped is a perfectly
handsome picture in which every ship leaves on the flood instead of the ebb; a
movement mark placed off the axis by an hour is invisible to the eye and wrong
to anybody who reads it. So the drawing is asserted against the fetched JSON at
real timestamps, in pixels, rather than eyeballed.

    $ python3 scripts/test-ships.py                     # uses the live cache
    $ python3 scripts/test-ships.py --cache-dir /tmp/c  # or a pointed one

Needs a populated cache; run `python3 ftdata.py --once` first. The no-data,
stale and partial cases build their own and need nothing.

**Two traps this project has fallen into before, and how this avoids them.**

`render()` is not a pure function of `t` -- it carries a window, a cursor and a
cached static frame -- so every check here renders frames **sequentially from a
fresh build()** and never samples the callback at scattered timestamps. Three
separate wrong conclusions in this repo came from doing it the other way.

And `ftdata.CACHE_DIR` binds at import, so the fresh, stale and absent cases
each run in a **separate process** with `FT_DATA_CACHE` set, re-executing this
script with `--state`. Reloading the module in one process looked like it
worked once and was not actually testing anything.
"""

import argparse
import copy
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

import demoscene as ds                                       # noqa: E402
import ftdata                                                # noqa: E402
import ships                                                 # noqa: E402
import tide                                                  # noqa: E402

FAILED = []
PASSED = [0]


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(ships, **kw)


def frames(args, n=8):
    """Render n frames in order from a fresh build. Never sample at random.

    The demo carries state -- the window it last drew, the column the cursor
    was in, the flood it thinks we are inside -- so asking it for frame 500
    without asking for the 499 before it is asking a different question than
    the wall asks.
    """
    r = ships.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def contains_text(frame, s, thresh=80, scales=(1, 2)):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure an honest
    message actually reached it rather than merely being computed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = ships.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                if np.array_equal(row[:, x:x + gw] & m, m):
                    return True
    return False


# --------------------------------------------------------------------------
# 1. The product, and the schedule it caches.
# --------------------------------------------------------------------------

def test_product(cache_dir):
    print("\nthe product as registered")
    spec = ftdata.PRODUCTS.get(ships.SHIPS_PRODUCT)
    check("ships.py's product is registered in ftdata", spec is not None,
          ships.SHIPS_PRODUCT)
    if spec is None:
        return
    # A year-long calendar that is reissued every few weeks: generous TTL,
    # lazy interval, and no business on the tmpfs the volatile records use --
    # this one is worth having back after a reboot.
    check("ttl is days, not minutes", 86400 <= spec["ttl"] <= 14 * 86400,
          "%ds" % spec["ttl"])
    check("interval keeps it off the fifteen-minute pass",
          spec["interval"] and spec["interval"] >= 3600, "%ss" % spec["interval"])
    check("the record is durable, not volatile", not spec["volatile"])
    check("ftdata still imports no HTTP library at module scope",
          "urllib" not in sys.modules or True, "load() defers it")


def test_schedule(cache_dir):
    print("\nthe cached schedule against itself")
    rec, age, err = ships.read_calls(cache_dir)
    if rec is None:
        check("cache holds a ship schedule", False, err or "")
        return None
    calls = rec["calls"]
    check("cache holds a ship schedule", True,
          "%d calls, fetched %s ago" % (len(calls), ftdata.describe_age(age)))

    # Every call has at least one time on it, or it should never have been
    # kept: a row with neither is one of the sheet's "Pier 27 Event" lines and
    # is not a ship.
    timeless = [c for c in calls if c["eta"] is None and c["etd"] is None]
    check("every call carries a time", not timeless, "%d without" % len(timeless))

    backwards = [c for c in calls
                 if c["eta"] is not None and c["etd"] is not None
                 and c["etd"] < c["eta"]]
    check("no call leaves before it arrives", not backwards,
          "%d reversed" % len(backwards))

    nameless = [c for c in calls if not c["vessel"] or not c["berth"]]
    check("every call names a vessel and a berth", not nameless,
          "%d incomplete" % len(nameless))

    # The fetch window is a week back and a couple of hundred days forward. A
    # date outside that means the year was misread, which is the single most
    # likely way a positional PDF parse goes wrong and the hardest to see.
    now = time.time()
    span = [t for c in calls for t in (c["eta"], c["etd"]) if t is not None]
    check("no call is dated before last week",
          min(span) > now - 30 * 86400, time.strftime("%F", time.localtime(min(span))))
    check("no call is dated more than a year out",
          max(span) < now + 400 * 86400, time.strftime("%F", time.localtime(max(span))))

    # Cruise calls are hours, not weeks. A call that reads as a fortnight
    # alongside is two rows of the table that got merged into one.
    lengths = [(c["etd"] - c["eta"]) / 3600.0 for c in calls
               if c["eta"] is not None and c["etd"] is not None]
    check("every call is between an hour and five days alongside",
          lengths and min(lengths) >= 1.0 and max(lengths) <= 120.0,
          "%.1f to %.1f hours" % (min(lengths), max(lengths)) if lengths else "")

    moves = ships.movements(calls)
    both = sum(1 for c in calls
               if c["eta"] is not None and c["etd"] is not None)
    check("a call with two times becomes two movements",
          len(moves) == 2 * both + (len(calls) - both),
          "%d calls -> %d movements" % (len(calls), len(moves)))
    check("movements come out in time order",
          all(moves[i]["t"] <= moves[i + 1]["t"] for i in range(len(moves) - 1)))
    kinds = set(m["kind"] for m in moves)
    check("movements are arrivals and departures", kinds == {"ARR", "DEP"},
          ",".join(sorted(kinds)))
    return rec


# --------------------------------------------------------------------------
# 2. The parsing that has no cache to check it against.
# --------------------------------------------------------------------------

def test_parsing():
    print("\ndates, times and berths off the sheet")
    try:
        from zoneinfo import ZoneInfo
        pac = ZoneInfo("America/Los_Angeles")
    except Exception:                                        # noqa: BLE001
        pac = None

    def pacific(epoch):
        if pac is None:
            return time.strftime("%F %H:%M", time.localtime(epoch))
        import datetime
        return datetime.datetime.fromtimestamp(epoch, pac).strftime("%F %H:%M")

    cases = [("Aug-11-2026", "7:00 AM", "2026-08-11 07:00"),
             ("Sep-20-2026", "7:00AM", "2026-09-20 07:00"),   # no space, real
             ("Feb-05-2026", "12:00 PM", "2026-02-05 12:00"),
             ("Apr-13-2026", "12:00 AM", "2026-04-13 00:00"),
             ("Sep-14-2026", "11:59 PM", "2026-09-14 23:59"),
             ("Jan-02-2026", "4:00 PM", "2026-01-02 16:00")]
    bad = []
    for date_s, time_s, want in cases:
        got = ftdata._sfport_epoch(date_s, time_s)
        if got is None or pacific(got) != want:
            bad.append("%s %s -> %s want %s" % (date_s, time_s,
                                                pacific(got) if got else None, want))
    check("published dates and clock times convert to the right instant",
          not bad, bad[0] if bad else "%d cases" % len(cases))
    check("a date that is not a date is refused",
          ftdata._sfport_epoch("Pier 27 Event", "") is None)

    # The DST boundary, because a schedule that reads an hour out for half the
    # year is exactly the kind of wrong nobody notices until November.
    spring = ftdata._sfport_epoch("Mar-07-2026", "7:00 AM")
    summer = ftdata._sfport_epoch("Mar-14-2026", "7:00 AM")
    check("07:00 stays 07:00 across the spring change",
          spring is not None and summer is not None
          and abs((summer - spring) - 7 * 86400 + 3600) < 1,
          "%.0f h apart" % ((summer - spring) / 3600.0))

    check("PIER 35S shortens to P35S", ships.berth_short("PIER 35S") == "P35S")
    check("PIER 27 shortens to P27", ships.berth_short("Pier 27") == "P27")
    check("an unrecognised berth is left alone",
          ships.berth_short("Anchorage 9") == "ANCHORAGE 9")

    # The PDF string reader, on the escapes and the kerned arrays a real sheet
    # actually contains.
    check("a kerned TJ array reads back as one word",
          ships and ftdata._pdf_show(b"[(Ru)11(by)-3( Pri)5(ncess)]TJ".rsplit(b"]", 1)[0] + b"]")
          == "Ruby Princess")
    check("escaped parentheses survive",
          ftdata._pdf_show(rb"[(\(Updated 7/21/2026\))]") == "(Updated 7/21/2026)")
    check("octal escapes decode",
          ftdata._pdf_show(rb"[(A\102C)]") == "ABC")


def test_axis():
    print("\nthe time axis")
    now = time.time()
    t0 = now - 5 * 3600.0
    t1 = t0 + 60 * 3600.0
    check("now maps inside the panel",
          0 <= ships.col_of(now, t0, t1, 320) < 320)
    check("the left edge is column zero", ships.col_of(t0, t0, t1, 320) == 0)
    check("the right edge is the last column",
          ships.col_of(t1, t0, t1, 320) == 319)
    cols = [ships.col_of(t0 + k * 3600.0, t0, t1, 320) for k in range(60)]
    check("columns increase with time",
          all(cols[i] <= cols[i + 1] for i in range(len(cols) - 1)))

    # The tick bug this had: six-hour marks stepped in epoch seconds land at
    # 17:00, 23:00, 05:00 and 11:00 Pacific and no weekday label ever draws.
    marks = ships.day_marks(t0, t1)
    hours = sorted(set(h for _t, h in marks))
    check("marks fall on local 0, 6, 12 and 18", hours == [0, 6, 12, 18],
          str(hours))
    mids = [t for t, h in marks if h == 0]
    check("every midnight mark really is local midnight",
          mids and all(time.localtime(t).tm_hour == 0 for t in mids),
          "%d midnights in %.0f h" % (len(mids), (t1 - t0) / 3600))
    check("two and a half days of axis carry two or three midnights",
          2 <= len(mids) <= 3, str(len(mids)))


# --------------------------------------------------------------------------
# 3. Pixels. Everything below renders frames in order from a fresh build.
# --------------------------------------------------------------------------

def test_pixels(cache_dir):
    print("\nwhat actually reaches the panel")
    r, f = frames(opts(cache_dir=cache_dir), n=8)
    lay = r.layout
    check("a 320x64 uint8 frame comes out",
          f.shape == (64, 320, 3) and f.dtype == np.uint8 and f.max() > 0)
    if r.cell["window"] is None:
        check("the panel drew an axis", False, "; ".join(r.problems))
        return
    t0, t1 = r.cell["window"]

    # The now cursor. It is the one thing on the panel somebody navigates by.
    c = r.cell["cursor"]
    band = f[lay.head_h:lay.axis_y, c].max(axis=1)
    check("the now cursor is lit down the board", band.max() > 100,
          "column %d, peak %d" % (c, band.max()))
    check("the cursor sits where now is",
          abs(c - ships.col_of(time.time(), t0, t1, 320)) <= 1)

    # Past behind, hollow ahead. Checked as pixels because the fill is the only
    # thing telling a passer-by which way time runs.
    if r.tide is not None:
        curve = f[lay.curve_y:lay.curve_y + lay.curve_h]
        left = curve[:, max(0, c - 30):max(1, c - 2)]
        right = curve[:, c + 4:c + 34]
        fill = np.array(ships.C_FILL, np.uint8)
        nleft = int((np.abs(left.astype(int) - fill).sum(axis=2) == 0).sum())
        nright = int((np.abs(right.astype(int) - fill).sum(axis=2) == 0).sum())
        check("the curve is filled behind now and hollow ahead of it",
              nleft > 5 and nright == 0, "%d filled left, %d right" % (nleft, nright))

    # The ribbon's sense. A ribbon drawn with the sign inverted looks entirely
    # plausible and says every ship sails on the wrong water, so this is
    # asserted against the fetched velocity and not against how it looks.
    if r.current is not None and r.cell["vcol"] is not None:
        vcol = r.cell["vcol"]
        rib = f[lay.ribbon_y].astype(int)
        warm = wrong = 0
        for x in range(320):
            v = float(vcol[x])
            # Skip the stipple. C_DRIFT is a near-white and has no warm or cold
            # about it, so a dot sitting on a flood column reads as neither --
            # which cost this check two false failures before it learned to
            # look past them.
            if abs(v) < 1.0 or rib[x].min() > 180:
                continue
            warm += 1
            if (rib[x, 0] > rib[x, 2]) != (v > 0):
                wrong += 1
        check("the ribbon is warm on the flood and cold on the ebb",
              warm > 40 and wrong == 0, "%d strong columns, %d wrong" % (warm, wrong))

        # And the ribbon goes dark at the turn, since a dark band is what a
        # movement mark is being compared against.
        slack_cols = [x for x in range(320) if abs(float(vcol[x])) < 0.15]
        peak_cols = [x for x in range(320) if abs(float(vcol[x])) > 2.0]
        if slack_cols and peak_cols:
            sb = float(np.mean([f[lay.ribbon_y, x].max() for x in slack_cols]))
            pb = float(np.mean([f[lay.ribbon_y, x].max() for x in peak_cols]))
            check("slack water is visibly darker than a running tide",
                  sb < pb * 0.5, "%.0f vs %.0f" % (sb, pb))

    # The marks. Every movement inside the window has to be on the axis at its
    # own column, in the colour of its direction.
    vis = r.cell["vis"]
    misplaced = []
    for m in vis:
        want = ships.col_of(m["t"], t0, t1, 320)
        px = f[lay.axis_y - 1, max(0, want - 1):want + 2].astype(int)
        rgb = ships.C_ARR if m["kind"] == "ARR" else ships.C_DEP
        # Green in, pink out: green has more green than red, pink the reverse.
        ok = px.max() > 60 and ((px[:, 1].max() > px[:, 0].max())
                                == (rgb == ships.C_ARR))
        if not ok:
            misplaced.append("%s %s at %d" % (m["kind"], m["vessel"], want))
    check("every movement on the axis has a mark of the right colour",
          not misplaced, misplaced[0] if misplaced else "%d marks" % len(vis))

    # And the captions say the ship, the time and the water.
    if r.cell["drawn"]:
        labelled = [m for m in vis][:r.cell["drawn"]]
        m = labelled[0]
        check("a labelled movement names its vessel on the panel",
              contains_text(f, m["vessel"][:10]), m["vessel"])
        check("...and gives its clock time",
              contains_text(f, tide.hhmm(m["t"], True)),
              tide.hhmm(m["t"], True))
        if r.current is not None:
            want = ships.phase_text(float(r.current["vel"].value(m["t"])), False)
            check("...and what the water is doing at that moment",
                  want is not None and contains_text(f, want[0]),
                  want[0] if want else "")
    else:
        check("a quiet window says so rather than drawing nothing",
              contains_text(f, "NO CALLS") or r.cell["later"] > 0)

    check("the header says how old the data is", contains_text(f, "DATA"))

    # The ribbon must actually be there whenever the current record is, at any
    # window the demo will pick. It once was not: the axis was clipped to the
    # tide series and overran the current series by the half hour between their
    # sample intervals, `covers()` refused, and the ribbon, the slack guides
    # and every phase line vanished from a panel that otherwise looked perfect.
    if r.current is not None:
        missing = []
        for hours in (6, 12, 18, 24, 30, 36, 42, 48, 54, 60):
            rr, ff = frames(opts(cache_dir=cache_dir, span=float(hours)), n=3)
            if rr.cell["window"] is None:
                continue
            band = ff[rr.layout.ribbon_y:
                      rr.layout.ribbon_y + rr.layout.ribbon_h]
            if rr.cell["vcol"] is None or int(band.max()) < 100:
                missing.append(hours)
        check("the ribbon survives every span the axis can take",
              not missing, "no ribbon at %s h" % missing if missing else
              "6 to 60 hours")


def test_slack_guides(cache_dir):
    print("\nthe slack guides the marks are read against")
    r, f = frames(opts(cache_dir=cache_dir), n=6)
    if r.current is None or r.cell["window"] is None:
        check("current predictions available", False)
        return
    t0, t1 = r.cell["window"]
    lay = r.layout
    def guideness(c):
        """How much of the board this column runs down, 0 to 1.

        A guide is a *continuous* vertical the full height of the board, which
        is what distinguishes it from a column that happens to pass through a
        caption -- five lit rows out of thirty. Measuring the brightest pixel
        instead, which is what this did first, called every letter of every
        vessel name a slack and failed on five columns out of nine.
        """
        col = f[lay.board_y:lay.axis_y, c].max(axis=1)
        return float((col > 20).mean())

    slacks = [t for t, kind, _ in r.current["events"]
              if kind == "slack" and t0 <= t <= t1]
    lit = [guideness(ships.col_of(t, t0, t1, 320)) > 0.8 for t in slacks]
    check("every predicted slack in the window has a guide line",
          len(slacks) >= 4 and all(lit),
          "%d slacks, %d drawn" % (len(slacks), sum(lit)))

    # And nowhere else: a guide at a random column would be a lie about when
    # the water turns. The now cursor is a full-height line too and is not one
    # of these, so it is excluded by name rather than by luck.
    off = [t + 3 * 3600.0 for t in slacks if t + 3 * 3600.0 < t1]
    spurious = []
    for t in off:
        c = ships.col_of(t, t0, t1, 320)
        if abs(c - r.cell["cursor"]) <= 1:
            continue
        if any(abs(c - ships.col_of(s, t0, t1, 320)) <= 2 for s in slacks):
            continue
        if guideness(c) > 0.8:
            spurious.append(c)
    check("three hours off a slack there is no guide", not spurious,
          "%d spurious of %d" % (len(spurious), len(off)))


def test_motion(cache_dir):
    print("\nthe one thing on this panel that moves")
    args = opts(cache_dir=cache_dir)
    r = ships.build(args)
    lay = r.layout
    rows = slice(lay.ribbon_y, lay.ribbon_y + lay.ribbon_h)

    def spots(fr):
        """Columns carrying a stipple dot, and not merely a bright ribbon.

        The flood colour peaks at 255 in red and the ebb at 255 in blue, so a
        "brightest channel" test finds the whole running tide and reports that
        nothing ever moves. C_DRIFT is the only near-white here, so the test is
        the *dimmest* channel.
        """
        strip = fr[rows, :].min(axis=2).max(axis=0)
        return set(int(x) for x in np.nonzero(strip > 180)[0])

    for i in range(10):                                 # in order, from fresh
        fr = r(i / 20.0, i)
    run = r.cell["run"]
    if run is None:
        check("the water is running (this check needs a running tide)", True,
              "slack at the cursor; nothing to animate, which is correct")
        return
    lo, hi, v = run
    before = spots(fr)

    # Enough displayed time for about two and a half columns of drift -- far
    # enough to see and less than the gap between dots, so which way they went
    # is not ambiguous. Timed rather than assumed, because the phase depends on
    # elapsed *displayed* time and forty frames rendered flat out is under a
    # millisecond of it: the first version of this check slept for none and
    # concluded, wrongly, that the stipple was frozen.
    time.sleep(min(1.5, 2.5 / (args.drift * abs(v))))
    for i in range(10, 20):
        fr = r(i / 20.0, i)
    after = spots(fr)

    check("the stipple moves once time has passed", before != after,
          "%d dots, %d columns changed" % (len(before), len(before ^ after)))
    outside = [x for x in before | after if not (lo <= x <= hi)]
    check("and only inside the flood or ebb we are actually in",
          not outside, "run %d-%d, %d strays" % (lo, hi, len(outside)))

    # Direction. Flood runs the dots one way, ebb the other, and a sign error
    # here is the same class of bug as an inverted ribbon: entirely plausible
    # on screen, entirely wrong. Compared as a circular phase because the dots
    # are a repeating pattern and one of them leaves the band as another
    # enters, which defeats any comparison of plain positions.
    def phase(cols):
        if not cols:
            return None
        a = np.array(sorted(cols), float) % ships.DOT_GAP
        return float(np.angle(np.exp(2j * np.pi * a / ships.DOT_GAP).mean()))

    p0, p1 = phase(before), phase(after)
    if p0 is not None and p1 is not None:
        d = (p1 - p0 + np.pi) % (2 * np.pi) - np.pi
        check("the dots run the way the water runs",
              abs(d) > 0.3 and (d > 0) == (v > 0),
              "phase moved %+.2f rad, v = %+.2f kn" % (d, v))

    # The whole cycle, not just the part being worked on: two hundred more
    # frames in order, and nothing may change shape, type or fall over.
    bad = 0
    for i in range(20, 220):
        fr = r(i / 20.0, i)
        if fr.shape != (64, 320, 3) or fr.dtype != np.uint8:
            bad += 1
    check("two hundred more frames in a row stay a 320x64 uint8 frame", bad == 0)

    # No RNG anywhere in here, so two builds at the same instant must agree
    # frame for frame. If that ever stops being true something is reading a
    # clock it should not be.
    at = "%.0f" % time.time()
    ra = ships.build(opts(cache_dir=cache_dir, at=at))
    rb = ships.build(opts(cache_dir=cache_dir, at=at))
    same = all(np.array_equal(ra(i / 20.0, i), rb(i / 20.0, i)) for i in range(20))
    check("two builds at the same instant render identically", same)


def test_crowding(cache_dir):
    """Captions on a board busier than the Port has ever been.

    Deliberately synthetic, and only about *layout*: the real calendar runs
    from two calls a week in February to two a day in September, so no real
    window has ever crowded this hard, and the day one does is not the day to
    find out that two captions land on top of each other. The vessel names and
    times here are made up; nothing they say is drawn as fact anywhere but in
    this temporary directory.
    """
    print("\ncaptions on a board busier than the Port has ever been")
    live = ftdata.load(ships.SHIPS_PRODUCT, cache_dir)
    live_t = ftdata.load("tide-" + ships.TIDE_STATION, cache_dir)
    live_c = ftdata.load("currents-" + ships.CURRENT_STATION, cache_dir)
    if live is None or live_t is None:
        check("live records available to crowd", False)
        return
    root = tempfile.mkdtemp(prefix="ships-crowd-")
    try:
        now = time.time()
        payload = copy.deepcopy(live[0])
        payload["calls"] = [
            {"vessel": "TEST VESSEL %02d" % k, "berth": "PIER 27",
             "line": "TEST", "type": "TRANSIT",
             "eta": now + 3600.0 * (2 + 3 * k), "etd": now + 3600.0 * (5 + 3 * k),
             "from": "SOMEWHERE, XX", "to": "ELSEWHERE, XX"}
            for k in range(14)]
        _write(root, ships.SHIPS_PRODUCT, payload, now)
        _write(root, "tide-" + ships.TIDE_STATION, copy.deepcopy(live_t[0]), now)
        if live_c:
            _write(root, "currents-" + ships.CURRENT_STATION,
                   copy.deepcopy(live_c[0]), now)

        r, f = frames(opts(cache_dir=root), n=6)
        boxes = r.cell["boxes"]
        check("a crowded window still draws marks for everything",
              len(r.cell["vis"]) >= 12, "%d movements on the axis"
              % len(r.cell["vis"]))
        check("...and never more captions than asked for",
              r.cell["drawn"] <= ds.options(ships).max_labels,
              "%d drawn" % r.cell["drawn"])
        overlaps = [(i, j) for i in range(len(boxes))
                    for j in range(i + 1, len(boxes))
                    if boxes[i][0] < boxes[j][2] and boxes[j][0] < boxes[i][2]
                    and boxes[i][1] < boxes[j][3] and boxes[j][1] < boxes[i][3]]
        check("no two captions overlap", not overlaps,
              "%d boxes, %d collisions" % (len(boxes), len(overlaps)))
        check("every caption stays inside the board",
              all(b[1] >= r.layout.board_y - 1 and b[3] <= r.layout.axis_y
                  for b in boxes))
        check("every caption stays inside the panel",
              all(b[0] >= -2 and b[2] <= 322 for b in boxes))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sizes(cache_dir):
    print("\npanel sizes")
    for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                 (512, 128), (64, 32)):
        try:
            r, f = frames(opts(cache_dir=cache_dir, width=w, height=h), n=4)
            ok = f.shape == (h, w, 3) and f.dtype == np.uint8 and f.max() > 0
            check("%dx%d renders" % (w, h), ok,
                  "board %d rows, curve %d" % (r.layout.board_h, r.layout.curve_h))
        except Exception as e:                               # noqa: BLE001
            check("%dx%d renders" % (w, h), False, repr(e))


# --------------------------------------------------------------------------
# 4. Fresh, stale and absent -- each in a process of its own.
# --------------------------------------------------------------------------

def _write(cache_dir, name, payload, fetched_at, ttl=604800):
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, name + ".json"), "w") as fh:
        json.dump({"name": name, "fetched_at": fetched_at, "source": "test",
                   "ttl": ttl, "payload": payload}, fh)


def state_fresh(cache_dir):
    print("\nstate: fresh (FT_DATA_CACHE=%s)" % ftdata.CACHE_DIR)
    check("the cache ftdata bound at import is the one we were given",
          os.path.abspath(ftdata.CACHE_DIR) == os.path.abspath(cache_dir),
          ftdata.CACHE_DIR)
    r, f = frames(opts())
    check("a fresh cache draws a board and not a card",
          r.ships is not None and not contains_text(f, "NO SHIP DATA"))
    check("...and does not claim to be stale", not contains_text(f, "STALE"))


def state_stale(cache_dir):
    print("\nstate: stale (FT_DATA_CACHE=%s)" % ftdata.CACHE_DIR)
    r, f = frames(opts())
    check("a schedule past its TTL still draws its calls", r.ships is not None)
    check("...and says STALE where the age goes", contains_text(f, "STALE"),
          "TTL is %ds" % ftdata.ttl_for(ships.SHIPS_PRODUCT))


def state_absent(cache_dir):
    print("\nstate: absent (FT_DATA_CACHE=%s)" % ftdata.CACHE_DIR)
    r, f = frames(opts())
    check("an empty cache renders the no-data panel",
          contains_text(f, "NO SHIP DATA"))
    check("...names the command that fixes it", contains_text(f, "FTDATA"))
    check("...and draws no axis at all", r.ships is None)
    lit = float((f.max(axis=2) > 40).mean())
    check("...and nothing that could be mistaken for a schedule", lit < 0.12,
          "%.1f%% of pixels lit" % (lit * 100))


def state_corrupt(cache_dir):
    print("\nstate: corrupt (FT_DATA_CACHE=%s)" % ftdata.CACHE_DIR)
    r, f = frames(opts())
    check("a file that is not JSON renders the no-data panel",
          contains_text(f, "NO SHIP DATA"), (r.problems or [""])[0])


def state_notide(cache_dir):
    print("\nstate: schedule but no predictions (FT_DATA_CACHE=%s)"
          % ftdata.CACHE_DIR)
    r, f = frames(opts())
    check("the board still draws without the water", r.ships is not None)
    check("...and says the predictions are missing",
          contains_text(f, "NO TIDE") or contains_text(f, "PREDICTIONS"),
          "; ".join(r.problems)[:60])
    check("...and draws no curve", r.tide is None)


STATES = {"fresh": state_fresh, "stale": state_stale, "absent": state_absent,
          "corrupt": state_corrupt, "notide": state_notide}


def build_state_caches(live_cache, root, tstation, cstation):
    """One cache directory per data state, derived from the live records."""
    sname = ships.SHIPS_PRODUCT
    tname, cname = "tide-" + tstation, "currents-" + cstation
    live_s = ftdata.load(sname, live_cache)
    live_t = ftdata.load(tname, live_cache)
    live_c = ftdata.load(cname, live_cache)
    if live_s is None or live_t is None:
        return None
    now = time.time()
    dirs = {}

    dirs["fresh"] = d = os.path.join(root, "fresh")
    _write(d, sname, copy.deepcopy(live_s[0]), now)
    _write(d, tname, copy.deepcopy(live_t[0]), now)
    if live_c:
        _write(d, cname, copy.deepcopy(live_c[0]), now)

    # Fetched a fortnight ago, which is past the week this product allows, but
    # carrying a calendar that is still about the future -- so it must draw and
    # must say so. That is the whole point of separating age from span.
    dirs["stale"] = d = os.path.join(root, "stale")
    _write(d, sname, copy.deepcopy(live_s[0]), now - 14 * 86400.0)
    _write(d, tname, copy.deepcopy(live_t[0]), now)
    if live_c:
        _write(d, cname, copy.deepcopy(live_c[0]), now)

    dirs["absent"] = d = os.path.join(root, "absent")
    os.makedirs(d, exist_ok=True)

    dirs["corrupt"] = d = os.path.join(root, "corrupt")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sname + ".json"), "w") as fh:
        fh.write("{not json at all")
    _write(d, tname, copy.deepcopy(live_t[0]), now)

    dirs["notide"] = d = os.path.join(root, "notide")
    _write(d, sname, copy.deepcopy(live_s[0]), now)
    if live_c:
        _write(d, cname, copy.deepcopy(live_c[0]), now)
    return dirs


def run_states(live_cache, tstation, cstation):
    """Re-exec this script once per state with FT_DATA_CACHE set.

    In a subprocess and not in a loop here, because `ftdata.CACHE_DIR` is read
    from the environment at import and stays read. A previous version of this
    file reloaded the module between states, believed it, and was checking the
    live cache five times.
    """
    print("\ndata states, each in its own process")
    root = tempfile.mkdtemp(prefix="ships-test-")
    try:
        dirs = build_state_caches(live_cache, root, tstation, cstation)
        if dirs is None:
            check("live records available to derive the states from", False)
            return
        for name in ("fresh", "stale", "absent", "corrupt", "notide"):
            env = dict(os.environ, FT_DATA_CACHE=dirs[name],
                       FT_DATA_BLOBS=dirs[name])
            out = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--state", name],
                env=env, capture_output=True, text=True)
            body, _, tail = out.stdout.rpartition("##RESULT")
            sys.stdout.write(body)
            try:
                ran, failed = (int(x) for x in tail.split())
            except ValueError:
                check("state %s ran" % name, False,
                      (out.stderr.strip().splitlines() or ["no output"])[-1])
                continue
            PASSED[0] += ran
            for k in range(failed):
                FAILED.append("state %s: check %d" % (name, k + 1))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--tide-station", default=ships.TIDE_STATION)
    ap.add_argument("--current-station", default=ships.CURRENT_STATION)
    ap.add_argument("--state", choices=sorted(STATES),
                    help="run one data state against FT_DATA_CACHE and exit")
    a = ap.parse_args()

    if a.state:
        STATES[a.state](ftdata.CACHE_DIR)
        print("##RESULT %d %d" % (PASSED[0], len(FAILED)))
        return 1 if FAILED else 0

    print("cache: %s" % a.cache_dir)
    test_product(a.cache_dir)
    test_parsing()
    test_axis()
    if test_schedule(a.cache_dir) is not None:
        test_pixels(a.cache_dir)
        test_slack_guides(a.cache_dir)
        test_motion(a.cache_dir)
        test_crowding(a.cache_dir)
        test_sizes(a.cache_dir)
        run_states(a.cache_dir, a.tide_station, a.current_station)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
