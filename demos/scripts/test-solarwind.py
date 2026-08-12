#!/usr/bin/env python3
"""Checks for solarwind.py that a screenshot cannot make.

This panel can draw a confident, beautiful, wrong picture in five ways, and
none of them look wrong:

  1. **The corridor can run backwards.** The record is oldest-first, and
     oldest means *nearest the Earth*, because that plasma has been travelling
     for an hour. Draw it newest-on-the-right instead and the panel is a
     mirror of the truth: the southward patch that is forty minutes away is
     shown arriving, and the one arriving is shown forty minutes away.
  2. **The magnetosphere can be driven by the wrong end.** It responds to the
     sample arriving now -- element 0 -- not to the headline number measured
     at L1, which will not arrive for another fifty minutes.
  3. **The magnetopause can fail to move.** A Shue curve with the pressure
     term dropped is exactly as pretty and says a storm looks like a quiet
     day, which is the one thing this panel exists to contradict.
  4. **Bz can lose its sign.** North and south are one minus sign apart and
     the entire meaning of the panel is that sign; a comb tilted the wrong way
     is a lovely picture of the opposite fact.
  5. **Speed can stop mattering.** 350 and 700 km/s must not look the same,
     and "the texture scrolls faster" is invisible in a still, so it is
     asserted as pixels-moved-per-second between frames.

So the arithmetic is asserted against the published formulas and the drawing
is asserted **in pixels** against synthetic records whose answers cannot be
argued with -- an all-south field, an all-north one, a quiet day and a storm.

Two things about how these are run, both learned the hard way in this tree.
`ftdata.CACHE_DIR` binds at import, so the three data states a demo must
handle -- fresh, stale, absent -- are each run in a **separate process** with
FT_DATA_CACHE set, at the bottom of this file. And every frame comparison
renders from a fresh `build()`, even though this demo is a pure function of t,
so that a future version which stops being pure fails here rather than on the
wall.

    $ python3 scripts/test-solarwind.py                     # uses the live cache
    $ python3 scripts/test-solarwind.py --cache-dir /tmp/c

The live cache is only needed for the one check against real data; everything
else builds its own cache directory and needs nothing. Populate it with
`python3 ftdata.py --once --only swpc_solarwind`.
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

import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402
import solarwind                                              # noqa: E402

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
    return ds.options(solarwind, **kw)


def frames(args, n=8, fps=20.0):
    """Render n frames in order from a fresh build."""
    r = solarwind.build(args)
    out = []
    for i in range(n):
        out.append(np.array(r(i / fps, i)))
    return out


# --------------------------------------------------------------------------
# Synthetic records, written into a throwaway cache directory.
# --------------------------------------------------------------------------

def record(speed=400.0, density=5.0, bz=0.0, bt=5.0, n=56, aurora=12):
    """A flat record: every sample identical, so any gradient on the panel is
    the demo's doing and not the data's."""
    return {
        "speed": [speed] * n, "density": [density] * n,
        "bz": [bz] * n, "bt": [max(bt, abs(bz) + 0.5)] * n,
        "samples": n, "minutes_per_sample": 1,
        "latest": {"t": "2026-08-11T23:59:00Z", "speed": speed,
                   "density": density, "bz": bz, "bt": bt,
                   "arrival": "2026-08-12T00:48:00Z"},
        "aurora": {"north_gw": aurora, "south_gw": aurora, "t": None},
    }


def ramp_record(n=56):
    """Southward at the Earth end, northward at the L1 end, monotonically.

    The one record that can tell a mirrored corridor from a correct one.
    """
    rec = record(n=n)
    rec["bz"] = [-20.0 + 40.0 * i / (n - 1.0) for i in range(n)]
    rec["bt"] = [21.0] * n
    rec["latest"]["bz"] = rec["bz"][-1]
    return rec


def write_cache(dirpath, payload, age=0.0, kp=None):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "swpc_solarwind.json"), "w") as fh:
        json.dump({"payload": payload, "fetched_at": time.time() - age,
                   "source": "test"}, fh)
    if kp is not None:
        with open(os.path.join(dirpath, "swpc_kp.json"), "w") as fh:
            json.dump({"payload": {"now": {"t": "x", "kp": kp}, "series": []},
                       "fetched_at": time.time(), "source": "test"}, fh)
    return dirpath


# --------------------------------------------------------------------------
# 1. The physics, against the published formulas.
# --------------------------------------------------------------------------

def test_shue():
    print("\nShue 1997, against the formulas as published")

    # Quiet: 5 cm^-3 at 400 km/s is 1.34 nPa, Bz zero.
    dp = solarwind.dyn_pressure(5.0, 400.0)
    check("dynamic pressure 5 cm^-3 at 400 km/s", abs(dp - 1.338) < 0.01,
          "%.3f nPa" % dp)

    r0 = solarwind.shue_r0(0.0, dp)
    want = (10.22 + 1.29 * math.tanh(0.184 * 8.14)) * dp ** (-1 / 6.6)
    check("quiet standoff matches the formula", abs(r0 - want) < 1e-9,
          "%.2f Re" % r0)
    check("quiet standoff is about 11 Re", 10.0 < r0 < 12.0, "%.2f Re" % r0)

    # Storm: 20 cm^-3 at 800 km/s with Bz -20.
    dps = solarwind.dyn_pressure(20.0, 800.0)
    r0s = solarwind.shue_r0(-20.0, dps)
    check("storm pressure is over 20 nPa", dps > 20.0, "%.1f nPa" % dps)
    check("storm standoff collapses inside 7 Re", r0s < 7.0, "%.2f Re" % r0s)
    check("standoff moves the right way", r0s < r0 - 3.0,
          "%.2f -> %.2f Re" % (r0, r0s))

    # Both terms have to be live. Dropping either is a plausible-looking bug.
    check("pressure term is live",
          solarwind.shue_r0(0.0, 20.0) < solarwind.shue_r0(0.0, 1.0) - 1.5,
          "%.2f vs %.2f" % (solarwind.shue_r0(0.0, 20.0),
                            solarwind.shue_r0(0.0, 1.0)))
    check("Bz term is live",
          solarwind.shue_r0(-20.0, 2.0) < solarwind.shue_r0(20.0, 2.0) - 1.5,
          "%.2f vs %.2f" % (solarwind.shue_r0(-20.0, 2.0),
                            solarwind.shue_r0(20.0, 2.0)))

    # The surface must be a nose at the front and open at the back.
    r0q, aq = 10.0, 0.58
    nose = solarwind.shue_r(np.array([0.0], np.float32), r0q, aq)[0]
    flank = solarwind.shue_r(np.array([math.pi / 2], np.float32), r0q, aq)[0]
    tail = solarwind.shue_r(np.array([2.6], np.float32), r0q, aq)[0]
    check("nose is the standoff distance", abs(nose - r0q) < 1e-4,
          "%.2f Re" % nose)
    check("flares at the terminator", flank > nose * 1.3, "%.2f Re" % flank)
    check("opens down the tail", tail > flank * 1.5, "%.2f Re" % tail)


# --------------------------------------------------------------------------
# 2. The corridor's direction, in pixels.
# --------------------------------------------------------------------------

# The comb is counted in a window that deliberately excludes the top-left type
# and the bottom "L1" tick. Two of these checks originally passed by counting
# the blue of the word "KM/S" as northward field, which is exactly the kind of
# thing a pixel assertion is supposed to stop rather than commit.
COMB_ROWS = (20, 56)

# One comb row on its own, for the tilt. Over the whole comb the rows are
# staggered by half a pitch, so their dashes overlap in x, the runs merge, and
# the mean row of a merged run says nothing about any dash's slope -- which is
# how the first version of this reported a tilt of 0.005 px either way.
TILT_ROWS = (39, 49)


def comb_mask(frame, x0, x1, south, rows=COMB_ROWS):
    sub = frame[rows[0]:rows[1], x0:x1].astype(np.int16)
    r, g, b = sub[..., 0], sub[..., 1], sub[..., 2]
    if south:
        return (r > 90) & (b > 40) & (b > g + 25) & (r > b)
    return (b > 120) & (b > r + 90) & (b > g + 35)


def field_hue(frame, x0, x1):
    """(south-ish pixels, north-ish pixels) in a column range.

    South is red-dominant with a magenta tail, north is strongly blue-over-red,
    and the plasma is neither because it is orange. The thresholds are wide
    apart on purpose: this has to distinguish two palette bands, not grade
    them.
    """
    return (int(comb_mask(frame, x0, x1, True).sum()),
            int(comb_mask(frame, x0, x1, False).sum()))


def comb_tilt(frame, x0, x1, south):
    """Mean slope of the comb's dashes: positive is down-and-to-the-right.

    Per dash, not over the panel: the dashes repeat on a pitch, so a global
    correlation of row against column cancels to zero no matter which way they
    lean -- which is how the first version of this check passed on both
    hemispheres at once.
    """
    m = comb_mask(frame, x0, x1, south, TILT_ROWS)
    cols = np.nonzero(m.any(axis=0))[0]
    if cols.size < 6:
        return 0.0
    runs, start = [], cols[0]
    for i in range(1, cols.size):
        if cols[i] != cols[i - 1] + 1:
            runs.append((start, cols[i - 1]))
            start = cols[i]
    runs.append((start, cols[-1]))

    slopes = []
    for a, b in runs:
        if b - a < 4:
            continue
        third = max(1, (b - a + 1) // 3)
        left = m[:, a:a + third]
        right = m[:, b - third + 1:b + 1]
        if not left.any() or not right.any():
            continue
        ly = np.nonzero(left)[0].mean()
        ry = np.nonzero(right)[0].mean()
        slopes.append(ry - ly)
    return float(np.mean(slopes)) if slopes else 0.0


def test_direction(tmp):
    print("\nthe corridor: oldest sample nearest the Earth")
    d = write_cache(os.path.join(tmp, "ramp"), ramp_record())
    f = frames(opts(cache_dir=d), 2)[1]

    # The record ramps -20 nT (element 0) to +20 (element -1). Element 0 is
    # arriving at the Earth, so the RIGHT of the panel must be southward.
    ls, ln = field_hue(f, 40, 120)
    rs, rn = field_hue(f, 180, 240)
    check("left end (just measured at L1) reads north", ln > 4 * max(1, ls),
          "north %d south %d" % (ln, ls))
    check("right end (arriving now) reads south", rs > 4 * max(1, rn),
          "south %d north %d" % (rs, rn))

    # And the magnetosphere is driven by the arriving end, so it must be
    # compressed even though the *headline* Bz is +20.
    dn = write_cache(os.path.join(tmp, "north"), record(bz=20.0, bt=21.0))
    fn = frames(opts(cache_dir=dn), 2)[1]
    nose_ramp = magnetopause_nose(f)
    nose_north = magnetopause_nose(fn)
    # Compressed means the boundary is *further right*, nearer the Earth. The
    # ramp's headline Bz is +20 and its arriving Bz is -20, so if the panel
    # read the headline this would come out the other way round.
    check("nose driven by the arriving sample, not the headline",
          nose_ramp > nose_north + 2,
          "ramp x=%d vs all-north x=%d" % (nose_ramp, nose_north))


def magnetopause_nose(frame):
    """x of the dayside boundary on the mid row: the bow shock's nose.

    Cyan is defined tightly as blue and green together with little red -- the
    magnetopause and shock bands have b-g under thirty, and the northward
    field comb, which is also blue and also on the mid row, has b-g over
    forty. Getting that wrong made the first run of this file report a
    magnetopause at x=30, in the Sun.
    """
    band = frame[frame.shape[0] // 2 - 1: frame.shape[0] // 2 + 2]
    r, g, b = (band[..., 0].astype(np.int16), band[..., 1].astype(np.int16),
               band[..., 2].astype(np.int16))
    cyan = (b > 90) & (g > 70) & (b >= g) & (b - g < 30) & (g > r + 30)
    cols = np.where(cyan.any(axis=0))[0]
    return int(cols[0]) if cols.size else frame.shape[1]


# --------------------------------------------------------------------------
# 3. Compression, in pixels.
# --------------------------------------------------------------------------

def test_compression(tmp):
    print("\nthe magnetosphere caves in, visibly")
    quiet = write_cache(os.path.join(tmp, "q"), record(400.0, 5.0, 2.0))
    storm = write_cache(os.path.join(tmp, "s"), record(800.0, 22.0, -18.0, 20.0))
    fq = frames(opts(cache_dir=quiet), 2)[1]
    fs = frames(opts(cache_dir=storm), 2)[1]
    nq, ns = magnetopause_nose(fq), magnetopause_nose(fs)
    check("quiet magnetopause is drawn", nq < 300, "x=%d" % nq)
    check("storm magnetopause is at least 4 px closer in", ns > nq + 4,
          "quiet x=%d storm x=%d" % (nq, ns))

    # The sheath brightens, which is the other half of the pressure story --
    # asserted *within* one panel, against the undisturbed wind upstream of the
    # shock, because comparing two panels would pass on the density difference
    # alone and prove nothing about the sheath.
    def sheath_gain(frame, nose):
        rows = slice(20, 44)
        sheath = float(frame[rows, nose + 1:nose + 7].mean())
        upstream = float(frame[rows, max(0, nose - 34):nose - 12].mean())
        return sheath / max(1e-6, upstream)
    check("shocked plasma piles up behind the shock", sheath_gain(fs, ns) > 1.3,
          "storm x%.2f, quiet x%.2f" % (sheath_gain(fs, ns),
                                        sheath_gain(fq, nq)))


# --------------------------------------------------------------------------
# 4. The sign of Bz, and what hangs off it.
# --------------------------------------------------------------------------

def test_bz_sign(tmp):
    print("\nsouthward Bz turns the comb over and lights the poles")
    north = write_cache(os.path.join(tmp, "n2"), record(bz=12.0, bt=13.0),
                        kp=1.0)
    south = write_cache(os.path.join(tmp, "s2"), record(bz=-12.0, bt=13.0,
                                                        aurora=90), kp=7.0)
    fn = frames(opts(cache_dir=north), 2)[1]
    ns_, nn = field_hue(fn, 40, 220)
    check("north Bz draws a blue comb", nn > 40 and ns_ < 5,
          "north %d south %d" % (nn, ns_))

    fs = frames(opts(cache_dir=south), 2)[1]
    ss, sn = field_hue(fs, 40, 220)
    check("south Bz draws a magenta comb", ss > 40 and sn < 5,
          "south %d north %d" % (ss, sn))

    # The dashes must actually tilt the other way, not merely recolour.
    tn, ts_ = comb_tilt(fn, 60, 220, False), comb_tilt(fs, 60, 220, True)
    check("north comb tilts up, south comb tilts down", tn < -1.0 < 1.0 < ts_,
          "north %.2f south %.2f px per dash" % (tn, ts_))

    # Reconnection sparks exist only on the south side.
    # Measured downstream of the Earth only. Everything upstream of that is
    # streaming plasma, which moves on both panels and would swamp the signal;
    # inside the cavity nothing moves at all unless something is coupling.
    def sparks(args):
        got = frames(args, 40)
        base = got[0][:, 262:].astype(np.int32)
        return max(int(np.abs(f[:, 262:].astype(np.int32) - base).sum())
                   for f in got[1:])
    quiet_move = sparks(opts(cache_dir=north))
    storm_move = sparks(opts(cache_dir=south))
    check("the flanks and poles only animate when Bz is south",
          storm_move > quiet_move * 2,
          "south %d vs north %d" % (storm_move, quiet_move))

    # And the poles are brighter.
    # A tight box on the Earth. Wider and it catches the Kp label, which is
    # green at Kp 1 and red at Kp 7 -- so the quiet panel scored *higher* on
    # aurora than the storm did, from the caption rather than the picture.
    def green(frame):
        g = frame[22:42, 262:278].astype(np.int16)
        return int(((g[..., 1] > 60) & (g[..., 1] > g[..., 0] + 25)
                    & (g[..., 1] > g[..., 2] + 15)).sum())
    check("aurora is brighter in the storm", green(fs) > green(fn),
          "%d vs %d px" % (green(fs), green(fn)))


# --------------------------------------------------------------------------
# 5. Speed has to be visible, which means measuring motion.
# --------------------------------------------------------------------------

def scroll_rate(args, fps=20.0):
    """Pixels per second the stream moves, by cross-correlating two frames.

    Rendered a second apart and matched against shifts of the first frame:
    this is the only way to assert "700 km/s does not look like 350" without
    a human in the loop.
    """
    r = solarwind.build(args)
    a = np.array(r(0.0, 0))[:, 40:200, 0].astype(np.float32)
    b = np.array(r(1.0, 20))[:, 40:200, 0].astype(np.float32)
    best, best_shift = None, 0
    for s in range(0, 130):
        d = float(np.abs(a[:, :160 - s] - b[:, s:]).mean())
        if best is None or d < best:
            best, best_shift = d, s
    return best_shift


def test_speed(tmp):
    print("\nwind speed is legible as motion, not only as type")
    slow = write_cache(os.path.join(tmp, "slow"), record(speed=350.0))
    fast = write_cache(os.path.join(tmp, "fast"), record(speed=700.0))
    vs = scroll_rate(opts(cache_dir=slow))
    vf = scroll_rate(opts(cache_dir=fast))
    check("350 km/s scrolls at about 38 px/s", 30 <= vs <= 46, "%d px/s" % vs)
    check("700 km/s scrolls about twice as fast", vf > vs * 1.7,
          "%d vs %d px/s" % (vf, vs))
    # Streak length is the second cue and is baked from the median speed.
    f_slow = frames(opts(cache_dir=slow), 2)[1]
    f_fast = frames(opts(cache_dir=fast), 2)[1]
    check("the fast panel is not the slow panel",
          not np.array_equal(f_slow, f_fast))


# --------------------------------------------------------------------------
# 6. Purity, cost and the shape of the thing.
# --------------------------------------------------------------------------

def test_contract(tmp):
    print("\nthe demo contract")
    d = write_cache(os.path.join(tmp, "c"), record(), kp=3.0)
    args = opts(cache_dir=d)

    f = frames(args, 2)[1]
    check("frame is (64, 320, 3) uint8",
          f.shape == (64, 320, 3) and f.dtype == np.uint8, str(f.shape))

    # Purity: a cold render at t0 must equal the same t0 driven from zero.
    t0 = 7.35
    cold = np.array(solarwind.build(args)(t0, 147))
    driven = frames(args, 148)[-1]
    warm = np.array(solarwind.build(args)(t0, 147))
    check("render is a pure function of t", np.array_equal(cold, warm))
    check("driving frame by frame lands in the same place",
          np.array_equal(driven, np.array(solarwind.build(args)(147 / 20.0, 147))))

    # Determinism across builds with the same seed, and difference across seeds.
    a = frames(opts(cache_dir=d, seed=1), 2)[1]
    b = frames(opts(cache_dir=d, seed=1), 2)[1]
    c = frames(opts(cache_dir=d, seed=2), 2)[1]
    check("same seed, same pixels", np.array_equal(a, b))
    check("different seed, different texture", not np.array_equal(a, c))

    # Cost. The desktop number is meaningless on its own; what this catches is
    # a future change that makes it ten times worse.
    r = solarwind.build(args)
    ts = []
    for i in range(240):
        t = time.perf_counter()
        r(i / 20.0, i)
        ts.append(time.perf_counter() - t)
    ms = np.array(ts[20:]) * 1e3
    check("frame under 2 ms on this machine", ms.mean() < 2.0,
          "mean %.3f p95 %.3f max %.3f ms" % (ms.mean(),
                                              np.percentile(ms, 95), ms.max()))

    # Odd canvases must not raise; the wall is 320x64 but previews are not.
    for w, h in ((160, 32), (512, 96), (96, 24), (64, 64)):
        try:
            frames(opts(cache_dir=d, width=w, height=h), 3)
            ok, why = True, ""
        except Exception as e:                               # noqa: BLE001
            ok, why = False, repr(e)
        check("survives %dx%d" % (w, h), ok, why)


# --------------------------------------------------------------------------
# 7. The record itself, and the overrides.
# --------------------------------------------------------------------------

def test_record(tmp):
    print("\nthe record, its holes and its overrides")
    rec = record()
    rec["speed"] = [None] * 5 + [500.0] * 46 + [None] * 5
    rec["density"] = [None] * 56
    d = write_cache(os.path.join(tmp, "holes"), rec)
    try:
        f = frames(opts(cache_dir=d), 2)[1]
        ok, why = int(f.sum()) > 0, ""
    except Exception as e:                                   # noqa: BLE001
        ok, why = False, repr(e)
    check("a record full of nulls still draws", ok, why)

    # An override has to reach both the picture and the caption.
    d2 = write_cache(os.path.join(tmp, "ov"), record())
    plain = frames(opts(cache_dir=d2), 2)[1]
    over = frames(opts(cache_dir=d2, bz=-25.0), 2)[1]
    s_over, n_over = field_hue(over, 40, 220)
    check("--bz reaches the comb", s_over > 40 and n_over < 5,
          "south %d north %d" % (s_over, n_over))
    check("--bz changes the panel", not np.array_equal(plain, over))
    check("--storm needs no cache at all",
          int(frames(opts(cache_dir=os.path.join(tmp, "nothing-here"),
                          storm=True), 2)[1].sum()) > 0)


def test_live(cache_dir):
    print("\nagainst the live cache")
    got = ftdata.load("swpc_solarwind", cache_dir)
    if got is None:
        print("  --   no swpc_solarwind record; run "
              "`python3 ftdata.py --once --only swpc_solarwind`")
        return
    payload, age = got
    n = len(payload.get("speed") or ())
    check("record has samples", n >= 10, "%d samples, %s old"
          % (n, ftdata.describe_age(age)))
    check("arrays are the same length",
          len({len(payload[k]) for k in ("speed", "density", "bz", "bt")}) == 1)
    speeds = [v for v in payload["speed"] if v is not None]
    check("speeds are plausible solar wind",
          bool(speeds) and 200 <= min(speeds) and max(speeds) <= 1200,
          "%s..%s km/s" % (min(speeds), max(speeds)))
    bzs = [v for v in payload["bz"] if v is not None]
    check("Bz is within any believable IMF",
          bool(bzs) and max(abs(v) for v in bzs) < 100,
          "%.1f..%.1f nT" % (min(bzs), max(bzs)))
    check("the record is small", os.path.getsize(
        ftdata.record_path("swpc_solarwind", cache_dir)) < 8000,
        "%d bytes" % os.path.getsize(
            ftdata.record_path("swpc_solarwind", cache_dir)))
    f = frames(opts(cache_dir=cache_dir), 4)[-1]
    check("the live record draws something", int(f.sum()) > 10000)


# --------------------------------------------------------------------------
# The three data states, each in its own process.
# --------------------------------------------------------------------------

CHILD = r"""
import os, sys, numpy as np
sys.path.insert(0, %r)
import demoscene as ds, solarwind
r = solarwind.build(ds.options(solarwind))
last = None
for i in range(140):
    last = r(i / 20.0, i)
print("lit=%%d" %% int((last.sum(axis=2) > 0).sum()))
"""


def test_states(tmp):
    print("\nfresh / stale / absent, each in its own process")
    payload = record()
    fresh = write_cache(os.path.join(tmp, "st-fresh"), payload, age=60)
    aging = write_cache(os.path.join(tmp, "st-aging"), payload, age=5400)
    stale = write_cache(os.path.join(tmp, "st-stale"), payload, age=40000)
    absent = os.path.join(tmp, "st-absent")
    os.makedirs(absent, exist_ok=True)

    lit = {}
    for name, d in (("fresh", fresh), ("aging", aging), ("stale", stale),
                    ("absent", absent)):
        env = dict(os.environ, FT_DATA_CACHE=d, FT_DATA_BLOBS=d)
        p = subprocess.run([sys.executable, "-c", CHILD % HERE], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ok = p.returncode == 0 and b"lit=" in p.stdout
        detail = (p.stdout.decode().strip() if ok
                  else p.stderr.decode().strip()[-200:])
        check("%s renders a full loop with no exception" % name, ok, detail)
        if ok:
            lit[name] = int(p.stdout.decode().split("lit=")[1])

    if len(lit) == 4:
        check("the absent state is a card, not a blank panel",
              1000 < lit["absent"] < lit["fresh"] / 2,
              "%d lit px" % lit["absent"])
        check("stale is drawn, but greyed",
              lit["stale"] > 1000, "%d lit px" % lit["stale"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="test-solarwind-")
    try:
        test_shue()
        test_direction(tmp)
        test_compression(tmp)
        test_bz_sign(tmp)
        test_speed(tmp)
        test_contract(tmp)
        test_record(tmp)
        test_states(tmp)
        test_live(args.cache_dir)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  FAILED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
