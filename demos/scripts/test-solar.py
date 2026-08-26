#!/usr/bin/env python3
"""Checks for solar.py that a screenshot cannot make.

This panel can draw a confident, pretty, wrong picture in at least five ways,
and not one of them looks wrong:

  1. **The terrain can be the wrong series.** A hill is a hill. Drawn from the
     charge current instead of the voltage it is exactly as handsome and it is
     a different day, and the two are correlated enough that eyeballing it
     would never catch the swap.
  2. **The sky can follow the data instead of the sun.** The whole point of the
     panel is that the blue comes from astronomy and the warm glow comes from
     the shunt, so that a foggy noon looks like a bright sky over a dark ridge.
     If both came from the same place that case would be invisible -- and it is
     the case the panel exists for.
  3. **The time-of-day axis can slip.** Binning 288 buckets by age rather than
     by local time of day produces a chart that is right on average and puts
     noon in the wrong column, which nobody would notice from three metres.
  4. **Today and yesterday can be the wrong way round.** The columns past the
     cursor are yesterday's tail. Dimming the other side is a plausible-looking
     panel that says the opposite thing about which half already happened.
  5. **The battery can be full of the wrong number.** A gradient fill in a case
     always looks like a battery.

So the drawing is asserted **in pixels** against synthetic days whose answers
cannot be argued with -- a voltage hump at a known hour, a charge hump at a
different one, a fog day with sun and no current -- and the arithmetic is
asserted against the real fetched record separately.

Two things about how these are run, both learned in this tree. The demo *is* a
pure function of `t` once `--reload` is off, and that is asserted here rather
than assumed. And `ftdata.CACHE_DIR` binds at import, so the states a demo must
handle -- fresh, stale, silent, absent -- are each run in a **separate process**
with FT_DATA_CACHE set, at the bottom of this file.

    $ python3 scripts/test-solar.py                     # uses the live cache
    $ python3 scripts/test-solar.py --cache-dir /tmp/c  # or a pointed one

The live cache is only needed for the checks against real data; everything else
builds its own cache directory and needs nothing. Populate it with
`python3 ftdata.py --once --only solar-garden`.
"""

import argparse
import json
import math
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
import ftsite                                                 # noqa: E402
import solar                                                  # noqa: E402

FAILED = []
PASSED = [0]

STEP = 300.0
BUCKETS = 288


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    kw.setdefault("reload", 0.0)
    return ds.options(solar, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build. Never sample sparsely."""
    r = solar.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def settled(args, n=220):
    """A frame from after the reveal has finished, still rendered in order."""
    return frames(args, n)


def contains_text(frame, s, thresh=90, scales=(1, 2), bg_max=0.25):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure the honest
    message reached it rather than merely being computed. The counters between
    the strokes have to be dark too: this panel has a lit sky across most of its
    width, so a matcher that only asks "are the strokes on" answers yes to
    every string in the language somewhere inside a blue afternoon.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = solar.text_mask(s, scale)
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


# --------------------------------------------------------------------------
# A synthetic day, with the answers written down.
#
# The two humps are deliberately at *different* hours, which is the only way a
# terrain drawn from the wrong series can be caught: the voltage peaks at 13:00
# and the charge current at 10:00, so a ridge whose summit is at column 120
# is being drawn from the current and a ridge whose summit is at 156 is being
# drawn from the voltage, and there is no way to confuse the two.
# --------------------------------------------------------------------------

V_PEAK_H = 13.0
I_PEAK_H = 10.0


def _hump(hour, centre, width, height, base):
    return base + height * math.exp(-((hour - centre) / width) ** 2)


def synthetic(cache_dir, latest_local_h=17.0, fetched_ago=120.0, soc=99.8,
              v_amp=1.0, i_amp=300.0, sensor_stale=False, n=BUCKETS,
              day=None, drop_slots=()):
    """Write a record whose voltage and current peaks are at known hours.

    `latest_local_h` is the local hour of the newest bucket, so the cursor's
    column is arithmetic and not a guess.
    """
    os.makedirs(cache_dir, exist_ok=True)
    base_day = time.time() if day is None else day
    mid = solar.local_midnight(base_day)
    latest = mid + latest_local_h * 3600.0
    latest -= latest % STEP
    t0 = latest - (n - 1) * STEP

    volt, cur, socs = [], [], []
    for i in range(n):
        e = t0 + i * STEP
        lt = time.localtime(e)
        hour = lt.tm_hour + lt.tm_min / 60.0
        if i in drop_slots:
            volt.append(None)
            cur.append(None)
            socs.append(None)
            continue
        volt.append(round(_hump(hour, V_PEAK_H, 1.4, v_amp, 13.25), 3))
        cur.append(round(_hump(hour, I_PEAK_H, 1.6, i_amp, 5.0), 1))
        socs.append(round(soc, 2))

    payload = {
        "t0": t0, "step": STEP, "n": n,
        "volt": volt, "cur_ma": cur, "soc": socs,
        "soc_pct": round(soc), "status": "Full",
        "v": volt[-1], "i_ma": cur[-1], "load_w": 0.0, "p_in_w": 0.0,
        "cpu_c": 50.5, "cpu_load": 0.1,
        "sensor_stale": bool(sensor_stale),
        "sensor_age_s": 5400.0 if sensor_stale else 240.0,
        "uptime": "190d 20h 5m", "up_days": 190,
        "local_time": "", "avg_volt": 13.3, "avg_cur_ma": 30.0,
        "avg_soc": soc, "site": "sequoia.garden",
    }
    rec = {"name": solar.PRODUCT, "fetched_at": time.time() - fetched_ago,
           "source": "synthetic", "ttl": ftdata.ttl_for(solar.PRODUCT),
           "payload": payload}
    with open(os.path.join(cache_dir, solar.PRODUCT + ".json"), "w") as fh:
        json.dump(rec, fh)
    return latest, t0


RIDGE_MIN = 120


def ridge_rows(frame, lay):
    """The row of the ridge line in each day column, read off the pixels.

    Scanned **from the bottom up**, looking for the first row brighter than
    RIDGE_MIN. That works because of two facts about the palettes rather than
    by luck: every ground colour tops out under 100 in its brightest channel,
    and every ridge colour is over 230 in one. Scanning downwards instead would
    find the charge glow first, which reaches 225 in red just above the ridge on
    a sunny noon -- and a check that mistook the glow for the terrain would pass
    happily while the terrain was drawn from the wrong series, which is the one
    thing this file exists to catch.

    Callers must render with `sweep=0` and past the reveal, and must not ask
    about columns past the cursor: the sweep lifts the ground towards white and
    yesterday's dim takes the ridge under the threshold. Both are checked
    elsewhere; here they would only be noise.
    """
    out = np.full(lay.day_w, frame.shape[0], np.int32)
    bright = frame[:, :lay.day_w].max(axis=2) >= RIDGE_MIN
    for c in range(lay.day_w):
        rows = np.flatnonzero(bright[:, c])
        rows = rows[rows > lay.head_h + 6]
        if len(rows):
            out[c] = rows[-1]
    return out


# --------------------------------------------------------------------------
# 1. The terrain is the voltage, and the sky is not.
# --------------------------------------------------------------------------

def test_terrain_is_voltage():
    print("\nthe ridge is the voltage curve, at the hour the voltage peaks")
    tmp = tempfile.mkdtemp(prefix="solar-terr")
    try:
        # Newest sample at 23:55, so the whole synthetic day is "today" and
        # nothing is dimmed -- this check is about geometry, not brightness.
        synthetic(tmp, latest_local_h=23.9)
        r, f = settled(opts(cache_dir=tmp, sweep=0.0))
        lay = r.layout
        rows = ridge_rows(f, lay)
        summit = int(np.argmin(rows))
        want_v = int(V_PEAK_H / 24.0 * lay.day_w)
        want_i = int(I_PEAK_H / 24.0 * lay.day_w)
        check("the summit is at the voltage peak's column",
              abs(summit - want_v) <= 3,
              "summit col %d, voltage peak %d, current peak %d"
              % (summit, want_v, want_i))
        check("...and is nowhere near the current peak's column",
              abs(summit - want_i) > 20, "%d columns apart"
              % abs(summit - want_i))

        # Midnight is the flat part of both humps; it must be the low ground.
        check("midnight is the lowest ground on the panel",
              rows[2] >= rows[summit] + 8,
              "midnight row %d, summit row %d" % (rows[2], rows[summit]))

        # And the ridge has to actually move: a constant voltage that gets
        # auto-scaled into a mountain range would pass every check above.
        flat = os.path.join(tmp, "flat")
        synthetic(flat, latest_local_h=23.9, v_amp=0.0)
        r2, f2 = settled(opts(cache_dir=flat, sweep=0.0))
        # Up to, but not including, the cursor: the cursor column is painted
        # over with the cursor and the one beside it with its halo, so neither
        # has a ridge to read.
        rows2 = ridge_rows(f2, r2.layout)[:r2.state["now_col"] - 1]
        check("a day with no voltage variation draws no hill",
              int(rows2.max() - rows2.min()) <= 1,
              "ridge spans %d rows" % int(rows2.max() - rows2.min()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sky_is_the_sun_and_the_glow_is_the_shunt():
    print("\nthe blue is astronomy and the warm glow is the shunt")
    tmp = tempfile.mkdtemp(prefix="solar-sky")
    try:
        sunny = os.path.join(tmp, "sunny")
        foggy = os.path.join(tmp, "foggy")
        synthetic(sunny, latest_local_h=23.9, i_amp=300.0)
        synthetic(foggy, latest_local_h=23.9, i_amp=0.0)

        out = {}
        for name, d in (("sunny", sunny), ("foggy", foggy)):
            r, f = settled(opts(cache_dir=d, sweep=0.0))
            lay = r.layout
            rows = ridge_rows(f, lay)
            col = int(I_PEAK_H / 24.0 * lay.day_w)
            # The four rows immediately above that column's own ridge, which is
            # where the glow lives.
            y = max(0, rows[col] - 4)
            band = f[y:rows[col], col].astype(np.int32)
            sky = f[lay.head_h + 2, col].astype(np.int32)
            out[name] = (band.mean(axis=0), sky)

        check("with current flowing, the ridge glows warm",
              out["sunny"][0][0] > out["sunny"][0][2],
              "R %.0f vs B %.0f" % (out["sunny"][0][0], out["sunny"][0][2]))
        check("with no current, the same hour's ridge does not",
              out["foggy"][0][0] < out["foggy"][0][2],
              "R %.0f vs B %.0f" % (out["foggy"][0][0], out["foggy"][0][2]))
        check("...but the sky above it is just as blue, because the sun was up",
              abs(int(out["sunny"][1][2]) - int(out["foggy"][1][2])) <= 2,
              "sunny B %d, foggy B %d"
              % (out["sunny"][1][2], out["foggy"][1][2]))

        # Midnight must be dark whatever the shunt says, or the sky is being
        # driven by the data rather than by the sun.
        r, f = settled(opts(cache_dir=sunny, sweep=0.0))
        top = f[r.layout.head_h + 2]
        noon = int(top[r.layout.day_w // 2].max())
        night = int(top[2].max())
        check("the top of the sky is darker at midnight than at noon",
              night < noon, "midnight %d, noon %d" % (night, noon))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2. x is local time of day, and the split is today / yesterday.
# --------------------------------------------------------------------------

def test_time_of_day_axis():
    print("\nx is local time of day, and the cursor is the data's edge")
    tmp = tempfile.mkdtemp(prefix="solar-axis")
    try:
        for hour in (3.0, 10.0, 17.5, 22.0):
            d = os.path.join(tmp, "h%02d" % int(hour))
            latest, _ = synthetic(d, latest_local_h=hour)
            r, f = settled(opts(cache_dir=d, sweep=0.0))
            lay = r.layout
            want = solar.slot_of(latest) * lay.day_w // solar.SLOTS
            check("newest sample at %04.1fh puts the cursor at its column"
                  % hour, abs(r.state["now_col"] - want) <= 1,
                  "cursor col %d, wanted %d" % (r.state["now_col"], want))

        # Yesterday's tail is dimmer than today. Compared at two columns whose
        # underlying data is identical by construction -- the humps are a
        # function of the hour, so an hour before the cursor and the same hour
        # a day earlier are the same numbers, and any difference in the pixels
        # is the dim and nothing else.
        d = os.path.join(tmp, "split")
        synthetic(d, latest_local_h=12.0)
        r, f = settled(opts(cache_dir=d, sweep=0.0, reveal=0.0))
        lay = r.layout
        col = r.state["now_col"]
        left = f[:, max(0, col - 30):col - 2].astype(np.int32).sum()
        right = f[:, col + 3:min(lay.day_w, col + 31)].astype(np.int32).sum()
        check("the columns past the cursor are drawn dimmer than today's",
              right < left * 0.75, "today %d vs yesterday %d" % (left, right))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 3. The battery says what the record says.
# --------------------------------------------------------------------------

def _fill_rows(frame, lay):
    """How many rows of the battery well are lit."""
    x0, x1, y0, y1 = solar.battery_rect(lay)
    col = frame[y0 + 2:y1 - 1, (x0 + x1) // 2].astype(np.int32)
    return int((col.max(axis=1) > 24).sum())


def test_battery():
    print("\nthe battery is the state of charge, and the reserve mark is fixed")
    tmp = tempfile.mkdtemp(prefix="solar-batt")
    try:
        # Newest sample just before midnight, so no column is dimmed as
        # yesterday and the ridge colour can be read straight off the pixels.
        synthetic(tmp, latest_local_h=23.9)
        heights = {}
        for soc in (100.0, 75.0, 50.0, 25.0, 4.0):
            r, f = settled(opts(cache_dir=tmp, soc=soc, sweep=0.0))
            heights[soc] = _fill_rows(f, r.layout)
        order = [heights[s] for s in (100.0, 75.0, 50.0, 25.0, 4.0)]
        check("the fill falls monotonically with the state of charge",
              all(order[i] > order[i + 1] for i in range(len(order) - 1)),
              " > ".join(str(v) for v in order))
        check("...and is proportional, not merely ordered",
              abs(heights[50.0] * 2 - heights[100.0]) <= 3,
              "50%% is %d rows, 100%% is %d" % (heights[50.0], heights[100.0]))

        # The number, read back off the panel.
        for soc, s in ((100.0, "100"), (24.0, "24"), (7.0, "7")):
            r, f = settled(opts(cache_dir=tmp, soc=soc, sweep=0.0))
            check("%g%% prints %s on the panel" % (soc, s),
                  contains_text(f, s), "")

        # Health steps. Read the ridge colour, which is what carries it across
        # the room; the number is only legible up close.
        seen = {}
        for soc, want in ((90.0, solar.C_RIDGE_OK), (40.0, solar.C_RIDGE_WARN),
                          (12.0, solar.C_RIDGE_LOW)):
            r, f = settled(opts(cache_dir=tmp, soc=soc, sweep=0.0,
                                reveal=0.0))
            rows = ridge_rows(f, r.layout)
            # Three in the morning: flat ground, no glow over it, and a long
            # way from the cursor at 23:55.
            c = r.layout.day_w // 8
            seen[soc] = tuple(int(v) for v in f[rows[c], c])
            check("soc %g draws the %s ridge" % (soc, {
                solar.C_RIDGE_OK: "healthy", solar.C_RIDGE_WARN: "warning",
                solar.C_RIDGE_LOW: "low"}[want]),
                seen[soc] == want, "%s wanted %s" % (seen[soc], want))
        check("the three health colours are three different colours",
              len(set(seen.values())) == 3, str(sorted(seen.values())))

        # A simulated state of charge must say so on the panel, always. A
        # screenshot of --soc 24 that did not would be a photograph of a
        # disaster that never happened.
        r, f = settled(opts(cache_dir=tmp, soc=24.0, sweep=0.0))
        check("a simulated state of charge stamps SIM on the panel",
              contains_text(f, "SIM") and r.state["sim"])
        r, f = settled(opts(cache_dir=tmp, sweep=0.0))
        check("...and a real one does not", not contains_text(f, "SIM")
              and not r.state["sim"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. Purity, and that something moves.
# --------------------------------------------------------------------------

def test_purity():
    print("\nrender() is a pure function of t")
    tmp = tempfile.mkdtemp(prefix="solar-pure")
    try:
        synthetic(tmp, latest_local_h=14.0)
        for state in ({}, {"soc": 18.0}, {"off": True}):
            for n in (1, 33, 141, 401):
                a = frames(opts(cache_dir=tmp, **state), n)[1]
                r = solar.build(opts(cache_dir=tmp, **state))
                b = r((n - 1) / 20.0, n - 1).copy()
                check("cold render at t=%.2f matches driving from zero %s"
                      % ((n - 1) / 20.0, state or "{}"),
                      np.array_equal(a, b),
                      "" if np.array_equal(a, b)
                      else "%d pixels differ" % int((a != b).any(2).sum()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_motion():
    print("\nsomething moves in every frame")
    tmp = tempfile.mkdtemp(prefix="solar-move")
    try:
        synthetic(tmp, latest_local_h=14.0)
        for label, kw in (("the chart", {}), ("the silent card", {"off": True})):
            r = solar.build(opts(cache_dir=tmp, **kw))
            prev = None
            still = 0
            for i in range(200):
                f = r(i / 20.0, i).copy()
                if prev is not None and np.array_equal(prev, f):
                    still += 1
                prev = f
            check("%s never holds a frame" % label, still == 0,
                  "%d of 199 frames identical to the one before" % still)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 5. Degraded records.
# --------------------------------------------------------------------------

def test_degraded():
    print("\nrecords that are old, holed, short or absent")
    tmp = tempfile.mkdtemp(prefix="solar-bad")
    try:
        ttl = ftdata.ttl_for(solar.PRODUCT)

        stale = os.path.join(tmp, "stale")
        synthetic(stale, latest_local_h=14.0, fetched_ago=ttl * 1.6)
        r, f = settled(opts(cache_dir=stale, sweep=0.0))
        check("a record past its TTL still draws the day",
              r.state["rec"] is not None and not r.state["silent"])
        check("...and says STALE on it",
              contains_text(f, "STALE") and r.state["stale"])

        silent = os.path.join(tmp, "silent")
        synthetic(silent, latest_local_h=14.0,
                  fetched_ago=ttl * solar.SILENT_TTL_MULT + 600.0)
        r, f = settled(opts(cache_dir=silent))
        check("a record nobody has refreshed for hours stops pretending",
              r.state["silent"] and contains_text(f, "NO ANSWER"))
        check("...and quotes the site's own warning back at it",
              contains_text(f, "IT DID SAY IT MIGHT"))
        check("...and says how long it has been",
              contains_text(f, "LAST SPOKE"))
        check("...but does not claim the site is down",
              not contains_text(f, "OFFLINE")
              and not contains_text(f, "DOWN"))

        quiet = os.path.join(tmp, "quiet")
        synthetic(quiet, latest_local_h=14.0, sensor_stale=True)
        r, f = settled(opts(cache_dir=quiet, sweep=0.0))
        check("the site's own stale-sensor flag draws the day and says QUIET",
              r.state["quiet"] and not r.state["silent"]
              and contains_text(f, "QUIET"))

        holed = os.path.join(tmp, "holed")
        synthetic(holed, latest_local_h=14.0,
                  drop_slots=tuple(range(60, 96)))
        r, f = settled(opts(cache_dir=holed, sweep=0.0))
        check("three hours the sensor never sent draw as a gap, not a line",
              r.state["rec"] is not None
              and int((f[-1, :r.layout.day_w, 0]
                       > f[-1, :r.layout.day_w, 2]).sum()) >= 20,
              "%d scarred columns"
              % int((f[-1, :r.layout.day_w, 0]
                     > f[-1, :r.layout.day_w, 2]).sum()))

        thin = os.path.join(tmp, "thin")
        synthetic(thin, latest_local_h=14.0, n=4)
        r, f = frames(opts(cache_dir=thin), 8)
        check("a record with four samples is refused, not drawn",
              r.state["rec"] is None and contains_text(f, "NO WORD YET"),
              str(r.state["problem"])[:50])

        corrupt = os.path.join(tmp, "corrupt")
        os.makedirs(corrupt)
        with open(os.path.join(corrupt, solar.PRODUCT + ".json"), "w") as fh:
            fh.write("{not json at all")
        r, f = frames(opts(cache_dir=corrupt), 8)
        check("a corrupt record draws the no-data card",
              r.state["rec"] is None and contains_text(f, "NO WORD YET"))

        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        r, f = frames(opts(cache_dir=empty), 8)
        check("an empty cache draws the no-data card and says what to run",
              contains_text(f, "NO WORD YET")
              and contains_text(f, "SOLAR-GARDEN"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 6. Fresh, stale, silent and absent, each in a process of its own.
# --------------------------------------------------------------------------

def _one_state(state, cache_dir):
    args = opts()                       # note: no cache_dir, so CACHE_DIR wins
    r = solar.build(args)
    out = None
    for i in range(220):
        out = r(i / 20.0, i)
    card = contains_text(out, "NO WORD YET")
    quiet = contains_text(out, "NO ANSWER")
    stale = contains_text(out, "STALE")
    drew = r.state["rec"] is not None
    verdict = {
        "fresh": (drew and not card and not quiet and not stale,
                  "drew the day, no flags"),
        "stale": (drew and not card and not quiet and stale,
                  "drew the day with STALE on it"),
        "silent": (drew and not card and quiet, "drew the silent card"),
        "absent": (not drew and card, "drew the no-data card"),
    }[state]
    print("RESULT %s %s cache=%s drew=%s card=%s silent=%s stale=%s"
          % (state, "ok" if verdict[0] else "FAIL",
             os.path.basename(cache_dir), drew, card, quiet, stale))
    return 0 if verdict[0] else 1


def test_states_in_separate_processes():
    print("\nfresh, stale, silent and absent -- one process each")
    ttl = ftdata.ttl_for(solar.PRODUCT)
    tmp = tempfile.mkdtemp(prefix="solar-proc")
    try:
        dirs = {}
        for state, ago in (("fresh", 120.0), ("stale", ttl * 1.5),
                           ("silent", ttl * solar.SILENT_TTL_MULT + 600.0)):
            d = os.path.join(tmp, state)
            synthetic(d, latest_local_h=14.0, fetched_ago=ago)
            dirs[state] = d
        dirs["absent"] = os.path.join(tmp, "absent")
        os.makedirs(dirs["absent"])

        for state in ("fresh", "stale", "silent", "absent"):
            d = dirs[state]
            env = dict(os.environ, FT_DATA_CACHE=d, FT_DATA_BLOBS=d)
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--state", state, "--cache-dir", d],
                env=env, capture_output=True, text=True, timeout=180)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT")]
            check("%s cache, in its own process" % state,
                  proc.returncode == 0 and bool(line),
                  line[0][7:] if line
                  else (proc.stderr.strip().splitlines() or ["no output"])[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 7. Other sizes, the network promise, and the real record.
# --------------------------------------------------------------------------

def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="solar-size")
    try:
        synthetic(tmp, latest_local_h=14.0)
        for w, h in ((320, 64), (256, 64), (192, 64), (128, 64), (320, 32),
                     (192, 96), (512, 128), (64, 32), (320, 16)):
            try:
                r, f = frames(opts(cache_dir=tmp, width=w, height=h), 90)
                lay = r.layout
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                detail = "day %d cols, gutter %d, ridge %d..%d" % (
                    lay.day_w, lay.gut_w, lay.ridge_hi, lay.ridge_lo)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load(solar.PRODUCT, tempfile.mkdtemp(prefix="solar-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "solar.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in src]
    check("solar.py does not import one either", not imported,
          ",".join(imported))
    check("solar.py hardcodes no coordinates",
          "37.76" not in src and "-122.3" not in src
          and "ftsite.LAT" in src)


def test_live(cache_dir):
    print("\nthe real record, if there is one")
    got = ftdata.load(solar.PRODUCT, cache_dir)
    if got is None:
        print("  (no cached %s; run ftdata.py --once --only %s)"
              % (solar.PRODUCT, solar.PRODUCT))
        return
    payload, age = got
    rec, _age, problem = solar.read_garden(cache_dir)
    check("the live record parses", rec is not None, str(problem or ""))
    if rec is None:
        return
    check("it carries a day of five-minute buckets",
          rec["step"] == 300.0 and 200 <= rec["n"] <= solar.SLOTS,
          "%d buckets of %gs = %.1f hours"
          % (rec["n"], rec["step"], rec["n"] * rec["step"] / 3600.0))
    n_fin = int(np.isfinite(rec["volt"]).sum())
    check("most buckets have a voltage in them", n_fin >= rec["n"] * 0.9,
          "%d of %d" % (n_fin, rec["n"]))
    check("the voltages are plausible for a 12 V bank",
          11.0 < float(np.nanmin(rec["volt"])) < 15.5
          and float(np.nanmax(rec["volt"])) < 15.5,
          "%.2f - %.2f V" % (float(np.nanmin(rec["volt"])),
                             float(np.nanmax(rec["volt"]))))
    check("the state of charge is a percentage",
          rec["soc_pct"] is None or 0.0 <= rec["soc_pct"] <= 100.0,
          "%s%%" % rec["soc_pct"])
    size = os.path.getsize(ftdata.record_path(solar.PRODUCT, cache_dir))
    check("the record is small enough for a Pi's cache", size < 20000,
          "%d bytes, %s old" % (size, ftdata.describe_age(age)))

    r, f = settled(opts(cache_dir=cache_dir, sweep=0.0))
    check("the live record draws", f.max() > 0 and r.state["rec"] is not None,
          "soc %s, cursor col %d" % (r.state["soc"], r.state["now_col"]))
    check("...and prints the site's name", contains_text(f, "SEQUOIA.GARDEN"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--state", default="",
                    choices=("", "fresh", "stale", "silent", "absent"),
                    help="internal: run one data state and print RESULT")
    a = ap.parse_args()
    if a.state:
        return _one_state(a.state, a.cache_dir)

    print("cache: %s   site: %s (%.4f, %.4f)"
          % (a.cache_dir, ftsite.NAME, ftsite.LAT, ftsite.LON))
    test_no_network()
    test_terrain_is_voltage()
    test_sky_is_the_sun_and_the_glow_is_the_shunt()
    test_time_of_day_axis()
    test_battery()
    test_purity()
    test_motion()
    test_degraded()
    test_states_in_separate_processes()
    test_sizes()
    test_live(a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
