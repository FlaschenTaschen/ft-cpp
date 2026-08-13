#!/usr/bin/env python3
"""Checks for marble.py that a screenshot cannot make.

The panel is a physics solve, and every way it can be wrong is a way of
looking *nearly* right in a still frame:

  1. **The marble can fail the loop.** The whole run is paid for by the first
     ramp, and the loop only works if `v^2 >= g r` at the top. Nothing in a
     screenshot says whether the margin is 30% or -5%, and at -5% the panel
     shows a ball serenely gliding round the inside of a loop it could not
     physically stay on. Asserted from the solved table.
  2. **Gravity can stop looking like gravity.** Asserted directly: speed rises
     monotonically down the steep ramp, falls climbing to the see-saw's pivot,
     and rises again once the lever has tipped. If any of those flips sign the
     kinematics have been replaced by a tween somewhere.
  3. **It can teleport.** One marble, one journey. The only permitted
     discontinuity in position is the Newton's cradle transfer, and it must be
     exactly the length of the queue -- if it drifts, the ball appears out of
     nowhere next to the row rather than out of the end of it. Asserted by
     walking a whole lap at the frame rate and counting jumps.
  4. **The mechanisms can overlap.** Each mechanism is drawn from a single
     patch stack, so two marbles inside one at the same time would be drawn
     as one. Asserted for every marble count the demo offers.
  5. **The screw can be out of phase.** The lift is the one thing on the panel
     that is not gravity, and its period has to divide the lap *and* divide it
     by the number of marbles, or a marble arrives at the bottom between two
     threads and rides up through the metal. Asserted arithmetically.
  6. **It can stop being a pure function of t.** Checked by driving one build
     frame by frame from zero and comparing against cold calls on another.

    $ python3 scripts/test-marble.py
    $ python3 scripts/test-marble.py --bench      # also time a full loop
"""

import argparse
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import marble                                                 # noqa: E402

FAILED = []
PASSED = [0]


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-56s %s" % (name, detail))
    else:
        print("  FAIL %-56s %s" % (name, detail))
        FAILED.append(name)


def speeds(run):
    """Speed at each sample of the solved table, from the table itself."""
    t = np.array(run.T)
    x = np.array(run.X)
    z = np.array(run.Z)
    h = np.array(run.Hh)
    ds3 = np.sqrt(np.diff(x) ** 2 + np.diff(z) ** 2 + np.diff(h) ** 2)
    dt = np.maximum(np.diff(t), 1e-9)
    return t, ds3 / dt


# --------------------------------------------------------------------------

def test_loop(r):
    """The loop is a constraint, not a drawing."""
    run = r.run
    t = np.array(run.T)
    h = np.array(run.Hh)
    _, v = speeds(run)
    a, b = run.marks[marble.MARK_LOOP]
    i0, i1 = np.searchsorted(t, a), np.searchsorted(t, b)
    top = i0 + int(np.argmin(h[i0:i1]))
    v2 = float(v[min(top, len(v) - 1)]) ** 2
    need = 190.0 * marble.LOOP_R                # default --gravity
    check("loop: v^2 at the top clears g*r", v2 >= need * 1.15,
          "%.0f vs %.0f, margin %.0f%%" % (v2, need, (v2 / need - 1) * 100))
    check("loop: the top of the arc is on the panel",
          h[top] > 2.0 and h[top] < marble.LOOP_C[1],
          "y = %.1f" % h[top])
    # And the bottom entry speed has to clear the whole-loop condition too.
    vb = float(v[i0]) ** 2
    check("loop: entry speed clears 5 g r", vb >= 5.0 * need,
          "%.0f vs %.0f" % (vb, 5.0 * need))


def test_gravity(r):
    """Accelerates downhill, decelerates uphill, and the numbers say so."""
    run = r.run
    t, v = speeds(run)
    tv = t[:-1]

    a, b = run.marks[marble.MARK_RAMP1]
    seg = v[(tv > a + 0.05) & (tv < b - 0.05)]
    check("ramp: speed rises all the way down the steep ramp",
          bool(np.all(np.diff(seg) > -1e-6)),
          "%.1f -> %.1f px/s" % (seg[0], seg[-1]))

    a, b = run.marks[marble.MARK_SEESAW]
    x = np.array(run.X)[:-1]
    on = (tv >= a + 0.03) & (tv <= b - 0.01)
    # u is the marble's signed distance past the pivot; it enters from the
    # high-x side because the lower deck runs right to left.
    u = marble.SEE_CX - x[on]
    vv = v[on]
    up = vv[u < -2.0]
    down = vv[u > marble.SEE_EPS + 2.0]
    check("see-saw: decelerates climbing to the pivot",
          up[-1] < up[0] - 1.0, "%.1f -> %.1f px/s" % (up[0], up[-1]))
    check("see-saw: accelerates once the lever has tipped",
          down[-1] > down[0] + 0.5, "%.1f -> %.1f px/s" % (down[0], down[-1]))
    check("see-saw: the lever really does flip sign",
          marble._see_slope(-marble.SEE_L) > 0 >
          marble._see_slope(marble.SEE_L),
          "%.3f -> %.3f" % (marble._see_slope(-marble.SEE_L),
                            marble._see_slope(marble.SEE_L)))

    # The jump is ballistic: no rail, so gravity alone, so the vertical speed
    # must grow linearly. Fit it and compare the slope against --gravity.
    a, b = run.marks[marble.MARK_JUMP]
    i0, i1 = np.searchsorted(t, a), np.searchsorted(t, b)
    hh = np.array(run.Hh)[i0:i1]
    tt = t[i0:i1]
    vy = np.diff(hh) / np.maximum(np.diff(tt), 1e-9)
    g = np.polyfit(tt[:-1], vy, 1)[0]
    check("jump: the flight is free fall at exactly g", abs(g - 190.0) < 6.0,
          "fitted %.1f px/s^2" % g)


def test_continuity(r):
    """One marble, one journey: the cradle is the only permitted jump."""
    n = int(r.period * 60)
    prev = None
    jumps = []
    for i in range(n + 1):
        t = i * r.period / n
        x, y, _, _ = r.at(t)
        if prev is not None:
            d = math.hypot(x - prev[0], y - prev[1])
            if d > 4.0:
                jumps.append((t, d))
        prev = (x, y)
    check("continuity: exactly one discontinuity in a lap", len(jumps) == 1,
          "%d found" % len(jumps))
    if jumps:
        want = marble.CRADLE_HIT - marble.CRADLE_STOP
        # Sampled at 60 Hz, so the measured jump also contains one step of
        # ordinary rolling and the queue's slight fall; 2 px covers both.
        check("continuity: it is the length of the cradle's queue",
              abs(jumps[0][1] - want) < 2.0,
              "%.1f px, queue is %.1f px" % (jumps[0][1], want))
        check("continuity: it happens at the cradle",
              abs(jumps[0][0] - r.run.marks[marble.MARK_CRADLE][1]) < 0.1,
              "t = %.2f s" % jumps[0][0])


def test_onscreen(r):
    """Every marble stays on the panel, and the lap closes."""
    n = 600
    bad = []
    for i in range(n):
        t = i * r.period / n
        x, y, _, _ = r.at(t)
        if not (1.0 <= x <= marble.W - 1.0 and 1.0 <= y <= marble.H - 2.0):
            bad.append((t, x, y))
    check("bounds: the marble never leaves the panel", not bad,
          "worst %s" % (bad[0] if bad else "-"))
    x0, y0, _, _ = r.at(0.0)
    x1, y1, _, _ = r.at(r.period - 1e-6)
    check("lap: the run closes on itself",
          math.hypot(x1 - x0, y1 - y0) < 1.0,
          "start (%.1f, %.1f) end (%.1f, %.1f)" % (x0, y0, x1, y1))


def test_mechanism_exclusivity():
    """No mechanism may hold two marbles at once: it is one patch stack."""
    for nm in (1, 2, 3, 4, 5):
        r = ds.build(marble, marbles=nm)
        run = r.run
        worst = None
        # Only the three patch-driven mechanisms are exclusive. The loop and
        # the funnel draw marbles as plain sprites, so two of them in there at
        # once is legal -- it happens at --marbles 5, and it looks fine.
        for key in (marble.MARK_SEESAW, marble.MARK_CRADLE, marble.MARK_SCREW):
            a, b = run.marks[key]
            dwell = b - a
            if key == marble.MARK_SEESAW:
                dwell += marble.RESET
            if key == marble.MARK_CRADLE:
                dwell += marble.SETTLE
            gap = r.period / nm
            if worst is None or dwell / gap > worst[1]:
                worst = (key, dwell / gap, dwell, gap)
        check("spacing: marbles=%d, longest dwell fits the gap" % nm,
              worst[1] < 1.0,
              "mark %d dwells %.2f s, marbles are %.2f s apart"
              % (worst[0], worst[2], worst[3]))


def test_screw_phase():
    """The lift's period must divide the lap, and divide it nm ways."""
    for nm in (1, 2, 3, 4, 5):
        r = ds.build(marble, marbles=nm)
        omega = 2.0 * math.pi * marble.SCREW_TURNS / r.ride
        turns = omega * r.period / (2.0 * math.pi)
        check("screw: marbles=%d, whole turns per lap" % nm,
              abs(turns - round(turns)) < 1e-6, "%.6f turns" % turns)
        check("screw: marbles=%d, and a thread per marble slot" % nm,
              abs(turns / nm - round(turns / nm)) < 1e-6,
              "%.4f turns between marbles" % (turns / nm))
        check("screw: marbles=%d, ride is a sane length" % nm,
              1.0 < r.ride < 4.0, "%.2f s" % r.ride)


def test_purity():
    """render(t) must not depend on how you got to t."""
    a = ds.build(marble)
    b = ds.build(marble)
    for i in range(240):                       # drive one from zero
        a(i / 20.0, i)
    bad = 0
    for t in (0.0, 1.7, 4.4, 7.9, 11.1, 13.0):
        fa = np.array(a(t, 0))
        fb = np.array(b(t, 999))
        if not np.array_equal(fa, fb):
            bad += 1
    check("purity: cold render matches a driven one", bad == 0,
          "%d of 6 times differ" % bad)
    c = ds.build(marble)
    f0 = np.array(c(3.3, 0))
    f1 = np.array(c(3.3 + c.period, 7))
    check("purity: the lap is exactly periodic", np.array_equal(f0, f1))
    d = ds.build(marble, seed=7)
    e = ds.build(marble, seed=7)
    check("determinism: the same seed gives the same panel",
          np.array_equal(np.array(d(5.0, 0)), np.array(e(5.0, 0))))


def test_runs_headless():
    r = ds.build(marble)
    n = int(r.period * 20) + 5
    shape_ok = True
    lit = 0
    for i in range(n):
        f = r(i / 20.0, i)
        if f.shape != (64, 320, 3) or f.dtype != np.uint8:
            shape_ok = False
        lit = max(lit, int(f.max()))
    check("headless: a full loop renders (64,320,3) uint8", shape_ok,
          "%d frames" % n)
    check("headless: something is actually lit", lit > 200, "peak %d" % lit)


def bench():
    r = ds.build(marble)
    n = int(r.period * 20)
    ts = []
    for i in range(n):
        a = time.perf_counter()
        r(i / 20.0, i)
        ts.append((time.perf_counter() - a) * 1000.0)
    ts = np.array(ts)
    t0 = time.time()
    ds.build(marble)
    print("\n  build      %.3f s" % (time.time() - t0))
    print("  ms/frame   mean %.3f  p95 %.3f  max %.3f  over %d frames"
          % (ts.mean(), np.percentile(ts, 95), ts.max(), n))
    print("  lap        %.2f s at 20 fps" % r.period)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bench", action="store_true")
    args = ap.parse_args()

    r = ds.build(marble)
    print("marble: lap %.2f s (gravity %.2f s + lift %.2f s), %d samples"
          % (r.period, r.t_grav, r.ride, len(r.run.T)))

    print("\nphysics")
    test_loop(r)
    test_gravity(r)
    print("\ncontinuity")
    test_continuity(r)
    test_onscreen(r)
    print("\nmachinery")
    test_mechanism_exclusivity()
    test_screw_phase()
    print("\npanel")
    test_purity()
    test_runs_headless()

    if args.bench:
        bench()

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
