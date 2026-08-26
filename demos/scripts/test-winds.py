#!/usr/bin/env python3
"""Checks for winds.py that a screenshot cannot make.

A wind map has one failure mode that beats every other: **a field running the
wrong way looks completely plausible**. Meteorological direction is the bearing
the wind comes *from*, the particles have to move towards the opposite one, and
a panel that drops those two minus signs is a beautiful, confident, backwards
picture of the sea breeze. Nobody eyeballing an LED wall will catch it.

So the direction is asserted three ways, each less forgiving than the last:

  1. against a **synthetic** cache -- a uniform 20 kt wind from each of the
     four cardinal points, where the answer is unarguable and any sign error
     shows up as motion in exactly the wrong direction;
  2. against the **fetched JSON**, at each real station, by stepping the render
     and measuring where the drifters near that station actually went;
  3. against the **rendered pixels**, by taking the still picture off two
     frames, cross-correlating them in a window over the Gate, and asking
     which way what is left has moved.

The third is the one that matters, because it is the only one that cannot be
fooled by a bug living between the particle array and the screen. It is also
the one that is hard to do right, and it was wrong: it subtracted a scalar
mean rather than the static picture, so the coastline -- which correlates
perfectly with itself and never moves -- owned the peak, and it read two
frames six apart, which at thirteen knots is two pixels across a stretched
panel and a third of a pixel down it. Both are fixed here: the background is
a per-pixel temporal median of the picture itself, the peak is refined to a
fraction of a pixel, and the interval grows until the picture has actually
gone somewhere. When it cannot, the check is **skipped and says why**, which
is counted separately from a pass. Three controls keep it honest, and they
are checks like any other: a field read with the FROM convention reversed has
to be rejected, a render that pushes the drifters backwards under a correct
header has to be rejected at the Gate on the live data, and a dead-calm field
has to be skipped rather than waved through.

    $ python3 scripts/test-winds.py                     # uses the live cache
    $ python3 scripts/test-winds.py --cache-dir /tmp/c  # or a pointed one

Needs a populated cache; run `python3 ftdata.py --once --only wind-bay` first.
The synthetic, no-data, stale, partial and corrupt cases build their own cache
directories and need nothing.
"""

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                       # noqa: E402
import ftdata                                                # noqa: E402
import tide                                                  # noqa: E402
import winds                                                 # noqa: E402

FAILED = []
SKIPPED = []
PASSED = [0]


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-54s %s" % (name, detail))
    else:
        print("  FAIL %-54s %s" % (name, detail))
        FAILED.append(name)


def skip(name, reason):
    """A check that could not be made, said out loud.

    Distinct from a pass on purpose. A direction check that quietly reports
    success when it had nothing to measure is worse than one that fails: it
    keeps the count at forty and stops anyone looking. So a skip is counted,
    named and printed with the number that made it impossible.
    """
    print("  SKIP %-54s %s" % (name, reason))
    SKIPPED.append("%s (%s)" % (name, reason))


def opts(**kw):
    return ds.options(winds, **kw)


def frames(args, n=8):
    r = winds.build(args)
    out = None
    for i in range(n):
        out = r(i / 30.0, i)
    return r, out.copy()


def contains_text(frame, s, thresh=90, scales=(1, 2)):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure an honest
    message actually reached it rather than merely being computed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = winds.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                if np.array_equal(row[:, x:x + gw] & m, m):
                    return True
    return False


def angle_diff(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


# --------------------------------------------------------------------------
# A cache we control, so the wind can be made to blow from a known bearing.
# --------------------------------------------------------------------------

def synthetic(cache_dir, speed=20.0, direction=270.0, gust=None, hours=6,
              nlat=5, nlon=7, extent=(37.70, 37.94, -122.72, -122.24),
              fetched_at=None, t0=None, holes=(), drop=()):
    """Write a wind-bay record by hand. Returns its path."""
    la0, la1, lo0, lo1 = extent
    now = time.time()
    t0 = (now // 3600.0) * 3600.0 - 3600.0 if t0 is None else t0
    grid = []
    k = 0
    for i in range(nlat):
        for j in range(nlon):
            lat = la0 + (la1 - la0) * i / (nlat - 1.0)
            lon = lo0 + (lo1 - lo0) * j / (nlon - 1.0)
            sp = [speed] * hours
            dr = [direction] * hours
            gu = [(gust if gust is not None else speed * 1.4)] * hours
            if k in drop:
                sp = dr = gu = [None] * hours
            elif k in holes:
                sp[1] = dr[1] = gu[1] = None
            grid.append({"lat": round(lat, 4), "lon": round(lon, 4),
                         "elev": 0.0, "speed": sp, "dir": dr, "gust": gu})
            k += 1
    payload = {"model": "synthetic", "units": {"speed": "kn"},
               "extent": list(extent), "requested": [nlat, nlon],
               "t0": t0, "step": 3600.0, "n": hours,
               "span": [t0, t0 + 3600.0 * (hours - 1)], "grid": grid}
    os.makedirs(cache_dir, exist_ok=True)
    rec = {"name": "wind-bay", "source": "synthetic", "ttl": ftdata.WIND_TTL,
           "fetched_at": now if fetched_at is None else fetched_at,
           "payload": payload}
    path = os.path.join(cache_dir, "wind-bay.json")
    with open(path, "w") as fh:
        json.dump(rec, fh)
    return path


# --------------------------------------------------------------------------
# 1. Direction, against a field whose answer cannot be argued with.
# --------------------------------------------------------------------------

def measured_motion(r, frames_n=10):
    """Median (drow, dcol) per frame of the drifters, in map pixels.

    Taken from the particle array *after* render has advanced it, and with the
    ones that were recycled this frame thrown out -- a respawn is a jump of a
    hundred pixels and would eat any median it got into.
    """
    pos = r.particles
    dy, dx = [], []
    for i in range(frames_n):
        before = pos.copy()
        r(i / 30.0, i)
        d = pos - before
        good = np.hypot(d[0], d[1]) < 12.0
        if good.sum() > 20:
            dy.append(float(np.median(d[0][good])))
            dx.append(float(np.median(d[1][good])))
    return float(np.median(dy)), float(np.median(dx))


def bearing_of(r, drow, dcol):
    """A screen displacement as a compass bearing the motion is TOWARDS.

    The panel is stretched about three times horizontally, so a raw pixel
    angle is not a bearing; the stretch is undone here with the same metres
    per pixel the demo used to apply it.
    """
    wm, hm = winds.extent_metres(r.extent)
    east = dcol * (wm / r.layout.w)
    north = -drow * (hm / r.layout.map_rows)
    if abs(east) < 1e-9 and abs(north) < 1e-9:
        return None
    return math.degrees(math.atan2(east, north)) % 360.0


def test_synthetic_direction():
    print("\ndirection, against a synthetic uniform wind")
    tmp = tempfile.mkdtemp(prefix="ftw-syn")
    try:
        for frm in (0.0, 90.0, 180.0, 270.0, 315.0):
            synthetic(tmp, speed=20.0, direction=frm)
            r = winds.build(opts(cache_dir=tmp, hour=0, particles=600))
            for i in range(6):
                r(i / 30.0, i)            # settle
            drow, dcol = measured_motion(r)
            got = bearing_of(r, drow, dcol)
            want = (frm + 180.0) % 360.0
            check("wind FROM %03d moves the field towards %03d" % (frm, want),
                  got is not None and angle_diff(got, want) < 12.0,
                  "measured %s (drow %+.2f dcol %+.2f)"
                  % ("none" if got is None else "%.1f" % got, drow, dcol))

        # And the crudest possible statement of the same thing, in case some
        # future refactor gets clever about bearings: a westerly moves right.
        synthetic(tmp, speed=20.0, direction=270.0)
        r = winds.build(opts(cache_dir=tmp, hour=0, particles=600))
        for i in range(6):
            r(i / 30.0, i)
        drow, dcol = measured_motion(r)
        check("a westerly moves the drifters to the right", dcol > 0.4,
              "dcol %+.3f px/frame" % dcol)
        synthetic(tmp, speed=20.0, direction=180.0)
        r = winds.build(opts(cache_dir=tmp, hour=0, particles=600))
        for i in range(6):
            r(i / 30.0, i)
        drow, dcol = measured_motion(r)
        check("a southerly moves the drifters up the screen", drow < -0.1,
              "drow %+.3f px/frame" % drow)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2. Direction, against the fetched JSON, station by station.
# --------------------------------------------------------------------------

def test_station_direction(cache_dir):
    print("\ndirection, against the fetched grid at each station")
    rec, age, err = winds.read_wind(cache_dir)
    if rec is None:
        check("cache is populated", False, err or "no record")
        return
    args = opts(cache_dir=cache_dir, hour=0, particles=1400)
    r = winds.build(args)
    lay = r.layout
    extent = r.extent
    for i in range(8):
        r(i / 30.0, i)

    # Where each station's own numbers say the air should be going, right now.
    x = (r.clock() - rec["t0"]) / rec["step"]
    j = int(np.clip(math.floor(x), 0, rec["n"] - 2))
    f = np.clip(x - j, 0.0, 1.0)
    spd = rec["spd"][:, j] * (1 - f) + rec["spd"][:, j + 1] * f
    u = rec["u"][:, j] * (1 - f) + rec["u"][:, j + 1] * f
    v = rec["v"][:, j] * (1 - f) + rec["v"][:, j + 1] * f

    pos = r.particles
    order = np.argsort(-spd)
    tested, worst, worst_at = 0, 0.0, ""
    for si in order:
        if not np.isfinite(spd[si]) or spd[si] < 4.0:
            continue
        row, col = winds.cell_of(extent, (lay.map_rows, lay.w),
                                 float(rec["lat"][si]), float(rec["lon"][si]))
        if not (3 <= row < lay.map_rows - 3 and 8 <= col < lay.w - 8):
            continue
        want = math.degrees(math.atan2(float(u[si]), float(v[si]))) % 360.0
        dy, dx = [], []
        for i in range(14):
            near = ((np.abs(pos[0] - row) < 4.0)
                    & (np.abs(pos[1] - col) < 12.0))
            before = pos.copy()
            r(i / 30.0, i)
            d = pos - before
            sel = near & (np.hypot(d[0], d[1]) < 12.0)
            if sel.sum() >= 6:
                dy.append(float(np.median(d[0][sel])))
                dx.append(float(np.median(d[1][sel])))
        if len(dy) < 6:
            continue
        got = bearing_of(r, float(np.median(dy)), float(np.median(dx)))
        if got is None:
            continue
        tested += 1
        e = angle_diff(got, want)
        if e > worst:
            worst, worst_at = e, "%.4f,%.4f want %03.0f got %03.0f" % (
                rec["lat"][si], rec["lon"][si], want, got)
        if tested >= 8:
            break
    check("drifters run with the model wind at every station tested",
          tested >= 4 and worst < 25.0,
          "%d stations, worst %.1f deg (%s)" % (tested, worst, worst_at))


# --------------------------------------------------------------------------
# 3. Direction, off the rendered pixels. The one that cannot be fooled.
# --------------------------------------------------------------------------

# Intervals tried, shortest first, and how far the picture has to have gone
# before a bearing read off it means anything. Three pixels over the interval
# is about eight degrees of angular resolution once the parabolic fit is doing
# its work, which is comfortably inside the 30 degree gate below and nowhere
# near being able to confuse a bearing with its reverse.
GAPS = (6, 12, 24, 48, 96)
MIN_SHIFT_PX = 3.0


def window_of(frame, lay, win):
    """The map window of a rendered frame, as one float plane."""
    r0, r1, c0, c1 = win
    return frame[lay.map_y + r0:lay.map_y + r1,
                 c0:c1].max(axis=2).astype(np.float32)


def still_background(r, lay, win, n=40):
    """The part of the picture that does not move, taken from the picture.

    A per-pixel temporal median. The coastline, the land and sea fill and the
    speed wash are in every single frame; a drifter is over any given pixel for
    a frame or two out of forty, so the median is the static picture and
    nothing else.

    This is the fix to the bug that made this check useless. Subtracting the
    scalar *mean*, which is what used to happen here, leaves every bit of that
    structure in place -- and static structure correlates perfectly with itself
    at zero offset, so the peak sat at (0, 0) however far the drifters had
    really gone. Measured in the Gate window: the still picture carries a
    variance of 749 and the drifters 1871, but the drifters are sheared by the
    field and do not translate rigidly, so their peak is smeared over several
    pixels while the coastline's is a spike. The coastline won, at every
    interval from two frames to eighty.
    """
    stack = np.stack([window_of(r(i / 30.0, i), lay, win) for i in range(n)])
    return np.median(stack, axis=0).astype(np.float32)


def _sub_pixel(sm, s0, sp):
    """Where the peak really is, given the three samples about it.

    A parabola through (-1, sm), (0, s0), (+1, sp). Standard, and the reason a
    third of a pixel per frame is measurable at all on an integer grid.
    """
    denom = sm - 2.0 * s0 + sp
    if not np.isfinite(denom) or denom > -1e-9:
        return 0.0                       # flat or convex: no peak to refine
    return float(np.clip(0.5 * (sm - sp) / denom, -0.5, 0.5))


def frame_shift(a, b, max_dy=4, max_dx=14):
    """The (dy, dx) that best lines image `a` up onto image `b`, sub-pixel.

    Normalised cross-correlation over a search box, then a parabolic fit
    through the three samples either side of the peak in each axis. Both
    images are expected to have had the static picture taken off them already;
    the mean subtraction here is only tidying.
    """
    a = a.astype(np.float32) - a.mean()
    b = b.astype(np.float32) - b.mean()
    h, w = a.shape
    s = np.full((2 * max_dy + 1, 2 * max_dx + 1), -np.inf, np.float64)
    for iy, dy in enumerate(range(-max_dy, max_dy + 1)):
        ay0, ay1 = max(0, -dy), min(h, h - dy)
        for ix, dx in enumerate(range(-max_dx, max_dx + 1)):
            ax0, ax1 = max(0, -dx), min(w, w - dx)
            if ay1 - ay0 < 4 or ax1 - ax0 < 4:
                continue
            v = float((a[ay0:ay1, ax0:ax1]
                       * b[ay0 + dy:ay1 + dy, ax0 + dx:ax1 + dx]).sum())
            s[iy, ix] = v / ((ay1 - ay0) * (ax1 - ax0))
    ky, kx = np.unravel_index(int(np.argmax(s)), s.shape)
    fy = fx = 0.0
    if 0 < ky < s.shape[0] - 1:
        fy = _sub_pixel(s[ky - 1, kx], s[ky, kx], s[ky + 1, kx])
    if 0 < kx < s.shape[1] - 1:
        fx = _sub_pixel(s[ky, kx - 1], s[ky, kx], s[ky, kx + 1])
    return (ky - max_dy) + fy, (kx - max_dx) + fx


def measure_picture(r, lay, win, gaps=GAPS, want=MIN_SHIFT_PX):
    """(dy, dx, gap): how far the picture moved, over an interval that suits.

    Six frames was hardcoded here. Six frames at this bay's ordinary thirteen
    knots is two pixels across the panel and a third of a pixel down it -- so
    even a correlator that worked had almost nothing to bite on, and the check
    failed on the wind rather than on the code. The interval is grown until
    the picture has actually gone somewhere; if none of them gets it there,
    the caller is told the number and skips rather than guessing.
    """
    bg = still_background(r, lay, win)
    f0 = window_of(r(0.0, 0), lay, win) - bg
    seen, f = 0, None
    out = (0.0, 0.0, gaps[0])
    for gap in gaps:
        while seen < gap:
            f = r(seen / 30.0, seen)
            seen += 1
        f1 = window_of(f, lay, win) - bg
        # The box has to hold the motion but no more: a box far wider than the
        # displacement is just more places for a spurious peak to hide. North
        # is squashed three times by the stretch, so it never needs much.
        mdx = int(min(44, 6 + gap))
        mdy = int(min(6, 2 + gap // 8))
        dy, dx = frame_shift(f0, f1, mdy, mdx)
        out = (dy, dx, gap)
        if math.hypot(dy, dx) >= want:
            break
    return out


def pixel_bearing(r, label, want_from, window, note=""):
    """Read the bearing off the rendered pixels and check it, or skip loudly."""
    lay = r.layout
    for i in range(10):
        r(i / 30.0, i)                    # settle
    dy, dx, gap = measure_picture(r, lay, window)
    want = (want_from + 180.0) % 360.0
    name = "%s: picture moves towards %03.0f" % (label, want)
    moved = math.hypot(dy, dx)
    if moved < MIN_SHIFT_PX:
        skip(name, "%sthe picture moved %.2f px in %d frames -- too little to "
                   "read a bearing off" % (note, moved, gap))
        return None
    got = bearing_of(r, dy / float(gap), dx / float(gap))
    if got is None:
        skip(name, "%sno displacement at all over %d frames" % (note, gap))
        return None
    check(name, angle_diff(got, want) < 30.0,
          "%sshifted (%+.2f,%+.2f)px over %d frames -> %03.0f"
          % (note, dy, dx, gap, got))
    return got


def gate_window(r):
    lay = r.layout
    row, col = winds.cell_of(r.extent, (lay.map_rows, lay.w), *winds.GATE)
    return (max(0, row - 10), min(lay.map_rows, row + 10),
            max(0, col - 90), min(lay.w, col + 90))


def reversing(fn):
    """winds.read_wind with the velocity flipped: the bug this check exists for."""
    def go(cache_dir):
        rec, age, err = fn(cache_dir)
        if rec is not None:
            rec["u"], rec["v"] = -rec["u"], -rec["v"]
        return rec, age, err
    return go


def draw_backwards(r):
    """Flip the sign of the field the drifters are actually pushed by.

    The other reversal -- flipping `read_wind` -- flips the header along with
    the picture, because the demo quotes the same u and v it draws; the two
    stay consistent and the *live* check cannot see it. That one is caught by
    the synthetic cases above, which know the bearing from the file rather than
    from the demo. This one is the bug only the live check can catch: the
    numbers are right, the header is right, and the render pushes the drifters
    the other way. It is the exact failure this whole section exists for.
    """
    for hr in r.state["hours"]:
        hr["field"] *= -1.0
    return r


def test_pixel_direction(cache_dir):
    print("\ndirection, measured off the rendered picture")

    tmp = tempfile.mkdtemp(prefix="ftw-pix")
    try:
        lay = winds.Layout(ds.WIDTH, ds.HEIGHT)
        mid = (lay.map_rows // 2 - 12, lay.map_rows // 2 + 12, 60, 260)
        for frm in (270.0, 90.0, 200.0):
            synthetic(tmp, speed=22.0, direction=frm)
            r = winds.build(opts(cache_dir=tmp, hour=0, particles=900))
            pixel_bearing(r, "synthetic %03d" % frm, frm, mid)

        # Air too slow to move a drifter a pixel in any sane interval. There
        # is nothing to measure and the check must say so rather than pass.
        synthetic(tmp, speed=0.8, direction=270.0)
        r = winds.build(opts(cache_dir=tmp, hour=0, particles=900))
        n_before = len(SKIPPED)
        pixel_bearing(r, "synthetic calm 0.8 kt", 270.0, mid)
        check("a field too calm to measure is skipped, not passed",
              len(SKIPPED) == n_before + 1,
              "%d skips" % (len(SKIPPED) - n_before))

        # The control: the same measurement, on a field with the two minus
        # signs of the FROM convention dropped. If this does not come back
        # wrong, nothing above is worth anything.
        synthetic(tmp, speed=22.0, direction=270.0)
        real = winds.read_wind
        winds.read_wind = reversing(real)
        try:
            r = winds.build(opts(cache_dir=tmp, hour=0, particles=900))
            dy, dx, gap = measure_picture(r, r.layout, mid)
            got = bearing_of(r, dy / float(gap), dx / float(gap))
        finally:
            winds.read_wind = real
        check("a reversed field is caught, not tolerated",
              got is not None and angle_diff(got, 90.0) >= 30.0,
              "westerly rendered backwards reads %s, wanted 090 rejected"
              % ("none" if got is None else "%03.0f" % got))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # And on the real forecast, in a box around the Gate, against the bearing
    # the demo itself is quoting in its header for that same spot.
    rec, _, err = winds.read_wind(cache_dir)
    if rec is None:
        check("cache is populated", False, err or "no record")
        return
    args = opts(cache_dir=cache_dir, hour=0, particles=900)
    r = winds.build(args)
    cond = r.state["hours"][0]["cond"]
    got = pixel_bearing(r, "live field at the gate", cond["dir"], gate_window(r),
                        note="header says FROM %03.0f at %.1f kt; "
                             % (cond["dir"], cond["speed"]))

    # The control for the check immediately above, on the same live data: the
    # same window, the same correlator, the same tolerance, and a render that
    # pushes the drifters the wrong way. If this comes back inside 30 degrees
    # of the header, the check above proves nothing and the suite says so.
    if got is not None:
        r = draw_backwards(winds.build(args))
        for i in range(10):
            r(i / 30.0, i)
        dy, dx, gap = measure_picture(r, r.layout, gate_window(r))
        bad = bearing_of(r, dy / float(gap), dx / float(gap))
        want = (cond["dir"] + 180.0) % 360.0
        check("a backwards render at the gate is caught, not tolerated",
              bad is not None and angle_diff(bad, want) >= 30.0,
              "drawn backwards it reads %s against a header saying %03.0f"
              % ("none" if bad is None else "%03.0f" % bad, want))


# --------------------------------------------------------------------------
# 4. Geography. Is the coastline where the demo says it is?
# --------------------------------------------------------------------------

LANDMARKS = [
    ("Golden Gate mid-channel", 37.8199, -122.4783, True),
    ("Alcatraz", 37.8267, -122.4233, False),
    ("Angel Island", 37.8610, -122.4310, False),
    ("Treasure Island", 37.8235, -122.3704, False),
    ("Mt Sutro, San Francisco", 37.7580, -122.4580, False),
    ("Sausalito", 37.8590, -122.4850, False),
    ("Pacific, 12 km west of the Gate", 37.8100, -122.6200, True),
    ("Central Bay, off Berkeley", 37.8600, -122.3600, True),
    ("South Bay, below the bridge", 37.7600, -122.3400, True),
]


def test_geography():
    print("\ngeography of the crop")
    r = winds.build(opts(cache_dir=tempfile.mkdtemp(prefix="ftw-empty")))
    lay, extent, sea = r.layout, r.extent, r.sea_map
    bad = []
    for name, lat, lon, want_sea in LANDMARKS:
        row, col = winds.cell_of(extent, (lay.map_rows, lay.w), lat, lon)
        if not (0 <= row < lay.map_rows and 0 <= col < lay.w):
            bad.append("%s off the map" % name)
            continue
        if bool(sea[row, col]) != want_sea:
            bad.append("%s: %s, wanted %s" % (
                name, "sea" if sea[row, col] else "land",
                "sea" if want_sea else "land"))
    check("nine landmarks land on the right side of the coastline",
          not bad, "; ".join(bad) if bad else "all nine")

    # The scale bar in words: one column is 110 m, one row is 342 m.
    wm, hm = winds.extent_metres(extent)
    check("crop is 35 km by 18 km", abs(wm - 35200) < 400 and abs(hm - 17800) < 400,
          "%.1f x %.1f km" % (wm / 1000, hm / 1000))
    stretch = (hm / lay.map_rows) / (wm / lay.w)
    check("horizontal stretch is the ~3x the docstring claims",
          2.5 < stretch < 3.6, "%.2fx" % stretch)

    # The Gate is the middle of the panel on purpose; if it drifts, the whole
    # composition is wrong even though the map is still correct.
    row, col = winds.cell_of(extent, (lay.map_rows, lay.w), *winds.GATE)
    check("the Golden Gate is near the centre of the panel",
          abs(col - lay.w / 2) < 30 and abs(row - lay.map_rows / 2) < 12,
          "row %d of %d, col %d of %d" % (row, lay.map_rows, col, lay.w))


# --------------------------------------------------------------------------
# 5. The field. Interpolation sanity, and the gradient the panel exists for.
# --------------------------------------------------------------------------

def test_field(cache_dir):
    print("\nthe interpolated field")
    tmp = tempfile.mkdtemp(prefix="ftw-fld")
    try:
        # A uniform field must interpolate to itself. Any weighting scheme
        # whose rows do not sum to one fails this and nothing else would show
        # it, because a 12% error everywhere still looks like weather.
        synthetic(tmp, speed=20.0, direction=270.0)
        r = winds.build(opts(cache_dir=tmp, hour=0))
        cond = r.state["hours"][0]["cond"]
        check("a uniform 20 kt westerly reads back as 20 kt from 270",
              abs(cond["speed"] - 20.0) < 0.4 and angle_diff(cond["dir"], 270) < 2,
              "%.2f kt from %.1f" % (cond["speed"], cond["dir"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rec, _, err = winds.read_wind(cache_dir)
    if rec is None:
        check("cache is populated", False, err or "no record")
        return
    r = winds.build(opts(cache_dir=cache_dir, hour=0))
    hr = r.state["hours"][0]
    spd = np.hypot(hr["field"][1] / r.px_per_kt[0], hr["field"][0] / r.px_per_kt[1])
    st = rec["spd"][:, 0]
    st = st[np.isfinite(st)]
    # Interpolation cannot invent air faster than the fastest station, and a
    # field whose maximum is well outside the data is a weighting bug.
    check("the field stays inside the range of the stations that made it",
          spd.max() <= st.max() * 1.05 + 0.2,
          "field max %.1f kt, station max %.1f kt" % (spd.max(), st.max()))
    check("the field is not flat", spd.max() - spd.min() > 0.5,
          "%.1f to %.1f kt across the panel" % (spd.min(), spd.max()))


# --------------------------------------------------------------------------
# 6. Degraded data. Every one of these must reach the panel in words.
# --------------------------------------------------------------------------

def test_degraded():
    print("\nwhen the data is missing, stale, partial or rubbish")
    tmp = tempfile.mkdtemp(prefix="ftw-bad")
    try:
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        _, f = frames(opts(cache_dir=empty))
        check("no cache at all says NO WIND DATA",
              contains_text(f, "NO WIND DATA"))
        check("...and names the command that fixes it",
              contains_text(f, "FTDATA.PY"))

        bad = os.path.join(tmp, "corrupt")
        os.makedirs(bad)
        with open(os.path.join(bad, "wind-bay.json"), "w") as fh:
            fh.write('{"payload": {"grid": ')
        _, f = frames(opts(cache_dir=bad))
        check("a half-written file says NO WIND DATA",
              contains_text(f, "NO WIND DATA"))

        wrong = os.path.join(tmp, "wrongshape")
        os.makedirs(wrong)
        with open(os.path.join(wrong, "wind-bay.json"), "w") as fh:
            json.dump({"name": "wind-bay", "fetched_at": time.time(),
                       "payload": {"hello": "world"}}, fh)
        _, f = frames(opts(cache_dir=wrong))
        check("a payload from some other product says NO WIND DATA",
              contains_text(f, "NO WIND DATA"))

        old = os.path.join(tmp, "expired")
        os.makedirs(old)
        # Fetched two days ago and its hours ran out yesterday: the record
        # parses perfectly and is entirely useless.
        synthetic(old, t0=time.time() - 2 * 86400,
                  fetched_at=time.time() - 2 * 86400)
        _, f = frames(opts(cache_dir=old))
        check("a forecast whose hours no longer reach now draws nothing",
              contains_text(f, "NO WIND DATA"))

        stale = os.path.join(tmp, "stale")
        os.makedirs(stale)
        # Fetched five hours ago, but the hours it carries still cover now.
        # That is a picture worth drawing, with a warning on it.
        synthetic(stale, hours=12, t0=(time.time() // 3600) * 3600 - 3600,
                  fetched_at=time.time() - 5 * 3600)
        r, f = frames(opts(cache_dir=stale))
        check("a stale but still-valid forecast draws, and says STALE",
              not contains_text(f, "NO WIND DATA") and contains_text(f, "STALE"),
              "age %s" % ftdata.describe_age(r.state["age"]))

        part = os.path.join(tmp, "partial")
        os.makedirs(part)
        # Six stations answer with nothing at all, four have a hole in hour 1.
        synthetic(part, drop=(0, 1, 2, 3, 4, 5), holes=(9, 10, 11, 12))
        r, f = frames(opts(cache_dir=part))
        rec, _, _ = winds.read_wind(part)
        ok = rec is not None and rec["dropped"] == 6
        check("dead stations are dropped and the rest still draw",
              ok and not contains_text(f, "NO WIND DATA"),
              "dropped %s of 35" % (rec["dropped"] if rec else "?"))
        if rec is not None:
            fld = np.hypot(r.state["hours"][0]["field"][0],
                           r.state["hours"][0]["field"][1])
            check("a hole in one hour does not poison the field with NaN",
                  bool(np.isfinite(fld).all()), "%d pixels" % fld.size)

        thin = os.path.join(tmp, "thin")
        os.makedirs(thin)
        synthetic(thin, nlat=2, nlon=2, drop=(0, 1, 2))
        _, f = frames(opts(cache_dir=thin))
        check("a grid with fewer than three live stations says NO WIND DATA",
              contains_text(f, "NO WIND DATA"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 7. Sizes, the loop point, and the promise that load() never talks to anyone.
# --------------------------------------------------------------------------

def test_sizes(cache_dir):
    print("\nother panel sizes, and running past the loop")
    for w, h in ((320, 64), (256, 64), (160, 32), (128, 16), (512, 96),
                 (320, 40), (64, 64)):
        try:
            args = opts(cache_dir=cache_dir, width=w, height=h,
                        particles=120, cycle=2.0)
            r = winds.build(args)
            out = None
            # Well past one full cycle at 30 fps, so every hour of the sweep
            # is rendered and the wrap back to now happens several times.
            for i in range(200):
                out = r(i / 30.0, i)
            ok = out.shape == (h, w, 3) and out.dtype == np.uint8
        except Exception as e:                               # noqa: BLE001
            ok, out = False, repr(e)
        check("%dx%d survives two hundred frames" % (w, h), ok,
              "" if ok is True and not isinstance(out, str) else str(out)[:60])

    args = opts(cache_dir=cache_dir, cycle=1.5)
    r = winds.build(args)
    seen = set()
    for i in range(400):
        r(i / 30.0, i)
        seen.add(r.state["cur"])
    n = len(r.state["hours"])
    check("the sweep visits every forecast hour and wraps",
          len(seen) == n, "%d of %d hours seen over 400 frames" % (len(seen), n))


def test_reload(cache_dir):
    print("\nre-reading the cache without dropping frames")
    # --rate winds the demo's clock forward fast, which is what makes the
    # reload signature change inside a test rather than in half an hour.
    args = opts(cache_dir=cache_dir, reload=0.5, rate=4000.0, cycle=3.0)
    r = winds.build(args)
    first = r.state["hours"]
    pended, out = 0, None
    for i in range(400):
        out = r(i / 30.0, i)
        if r.state["pending"] is not None:
            pended += 1
    check("a reload re-bakes in the background and swaps in",
          pended > 0 and r.state["hours"] is not first
          and out.shape == (args.height, args.width, 3),
          "%d frames spent mid-rebuild, %d hours now"
          % (pended, len(r.state["hours"])))
    # The whole point of doing it a moment per frame: no single frame pays for
    # two dozen fields. A rebuild that ran inline would be a third of a second.
    slow = 0
    for i in range(200):
        t = time.perf_counter()
        r(i / 30.0, i)
        if time.perf_counter() - t > 0.15:
            slow += 1
    check("no frame stalls for a whole rebuild", slow == 0,
          "%d frames over 150 ms" % slow)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("wind-bay", tempfile.mkdtemp(prefix="ftw-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    check("winds.py does not import one either",
          not any(m in sys.modules for m in ("requests",))
          and "urllib.request" not in sys.modules,
          "loaded: %s" % ",".join(sorted(
              m for m in sys.modules if m.startswith("urllib"))))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    args = ap.parse_args()
    print("cache: %s" % args.cache_dir)

    test_no_network()
    test_geography()
    test_synthetic_direction()
    test_pixel_direction(args.cache_dir)
    test_station_direction(args.cache_dir)
    test_field(args.cache_dir)
    test_degraded()
    test_reload(args.cache_dir)
    test_sizes(args.cache_dir)

    print("\n%d checks, %d failed, %d skipped"
          % (PASSED[0], len(FAILED), len(SKIPPED)))
    for name in FAILED:
        print("  FAILED: %s" % name)
    for name in SKIPPED:
        print("  SKIPPED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
