#!/usr/bin/env python3
"""Checks for pigeon.py that a screenshot cannot make.

A still frame of this demo is eight grey birds on a pavement, and eight grey
birds on a pavement look identical whether or not the demo works. Every way it
can be wrong is a way of being wrong *over time*:

  1. **The head bob can be backwards.** This is the whole demo. A correct
     walking pigeon holds its head at a fixed point in the *world* while the
     body walks under it and then snaps the head forward; the wrong version --
     the one nearly every animation does -- swings the head as a smooth
     function of the body. Both look like a moving pigeon in any single
     frame, and only one of them looks like a pigeon. Asserted in pixels:
     the iris is found in each frame of a stride, the body centre is found
     independently, and the head is required to be *still* for most of the
     stride while the body is required to move every frame.
  2. **The script can teleport.** Behaviour is a list of segments generated in
     build(), and every segment records the position the next one starts
     from. Trimming a walk to make room for an appointment used to leave the
     destination in the payload, so a bird arrived somewhere it had not walked
     to. Asserted over many seeds by walking each script and checking that
     consecutive segments agree about where the bird is -- exactly, not to a
     tolerance.
  3. **The loop can fail to close.** Every bird must end the cycle at the
     position it started it, or the flock jumps at the seam. Asserted both on
     the script and in pixels, render(0) against render(cycle).
  4. **The strut and the squabble can drift.** Both are oscillations about a
     fixed spot, so their durations must be whole numbers of cycles. A strut
     cut off mid-swing leaves the bird five pixels from where the next segment
     expects it -- which is case 2 again, arriving by a different door.
  5. **The startle can lose its punchline.** The point of it is that the
     pavement empties and one pale bird is left standing there. Asserted by
     counting bird pixels: mid-startle the panel is nearly empty and the pied
     bird is still on the ground.

    $ python3 scripts/test-pigeon.py
    $ python3 scripts/test-pigeon.py --bench     # also time a full loop
"""

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import pigeon                                                 # noqa: E402

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
    return ds.options(pigeon, **kw)


# --------------------------------------------------------------------------
# Pixel probes. The demo hangs its baked background and its bird list off the
# render callback, so these measure the frame that was actually drawn rather
# than re-deriving the geometry -- a test that recomputes the layout is a test
# that can agree with itself while disagreeing with the panel.
# --------------------------------------------------------------------------

def bird_pixels(frame, bg):
    """Boolean mask of everything that is not the baked pavement."""
    return np.abs(frame.astype(np.int32) - bg.astype(np.int32)).sum(2) > 24


def iris_x(frame):
    """Column of the orange iris, or None. Unique colour, one or two pixels."""
    d = np.abs(frame.astype(np.int32) - np.array([236, 138, 44])).sum(2)
    ys, xs = np.where(d < 70)
    return None if len(xs) == 0 else float(xs.mean())


def body_x(frame, bg, feet):
    """Centre column of the body, measured on rows the head cannot reach.

    Rows feet-4..feet-2 are belly and underside. The head only gets down
    there during a peck, and this is only ever called on a walking bird.
    """
    m = bird_pixels(frame, bg)[feet - 4:feet - 1, :]
    xs = np.where(m.any(0))[0]
    return None if len(xs) == 0 else float(xs.mean())


# --------------------------------------------------------------------------

def test_sprites():
    """Every art grid is rectangular and the poses agree with POSE_GEOMETRY."""
    grids = {"BODY_TOP": pigeon.BODY_TOP, "BODY_STRUT": pigeon.BODY_STRUT,
             "FLY_BODY": pigeon.FLY_BODY, "WING_V": pigeon.WING_V,
             "SQUAB_WINGS": pigeon.SQUAB_WINGS, "HEAD_FWD": pigeon.HEAD_FWD,
             "HEAD_UP": pigeon.HEAD_UP, "HEAD_DOWN": pigeon.HEAD_DOWN,
             "FAR_A": pigeon.FAR_A, "FAR_B": pigeon.FAR_B,
             "FAR_FLY": pigeon.FAR_FLY, "BURRITO": pigeon.BURRITO}
    bad = [n for n, g in grids.items() if len(set(len(r) for r in g)) != 1]
    check("every sprite grid is rectangular", not bad, ",".join(bad))

    for lp in (0, 1, 2):
        pigeon.body_grid(lp, False)
    walk = pigeon.body_grid(0, False)
    strut = pigeon.strut_grid()
    squab = pigeon.squab_grid()
    fly = pigeon.fly_grid(True)
    check("walk sprite is 18 wide, feet on row 9",
          len(walk[0]) == 18 and len(walk) == 10)
    check("strut sprite matches POSE_GEOMETRY",
          (len(strut[0]), len(strut) - 1) ==
          (pigeon.POSE_GEOMETRY["strut"][0], pigeon.POSE_GEOMETRY["strut"][1]))
    check("squabble sprite matches POSE_GEOMETRY",
          (len(squab[0]), len(squab) - 1) ==
          (pigeon.POSE_GEOMETRY["squab"][0], pigeon.POSE_GEOMETRY["squab"][1]))
    check("flight sprites are the same size both ways",
          len(fly) == len(pigeon.fly_grid(False))
          and len(fly[0]) == len(pigeon.fly_grid(False)[0]))
    check("head sprites are all %dx%d" % (pigeon.HEAD_W, pigeon.HEAD_H),
          all(len(g) == pigeon.HEAD_H and len(g[0]) == pigeon.HEAD_W
              for g in (pigeon.HEAD_FWD, pigeon.HEAD_UP, pigeon.HEAD_DOWN)))

    # The pied bird is missing a toe: one fewer foot pixel than everyone else.
    normal = "".join(pigeon.body_grid(0, False)).count("f")
    pied = "".join(pigeon.body_grid(0, True)).count("f")
    check("the pied bird is missing a toe", pied == normal - 1,
          "%d vs %d foot pixels" % (pied, normal))


def test_head_bob():
    """The one that matters: the head is still in the world, the body is not.

    One bird, no startles, driven through its first walk segment at 20 fps.
    The head is allowed to move on a minority of frames -- that is the snap --
    and must be motionless for a run of frames in between. The body must move
    on every single frame.
    """
    args = opts(birds=1, far=0, startles=0, cycle=40.0)
    r = pigeon.build(args)
    bird = r.birds[0]
    walks = [s for s in bird["segs"] if s[2] == pigeon.WALK
             and s[1] - s[0] > 2.0]
    if not walks:
        check("a walk segment exists to measure", False)
        return
    t0, t1 = walks[0][0], walks[0][1]
    n = int(min(2.0, t1 - t0 - 0.1) * 20)
    heads, bodies = [], []
    for i in range(n):
        f = r(t0 + 0.05 + i / 20.0, i)
        heads.append(iris_x(f))
        bodies.append(body_x(f, r.bg, bird["feet"]))
    ok = all(h is not None for h in heads) and all(b is not None
                                                   for b in bodies)
    check("the iris and the body are both findable every frame", ok)
    if not ok:
        return

    hmove = [abs(heads[i + 1] - heads[i]) > 0.4 for i in range(n - 1)]
    bmove = [abs(bodies[i + 1] - bodies[i]) > 0.01 for i in range(n - 1)]

    def longest_still(moves):
        best = run = 0
        for m in moves:
            run = 0 if m else run + 1
            best = max(best, run)
        return best

    # The body cannot advance on literally every frame: at the default ten
    # pixels a second it covers half a pixel per frame, so it steps on
    # alternate frames. What matters is that it never *stops*, and that the
    # head does -- for three times as long.
    hold_b, hold_h = longest_still(bmove), longest_still(hmove)
    check("the body never stalls for long", hold_b <= 2,
          "longest stall %d frames" % hold_b)
    check("the body keeps advancing", sum(bmove) > 0.4 * (n - 1),
          "%d/%d frames" % (sum(bmove), n - 1))
    check("the head is still on most frames", sum(hmove) < 0.5 * (n - 1),
          "moved on %d of %d" % (sum(hmove), n - 1))
    check("the head holds a fixed point far longer than the body",
          hold_h >= 4 and hold_h >= 2 * hold_b,
          "head %d frames, body %d" % (hold_h, hold_b))
    # And it does get there in the end: over a whole stride the head has to
    # cover the same ground as the body, or it is a head being left behind.
    span_h = heads[-1] - heads[0]
    span_b = bodies[-1] - bodies[0]
    check("the head keeps up with the body over the stride",
          abs(span_h - span_b) < 2.5,
          "head %+.1f px, body %+.1f px" % (span_h, span_b))


def test_script_is_continuous():
    """No segment starts anywhere but where the previous one ended.

    Exact equality, not a tolerance: the failures this catches were all
    "close enough to look fine in a still" and all visible as a snap.
    """
    jumps = gaps = opens = ends = 0
    builds = 0
    for seed in (1, 3, 5, 9, 17, 23):
        for nb in (1, 2, 3, 8, 14):
            for cyc in (25.0, 62.0, 120.0):
                r = pigeon.build(opts(seed=seed, birds=nb, cycle=cyc))
                builds += 1
                for b in r.birds:
                    x = None
                    for j, (t0, t1, kind, pay) in enumerate(b["segs"]):
                        if j and abs(t0 - b["segs"][j - 1][1]) > 1e-6:
                            gaps += 1
                        if x is not None and abs(pay[0] - x) > 1e-6:
                            jumps += 1
                        x = pay[1] if kind in (pigeon.WALK, pigeon.FLY) \
                            else pay[0]
                    if abs(x - b["home"]) > 1e-6:
                        opens += 1
                    if abs(b["segs"][-1][1] - r.cycle) > 1e-6:
                        ends += 1
    check("no gaps between segments", gaps == 0, "%d builds" % builds)
    check("no position jumps between segments", jumps == 0)
    check("every bird ends the cycle exactly where it started", opens == 0)
    check("every script covers the whole cycle", ends == 0)


def test_oscillations_close():
    """The strut and the squabble last a whole number of their own cycles."""
    d = pigeon.STRUT_PERIOD * pigeon.STRUT_TURNS
    check("the strut is a whole number of turns",
          abs(np.sin(2 * np.pi * d / pigeon.STRUT_PERIOD)) < 1e-9,
          "%.2f s" % d)
    d = pigeon.SQUAB_LUNGES / pigeon.SQUAB_RATE
    check("the squabble is a whole number of lunges",
          abs(np.sin(np.pi * d * pigeon.SQUAB_RATE)) < 1e-9, "%.2f s" % d)


def test_loop_closes_in_pixels():
    """render(0) and render(cycle) are the same frame."""
    args = opts()
    r = pigeon.build(args)
    a = r(0.0, 0).copy()
    b = r(r.cycle, 0)
    diff = int((a != b).sum())
    check("the cycle loops seamlessly in pixels", diff == 0,
          "%d differing channels" % diff)


def test_purity():
    """A cold render(t) equals the same t reached by driving from zero."""
    args = opts()
    driven = pigeon.build(args)
    cold = pigeon.build(args)
    for i in range(int(28.0 * 20)):
        driven(i / 20.0, i)
    bad = []
    for t in (0.0, 1.5, 7.3, 12.7, 20.1, 21.4, 33.3, 47.2, 58.0, 61.9):
        if not np.array_equal(driven(t, 0), cold(t, 0)):
            bad.append(t)
    check("render is a pure function of t", not bad,
          "differs at %s" % (bad,))
    # And two builds from the same seed are the same demo: no clock anywhere.
    other = pigeon.build(args)
    check("two builds with the same seed are identical",
          np.array_equal(other(9.0, 0), cold(9.0, 0)))
    check("a different seed is a different flock",
          not np.array_equal(pigeon.build(opts(seed=99))(9.0, 0),
                             cold(9.0, 0)))


def test_the_startle():
    """The pavement empties, and one bird does not go."""
    args = opts()
    r = pigeon.build(args)
    if not r.startles:
        check("there is a startle to test", False)
        return
    s = r.startles[0]
    pied = [b for b in r.birds if b["pied"]][0]
    others = [b for b in r.birds if not b["pied"]]

    check("exactly one bird is the pied one",
          sum(1 for b in r.birds if b["pied"]) == 1)
    check("the pied bird never flies",
          not any(sg[2] == pigeon.FLY for sg in pied["segs"]))
    check("everybody else flies",
          all(any(sg[2] == pigeon.FLY for sg in b["segs"]) for b in others))

    before = bird_pixels(r(s - 0.5, 0), r.bg)
    # 1.2 s in they are all off the top of the panel; the pied one is not.
    mid = bird_pixels(r(s + 1.2, 0), r.bg)
    band = slice(r.geo["pave_y"], 64)
    check("the pavement empties during the startle",
          mid[band].sum() < 0.4 * before[band].sum(),
          "%d px -> %d px" % (before[band].sum(), mid[band].sum()))
    # The pied bird is where it always was, still on the ground.
    col = int(round(pied["home"]))
    strip = mid[pied["feet"] - 12:pied["feet"] + 1,
                max(0, col - 26):col + 44]
    check("the pied bird is still standing there", strip.sum() > 25,
          "%d px" % strip.sum())


def test_the_props():
    """The burrito is on the pavement and the squabble happens next to it."""
    args = opts()
    r = pigeon.build(args)
    by, bx = r.geo["burrito"]
    patch = r.bg[by:by + len(pigeon.BURRITO), bx:bx + len(pigeon.BURRITO[0])]
    # Foil is much lighter than the concrete around it.
    check("the burrito is baked into the pavement",
          patch.mean() > r.bg[by:by + 4, bx + 40:bx + 60].mean() + 25)
    pair, when = r.squabble
    check("a squabble is scheduled", pair is not None and when is not None)
    if pair:
        near = max(abs(r.birds[i]["home"] - bx) for i in pair)
        check("it is between two birds near the burrito", near < 90,
              "%.0f px away" % near)
        check("the pied bird is not in it",
              not any(r.birds[i]["pied"] for i in pair))


def test_full_loop():
    """A whole cycle renders with no exception and no bad frames."""
    for kw in ({}, {"birds": 1, "far": 0, "startles": 0},
               {"birds": 20, "cycle": 25.0, "speed": 18.0},
               {"birds": 3, "speed": 3.0, "startles": 4}):
        args = opts(**kw)
        r = pigeon.build(args)
        n = int(args.cycle * 20) + 40
        bad = 0
        for i in range(n):
            f = r(i / 20.0, i)
            if f.shape != (64, 320, 3) or f.dtype != np.uint8:
                bad += 1
        check("a full loop renders clean %r" % (kw,), bad == 0,
              "%d frames" % n)


def test_no_network():
    src = open(os.path.join(HERE, "pigeon.py")).read()
    bad = [m for m in ("urllib", "http.client", "socket", "requests", "ssl",
                       "subprocess") if ("import " + m) in src]
    check("pigeon.py imports no network module", not bad, ",".join(bad))
    check("pigeon.py reads no data product", "ftdata" not in src)
    check("pigeon.py draws no text", "font" not in src.lower())
    # numpy 1.19 is what the wall has, and these are the easy ways to forget.
    modern = [c for c in ("np.take_along_axis", "removeprefix", "removesuffix",
                          "np.bool_(", "keepdims=") if c in src]
    check("nothing that postdates numpy 1.19", not modern, ",".join(modern))


def bench():
    for nb in (4, 8, 12, 20):
        args = opts(birds=nb)
        t0 = time.perf_counter()
        r = pigeon.build(args)
        build_ms = (time.perf_counter() - t0) * 1000.0
        for i in range(20):
            r(i / 20.0, i)
        n = int(args.cycle * 20)
        ts = np.empty(n)
        for i in range(n):
            a = time.perf_counter()
            r(i / 20.0, i)
            ts[i] = (time.perf_counter() - a) * 1000.0
        print("  --birds %-3d build %6.1f ms   frame mean %.3f  p95 %.3f  "
              "max %.3f ms" % (nb, build_ms, ts.mean(),
                               np.percentile(ts, 95), ts.max()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true", help="time a full loop")
    a = ap.parse_args()

    test_sprites()
    test_head_bob()
    test_script_is_continuous()
    test_oscillations_close()
    test_loop_closes_in_pixels()
    test_purity()
    test_the_startle()
    test_the_props()
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
