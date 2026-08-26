#!/usr/bin/env python3
"""Checks for adsb.py that a screenshot cannot make.

A traffic map has one failure mode that beats every other: **marks moving the
wrong way still look completely plausible**. A track is a bearing clockwise
from true north, the screen is rows-down and columns-right, the map is
stretched three times horizontally, and getting any one of those wrong gives a
beautiful, confident panel full of aircraft flying backwards over a coastline
that is still perfectly correct. Nobody looking at an LED wall from across a
workshop will catch it. So the direction is asserted three ways, each less
forgiving than the last:

  1. against a **synthetic** record -- one aircraft, a known track, a known
     groundspeed -- where the answer is arithmetic and any sign error shows up
     as motion in exactly the wrong direction;
  2. against the **rendered pixels**, by taking the mark's centroid off two
     frames thirty seconds apart and reading a bearing and a speed back off the
     screen, undoing the stretch with the same metres per pixel the demo used
     to apply it;
  3. with a **control** that must fail: the same measurement on a render whose
     velocities have been negated. If a backwards panel passes, nothing above
     is worth anything.

The other thing worth being careful about is that **this demo is not a pure
function of t**. It carries the last frame's ease, the last reload's arrays and
a clock, so every check here renders a *sequence* from a fresh `build()` and
drives the demo's clock itself rather than sampling `render()` at scattered
timestamps. `render.state["now"]` exists for exactly that.

    $ python3 scripts/test-adsb.py                     # uses the live cache
    $ python3 scripts/test-adsb.py --cache-dir /tmp/c  # or a pointed one

Only the last section needs a populated cache; run
`python3 ftdata.py --once --only adsb-bay` first. Everything else writes the
records it wants.
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

import adsb                                                  # noqa: E402
import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402

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
    """A check that could not be made, said out loud, and counted separately.

    A check that quietly reports success when it had nothing to measure is
    worse than one that fails: it keeps the total up and stops anyone looking.
    """
    print("  SKIP %-56s %s" % (name, reason))
    SKIPPED.append("%s (%s)" % (name, reason))


def opts(**kw):
    return ds.options(adsb, **kw)


def angle_diff(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def find_text(frame, s, thresh=90, scales=(1, 2)):
    """Where this string is drawn on the frame, or None. (y, x, w, h).

    Reading the words back off the panel is the only way to be sure an honest
    message actually reached it rather than merely being computed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = adsb.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                if np.array_equal(row[:, x:x + gw] & m, m):
                    return y, x, gw, gh
    return None


def contains_text(frame, s, thresh=90, scales=(1, 2)):
    return find_text(frame, s, thresh, scales) is not None


# --------------------------------------------------------------------------
# A record we write ourselves, so the aircraft can be made to fly a known
# track at a known speed from a known place.
# --------------------------------------------------------------------------

class Clock(object):
    """The demo's present moment, under the test's control.

    Not the wall clock and not a function of the frame index. The ease and the
    reload both measure seconds, and a check that cannot say what second it is
    cannot assert anything about either.
    """

    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t

    def step(self, dt):
        self.t += dt


def plane(lat, lon, trk, gs=300.0, alt=8000.0, call="TEST01", hexid="000001",
          typ="B738", cat="A3", dst=1.0, pa=0.0):
    return {"hex": hexid, "call": call, "type": typ, "cat": cat,
            "lat": lat, "lon": lon, "alt": alt, "gs": gs, "trk": trk,
            "dst": dst, "pa": pa}


def synthetic(cache_dir, planes, t=None, fetched_at=None, n_ground=7,
              capped=False, radius=50):
    """Write an adsb-bay record by hand. Returns its path."""
    now = time.time()
    t = now if t is None else t
    cols = ("hex", "call", "type", "cat", "lat", "lon", "alt", "gs", "trk",
            "dst", "pa")
    payload = {"origin": list(adsb.HOME), "radius_nm": radius, "t": t,
               "n": len(planes), "n_air": len(planes), "n_ground": n_ground,
               "n_seen": len(planes) + n_ground, "capped": capped,
               "source": "synthetic"}
    payload.update({c: [p[c] for p in planes] for c in cols})
    os.makedirs(cache_dir, exist_ok=True)
    rec = {"name": "adsb-bay", "source": "synthetic", "ttl": ftdata.ADSB_TTL,
           "fetched_at": now if fetched_at is None else fetched_at,
           "payload": payload}
    path = os.path.join(cache_dir, "adsb-bay.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh)
    os.replace(tmp, path)
    return path


def started(cache_dir, t=None, **kw):
    """build() a demo against `cache_dir`, with its clock pinned to the record.

    Pinned to the record's own timestamp rather than to `time.time()` so that
    dt starts at zero and every displacement measured afterwards is one the
    test asked for.
    """
    args = opts(cache_dir=cache_dir, **kw)
    r = adsb.build(args)
    clk = Clock(r.state["rec"]["t"] if (r.state["rec"] and t is None)
                else (t if t is not None else time.time()))
    r.state["now"] = clk
    return r, clk


def run(r, clk, n, dt=0.05):
    """Render `n` frames in sequence, advancing the clock between them."""
    out = None
    for i in range(n):
        out = r(i * dt, i)
        clk.step(dt)
    return out


# --------------------------------------------------------------------------
# Reading a mark back off the panel.
# --------------------------------------------------------------------------

def centroid(frame, lay, win, thresh=40):
    """Brightness-weighted centre of the lit pixels in a window, or None.

    The window is always chosen over open water, where the map's own pixels are
    C_SEA at a brightness of 13 and nothing else is drawn, so anything above
    the threshold in there is an aircraft and the centroid is sub-pixel.
    """
    r0, r1, c0, c1 = win
    sub = frame[r0:r1, c0:c1].max(axis=2).astype(np.float64)
    sub = np.where(sub >= thresh, sub, 0.0)
    total = sub.sum()
    if total <= 0:
        return None
    rows = np.arange(r0, r1, dtype=np.float64)
    cols = np.arange(c0, c1, dtype=np.float64)
    return (float((sub.sum(axis=1) * rows).sum() / total),
            float((sub.sum(axis=0) * cols).sum() / total))


def marks(frame, rows, thresh=35):
    """How many pixels of the map are an aircraft rather than the map.

    Counted by *saturation*, not by brightness. Every colour on the map is a
    shade of slate -- the shoreline is the brightest thing on it at 104, and a
    threshold on brightness counts the coastline, the airports and the scale
    bar, which between them are ten times any traffic and never move. The
    altitude ramp is saturated everywhere along its length; the map is not.
    """
    px = frame[:rows].astype(np.int16)
    return int(((px.max(axis=2) - px.min(axis=2)) > thresh).sum())


def bearing_of(r, drow, dcol):
    """A screen displacement as the compass bearing it is travelling TOWARDS.

    The panel is stretched three times horizontally, so a raw pixel angle is
    not a bearing; the stretch is undone with the same metres per pixel the
    demo used to apply it.
    """
    wm, hm = adsb.extent_metres(r.extent)
    east = dcol * (wm / r.layout.w)
    north = -drow * (hm / r.layout.map_rows)
    if abs(east) < 1e-9 and abs(north) < 1e-9:
        return None, 0.0
    return math.degrees(math.atan2(east, north)) % 360.0, math.hypot(east, north)


# Open Pacific, well clear of the coastline, the scale bar and every label.
# Checked against the demo's own sea mask below before anything is measured in
# it; a window with a headland in the corner would put the coastline's own
# pixels into the centroid and quietly bias every bearing.
PROBE_LAT, PROBE_LON = 37.72, -122.80
PROBE_WIN = (10, 42, 6, 92)


def probe_window(r, pad_r=16, pad_c=44):
    row, col = adsb.cell_of(r.extent, (r.layout.map_rows, r.layout.w),
                            PROBE_LAT, PROBE_LON)
    return (max(0, int(row) - pad_r), min(r.layout.map_rows, int(row) + pad_r),
            max(0, int(col) - pad_c), min(r.layout.w, int(col) + pad_c))


# --------------------------------------------------------------------------
# 1. The promise that nothing here talks to anybody.
# --------------------------------------------------------------------------

def test_no_network():
    print("\nthe network promise")
    tmp = tempfile.mkdtemp(prefix="adsb-net")
    try:
        before = set(sys.modules)
        ftdata.load("adsb-bay", tmp)
        new = set(sys.modules) - before
        bad = [m for m in new if m.split(".")[0] in
               ("urllib", "http", "socket", "ssl", "requests")]
        check("ftdata.load() imports no network module", not bad, ",".join(bad))
        # Import statements only, not the prose: the docstring explains at
        # length why a build() blocked on a socket stops the render loop, and
        # a check that greps for the word "socket" fails on the paragraph that
        # exists to prevent the bug.
        bad = []
        for line in open(os.path.join(HERE, "adsb.py")):
            head = line.split("#")[0].strip()
            if head.startswith("import ") or head.startswith("from "):
                mod = head.split()[1].split(".")[0]
                if mod in ("urllib", "http", "socket", "ssl", "requests",
                           "urllib2", "httplib"):
                    bad.append(head)
        check("adsb.py imports no network module", not bad, "; ".join(bad))
        check("...and importing it has not pulled one in either",
              "urllib.request" not in sys.modules
              and "requests" not in sys.modules,
              ",".join(sorted(m for m in sys.modules
                              if m.startswith(("urllib", "requests")))))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2. Geography. Is the coastline where the demo says it is?
# --------------------------------------------------------------------------

LANDMARKS = [
    ("Golden Gate mid-channel", 37.8199, -122.4783, True),
    ("Central Bay, off Berkeley", 37.8600, -122.3600, True),
    ("South Bay, mid-channel", 37.5500, -122.2000, True),
    ("Pacific, 30 km west of the Gate", 37.7500, -122.7000, True),
    ("San Pablo Bay, off Richmond", 37.9200, -122.4000, True),
    ("SFO", 37.6189, -122.3750, False),
    ("Oakland airport", 37.7213, -122.2208, False),
    ("the Mission, San Francisco", 37.7627, -122.3966, False),
    ("San Bruno Mountain", 37.6900, -122.4350, False),
    ("Livermore valley", 37.6934, -121.8700, False),
    ("Half Moon Bay airport", 37.5133, -122.5011, False),
]


def test_geography():
    print("\ngeography of the crop")
    tmp = tempfile.mkdtemp(prefix="adsb-geo")
    try:
        r = adsb.build(opts(cache_dir=tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lay, extent, sea = r.layout, r.extent, r.sea_map

    bad = []
    for name, lat, lon, want_sea in LANDMARKS:
        row, col = adsb.cell_of(extent, (lay.map_rows, lay.w), lat, lon)
        row, col = int(row), int(col)
        if not (0 <= row < lay.map_rows and 0 <= col < lay.w):
            bad.append("%s off the map" % name)
            continue
        if bool(sea[row, col]) != want_sea:
            bad.append("%s: %s wanted %s" % (
                name, "sea" if sea[row, col] else "land",
                "sea" if want_sea else "land"))
    check("eleven landmarks land on the right side of the coastline",
          not bad, "; ".join(bad) if bad else "all eleven")

    wm, hm = adsb.extent_metres(extent)
    check("the crop is 95 km by 51 km",
          abs(wm - 95100) < 900 and abs(hm - 50900) < 900,
          "%.1f x %.1f km" % (wm / 1000, hm / 1000))
    stretch = (hm / lay.map_rows) / (wm / lay.w)
    check("the horizontal stretch is the 3x the docstring claims",
          2.7 < stretch < 3.3, "%.2fx" % stretch)

    row, col = adsb.cell_of(extent, (lay.map_rows, lay.w), *adsb.HOME)
    check("the wall's own address is near the middle of the panel",
          abs(col - lay.w / 2) < 26 and abs(row - lay.map_rows / 2) < 14,
          "row %.0f of %d, col %.0f of %d"
          % (row, lay.map_rows, col, lay.w))

    # SFO is the whole reason this demo does not reuse voxel-dem.npz, whose
    # southern edge is 37.635. If it ever falls off the bottom again, that is
    # the same bug back and it should be loud.
    row, col = adsb.cell_of(extent, (lay.map_rows, lay.w), 37.6189, -122.3750)
    check("SFO is on the map, with room below it",
          0 <= row < lay.map_rows - 6 and 0 <= col < lay.w,
          "row %.0f of %d" % (row, lay.map_rows))

    win = probe_window(r)
    patch = sea[win[0]:win[1], win[2]:win[3]]
    check("the probe window used by every motion check is open water",
          bool(patch.all()), "%d of %d cells are sea"
          % (int(patch.sum()), patch.size))


# --------------------------------------------------------------------------
# 3. Dead reckoning: does a mark go where the track says it goes?
# --------------------------------------------------------------------------

def measure_track(tmp, trk, gs=300.0, seconds=30.0, negate=False, **kw):
    """(bearing, metres per second, detail) read off the rendered pixels."""
    synthetic(tmp, [plane(PROBE_LAT, PROBE_LON, trk, gs=gs)])
    r, clk = started(tmp, labels=0, reload=0, **kw)
    if negate:
        # The control. The record is right, the header is right, and the render
        # pushes the marks the other way. This is the bug the whole section
        # exists for, and it is the one only a pixel measurement can catch.
        r.state["arr"]["vel"] *= -1.0
    win = probe_window(r)
    a = centroid(run(r, clk, 4), r.layout, win)
    # Advance a real interval rather than a couple of frames: at 300 knots
    # thirty seconds is fifteen pixels across this panel and five down it,
    # which is enough to read a bearing off. Four frames is a fifth of a pixel.
    clk.step(seconds)
    b = centroid(r(1.0, 99), r.layout, win)
    if a is None or b is None:
        return None, 0.0, "no mark found in the probe window"
    drow, dcol = b[0] - a[0], b[1] - a[1]
    bearing, metres = bearing_of(r, drow, dcol)
    return bearing, metres / seconds, "moved (%+.2f,%+.2f) px" % (drow, dcol)


def test_dead_reckoning():
    print("\ndead reckoning, measured off the rendered pixels")
    tmp = tempfile.mkdtemp(prefix="adsb-dr")
    try:
        for trk in (0.0, 45.0, 90.0, 180.0, 270.0, 315.0):
            got, ms, detail = measure_track(tmp, trk)
            check("track %03d moves the mark towards %03d" % (trk, trk),
                  got is not None and angle_diff(got, trk) < 8.0,
                  "%s -> %s" % (detail, "none" if got is None
                                else "%03.0f" % got))

        # The crudest possible statements of the same thing, in case some
        # future refactor gets clever about bearings.
        synthetic(tmp, [plane(PROBE_LAT, PROBE_LON, 90.0)])
        r, clk = started(tmp, labels=0, reload=0)
        win = probe_window(r)
        a = centroid(run(r, clk, 4), r.layout, win)
        clk.step(30.0)
        b = centroid(r(1.0, 99), r.layout, win)
        check("an eastbound aircraft moves right across the panel",
              b[1] - a[1] > 8.0, "%+.2f columns in 30 s" % (b[1] - a[1]))
        synthetic(tmp, [plane(PROBE_LAT, PROBE_LON, 0.0)])
        r, clk = started(tmp, labels=0, reload=0)
        a = centroid(run(r, clk, 4), r.layout, win)
        clk.step(30.0)
        b = centroid(r(1.0, 99), r.layout, win)
        check("a northbound aircraft moves up the panel",
              b[0] - a[0] < -1.5, "%+.2f rows in 30 s" % (b[0] - a[0]))

        # Speed, not just direction. A stretch applied to one axis and not the
        # other gives a bearing that is right at 090 and 000 and wrong at 045,
        # and a speed that is wrong everywhere.
        for trk, gs in ((45.0, 300.0), (135.0, 480.0), (270.0, 120.0)):
            got, ms, detail = measure_track(tmp, trk, gs=gs)
            want = gs * adsb.KNOT_MS
            check("a %d kt aircraft on %03d crosses the map at %d m/s"
                  % (gs, trk, want),
                  got is not None and abs(ms - want) < want * 0.12,
                  "measured %.1f m/s, wanted %.1f" % (ms, want))

        got, ms, detail = measure_track(tmp, 90.0, negate=True)
        check("a render pushing the marks backwards is caught, not tolerated",
              got is None or angle_diff(got, 90.0) > 90.0,
              "drawn backwards, 090 reads %s"
              % ("none" if got is None else "%03.0f" % got))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. Per-aircraft position age, and the TTL that stops the extrapolation.
# --------------------------------------------------------------------------

def test_position_age():
    print("\nposition age, and the limit on extrapolating it")
    tmp = tempfile.mkdtemp(prefix="adsb-pa")
    try:
        # Two identical aircraft, one of them last heard thirty seconds ago.
        # The stale one must already be drawn thirty seconds further along its
        # track, because that is where it is.
        for pa in (0.0, 30.0):
            synthetic(tmp, [plane(PROBE_LAT, PROBE_LON, 90.0, pa=pa)])
            r, clk = started(tmp, labels=0, reload=0)
            win = probe_window(r)
            c = centroid(run(r, clk, 4), r.layout, win)
            if pa == 0.0:
                base = c
            else:
                moved = c[1] - base[1]
                want = 300.0 * adsb.KNOT_MS * 30.0 * (
                    r.layout.w / adsb.extent_metres(r.extent)[0])
                check("an aircraft last heard 30 s ago starts 30 s along "
                      "its track",
                      abs(moved - want) < max(1.5, want * 0.15),
                      "%+.2f columns, wanted %+.2f" % (moved, want))

        # Past the TTL there is no honest picture, and the panel must say so
        # rather than extrapolate a five minute old fix forty miles.
        #
        # "No aircraft" is asserted by rendering the same stale panel twice,
        # once from a record holding twelve aircraft and once from a record
        # holding none, and requiring the two to be identical pixel for pixel.
        # Looking for a bright pixel instead does not work, because the stale
        # card's own type is the brightest thing on the panel and lands in
        # every window worth measuring in.
        old = time.time() - 900
        synthetic(tmp, [plane(PROBE_LAT + 0.02 * i, PROBE_LON + 0.03 * i,
                              (i * 29) % 360, hexid="0000%02d" % i)
                        for i in range(12)], t=old, fetched_at=old)
        r, clk = started(tmp, labels=0, reload=0, t=time.time())
        f = run(r, clk, 6)
        synthetic(tmp, [], t=old, fetched_at=old)
        r2, clk2 = started(tmp, labels=0, reload=0, t=time.time())
        f2 = run(r2, clk2, 6)
        rows = r.layout.map_rows
        check("a record past its TTL draws no aircraft at all",
              r.state["state"] == "stale"
              and np.array_equal(f[:rows], f2[:rows]),
              "state %s, %d map pixels differ from the empty-record panel"
              % (r.state["state"],
                 int((f[:rows] != f2[:rows]).any(axis=2).sum())))
        check("...and says STALE in words on the panel",
              contains_text(f, "ADS-B DATA STALE"))
        check("...and the map is still drawn under it, not blanked",
              int((f[:rows].max(axis=2) > 0).sum()) > 2000,
              "%d lit pixels" % int((f[:rows].max(axis=2) > 0).sum()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 5. Altitude, as colour.
# --------------------------------------------------------------------------

def test_altitude_colour():
    print("\naltitude, as colour")
    tmp = tempfile.mkdtemp(prefix="adsb-alt")
    try:
        # Two aircraft, sixty columns apart over open water, both stationary in
        # the frame that is measured. Read the colour back off the pixels.
        lo = plane(PROBE_LAT + 0.06, PROBE_LON - 0.10, 90.0, alt=600.0,
                   hexid="000001")
        hi = plane(PROBE_LAT - 0.06, PROBE_LON + 0.10, 90.0, alt=38000.0,
                   hexid="000002")
        synthetic(tmp, [lo, hi])
        r, clk = started(tmp, labels=0, reload=0)
        f = run(r, clk, 4)
        lay = r.layout

        def peak(p):
            row, col = adsb.cell_of(r.extent, (lay.map_rows, lay.w),
                                    p["lat"], p["lon"])
            sub = f[max(0, int(row) - 4):int(row) + 5,
                    max(0, int(col) - 6):int(col) + 7].reshape(-1, 3)
            return sub[int(np.argmax(sub.max(axis=1)))].astype(int)

        clo, chi = peak(lo), peak(hi)
        check("an aircraft on the deck is drawn warm", clo[0] > clo[2] + 60,
              "rgb %s" % (tuple(clo),))
        check("one in the flight levels is drawn cold", chi[2] > chi[0] + 20,
              "rgb %s" % (tuple(chi),))
        check("the ramp is monotonic from the deck to the ceiling",
              bool(np.all(np.diff(adsb.alt_index(
                  np.linspace(0, adsb.ALT_CEIL, 64))) >= 0)),
              "64 samples")
        check("and it saturates rather than wrapping past the ceiling",
              int(adsb.alt_index(np.array([90000.0]))[0]) == 255,
              "90,000 ft indexes %d"
              % int(adsb.alt_index(np.array([90000.0]))[0]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 6. The ease. A new fix must slide in, not jump.
# --------------------------------------------------------------------------

def ease_run(tmp, ease, jump_deg=0.03, frames=90, dt=0.05):
    """Render across a reload that moves an aircraft, and track every step.

    Returns (biggest single-frame jump in pixels, final error against the new
    fix in pixels). The record is swapped underneath a *running* demo, which is
    the only way to exercise the ease at all -- it lives entirely in the frames
    between one record and the next.
    """
    t0 = time.time()
    synthetic(tmp, [plane(PROBE_LAT, PROBE_LON, 90.0, pa=0.0)], t=t0)
    r, clk = started(tmp, t=t0, labels=0, reload=0.2, ease=ease)
    # The default probe window and no wider. A window that reaches the
    # coastline puts the shoreline's own pixels into the centroid, which does
    # not move, and every displacement measured after that is a fraction of
    # the real one -- an error that looks like a bug in the demo rather than
    # like a bug in the measurement.
    win = probe_window(r)
    steps, last, swapped = [], None, False
    for i in range(frames):
        c = centroid(r(i * dt, i), r.layout, win)
        if c is not None and last is not None:
            steps.append(math.hypot(c[0] - last[0], c[1] - last[1]))
        last = c
        clk.step(dt)
        if not swapped and i == frames // 3:
            # A fix that puts the aircraft somewhere the old one did not
            # predict: a real correction, of about six columns.
            synthetic(tmp, [plane(PROBE_LAT, PROBE_LON + jump_deg, 90.0,
                                  pa=0.0)], t=clk())
            swapped = True
    row, col = adsb.cell_of(r.extent, (r.layout.map_rows, r.layout.w),
                            PROBE_LAT, PROBE_LON + jump_deg)
    dt_since = clk() - r.state["rec"]["t"]
    col += (300.0 * adsb.KNOT_MS * dt_since
            * r.layout.w / adsb.extent_metres(r.extent)[0])
    err = math.hypot(last[0] - row, last[1] - col) if last else 1e9
    return (max(steps) if steps else 0.0), err, len(steps)


def test_ease():
    print("\nthe ease, across a record landing under a running demo")
    tmp = tempfile.mkdtemp(prefix="adsb-ease")
    try:
        jump, err, n = ease_run(tmp, ease=1.1)
        check("a new fix slides in rather than jumping", jump < 2.0,
              "biggest single-frame step %.2f px over %d frames" % (jump, n))
        check("...and lands on the new fix once the ease has run out",
              err < 2.0, "%.2f px from where the new record says it is" % err)

        # The control. With no ease the same correction has to arrive as one
        # visible jump; if it does not, the check above was measuring nothing.
        jump0, err0, _ = ease_run(tmp, ease=0.0)
        check("with --ease 0 the same correction is a visible jump",
              jump0 > 3.0, "biggest single-frame step %.2f px" % jump0)
        check("...and it still lands in the right place", err0 < 2.0,
              "%.2f px" % err0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 7. Aircraft off the map. The clip must discard them, not smear them.
# --------------------------------------------------------------------------

def test_offmap():
    print("\naircraft outside the crop")
    tmp = tempfile.mkdtemp(prefix="adsb-off")
    try:
        near = plane(PROBE_LAT, PROBE_LON, 90.0, hexid="000001", dst=1.0)
        far = [plane(39.9, -124.9, 90.0, hexid="0000f%d" % i, dst=40.0 + i)
               for i in range(4)]
        far += [plane(36.1, -120.1, 270.0, hexid="0000g%d" % i, dst=44.0 + i)
                for i in range(4)]

        synthetic(tmp, [near])
        r1, c1 = started(tmp, labels=0, reload=0)
        only = run(r1, c1, 6)

        synthetic(tmp, [near] + far)
        r2, c2 = started(tmp, labels=0, reload=0)
        both = run(r2, c2, 6)

        # Identical, pixel for pixel, apart from the status line -- which
        # honestly reports nine aircraft rather than one. Eight aircraft far
        # outside the crop must contribute exactly nothing to the map, and a
        # clip that smeared them onto the border would show up here as a
        # difference on the edge rows.
        rows = r1.layout.map_rows
        same = np.array_equal(only[:rows], both[:rows])
        diff = int((only[:rows] != both[:rows]).any(axis=2).sum())
        check("eight aircraft off the crop draw exactly nothing", same,
              "%d map pixels differ" % diff)
        check("...and the counts still include them", contains_text(both, "9 AIRBORNE"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 8. Labels.
# --------------------------------------------------------------------------

def test_labels():
    print("\nthe two named aircraft")
    tmp = tempfile.mkdtemp(prefix="adsb-lab")
    try:
        planes = [
            plane(PROBE_LAT, PROBE_LON, 90.0, alt=9000.0, call="NEAREST",
                  hexid="000001", dst=0.4),
            plane(PROBE_LAT + 0.10, PROBE_LON + 0.20, 90.0, alt=300.0,
                  call="LOWEST", hexid="000002", dst=22.0),
            plane(PROBE_LAT - 0.10, PROBE_LON + 0.10, 270.0, alt=31000.0,
                  call="NEITHER", hexid="000003", dst=14.0),
        ]
        synthetic(tmp, planes)
        r, clk = started(tmp, reload=0)
        f = run(r, clk, 6)
        check("the nearest aircraft is named", contains_text(f, "NEAREST"))
        check("the lowest aircraft is named", contains_text(f, "LOWEST"))
        check("and nothing else is", not contains_text(f, "NEITHER"))

        # Two aircraft in the same place. Their labels want the same rectangle,
        # and the loser has to move somewhere clear -- or, if there is nowhere,
        # not be drawn at all. What it may not do is print over the winner:
        # two callsigns on top of each other read as a third callsign that
        # belongs to nothing.
        stack = [plane(PROBE_LAT, PROBE_LON, 90.0, alt=9000.0, call="AAAAAA",
                       hexid="000001", dst=0.4),
                 plane(PROBE_LAT, PROBE_LON, 90.0, alt=300.0, call="BBBBBB",
                       hexid="000002", dst=0.5)]
        synthetic(tmp, stack)
        r, clk = started(tmp, reload=0)
        f = run(r, clk, 6)
        a, b = find_text(f, "AAAAAA"), find_text(f, "BBBBBB")
        overlap = (a is not None and b is not None
                   and a[1] < b[1] + b[2] and b[1] < a[1] + a[2]
                   and a[0] < b[0] + b[3] and b[0] < a[0] + a[3])
        check("two labels in the same place do not overprint each other",
              not overlap, "at %s and %s" % (a and a[:2], b and b[:2]))
        check("...and the first of them is still drawn", a is not None,
              "" if a else "the nearest aircraft lost its name")

        synthetic(tmp, planes)
        r, clk = started(tmp, reload=0, labels=0)
        f = run(r, clk, 6)
        check("--labels 0 names nobody", not contains_text(f, "NEAREST"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 9. Degraded data, each state in a process of its own.
#
# `ftdata.CACHE_DIR` binds at import, so a state that is meant to be reached
# through the environment has to be reached in a fresh interpreter. Reloading
# the module in one process does not do it, and has produced a false pass in
# this project before.
# --------------------------------------------------------------------------

_CHILD = r"""
import os, sys
sys.path.insert(0, "__HERE__")
import numpy as np, demoscene as ds, adsb
r = adsb.build(ds.options(adsb))
out = None
for i in range(24):
    out = r(i / 20.0, i)
lit = out.max(axis=2) >= 90
def has(s):
    for sc in (1, 2):
        m = adsb.text_mask(s, sc); gh, gw = m.shape
        if gh > lit.shape[0] or gw > lit.shape[1]: continue
        for y in range(lit.shape[0] - gh + 1):
            row = lit[y:y+gh]
            for x in range(lit.shape[1] - gw + 1):
                if np.array_equal(row[:, x:x+gw] & m, m): return True
    return False
words = [w for w in ("NO ADS-B DATA", "ADS-B DATA STALE", "NO AIRCRAFT",
                     "FTDATA.PY", "AIRBORNE", "NO FIX") if has(w)]
print("STATE %s WORDS %s LIT %d" % (r.state["state"], "|".join(words),
                                    int((out.max(axis=2) > 0).sum())))
"""


def child(cache_dir):
    """Build and render the demo in a fresh interpreter, with FT_DATA_CACHE set."""
    env = dict(os.environ)
    env["FT_DATA_CACHE"] = cache_dir
    # Nothing should be reaching /run/ftdata during a test, and if the machine
    # happens to have one, a volatile product would be read out of it instead
    # of out of the directory this check just wrote.
    env["FT_DATA_BLOBS"] = cache_dir
    out = subprocess.run([sys.executable, "-c", _CHILD.replace("__HERE__", HERE)],
                         cwd=HERE, env=env, capture_output=True, text=True,
                         timeout=180)
    if out.returncode != 0:
        return None, out.stderr.strip().splitlines()[-1:] or ["failed"]
    line = [l for l in out.stdout.splitlines() if l.startswith("STATE ")]
    if not line:
        return None, ["no result line"]
    parts = line[0].split(" WORDS ")
    state = parts[0][6:]
    words, lit = parts[1].split(" LIT ")
    return (state, set(w for w in words.split("|") if w), int(lit)), None


def test_states_in_subprocesses(live_cache):
    print("\nfresh, stale and absent -- each in its own interpreter")
    tmp = tempfile.mkdtemp(prefix="adsb-sub")
    try:
        empty = os.path.join(tmp, "absent")
        os.makedirs(empty)
        got, err = child(empty)
        if got is None:
            check("an empty cache directory draws the no-data card", False,
                  "; ".join(err))
        else:
            state, words, lit = got
            check("an empty cache directory draws the no-data card",
                  state == "absent" and "NO ADS-B DATA" in words, str(sorted(words)))
            check("...and names the command that fixes it", "FTDATA.PY" in words)
            check("...and the map underneath it is still drawn", lit > 2000,
                  "%d lit pixels" % lit)

        fresh = os.path.join(tmp, "fresh")
        synthetic(fresh, [plane(PROBE_LAT, PROBE_LON, 90.0)])
        got, err = child(fresh)
        if got is None:
            check("a fresh record draws the traffic", False, "; ".join(err))
        else:
            state, words, lit = got
            check("a fresh record draws the traffic",
                  state == "fresh" and "NO ADS-B DATA" not in words
                  and "ADS-B DATA STALE" not in words, str(sorted(words)))
            check("...and prints a count", "AIRBORNE" in words)

        stale = os.path.join(tmp, "stale")
        # Both timestamps, and `t` is the one that counts: the demo ages the
        # record by when its *fixes* were taken, not by when the socket was
        # read. A record whose fetch is half an hour old but whose positions
        # claim to be from this second is not a stale record, it is a
        # nonsensical one, and writing it here would test nothing.
        synthetic(stale, [plane(PROBE_LAT, PROBE_LON, 90.0)],
                  t=time.time() - 1800, fetched_at=time.time() - 1800)
        got, err = child(stale)
        if got is None:
            check("a record past its TTL draws the stale card", False,
                  "; ".join(err))
        else:
            state, words, lit = got
            check("a record past its TTL draws the stale card",
                  state == "stale" and "ADS-B DATA STALE" in words,
                  str(sorted(words)))

        corrupt = os.path.join(tmp, "corrupt")
        os.makedirs(corrupt)
        with open(os.path.join(corrupt, "adsb-bay.json"), "w") as fh:
            fh.write('{"payload": {"n": ')
        got, err = child(corrupt)
        check("a half-written file draws the no-data card",
              got is not None and got[0] == "absent"
              and "NO ADS-B DATA" in got[1],
              "; ".join(err) if got is None else str(sorted(got[1])))

        wrong = os.path.join(tmp, "wrong")
        os.makedirs(wrong)
        with open(os.path.join(wrong, "adsb-bay.json"), "w") as fh:
            json.dump({"name": "adsb-bay", "fetched_at": time.time(),
                       "payload": {"hello": "world"}}, fh)
        got, err = child(wrong)
        check("a payload from some other product draws the no-data card",
              got is not None and got[0] == "absent"
              and "NO ADS-B DATA" in got[1],
              "; ".join(err) if got is None else str(sorted(got[1])))

        ragged = os.path.join(tmp, "ragged")
        os.makedirs(ragged)
        synthetic(ragged, [plane(PROBE_LAT, PROBE_LON, 90.0),
                           plane(PROBE_LAT, PROBE_LON, 180.0, hexid="000002")])
        with open(os.path.join(ragged, "adsb-bay.json")) as fh:
            rec = json.load(fh)
        rec["payload"]["alt"] = rec["payload"]["alt"][:1]
        with open(os.path.join(ragged, "adsb-bay.json"), "w") as fh:
            json.dump(rec, fh)
        got, err = child(ragged)
        check("a truncated column draws the no-data card rather than "
              "pairing the wrong numbers",
              got is not None and got[0] == "absent"
              and "NO ADS-B DATA" in got[1],
              "; ".join(err) if got is None else str(sorted(got[1])))

        quiet = os.path.join(tmp, "quiet")
        synthetic(quiet, [])
        got, err = child(quiet)
        check("a good record with nobody airborne says so, and is not an error",
              got is not None and got[0] == "fresh" and "NO AIRCRAFT" in got[1],
              "; ".join(err) if got is None else str(sorted(got[1])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 10. Other panel sizes, and a long sequential run.
# --------------------------------------------------------------------------

def test_sizes():
    print("\nother panel sizes, and a long run")
    tmp = tempfile.mkdtemp(prefix="adsb-size")
    try:
        synthetic(tmp, [plane(PROBE_LAT + 0.01 * i, PROBE_LON + 0.02 * i,
                              (i * 37) % 360, gs=90.0 + 20 * i,
                              alt=500.0 * i, hexid="0000%02d" % i)
                        for i in range(20)])
        for w, h in ((320, 64), (256, 64), (160, 32), (128, 16), (512, 96),
                     (320, 40), (64, 64)):
            try:
                r, clk = started(tmp, width=w, height=h, reload=0)
                out = run(r, clk, 200, dt=0.1)
                ok = out.shape == (h, w, 3) and out.dtype == np.uint8
                detail = ""
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d survives two hundred frames" % (w, h), ok, detail)

        # Four minutes of dead reckoning in one sequence, which is as far as
        # the TTL lets it go. Some aircraft leave the crop and some do not --
        # a 90 kt Cessna starting in the middle of the Bay is still in the
        # middle of the Bay four minutes later, and a check that demanded an
        # empty map would be asserting something untrue. What must hold is
        # that the map never *gains* marks: an aircraft that has flown off the
        # edge and been smeared onto the border by a clip is a mark that was
        # never given up, and it shows here as a count that will not come down.
        r, clk = started(tmp, reload=0, labels=0)
        rows = r.layout.map_rows
        first, seen = None, []
        for i in range(480):
            f = r(i * 0.05, i)
            if first is None:
                first = f[:rows].copy()
            seen.append(marks(f, rows))
            clk.step(0.5)
        check("four minutes of extrapolation never adds marks to the map",
              max(seen) <= seen[0] and seen[-1] < seen[0]
              and r.state["state"] == "fresh",
              "%d mark pixels at the start, peak %d, %d at four minutes"
              % (seen[0], max(seen), seen[-1]))
        check("...and the picture has actually moved by then",
              not np.array_equal(first, f[:rows]),
              "%d map pixels differ from the first frame"
              % int((first != f[:rows]).any(axis=2).sum()))
        # Past the TTL now: the aircraft must stop, not keep flying.
        clk.step(200.0)
        f = r(99.0, 999)
        check("...and past the TTL it stops drawing them and says so",
              contains_text(f, "ADS-B DATA STALE") and r.state["state"] == "stale",
              "state %s" % r.state["state"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 11. The live record, if there is one.
# --------------------------------------------------------------------------

def test_live(cache_dir):
    print("\nthe record ftdata actually fetched")
    rec, age, err = adsb.read_traffic(cache_dir)
    if rec is None:
        skip("the live cache holds a usable adsb-bay record",
             err or "no record; run python3 ftdata.py --once --only adsb-bay")
        return
    check("the live cache holds a usable adsb-bay record", True,
          "%d aircraft, %s old" % (rec["n"], ftdata.describe_age(age)))
    check("every column is the same length as every other",
          all(len(rec[c]) == rec["n"]
              for c in ("hex", "call", "type", "cat")) and
          all(rec[c].shape == (rec["n"],)
              for c in ("lat", "lon", "alt", "gs", "trk", "dst", "pa")),
          "n = %d" % rec["n"])
    if rec["n"]:
        check("no ground traffic survived the fetcher's filter",
              bool(np.all(np.isfinite(rec["alt"]))) and rec["n_ground"] >= 0,
              "%d airborne against %d on the ground"
              % (rec["n_air"], rec["n_ground"]))
        check("every track is a bearing and every groundspeed is positive",
              bool(np.all((rec["trk"] >= 0) & (rec["trk"] < 360))
                   and np.all(rec["gs"] >= 0)),
              "track %.0f..%.0f, gs %.0f..%.0f"
              % (rec["trk"].min(), rec["trk"].max(),
                 rec["gs"].min(), rec["gs"].max()))
        check("everything kept is inside the radius that was asked for",
              bool(np.all(rec["dst"] <= rec["radius_nm"] + 1.0)),
              "furthest %.1f nm of %.0f" % (rec["dst"].max(), rec["radius_nm"]))
        check("the record is small enough to rewrite every minute",
              os.path.getsize(ftdata.record_path("adsb-bay", cache_dir)) < 24000,
              "%d bytes for %d aircraft"
              % (os.path.getsize(ftdata.record_path("adsb-bay", cache_dir)),
                 rec["n"]))

    r, clk = started(cache_dir, reload=0)
    f = run(r, clk, 20)
    check("the live record renders a 320x64 uint8 frame",
          f.shape == (64, 320, 3) and f.dtype == np.uint8, str(f.shape))
    on = int((f[:r.layout.map_rows].max(axis=2) > 60).sum())
    check("...with something on the map", on > 40, "%d bright map pixels" % on)

    # The cost, on this machine, against the 50 ms a 20 fps segment gets on the
    # wall's Pi 3 at 600 MHz.
    ts = []
    for i in range(400):
        t0 = time.perf_counter()
        r(i * 0.05, i)
        clk.step(0.05)
        ts.append((time.perf_counter() - t0) * 1000.0)
    ts = np.array(ts)
    p95 = float(np.percentile(ts, 95))
    check("p95 frame cost leaves room for 100x on the Pi", p95 * 100 < 50.0,
          "p50 %.3f ms, p95 %.3f ms, max %.3f ms here -> %.0f ms at 100x"
          % (float(np.percentile(ts, 50)), p95, ts.max(), p95 * 100))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    args = ap.parse_args()
    print("cache: %s" % args.cache_dir)

    test_no_network()
    test_geography()
    test_dead_reckoning()
    test_position_age()
    test_altitude_colour()
    test_ease()
    test_offmap()
    test_labels()
    test_states_in_subprocesses(args.cache_dir)
    test_sizes()
    test_live(args.cache_dir)

    print("\n%d checks, %d failed, %d skipped"
          % (PASSED[0], len(FAILED), len(SKIPPED)))
    for name in FAILED:
        print("  FAILED: %s" % name)
    for name in SKIPPED:
        print("  SKIPPED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
