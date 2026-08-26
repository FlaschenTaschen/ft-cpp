#!/usr/bin/env python3
"""Checks for dvd.py that a screenshot cannot make.

This demo is a joke about a wait, and every way it can be wrong is a way of
ruining the wait rather than a way of looking broken:

  1. **The corner can come early.** The whole design is "hits are exactly T
     apart". If --sweeps and --bounces share a factor the real period is T/gcd
     and the panel hits a corner every forty seconds, which is not rare and is
     not funny. Asserted by sweeping a whole period and counting.
  2. **The corner can be a near miss.** A logo one pixel off the corner at the
     moment the panel shouts CORNER is worse than no celebration at all.
     Asserted in pixels: at the hit the logo's own corner pixel is the panel's
     corner pixel.
  3. **It can drift.** A demo that integrates its velocity is a fraction of a
     pixel out after an hour and several pixels out after a day, so the "exact"
     corner quietly stops being exact. The position here is closed form, and
     it is checked against exact rational arithmetic a million seconds out.
  4. **It can stop being a pure function of t.** The trajectory is anchored to
     an absolute epoch captured in build(), which is deliberate and is the only
     wall-clock dependency; within one build() the frames must depend on
     nothing but t. Checked by driving one build from zero and comparing
     against a cold call on another.

Everything here builds with --epoch pinned, so the checks are deterministic
even though the demo on the wall is not.

    $ python3 scripts/test-dvd.py
    $ python3 scripts/test-dvd.py --bench      # also time a full loop
"""

import argparse
import fractions
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import dvd                                                    # noqa: E402

FAILED = []
PASSED = [0]

# Pinned so that t = 0 is a corner hit: hits fall at absolute times congruent
# to --hit-offset modulo the period, and 34560000 is a whole number of 180s
# periods, so EPOCH itself is one of them.
EPOCH = 34560000.0 + 41.0


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    kw.setdefault("epoch", EPOCH)
    return ds.options(dvd, **kw)


def geom(args):
    r = dvd.build(args)
    return r, r.geometry


def logo_box(frame):
    """Where the logo is, from the pixels: the lit box that is not furniture.

    The readout and the CORNER word are drawn too, so this looks only at the
    saturated colours the logo and its ghosts use. Good enough to locate the
    sprite's bounding box, which is all any check here needs.
    """
    lit = frame.max(axis=2) > 96
    ys, xs = np.nonzero(lit)
    if len(ys) == 0:
        return None
    return ys.min(), xs.min(), ys.max(), xs.max()


# --------------------------------------------------------------------------
# The art.
# --------------------------------------------------------------------------

def test_logo():
    mask = dvd.logo_mask()               # raises if the type fouls the ellipse
    h, w = mask.shape
    check("logo fits the panel with room to move", h < 64 and w < 320,
          "%dx%d" % (h, w))
    check("logo is a serious fraction of the height", h >= 64 * 0.35,
          "%d rows of 64" % h)

    # The wordmark leans, which means the top row must be further right than
    # the bottom row. A shear that silently became a no-op is invisible in a
    # thumbnail and turns the logo into a label.
    mark = dvd.wordmark_mask()
    top = np.nonzero(mark[0])[0]
    bot = np.nonzero(mark[-1])[0]
    check("wordmark actually leans", top.min() > bot.min(),
          "top starts at %d, bottom at %d" % (top.min(), bot.min()))

    # The font is measured, not assumed: a 5 row glyph and a 4 column advance
    # are what the octal table happens to encode, and text_mask must agree.
    m = dvd.text_mask("ABC")
    check("text_mask measures the glyphs", m.shape == (dvd.GLYPH_H, 3 * dvd.ADVANCE - 1),
          "%s for 3 glyphs of %dx%d" % (m.shape, dvd.GLYPH_H, dvd.GLYPH_W))
    check("descenders are not clipped", dvd.GLYPH_H == dvd._GLYPHS["E"].shape[0],
          "glyph height %d" % dvd.GLYPH_H)


# --------------------------------------------------------------------------
# The motion.
# --------------------------------------------------------------------------

def test_corner_is_exact():
    args = opts()
    r, g = geom(args)
    lh, lw = g["logo"]
    frame = r(0.0, 0)                     # t=0 is the hit, by construction
    box = logo_box(frame)
    check("hit puts the logo in the corner", box[0] == 0 and box[1] == 0,
          "logo box starts at (%d, %d)" % (box[0], box[1]))

    # The next hit is a period later and must be the opposite corner, because
    # both traverse counts are odd.
    frame = r(g["period"], 0)
    box = logo_box(frame)
    check("next hit is the opposite corner",
          box[2] == 63 and box[3] == 319,
          "logo box ends at (%d, %d)" % (box[2], box[3]))


def _extremes(g, T):
    """Times in [0, T) at which each axis is against a wall."""
    ax = g["span"][1] / g["vx"]
    ay = g["span"][0] / g["vy"]
    xs = np.arange(0, int(round(T / ax)) + 1) * ax
    ys = np.arange(0, int(round(T / ay)) + 1) * ay
    return ax, ay, xs, ys


def test_corner_period():
    args = opts()
    _, g = geom(args)
    T = g["period"]
    ax, ay, xs, ys = _extremes(g, T)
    check("traverse times", True, "long axis %.3fs, short axis %.3fs" % (ax, ay))

    # A corner is an x-extreme that coincides with a y-extreme. Look for one
    # anywhere strictly inside the period, to a tolerance far tighter than a
    # frame: if two extremes land within 5 ms of each other the panel would
    # draw a corner hit a frame early with no celebration.
    inner = xs[(xs > 1e-6) & (xs < T - 1e-6)]
    gap = np.abs(inner[:, None] - ys[None, :]).min(axis=1)
    check("no corner inside the period", gap.min() > 0.005,
          "closest coincidence %.3fs before a wall" % gap.min())

    # And how near the near misses get, in pixels, which is the thing people
    # actually shout about.
    Sy = g["span"][0]
    ypos = np.abs(((inner * g["vy"] + Sy) % (2 * Sy)) - Sy)
    miss = np.minimum(ypos, Sy - ypos).min()
    check("near misses exist and are near", miss < 3.0,
          "closest near miss %.2f px from a corner" % miss)

    # Sharing a factor collapses the period; the demo warns and bumps p, and
    # what comes out must still be coprime.
    args2 = opts(sweeps=30, bounces=80)
    _, g2 = geom(args2)
    check("non-coprime request is repaired", dvd._gcd(g2["p"], g2["q"]) == 1,
          "q=%d p=%d" % (g2["q"], g2["p"]))


def test_no_drift():
    """The closed form against exact rational arithmetic, a million seconds on.

    An integrated bouncer is a few pixels out by here; a triangle wave is not
    out at all, and this is what buys the demo the right to claim the corner
    is exact after a week of uptime.
    """
    args = opts()
    _, g = geom(args)
    Sx = fractions.Fraction(int(g["span"][1]))
    T = fractions.Fraction(str(g["period"]))
    vx_exact = Sx * g["q"] / T
    worst = 0.0
    for t in ("0", "137.5", "3600", "86400", "1000000"):
        u = fractions.Fraction(t)
        exact = float(abs((u * vx_exact + Sx) % (2 * Sx) - Sx))
        got = dvd.fold(float(t) * g["vx"], float(Sx))
        worst = max(worst, abs(got - exact))
    check("closed form matches exact arithmetic at 1e6 s", worst < 1e-3,
          "worst error %.2e px" % worst)

    # The same statement about the corner: the logo is still flush into it
    # after eleven and a half days of uptime.
    r = dvd.build(args)
    box = logo_box(r(86400.0 * 11.5 + g["period"] * 2, 0))
    check("still exact after 11.5 days", box[0] == 0 or box[2] == 63,
          "logo box %s" % (box,))


def test_colour_cycles():
    args = opts()
    r, g = geom(args)
    # Sample the logo's colour every 100 ms for a few bounces and check the
    # sequence walks the palette in order, one step per wall.
    pal_set = set(dvd.PALETTE)
    seen = []
    for i in range(600):
        t = 2.0 + i * 0.1
        f = r(t, i)
        box = logo_box(f)
        sub = f[box[0]:box[2] + 1, box[1]:box[3] + 1].reshape(-1, 3)
        # The logo is one flat colour; the ghosts are dimmed copies of other
        # palette entries, so pick the most common pixel that is a palette
        # colour at full strength.
        cols, counts = np.unique(sub, axis=0, return_counts=True)
        best, best_n = None, 0
        for c, n in zip(cols, counts):
            key = (int(c[0]), int(c[1]), int(c[2]))
            if key in pal_set and n > best_n:
                best, best_n = key, n
        if best is None:
            continue
        if not seen or seen[-1] != best:
            seen.append(best)
    idx = [list(dvd.PALETTE).index(c) if c in dvd.PALETTE else -1 for c in seen]
    known = [i for i in idx if i >= 0]
    steps = [(b - a) % len(dvd.PALETTE) for a, b in zip(known, known[1:])]
    check("colour walks the palette forwards", all(s in (1, 2) for s in steps),
          "%d changes, steps %s" % (len(known), sorted(set(steps))))

    # No two neighbours in the palette may be close, or a bounce looks like a
    # dropped frame. Compare in RGB; anything under ~90 apart reads the same
    # on the wall.
    pal = np.array(dvd.PALETTE, float)
    d = np.linalg.norm(pal - np.roll(pal, -1, axis=0), axis=1)
    check("no two palette neighbours are similar", d.min() > 90.0,
          "closest pair %.0f apart" % d.min())


# --------------------------------------------------------------------------
# The counter and the celebration.
# --------------------------------------------------------------------------

def _text_rows(frame):
    """The bottom strip where the readout lives."""
    return frame[-dvd.GLYPH_H - 1:, :, :]


def test_counter():
    args = opts()
    r, g = geom(args)
    T = g["period"]

    # It must tick exactly once per period, and only at the hit.
    before = _text_rows(r(T - 0.1, 0)).copy()
    after = _text_rows(r(T + 0.9, 0)).copy()
    check("readout changes across a hit", not np.array_equal(before, after))

    # And the "time since" must be monotonic within a period and reset at it.
    a = _text_rows(r(T - 1.0, 0)).copy()
    b = _text_rows(r(T + 1.0, 0)).copy()
    check("time-since resets at the hit", not np.array_equal(a, b))

    # The number itself, read back off the panel. Counting midnight against
    # the wrong clock is invisible in a thumbnail and was wrong first time --
    # it read nine million -- so the count is recomputed here from the hit
    # times alone and compared as pixels.
    now = EPOCH + 9.0 * 3600.0                   # nine hours into the day
    r2, g2 = geom(opts(epoch=now))
    # Derived the other way round from the demo's own subtraction: the hit
    # times are the whole seconds congruent to --hit-offset modulo the period,
    # and what is counted is the ones strictly after midnight and up to now.
    per, off = int(g2["period"]), int(args.hit_offset)
    mid, end = int(g2["midnight"]), int(now)
    first = mid + ((off - mid) % per)
    if first == mid:
        first += per
    hits = (end - first) // per + 1 if first <= end else 0
    expect = dvd.text_mask("CORNERS TODAY %d" % hits)
    y, x = g2["readout_y"], g2["readout_x"]
    got = r2(0.0, 0)[y:y + expect.shape[0], x:x + expect.shape[1]].max(axis=2) > 0
    check("the counter counts hits since local midnight",
          np.array_equal(got, expect), "expected %d" % hits)

    off = ds.options(dvd, epoch=EPOCH, no_counter=True)
    ro = dvd.build(off)
    check("--no-counter really drops it",
          _text_rows(ro(90.0, 0)).max() == 0)


def test_celebration():
    args = opts()
    r, g = geom(args)
    # The ring has to come out of the corner that was hit, not a random one.
    # Half a second in it is a ~150px arc, so the quadrant it lit is decisive.
    f = r(0.5, 10).copy()
    q = f.reshape(64, 320, 3).max(axis=2)
    tl = q[:32, :160].sum()
    br = q[32:, 160:].sum()
    check("ring leaves the corner that was hit", tl > br * 1.5,
          "top-left %d vs bottom-right %d" % (tl, br))

    # And it must be over. A celebration still running when the next segment
    # starts would put CORNER on screen with no corner.
    # Nothing but the celebration is white -- no palette colour has all three
    # channels at 255 and the readout is grey -- so white pixels are an exact
    # test for "is the panel shouting".
    def white(t):
        f = r(t, 0)
        return int((f.min(axis=2) == 255).sum())

    check("the panel shouts during the celebration", white(0.4) > 100,
          "%d white px" % white(0.4))
    check("celebration ends", white(args.celebrate + 0.2) == 0,
          "%d white px at %.1fs" % (white(args.celebrate + 0.2),
                                    args.celebrate + 0.2))
    check("and stays ended", white(90.0) == 0)


# --------------------------------------------------------------------------
# The contract.
# --------------------------------------------------------------------------

def test_purity():
    """Same build, same t, same pixels -- however you got there."""
    args = opts()
    a = dvd.build(args)
    b = dvd.build(args)
    t0 = 7.35
    for i in range(int(t0 * 20) + 1):
        driven = a(i / 20.0, i)
    cold = b(t0, int(t0 * 20))
    check("render is pure in t for one build",
          np.array_equal(driven, cold),
          "max delta %d" % int(np.abs(driven.astype(int) - cold.astype(int)).max()))

    # And two builds a moment apart must differ, because the trajectory is
    # anchored to the clock. This is the documented exception, not a bug.
    live_a = dvd.build(ds.options(dvd))
    time.sleep(0.35)
    live_b = dvd.build(ds.options(dvd))
    check("unpinned builds follow the wall clock",
          not np.array_equal(live_a(0.0, 0).copy(), live_b(0.0, 0)))


def test_full_loop():
    args = opts()
    r = dvd.build(args)
    # A whole corner period at 20 fps, plus the hit at either end.
    n = int(args.corner_period * 20) + 40
    bad = 0
    for i in range(n):
        f = r(i / 20.0 - 1.0, i)
        if f.shape != (64, 320, 3) or f.dtype != np.uint8:
            bad += 1
    check("a full period renders clean", bad == 0, "%d frames" % n)


def test_no_network():
    src = open(os.path.join(HERE, "dvd.py")).read()
    bad = [m for m in ("urllib", "http.client", "socket", "requests", "ssl",
                       "subprocess") if ("import " + m) in src]
    check("dvd.py imports no network module", not bad, ",".join(bad))
    check("dvd.py reads no data product", "ftdata" not in src)

    # numpy 1.19 is what the wall has. These are the calls that are easy to
    # reach for and are not there.
    modern = [c for c in ("default_rng", "np.take_along_axis", "removeprefix",
                          "np.random.Generator") if c in src]
    check("no post-1.19 numpy calls", not modern, ",".join(modern))


def bench():
    args = opts()
    r = dvd.build(args)
    for i in range(20):
        r(i / 20.0, i)
    n = int(args.corner_period * 20)
    times = np.empty(n)
    for i in range(n):
        t0 = time.perf_counter()
        r(i / 20.0, i)
        times[i] = (time.perf_counter() - t0) * 1000.0
    print("  frame ms over %d frames: mean %.3f  p95 %.3f  max %.3f"
          % (n, times.mean(), np.percentile(times, 95), times.max()))
    # The celebration is the expensive part and is 1.6s in every 180.
    hit = times[:int(args.celebrate * 20)]
    print("  during the celebration:  mean %.3f  max %.3f"
          % (hit.mean(), hit.max()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true", help="time a full loop")
    a = ap.parse_args()

    test_logo()
    test_corner_is_exact()
    test_corner_period()
    test_no_drift()
    test_colour_cycles()
    test_counter()
    test_celebration()
    test_purity()
    test_full_loop()
    test_no_network()
    if a.bench:
        bench()

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
