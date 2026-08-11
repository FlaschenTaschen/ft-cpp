#!/usr/bin/env python3
"""Checks for bikes.py that a screenshot cannot make.

This panel puts two different measurements on one axis and asserts that they
are comparable. Every way it can be wrong produces a picture that looks exactly
as convincing as the right one:

  1. **The calibration can be upside down, or absent.** A gold line drawn from
     raw dock-count movement is a plausible, well-shaped, confident curve that
     is roughly half the truth, and nothing about it looks halved.
  2. **The typical day can be the wrong weekday.** Saturday's single midday
     hump under a Tuesday's clock is a beautiful chart and a lie about the
     morning.
  3. **A missed fetch can become a spike.** Charging a forty minute difference
     to the ten minute slot it was written into draws a tower with three holes
     beside it, and towers are what a viewer looks at.
  4. **A cold start can draw a line from zero.** A panel that has been up for
     an hour and draws a flat gold line back to midnight is asserting that
     nothing happened all morning.
  5. **Yesterday's record draws perfectly.** It parses, it has 144 slots, and
     it is the wrong day.

So the arithmetic is asserted against records built by hand whose answers are
known before they are drawn, the drawing is asserted **in pixels**, and the
words the panel uses to qualify its own number are read back off the frame.

Two things about how these are run, both learned the hard way in this tree.
The demo is **not a pure function of `t`** unless `--reload 0` is set -- it
takes the present moment from the wall clock, as `caiso` does -- so every check
renders frames **sequentially from a fresh `build()`**. And `ftdata.CACHE_DIR`
binds at import, so the three data states -- fresh, stale, absent -- are each
run in a **separate process** with FT_DATA_CACHE set, at the bottom of this
file. Reloading the module in one process does not test what it looks like it
tests.

    $ python3 scripts/test-bikes.py                     # uses the live cache
    $ python3 scripts/test-bikes.py --cache-dir /tmp/c  # or a pointed one

The live cache is only needed for the last group; everything else builds its
own. Populate it with `python3 ftdata.py --once --only baywheels`, twice, ten
minutes apart.
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

import bikes                                                  # noqa: E402
import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402

FAILED = []
PASSED = [0]

SLOTS = bikes.SLOTS
BUCKET = 600.0


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(bikes, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build. Never sample sparsely."""
    r = bikes.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def settled(args, n=120):
    """A frame from after the reveal has finished, still rendered in order."""
    return frames(args, n)


def contains_text(frame, s, thresh=80, scales=(1, 2), bg_max=0.25):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure the caveat
    actually reached it rather than merely being computed. The counters between
    the strokes have to be dark as well: this panel has a filled silhouette on
    it, and a matcher that only asks "are the strokes lit" answers yes to every
    string in the language somewhere inside a solid block.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = bikes.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                win = row[:, x:x + gw]
                if not np.array_equal(win & m, m):
                    continue
                if (win & ~m).mean() <= bg_max:
                    return True
    return False


def gold_rows(frame, lay):
    """The row of the brightest gold pixel in each column, or -1."""
    reg = frame[lay.chart_y:lay.chart_bot + 1].astype(int)
    # Saturated gold only: not the halo, not the silhouette, and -- the one
    # that cost a false failure here -- not the cream now-rule and not the
    # near-white head of the line either. Both of those are red-heavy too, but
    # only by about forty of blue, where the line itself is by a hundred and
    # ninety. A looser threshold reported a gold line on a panel that has none.
    warm = (reg[:, :, 0] > 150) & (reg[:, :, 0] > reg[:, :, 2] + 80)
    out = np.full(frame.shape[1], -1)
    for c in range(frame.shape[1]):
        got = np.flatnonzero(warm[:, c])
        if len(got):
            out[c] = got[0] + lay.chart_y
    return out


# --------------------------------------------------------------------------
# Records we write by hand, so that every answer is known before it is drawn.
# --------------------------------------------------------------------------

def record(cache_dir, when, mov, dt=None, fetched_ago=0.0, day_offset=0,
           drop_today=False, mangle=None):
    """Write a baywheels record carrying exactly these slots. Returns `when`.

    `mov` is a list of 144 values in the record's own unit -- the sum of
    |change| over the stations, so twice the docked bikes that changed place --
    with None for a slot no pass landed in.
    """
    day0 = bikes.day0_of(when) + day_offset * 86400.0
    dt = dt if dt is not None else [None if m is None else BUCKET for m in mov]
    payload = {"as_of": when, "region": "San Francisco", "n": 383,
               "totals": {"bikes": 2700, "loose": 600},
               "today": {"day0": day0, "bucket": BUCKET, "n": SLOTS,
                         "mov": list(mov), "dt": list(dt)}}
    if drop_today:
        del payload["today"]
    if mangle:
        mangle(payload)
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "baywheels.json"), "w") as fh:
        json.dump({"name": "baywheels", "fetched_at": time.time() - fetched_ago,
                   "source": "test", "payload": payload}, fh)
    return when


def flat_day(rate, upto, start=0, k_at=None):
    """144 slots of a constant `rate` estimated trips an hour, up to `upto`.

    Inverts the demo's own arithmetic so the check can state the answer in the
    units the panel prints: rate = mov/2 * 3600/dt * k, so mov = 2 * rate * dt
    / 3600 / k. `k_at(slot)` supplies the calibration the demo will apply.
    """
    mov = [None] * SLOTS
    for s in range(start, min(upto + 1, SLOTS)):
        k = k_at(s) if k_at else 1.0
        mov[s] = 2.0 * rate * (BUCKET / 3600.0) / k
    return mov


def k_of(wd):
    z, err = bikes.read_typical()
    assert z is not None, err
    k_hour, spread = bikes.calibration(z, wd)
    return (lambda s: float(k_hour[min(23, s // 6)])), float(spread)


def a_monday(hour=12, minute=0):
    """An epoch at `hour` on the most recent Monday. The weekday is the point:
    every check that names a shape names a weekday's shape."""
    now = time.time()
    day0 = bikes.day0_of(now)
    back = time.localtime(day0).tm_wday
    return day0 - back * 86400.0 + hour * 3600.0 + minute * 60.0


# --------------------------------------------------------------------------
# 1. The archive, which every number on the panel is anchored to.
# --------------------------------------------------------------------------

def test_asset():
    print("\nthe baked archive")
    z, err = bikes.read_typical()
    check("bikes-typical.npz loads", z is not None, err or
          "%s, %s to %s" % (",".join(z["months"]), z["span"][0], z["span"][1]))
    if z is None:
        return

    for wd in range(7):
        check("weekday %d has dates" % wd, len(z["t%d" % wd]) >= 8,
              "%d dates" % len(z["t%d" % wd]))

    # The shape check the brief demanded, and the one that catches a timezone
    # error in the parse: SF weekday bike traffic has two commute peaks around
    # eight and five, and weekends are a single midday hump. A profile parsed
    # in UTC would put the morning peak at one in the morning.
    for wd in range(5):
        _lo, med, _hi = bikes.profile(z, wd)
        hourly = med.reshape(24, 6).mean(1)
        am = int(np.argmax(hourly[5:11])) + 5
        pm = int(np.argmax(hourly[14:21])) + 14
        noon = hourly[12:14].mean()
        check("weekday %d peaks at %dh and %dh" % (wd, am, pm),
              7 <= am <= 9 and 16 <= pm <= 18
              and hourly[am] > noon * 1.5 and hourly[pm] > noon * 1.5,
              "%d/h, %d/h vs %d/h at noon" % (hourly[am], hourly[pm], noon))
    for wd in (5, 6):
        _lo, med, _hi = bikes.profile(z, wd)
        hourly = med.reshape(24, 6).mean(1)
        peak = int(np.argmax(hourly))
        check("weekend %d is one midday hump" % wd,
              11 <= peak <= 16 and hourly[8] < hourly[peak] * 0.6,
              "peak %dh, 8am is %d%% of it"
              % (peak, 100 * hourly[8] / hourly[peak]))

    for wd in (0, 5):
        k, spread = bikes.calibration(z, wd)
        check("weekday %d calibration is plausible" % wd,
              len(k) == 24 and 1.0 < k.min() and k.max() < 4.0
              and 0.0 < spread < 0.3,
              "k %.2f-%.2f, spread +-%.1f%%" % (k.min(), k.max(), spread * 100))

    # Under one, the estimator would be claiming to see more movement than
    # there were trips, which is arithmetically impossible: a trip moves at
    # most one bike between two docks and contributes at most one to mov/2.
    k, _ = bikes.calibration(z, 0)
    check("calibration never goes below one", k.min() >= 1.0, "%.3f" % k.min())

    lo, med, hi = bikes.profile(z, 0)
    check("the band brackets its own median", (lo <= med).all() and (med <= hi).all())
    check("the band is a visible width", (hi - lo).mean() / med.mean() > 0.05,
          "%.1f%% of the median on average" % (100 * (hi / med - lo / med).mean()))


# --------------------------------------------------------------------------
# 2. The estimator, in arithmetic. No pixels here.
# --------------------------------------------------------------------------

def test_estimate():
    print("\nturning slots into a rate")
    k_at, _ = k_of(0)
    k1 = np.ones(24, np.float32)

    mov = [None] * SLOTS
    mov[10] = 100.0                       # 50 bikes moved in ten minutes
    est = bikes.estimate({"mov": np.array([np.nan if m is None else m
                                           for m in mov], np.float32),
                          "dt": np.array([np.nan if m is None else BUCKET
                                          for m in mov], np.float32),
                          "bucket": BUCKET}, k1)
    check("one slot becomes one rate", abs(est["rate"][10] - 300.0) < 0.5,
          "%.1f/h" % est["rate"][10])
    check("nothing else is claimed", not np.isfinite(est["rate"][11]))
    check("the slot is covered", est["seen"][10] and not est["seen"][11])
    check("the cumulative counts it once",
          abs(float(est["trips"].sum()) - 50.0) < 0.01,
          "%.2f trips" % est["trips"].sum())

    # A missed pass: one difference forty minutes long, written into the slot
    # it landed in. It has to spread backwards over the four slots it really
    # describes rather than becoming a spike with three holes beside it.
    mov = [None] * SLOTS
    mov[13] = 400.0
    dt = [None] * SLOTS
    dt[13] = 2400.0
    est = bikes.estimate({"mov": np.array([np.nan if m is None else m
                                           for m in mov], np.float32),
                          "dt": np.array([np.nan if m is None else m
                                          for m in dt], np.float32),
                          "bucket": BUCKET}, k1)
    check("a 40 minute difference covers four slots",
          est["seen"][10:14].all() and not est["seen"][9]
          and not est["seen"][14],
          "slots %s" % np.flatnonzero(est["seen"]).tolist())
    check("...at the rate it measured, not four times it",
          abs(est["rate"][10] - 300.0) < 0.5 and
          abs(est["rate"][13] - 300.0) < 0.5, "%.1f/h" % est["rate"][10])
    check("...and counts 200 trips exactly once",
          abs(float(est["trips"].sum()) - 200.0) < 0.01,
          "%.1f" % est["trips"].sum())
    check("...over the right seconds", abs(est["secs"] - 2400.0) < 1.0)

    # A fetcher on a five minute timer covers half of each slot, and the
    # comparison has to know that: counting it as a whole slot halves the
    # headline against an archive bucket twice as long.
    mov = [None] * SLOTS
    dt = [None] * SLOTS
    for s in (30, 31):
        mov[s], dt[s] = 60.0, 300.0
    est = bikes.estimate({"mov": np.array([np.nan if m is None else m
                                           for m in mov], np.float32),
                          "dt": np.array([np.nan if m is None else m
                                          for m in dt], np.float32),
                          "bucket": BUCKET}, k1)
    check("a five minute pass covers half a slot",
          abs(float(est["cover"][30]) - 300.0) < 1.0
          and abs(est["secs"] - 600.0) < 1.0,
          "%.0f s in slot 30, %.0f s total" % (est["cover"][30], est["secs"]))
    check("...at the right rate", abs(est["rate"][30] - 360.0) < 1.0,
          "%.1f/h" % est["rate"][30])

    # The calibration is applied, and it is the hour's own.
    mov = [None] * SLOTS
    mov[102] = 100.0                       # 5pm, the busiest calibration
    est = bikes.estimate({"mov": np.array([np.nan if m is None else m
                                           for m in mov], np.float32),
                          "dt": np.full(SLOTS, BUCKET, np.float32),
                          "bucket": BUCKET},
                         np.array([k_at(s * 6) for s in range(24)], np.float32))
    want = 50.0 * k_at(102)
    check("the hour's calibration is applied",
          abs(float(est["trips"][102]) - want) < 0.5,
          "%.1f trips from 50 moves (x%.2f)" % (est["trips"][102], k_at(102)))

    # A difference that reaches back past midnight belongs to yesterday as much
    # as today, and yesterday is not on this axis.
    mov = [None] * SLOTS
    mov[1] = 400.0
    dt = [None] * SLOTS
    dt[1] = 2400.0
    est = bikes.estimate({"mov": np.array([np.nan if m is None else m
                                           for m in mov], np.float32),
                          "dt": np.array([np.nan if m is None else m
                                          for m in dt], np.float32),
                          "bucket": BUCKET}, k1)
    check("nothing is drawn before midnight",
          est["seen"][0] and est["seen"][1] and not est["seen"][2:].any(),
          "slots %s" % np.flatnonzero(est["seen"]).tolist())


# --------------------------------------------------------------------------
# 3. The drawing, in pixels.
# --------------------------------------------------------------------------

def test_pixels():
    print("\nthe picture")
    tmp = tempfile.mkdtemp(prefix="bikes-px")
    try:
        when = a_monday(hour=18)
        k_at, _ = k_of(0)
        rate = 900.0
        record(tmp, when, flat_day(rate, upto=107, k_at=k_at))
        r, f = settled(opts(cache_dir=tmp, at=str(when), reload=0, reveal=0,
                            comet=0, weekday=0))
        lay = r.layout
        scale = r.state["scale"]
        want = int(round(lay.chart_bot - rate / scale * (lay.chart_h - 1)))
        rows = gold_rows(f, lay)
        drawn = rows[(rows >= 0)]
        check("a flat day draws a flat line", len(set(drawn.tolist())) <= 2,
              "rows %s" % sorted(set(drawn.tolist())))
        check("...at the row the arithmetic says",
              abs(int(np.median(drawn)) - want) <= 1,
              "row %d, wanted %d, %.0f/h of %.0f full scale"
              % (np.median(drawn), want, rate, scale))
        # The trace stops where the data stops. Six in the evening is column
        # 240 of 320, so a line running to the right edge is the whole failure
        # this panel exists to avoid.
        last = int(np.flatnonzero(rows >= 0)[-1])
        check("the line stops where the data stops",
              235 <= last <= 244, "last gold column %d" % last)
        check("nothing is drawn after it", (rows[last + 4:] < 0).all())

        # The silhouette is under the line where today is busier and over it
        # where today is quieter, which is the entire message of the panel.
        sil = f[lay.chart_y:lay.chart_bot + 1, 100].astype(int)
        blue = np.flatnonzero((sil[:, 2] > sil[:, 0] + 8) & (sil[:, 2] > 20))
        check("the typical day is drawn as a filled shape", len(blue) > 5,
              "%d blue rows in column 100" % len(blue))

        # ...and the whole point: a busier day sits above it.
        record(tmp, when, flat_day(2400.0, upto=107, k_at=k_at))
        r2, f2 = settled(opts(cache_dir=tmp, at=str(when), reload=0, reveal=0,
                              comet=0, weekday=0))
        hi_rows = gold_rows(f2, r2.layout)
        check("a busier day draws higher up the panel",
              int(np.median(hi_rows[hi_rows >= 0]))
              < int(np.median(drawn)) - 5,
              "row %d against %d" % (np.median(hi_rows[hi_rows >= 0]),
                                     np.median(drawn)))
        check("...and says so", contains_text(f2, "BUSIER THAN USUAL"))

        record(tmp, when, flat_day(180.0, upto=107, k_at=k_at))
        _r3, f3 = settled(opts(cache_dir=tmp, at=str(when), reload=0,
                               reveal=0, comet=0, weekday=0))
        check("a quiet day says so", contains_text(f3, "QUIETER THAN USUAL"))

        # An ordinary day gets the ordinary verdict. The estimator's own
        # uncertainty is bigger than the day-to-day spread of the real thing,
        # so this is the answer on almost every real day and it has to be
        # right; a panel that cries BUSIER every afternoon is noise.
        z, _ = bikes.read_typical()
        med = np.median(z["t0"], axis=0) * 6.0
        mov = [None] * SLOTS
        for s in range(108):
            mov[s] = 2.0 * float(med[s]) * (BUCKET / 3600.0) / k_at(s)
        record(tmp, when, mov)
        _r4, f4 = settled(opts(cache_dir=tmp, at=str(when), reload=0,
                               reveal=0, comet=0, weekday=0))
        check("a median day is called usual",
              contains_text(f4, "USUAL FOR A MONDAY")
              or contains_text(f4, "USUAL FOR A MON"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_weekday():
    """Saturday's shape must not be drawn under a Tuesday's clock."""
    print("\nthe right weekday")
    tmp = tempfile.mkdtemp(prefix="bikes-wd")
    try:
        when = a_monday(hour=23, minute=50)
        record(tmp, when, [None] * SLOTS)
        shapes = {}
        for wd in (0, 5):
            r, f = settled(opts(cache_dir=tmp, at=str(when), reload=0,
                                reveal=0, comet=0, weekday=wd))
            lay = r.layout
            reg = f[lay.chart_y:lay.chart_bot + 1].astype(int)
            blue = (reg[:, :, 2] > reg[:, :, 0] + 8) & (reg[:, :, 2] > 20)
            shapes[wd] = blue.sum(axis=0)
        am = slice(100, 125)            # 7:30 to 9:20
        noon = slice(160, 185)
        check("the weekday silhouette has a morning peak",
              shapes[0][am].mean() > shapes[0][noon].mean() * 1.3,
              "%.1f rows at 8am against %.1f at noon"
              % (shapes[0][am].mean(), shapes[0][noon].mean()))
        check("the weekend one does not",
              shapes[5][am].mean() < shapes[5][noon].mean(),
              "%.1f rows at 8am against %.1f at noon"
              % (shapes[5][am].mean(), shapes[5][noon].mean()))
        # Not in the header -- see bake_header on why the weekday is not there
        # -- but somewhere a viewer can read it, which is the line saying what
        # the shaded shape is.
        check("the panel names the day it is comparing against", contains_text(
            settled(opts(cache_dir=tmp, at=str(when), reload=0, reveal=0,
                         comet=0, weekday=5))[1], "SATURDAY"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_now():
    print("\nnow, and the axis")
    tmp = tempfile.mkdtemp(prefix="bikes-now")
    try:
        when = a_monday(hour=9)
        record(tmp, when, [None] * SLOTS)
        r, f = settled(opts(cache_dir=tmp, at=str(when), reload=0, reveal=0,
                            comet=0, weekday=0))
        col = r.state["now_col"]
        check("the now rule is where nine in the morning is",
              abs(col - int(9 / 24.0 * 320)) <= 1, "column %d" % col)
        lay = r.layout
        strip = f[lay.chart_y:lay.chart_bot + 1, col].astype(int)
        check("...and is drawn", (strip.max(axis=1) > 60).sum() > 5,
              "%d lit rows" % (strip.max(axis=1) > 60).sum())
        check("...and is labelled", contains_text(f, "NOW"))
        check("the axis is labelled in hours",
              contains_text(f, "12A") and contains_text(f, "6A")
              and contains_text(f, "12P"))
        check("the scale is labelled per hour",
              any(contains_text(f, "%d/H" % v)
                  for v in range(250, 4001, 250)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. Honesty: the words, the cold start, and the wrong day.
# --------------------------------------------------------------------------

def test_words():
    print("\nwhat the panel says about its own number")
    tmp = tempfile.mkdtemp(prefix="bikes-say")
    try:
        when = a_monday(hour=16)
        k_at, _ = k_of(0)
        record(tmp, when, flat_day(700.0, upto=95, k_at=k_at))
        _r, f = settled(opts(cache_dir=tmp, at=str(when), reload=0, reveal=0,
                             comet=0, weekday=0))
        check("the number is called an estimate",
              contains_text(f, "EST FROM DOCK COUNTS")
              or contains_text(f, "FROM DOCK COUNTS"))
        check("the calibration factor is printed",
              any(contains_text(f, "X%.1f" % (v / 10.0))
                  for v in range(10, 31)))
        check("the shading is explained", contains_text(f, "SHADED"))
        check("the headline names what it counts",
              contains_text(f, "TRIPS TODAY"))
        # There is deliberately no percentage difference on this panel; see the
        # module docstring for why. It is not asserted here, because a single
        # 3x5 glyph matches somewhere on almost any frame and a check that
        # cannot fail is worse than no check. The absence is enforced by there
        # being no string in strip_lines() that formats one.
        src = open(os.path.join(HERE, "bikes.py")).read()
        check("no percentage is formatted anywhere",
              "%%" not in src.split("def bake_typical")[0]
              .split("Options.")[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cold_start():
    print("\ncold start")
    tmp = tempfile.mkdtemp(prefix="bikes-cold")
    try:
        when = a_monday(hour=15, minute=10)
        k_at, _ = k_of(0)
        # The fetcher started at 2:40 this afternoon. Eighty-eight slots of
        # this day were never looked at and must not be drawn as zero.
        record(tmp, when, flat_day(700.0, upto=90, start=88, k_at=k_at))
        r, f = settled(opts(cache_dir=tmp, at=str(when), reload=0, reveal=0,
                            comet=0, weekday=0))
        rows = gold_rows(f, r.layout)
        first = int(np.flatnonzero(rows >= 0)[0])
        check("the line starts where the fetcher did",
              193 <= first <= 200, "first gold column %d" % first)
        check("nothing is drawn before it", (rows[:first - 2] < 0).all())
        check("the headline says when it starts from",
              contains_text(f, "TRIPS SINCE 2:40P"))
        check("no verdict is offered on half an hour",
              contains_text(f, "TOO EARLY TO SAY"))

        # A record with no differences at all: one fetch has landed and the
        # second has not. There is a whole day of silhouette to draw and no
        # line, and the panel has to say which.
        record(tmp, when, [None] * SLOTS)
        _r2, f2 = settled(opts(cache_dir=tmp, at=str(when), reload=0,
                               reveal=0, comet=0, weekday=0))
        check("one fetch draws no line",
              (gold_rows(f2, r.layout) < 0).all())
        check("...and says it is waiting",
              contains_text(f2, "WAITING")
              or contains_text(f2, "NEEDS TWO FETCHES TEN MINUTES APART"))

        # A hole in the middle is a hole in the line, never a bridge.
        mov = flat_day(700.0, upto=90, k_at=k_at)
        for s in range(40, 60):
            mov[s] = None
        record(tmp, when, mov)
        _r3, f3 = settled(opts(cache_dir=tmp, at=str(when), reload=0,
                               reveal=0, comet=0, weekday=0))
        rows = gold_rows(f3, r.layout)
        gap = rows[int(45 / 144.0 * 320) + 2:int(58 / 144.0 * 320)]
        check("a missed hour is a gap in the line", (gap < 0).all(),
              "%d columns still drawn" % (gap >= 0).sum())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wrong_day():
    print("\nthe wrong day, and other refusals")
    tmp = tempfile.mkdtemp(prefix="bikes-bad")
    try:
        when = a_monday(hour=14)
        k_at, _ = k_of(0)
        record(tmp, when, flat_day(700.0, upto=84, k_at=k_at), day_offset=-1)
        r, f = settled(opts(cache_dir=tmp, at=str(when), reload=0, reveal=0,
                            comet=0, weekday=0))
        check("yesterday's slots draw no line", (gold_rows(f, r.layout) < 0).all())
        check("...and the panel says which day it has",
              contains_text(f, "LAST DATA"))
        check("...and still draws the typical day",
              f[r.layout.chart_y:r.layout.chart_bot].max() > 20)

        for name, kw in (("no today block", {"drop_today": True}),
                         ("short today block",
                          {"mangle": lambda p: p["today"].update(
                              {"mov": p["today"]["mov"][:20]})}),
                         ("today is not a dict",
                          {"mangle": lambda p: p.update({"today": 7})}),
                         ("day0 is a string",
                          {"mangle": lambda p: p["today"].update(
                              {"day0": "yesterday"})})):
            record(tmp, when, [None] * SLOTS, **kw)
            try:
                _r2, f2 = frames(opts(cache_dir=tmp, at=str(when), reload=0,
                                      weekday=0), 30)
                ok = f2.shape == (64, 320, 3) and f2.max() > 0
                detail = "drew a panel"
            except Exception as exc:                         # noqa: BLE001
                ok, detail = False, repr(exc)[:70]
            check("%s still draws" % name, ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_purity_and_motion():
    print("\npurity and motion")
    tmp = tempfile.mkdtemp(prefix="bikes-pure")
    try:
        when = a_monday(hour=17)
        k_at, _ = k_of(0)
        record(tmp, when, flat_day(900.0, upto=101, k_at=k_at))
        args = opts(cache_dir=tmp, at=str(when), rate=0.0, reload=0, weekday=0)

        # Driven frame by frame from zero, against a cold build sampled at the
        # same t. With --reload 0 and a frozen clock the only input left is t,
        # and if they differ the demo is accumulating state between calls and
        # will desync from a scheduler that builds segments ahead.
        t0 = 4.35
        a = frames(args, int(t0 * 20) + 1)[1]
        r = bikes.build(args)
        b = r(t0, int(t0 * 20)).copy()
        check("render is a pure function of t", np.array_equal(a, b),
              "%d pixels differ" % int((a != b).any(axis=2).sum()))

        # ...and it must never hold a frame. A still panel between two animated
        # demos reads as a crash.
        r = bikes.build(args)
        seen = [r(i / 20.0, i).copy() for i in range(80)]
        same = sum(1 for i in range(1, len(seen))
                   if np.array_equal(seen[i], seen[i - 1]))
        check("no two consecutive frames are identical", same == 0,
              "%d repeats in 80 frames" % same)
        moved = sum(1 for i in range(1, len(seen))
                    if not np.array_equal(seen[i], seen[i - 1]))
        check("the panel is animated throughout", moved == len(seen) - 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_timing():
    print("\nframe cost")
    tmp = tempfile.mkdtemp(prefix="bikes-ms")
    try:
        when = a_monday(hour=20)
        k_at, _ = k_of(0)
        record(tmp, when, flat_day(900.0, upto=119, k_at=k_at))
        args = opts(cache_dir=tmp, at=str(when), reload=0, weekday=0)
        t0 = time.perf_counter()
        r = bikes.build(args)
        build_ms = (time.perf_counter() - t0) * 1000.0
        for i in range(40):
            r(i / 20.0, i)
        ms = []
        for i in range(700):
            t0 = time.perf_counter()
            r(i / 20.0, i)
            ms.append((time.perf_counter() - t0) * 1000.0)
        ms = np.array(ms)
        check("mean under 0.20 ms here", ms.mean() < 0.20,
              "mean %.3f p95 %.3f max %.3f ms, build %.1f ms"
              % (ms.mean(), np.percentile(ms, 95), ms.max(), build_ms))
        check("p95 is near the mean", np.percentile(ms, 95) < ms.mean() * 3.5,
              "%.3f against %.3f" % (np.percentile(ms, 95), ms.mean()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="bikes-size")
    try:
        when = a_monday(hour=19)
        k_at, _ = k_of(0)
        record(tmp, when, flat_day(900.0, upto=113, k_at=k_at))
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, f = frames(opts(cache_dir=tmp, at=str(when), reload=0,
                                   weekday=0, width=w, height=h), 60)
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                lay = r.layout
                detail = "chart %d rows, strip %d, axis %d" % (
                    lay.chart_h, lay.strip_h, lay.axis_h)
            except Exception as exc:                         # noqa: BLE001
                ok, detail = False, repr(exc)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 5. The three data states, each in its own process.
# --------------------------------------------------------------------------

def _one_state(state, cache_dir):
    when = a_monday(hour=16)
    k_at, _ = k_of(0)
    tmp = tempfile.mkdtemp(prefix="bikes-state")
    try:
        if state == "fresh":
            record(tmp, when, flat_day(700.0, upto=95, k_at=k_at))
        elif state == "stale":
            record(tmp, when, flat_day(700.0, upto=71, k_at=k_at),
                   fetched_ago=3.5 * 3600.0)
        # absent: nothing written at all
        _r, f = settled(opts(cache_dir=tmp, at=str(when), reload=0, reveal=0,
                             comet=0, weekday=0))
        words = {
            "fresh": ["TRIPS TODAY", "EST FROM DOCK COUNTS", "SHADED"],
            "stale": ["STALE", "SHADED"],
            "absent": ["NO LIVE DATA", "SHADED"],
        }[state]
        got = [w for w in words if contains_text(f, w)]
        print("RESULT %s %d/%d %s lit=%d"
              % (state, len(got), len(words), ",".join(got),
                 int((f.max(axis=2) > 8).sum())))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_states_in_separate_processes():
    print("\nthe three data states, one process each")
    for state in ("fresh", "stale", "absent"):
        env = dict(os.environ, FT_DATA_CACHE=tempfile.mkdtemp(prefix="ftc"))
        out = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--state", state],
            capture_output=True, text=True, env=env).stdout
        line = [x for x in out.splitlines() if x.startswith("RESULT")]
        if not line:
            check("%s state" % state, False, out.strip()[-90:])
            continue
        _tag, _s, ratio, said, lit = (line[0].split() + [""])[:5]
        got, want = (int(v) for v in ratio.split("/"))
        check("%s state says the right things" % state, got == want,
              "%s  %s" % (said, lit))


# --------------------------------------------------------------------------
# 6. The live cache, and the promise that none of this talks to anyone.
# --------------------------------------------------------------------------

def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("baywheels", tempfile.mkdtemp(prefix="bikes-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "bikes.py")).read()
    # urllib appears in this file, inside _fetch_month, which is the offline
    # bake and is imported there rather than at the top precisely so that
    # importing the demo cannot reach a socket.
    top = src.split("def add_arguments")[0]
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("\nimport " + m) in top]
    check("bikes.py imports none at module level", not imported,
          ",".join(imported))
    check("the bake imports urllib inside itself",
          "    import urllib.request" in src)


def test_live(cache_dir):
    print("\nthe live cache: %s" % cache_dir)
    got = ftdata.load("baywheels", cache_dir)
    if got is None:
        check("a baywheels record exists", False,
              "run: python3 ftdata.py --once --only baywheels")
        return
    payload, age = got
    day = payload.get("today")
    check("the record carries day slots", isinstance(day, dict),
          "fetcher is out of date" if not isinstance(day, dict) else
          "%d slots, %s old" % (day.get("n", 0), ftdata.describe_age(age)))
    if not isinstance(day, dict):
        return
    filled = [i for i, m in enumerate(day["mov"]) if m is not None]
    check("the day has at least one difference in it", bool(filled),
          "slots %s" % (filled[:6] + (["..."] if len(filled) > 6 else [])))

    r, f = settled(opts(cache_dir=cache_dir, reload=0))
    check("the live record draws", f.max() > 0 and f.shape == (64, 320, 3))
    check("...with the archive under it",
          f[r.layout.chart_y:r.layout.chart_bot].max() > 20)
    if filled:
        # The only end-to-end check on the calibration that can be made without
        # months of paired snapshots: the live estimate for a covered hour
        # ought to land within a factor of two of what that hour normally does.
        # Wider than the calibration's own spread on purpose -- this is looking
        # for a factor of ten, not a few per cent.
        est = r.state.get("est")
        z = r.typical
        wd = r.state["wd"]
        med = np.median(z["t%d" % wd], axis=0)
        sel = est["seen"]
        frac = est["cover"] / est["bucket"]
        mine = float(est["trips"][sel].sum())
        theirs = float((med * frac)[sel].sum())
        ratio = mine / theirs if theirs > 0 else 0.0
        check("the live estimate is the same size as the archive",
              0.4 <= ratio <= 2.5,
              "%.0f estimated against %.0f typical over %d slots (x%.2f)"
              % (mine, theirs, int(sel.sum()), ratio))


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
    test_asset()
    test_estimate()
    test_pixels()
    test_weekday()
    test_now()
    test_words()
    test_cold_start()
    test_wrong_day()
    test_purity_and_motion()
    test_timing()
    test_states_in_separate_processes()
    test_sizes()
    test_live(a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
