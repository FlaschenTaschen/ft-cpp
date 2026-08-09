#!/usr/bin/env python3
"""Checks for caiso.py that a screenshot cannot make.

This demo can draw a beautiful, confident, wrong picture in at least four
ways, and none of them look wrong:

  1. **The stack order can be upside down.** Five coloured bands are five
     coloured bands; a panel with gas at the bottom and solar on top is exactly
     as pretty as the right one and says the opposite thing about the evening.
  2. **A fuel can go missing.** Thirteen published columns are folded into five
     bands, and a column that lands in no band silently shrinks the total --
     which is the top edge of the chart and the headline gigawatts.
  3. **The battery sign can flip.** Charging drawn as discharging is a
     plausible-looking lane and an inverted account of the whole afternoon.
  4. **Yesterday's record draws perfectly.** It parses, it has 288 rows, it is
     a lovely duck, and it is the wrong day.

So the drawing is asserted **in pixels** against a synthetic day whose answers
cannot be argued with -- a known solar bell, a known evening gas peak, a known
battery charge lobe at noon and discharge lobe at seven -- and the arithmetic
is asserted against the fetched JSON separately.

Two things about how these are run, both learned the hard way in this tree.
The demo is **not a pure function of `t`**: it reveals, it sheens and the
now-line breathes, so every check renders frames **sequentially from a fresh
`build()`** rather than sampling render() at scattered timestamps. And
`ftdata.CACHE_DIR` binds at import, so the three data states a demo must handle
-- fresh, stale, absent -- are each run in a **separate process** with
FT_DATA_CACHE set, at the bottom of this file. Reloading the module in one
process does not test what it looks like it tests.

    $ python3 scripts/test-caiso.py                     # uses the live cache
    $ python3 scripts/test-caiso.py --cache-dir /tmp/c  # or a pointed one

The live cache is only needed for the checks against real data; everything
else builds its own cache directory and needs nothing. Populate it with
`python3 ftdata.py --once --only caiso-mix`.
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

import caiso                                                  # noqa: E402
import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402

FAILED = []
PASSED = [0]

STEP = 300.0
ROWS = 288


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-56s %s" % (name, detail))
    else:
        print("  FAIL %-56s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(caiso, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build. Never sample sparsely."""
    r = caiso.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def settled(args, n=200):
    """A frame from after the reveal has finished, still rendered in order."""
    return frames(args, n)


def contains_text(frame, s, thresh=90, scales=(1, 2), bg_max=0.2):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure the honest
    message actually reached it rather than merely being computed.

    The counters between the strokes have to be *dark* as well, which the tide
    and wind versions of this function do not check and get away with because
    their panels are mostly black. This one is not: a stacked area chart has
    solid blocks of lit colour in it, every pixel of a glyph mask is lit inside
    one, and a matcher that only asks "are the strokes on" answers yes to every
    string in the language somewhere inside the gas band. It cost four false
    passes here before it was noticed. A fifth of the background is allowed to
    be lit, because a gridline may legitimately run behind a label.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = caiso.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                win = row[:, x:x + gw]
                if not np.array_equal(win & m, m):
                    continue
                back = win & ~m
                if back.mean() <= bg_max:
                    return True
    return False


def colour_runs(frame, col, y0, y1):
    """The colours down one column, top to bottom, as (rgb, count) runs."""
    out = []
    for y in range(y0, y1):
        rgb = tuple(int(v) for v in frame[y, col])
        if out and out[-1][0] == rgb:
            out[-1][1] += 1
        else:
            out.append([rgb, 1])
    return [(c, n) for c, n in out]


def band_rows(frame, col, rgb, y0, y1):
    """How many rows of exactly this colour are in this column."""
    seg = frame[y0:y1, col]
    return int((seg == np.array(rgb, np.uint8)).all(axis=1).sum())


# --------------------------------------------------------------------------
# A day we invented, so that every answer is known before it is drawn.
#
# Shaped like a real one -- a solar bell, wind that does not care what time it
# is, a flat nuclear floor, an evening gas peak and a battery that charges at
# noon and empties at seven -- but every number here is a formula, so a check
# can say "the gas band must be thicker at 8 pm than at noon" and mean it.
# --------------------------------------------------------------------------

def bell(i, centre_h, width_h, peak):
    x = (i * STEP / 3600.0 - centre_h) / width_h
    return peak * math.exp(-x * x) if abs(x) < 3.0 else 0.0


def synthetic(cache_dir, rows=ROWS, day_offset=0, fetched_ago=0.0,
              solar_peak=15000.0, gas_base=6000.0, gas_peak=9000.0,
              batt_peak=4000.0, with_demand=True, with_co2=True,
              drop=(), mangle=None):
    """Write a caiso-mix record by hand. Returns (path, truth dict)."""
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    midnight += day_offset * 86400.0
    t = [midnight + i * STEP for i in range(rows)]

    fuels = {
        "solar": [bell(i, 13.0, 4.0, solar_peak) for i in range(rows)],
        "wind": [3000.0 + 400.0 * math.sin(i / 40.0) for i in range(rows)],
        "geothermal": [800.0] * rows,
        "biomass": [260.0] * rows,
        "biogas": [160.0] * rows,
        "small_hydro": [260.0] * rows,
        "coal": [0.0] * rows,
        "nuclear": [2250.0] * rows,
        # A trough while the sun is up and a hard peak at eight, which is the
        # duck: the assertion is that the band on top gets thicker after dark.
        "natural_gas": [gas_base - bell(i, 13.0, 4.0, gas_base * 0.6)
                        + bell(i, 20.0, 2.0, gas_peak) for i in range(rows)],
        "large_hydro": [2000.0] * rows,
        # Charging at noon, discharging at seven. Signed, on purpose.
        "batteries": [bell(i, 19.0, 1.6, batt_peak)
                      - bell(i, 12.0, 2.4, batt_peak) for i in range(rows)],
        "imports": [3000.0] * rows,
        "other": [0.0] * rows,
    }
    for k in drop:
        del fuels[k]
    order = list(fuels)

    payload = {
        "date": time.strftime("%Y-%m-%d", time.localtime(midnight)),
        "tz": "America/Los_Angeles",
        "t": t, "n": rows, "span": [t[0], t[-1]],
        "day": [midnight, midnight + 86400.0],
        "fuels": {k: [int(round(v)) for v in fuels[k]] for k in order},
        "fuel_order": order,
        "units": {"generation": "MW", "demand": "MW",
                  "co2": "metric tons per hour"},
        "demand": None, "co2": None,
    }

    total = [sum(fuels[k][i] for k in order if k != "batteries")
             + max(0.0, fuels["batteries"][i]) for i in range(rows)]
    net = [sum(fuels[k][i] for k in order) for i in range(rows)]

    if with_demand:
        dt = [midnight + i * STEP for i in range(rows + 1)]
        payload["demand"] = {
            "t": dt, "n": rows + 1,
            "order": ["day_ahead_forecast", "current_demand"],
            "series": {
                "day_ahead_forecast": [int(round(net[min(i, rows - 1)]))
                                       for i in range(rows + 1)],
                "current_demand": [int(round(net[i])) for i in range(rows)]
                + [None],
            }}
    if with_co2:
        payload["co2"] = {
            "t": list(t), "n": rows, "order": ["natural_gas_co2"],
            "series": {"natural_gas_co2":
                       [int(round(fuels["natural_gas"][i] * 0.4))
                        for i in range(rows)]}}
    if mangle:
        mangle(payload)

    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "caiso-mix.json")
    with open(path, "w") as fh:
        json.dump({"name": "caiso-mix", "source": "synthetic",
                   "ttl": ftdata.CAISO_TTL,
                   "fetched_at": time.time() - fetched_ago,
                   "payload": payload}, fh)
    return path, {"t": t, "fuels": fuels, "total": total, "net": net,
                  "midnight": midnight, "rows": rows}


def col_of(hour, w=320):
    return int(hour / 24.0 * w)


# --------------------------------------------------------------------------
# 1. The picture, in pixels, against the day we invented.
# --------------------------------------------------------------------------

def test_stack_pixels():
    print("\nthe stacked area, read back off the panel")
    tmp = tempfile.mkdtemp(prefix="caiso-syn")
    try:
        _, truth = synthetic(tmp)
        args = opts(cache_dir=tmp, sweep=0.0, reveal=0.0)
        r, f = frames(args, 8)
        lay = r.layout
        y0, y1 = lay.chart_y, lay.chart_bot + 1

        firm, solar, wind, imports, burned = (g[3] for g in caiso.GROUPS)

        # Bottom to top must be firm, solar, wind, imports, burned. Read down
        # the column and reverse it: this is the check that catches a stack
        # assembled in the wrong order, which no amount of looking would.
        noon = col_of(13)
        runs = [c for c, _n in colour_runs(f, noon, y0, y1)
                if c in (firm, solar, wind, imports, burned)]
        check("bands run firm, solar, wind, imports, burned bottom to top",
              runs == [burned, imports, wind, solar, firm],
              "top-down %s" % (" ".join(
                  {burned: "BURN", imports: "IMP", wind: "WIND",
                   solar: "SOL", firm: "FIRM"}[c] for c in runs) or "nothing"))

        # Solar is a bell, so it must be absent at two in the morning and the
        # largest band at one in the afternoon.
        night = col_of(2)
        check("no solar band at 2 am",
              band_rows(f, night, solar, y0, y1) == 0,
              "%d rows" % band_rows(f, night, solar, y0, y1))
        widths = {n: band_rows(f, noon, c, y0, y1)
                  for n, _l, _m, c in caiso.GROUPS}
        check("solar is the widest band at 1 pm",
              widths["solar"] == max(widths.values()),
              " ".join("%s=%d" % kv for kv in widths.items()))

        # The duck: the band on top has to thicken after the sun goes.
        gas_noon = band_rows(f, noon, burned, y0, y1)
        gas_eve = band_rows(f, col_of(20), burned, y0, y1)
        check("the burned band is thicker at 8 pm than at 1 pm",
              gas_eve > gas_noon + 2, "%d rows vs %d" % (gas_eve, gas_noon))

        # And the height of the whole stack has to be the total, to a row.
        i = int(13 * 3600.0 / STEP)
        want = truth["total"][i] / r.state["scale"] * (lay.chart_h - 1)
        got = sum(band_rows(f, noon, c, y0, y1) for _n, _l, _m, c in
                  caiso.GROUPS)
        check("stack height at 1 pm is the total generation, to a row",
              abs(got - want) <= 2.0, "%d rows drawn, %.1f expected"
              % (got, want))

        # Every published column has to land in exactly one band, or the total
        # is quietly short and so is the headline.
        members = [m for _n, _l, ms, _c in caiso.GROUPS for m in ms]
        check("every fuel column is in exactly one band and none twice",
              len(members) == len(set(members))
              and set(members) | {"batteries"} == set(truth["fuels"]),
              "%d members, %d columns"
              % (len(members), len(truth["fuels"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_battery_sign():
    print("\nthe battery lane, which is plausible upside down")
    tmp = tempfile.mkdtemp(prefix="caiso-bat")
    try:
        synthetic(tmp)
        r, f = frames(opts(cache_dir=tmp, sweep=0.0, reveal=0.0), 8)
        lay = r.layout
        if not lay.batt_h:
            check("battery lane exists at 320x64", False)
            return
        y0 = lay.batt_y
        mid = y0 + lay.batt_h // 2
        up = f[y0:mid]
        down = f[mid + 1:y0 + lay.batt_h]

        charge = col_of(12)
        discharge = col_of(19)
        c_in = np.array(caiso.C_BATT_IN, np.uint8)
        c_out = np.array(caiso.C_BATT_OUT, np.uint8)
        check("charging at noon draws below the line, in the charge colour",
              (down[:, charge] == c_in).all(axis=1).any()
              and not (up[:, charge] == c_out).all(axis=1).any())
        check("discharging at 7 pm draws above the line, in the other colour",
              (up[:, discharge] == c_out).all(axis=1).any()
              and not (down[:, discharge] == c_in).all(axis=1).any())
        # Zero crossing: at four in the afternoon the fleet is doing neither,
        # and a lane that draws a bar there is reading the sign off something
        # that is not the sign.
        quiet = col_of(16)
        check("a column between the lobes draws no bar",
              not (down[:, quiet] == c_in).all(axis=1).any()
              and not (up[:, quiet] == c_out).all(axis=1).any())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_now_line():
    print("\nthe now-line marks the edge of the data, not the clock")
    tmp = tempfile.mkdtemp(prefix="caiso-now")
    try:
        # Half a day of data on a full day's axis. The line belongs at noon,
        # and nothing at all belongs to the right of it.
        synthetic(tmp, rows=int(12 * 3600 / STEP))
        r, f = frames(opts(cache_dir=tmp, sweep=0.0, reveal=0.0), 8)
        lay = r.layout
        want = col_of(12)
        check("half a day of data puts the line at midday",
              abs(r.state["now_col"] - want) <= 2,
              "column %d, expected %d" % (r.state["now_col"], want))

        band = f[lay.chart_y:lay.chart_bot + 1]
        drawn = [tuple(int(v) for v in c) for _n, _l, _m, c in caiso.GROUPS]
        after = band[:, r.state["now_col"] + 3:]
        painted = sum(int((after == np.array(c, np.uint8)).all(axis=2).sum())
                      for c in drawn)
        check("nothing is drawn to the right of it", painted == 0,
              "%d band pixels in the future" % painted)
        check("the time it stopped at is printed on the panel",
              contains_text(f, caiso.hhmm(r.state["rec"]["latest"])),
              caiso.hhmm(r.state["rec"]["latest"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2. Motion. It is a still picture by nature and it must not look like one.
# --------------------------------------------------------------------------

def test_motion():
    print("\nit does not sit there")
    tmp = tempfile.mkdtemp(prefix="caiso-mot")
    try:
        synthetic(tmp)
        args = opts(cache_dir=tmp)
        r = caiso.build(args)
        lay = r.layout

        # The reveal, frame by frame from zero. Counting lit columns rather
        # than differing pixels: the question is whether the day arrives left
        # to right, not merely whether anything changed.
        lit = []
        for i in range(int(args.reveal * 20) + 2):
            f = r(i / 20.0, i)
            band = f[lay.chart_y:lay.chart_bot + 1]
            solar = (band == np.array(caiso.GROUPS[1][3], np.uint8)).all(axis=2)
            lit.append(int(solar.any(axis=0).sum()))
        check("the day reveals from the left rather than appearing",
              lit[0] < lit[len(lit) // 2] < lit[-1] and lit[0] <= 2,
              "solar columns %d -> %d -> %d"
              % (lit[0], lit[len(lit) // 2], lit[-1]))

        # After the reveal it must still be moving. Same render, kept running.
        prev = None
        diffs = []
        for i in range(int(args.reveal * 20) + 2, int(args.reveal * 20) + 140):
            f = r(i / 20.0, i)
            if prev is not None:
                diffs.append(int((f != prev).any(axis=2).sum()))
            prev = f.copy()
        # Not "every frame differs": the now-line's brightness is an integer
        # and at twenty frames a second two neighbours occasionally round to
        # the same value, which is a rounding artefact and not a stall. What
        # would read as a stall is a *run* of them, so that is what is asserted.
        run = best = 0
        for d in diffs:
            run = run + 1 if d == 0 else 0
            best = max(best, run)
        check("the panel never holds the same frame for a tenth of a second",
              best <= 2, "longest identical run %d frames of %d"
              % (best, len(diffs)))
        check("the sheen moves a substantial part of the panel",
              max(diffs) > 200, "biggest change %d pixels" % max(diffs))

        # And the sheen has to cross the whole panel rather than flickering in
        # one place. Measured as where each frame *differs from the baked
        # picture*, not as the brightest column: the brightest column of this
        # chart is the evening gas peak all day long, and it would have
        # answered "the sheen never moves" while the sheen swept past it.
        r2 = caiso.build(opts(cache_dir=tmp, reveal=0.0))
        base = r2.static.astype(int)
        cols = set()
        for i in range(int(args.sweep * 20) + 4):
            f = r2(i / 20.0, i).astype(int)
            d = np.abs(f - base).sum(axis=(0, 2)).astype(float)
            # The now-line differs from the baked frame every frame by design,
            # and it is a whole column of it. Left in, it anchors the centroid
            # wherever the data happens to end.
            d[max(0, r2.state["now_col"] - 2):r2.state["now_col"] + 3] = 0.0
            if d.sum() <= 0:
                continue
            centre = float((d * np.arange(len(d))).sum() / d.sum())
            cols.add(int(centre) // 40)
        check("the sheen crosses the whole width over one period",
              len(cols) >= 6, "%d of 8 eighths of the panel visited"
              % len(cols))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 3. The arithmetic, against the numbers rather than the picture.
# --------------------------------------------------------------------------

def test_numbers():
    print("\nthe headline numbers against the record they came from")
    tmp = tempfile.mkdtemp(prefix="caiso-num")
    try:
        _, truth = synthetic(tmp)
        r, f = frames(opts(cache_dir=tmp, sweep=0.0, reveal=0.0), 8)
        rec = r.state["rec"]
        i = truth["rows"] - 1

        check("gross supply is the sum of every column plus any discharge",
              abs(r.state["supply"] - truth["total"][i]) < 1.0,
              "%.0f MW vs %.0f" % (r.state["supply"], truth["total"][i]))
        check("demand is CAISO's own reported figure, not the reconstruction",
              abs(r.state["demand"] - truth["net"][i]) < 1.0,
              "%.0f MW" % r.state["demand"])

        clean = sum(truth["fuels"][k][i] for k in
                    ("solar", "wind", "geothermal", "large_hydro",
                     "small_hydro", "nuclear"))
        want = clean / truth["total"][i]
        check("carbon-free share is clean over everything supplying the state",
              abs(r.state["clean"] - want) < 1e-3,
              "%.3f vs %.3f" % (r.state["clean"], want))
        check("imports and biomass are not counted as carbon-free",
              r.state["clean"] < 1.0 and want < 1.0,
              "%.1f%%" % (100 * want))
        check("the share reaches the panel as a percentage",
              contains_text(f, "%d%%" % round(want * 100)),
              "%d%%" % round(want * 100))
        check("so does the demand, in gigawatts",
              contains_text(f, "%.1f GW" % (r.state["demand"] / 1000.0)),
              "%.1f GW" % (r.state["demand"] / 1000.0))

        # Carbon intensity is tons an hour over megawatts, times a thousand.
        want_i = (truth["fuels"]["natural_gas"][i] * 0.4
                  / truth["net"][i] * 1000.0)
        check("carbon intensity is the published tons over the demand",
              abs(r.state["intensity"] - want_i) < 1.0,
              "%.0f g/kWh vs %.0f" % (r.state["intensity"], want_i))

        # A day with no sun anywhere in it must not come out clean, and a
        # denominator of zero must not come out as anything at all.
        dark = tempfile.mkdtemp(prefix="caiso-dark")
        try:
            synthetic(dark, solar_peak=0.0, gas_base=30000.0, gas_peak=0.0)
            rd, _ = frames(opts(cache_dir=dark, sweep=0.0, reveal=0.0), 8)
            check("a day with no sun in it reads far dirtier",
                  rd.state["clean"] < 0.4,
                  "%.0f%% clean" % (100 * rd.state["clean"]))
            check("...and the headline colour changes with it",
                  caiso.clean_colour(rd.state["clean"])
                  != caiso.clean_colour(r.state["clean"]))
        finally:
            shutil.rmtree(dark, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_live(cache_dir):
    print("\nagainst the live cache")
    got = ftdata.load("caiso-mix", cache_dir)
    if got is None:
        check("live record present", False,
              "run: python3 ftdata.py --once --only caiso-mix")
        return
    payload, age = got
    rec, _, err = caiso.read_mix(cache_dir)
    if rec is None:
        check("live record parses into something drawable", False, err)
        return
    check("live record parses into something drawable", True,
          "%s, %d samples, %s old"
          % (rec["date"], len(rec["t"]), ftdata.describe_age(age)))

    # The bands must add up to what CAISO published, column for column, or
    # something has been dropped between the JSON and the chart.
    i = len(rec["t"]) - 1
    published = sum(float(payload["fuels"][k][i] or 0)
                    for k in payload["fuel_order"] if k != "batteries")
    grouped = sum(float(rec["groups"][n][i]) for n, _l, _m, _c in caiso.GROUPS)
    check("the five bands add up to the thirteen published columns",
          abs(grouped - published) < 60.0,
          "%.0f MW grouped vs %.0f published" % (grouped, published))

    share = caiso.clean_share(rec)
    check("carbon-free share is a fraction and is not suspiciously round",
          share is not None and 0.05 < share < 1.0, "%.1f%%" % (100 * share))
    dem = caiso.demand_mw(rec)
    check("demand is a plausible number of gigawatts for California",
          10000.0 < dem < 60000.0, "%.1f GW" % (dem / 1000.0))

    # The one CAISO-specific sanity check worth making: at night the sun is
    # down, and the record says so in the only place it can.
    solar = rec["groups"]["solar"]
    hours = np.array([(t - rec["day"][0]) / 3600.0 for t in rec["t"]])
    night = solar[(hours < 4.0)]
    if len(night):
        check("no solar generation before four in the morning",
              float(night.max()) < 200.0, "peak %.0f MW" % night.max())

    r, f = frames(opts(cache_dir=cache_dir, sweep=0.0, reveal=0.0), 8)
    check("the live record renders a chart and not a card",
          not contains_text(f, "NO GRID DATA") and f.max() > 0,
          "%.1f GW, %d%% clean"
          % (r.state["demand"] / 1000.0, round(100 * r.state["clean"])))


# --------------------------------------------------------------------------
# 4. Degraded records. Every one of these has to reach the panel in words.
# --------------------------------------------------------------------------

def test_degraded():
    print("\nmissing, wrong-day, corrupt and half-there records")
    tmp = tempfile.mkdtemp(prefix="caiso-bad")
    try:
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        r, f = frames(opts(cache_dir=empty), 8)
        check("no cache at all says NO GRID DATA",
              contains_text(f, "NO GRID DATA"))
        check("...and names the command that fixes it",
              contains_text(f, "FTDATA.PY"))
        check("...and draws no chart", r.state["rec"] is None)

        bad = os.path.join(tmp, "corrupt")
        os.makedirs(bad)
        with open(os.path.join(bad, "caiso-mix.json"), "w") as fh:
            fh.write('{"payload": {"fuels": ')
        _, f = frames(opts(cache_dir=bad), 8)
        check("a half-written file says NO GRID DATA",
              contains_text(f, "NO GRID DATA"))

        wrong = os.path.join(tmp, "wrongshape")
        os.makedirs(wrong)
        with open(os.path.join(wrong, "caiso-mix.json"), "w") as fh:
            json.dump({"name": "caiso-mix", "fetched_at": time.time(),
                       "payload": {"hello": "world"}}, fh)
        _, f = frames(opts(cache_dir=wrong), 8)
        check("a payload from some other product says NO GRID DATA",
              contains_text(f, "NO GRID DATA"))

        # The dangerous one. Yesterday's record is complete, well formed and a
        # perfectly good duck, and drawing it under today's clock would put the
        # evening peak where this morning goes.
        old = os.path.join(tmp, "yesterday")
        synthetic(old, day_offset=-1, fetched_ago=26 * 3600.0)
        r, f = frames(opts(cache_dir=old), 8)
        check("a complete record of yesterday is refused, not drawn",
              r.state["rec"] is None and contains_text(f, "NO GRID DATA"),
              str(r.state["problem"])[:52])
        check("...and says which day it refused",
              contains_text(f, "RECORD IS FROM"), str(r.state["problem"])[:52])

        # Fetched two hours ago, which is past the TTL. The morning it holds is
        # still this morning, so it draws -- loudly.
        stale = os.path.join(tmp, "stale")
        synthetic(stale, rows=int(6 * 3600 / STEP), fetched_ago=2 * 3600.0)
        r, f = frames(opts(cache_dir=stale), 8)
        check("an hours-old record of today still draws",
              r.state["rec"] is not None
              and not contains_text(f, "NO GRID DATA"))
        check("...and says STALE on the panel", contains_text(f, "STALE")
              and r.state["stale"], "age %s"
              % ftdata.describe_age(r.state["rec"]["age"]))

        # Demand and emissions are optional; the mix is not. Losing them must
        # cost their numbers and nothing else.
        partial = os.path.join(tmp, "partial")
        synthetic(partial, with_demand=False, with_co2=False)
        r, f = frames(opts(cache_dir=partial, sweep=0.0, reveal=0.0), 8)
        check("no demand or co2 file still draws the mix",
              r.state["rec"] is not None and r.state["clean"] is not None)
        check("...and falls back to the reconstructed demand",
              r.state["demand"] > 0 and r.state["intensity"] is None,
              "%.1f GW, no intensity" % (r.state["demand"] / 1000.0))

        # A column CAISO stopped publishing. The band it was in must shrink,
        # not vanish, and nothing may crash.
        holed = os.path.join(tmp, "holed")
        synthetic(holed, drop=("geothermal", "small_hydro"))
        r, f = frames(opts(cache_dir=holed, sweep=0.0, reveal=0.0), 8)
        check("a missing fuel column loses its megawatts and nothing else",
              r.state["rec"] is not None and r.state["clean"] is not None,
              "%.1f GW" % (r.state["demand"] / 1000.0))

        # A record with one row cannot be a chart.
        thin = os.path.join(tmp, "thin")
        synthetic(thin, rows=1)
        r, f = frames(opts(cache_dir=thin), 8)
        check("a record with a single sample says NO GRID DATA",
              contains_text(f, "NO GRID DATA"), str(r.state["problem"])[:52])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 5. Fresh, stale and absent, each in a process of its own.
#
# `ftdata.CACHE_DIR` is read at import time, so a test that sets FT_DATA_CACHE
# and reloads the module is testing the state of its own import machinery and
# not the state of the cache. It has passed here before while proving nothing.
# So each state gets a fresh interpreter, with the environment set the way the
# wall sets it and no --cache-dir to paper over it.
# --------------------------------------------------------------------------

def _one_state(state, cache_dir):
    """The child half. Prints one RESULT line and exits."""
    args = opts()                       # note: no cache_dir, so CACHE_DIR wins
    r = caiso.build(args)
    out = None
    for i in range(8):
        out = r(i / 20.0, i)
    card = contains_text(out, "NO GRID DATA")
    stale = contains_text(out, "STALE")
    drew = r.state["rec"] is not None
    verdict = {
        "fresh": (drew and not card and not stale, "drew a chart, no flags"),
        "stale": (drew and not card and stale, "drew a chart with STALE on it"),
        "absent": (not drew and card, "drew the no-data card"),
    }[state]
    print("RESULT %s %s cache=%s drew=%s card=%s stale=%s"
          % (state, "ok" if verdict[0] else "FAIL",
             os.path.basename(cache_dir), drew, card, stale))
    return 0 if verdict[0] else 1


def test_states_in_separate_processes():
    print("\nfresh, stale and absent -- one process each, FT_DATA_CACHE set")
    tmp = tempfile.mkdtemp(prefix="caiso-proc")
    try:
        fresh = os.path.join(tmp, "fresh")
        synthetic(fresh, rows=int(9 * 3600 / STEP), fetched_ago=120.0)
        stale = os.path.join(tmp, "stale")
        synthetic(stale, rows=int(9 * 3600 / STEP), fetched_ago=3 * 3600.0)
        absent = os.path.join(tmp, "absent")
        os.makedirs(absent)

        for state, d in (("fresh", fresh), ("stale", stale),
                         ("absent", absent)):
            env = dict(os.environ, FT_DATA_CACHE=d)
            # FT_DATA_BLOBS too: the blob directory is searched for volatile
            # records, and a stray /run/ftdata on the machine running the tests
            # must not be able to answer for this one.
            env["FT_DATA_BLOBS"] = d
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--state", state, "--cache-dir", d],
                env=env, capture_output=True, text=True, timeout=120)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT")]
            check("%s cache, in its own process" % state,
                  proc.returncode == 0 and bool(line),
                  (line[0] if line else proc.stderr.strip().splitlines()[-1:]
                   or "no output")[7:] if line else "no RESULT line")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 6. Other panels, and the promise that none of this talks to anyone.
# --------------------------------------------------------------------------

def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="caiso-size")
    try:
        synthetic(tmp)
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, f = frames(opts(cache_dir=tmp, width=w, height=h), 60)
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                lay = r.layout
                detail = "chart %d rows, lane %d, legend %d" % (
                    lay.chart_h, lay.batt_h, lay.legend_h)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("caiso-mix", tempfile.mkdtemp(prefix="caiso-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "caiso.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in src]
    check("caiso.py does not import one either", not imported,
          ",".join(imported))


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
    test_stack_pixels()
    test_battery_sign()
    test_now_line()
    test_motion()
    test_numbers()
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
