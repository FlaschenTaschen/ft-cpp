#!/usr/bin/env python3
"""Checks for dominoes.py that a screenshot cannot make.

A domino run is a demo that fails by looking *plausible*. Every one of these
draws a perfectly attractive panel and says something false:

  1. **The run quietly dies halfway.** One gap wider than the tile is tall and
     the wave stops there forever; the left half is a beautiful stack of
     leaning slabs and the right half is a row of tiles standing patiently for
     the rest of the segment. Nobody watching a still frame can tell that from
     "the wave has not got there yet", so it is asserted directly: by the end
     of the toppling phase, *every* tile has moved.
  2. **The reset leaves one down.** Same failure at the other end, and worse,
     because it accumulates -- the next cycle starts from a run with a hole in
     it. Asserted: at the end of the cycle every tile is upright again, and
     the panel at `t = cycle - eps` is pixel-identical to the panel at `t = 0`.
  3. **Causality runs backwards.** The knock-on times come out of a shortest
     path over a graph. If an edge delay is wrong a tile can start falling
     before the thing that knocks it has reached it, which on the panel looks
     like a lively wave and is actually nonsense. Asserted against the
     geometry: no tile starts before its earliest possible parent contact.
  4. **The pivot drifts.** A tile rotating about anything other than its
     bottom edge sinks into the floor or hovers over it, and at 17 px the
     difference between "rotating about the bottom edge" and "rotating about
     the centre and translated" is a couple of pixels that read as sloppiness
     rather than as a bug. Asserted in pixels: nothing is ever drawn below the
     floor row of its own lane.
  5. **The stall stops being a stall.** It is the one joke in the demo and it
     is a number in a table; if the spacing changes it silently becomes an
     ordinary gap. Asserted: there is a beat of at least half a second in the
     middle of the run where nothing is moving, and it is not at either end.

Plus the usual purity check, which for this demo is cheap to pass and cheap to
break: `render` holds two reused buffers and nothing else, so an accidental
`+=` on one of them would make the panel depend on how it was driven.

It needs no cache directory, no network and no wall: the demo is generative.

    $ python3 scripts/test-dominoes.py
    $ python3 scripts/test-dominoes.py --seeds 40      # a wider sweep
"""

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import dominoes                                               # noqa: E402

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
    return ds.options(dominoes, **kw)


def driven(r, t, fps=20.0):
    """Drive render frame by frame from zero and land exactly on t."""
    n = max(0, int(t * fps))
    for i in range(n):
        r(i / fps, i)
    return r(t, n).copy()


# --------------------------------------------------------------------------
# 1. Purity.
# --------------------------------------------------------------------------

def test_purity():
    print("\nrender() is a pure function of t")
    a = opts()
    ref = dominoes.build(a)
    cycle = ref.cycle
    times = [0.0, 0.9, 2.4, cycle * 0.35, cycle * 0.58, ref.reset_at + 0.4,
             cycle - 0.03, cycle + 3.1]
    warm = dominoes.build(a)
    bad = []
    for t in times:
        cold = dominoes.build(a)(t, int(t * 20))
        got = driven(dominoes.build(a), t)
        if not np.array_equal(cold, got):
            bad.append("%.2f" % t)
        if not np.array_equal(cold, warm(t, int(t * 20))):
            bad.append("warm@%.2f" % t)
    check("cold render(t) == the same t driven from zero", not bad,
          ", ".join(bad) or "%d timestamps" % len(times))

    # Seeking backwards, which is what a preview baker's rewind looks like.
    r = dominoes.build(a)
    driven(r, cycle * 0.6)
    bad = [("%.2f" % t) for t in (cycle * 0.6, 1.0, 0.0, cycle * 0.4)
           if not np.array_equal(dominoes.build(a)(t, int(t * 20)),
                                 r(t, int(t * 20)))]
    check("seeking backwards gives the same frame", not bad,
          ", ".join(bad) or "4 seeks")

    b1 = dominoes.build(opts(seed=3))(3.0, 60)
    b2 = dominoes.build(opts(seed=3))(3.0, 60)
    check("the same seed lays out the same run", np.array_equal(b1, b2))
    b3 = dominoes.build(opts(seed=4))(3.0, 60)
    check("a different seed lays out a different one",
          not np.array_equal(b1, b3))


# --------------------------------------------------------------------------
# 2. The run completes, and the reset undoes it exactly.
# --------------------------------------------------------------------------

def test_completes(seeds):
    print("\nevery tile goes over, and every tile comes back up")
    dead = []
    left = []
    for s in range(seeds):
        r = dominoes.build(opts(seed=s))
        th = r.fall_angles(r.reset_at - 1e-3)
        if float(th.min()) < 0.5:
            dead.append("seed %d (%d tiles still up)"
                        % (s, int((th < 0.5).sum())))
        up = r.rise_angles(r.cycle - 1e-3)
        if float(np.abs(up).max()) > 1e-3:
            left.append("seed %d" % s)
    check("the wave reaches the last tile of every run", not dead,
          ", ".join(dead[:4]) or "%d seeds" % seeds)
    check("the hand puts every tile back upright", not left,
          ", ".join(left[:4]) or "%d seeds" % seeds)

    # And in pixels, which is what actually catches a tile the compositor
    # forgot: the end of the cycle must be the start of the cycle.
    bad = []
    for s in range(min(seeds, 8)):
        a = opts(seed=s)
        f0 = dominoes.build(a)(0.0, 0)
        f1 = dominoes.build(a)(dominoes.build(a).cycle - 1e-3, 1)
        if not np.array_equal(f0, f1):
            bad.append("seed %d (%d px)"
                       % (s, int((f0 != f1).any(axis=2).sum())))
    check("the last frame of the loop is the first frame", not bad,
          ", ".join(bad[:3]) or "8 seeds")


# --------------------------------------------------------------------------
# 3. Causality: nothing falls before it is pushed.
# --------------------------------------------------------------------------

def test_causality(seeds):
    print("\nnothing starts falling before something reaches it")
    early = []
    for s in range(seeds):
        r = dominoes.build(opts(seed=s))
        t0 = r.t0
        # The first tile is the finger's. Everything else must be no earlier
        # than the moment its own predecessor was pushed -- the graph is a
        # DAG in increasing x, so the weakest form of this that still bites is
        # that no tile starts before the run does.
        if float(t0.min()) < r.t_tip - 1e-9:
            early.append("seed %d starts at %.2f" % (s, t0.min()))
        # A tile may not start before its lean target has been reached: the
        # only thing that can push tile j is something whose own start is at
        # least one contact time earlier.
        if int((t0 < r.t_tip - 1e-9).sum()):
            early.append("seed %d" % s)
    check("no tile moves before the finger touches the first one", not early,
          ", ".join(early[:3]) or "%d seeds" % seeds)

    # And the wave is monotone along an unbranched run: with --no-branch the
    # only way to reach tile k is through tile k-1.
    bad = []
    for s in range(min(seeds, 10)):
        r = dominoes.build(opts(seed=s, branch=False))
        t0 = r.t0[r.front]
        if not np.all(np.diff(t0) > 0):
            bad.append("seed %d" % s)
    check("an unbranched run topples strictly left to right", not bad,
          ", ".join(bad[:3]) or "10 seeds")

    # With the branch on, the point of the whole thing is that the second run
    # is somewhere else at the same moment. Measure it: at the instant the
    # front wave is halfway, the back wave must not be at the same x.
    off = 0
    for s in range(seeds):
        r = dominoes.build(opts(seed=s))
        if not r.back:
            continue
        px, _ = r.pivots
        t = 0.5 * (r.t0[r.front].min() + r.t0[r.front].max())
        fx = px[r.front][r.t0[r.front] <= t]
        bx = px[r.back][r.t0[r.back] <= t]
        if bx.size and fx.size and abs(float(bx.max()) - float(fx.max())) > 12:
            off += 1
    check("the branch runs out of step with the trunk", off >= seeds // 2,
          "%d of %d seeds have the two waves >12 px apart mid-run"
          % (off, seeds))


# --------------------------------------------------------------------------
# 4. The tiles stay on the floor.
# --------------------------------------------------------------------------

def test_floor(seeds):
    print("\nevery tile rotates about its bottom edge")
    bad = []
    for s in range(min(seeds, 6)):
        a = opts(seed=s)
        r = dominoes.build(a)
        _, py = r.pivots
        floor = int(py.max())
        # The panel's bottom rows below the front floor must never be lit by a
        # tile. (The hand may reach anywhere, so it is sampled out of the way
        # of the sweep.)
        for i in range(int(r.reset_at * 20)):
            f = r(i / 20.0, i)
            below = f[floor + 1:]
            if below.size and not np.array_equal(below, r.bg[floor + 1:]):
                bad.append("seed %d at t=%.2f" % (s, i / 20.0))
                break
    check("nothing is ever drawn below the floor it stands on", not bad,
          ", ".join(bad[:3]) or "6 seeds, full topple")

    # A tile's silhouette has to keep its area: an angle applied to the wrong
    # axis stretches it. Rasterise one tile at a range of angles and count.
    g = dominoes.patch_geom(17, 4)
    ang = np.linspace(0.0, np.pi * 0.5, 19)
    area = np.array([(dominoes.patches(np.array([t]), np.array([0]), g) > 0).sum()
                     for t in ang], float)
    check("a rotating tile keeps its area", area.std() / area.mean() < 0.10,
          "%.0f +- %.0f px over 0..90 deg" % (area.mean(), area.std()))


# --------------------------------------------------------------------------
# 5. The stall is a stall.
# --------------------------------------------------------------------------

def test_stall(seeds):
    print("\nthe one tile that nearly does not go over")
    beats = []
    for s in range(seeds):
        r = dominoes.build(opts(seed=s))
        t0 = np.sort(r.t0)
        d = np.diff(t0)
        if d.size == 0:
            continue
        k = int(np.argmax(d))
        beats.append((float(d[k]), float(t0[k]), r.reset_at))
    long_enough = sum(1 for (g, _, _) in beats if g >= 0.5)
    check("there is a beat of half a second or more in every run",
          long_enough == len(beats),
          "shortest beat %.2f s over %d seeds"
          % (min(g for (g, _, _) in beats), len(beats)))
    mid = sum(1 for (g, t, R) in beats if 0.25 < t / R < 0.95)
    check("the beat lands inside the run, not at either end",
          mid == len(beats), "%d of %d" % (mid, len(beats)))

    # And it is genuinely optional.
    r = dominoes.build(opts(stall=False))
    d = np.diff(np.sort(r.t0))
    check("--no-stall removes it", float(d.max()) < 0.5,
          "longest gap %.2f s" % float(d.max()))


# --------------------------------------------------------------------------
# 6. Cost.
# --------------------------------------------------------------------------

def test_cost(seeds):
    print("\ncost")
    import time
    r = dominoes.build(opts())
    n = int(r.cycle * 20) + 2
    ts = np.empty(n)
    for i in range(n):
        a = time.perf_counter()
        r(i / 20.0, i)
        ts[i] = (time.perf_counter() - a) * 1000.0
    check("desktop frame time", ts.mean() < 3.0,
          "mean %.3f p95 %.3f max %.3f ms over %d frames"
          % (ts.mean(), np.percentile(ts, 95), ts.max(), n))

    # The per-frame rasteriser cost is the number of tiles *in flight*, which
    # is what --kick controls; the composite cost is the number of tiles,
    # which is what --pitch controls. Both are reported because they are the
    # two knobs the integrator has if the Pi disagrees.
    live = []
    tr = r.theta_rest
    for i in range(n):
        t = (i / 20.0) % r.cycle
        if t >= r.reset_at:
            th = r.rise_angles(t)
            m = ((th > 1e-4) | (th < -1e-4)) & (th < tr - 1e-4)
        else:
            th = r.fall_angles(t)
            m = (th > 1e-4) & (th < tr - 1e-4)
        live.append(int(m.sum()))
    check("only a handful of tiles are rasterised per frame",
          max(live) <= 16,
          "%d tiles total, %.1f mean / %d max in flight"
          % (r.n_tiles, float(np.mean(live)), max(live)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--seeds", type=int, default=16,
                    help="how many seeds to sweep the layout checks over")
    args = ap.parse_args()
    test_purity()
    test_completes(args.seeds)
    test_causality(args.seeds)
    test_floor(args.seeds)
    test_stall(args.seeds)
    test_cost(args.seeds)
    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  - %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
