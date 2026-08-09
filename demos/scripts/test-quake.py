#!/usr/bin/env python3
"""Checks for quake.py that a screenshot cannot make.

This demo has two failure modes that both look like a working panel.

The first is that **its good state and its broken state are the same picture**.
Most weeks there is nothing within 100 km worth drawing, so a map with almost
nothing on it is the correct answer -- and it is also what a projection bug, an
empty cache, a bad date filter and a silently-dropped event list all produce. A
photograph cannot tell them apart. So the geography is asserted against known
coordinates in pixels, the event count on the panel is asserted against the
count in the record, and the three data states are asserted to draw three
visibly different things.

The second is that **the loud path is the one that matters and never runs**. A
local M5 happens every few years; the code that takes the panel over when one
does would otherwise ship untested and be discovered wrong at the worst
possible moment. So a synthetic M5.8 under Berkeley is written into a cache
directory and the panel is required to become dramatic: red header, the
magnitude in large type, the distance and bearing from this building, a mark on
the epicentre, and rings that actually expand -- measured across sequential
frames, not sampled.

Everything here renders **frames in sequence from a fresh `build()`**. This
demo is not a pure function of `t`: the pulse, the blink and the ring phase all
run off it and the reload runs off wall-clock, and sampling `render()` at
scattered timestamps has produced three separate false conclusions elsewhere in
this project.

The three cache states -- fresh, stale, absent -- each run in a **separate
process** with `FT_DATA_CACHE` set, because `ftdata.CACHE_DIR` binds at import
and reloading the module in one process does not move it. That has produced a
false pass here before.

    $ python3 scripts/test-quake.py                     # uses the live cache
    $ python3 scripts/test-quake.py --cache-dir /tmp/c  # or a pointed one

Needs a populated cache for the live checks; run
`python3 ftdata.py --once --only quake-usgs` first. The synthetic, stale and
no-data cases build their own cache directories and need nothing.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                       # noqa: E402
import ftdata                                                # noqa: E402
import quake                                                 # noqa: E402

FAILED = []
SKIPPED = []
PASSED = [0]


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-56s %s" % (name, detail))
    else:
        print("  FAIL %-56s %s" % (name, detail))
        FAILED.append(name)


def skip(name, reason):
    """A check that could not be made, said out loud rather than passed."""
    print("  skip %-56s %s" % (name, reason))
    SKIPPED.append(name)


def opts(**kw):
    return ds.options(quake, **kw)


def frames(args, n=8, fps=20.0):
    """Render n frames in sequence from a fresh build. Returns (render, list).

    Sequential and from scratch, always. See the module docstring.
    """
    r = quake.build(args)
    return r, [r(i / fps, i).copy() for i in range(n)]


def contains_text(frame, s, thresh=70, scales=(1, 2, 3, 4)):
    """Is this string drawn anywhere on the frame, at any position or size?

    Renders the same glyph mask the demo uses and slides it over the lit
    pixels. Reading the words back off the panel is the only way to be sure the
    honest message reached it rather than merely being computed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = quake.text_mask(s, scale)
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
# Building a cache to order. Every degraded and synthetic case writes one of
# these rather than waiting for the ground to cooperate.
# --------------------------------------------------------------------------

def write_cache(path, events, now=None, age=0.0, baseline=None, world=None):
    """A quake-usgs record in `path`, with `age` seconds on the clock."""
    now = time.time() if now is None else now
    os.makedirs(path, exist_ok=True)
    payload = {
        "site": [quake.SITE_LAT, quake.SITE_LON],
        "generated": now, "feed": "all_week.geojson", "span_h": 168.0,
        "local": {"radius_km": 300.0, "n": len(events),
                  "non_earthquakes_dropped": 0,
                  "events": sorted(events, key=lambda e: e["t"], reverse=True)},
        "world": {"min_mag": 4.5, "n": len(world or []), "biggest": None,
                  "events": world or []},
        "baseline": baseline,
    }
    with open(os.path.join(path, "quake-usgs.json"), "w") as fh:
        json.dump({"name": "quake-usgs", "fetched_at": now - age,
                   "source": "test", "ttl": 3600, "payload": payload}, fh)
    return path


def event(lat, lon, mag, t, place="TESTVILLE, CA", dep=8.0):
    km, bearing = km_bearing(lat, lon)
    return {"id": "test%d" % int(t * 1000), "t": t, "mag": mag, "magtype": "ml",
            "lat": lat, "lon": lon, "dep": dep, "km": round(km, 1),
            "bearing": round(bearing), "place": place}


def km_bearing(lat, lon):
    """The same haversine ftdata uses, repeated here on purpose.

    Importing ftdata's private helper would make this check agree with the
    fetcher by construction rather than by measurement, which is not a check.
    """
    la0, lo0 = math.radians(quake.SITE_LAT), math.radians(quake.SITE_LON)
    la1, lo1 = math.radians(lat), math.radians(lon)
    dlo = lo1 - lo0
    h = (math.sin((la1 - la0) / 2) ** 2
         + math.cos(la0) * math.cos(la1) * math.sin(dlo / 2) ** 2)
    km = 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))
    y = math.sin(dlo) * math.cos(la1)
    x = math.cos(la0) * math.sin(la1) - math.sin(la0) * math.cos(la1) * math.cos(dlo)
    return km, math.degrees(math.atan2(y, x)) % 360.0


# --------------------------------------------------------------------------
# 1. Geography. A map that is right about nothing else must still be right
#    about where things are, and this is the only part of the panel whose
#    correct answer is known to four decimal places in advance.
# --------------------------------------------------------------------------

LANDMARKS = [
    # name, lat, lon, which tile it must land on
    ("Sequoia Fabrica", 37.7627, -122.3966, "both"),
    ("Golden Gate Bridge", 37.8199, -122.4783, "both"),
    ("The Geysers", 38.7900, -122.7500, "both"),
    ("San Jose", 37.3382, -121.8863, "both"),
    ("Parkfield", 35.8997, -120.4327, "region"),
    ("Cape Mendocino", 40.4402, -124.4090, "region"),
]


def test_geography():
    print("\ngeography: known places land where they should")
    lay = quake.Layout(320, 64)
    bay, reg = lay.tiles()

    for name, lat, lon, where in LANDMARKS:
        on_bay = bay.holds(lat, lon)
        on_reg = reg.holds(lat, lon)
        want_bay = where == "both"
        check("%s is on the tiles it should be" % name,
              on_bay == want_bay and on_reg,
              "bay=%s region=%s" % (on_bay, on_reg))

    # Scale and orientation, asserted as a distance in pixels rather than as a
    # projection formula, which would just be the code again. SF to San Jose is
    # 68 km on a bearing of 145 degrees; on a tile squashed 2.4:1 that must come
    # out much wider than it is tall, and *down* the panel, not up it.
    r0, c0 = bay.project(quake.SITE_LAT, quake.SITE_LON)
    r1, c1 = bay.project(37.3382, -121.8863)
    check("san jose is below san francisco on the bay tile", r1 > r0,
          "rows %.1f -> %.1f" % (r0, r1))
    check("...and to the east of it", c1 > c0, "cols %.1f -> %.1f" % (c0, c1))
    km = km_bearing(37.3382, -121.8863)[0]
    check("bay tile scale matches its own km-per-pixel",
          abs(math.hypot((c1 - c0) / bay.px_km_x,
                         (r1 - r0) / bay.px_km_y) - km) < 3.0,
          "%.1f km measured, %.1f km true" % (
              math.hypot((c1 - c0) / bay.px_km_x, (r1 - r0) / bay.px_km_y), km))

    check("bay tile is squashed, and by the amount claimed",
          2.2 < bay.px_km_x / bay.px_km_y < 2.6,
          "%.2fx" % (bay.px_km_x / bay.px_km_y))
    check("region tile is within a few per cent of true scale",
          0.95 < reg.px_km_x / reg.px_km_y < 1.05,
          "%.3fx" % (reg.px_km_x / reg.px_km_y))
    check("the 300 km circle fits inside the region tile",
          reg.holds(*offset_km(quake.SITE_LAT, quake.SITE_LON, 300.0, 0.0))
          and reg.holds(*offset_km(quake.SITE_LAT, quake.SITE_LON, 300.0, 90.0))
          and reg.holds(*offset_km(quake.SITE_LAT, quake.SITE_LON, 300.0, 180.0))
          and reg.holds(*offset_km(quake.SITE_LAT, quake.SITE_LON, 300.0, 270.0)))

    # The water mask, checked against places that are unambiguously wet or dry.
    sea_bay = quake.unpack_sea(quake.SEA_BAY, 155, 57, bay.w, bay.h)
    sea_reg = quake.unpack_sea(quake.SEA_REGION, 57, 57, reg.w, reg.h)
    wet = [("mid-Pacific", 37.70, -123.40), ("San Pablo Bay", 38.07, -122.42)]
    dry = [("Livermore", 37.68, -121.77), ("Santa Rosa", 38.44, -122.71),
           ("San Jose", 37.34, -121.89)]
    for name, lat, lon in wet:
        r, c = [int(round(v)) for v in bay.project(lat, lon)]
        check("water mask has %s as water" % name, bool(sea_bay[r, c]))
    for name, lat, lon in dry:
        r, c = [int(round(v)) for v in bay.project(lat, lon)]
        check("water mask has %s as land" % name, not sea_bay[r, c])
    r, c = [int(round(v)) for v in reg.project(36.0, -125.0)]
    check("region mask has the open Pacific as water", bool(sea_reg[r, c]))

    # A mirrored mask passes every "is it 32% water" test and fails this one,
    # which is the point of checking places rather than fractions. East-west
    # rather than north-south, because this box is nearly all Pacific down its
    # western edge at every latitude and a vertical flip leaves the open ocean
    # still open ocean -- a control that cannot fail is not a control.
    flipped = sea_bay[:, ::-1]
    r, c = [int(round(v)) for v in bay.project(37.70, -123.40)]
    check("an east-west mirrored mask would be rejected", not flipped[r, c])


def offset_km(lat, lon, km, bearing):
    """A point `km` from (lat, lon) on a compass bearing."""
    d = km / 6371.0088
    b = math.radians(bearing)
    la0, lo0 = math.radians(lat), math.radians(lon)
    la1 = math.asin(math.sin(la0) * math.cos(d)
                    + math.cos(la0) * math.sin(d) * math.cos(b))
    lo1 = lo0 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la0),
                           math.cos(d) - math.sin(la0) * math.sin(la1))
    return math.degrees(la1), math.degrees(lo1)


# --------------------------------------------------------------------------
# 2. Magnitude. The whole design rests on rupture size, so the numbers that
#    come out of it are checked against seismology rather than against
#    themselves: Loma Prieta's rupture is a published length.
# --------------------------------------------------------------------------

def test_magnitude():
    print("\nmagnitude maps to rupture size, not to a marker size")
    # Wells & Coppersmith, strike-slip: an M6.9 breaks about 40 km of fault.
    lp = 2.0 * quake.rupture_km(6.9)
    check("M6.9 gives roughly Loma Prieta's rupture length", 30.0 < lp < 55.0,
          "%.1f km" % lp)
    m4 = 2.0 * quake.rupture_km(4.0)
    check("M4 breaks under a kilometre", 0.3 < m4 < 1.5, "%.2f km" % m4)

    # Each whole magnitude must multiply the rupture by about 4, which is what
    # 10^0.59 is. A linear-in-magnitude scale would give a constant difference,
    # not a constant ratio, and this is the check that tells them apart.
    ratios = [quake.rupture_km(m + 1) / quake.rupture_km(m) for m in range(2, 7)]
    check("rupture grows by a constant *ratio* per magnitude",
          all(abs(r - ratios[0]) < 1e-6 for r in ratios) and 3.5 < ratios[0] < 4.5,
          "x%.2f per unit" % ratios[0])

    # And on the panel: a big event has to cover far more pixels than a small
    # one, measured by counting them rather than by trusting the arithmetic.
    lay = quake.Layout(320, 64)
    bay, _ = lay.tiles()
    areas = {}
    for mag in (1.0, 3.0, 5.0, 6.5):
        buf = np.zeros((64, 320, 3), np.uint8)
        km = quake.rupture_km(mag)
        quake.blob(bay.region(buf), 28.0, 70.0,
                   km * bay.px_km_y, km * bay.px_km_x, (200, 200, 200))
        areas[mag] = int((buf.max(axis=2) > 0).sum())
    check("every event is at least one pixel", areas[1.0] >= 1,
          "M1 -> %d px" % areas[1.0])
    check("M1 and M3 are both floored to a dot", areas[3.0] <= 2,
          "M3 -> %d px" % areas[3.0])
    check("M6.5 dominates the map", areas[6.5] > 40 * areas[3.0],
          "M6.5 -> %d px vs M3 %d px" % (areas[6.5], areas[3.0]))
    # And the disc carries the tile's squash: a rupture is round on the ground,
    # so on a map squashed 2.4:1 it has to be an ellipse of that aspect. A disc
    # drawn round on screen would be the one thing on this panel not in the
    # same projection as everything else.
    buf = np.zeros((64, 320, 3), np.uint8)
    km = quake.rupture_km(6.5)
    quake.blob(bay.region(buf), 28.0, 70.0,
               km * bay.px_km_y, km * bay.px_km_x, (200, 200, 200))
    ys, xs = np.nonzero(buf.max(axis=2) > 0)
    aspect = (xs.max() - xs.min() + 1) / float(ys.max() - ys.min() + 1)
    check("a rupture disc carries the tile's squash",
          abs(aspect - bay.px_km_x / bay.px_km_y) < 0.45,
          "%.2f drawn vs %.2f expected" % (aspect, bay.px_km_x / bay.px_km_y))

    # Age fades, and it fades monotonically. A fade that is not monotonic makes
    # last Tuesday brighter than this morning somewhere in the middle.
    fades = [quake.age_fade(a) for a in
             (0, 3600, 86400, 3 * 86400, quake.WEEK)]
    check("age fades monotonically over the window",
          all(a > b for a, b in zip(fades, fades[1:])),
          " ".join("%.2f" % f for f in fades))
    check("...and never to nothing", fades[-1] > 0.2, "%.2f at 7d" % fades[-1])


# --------------------------------------------------------------------------
# 3. The quiet panel, on the live cache. The count on the screen has to be the
#    count in the record: a projection that silently drops events off a tile is
#    the single most plausible way for this map to be quietly wrong.
# --------------------------------------------------------------------------

def test_live(cache_dir):
    print("\nthe quiet panel, against the live cache")
    got = ftdata.load("quake-usgs", cache_dir)
    if got is None:
        skip("live cache is populated",
             "run: python3 ftdata.py --once --only quake-usgs")
        return
    payload, age = got
    cat = quake.Catalogue(cache_dir)
    check("the record parses into a catalogue", cat.usable and bool(cat.local),
          "%d local, %d world, %s old"
          % (len(cat.local), len(cat.world), ftdata.describe_age(age)))
    if not cat.usable:
        return

    check("every local event is inside the collection radius",
          all(e["km"] <= 300.5 for e in cat.local),
          "furthest %.1f km" % max(e["km"] for e in cat.local))
    check("the list is newest first",
          all(a["t"] >= b["t"] for a, b in zip(cat.local, cat.local[1:])))
    check("no event is in the future",
          all(e["t"] <= cat.now + 120 for e in cat.local))
    check("nothing older than the feed's own window",
          all(cat.now - e["t"] <= quake.WEEK + 7200 for e in cat.local))
    check("distances agree with the coordinates",
          max(abs(km_bearing(e["lat"], e["lon"])[0] - e["km"])
              for e in cat.local) < 0.2,
          "worst %.3f km"
          % max(abs(km_bearing(e["lat"], e["lon"])[0] - e["km"])
                for e in cat.local))

    r, fs = frames(opts(cache_dir=cache_dir), 40)
    f = fs[-1]
    check("the panel is 320x64 uint8",
          f.shape == (64, 320, 3) and f.dtype == np.uint8, str(f.shape))

    # The count on the panel against the count in the record. Reading the
    # number back off the pixels rather than off the object it was formatted
    # from is the whole point: a column that silently fell off the layout
    # formats perfectly and draws nothing.
    check("the event count is printed on the panel",
          contains_text(f, "%d IN 7D WITHIN 300KM" % len(cat.local)),
          "%d events" % len(cat.local))
    check("the fetch age is printed on the panel",
          contains_text(f, ftdata.describe_age(cat.age)),
          ftdata.describe_age(cat.age))

    days, ev = cat.days_since_baseline()
    if days is None:
        skip("days-since-M4 is on the panel", "no baseline in this record")
    else:
        check("days-since-M4 is on the panel", contains_text(f, "%d" % int(days)),
              "%d days" % int(days))
        check("...and it agrees with the record's own timestamp",
              abs(days - (cat.now - ev["t"]) / 86400.0) < 1e-6)

    # Every event that projects onto a tile must have left a lit pixel there.
    # Counted against a picture of the geography with no events on it, so the
    # coastline cannot be mistaken for an earthquake.
    bay, reg = r.tiles
    blank = np.zeros((64, 320, 3), np.uint8)
    sea = quake.unpack_sea(quake.SEA_BAY, 155, 57, bay.w, bay.h)
    quake.draw_geography(blank, bay, sea, quake.FAULTS, ())
    before = bay.region(blank).astype(int).sum(axis=2)
    after = bay.region(f).astype(int).sum(axis=2)
    on_tile = [e for e in cat.local
               if bay.holds(e["lat"], e["lon"], -1)
               and cat.now - e["t"] <= quake.WEEK]
    lit = 0
    for e in on_tile:
        rr, cc = [int(round(v)) for v in bay.project(e["lat"], e["lon"])]
        if after[rr, cc] > before[rr, cc]:
            lit += 1
    check("every event on the bay tile brightened its own pixel",
          on_tile and lit == len(on_tile), "%d of %d" % (lit, len(on_tile)))

    # The pulse. Sequential frames, because that is the only way to see it.
    rec = cat.recent()
    if rec is None or not (bay.holds(rec["lat"], rec["lon"], -1)
                           or reg.holds(rec["lat"], rec["lon"], -1)):
        skip("the most recent event breathes", "it is off both tiles")
    else:
        tile = bay if bay.holds(rec["lat"], rec["lon"], -1) else reg
        rr, cc = [int(round(v)) for v in tile.project(rec["lat"], rec["lon"])]
        # .copy(), because render() hands back a buffer it reuses: without it
        # this list is forty references to the last frame and the pulse looks
        # perfectly flat. That is exactly the false conclusion this file's
        # docstring is about.
        seq = [int(tile.region(r(i / 20.0, i).copy())[rr, cc].astype(int).sum())
               for i in range(40)]
        check("the most recent event breathes", max(seq) - min(seq) > 20,
              "%d..%d" % (min(seq), max(seq)))

    # And the heartbeat, which is the difference between a still panel and a
    # frozen one.
    corner = [int(x[0, -1].astype(int).sum()) for x in fs]
    check("the heartbeat blinks", max(corner) - min(corner) > 20,
          "%d..%d" % (min(corner), max(corner)))


# --------------------------------------------------------------------------
# 4. The loud path, on a synthetic M5.8. This is the code that has to work on
#    the one day a decade it runs, so it is the code with the most checks.
# --------------------------------------------------------------------------

def test_alert():
    print("\nthe loud path, on a synthetic M5.8 under Berkeley")
    tmp = tempfile.mkdtemp(prefix="ftq-alert")
    now = time.time()
    big = event(37.8716, -122.2727, 5.8, now - 600.0, "1 KM NE OF BERKELEY, CA",
                dep=9.0)
    quiet = [event(38.79, -122.75, 1.2, now - 3600.0 * k, "THE GEYSERS, CA")
             for k in range(1, 25)]
    aftershocks = [event(37.87, -122.27, 2.4, now - 60.0 * k,
                         "1 KM NE OF BERKELEY, CA") for k in range(1, 6)]
    write_cache(tmp, [big] + quiet + aftershocks, now=now,
                baseline=event(37.1157, -122.1125, 4.6, now - 129 * 86400.0,
                               "1 KM SE OF BOULDER CREEK, CA"))

    cat = quake.Catalogue(tmp, now)
    alert = cat.alert()
    check("an M5.8 ten minutes old raises an alert", alert is not None)
    check("...and it is the M5.8 and not the newest aftershock",
          alert is not None and alert["mag"] == 5.8,
          "M%.1f" % (alert["mag"] if alert else 0))

    r, fs = frames(opts(cache_dir=tmp), 60)
    f = fs[0]
    check("the magnitude is on the panel", contains_text(f, "M5.8"))
    check("the place is on the panel", contains_text(f, "BERKELEY"))
    km = km_bearing(big["lat"], big["lon"])[0]
    check("the distance from this building is on the panel",
          contains_text(f, "%d KM" % round(km)), "%.1f km" % km)
    check("the bearing from this building is on the panel",
          contains_text(f, "NE OF HERE"))
    check("the depth is on the panel", contains_text(f, "DEPTH 9 KM"))
    check("how long ago is on the panel", contains_text(f, "10M AGO"))
    check("the aftershock count is on the panel", contains_text(f, "5 SINCE"),
          "5 later events")

    # A local M5.8 resets the headline scalar even though the stored baseline
    # is 129 days old, because the week's events are consulted too. Getting
    # this wrong prints "129 DAYS" with the earthquake still on the screen.
    days, ev = cat.days_since_baseline()
    check("days-since-M4 resets to today", days is not None and days < 0.02,
          "%.4f days" % (days if days is not None else -1))

    # Red. Measured as a colour balance over the header strip, because "it
    # looks alarming" is not a check.
    lay = r.layout
    head = fs[0][:lay.head_h].astype(int)
    check("the header goes red", head[:, :, 0].sum() > 1.6 * head[:, :, 2].sum(),
          "R %d vs B %d" % (head[:, :, 0].sum(), head[:, :, 2].sum()))
    rule = fs[0][lay.head_h].astype(int)
    check("...and the rule under it turns into an alarm bar",
          rule[:, 0].mean() > 200 and rule[:, 2].mean() < 80,
          "R %.0f B %.0f" % (rule[:, 0].mean(), rule[:, 2].mean()))

    # And it blinks: over a couple of seconds of sequential frames the header
    # must take at least two distinctly different brightnesses.
    sums = sorted(set(int(x[:lay.head_h].astype(int).sum()) for x in fs))
    check("the header blinks", len(sums) >= 2 and sums[-1] > sums[0] * 1.4,
          "%d..%d over %d frames" % (sums[0], sums[-1], len(fs)))

    # The epicentre is marked, and marked in the right place.
    bay, _ = r.tiles
    er, ec = [int(round(v)) for v in bay.project(big["lat"], big["lon"])]
    px = bay.region(fs[0])[er, ec].astype(int)
    check("the epicentre is marked", int(px.sum()) > 400, "%s" % (tuple(px),))

    # The rings expand. Measured as the mean distance of the alert-coloured
    # pixels from the epicentre, across sequential frames -- a ring drawn at a
    # fixed radius, or drawn backwards, fails this and looks fine in a still.
    dist = ring_radius_series(r, bay, er, ec, 40)
    have = [d for d in dist if d is not None]
    check("there are rings to measure", len(have) > 20, "%d frames" % len(have))
    if len(have) > 20:
        # Not simply "increases": the ring restarts every --ring-period, so the
        # test is that it spends most of its time growing.
        deltas = [b - a for a, b in zip(have, have[1:])]
        grew = sum(1 for d in deltas if d > 0.5)
        check("the rings expand rather than sit still",
              grew > 0.55 * len(deltas),
              "%d of %d frames growing" % (grew, len(deltas)))
        check("...and they restart rather than run off the edge",
              min(have) < 40.0 and max(have) > 90.0,
              "%.0f..%.0f km" % (min(have), max(have)))

    # Nothing about the loud path may cost the panel its frame budget.
    t0 = time.perf_counter()
    for i in range(200):
        r(i / 20.0, i)
    ms = (time.perf_counter() - t0) / 200.0 * 1e3
    check("the alert path stays cheap", ms < 0.5, "%.3f ms/frame here" % ms)


def ring_radius_series(render, tile, er, ec, n):
    """Mean radius in km of the alert-red pixels, per frame. None if there are
    none, which is a legitimate answer between rings."""
    out = []
    yy = (np.arange(tile.h)[:, None] - er) / tile.px_km_y
    xx = (np.arange(tile.w)[None, :] - ec) / tile.px_km_x
    d = np.hypot(yy, xx)
    for i in range(n):
        reg = tile.region(render(i / 20.0, i)).astype(int)
        # Red and not much else, and away from the epicentre mark itself.
        m = ((reg[:, :, 0] > 90) & (reg[:, :, 0] > 2 * reg[:, :, 2])
             & (d > 25.0))
        out.append(float(d[m].mean()) if m.any() else None)
    return out


# --------------------------------------------------------------------------
# 5. The quiet state is the *good* state, and must not look like the broken
#    one. This is the check the whole design hangs on.
# --------------------------------------------------------------------------

def test_quiet_is_not_broken():
    print("\na quiet week reads as working, not as broken")
    tmp = tempfile.mkdtemp(prefix="ftq-quiet")
    now = time.time()
    # Four small events in a week, all a long way off. About as empty as this
    # panel ever legitimately gets.
    evs = [event(38.79, -122.75, 0.8, now - 86400.0 * k, "THE GEYSERS, CA")
           for k in range(1, 5)]
    write_cache(tmp, evs, now=now,
                baseline=event(37.1157, -122.1125, 4.6, now - 129 * 86400.0,
                               "1 KM SE OF BOULDER CREEK, CA"),
                world=[[now - 86400.0 * k, 4.6 + 0.2 * k] for k in range(1, 7)])
    r, fs = frames(opts(cache_dir=tmp), 40)
    f = fs[0]

    check("it says QUIET", contains_text(f, "QUIET"))
    check("it says how many days since the last M4", contains_text(f, "129"))
    check("...with the units spelled out", contains_text(f, "DAYS SINCE M4"))
    check("it still names the most recent event",
          contains_text(f, "LATEST"))
    check("the world strip is labelled", contains_text(f, "WORLD M4.5+ 7D"))
    check("it does NOT say NO USGS DATA", not contains_text(f, "NO USGS DATA"))

    # The geography is drawn whether or not anything happened on it: that is
    # what makes an empty week look like a map rather than like a dead panel.
    lay = r.layout
    bay, reg = r.tiles
    body = f[lay.body_y:, :lay.reg_x - 1]
    check("the map is drawn even with four events on it",
          int((body.max(axis=2) > 8).sum()) > 400,
          "%d lit pixels" % int((body.max(axis=2) > 8).sum()))
    check("the range rings are drawn on the region tile",
          int((reg.region(f).max(axis=2) > 8).sum()) > 150,
          "%d lit pixels" % int((reg.region(f).max(axis=2) > 8).sum()))

    # And it is moving. A quiet panel that is also a still panel is
    # indistinguishable from a crashed one.
    diffs = [int(np.abs(a.astype(int) - b.astype(int)).sum())
             for a, b in zip(fs, fs[1:])]
    check("a quiet panel still moves between frames", max(diffs) > 0,
          "max %d channel-counts between frames" % max(diffs))

    # The awkward middle: an M4.2 twenty hours ago is past every alert window,
    # so the panel is back to the quiet layout -- but the headline scalar is
    # now zero, and a bare 0 in large type reads as a broken counter rather
    # than as "it happened today". This is the case that needs a word.
    tmp2 = tempfile.mkdtemp(prefix="ftq-today")
    write_cache(tmp2, [event(37.85, -122.25, 4.2, now - 20 * 3600.0,
                             "1 KM E OF BERKELEY, CA")] + evs, now=now,
                baseline=event(37.1157, -122.1125, 4.6, now - 129 * 86400.0,
                               "1 KM SE OF BOULDER CREEK, CA"))
    cat2 = quake.Catalogue(tmp2, now)
    check("an M4.2 twenty hours old does not raise an alert",
          cat2.alert() is None)
    days2, _ = cat2.days_since_baseline()
    check("...but it does reset days-since-M4",
          days2 is not None and days2 < 1.0, "%.2f days" % days2)
    _, fs2 = frames(opts(cache_dir=tmp2), 4)
    check("...and the panel says TODAY rather than a bare zero",
          contains_text(fs2[-1], "TODAY"))


# --------------------------------------------------------------------------
# 6. The three data states, each in its own process. See the module docstring
#    on why in-process is not good enough.
# --------------------------------------------------------------------------

PROBE = r"""
import json, os, sys, time
import numpy as np
sys.path.insert(0, %(here)r)
import demoscene as ds, ftdata, quake
args = ds.options(quake)
r = quake.build(args)
# Forty frames is two seconds, which is the shortest window in
# which a 0.75 Hz blink is guaranteed to have blinked. Twelve was
# 0.6 s and reported every panel as frozen.
fs = [r(i / 20.0, i).copy() for i in range(40)]
f = fs[-1]
out = {
    "cache_dir": ftdata.CACHE_DIR,
    "shape": list(f.shape),
    "lit": int((f.max(axis=2) > 8).sum()),
    "moves": int(max(int(np.abs(a.astype(int) - b.astype(int)).sum())
                     for a, b in zip(fs, fs[1:]))),
    "state": quake.Catalogue(None).state,
}
np.save(os.environ["FT_PROBE_OUT"], np.stack(fs))
print(json.dumps(out))
"""


def probe(cache_dir):
    """Build and render quake.py in a fresh process against `cache_dir`.

    A separate process and not a reload: ftdata.CACHE_DIR is read from the
    environment at import, so a test that sets the variable and re-imports is
    testing the value it already had. That has passed here when it should not
    have.
    """
    out = tempfile.mktemp(suffix=".npy")
    env = dict(os.environ, FT_DATA_CACHE=cache_dir, FT_PROBE_OUT=out,
               FT_DATA_BLOBS=cache_dir)
    src = PROBE % {"here": HERE}
    res = subprocess.run([sys.executable, "-c", src], env=env,
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        return None, None, res.stderr.strip()[-300:]
    info = json.loads(res.stdout.strip().splitlines()[-1])
    fs = np.load(out)
    os.unlink(out)
    return info, fs, None


def test_states_in_separate_processes(cache_dir):
    print("\nfresh, stale and absent, each in its own process")
    now = time.time()
    tmp = tempfile.mkdtemp(prefix="ftq-states")

    fresh = write_cache(os.path.join(tmp, "fresh"),
                        [event(38.79, -122.75, 1.4, now - 1800.0,
                               "THE GEYSERS, CA")], now=now, age=300.0,
                        baseline=event(37.1157, -122.1125, 4.6,
                                       now - 129 * 86400.0, "BOULDER CREEK, CA"))
    # Four hours is past the 3600 s TTL but inside three times it: aging.
    aging = write_cache(os.path.join(tmp, "aging"),
                        [event(38.79, -122.75, 1.4, now - 14400.0,
                               "THE GEYSERS, CA")], now=now, age=7200.0,
                        baseline=None)
    # Two days is well past three TTLs: the events stop being drawn.
    stale = write_cache(os.path.join(tmp, "stale"),
                        [event(38.79, -122.75, 1.4, now - 2 * 86400.0,
                               "THE GEYSERS, CA")], now=now, age=2 * 86400.0,
                        baseline=None)
    absent = os.path.join(tmp, "absent")
    os.makedirs(absent)

    results = {}
    for name, path in (("fresh", fresh), ("aging", aging),
                       ("stale", stale), ("absent", absent)):
        info, fs, err = probe(path)
        if err:
            check("%s cache renders" % name, False, err)
            continue
        results[name] = (info, fs)
        check("%s cache renders in its own process" % name,
              info["shape"] == [64, 320, 3], "CACHE_DIR=%s" % info["cache_dir"])
        check("...and the process read the cache it was pointed at",
              info["cache_dir"] == path)
        check("...and quake.py classifies it as %s" % name,
              info["state"] == name, info["state"])

    if len(results) < 4:
        return

    f_fresh = results["fresh"][1][-1]
    f_stale = results["stale"][1][-1]
    f_absent = results["absent"][1][-1]
    f_aging = results["aging"][1][-1]

    check("a fresh cache draws no warning",
          not contains_text(f_fresh, "STALE")
          and not contains_text(f_fresh, "NO USGS DATA"))
    check("an aging cache says OLD and still draws the map",
          contains_text(f_aging, "OLD")
          and results["aging"][0]["lit"] > 400,
          "%d lit" % results["aging"][0]["lit"])
    check("a stale cache says STALE", contains_text(f_stale, "STALE"))
    check("...and says the catalogue is not being drawn",
          contains_text(f_stale, "NOT DRAWN"),
          "with its age next to it")
    check("an absent cache draws the no-data card",
          contains_text(f_absent, "NO USGS DATA"))
    check("...and names the command that fixes it",
          contains_text(f_absent, "FTDATA.PY"))
    check("...and names the directory it looked in",
          contains_text(f_absent, os.path.basename(absent).upper()))

    # The three must be *visibly* different pictures. Two states that render
    # near-identically is the failure this whole section exists to catch.
    def diff(a, b):
        return int(np.abs(a.astype(int) - b.astype(int)).sum())
    check("fresh and absent are different pictures",
          diff(f_fresh, f_absent) > 200000, "%d" % diff(f_fresh, f_absent))
    check("fresh and stale are different pictures",
          diff(f_fresh, f_stale) > 5000, "%d" % diff(f_fresh, f_stale))
    check("stale and absent are different pictures",
          diff(f_stale, f_absent) > 200000, "%d" % diff(f_stale, f_absent))
    for name in ("fresh", "aging", "stale", "absent"):
        check("the %s panel is alive between frames" % name,
              results[name][0]["moves"] > 0,
              "%d" % results[name][0]["moves"])


def test_rubbish():
    print("\nrecords that are wrong rather than missing")
    tmp = tempfile.mkdtemp(prefix="ftq-bad")
    cases = {
        "halfwritten": '{"payload": {"local": ',
        "wrongproduct": json.dumps({"name": "quake-usgs",
                                    "fetched_at": time.time(),
                                    "payload": {"hello": "world"}}),
        "nulls": json.dumps({"name": "quake-usgs", "fetched_at": time.time(),
                             "payload": {"local": None, "world": None}}),
    }
    for name, body in cases.items():
        path = os.path.join(tmp, name)
        os.makedirs(path)
        with open(os.path.join(path, "quake-usgs.json"), "w") as fh:
            fh.write(body)
        _, fs = frames(opts(cache_dir=path), 4)
        check("a %s record draws the no-data card" % name,
              contains_text(fs[-1], "NO USGS DATA"))

    # An event list with holes in it must lose the holes, not the panel.
    path = os.path.join(tmp, "holes")
    now = time.time()
    write_cache(path, [event(38.79, -122.75, 1.4, now - 600.0)], now=now)
    with open(os.path.join(path, "quake-usgs.json")) as fh:
        rec = json.load(fh)
    rec["payload"]["local"]["events"] += [
        {"t": now, "mag": None, "lat": 37.8, "lon": -122.4},
        {"t": now, "mag": 3.0, "lat": None, "lon": None},
    ]
    with open(os.path.join(path, "quake-usgs.json"), "w") as fh:
        json.dump(rec, fh)
    cat = quake.Catalogue(path, now)
    check("events with no magnitude or no location are dropped",
          len(cat.local) == 1, "%d kept of 3" % len(cat.local))
    _, fs = frames(opts(cache_dir=path), 4)
    check("...and the panel still draws", not contains_text(fs[-1], "NO USGS DATA"))


# --------------------------------------------------------------------------
# 7. The wall. No network on this side of it, asserted rather than assumed.
# --------------------------------------------------------------------------

def test_no_network():
    print("\nnothing on this side of the wall touches the network")
    src = open(os.path.join(HERE, "quake.py")).read()
    bad = [w for w in ("urllib", "http.client", "socket", "requests", "ssl")
           if ("import " + w) in src]
    check("quake.py imports no network module", not bad, ",".join(bad))

    before = set(sys.modules)
    ftdata.load("quake-usgs", tempfile.mkdtemp(prefix="ftq-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module either", not bad,
          ",".join(bad))

    # render() must not touch the disk. The reload path is the only thing in
    # here that ever would, and it is supposed to stat rather than read.
    tmp = tempfile.mkdtemp(prefix="ftq-io")
    now = time.time()
    write_cache(tmp, [event(38.79, -122.75, 1.4, now - 600.0)], now=now)
    args = opts(cache_dir=tmp, reload=0)
    r = quake.build(args)
    real_open = open
    opened = []

    import builtins
    builtins.open = lambda *a, **k: (opened.append(a[0]), real_open(*a, **k))[1]
    try:
        for i in range(30):
            r(i / 20.0, i)
    finally:
        builtins.open = real_open
    check("render() opens no files", not opened, ",".join(str(p) for p in opened[:3]))


# --------------------------------------------------------------------------
# 8. Other canvases. A demo that raises on an odd size takes the rotation down.
# --------------------------------------------------------------------------

def test_sizes(cache_dir):
    print("\nother canvas sizes")
    tmp = tempfile.mkdtemp(prefix="ftq-size")
    now = time.time()
    write_cache(tmp, [event(38.79, -122.75, 1.4, now - 600.0),
                      event(37.87, -122.27, 3.1, now - 7200.0)], now=now,
                baseline=event(37.1157, -122.1125, 4.6, now - 129 * 86400.0))
    for w, h in ((320, 64), (256, 64), (160, 64), (320, 32), (128, 32),
                 (640, 128)):
        try:
            _, fs = frames(opts(cache_dir=tmp, width=w, height=h), 4)
            ok = fs[-1].shape == (h, w, 3)
            detail = "%dx%d" % (w, h)
        except Exception as e:                               # noqa: BLE001
            ok, detail = False, repr(e)
        check("renders at %dx%d" % (w, h), ok, detail)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    args = ap.parse_args()
    print("cache: %s" % args.cache_dir)

    test_no_network()
    test_geography()
    test_magnitude()
    test_quiet_is_not_broken()
    test_alert()
    test_rubbish()
    test_states_in_separate_processes(args.cache_dir)
    test_live(args.cache_dir)
    test_sizes(args.cache_dir)

    print("\n%d checks, %d failed%s"
          % (PASSED[0], len(FAILED),
             ", %d skipped" % len(SKIPPED) if SKIPPED else ""))
    for name in FAILED:
        print("  FAILED: %s" % name)
    for name in SKIPPED:
        print("  SKIPPED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
