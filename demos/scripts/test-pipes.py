#!/usr/bin/env python3
"""Checks for pipes.py that a screenshot cannot make.

This demo can draw a confident, pretty, wrong picture in several ways, and none
of them look wrong in a thumbnail:

  1. **The z-buffer can stop being consulted.** Without it the picture is still
     a mass of shaded tubes; it just stops being a *volume*, because whichever
     pipe happened to be drawn last is in front. Asserted by drawing the same
     finished lattice with the primitives in a shuffled order and requiring
     essentially the same pixels: a z-buffered scene is order independent, a
     painter's-algorithm scene is not.
  2. **The walk can revisit a cell.** A self-avoiding walk that quietly stops
     avoiding itself draws pipes growing through each other, which reads as a
     rendering bug rather than as a walk bug and would be chased in the wrong
     file. Asserted on the cells themselves.
  3. **The pipes can stop interleaving.** Every pipe is advanced in time order
     so that at most `--pipes` tubes are growing at once; if that scheduling
     breaks, the whole lattice grows in one burst and the per-frame cost --
     which is one capsule per growing tip -- multiplies. Asserted against the
     timeline, and separately against the fixed-size window render() scans.
  4. **The memoised world can drift from a cold render.** The frame buffer is a
     cache keyed on an integer, restored from snapshots on a rewind. If
     "restore and walk forward" were not bit identical to "walk from zero", the
     wall and the preview baker would show different pictures of the same
     second. Asserted with array_equal, forwards, backwards, across a run
     boundary and across the loop wrap.
  5. **The camera fit can push the lattice off the panel.** Solved from the
     projected lattice rather than hardcoded, which is right up until an angle
     changes; asserted by measuring the lit pixels of a finished run.

Everything here is seeded and builds with pipes' own defaults, so the checks are
deterministic.

    $ python3 scripts/test-pipes.py
    $ python3 scripts/test-pipes.py --bench      # also time a full loop
"""

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import pipes                                                  # noqa: E402

FAILED = []
PASSED = [0]

FPS = 20


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(pipes, **kw)


def lit(frame, thresh=24):
    return frame.max(axis=2) > thresh


# --------------------------------------------------------------------------
# The walk.
# --------------------------------------------------------------------------

def test_walk():
    args = opts()
    rng = np.random.RandomState(args.seed)
    prims = pipes._walk(rng, args, 3)
    check("the walk produced something", len(prims) > 40, "%d primitives" % len(prims))

    # Every cell a run passes through, including both endpoints. A tube that
    # starts where the last one ended shares that cell, which is the elbow, so
    # the shared endpoints are counted once.
    seen = set()
    dupes = 0
    offgrid = 0
    diagonal = 0
    toolong = 0
    for kind, c0, c1, _ci, _t0, _t1 in prims:
        for c in (c0, c1):
            if not (0 <= c[0] < pipes.NX and 0 <= c[1] < pipes.NY
                    and 0 <= c[2] < pipes.NZ):
                offgrid += 1
        if kind != pipes.TUBE:
            continue
        d = (c1[0] - c0[0], c1[1] - c0[1], c1[2] - c0[2])
        moving = [i for i in range(3) if d[i]]
        if len(moving) != 1:
            diagonal += 1
            continue
        ax = moving[0]
        n = abs(d[ax])
        if n < 1 or n > args.max_run:
            toolong += 1
        stp = 1 if d[ax] > 0 else -1
        c = list(c0)
        for _ in range(n):
            c[ax] += stp
            key = tuple(c)
            if key in seen:
                dupes += 1
            seen.add(key)

    check("every run is axis aligned", diagonal == 0, "%d diagonal" % diagonal)
    check("every run is 1..--max-run cells", toolong == 0, "%d out of range" % toolong)
    check("nothing leaves the lattice", offgrid == 0, "%d off grid" % offgrid)
    check("the walk is self avoiding", dupes == 0, "%d cells revisited" % dupes)
    check("the walk respects --fill",
          abs(len(seen) - args.fill * pipes.NX * pipes.NY * pipes.NZ) <= 12,
          "%d cells of %d" % (len(seen), pipes.NX * pipes.NY * pipes.NZ))

    # An elbow ball at the end of every run, or the tube ends are open.
    joints = set()
    for kind, c0, c1, _ci, _t0, _t1 in prims:
        if kind == pipes.JOINT:
            joints.add(c1)
    missing = sum(1 for k, _c0, c1, _ci, _t0, _t1 in prims
                  if k == pipes.TUBE and c1 not in joints)
    check("every run ends in a joint", missing == 0, "%d bare ends" % missing)


def test_timeline():
    args = opts()
    r = pipes.build(args)
    bad_order = 0
    bad_span = 0
    worst_together = 0
    worst_window = 0
    for R in r.runs:
        t0, t1, kind = R["t0"], R["t1"], R["kind"]
        if np.any(np.diff(t1) < -1e-6):
            bad_order += 1
        if np.any(t1 < t0 - 1e-6):
            bad_span += 1
        # Sample the whole growth at frame rate and count what is in flight.
        for i in range(int(R["grow"] * FPS) + 1):
            t = i / float(FPS)
            live = np.nonzero((t0 <= t) & (t < t1) & (kind == pipes.TUBE))[0]
            worst_together = max(worst_together, len(live))
            done = int(np.searchsorted(t1, t, side="right"))
            if len(live):
                worst_window = max(worst_window, int(live.max()) - done)
        done_all = int(np.searchsorted(t1, R["grow"], side="right"))
        check("run finishes everything it started",
              done_all == len(t1), "%d of %d" % (done_all, len(t1)))

    check("primitives are sorted by finish time", bad_order == 0)
    check("no primitive ends before it starts", bad_span == 0)
    check("at most --pipes tubes grow at once", worst_together <= args.pipes,
          "worst %d, --pipes %d" % (worst_together, args.pipes))
    # This is why render() finds its live tips with a test over the whole list
    # rather than by scanning a window forward from the finished index: the
    # list is ordered by finish time, and a tip can sit a long way behind it.
    # A window of four per pipe -- which the first version of this used --
    # would have dropped a growing pipe for a second at a time.
    check("a fixed scan window would not have been safe", worst_window > 0,
          "worst live tip is %d entries past the finished index" % worst_window)


# --------------------------------------------------------------------------
# The renderer.
# --------------------------------------------------------------------------

def _finished(r, run=0):
    """A frame with run `run` complete and held, i.e. every primitive drawn."""
    t = float(r.cycle_starts[run]) + r.runs[run]["grow"] + 0.5
    return r(t, int(t * FPS)).copy(), t


def test_zbuffer():
    """Shuffling the draw order must not change the picture.

    That is the definition of a z-buffer and the negation of a painter's
    algorithm, and it is the one property that makes crossing pipes read as a
    volume rather than as a pile. The antialiased silhouettes blend with
    whatever is behind them, so a handful of edge pixels legitimately differ.
    """
    r = pipes.build(opts())
    base, t = _finished(r)

    R = r.runs[0]
    m = len(R["kind"])
    rng = np.random.RandomState(4)
    perm = rng.permutation(m)
    for key in ("kind", "colour", "p0", "p1", "is_tube"):
        R[key] = R[key][perm]
    # Force the cache to rebuild with the shuffled geometry.
    r(0.0, 0)
    other = r(t, int(t * FPS)).copy()

    diff = np.any(base != other, axis=2)
    on = lit(base) | lit(other)
    frac = diff.sum() / float(max(1, on.sum()))
    check("shuffling the draw order barely changes the picture", frac < 0.06,
          "%.1f%% of lit pixels differ" % (100.0 * frac))

    # And a sanity floor: if the scene had no overlaps at all the check above
    # would pass trivially, so require that pipes genuinely occlude each other.
    check("the lattice is dense enough for occlusion to matter",
          on.sum() > 0.16 * base.shape[0] * base.shape[1],
          "%d lit pixels" % int(on.sum()))


def test_camera_fills_the_panel():
    # Against a nearly full lattice, not a default run: a 30% run leaves holes
    # at the edges by chance, and this check is about the camera fit, not the
    # walk's luck.
    r = pipes.build(opts(fill=0.95, runs=1, speed=12.0))
    frame, _t = _finished(r)
    ys, xs = np.nonzero(lit(frame))
    w = xs.max() - xs.min() + 1
    h = ys.max() - ys.min() + 1
    check("the lattice spans the panel's width", w >= 0.85 * frame.shape[1],
          "%d of %d columns" % (w, frame.shape[1]))
    check("the lattice spans the panel's height", h >= 0.70 * frame.shape[0],
          "%d of %d rows" % (h, frame.shape[0]))

    cam = r.camera
    near = cam.focal * pipes.TUBE_R / cam.znear
    far = cam.focal * pipes.TUBE_R / cam.zfar
    check("perspective survives: near tubes are fatter than far ones",
          near / far > 1.35, "%.1f px vs %.1f px diameter" % (2 * near, 2 * far))


def test_wipe_clears():
    args = opts()
    r = pipes.build(args)
    grow = r.runs[0]["grow"]
    counts = []
    for i in range(int((grow + args.hold) * FPS), int(r.cycle_starts[1] * FPS)):
        counts.append(int(lit(r(i / float(FPS), i)).sum()))
    check("the eraser only ever removes", all(b <= a + 130 for a, b in
                                              zip(counts, counts[1:])),
          "%d frames of wipe" % len(counts))
    last = r(float(r.cycle_starts[1]) - 1.0 / FPS, 0)
    check("the panel is black when the next run starts", last.max() < 40,
          "brightest pixel %d" % int(last.max()))


def test_teapot():
    """The easter egg exists, is bigger than the elbow it replaced, and is off
    on request. It is drawn out of the same primitives as everything else, so
    the way it can silently vanish is by being placed and then never revealed."""
    r = pipes.build(opts())
    R = r.runs[0]
    pots = [i for i in range(len(R["kind"])) if R["kind"][i] == pipes.TEAPOT]
    check("a teapot was placed", len(pots) == 1, "%d found" % len(pots))
    if not pots:
        return
    off = pipes.build(opts(teapot=0))
    check("--teapot 0 removes it",
          not any(k == pipes.TEAPOT for k in off.runs[0]["kind"]))

    # --teapot 0 changes nothing except the kind flag on that one elbow, so
    # differencing the two finished frames isolates the teapot exactly -- which
    # differencing across time cannot do, because the tips have moved.
    t = float(R["t1"][pots[0]]) + 0.4
    with_pot = r(t, 0).copy()
    without = off(t, 0).copy()
    ys, xs = np.nonzero(np.any(with_pot != without, axis=2))
    check("the teapot is drawn", len(ys) > 120, "%d pixels differ" % len(ys))
    if len(ys):
        w = xs.max() - xs.min() + 1
        h = ys.max() - ys.min() + 1
        check("the teapot is much bigger than the elbow it replaced",
              20 <= w <= 40 and 12 <= h <= 30,
              "%dx%d px, the elbow it replaced is about 8x8" % (w, h))


# --------------------------------------------------------------------------
# Purity.
# --------------------------------------------------------------------------

TIMES = (0.0, 4.35, 17.0, 29.9, 31.05, 34.0, 35.3, 36.0, 52.1, 71.5, 99.0)


def test_purity():
    """Cold calls must equal the same moment reached frame by frame from zero.

    The frame buffer is a memoised cache, so this is the check that the cache
    is a *function* rather than an accumulator: bit identical, not close.
    """
    stepped = pipes.build(opts())
    want = {}
    n = int(max(TIMES) * FPS) + 2
    for i in range(n):
        t = i / float(FPS)
        f = stepped(t, i)
        for x in TIMES:
            if abs(t - x) < 1e-9:
                want[x] = f.copy()

    cold = pipes.build(opts())
    bad = [x for x in sorted(want) if not np.array_equal(cold(x, int(x * FPS)),
                                                         want[x])]
    check("a cold render equals a stepped one", not bad,
          "differs at %s" % (bad or "nothing"))

    # Backwards, which is a preview rewind and is what exercises the snapshots.
    rew = pipes.build(opts())
    bad = [x for x in sorted(want, reverse=True)
           if not np.array_equal(rew(x, int(x * FPS)), want[x])]
    check("a rewind equals a stepped one", not bad,
          "differs at %s" % (bad or "nothing"))

    # And across the loop wrap: t and t + period are the same frame.
    wrap = pipes.build(opts())
    p = wrap.period
    bad = []
    for x in (2.0, 33.0, 74.0):
        a = wrap(x, 0).copy()
        b = wrap(x + p, 0)
        if not np.array_equal(a, b):
            bad.append(x)
    check("the loop wraps exactly", not bad, "differs at %s" % (bad or "nothing"))


def test_full_loop():
    args = opts()
    r = pipes.build(args)
    n = int(r.period * FPS) + 3
    blank = 0
    for i in range(n):
        f = r(i / float(FPS), i)
        if f.shape != (args.height, args.width, 3) or f.dtype != np.uint8:
            check("frame shape and dtype", False, str((f.shape, f.dtype)))
            return
        if f.max() == 0:
            blank += 1
    check("a full loop renders", True, "%d frames, period %.1f s" % (n, r.period))
    # Black frames are legitimate only at the very end of a wipe.
    check("the panel is not blank for long", blank < 0.06 * n,
          "%d blank frames of %d" % (blank, n))


def test_variants():
    """Every option combination the integrator is likely to reach for."""
    cases = [{"pipes": 1}, {"pipes": 5}, {"no_aa": True}, {"teapot": 0},
             {"scheme": "candy"}, {"fill": 0.5}, {"fill": 0.05}, {"runs": 1},
             {"speed": 6.0}, {"yaw": 0.0, "pitch": 0.0}, {"depth_ratio": 1.02},
             {"fog": 0.0}, {"seed": 99}, {"max_run": 1}, {"hold": 0.0}]
    bad = []
    for kw in cases:
        try:
            r = pipes.build(opts(**kw))
            for i in range(0, int(r.period * FPS) + 2, 3):
                r(i / float(FPS), i)
        except Exception as exc:                              # noqa: BLE001
            bad.append("%s: %s" % (kw, exc))
    check("every option variant renders a whole loop", not bad, "; ".join(bad))


def test_no_network():
    src = open(os.path.join(HERE, "pipes.py")).read()
    bad = [m for m in ("urllib", "http.client", "socket", "requests", "ssl",
                       "subprocess") if ("import " + m) in src]
    check("pipes.py imports no network module", not bad, ",".join(bad))
    check("pipes.py reads no data product", "ftdata" not in src)

    # numpy 1.19 is what the wall has. These are the calls it is easy to reach
    # for by habit and that are not in it.
    modern = [c for c in ("default_rng", "np.random.Generator", "take_along_axis",
                          "removeprefix", "removesuffix", "np.bool8") if c in src]
    check("no post-1.19 numpy calls", not modern, ",".join(modern))


def bench():
    args = opts()
    r = pipes.build(args)
    for i in range(40):
        r(i / float(FPS), i)
    n = int(r.period * FPS)
    ts = np.empty(n)
    for i in range(n):
        a = time.perf_counter()
        r(i / float(FPS), i)
        ts[i] = (time.perf_counter() - a) * 1000.0
    print("  frame ms over %d frames: mean %.3f  p95 %.3f  max %.3f"
          % (n, ts.mean(), np.percentile(ts, 95), ts.max()))
    g = ts[:int(r.runs[0]["grow"] * FPS)]
    print("  while growing:           mean %.3f  p95 %.3f  max %.3f"
          % (g.mean(), np.percentile(g, 95), g.max()))
    # Cost is one capsule per growing tip, so it should be close to linear.
    for p in (1, 2, 3, 4):
        rr = pipes.build(opts(pipes=p))
        for i in range(20):
            rr(i / float(FPS), i)
        m = int(rr.runs[0]["grow"] * FPS)
        q = np.empty(m)
        for i in range(m):
            a = time.perf_counter()
            rr(i / float(FPS), i)
            q[i] = (time.perf_counter() - a) * 1000.0
        print("    --pipes %d while growing: mean %.3f  p95 %.3f"
              % (p, q.mean(), np.percentile(q, 95)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true", help="time a full loop")
    a = ap.parse_args()

    test_walk()
    test_timeline()
    test_zbuffer()
    test_camera_fills_the_panel()
    test_wipe_clears()
    test_teapot()
    test_purity()
    test_full_loop()
    test_variants()
    test_no_network()
    if a.bench:
        bench()

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
