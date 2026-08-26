#!/usr/bin/env python3
"""Checks for stringline.py that a screenshot cannot make.

A Marey diagram is all geometry, and geometry fails quietly. Every one of these
draws a perfectly convincing panel of diagonal lines:

  1. **The distance axis is evenly spaced.** Stations at equal row intervals
     instead of at their true track kilometres is a picture in which every
     train travels at a constant speed, which destroys the one thing a
     stringline is for. It looks *tidier* than the right answer.
  2. **A direction is inverted.** Southbound trains sloping up is a diagram of
     a railway that does not exist, and at seventeen degrees on a 64-row panel
     nobody spots it by eye.
  3. **Past and future are the same ink.** Prediction drawn as observation is
     the specific dishonesty this panel exists to avoid, and it is invisible.
  4. **A train from another line gets in.** One extra diagonal invents a
     headway. It is the most plausible-looking error here.
  5. **The hand-rolled protobuf reader mis-parses.** There is no schema to
     catch it; a wrong field number yields plausible small integers.

So the geometry is asserted **in pixels** against synthetic trains whose
answers are arithmetic -- a train that leaves station 0 at a known minute and
reaches station n-1 at another known one has to be at a computable row in a
computable column -- and the wire reader is asserted against a message this
file encodes itself, including the two cases the live feed rarely shows: a
negative delay and a SKIPPED stop.

Two things about how these run, both house convention. The demo is **not a pure
function of `t`**: like tide.py and adsb.py it takes the present moment from the
wall clock, so every check builds with `--at` and `--rate 0` to freeze it, and
renders frames sequentially from a fresh `build()`. And `ftdata.CACHE_DIR` binds
at import, so fresh, stale and absent are each run in a **separate process**
with FT_DATA_CACHE set, at the bottom of this file.

    $ python3 scripts/test-stringline.py                     # live cache too
    $ python3 scripts/test-stringline.py --cache-dir /tmp/c

The live cache is only needed for the checks against real data; everything else
builds its own. Populate it with
`python3 ftdata.py --once --only bart-stringline`.
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

import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402
import stringline                                             # noqa: E402

FAILED = []
PASSED = [0]

# The product is `volatile`, which means ftdata looks in the *blob* directory
# before the cache directory -- so a --cache-dir pointed at a scratch record is
# quietly overruled by whatever the machine has in /run/ftdata or in
# FT_DATA_BLOBS. That is correct on the wall and a trap in a test: every check
# below passed against the live cache once before it was noticed. So the blob
# directory is redirected alongside every synthetic cache, and put back before
# the live checks.
REAL_BLOB_DIR = ftdata.BLOB_DIR


def point_blobs(d):
    """Make `d` the only place a volatile record can come from."""
    ftdata.BLOB_DIR = d
    return d

# A fixed moment to freeze the clock at, so every row and column in this file
# is arithmetic rather than whatever time it happens to be. Monday teatime.
T0 = 1786408800.0


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    kw.setdefault("at", "%f" % T0)
    kw.setdefault("rate", 0.0)                # a frozen, reproducible present
    return ds.options(stringline, **kw)


def frames(args, n=6):
    """Render `n` frames in order from a fresh build. Never sample sparsely."""
    r = stringline.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def contains_text(frame, s, thresh=90, scales=(1, 2)):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure the honest
    message actually reached it rather than merely having been computed. This
    panel is mostly black, so unlike caiso's version this one does not have to
    check that the counters are dark as well.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = stringline.text_mask(s, scale)
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
# Synthetic records. The whole point is that the answers are arithmetic.
# --------------------------------------------------------------------------

def lines():
    return stringline.load_lines()


def write_record(cache_dir, trips, fetched_ago=30.0, now=T0, **extra):
    """Write a bart-stringline record by hand. `trips` are payload dicts."""
    os.makedirs(cache_dir, exist_ok=True)
    point_blobs(cache_dir)
    payload = {"t": now - fetched_ago, "feed_t": now - fetched_ago,
               "lines": [k for k, _ in lines()], "keep": 5400.0,
               "n_feed": len(trips), "n_trips": len(trips), "n_unknown": 0,
               "n_points": sum(len(t.get("s", ())) for t in trips),
               "source": "synthetic", "trips": trips}
    payload.update(extra)
    # `fetched_at` is measured against the *real* clock, not the frozen one:
    # ftdata.load() computes age from time.time(), so a record stamped relative
    # to T0 is however old T0 happens to be today, and every "is it fresh"
    # check would answer for the calendar rather than for the test.
    rec = {"name": stringline.PRODUCT, "fetched_at": time.time() - fetched_ago,
           "source": "synthetic", "ttl": 300, "payload": payload}
    with open(os.path.join(cache_dir, stringline.PRODUCT + ".json"), "w") as fh:
        json.dump(rec, fh)
    return cache_dir


def straight(line_i, direction, t_start, minutes, first=0, last=None,
             delay=0, tid="T"):
    """A train running at constant *speed* between two stations.

    Constant speed, not constant time per station, because the point of the
    distance axis is that those are different: a train doing the same
    kilometres per minute the whole way is a straight line on a correct panel
    and a bent one on a panel whose axis is evenly spaced. That is check 1.
    """
    key, ln = lines()[line_i]
    last = ln.n - 1 if last is None else last
    a, b = (first, last) if direction == 0 else (last, first)
    step = 1 if b > a else -1
    idx = list(range(a, b + step, step))
    km = np.asarray([float(ln.km[i]) for i in idx])
    frac = np.abs(km - km[0]) / max(abs(km[-1] - km[0]), 1e-6)
    secs = (frac * minutes * 60.0).round().astype(int)
    return {"i": tid, "l": line_i, "d": direction, "t0": int(t_start),
            "s": idx, "a": [int(v) for v in secs], "y": delay}


# --------------------------------------------------------------------------
# 1. The distance axis is track kilometres.
# --------------------------------------------------------------------------

def test_axis_is_distance():
    print("\nthe distance axis")
    key, ln = lines()[0]
    check("yellow is the whole line, Antioch to Millbrae",
          ln.n == 28 and ln.codes[0] == "ANTC" and ln.codes[-1] == "MLBR",
          "%d stations, %.1f km" % (ln.n, ln.span))
    check("...and its length is BART's, not a sum of straight hops",
          95.0 < ln.span < 105.0, "%.2f km" % ln.span)

    tmp = tempfile.mkdtemp(prefix="sl-axis")
    try:
        write_record(tmp, [straight(0, 0, T0 - 1800, 90)])
        r, f = frames(opts(cache_dir=tmp))
        rows = r.rows
        # Row spacing has to follow kilometres. Downtown San Francisco is eight
        # stations in seven kilometres and the Berkeley hills are two in seven;
        # an evenly spaced axis would give those the same number of rows.
        i_emb, i_civ = ln.codes.index("EMBR"), ln.codes.index("CIVC")
        i_ori, i_roc = ln.codes.index("ORIN"), ln.codes.index("ROCK")
        d_sf = rows[i_civ] - rows[i_emb]
        d_hills = rows[i_roc] - rows[i_ori]
        km_sf = float(ln.km[i_civ] - ln.km[i_emb])
        km_hills = float(ln.km[i_roc] - ln.km[i_ori])
        check("three downtown hops take fewer rows than one hill hop",
              d_sf < d_hills, "%.1f rows / %.1f km vs %.1f rows / %.1f km"
              % (d_sf, km_sf, d_hills, km_hills))
        ratio = (d_sf / d_hills) / (km_sf / km_hills)
        check("...and rows per kilometre is the same for both",
              abs(ratio - 1.0) < 0.02, "ratio %.4f" % ratio)
        check("the terminals sit on the first and last plot rows",
              abs(rows[0]) < 0.51 and abs(rows[-1] - (r.layout.plot_h - 1)) < 0.51,
              "%.2f .. %.2f of %d" % (rows[0], rows[-1], r.layout.plot_h))
        check("both terminals are labelled in the gutter",
              contains_text(f, "ANTIOCH") and contains_text(f, "MILLBRA"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2. Direction, slope and where a train actually is.
# --------------------------------------------------------------------------

def _trip_pixels(f, lay, colour, tol=40):
    """(rows, cols) of the pixels drawn in roughly one direction's colour."""
    reg = f[lay.chart_y:lay.chart_y + lay.plot_h, lay.plot_x:]
    d = np.abs(reg.astype(np.int32) - np.asarray(colour, np.int32)).sum(2)
    ys, xs = np.nonzero(d <= tol)
    return ys, xs


def test_direction_and_slope():
    print("\ndirection, slope and position")
    key, ln = lines()[0]
    tmp = tempfile.mkdtemp(prefix="sl-dir")
    try:
        # One train, Antioch to Millbrae, ninety minutes, starting half an hour
        # before now. It must slope downwards and be exactly halfway down the
        # line at T0 + 15 minutes... but we freeze at T0, when it is 30/90 of
        # the way along.
        write_record(tmp, [straight(0, 0, T0 - 1800, 90)])
        r, f = frames(opts(cache_dir=tmp))
        lay = r.layout
        ys, xs = _trip_pixels(f, lay, stringline.C_DOWN)
        check("a train towards Millbrae is drawn in the warm colour",
              len(ys) > 40, "%d pixels" % len(ys))
        if len(ys) > 4:
            slope = np.polyfit(xs, ys, 1)[0]
            check("...and slopes downwards as time runs right",
                  slope > 0, "%.3f rows per column" % slope)

        write_record(tmp, [straight(0, 1, T0 - 1800, 90)])
        r, f = frames(opts(cache_dir=tmp))
        ys, xs = _trip_pixels(f, lay, stringline.C_UP)
        check("a train towards Antioch is drawn in the cool colour",
              len(ys) > 40, "%d pixels" % len(ys))
        if len(ys) > 4:
            slope = np.polyfit(xs, ys, 1)[0]
            check("...and slopes upwards", slope < 0,
                  "%.3f rows per column" % slope)

        # Where is it *now*? Thirty minutes into a ninety minute run at
        # constant speed is a third of the way down the panel, and the dot on
        # the now-line is what says so.
        write_record(tmp, [straight(0, 0, T0 - 1800, 90)])
        r, f = frames(opts(cache_dir=tmp))
        spp, nowc = r.geometry[0], r.geometry[1]
        colx = lay.plot_x + nowc
        band = f[lay.chart_y:lay.chart_y + lay.plot_h, colx]
        lit = np.nonzero(band.max(1) > 120)[0]
        want = (lay.plot_h - 1) / 3.0
        check("the dot on the now-line is a third of the way down",
              len(lit) and abs(float(lit.mean()) - want) <= 2.0,
              "row %.1f, wanted %.1f" % (float(lit.mean()) if len(lit) else -1,
                                         want))
        check("...and the now-line is labelled NOW", contains_text(f, "NOW"))

        # A train that finished an hour ago, and one that starts in two hours.
        # Neither is on the panel, and neither is a crash.
        write_record(tmp, [straight(0, 0, T0 - 9000, 60, tid="old"),
                           straight(0, 0, T0 + 7200, 60, tid="new")])
        r, f = frames(opts(cache_dir=tmp))
        ys, _ = _trip_pixels(f, lay, stringline.C_DOWN)
        check("trains outside the window draw nothing", len(ys) == 0,
              "%d pixels" % len(ys))
        check("...and are not counted as running", r.state["running"] == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 3. Past is fact, future is prediction, and they must not look the same.
# --------------------------------------------------------------------------

def test_past_and_future():
    print("\npast solid, future dashed")
    tmp = tempfile.mkdtemp(prefix="sl-tense")
    try:
        # One long train straddling the now-line, so the same geometry is in
        # both halves of the panel and the only difference is the styling.
        write_record(tmp, [straight(0, 0, T0 - 2400, 90)])
        r, f = frames(opts(cache_dir=tmp))
        lay = r.layout
        nowc = r.geometry[1]
        reg = f[lay.chart_y:lay.chart_y + lay.plot_h,
                lay.plot_x:lay.plot_x + lay.plot_w]
        past = reg[:, :nowc]
        fut = reg[:, nowc:]
        # Only columns the train is actually in, so the empty right-hand end
        # does not dilute the measurement.
        # Well above the gridlines, whose brightest is 50: a threshold that
        # counts a gridline as "the train is here" would report the dashed
        # side as unbroken, because the gaps in the dash show the grid.
        pc = past.max(2).max(0) > 60
        fc = fut.max(2).max(0) > 60
        pbright = past.max(2).max(0)[pc].mean() if pc.any() else 0
        fbright = fut.max(2).max(0)[fc].mean() if fc.any() else 0
        check("both halves have the train in them", pc.sum() > 20
              and fc.sum() > 20, "%d past columns, %d future" % (pc.sum(),
                                                                 fc.sum()))
        check("the future is dimmer than the past", fbright < pbright * 0.75,
              "%.0f vs %.0f" % (fbright, pbright))
        # The dash: in the future, whole columns of the train are missing on a
        # fixed period. In the past, none are.
        span_p = np.nonzero(pc)[0]
        span_f = np.nonzero(fc)[0]
        gap_p = 1.0 - pc[span_p[0]:span_p[-1] + 1].mean()
        gap_f = 1.0 - fc[span_f[0]:span_f[-1] + 1].mean()
        check("the past is unbroken", gap_p < 0.02, "%.3f of columns blank"
              % gap_p)
        check("the future is dashed", gap_f > 0.15, "%.3f of columns blank"
              % gap_f)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_crossing():
    print("\ntrains passing each other")
    tmp = tempfile.mkdtemp(prefix="sl-cross")
    try:
        # Two trains, opposite directions, same ninety minutes. They cross once
        # in the middle, and that crossing is the question a stringline answers.
        write_record(tmp, [straight(0, 0, T0 - 2700, 90, tid="a"),
                           straight(0, 1, T0 - 2700, 90, tid="b")])
        r, f = frames(opts(cache_dir=tmp))
        lay = r.layout
        ys, xs = _trip_pixels(f, lay, stringline.C_CROSS, tol=30)
        check("where the two lines meet is drawn as a crossing",
              len(ys) > 0, "%d pixels" % len(ys))
        if len(ys):
            check("...and it is near the middle of the line",
                  abs(float(ys.mean()) - lay.plot_h / 2.0) < lay.plot_h / 4.0,
                  "row %.1f of %d" % (float(ys.mean()), lay.plot_h))
        # One train alone must never paint the crossing colour.
        write_record(tmp, [straight(0, 0, T0 - 2700, 90, tid="a")])
        r, f = frames(opts(cache_dir=tmp))
        ys, _ = _trip_pixels(f, lay, stringline.C_CROSS, tol=30)
        check("one train alone never paints a crossing", len(ys) == 0,
              "%d pixels" % len(ys))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_line_filter():
    print("\nonly this line's trains")
    tmp = tempfile.mkdtemp(prefix="sl-filter")
    try:
        write_record(tmp, [straight(0, 0, T0 - 1800, 90, tid="yellow"),
                           straight(3, 0, T0 - 1800, 60, tid="red"),
                           straight(4, 1, T0 - 1800, 50, tid="blue")])
        r, _ = frames(opts(cache_dir=tmp, line="yellow"))
        check("a record of five lines yields one line's trains",
              len(r.state["trips"]) == 1, "%d trips" % len(r.state["trips"]))
        r, _ = frames(opts(cache_dir=tmp, line="red"))
        check("...and asking for another gets that one",
              len(r.state["trips"]) == 1 and r.line.key == "red")
        r, _ = frames(opts(cache_dir=tmp, line="green"))
        check("a line with no trains in the record draws no trains",
              len(r.state["trips"]) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. The hand-rolled protobuf reader, against a message this file encodes.
# --------------------------------------------------------------------------

def _v(n):
    """Encode a varint."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _fld(num, wire, body):
    return _v((num << 3) | wire) + body


def _msg(num, body):
    return _fld(num, 2, _v(len(body)) + body)


def _varfield(num, value):
    if value < 0:
        value += 1 << 64                     # int32 negatives, protobuf style
    return _fld(num, 0, _v(value))


def _feed(ts, trips):
    """trips: [(trip_id, delay, [(stop_id, time, skipped)])]"""
    out = _msg(1, _varfield(3, ts) + _msg(1, b"2.0"))
    for tid, delay, stops in trips:
        tu = _msg(1, _msg(1, tid.encode()))
        for sid, when, skipped in stops:
            stu = _msg(2, _varfield(1, 0) + _varfield(2, when))
            stu += _msg(4, sid.encode())
            if skipped:
                stu += _varfield(5, 1)
            tu += _msg(2, stu)
        if delay is not None:
            tu += _varfield(5, delay)
        out += _msg(2, _msg(1, b"e") + _msg(3, tu))
    return out


def test_protobuf():
    print("\nthe hand-rolled protobuf reader")
    stop_of = ftdata._bart_geometry()[0]
    blob = _feed(1786408331, [
        ("1965520", 61, [("M30-1", 1786408411, False),
                         ("M40-1", 1786408515, False),
                         ("M16-2", 1786408681, True)]),
        ("1965521", -95, [("A10-1", 1786408900, False),
                          ("ZZZZ-9", 1786409000, False)]),
    ])
    feed_t, trips = ftdata._bart_parse(blob, stop_of)
    check("the feed timestamp comes back", feed_t == 1786408331.0,
          "%r" % feed_t)
    check("both trips come back", len(trips) == 2, "%d" % len(trips))
    tid, stops, delay = trips[0]
    check("the trip id is a string, not bytes", tid == "1965520", repr(tid))
    check("platform stop ids become station codes",
          [c for c, _ in stops] == ["POWL", "CIVC"],
          str([c for c, _ in stops]))
    check("a SKIPPED stop is dropped", len(stops) == 2)
    check("the arrival times survive", stops[0][1] == 1786408411.0)
    check("a positive delay reads back", delay == 61.0, "%r" % delay)
    check("a negative delay is not read as 2**64",
          trips[1][2] == -95.0, "%r" % trips[1][2])
    check("an unknown stop id is dropped, not guessed",
          [c for c, _ in trips[1][1]] == ["LAKE"],
          str([c for c, _ in trips[1][1]]))

    # A field the reader does not know about must cost nothing. This is the
    # property that makes reading a schemaless feed by hand defensible.
    blob2 = _feed(1786408331, [("9", None, [("M50-1", 1786408411, False)])])
    blob2 += _fld(99, 0, _v(12345)) + _msg(98, b"whatever")
    feed_t2, trips2 = ftdata._bart_parse(blob2, stop_of)
    check("unknown top-level fields are skipped",
          feed_t2 == feed_t and len(trips2) == 1, "%d trips" % len(trips2))
    for wire, body in ((5, b"\x01\x02\x03\x04"), (1, b"\x01" * 8)):
        try:
            ftdata._pb_fields(_fld(7, wire, body) + _msg(1, b"x"))
            ok = True
        except Exception as e:                               # noqa: BLE001
            ok = repr(e)
        check("fixed%d fields are skipped cleanly" % (32 if wire == 5 else 64),
              ok is True, "" if ok is True else str(ok)[:50])


def test_classifier():
    print("\nmatching a trip to a line")
    stop_of, index, ends, hints, keys = ftdata._bart_geometry()
    yellow = keys.index("yellow")

    # A full Yellow run is on exactly one line, and nothing else is.
    codes = ["ANTC", "PCTR", "PITT", "NCON", "CONC", "PHIL", "WCRK"]
    li, dr = ftdata._bart_line_of("nope", codes, index, ends, hints)
    check("a run down the Concord line is the Yellow line",
          li == yellow and dr == 0, "%r %r" % (li, dr))
    li, dr = ftdata._bart_line_of("nope", codes[::-1], index, ends, hints)
    check("...and backwards is the other direction", li == yellow and dr == 1)

    # Trunk-only, no hint: several lines contain it and it stays undecided
    # rather than being guessed onto the busiest one.
    li, _ = ftdata._bart_line_of("nope", ["EMBR", "MONT", "POWL"],
                                 index, ends, hints)
    check("a train seen only in the Market Street trunk is left unassigned",
          li is None, "%r" % li)

    # The same trip with a hint from the baked schedule resolves, and the hint
    # is checked: point it at a line the stops are not on and it is ignored.
    fake = {"X": (keys.index("blue"), 0,
                  len([c for c in index[keys.index("blue")]]) - 1)}
    li, _ = ftdata._bart_line_of("X", ["EMBR", "MONT", "POWL"],
                                 index, ends, fake)
    check("a hint resolves a trunk-only train", li == keys.index("blue"),
          "%r" % li)
    li, _ = ftdata._bart_line_of("X", ["ANTC", "PCTR"], index, ends, fake)
    check("...but a hint contradicted by the stops is ignored",
          li == yellow, "%r" % li)
    li, _ = ftdata._bart_line_of("X", ["POWL", "MONT", "EMBR"],
                                 index, ends, fake)
    check("...and so is one pointing the wrong way", li is None, "%r" % li)

    # The real schedule's own trips, run back through the station-set matcher.
    ids = list(hints.items())[:400]
    wrong = 0
    for tid, (li0, dr0, last) in ids:
        got, _ = ftdata._bart_line_of("unknown-id",
                                      _stations_of(index, li0, dr0, last),
                                      index, ends, {})
        if got is not None and got != li0:
            wrong += 1
    check("no scheduled trip is matched to the wrong line", wrong == 0,
          "%d of %d wrong" % (wrong, len(ids)))


def _stations_of(index, li, dr, last):
    """The whole line's station codes, in the direction `dr`, ending at `last`."""
    codes = [None] * len(index[li])
    for c, i in index[li].items():
        codes[i] = c
    return codes[:last + 1] if dr == 0 else codes[last:][::-1]


# --------------------------------------------------------------------------
# 5. The fetcher's merge -- the thing that makes the past half exist.
# --------------------------------------------------------------------------

def test_history_merge():
    print("\nthe rolling history")
    tmp = tempfile.mkdtemp(prefix="sl-merge")
    try:
        stop_of, index, ends, hints, keys = ftdata._bart_geometry()
        yellow = keys.index("yellow")
        prev = {"trips": [{"i": "Z", "l": yellow, "d": 0,
                           "t0": int(T0 - 600), "s": [10, 11, 12],
                           "a": [0, 120, 240], "y": 0}]}
        rec = {"name": ftdata.BART_PRODUCT, "fetched_at": T0 - 60,
               "source": "x", "ttl": 300, "payload": prev}
        os.makedirs(tmp, exist_ok=True)
        point_blobs(tmp)
        with open(os.path.join(tmp, ftdata.BART_PRODUCT + ".json"), "w") as fh:
            json.dump(rec, fh)
        back = ftdata._bart_previous(tmp, index)
        check("last pass's record reads back as trip state", "Z" in back
              and len(back["Z"][2]) == 3, str(sorted(back.get("Z", [0, 0, {}])[2])))
        check("...with absolute times, not offsets",
              back["Z"][2][10] == T0 - 600, "%r" % back["Z"][2].get(10))
        bad = ftdata._bart_previous(point_blobs(os.path.join(tmp, "nope")),
                                    index)
        check("no previous record is an empty history, not an error", bad == {})

        # A record naming a line index that no longer exists must contribute
        # nothing rather than throwing the whole pass away.
        prev["trips"].append({"i": "Q", "l": 99, "d": 0, "t0": int(T0),
                              "s": [1], "a": [0], "y": 0})
        with open(os.path.join(tmp, ftdata.BART_PRODUCT + ".json"), "w") as fh:
            json.dump(rec, fh)
        back = ftdata._bart_previous(tmp, index)
        check("a trip on an unknown line is skipped, not fatal",
              list(back) == ["Z"], str(list(back)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_monotonic_times():
    print("\ntimes that go backwards")
    tmp = tempfile.mkdtemp(prefix="sl-mono")
    try:
        # np.interp against a decreasing x is not an error; it is silently
        # wrong. The demo has to survive a record that carries one anyway.
        tr = straight(0, 0, T0 - 1800, 90)
        tr["a"][5] = tr["a"][8]
        tr["a"][6] = tr["a"][3]
        write_record(tmp, [tr])
        r, f = frames(opts(cache_dir=tmp))
        check("a record with out-of-order times still renders",
              f.shape == (64, 320, 3) and f.max() > 0)
        t = r.state["trips"][0].t
        check("...and the demo has forced its times non-decreasing",
              bool(np.all(np.diff(t) >= 0)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 6. Degraded states.
# --------------------------------------------------------------------------

def test_degraded():
    print("\nmissing, empty and stale")
    tmp = tempfile.mkdtemp(prefix="sl-degraded")
    try:
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        point_blobs(empty)
        r, f = frames(opts(cache_dir=empty))
        check("no record at all draws the no-data card",
              contains_text(f, "NO BART DATA") and r.state["payload"] is None)
        check("...and says how to fix it", contains_text(f, "FTDATA"))

        quiet = write_record(os.path.join(tmp, "quiet"), [])
        r, f = frames(opts(cache_dir=quiet))
        check("a record with no trains draws the grid and says so",
              contains_text(f, "NO TRAINS") and contains_text(f, "ANTIOCH"),
              "BART is shut for six hours every night")

        stale = write_record(os.path.join(tmp, "stale"),
                             [straight(0, 0, T0 - 1800, 90)],
                             fetched_ago=1800.0)
        r, f = frames(opts(cache_dir=stale))
        check("a half-hour-old record still draws its trains",
              len(r.state["trips"]) == 1 and not contains_text(f, "NO BART"))
        check("...and says STALE on the panel", contains_text(f, "STALE"))

        broken = os.path.join(tmp, "broken")
        os.makedirs(broken)
        point_blobs(broken)
        with open(os.path.join(broken, stringline.PRODUCT + ".json"), "w") as fh:
            fh.write("{not json")
        r, f = frames(opts(cache_dir=broken))
        check("a corrupt record draws the no-data card, not a traceback",
              contains_text(f, "NO BART DATA"))

        # A record whose trips are individually malformed: one bad train is one
        # train missing, not a dead panel.
        mixed = os.path.join(tmp, "mixed")
        good = straight(0, 0, T0 - 1800, 90, tid="good")
        write_record(mixed, [good,
                             {"i": "b1", "l": 0, "d": 0, "t0": T0,
                              "s": [1, 2], "a": [0]},
                             {"i": "b2", "l": 0, "d": 0, "t0": T0,
                              "s": [999, 1000], "a": [0, 60], "y": 0},
                             {"i": "b3"}])
        r, f = frames(opts(cache_dir=mixed))
        check("malformed trips are dropped and the good one still draws",
              len(r.state["trips"]) == 1, "%d trips" % len(r.state["trips"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_delay_readout():
    print("\nthe delay readout")
    tmp = tempfile.mkdtemp(prefix="sl-delay")
    try:
        write_record(tmp, [straight(0, 0, T0 - 1800, 90, delay=0, tid="a"),
                           straight(0, 1, T0 - 1800, 90, delay=30, tid="b")])
        r, f = frames(opts(cache_dir=tmp))
        check("a punctual line says ON TIME", contains_text(f, "ON TIME"),
              "median %r s" % r.state["delay"])
        write_record(tmp, [straight(0, 0, T0 - 1800, 90, delay=240, tid="a"),
                           straight(0, 1, T0 - 1800, 90, delay=300, tid="b")])
        r, f = frames(opts(cache_dir=tmp))
        check("a late line says how late", contains_text(f, "LATE")
              and contains_text(f, "RUNNING"), "median %r s" % r.state["delay"])
        check("...and counts the trains that are running",
              r.state["running"] == 2, "%d" % r.state["running"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 7. Motion: the picture slides, it does not jitter.
# --------------------------------------------------------------------------

def test_motion():
    print("\nhow the picture moves")
    tmp = tempfile.mkdtemp(prefix="sl-motion")
    try:
        write_record(tmp, [straight(0, 0, T0 - 2400, 90, tid="a"),
                           straight(0, 1, T0 - 2400, 90, tid="b")])
        # A frozen clock: only the pulse may move, and the pulse only touches
        # the now column and the dots on it.
        args = opts(cache_dir=tmp)
        r = stringline.build(args)
        a = r(0.0, 0).copy()
        b = r(1.0, 20).copy()
        nowx = r.layout.plot_x + r.geometry[1]
        off = np.abs(a.astype(int) - b.astype(int)).max(2)
        off[:, max(0, nowx - 1):nowx + 2] = 0
        check("with the clock stopped, only the now-line changes",
              off.max() == 0, "%d elsewhere" % int(off.max()))

        # Now let time run. The chart must translate left by whole columns,
        # which is what the strip trick buys: no rebuild, no jitter.
        spp = r.geometry[0]
        r1 = stringline.build(opts(cache_dir=tmp, at="%f" % T0))
        r2 = stringline.build(opts(cache_dir=tmp, at="%f" % (T0 + 4 * spp)))
        f1, f2 = r1(0.0, 0).copy(), r2(0.0, 0).copy()
        lay = r1.layout
        y0, y1 = lay.chart_y, lay.chart_y + lay.plot_h
        # Four columns later, the past half of the picture is the same picture
        # four columns to the left.
        nowc = r1.geometry[1]
        x0 = lay.plot_x + 10
        a = f1[y0:y1, x0 + 4:lay.plot_x + nowc - 4]
        b = f2[y0:y1, x0:lay.plot_x + nowc - 8]
        same = float((a == b).mean())
        check("four columns of clock slide the past four columns left",
              same > 0.995, "%.4f of pixels identical" % same)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 8. Fresh, stale and absent, each in a process of its own.
# --------------------------------------------------------------------------

def _one_state(state, cache_dir):
    args = opts()                       # note: no cache_dir, so CACHE_DIR wins
    r = stringline.build(args)
    out = None
    for i in range(6):
        out = r(i / 20.0, i)
    card = contains_text(out, "NO BART DATA")
    stale = contains_text(out, "STALE")
    drew = bool(r.state["trips"])
    verdict = {
        "fresh": (drew and not card and not stale, "drew trains, no flags"),
        "stale": (drew and not card and stale, "drew trains with STALE on it"),
        "absent": (not drew and card, "drew the no-data card"),
    }[state]
    print("RESULT %s %s cache=%s drew=%s card=%s stale=%s"
          % (state, "ok" if verdict[0] else "FAIL",
             os.path.basename(cache_dir), drew, card, stale))
    return 0 if verdict[0] else 1


def test_states_in_separate_processes():
    print("\nfresh, stale and absent -- one process each, FT_DATA_CACHE set")
    tmp = tempfile.mkdtemp(prefix="sl-proc")
    try:
        fresh = write_record(os.path.join(tmp, "fresh"),
                             [straight(0, 0, T0 - 1800, 90)], fetched_ago=30.0)
        stale = write_record(os.path.join(tmp, "stale"),
                             [straight(0, 0, T0 - 1800, 90)], fetched_ago=1800.0)
        absent = os.path.join(tmp, "absent")
        os.makedirs(absent)
        point_blobs(absent)
        for state, d in (("fresh", fresh), ("stale", stale),
                         ("absent", absent)):
            env = dict(os.environ, FT_DATA_CACHE=d, FT_DATA_BLOBS=d)
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--state", state, "--cache-dir", d],
                env=env, capture_output=True, text=True, timeout=180)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT")]
            check("%s cache, in its own process" % state,
                  proc.returncode == 0 and bool(line),
                  line[0][7:] if line else
                  (proc.stderr.strip().splitlines() or ["no output"])[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 9. Other panel sizes, cost, and the promise that none of this talks to anyone.
# --------------------------------------------------------------------------

def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="sl-size")
    try:
        write_record(tmp, [straight(0, 0, T0 - 2400, 90, tid="a"),
                           straight(0, 1, T0 - 2400, 90, tid="b")])
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, f = frames(opts(cache_dir=tmp, width=w, height=h), 30)
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                detail = "plot %dx%d, gutter %d" % (r.layout.plot_w,
                                                    r.layout.plot_h,
                                                    r.layout.gutter)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cost():
    print("\nframe cost")
    tmp = tempfile.mkdtemp(prefix="sl-cost")
    try:
        # Rush hour: thirty trains, which is more than BART has ever had on one
        # line at once. Timed with the clock running, so rebuilds are included.
        trips = []
        for i in range(30):
            trips.append(straight(0, i % 2, T0 - 3600 + i * 240, 90,
                                  tid="t%d" % i))
        write_record(tmp, trips)
        r = stringline.build(ds.options(stringline, cache_dir=tmp))
        for i in range(20):
            r(i / 20.0, i)
        n = 400
        t = time.perf_counter()
        times = []
        for i in range(n):
            t1 = time.perf_counter()
            r(i / 20.0, i)
            times.append(time.perf_counter() - t1)
        total = time.perf_counter() - t
        times.sort()
        mean = total / n * 1e3
        p95 = times[int(n * 0.95)] * 1e3
        worst = times[-1] * 1e3
        check("thirty trains render inside the budget", mean < 4.0,
              "mean %.3f ms  p95 %.3f ms  max %.3f ms" % (mean, p95, worst))
        check("...and the worst frame is a rebuild, not a stall", worst < 60.0,
              "%.2f ms" % worst)

        # The rebuild is the expensive event and it happens twice a minute, so
        # it is timed on its own rather than hidden in a mean over 400 frames
        # that contains none of them. One np.interp a train is the whole cost.
        t = time.perf_counter()
        for _ in range(20):
            r.state["baked"] = -1e18
            r(0.0, 0)
        bake = (time.perf_counter() - t) / 20 * 1e3
        check("a full rebuild of thirty trains is not a dropped frame",
              bake < 25.0, "%.2f ms" % bake)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load(stringline.PRODUCT, point_blobs(tempfile.mkdtemp(prefix="sl-net")))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "stringline.py")).read()
    body = src.split("def bake_lines", 1)[0]        # the offline tool may
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in body]
    check("stringline.py does not import one either", not imported,
          ",".join(imported))


def test_live(cache_dir):
    print("\nagainst the live cache (%s)" % cache_dir)
    ftdata.BLOB_DIR = REAL_BLOB_DIR
    got = ftdata.load(stringline.PRODUCT, cache_dir)
    if got is None:
        print("  --   no live record; run ftdata.py --once --only %s"
              % stringline.PRODUCT)
        return
    payload, age = got
    check("the live record is a five-line record",
          len(payload.get("lines", [])) == 5, str(payload.get("lines")))
    n = len(payload.get("trips", []))
    check("it carries trips", n > 0, "%d trips, %d stop times, age %s"
          % (n, payload.get("n_points", 0), ftdata.describe_age(age)))
    size = os.path.getsize(ftdata.record_path(stringline.PRODUCT, cache_dir))
    check("and it is small enough for a Pi's flash", size < 96 * 1024,
          "%.1f KB" % (size / 1024.0))
    unknown = payload.get("n_unknown", 0)
    feed = max(1, payload.get("n_feed", 1))
    check("most of the feed was matched to a line",
          unknown < feed * 0.4, "%d of %d unmatched" % (unknown, feed))
    args = ds.options(stringline, cache_dir=cache_dir)
    r = stringline.build(args)
    f = None
    for i in range(10):
        f = r(i / 20.0, i)
    check("the live record draws", f.max() > 0 and f.shape == (64, 320, 3),
          "%d trains running on the %s line" % (r.state["running"],
                                                r.line.key))


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
    test_axis_is_distance()
    test_direction_and_slope()
    test_past_and_future()
    test_crossing()
    test_line_filter()
    test_protobuf()
    test_classifier()
    test_history_merge()
    test_monotonic_times()
    test_degraded()
    test_delay_readout()
    test_motion()
    test_states_in_separate_processes()
    test_sizes()
    test_cost()
    test_live(a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
