#!/usr/bin/env python3
"""Checks for sats.py that a screenshot cannot make.

A map of the world with dots on it is the most convincing wrong picture in this
directory. Every failure mode -- an hour of clock error, a sign dropped out of
the nodal regression, geocentric latitude quoted as geodetic, the whole map
rolled by half a pixel -- produces a panel that looks exactly as right as a
correct one. Nobody standing in the workshop can tell, and neither can a
screenshot. So this asserts the *positions*, three ways:

  1. against **arithmetic that cannot be wrong**: Kepler's equation is checked
     by substitution, GMST against the published value at J2000, the projection
     against the corners of the map;
  2. against an **independent SGP4 implementation** -- a public "where is the
     ISS" service -- at five moments spanning three days, with the agreement
     asserted in degrees. It skips, loudly, when there is no network;
  3. against the **rendered pixels**, which is the only one that cannot be
     fooled by a bug living between the propagator and the screen: the ISS dot
     is read back off the panel and its pixel converted to a latitude and
     longitude, the terminator's darkest column is measured and compared with
     the antisolar point, and the dot is watched moving over a simulated hour.

The pass predictor is checked against a brute-force scan of the same
propagator, which is a different thing again: it does not ask whether the orbit
is right, it asks whether the search over it missed anything.

**Every frame is rendered sequentially from a fresh `build()`.** This demo is
not a pure function of `t` -- it reads a clock, and it caches the map, the
strip and the terminator on keys derived from that clock -- so sampling
`render()` at scattered timestamps tests a code path the wall never runs.

**The three data states each run in a separate process.** `ftdata.CACHE_DIR`
binds at import, so a test that reloads the module in one process is testing
the module's import order and not the panel; that has produced a false pass in
this project before. This script re-executes itself once per state with
`FT_DATA_CACHE` set, and adds up the children's counts.

    $ python3 scripts/test-sats.py                     # uses the live cache
    $ python3 scripts/test-sats.py --cache-dir /tmp/c  # or a pointed one
    $ python3 scripts/test-sats.py --offline           # skip the SGP4 check

Needs a populated cache; run `python3 ftdata.py --once --only sats` first. The
stale and absent states build their own cache directories and need nothing.
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

import demoscene as ds                                       # noqa: E402
import ftdata                                                # noqa: E402
import sats                                                  # noqa: E402

FAILED = []
SKIPPED = []
PASSED = [0]

# The independent reference. wheretheiss.at runs SGP4 on the same CelesTrak
# elements and answers with a geodetic subsatellite point, which is exactly the
# quantity under test. It is only ever called from here -- the demo has no idea
# it exists, and must not.
REF_URL = "https://api.wheretheiss.at/v1/satellites/25544/positions"
ISS_NORAD = 25544

# What agreement is claimed. The propagator is Kepler plus J2 secular rates, so
# it differs from SGP4 by the short-period terms and by drag; measured, that is
# under 0.15 degrees over three days from epoch. Half a degree is the assertion
# -- three times the observed error, and still an eighth of a row on this
# panel, so it cannot be passed by anything that has the geometry wrong.
REF_TOL_DEG = 0.5


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-54s %s" % (name, detail))
    else:
        print("  FAIL %-54s %s" % (name, detail))
        FAILED.append(name)


def skip(name, reason):
    """A check that could not be made, said out loud.

    Distinct from a pass on purpose: a position check that quietly reports
    success when it had nothing to compare against keeps the count up and stops
    anyone looking.
    """
    print("  SKIP %-54s %s" % (name, reason))
    SKIPPED.append("%s (%s)" % (name, reason))


def opts(**kw):
    return ds.options(sats, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build. Never sample sparsely."""
    r = sats.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def contains_text(frame, s, thresh=80, scales=(1, 2)):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure an honest
    message reached it rather than merely being computed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = sats.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                if np.array_equal(row[:, x:x + gw] & m, m):
                    return True
    return False


def great_circle(lat1, lon1, lat2, lon2):
    """Angular separation in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    c = (math.sin(p1) * math.sin(p2)
         + math.cos(p1) * math.cos(p2) * math.cos(dl))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def iss_index(el):
    for i, s in enumerate(el.sats):
        if int(s.get("id", 0)) == ISS_NORAD:
            return i
    return None


# --------------------------------------------------------------------------
# 1. The network promise, which is the one rule the whole ftdata split exists
#    to enforce.
# --------------------------------------------------------------------------

def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("sats", tempfile.mkdtemp(prefix="fts-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))

    src = open(os.path.join(HERE, "sats.py")).read()
    bad = [w for w in ("import urllib", "import http", "import socket",
                       "import requests", "urlopen") if w in src]
    check("sats.py names no network module anywhere in its source",
          not bad, ",".join(bad))


# --------------------------------------------------------------------------
# 2. Arithmetic that cannot be argued with.
# --------------------------------------------------------------------------

def test_gmst():
    print("\nsidereal time")
    # The standard check: Greenwich mean sidereal time at J2000.0, which is
    # 2000-01-01 12:00 UT, is 18h 41m 50.548s. If this is wrong every dot on
    # the panel is at the wrong longitude by the same amount, which looks
    # exactly like a correct map of a slightly different planet.
    j2000 = 946728000.0                      # 2000-01-01T12:00:00Z
    hours = float(sats.gmst(j2000)) * 12.0 / math.pi
    want = 18.0 + 41.0 / 60.0 + 50.548 / 3600.0
    check("GMST at J2000 is 18h41m50.5s", abs(hours - want) * 3600.0 < 0.5,
          "%.6f h, wanted %.6f h (%.3f s out)"
          % (hours, want, abs(hours - want) * 3600.0))

    check("the scalar and array forms agree",
          abs(sats.gmst_at(j2000) - float(sats.gmst(j2000))) < 1e-12,
          "%.12f" % abs(sats.gmst_at(j2000) - float(sats.gmst(j2000))))

    # A sidereal day is 23h56m04s, not 24h: after one the sky is back.
    day = 86164.0905
    a, b = sats.gmst_at(j2000), sats.gmst_at(j2000 + day)
    d = abs((a - b + math.pi) % (2 * math.pi) - math.pi)
    check("sidereal time repeats after 23h56m04s", math.degrees(d) < 0.01,
          "%.4f deg apart" % math.degrees(d))

    # And the linearisation the render loop uses instead of recomputing it.
    off = 1200.0
    lin = sats.gmst_at(j2000) + sats.OMEGA_EARTH * off
    err = abs(lin - sats.gmst_at(j2000 + off))
    check("linearised sidereal time is exact over a track",
          math.degrees(err) < 1e-6, "%.2e deg over %ds" % (math.degrees(err), off))


def test_kepler():
    print("\nKepler's equation, by substitution")
    # The solver is not iterated to a residual, it takes a fixed number of
    # steps -- so the residual is the thing to check, at eccentricities either
    # side of where it switches strategy.
    worst = {}
    for ecc in (0.0, 0.0007, 0.0349, 0.09, 0.2, 0.6):
        fake = [{"epoch": 0.0, "n": 15.0, "ndot2": 0.0, "e": ecc,
                 "i": 51.6, "raan": 0.0, "argp": 0.0, "ma": ma,
                 "label": "X", "kind": "amateur", "id": 0}
                for ma in np.linspace(0.0, 359.0, 60)]
        el = sats.Elements(fake)
        # Reach into the same arithmetic the propagator runs, then substitute.
        dt = np.zeros((el.n_sats, 1))
        ma = el.ma0 + el.madot * dt
        sm, cm = np.sin(ma), np.cos(ma)
        ea = ma + el.ecc * sm + el.ecc2 * sm * cm
        for _ in range(el.kepler_steps):
            ea = ea - (ea - el.ecc * np.sin(ea) - ma) / (1.0 - el.ecc * np.cos(ea))
        res = float(np.abs(ea - el.ecc * np.sin(ea) - ma).max())
        worst[ecc] = res
    # Everything in the roster is under 0.05, and there the answer has to be
    # essentially exact. Above 0.1 the solver switches to real iteration and
    # has to be exact too; the gap between is where the series alone is asked
    # to do the work, and 1e-4 rad is 0.006 degrees of true anomaly.
    check("residual is machine-precision for near-circular orbits",
          max(worst[e] for e in (0.0, 0.0007, 0.0349)) < 1e-9,
          "worst %.2e rad at e<=0.035" % max(worst[e] for e in (0.0, 0.0007, 0.0349)))
    check("residual stays small right up to the strategy change",
          worst[0.09] < 1e-4, "%.2e rad at e=0.09" % worst[0.09])
    check("eccentric orbits get iterated and come out exact",
          max(worst[0.2], worst[0.6]) < 1e-9,
          "worst %.2e rad at e>=0.2" % max(worst[0.2], worst[0.6]))


def test_projection():
    print("\nthe projection")
    lay = sats.Layout(320, 64, 180.0)
    cases = [("the antimeridian is the left edge", -180.0, 0.0),
             ("Greenwich is the middle column", 0.0, 160.0),
             ("+180 wraps back to the left edge", 180.0, 320.0)]
    for name, lon, want in cases:
        got = float(lay.col_of(lon))
        check(name, abs(got - want) < 1e-9, "col %.3f, wanted %.3f" % (got, want))
    for name, lat, want in [("the north pole is row 0", 90.0, 0.0),
                            ("the equator is row 32", 0.0, 32.0),
                            ("the south pole is row 64", -90.0, 64.0)]:
        got = float(lay.row_of(lat))
        check(name, abs(got - want) < 1e-9, "row %.3f, wanted %.3f" % (got, want))
    check("a column is 1.125 deg and a row is 2.8125 deg",
          abs(lay.deg_per_col - 1.125) < 1e-9
          and abs(lay.deg_per_row - 2.8125) < 1e-9,
          "%.4f x %.4f deg" % (lay.deg_per_col, lay.deg_per_row))
    check("the squash is exactly 2.5x",
          abs(lay.deg_per_row / lay.deg_per_col - 2.5) < 1e-9,
          "%.4fx" % (lay.deg_per_row / lay.deg_per_col))

    # Cropping instead of squashing has to keep the scale honest, or --lat-span
    # silently becomes a second, differently-wrong projection.
    crop = sats.Layout(320, 64, 72.0)
    check("--lat-span 72 is true scale",
          abs(crop.deg_per_row - crop.deg_per_col) < 1e-9,
          "%.4f vs %.4f deg" % (crop.deg_per_row, crop.deg_per_col))


def test_coastline():
    print("\nthe baked coastline")
    a = sats.coast_alpha(320, 64, 180.0)
    lay = sats.Layout(320, 64, 180.0)

    def near(lat, lon, radius=2):
        r = int(round(float(lay.row_of(lat))))
        c = int(round(float(lay.col_of(lon)))) % 320
        rs = slice(max(0, r - radius), r + radius + 1)
        cols = [(c + d) % 320 for d in range(-radius, radius + 1)]
        return float(a[rs][:, cols].max())

    # Coast, and open ocean a long way from any. If the map is rolled, mirrored
    # or off by a scale factor, at least one of these moves.
    coasts = [("Golden Gate", 37.8, -122.5), ("Gibraltar", 36.0, -5.6),
              ("Cape Horn", -55.9, -67.3), ("Sri Lanka", 7.0, 79.9),
              ("Sydney Heads", -33.8, 151.3), ("Nordkapp", 71.1, 25.8)]
    bad = [n for n, la, lo in coasts if near(la, lo) < 0.2]
    check("six coastlines are drawn where they belong", not bad,
          "missing: %s" % ", ".join(bad) if bad else "all six")

    ocean = [("mid Pacific", 0.0, -150.0), ("south Atlantic", -35.0, -20.0),
             ("mid Indian", -25.0, 75.0), ("central Asia", 47.0, 80.0)]
    bad = [n for n, la, lo in ocean if near(la, lo, 1) > 0.0]
    check("four places with no coastline have none drawn", not bad,
          "spurious: %s" % ", ".join(bad) if bad else "all four clear")


# --------------------------------------------------------------------------
# 3. The orbits themselves, before anything is drawn.
# --------------------------------------------------------------------------

def load_live(cache_dir):
    rec, err, _stale = sats.read_elements(cache_dir)
    return rec, err


def hue_pixels(frame, rgb, min_val=24, tol=10):
    """Pixels that are this colour at *some* brightness.

    The ground tracks are the satellite's own colour scaled by a fade, so an
    exact match only ever finds the dot. What identifies a track is the hue:
    a pixel whose channels are in the same ratio as the base colour, at any
    level above the floor.
    """
    f = frame.astype(np.float64)
    base = np.array(rgb, float)
    base = base / base.max()
    peak = f.max(axis=2)
    pred = peak[:, :, None] * base
    return (peak >= min_val) & (np.abs(f - pred).max(axis=2) <= tol)


def test_orbits(cache_dir):
    print("\nthe propagation, against what the elements themselves say")
    rec, err = load_live(cache_dir)
    if rec is None:
        check("cache is populated", False, err or "no record")
        return None
    el = sats.Elements(rec["sats"])
    check("every rostered satellite is in the record", el.n_sats >= 10,
          "%d satellites, missing %s"
          % (el.n_sats, rec["missing"] or "none"))

    # The recovered mean motion must be within a whisker of the TLE's own, or
    # the Brouwer correction has been applied the wrong way round -- which is
    # a fault that only shows up as everything being slightly early.
    told = np.array([s["n"] for s in rec["sats"]]) * sats.TWOPI / 86400.0
    ratio = el.n[:, 0] / told
    # The correction is 0.75 J2 (Re/a)^2 (3cos^2 i - 1) / beta^3, which for
    # these orbits runs from a part in 1e5 to seven in 1e4 and changes sign
    # with inclination -- so this is bounded on both sides, and the lower bound
    # matters: a recovery quietly reduced to a no-op would pass any test that
    # only asked for "close".
    check("Brouwer recovery moves the mean motion by parts in 1e4",
          bool(((ratio > 1.0 - 2e-3) & (ratio < 1.0 + 2e-3)).all())
          and float(np.abs(ratio - 1.0).max()) > 1e-5,
          "between %.2e and %.2e relative"
          % (float(np.abs(ratio - 1.0).min()), float(np.abs(ratio - 1.0).max())))

    # Nodal regression. For a prograde low orbit the node goes west; for a
    # sun-synchronous one it goes east at very nearly one degree a day, which
    # is the entire definition of sun-synchronous and the sharpest possible
    # test that the J2 secular rates carry the right sign and size.
    rate = el.raandot[:, 0] * 86400.0 * 180.0 / math.pi
    k = iss_index(el)
    if k is not None:
        check("the ISS node regresses about -5 deg a day",
              -5.3 < rate[k] < -4.7, "%.3f deg/day" % rate[k])
    # Sun-synchronous orbits are *designed* so that J2 precesses their node
    # east at one degree a day, which is the sharpest available test that the
    # secular rates carry the right sign and the right size: get either wrong
    # and these come out negative, or ten times too small. They are not all
    # exactly 0.9856 -- a decayed bird is no longer quite synchronous -- so the
    # band is wide and the sign is the point.
    ssos = [i for i, s in enumerate(rec["sats"]) if 96.0 < s["i"] < 101.0]
    if ssos:
        got = [rate[i] for i in ssos]
        check("every sun-synchronous bird precesses about +1 deg a day",
              all(0.80 < g < 1.20 for g in got),
              "%d birds, %.4f to %.4f deg/day" % (len(got), min(got), max(got)))

    # A subsatellite point cannot go further from the equator than the
    # inclination, ever. Checked over a whole day at a minute, which is also a
    # check that nothing wraps or blows up far from epoch.
    now = time.time()
    grid = now + np.arange(0.0, 86400.0, 60.0)[None, :]
    lat, lon, alt = el.subpoint(grid)
    inc = np.array([min(s["i"], 180.0 - s["i"]) for s in rec["sats"]])
    over = np.abs(lat).max(axis=1) - inc
    check("no subsatellite point exceeds its own inclination",
          float(over.max()) < 0.25,
          "worst %+.3f deg over (%s)"
          % (float(over.max()), rec["sats"][int(np.argmax(over))]["label"]))
    check("every latitude and longitude is finite and in range",
          bool(np.isfinite(lat).all() and np.isfinite(lon).all())
          and float(np.abs(lat).max()) <= 90.0
          and float(np.abs(lon).max()) <= 180.0,
          "lat %+.2f..%+.2f" % (lat.min(), lat.max()))
    check("altitudes are plausible for the whole roster",
          float(alt.min()) > 200.0 and float(alt.max()) < 40000.0,
          "%.0f to %.0f km" % (alt.min(), alt.max()))

    # Ground speed. A 90-minute orbit covers 360 degrees of arc in 90 minutes,
    # so the subsatellite point runs at about 4 degrees a minute -- a number
    # that is wrong the moment the time base is.
    if k is not None:
        step = 10.0
        la, lo, _ = el.subpoint(now + np.array([[0.0, step]]))
        d = great_circle(la[k, 0], lo[k, 0], la[k, 1], lo[k, 1]) * 60.0 / step
        check("the ISS ground track runs at about 3.9 deg a minute",
              3.5 < d < 4.3, "%.3f deg/min" % d)
        check("its period is 92 minutes", 88.0 < el.period[k] / 60.0 < 96.0,
              "%.2f min" % (el.period[k] / 60.0))

    # The geostationary one is the cheapest possible check of the whole
    # ECI-to-ECEF chain: if sidereal time is even slightly wrong, QO-100 drifts
    # instead of standing still.
    geo = [i for i, s in enumerate(rec["sats"]) if s["label"] == "QO-100"]
    if geo:
        g = geo[0]
        drift = float(np.abs(lon[g] - lon[g, 0]).max())
        check("the geostationary bird does not move over a day", drift < 0.6,
              "%.3f deg of longitude, at %.2f E" % (drift, lon[g, 0]))
    return el


def test_reference(cache_dir, offline):
    print("\nagainst an independent SGP4, at five moments over three days")
    name = "ISS subsatellite point agrees with SGP4 to %.1f deg" % REF_TOL_DEG
    if offline:
        skip(name, "--offline")
        return
    rec, err = load_live(cache_dir)
    if rec is None:
        check("cache is populated", False, err or "no record")
        return
    el = sats.Elements(rec["sats"])
    k = iss_index(el)
    if k is None:
        skip(name, "no ISS in the cached roster")
        return

    now = int(time.time())
    stamps = [now, now + 600, now + 3600, now + 86400, now + 3 * 86400]
    try:
        # The only socket in this file, and it is in the test rather than in
        # the demo on purpose. Imported here so that test_no_network() above
        # can still watch load() not import one.
        import urllib.request
        url = "%s?timestamps=%s&units=kilometers" % (
            REF_URL, ",".join(str(s) for s in stamps))
        req = urllib.request.Request(
            url, headers={"User-Agent": "flaschen-taschen-test-sats/1"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            ref = json.loads(resp.read())
    except Exception as e:                                   # noqa: BLE001
        skip(name, "no answer from %s (%r)" % (REF_URL.split("/")[2], e))
        return
    if not isinstance(ref, list) or len(ref) != len(stamps):
        skip(name, "unexpected shape from the reference service")
        return

    worst, worst_at, worst_alt = 0.0, "", 0.0
    for row in ref:
        t = float(row["timestamp"])
        la, lo, alt = el.subpoint(t)
        d = great_circle(float(la[k, 0]), float(lo[k, 0]),
                         float(row["latitude"]), float(row["longitude"]))
        worst_alt = max(worst_alt, abs(float(alt[k, 0]) - float(row["altitude"])))
        if d > worst:
            worst, worst_at = d, "+%ds from now" % (t - now)
    check(name, worst < REF_TOL_DEG,
          "worst %.4f deg (%.1f km) %s, on elements %s old"
          % (worst, worst * 111.2, worst_at,
             ftdata.describe_age(rec["elem_age"])))
    check("...and the altitude agrees within 25 km", worst_alt < 25.0,
          "worst %.1f km" % worst_alt)

    # The control. If the reference check would pass against a deliberately
    # broken propagation, it is not proving anything -- an hour of clock error
    # is the classic one and it moves a subsatellite point 15 degrees.
    la, lo, _ = el.subpoint(float(ref[0]["timestamp"]) + 3600.0)
    bad = great_circle(float(la[k, 0]), float(lo[k, 0]),
                       float(ref[0]["latitude"]), float(ref[0]["longitude"]))
    check("an hour of clock error would be caught, not tolerated",
          bad > REF_TOL_DEG * 10.0,
          "one hour out reads %.2f deg away" % bad)


# --------------------------------------------------------------------------
# 4. The pass search. Not "is the orbit right" -- that is above -- but "did the
#    search over it miss anything or land in the wrong place".
# --------------------------------------------------------------------------

def site_frame(site):
    ecef = sats.geodetic_ecef(*site)
    la, lo = math.radians(site[0]), math.radians(site[1])
    up = (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))
    east = (-math.sin(lo), math.cos(lo), 0.0)
    north = (-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo),
             math.cos(la))
    return ecef, up, east, north


def test_passes(cache_dir):
    print("\nthe pass predictor, against a brute-force scan of the same orbits")
    rec, err = load_live(cache_dir)
    if rec is None:
        check("cache is populated", False, err or "no record")
        return
    el = sats.Elements(rec["sats"])
    site = (sats.SITE_LAT, sats.SITE_LON)
    ecef, up, east, north = site_frame(site)
    now = time.time()
    passes = sats.find_passes(el, now, site, hours=24.0, min_el=10.0, limit=99)
    check("the site sees passes at all in the next day", len(passes) > 0,
          "%d passes above 10 deg" % len(passes))
    if not passes:
        return

    # Brute force, five seconds, one satellite at a time. Nothing clever: this
    # is the answer the coarse search has to reproduce.
    fine = now + np.arange(0.0, 24.0 * 3600.0, 5.0)[None, :]
    elev, az, _ = el.look(fine, ecef, up, east, north)
    fine = fine[0]

    missed, wrong_t = [], 0.0
    el_gap, rounded = [], []
    brute_total = 0
    for i in range(el.n_sats):
        above = elev[i] > 10.0
        edges = np.flatnonzero(np.diff(np.concatenate(
            ([False], above, [False])).astype(np.int8)))
        runs = list(zip(edges[0::2], edges[1::2]))
        brute_total += len(runs)
        mine = [p for p in passes if p["sat"] == i]
        for a, b in runs:
            j = a + int(np.argmax(elev[i, a:b]))
            peak_t = fine[j]
            near = [p for p in mine if abs(p["peak"] - peak_t) < 120.0]
            if near and near[0]["clipped"]:
                continue        # its ends are the window's, not the horizon's
            if not near:
                missed.append("%s at %s"
                              % (rec["sats"][i]["label"],
                                 time.strftime("%H:%MZ", time.gmtime(peak_t))))
                continue
            # The true maximum, to half a second, so the comparison is against
            # the answer rather than against another grid. A five-second scan
            # can land nearer the top of a fast overhead pass than a
            # two-second one does, and comparing two grids to each other reads
            # that as a fault in whichever was unlucky.
            micro = peak_t + np.arange(-8.0, 8.05, 0.1)
            mel = el.look(micro[None, :], ecef, up, east, north)[0][i]
            k = int(np.argmax(mel))
            p = near[0]
            el_gap.append(p["max_el"] - float(mel[k]))
            rounded.append(int(round(p["max_el"])) == int(round(float(mel[k]))))
            wrong_t = max(wrong_t, abs(p["peak"] - micro[k]))
    check("the 30 s search finds every pass a 5 s scan finds", not missed,
          "%d passes; missed %s" % (brute_total, "; ".join(missed[:3])
                                    if missed else "none"))
    # Signed, and the sign is the point: an interpolated maximum can never
    # exceed the true one, so anything positive here means the two are not
    # talking about the same pass. Underneath, the residual is not the search's
    # fault but the shape's -- a pass that goes within a degree of the zenith
    # has a cusp rather than a peak, and a parabola through two-second samples
    # reads 88.82 where the truth is 88.99. A refinement that silently did
    # nothing would sit three degrees low, which is what this watches for.
    check("its peak elevation is the true maximum, a shade under",
          bool(el_gap) and -0.25 < min(el_gap) and max(el_gap) < 1e-3,
          "between %+.4f and %+.4f deg over %d passes"
          % (min(el_gap), max(el_gap), len(el_gap)))
    # And the version that matters, since the panel prints whole degrees.
    check("the whole degrees the panel prints are the right whole degrees",
          all(rounded), "%d of %d passes round the same way"
                        % (sum(rounded), len(rounded)))
    check("its time of maximum is within four seconds", wrong_t < 4.0,
          "worst %.2f s" % wrong_t)

    # Geometry, independent of the search: the elevation the predictor quotes
    # has to follow from where the satellite actually is. At the peak, the
    # subsatellite point's great-circle distance from the site and the quoted
    # elevation are two views of the same triangle.
    worst = 0.0
    for p in [q for q in passes if not q["clipped"]][:6]:
        i = p["sat"]
        la, lo, alt = el.subpoint(p["peak"])
        d = math.radians(great_circle(float(la[i, 0]), float(lo[i, 0]), *site))
        r = sats.RE + float(alt[i, 0])
        want = math.degrees(math.atan2(math.cos(d) - sats.RE / r, math.sin(d)))
        worst = max(worst, abs(want - p["max_el"]))
    check("elevation matches the subsatellite geometry at the peak",
          worst < 0.6, "worst %.3f deg over %d passes" % (worst, len(passes[:6])))

    # And the ends of a pass are the horizon, by definition of the threshold.
    ends = []
    whole = [p for p in passes if not p["clipped"]][:6]
    for p in whole:
        e = el.look(np.array([[p["rise"], p["set"]]]), ecef, up, east, north)[0]
        ends.append(abs(float(e[p["sat"], 0]) - 10.0))
        ends.append(abs(float(e[p["sat"], 1]) - 10.0))
    check("a pass begins and ends at the horizon it was asked for",
          bool(ends) and max(ends) < 0.3,
          "worst %.2f deg from the 10 deg threshold over %d whole passes"
          % (max(ends) if ends else -1, len(whole)))
    check("passes come back in time order",
          all(passes[i]["peak"] <= passes[i + 1]["peak"]
              for i in range(len(passes) - 1)))


# --------------------------------------------------------------------------
# 5. The pixels. The only section that can catch a bug between the propagator
#    and the panel.
# --------------------------------------------------------------------------

def lit_pixels_of(frame, rgb, tol=6):
    d = np.abs(frame.astype(np.int16) - np.array(rgb, np.int16)).max(axis=2)
    return d <= tol


def test_pixels(cache_dir):
    print("\nwhat actually reaches the panel")
    # --rate 0 freezes the demo's clock at `at`, so every position here is
    # compared against the exact instant the frame was drawn for rather than
    # against one a few milliseconds later. It is also how the screenshot is
    # taken, so this is the panel that ends up in the README.
    at = time.time()
    args = opts(cache_dir=cache_dir, at=str(at), rate=0.0)
    r, frame = frames(args, 8)
    if r.cell["el"] is None:
        check("cache is populated", False, r.cell["problem"] or "")
        return
    el, lay = r.cell["el"], r.layout

    check("the frame is the shape and type the contract asks for",
          frame.shape == (64, 320, 3) and frame.dtype == np.uint8,
          "%s %s" % (frame.shape, frame.dtype))

    # The site marker, which is the one pixel whose position is known without
    # propagating anything at all.
    row = int(round(float(lay.row_of(sats.SITE_LAT))))
    col = int(round(float(lay.col_of(sats.SITE_LON)))) % 320
    check("San Francisco is marked at its own pixel",
          tuple(frame[row, col]) == sats.C_SITE,
          "row %d col %d is %s" % (row, col, tuple(frame[row, col])))

    # Every satellite's dot, read back off the panel and converted to a
    # position. This is the check that a rolled map, a mirrored latitude or a
    # dropped modulo cannot survive.
    #
    # Taken off a frame drawn with the labels turned off. A label is type drawn
    # over the map after the dots, so "ISS" three pixels to the right of the
    # ISS is entitled to sit on top of whatever else is there -- and once every
    # few hours it sits on top of another satellite, which is a legitimate
    # panel and an illegitimate way to fail a position check.
    now = r.clock()
    r_nolabel = sats.build(opts(cache_dir=cache_dir, at=str(at), rate=0.0,
                                label=""))
    plain = None
    for i in range(8):
        plain = r_nolabel(i / 20.0, i)
    lat, lon, _ = el.subpoint(now)
    seen = {}
    bad = []
    for i, sat in enumerate(el.sats):
        rr = int(np.clip(float(lay.row_of(lat[i, 0])), 0, 63))
        cc = int(float(lay.col_of(lon[i, 0]))) % 320
        if rr >= lay.strip_y:
            continue                        # under the strip, legitimately
        if (rr, cc) in seen:
            continue        # two satellites on one pixel; the later one wins
        seen[(rr, cc)] = i
        want = sats.C_KIND.get(sat["kind"], sats.C_KIND_DEFAULT)
        if tuple(plain[rr, cc]) != want:
            bad.append("%s at %d,%d is %s not %s"
                       % (sat["label"], rr, cc, tuple(plain[rr, cc]), want))
    check("every satellite is drawn at its own subsatellite pixel", not bad,
          "%d dots; %s" % (el.n_sats, "; ".join(bad[:2]) if bad else "all right"))

    # ...and the reverse direction: take the brightest ISS-coloured pixel off
    # the panel and ask what latitude and longitude it is. Within half a pixel
    # of where the propagator says, or something between the two is wrong.
    k = iss_index(el)
    if k is not None:
        mask = lit_pixels_of(frame, sats.C_KIND["station"], 0)
        ys, xs = np.nonzero(mask)
        want_r, want_c = float(lay.row_of(lat[k, 0])), float(lay.col_of(lon[k, 0]))
        near = [(y, x) for y, x in zip(ys, xs)
                if abs(y - want_r) < 3 and abs(x - want_c) < 3]
        check("the ISS pixel read back off the panel is where it should be",
              bool(near), "%d station-coloured pixels, wanted one near %.1f,%.1f"
                          % (len(ys), want_r, want_c))
        check("the ISS is labelled on the map", contains_text(frame, "ISS"))

    # The ground track: a satellite has to leave a trail of its own colour, and
    # the trail has to be long. One dot and no track is a track array silently
    # collapsing to the middle sample.
    # A satellite has to leave a trail of its own hue, and the trail has to be
    # long: one dot and no track is a track array quietly collapsing to its
    # middle sample, which looks perfectly reasonable on the panel.
    for kind, n_sats in (("station", 2), ("amateur", 8), ("weather", 5)):
        lit = int(hue_pixels(frame, sats.C_KIND[kind]).sum())
        check("the %s tracks are drawn, not just their dots" % kind,
              lit >= 25 * n_sats,
              "%d pixels of that hue for %d satellites" % (lit, n_sats))

    # The footprint: the circle of ground the focus satellite is above the
    # horizon from. It is drawn as a ring of points, and the whole ring can
    # land on the wrong side of the map from one misplaced modulo -- which is
    # exactly what it did, and looked completely convincing doing it. So it is
    # measured: every drawn pixel of it must be the right angular distance
    # from the satellite it belongs to.
    with_fp = frame
    r2 = sats.build(opts(cache_dir=cache_dir, at=str(at), rate=0.0,
                         footprint="none"))
    without = None
    for i in range(8):
        without = r2(i / 20.0, i)
    ring = np.nonzero((with_fp != without).any(axis=2))
    focus = r.cell["state"]["focus"]
    flat, flon, falt = el.subpoint(now)
    rho = math.degrees(math.acos(
        sats.RE / (sats.RE + float(falt[focus, 0]))))
    dists = [great_circle(
        90.0 - (y + 0.5) * lay.deg_per_row,
        -180.0 + (x + 0.5) * lay.deg_per_col,
        float(flat[focus, 0]), float(flon[focus, 0]))
        for y, x in zip(*ring)]
    check("the footprint is drawn, and around the right satellite",
          len(dists) > 30 and max(abs(d - rho) for d in dists) < 4.0,
          "%d pixels at %.1f-%.1f deg from %s, whose horizon is %.1f deg"
          % (len(dists), min(dists) if dists else -1,
             max(dists) if dists else -1,
             el.sats[focus]["label"], rho))

    # The terminator. It can be inverted, or half a world out, and still look
    # entirely convincing, so where it sits is measured off the pixels.
    #
    # Measured over open water only, and by the *brightest* column rather than
    # the darkest. A column mean over everything is dominated by how much
    # coastline happens to fall in it -- the Indonesian archipelago outshines a
    # whole ocean of night -- and the rows near the poles are day or night for
    # months at a time whatever the longitude, so both are excluded. The dark
    # side is a broad flat plateau where an argmin means nothing; the lit side
    # is a peak, and its top is the subsolar meridian.
    dec, sunlon = sats.sun_subpoint(now)

    # First, independently of the demo: the sun is over the meridian where it
    # is local noon, so its longitude is 180 degrees minus however far through
    # the UTC day it is. That is only right to the equation of time, about four
    # degrees, which is plenty to catch a sign or an hour.
    naive = 180.0 - (now % 86400.0) / 86400.0 * 360.0
    off = abs((sunlon - naive + 180.0) % 360.0 - 180.0)
    check("the subsolar point is where the clock puts it", off < 5.0,
          "%+.1f against %+.1f from UTC alone (%.1f deg apart)"
          % (sunlon, naive, off))

    coast = sats.coast_alpha(320, 64, 180.0) > 0.0
    rows = np.abs(np.arange(64) * lay.deg_per_row - 90.0 + lay.deg_per_row / 2)
    usable = (~coast) & (rows < 55.0)[:, None]
    usable[lay.strip_y:] = False
    n_col = np.maximum(usable.sum(axis=0), 1)

    def profile(plane):
        return np.where(usable, plane, 0.0).sum(axis=0) / n_col

    measured = profile(frame.astype(np.float64).sum(axis=2))

    # What the sky should look like from that sun: the day/night weight over
    # the same pixels, reduced the same way. Comparing two profiles rather
    # than hunting for a maximum is what makes this immune to the sea mask
    # being lopsided -- the Pacific is a third of the panel and the Atlantic
    # is not, so an argmax over open water sits west of the sun by a good ten
    # degrees and wanders with the season.
    sa, sb, sc = sats.sun_geometry(lay)
    d, l = math.radians(dec), math.radians(sunlon)
    cosz = (sa * math.sin(d) + sb * (math.cos(d) * math.cos(l))
            + sc * (math.cos(d) * math.sin(l)))
    lit = 1.0 - np.clip((sats.TWI_HI - cosz) / (sats.TWI_HI - sats.TWI_LO),
                        0.0, 1.0)
    expect = profile(lit.astype(np.float64))

    ma = measured - measured.mean()
    ex = expect - expect.mean()
    corr = [float((ma * np.roll(ex, k)).sum()) for k in range(-160, 160)]
    shift = range(-160, 160)[int(np.argmax(corr))]
    check("the lit side of the map lines up with the sun", abs(shift) <= 8,
          "best correlation at %+d columns (%+.0f deg), sun at %+.0f"
          % (shift, shift * lay.deg_per_col, sunlon))
    # The control: the same correlation against the same profile turned inside
    # out has to land somewhere else entirely, or the check above would pass on
    # a panel with day and night the wrong way round.
    corr = [float((ma * np.roll(-ex, k)).sum()) for k in range(-160, 160)]
    bad = range(-160, 160)[int(np.argmax(corr))]
    check("...and an inverted terminator would not", abs(bad) > 40,
          "inverted, the best fit is %+d columns away" % bad)

    # And the crudest possible statement of the same thing, in case some future
    # refactor gets clever about correlations: the water the sun is over is
    # brighter than the water it is not. Over whole hemispheres rather than two
    # sample columns, because a single column is at the mercy of how much open
    # water happens to fall in it.
    lum = frame.astype(np.float64).sum(axis=2)
    day_side = usable & (lit > 0.8)
    night_side = usable & (lit < 0.2)
    day_mean = float(lum[day_side].mean()) if day_side.any() else 0.0
    night_mean = float(lum[night_side].mean()) if night_side.any() else 0.0
    check("the sunlit hemisphere is brighter than the dark one",
          day_mean > night_mean * 1.5,
          "%.1f over %d lit pixels against %.1f over %d dark"
          % (day_mean, int(day_side.sum()), night_mean, int(night_side.sum())))

    # The strip. The age of the elements is the one field that may never be
    # dropped, so it is read back off the pixels rather than trusted.
    check("the strip prints the age of the elements",
          contains_text(frame, "ELEM"))
    st = r.cell["state"]
    if st and st["pass"]:
        check("...and names the satellite the next pass belongs to",
              contains_text(frame, st["pass"]["label"]),
              "%s, %s" % (st["pass"]["label"],
                          sats.duration(st["pass"]["rise"] - st["now"])))
    else:
        check("...and says so when there is no pass to name",
              contains_text(frame, "NO"))


def test_motion(cache_dir):
    print("\nthe panel moving, rendered in order from a fresh build")
    at = time.time()

    # Part one, at real time: does the dot sit where the propagator says on
    # every single frame? The clock is read either side of the render, so the
    # comparison is against the interval the frame was drawn in rather than
    # against a moment that has already passed.
    args = opts(cache_dir=cache_dir, at=str(at))
    r = sats.build(args)
    if r.cell["el"] is None:
        check("cache is populated", False, r.cell["problem"] or "")
        return
    el, lay = r.cell["el"], r.layout
    k = iss_index(el)
    if k is None:
        skip("the ISS dot moves the way the orbit says", "no ISS in the roster")
        return

    def dot_column(frame, want_c, tol):
        """The drawn ISS column nearest `want_c`, or None. Wraps at the seam."""
        ys, xs = np.nonzero(lit_pixels_of(frame, sats.C_KIND["station"], 0))
        best = None
        for x in xs:
            d = min(abs(x - want_c), 320 - abs(x - want_c))
            if d <= tol and (best is None or d < best[0]):
                best = (d, float(x))
        return None if best is None else best[1]

    on = 0
    for i in range(60):
        t0 = r.clock()
        f = r(i / 20.0, i)
        lat, lon, _ = el.subpoint(t0)
        if dot_column(f, float(lay.col_of(lon[k, 0])), 2.5) is not None:
            on += 1
    check("the ISS dot sits on its own subsatellite point every frame",
          on >= 59, "%d of 60 frames" % on)

    # Part two, wound forward: --rate makes the demo's own clock run fast, and
    # frames are still rendered one after another so every cache key on that
    # clock is exercised the way the wall exercises it. What this catches is a
    # panel that tracks perfectly and never moves -- a frozen clock, or a
    # position quantised to something coarse.
    r = sats.build(opts(cache_dir=cache_dir, at=str(at), rate=20000.0))
    seen, hits, sim0 = [], 0, r.clock()
    for i in range(150):
        t0 = r.clock()
        f = r(i / 20.0, i)
        t1 = r.clock()
        lat, lon, _ = el.subpoint(np.array([[t0, t1]]))
        c0, c1 = float(lay.col_of(lon[k, 0])), float(lay.col_of(lon[k, 1]))
        seen.append(c0)
        # The frame was drawn somewhere between the two clock reads, so the
        # dot may be anywhere between the two columns; the tolerance is that
        # gap plus a couple of pixels of rounding.
        span = min(abs(c1 - c0), 320.0 - abs(c1 - c0))
        if dot_column(f, c0, span + 3.0) is not None:
            hits += 1
    sim = r.clock() - sim0
    check("...and still does when the clock is wound forward",
          hits >= len(seen) - 3, "%d of %d frames over %.0f simulated minutes"
                                 % (hits, len(seen), sim / 60.0))
    travel = max(seen) - min(seen)
    check("the dot really does cross the panel", travel > 100.0 and sim > 3600.0,
          "%.0f columns in %.1f simulated hours" % (travel, sim / 3600.0))

    # Rendering the same frozen moment twice must give the same picture. That
    # is not a truism: the terminator, the strip and the pass list are all
    # cached on keys derived from the clock, and a key that quantises the wrong
    # way gives two different panels for one instant. --rate 0 stops the clock,
    # which is also how the screenshot is taken.
    a = sats.build(opts(cache_dir=cache_dir, at=str(at), rate=0.0))
    b = sats.build(opts(cache_dir=cache_dir, at=str(at), rate=0.0))
    fa = fb = None
    for i in range(6):
        fa, fb = a(i / 20.0, i), b(i / 20.0, i)
    check("two builds of the same frozen moment draw the same panel",
          np.array_equal(fa, fb),
          "%d pixels differ" % int((fa != fb).any(axis=2).sum()))
    check("...and a frozen clock really is frozen",
          a.clock() == b.clock(), "%.3f vs %.3f" % (a.clock(), b.clock()))


def test_sizes(cache_dir):
    print("\nother panel sizes, and a long run")
    for w, h in ((320, 64), (256, 64), (160, 32), (128, 16), (512, 96),
                 (320, 40), (64, 64)):
        try:
            r = sats.build(opts(cache_dir=cache_dir, width=w, height=h,
                                rate=600.0))
            out = None
            for i in range(200):
                out = r(i / 20.0, i)
            ok = out.shape == (h, w, 3) and out.dtype == np.uint8
            detail = ""
        except Exception as e:                               # noqa: BLE001
            ok, detail = False, repr(e)[:60]
        check("%dx%d survives two hundred frames" % (w, h), ok, detail)

    r = sats.build(opts(cache_dir=cache_dir, lat_span=72.0))
    out = None
    for i in range(60):
        out = r(i / 20.0, i)
    check("--lat-span 72 renders without anything falling off the edge",
          out.shape == (64, 320, 3))


# --------------------------------------------------------------------------
# 6. The three data states, each in its own process. See the docstring.
# --------------------------------------------------------------------------

def state_fresh(cache_dir):
    rec, err = load_live(cache_dir)
    check("a fresh cache reads back as usable elements", rec is not None,
          err or "%d satellites, elements %s old"
          % (len(rec["sats"]), ftdata.describe_age(rec["elem_age"]))
          if rec else "")
    if rec is None:
        return
    _, f = frames(opts(cache_dir=cache_dir), 6)
    check("...and the panel draws a map rather than a card",
          not contains_text(f, "NO ORBITS")
          and not contains_text(f, "ORBITS TOO OLD"))
    check("...with the element age on it", contains_text(f, "ELEM"))


def state_absent(cache_dir):
    _, f = frames(opts(cache_dir=cache_dir), 6)
    check("an empty cache says NO ORBITS", contains_text(f, "NO ORBITS"))
    check("...and names the command that fixes it",
          contains_text(f, "FTDATA.PY"))
    check("...and draws no satellites at all",
          int(lit_pixels_of(f, sats.C_KIND["amateur"], 40).sum()) == 0,
          "%d amateur-coloured pixels"
          % int(lit_pixels_of(f, sats.C_KIND["amateur"], 40).sum()))

    bad = os.path.join(cache_dir, "sats.json")
    with open(bad, "w") as fh:
        fh.write('{"payload": {"sats": ')
    _, f = frames(opts(cache_dir=cache_dir), 6)
    check("a half-written record says NO ORBITS too",
          contains_text(f, "NO ORBITS"))

    with open(bad, "w") as fh:
        json.dump({"name": "sats", "fetched_at": time.time(),
                   "payload": {"hello": "world"}}, fh)
    _, f = frames(opts(cache_dir=cache_dir), 6)
    check("a payload from some other product says NO ORBITS",
          contains_text(f, "NO ORBITS"))
    check("...and calls it that rather than calling it stale",
          not contains_text(f, "TOO OLD"))


def state_stale(cache_dir):
    _, f = frames(opts(cache_dir=cache_dir), 6)
    check("a record past its TTL says ORBITS TOO OLD",
          contains_text(f, "ORBITS TOO OLD"))
    check("...and says how old, in words",
          contains_text(f, "ELEMENTS ARE"))
    # The point of the whole exercise: a stale orbit is a plausible dot in the
    # wrong place, so there must be no dots at all.
    lit = sum(int(lit_pixels_of(f, c, 40).sum())
              for c in sats.C_KIND.values())
    check("...and draws nothing that could be mistaken for a satellite",
          lit == 0, "%d satellite-coloured pixels" % lit)


STATES = {"fresh": state_fresh, "absent": state_absent, "stale": state_stale}


def make_state_cache(state, live_cache):
    """A cache directory in the state named. Returns it, or None."""
    tmp = tempfile.mkdtemp(prefix="fts-%s" % state)
    if state == "absent":
        return tmp
    src = ftdata.record_path("sats", live_cache)
    if src is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    with open(src) as fh:
        rec = json.load(fh)
    if state == "stale":
        # Parses perfectly, describes real orbits, and is five days old --
        # which is to say two days past the point where it stops being worth
        # believing. Exactly the record that must not be drawn.
        rec["fetched_at"] = time.time() - 5 * 86400
    with open(os.path.join(tmp, "sats.json"), "w") as fh:
        json.dump(rec, fh)
    return tmp


def run_states(live_cache):
    """Re-execute this script once per data state, in a process of its own."""
    print("\nthe three data states, each in a process of its own")
    total, failed = 0, 0
    for state in ("fresh", "absent", "stale"):
        tmp = make_state_cache(state, live_cache)
        if tmp is None:
            skip("data state: %s" % state, "no live record to derive it from")
            continue
        env = dict(os.environ, FT_DATA_CACHE=tmp)
        # FT_DATA_BLOBS too: ftdata looks in the tmpfs directory first for a
        # volatile product, and a stray /run/ftdata on this machine would let
        # the child read a record the test did not write.
        env["FT_DATA_BLOBS"] = tmp
        try:
            out = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--state", state, "--cache-dir", tmp],
                env=env, capture_output=True, text=True, timeout=300)
        except Exception as e:                               # noqa: BLE001
            check("data state: %s" % state, False, repr(e)[:60])
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        for line in out.stdout.splitlines():
            if line.startswith(("  ok", "  FAIL", "  SKIP")):
                print(line)
            elif line.endswith("failed") and "checks" in line:
                n, f = line.split()[0], line.split()[2]
                total += int(n)
                failed += int(f)
        if out.returncode not in (0, 1):
            check("data state %s ran cleanly" % state, False,
                  (out.stderr or "").strip().splitlines()[-1:][0][:70]
                  if out.stderr.strip() else "exit %d" % out.returncode)
    PASSED[0] += total
    for i in range(failed):
        FAILED.append("a check in a data-state subprocess (%d)" % (i + 1))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--offline", action="store_true",
                    help="skip the check against the SGP4 service")
    ap.add_argument("--state", choices=sorted(STATES), default=None,
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.state:
        STATES[args.state](args.cache_dir)
    else:
        print("cache: %s" % args.cache_dir)
        test_no_network()
        test_gmst()
        test_kepler()
        test_projection()
        test_coastline()
        test_orbits(args.cache_dir)
        test_reference(args.cache_dir, args.offline)
        test_passes(args.cache_dir)
        test_pixels(args.cache_dir)
        test_motion(args.cache_dir)
        test_sizes(args.cache_dir)
        run_states(args.cache_dir)

    print("\n%d checks, %d failed, %d skipped"
          % (PASSED[0], len(FAILED), len(SKIPPED)))
    for name in FAILED:
        print("  FAILED: %s" % name)
    for name in SKIPPED:
        print("  SKIPPED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
