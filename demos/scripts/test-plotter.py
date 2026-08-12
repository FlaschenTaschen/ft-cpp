#!/usr/bin/env python3
"""Checks for plotter.py that a screenshot cannot make.

This demo is a *memoised* renderer: the ink on the sheet is defined as a pure
function of one integer -- how many moves of the tour have finished -- and then
cached across frames so that a frame only has to rasterise the two or three
segments that were completed since the last one. That is the whole design, and
it has exactly three ways to be quietly wrong, none of which look wrong:

  1. **The cache can drift.** If "restore from a snapshot and walk forward" is
     not bit identical to "walk forward from zero", the wall and the preview
     baker draw slightly different sheets and nobody ever notices -- until a
     transition crossfades two versions of the same frame. So purity is
     asserted as `np.array_equal` between a cold `build()` rendering one
     timestamp and a warm one driven frame by frame to the same timestamp,
     forwards and backwards, and across the loop point.
  2. **Ink can go missing or arrive early.** The finished plot must be exactly
     the union of every ink segment -- no more (a travel move that leaked onto
     the paper) and no less (a segment the cache skipped).
  3. **Travel can leave a mark.** The one thing the demo is *about* is that
     travel does not touch the paper. A ghost that failed to decay, or a travel
     move rasterised into the ink buffer, destroys the idea while looking
     perfectly attractive.

It needs no cache directory, no network and no wall: the demo is generative.

    $ python3 scripts/test-plotter.py
"""

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                         # noqa: E402
import plotter                                                 # noqa: E402

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
    return ds.options(plotter, **kw)


def driven(r, t, fps=20.0):
    """Drive render frame by frame from zero and land exactly on t.

    The last call has to be at `t` itself, not at the nearest frame boundary:
    this demo moves a pen a couple of pixels a frame, so comparing t against
    round(t*fps)/fps compares two different moments and fails a purity check
    that is not actually broken. That mistake cost half an hour here.
    """
    out = None
    n = max(0, int(t * fps))
    for i in range(n):
        out = r(i / fps, i)
    return r(t, n).copy()


def contains_text(frame, s, thresh=70, bg_max=0.30):
    """Is this string drawn anywhere on the panel? See test-caiso.py."""
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    m = plotter.text_mask(s) > 0.5
    gh, gw = m.shape
    if gh > h or gw > w:
        return False
    for y in range(h - gh + 1):
        row = lit[y:y + gh]
        for x in range(w - gw + 1):
            win = row[:, x:x + gw]
            if not np.array_equal(win & m, m):
                continue
            if (win & ~m).mean() <= bg_max:
                return True
    return False


# --------------------------------------------------------------------------
# 1. Purity, which is the whole point of the cache design.
# --------------------------------------------------------------------------

def test_purity():
    print("\nrender() is a pure function of t, cold or warm")
    args = opts()
    ref = plotter.build(args)
    cycle = ref.cycle

    # Points chosen to land in every phase of a couple of pieces, plus one
    # past the end of the loop so the wrap is covered too.
    times = [0.0, 0.35, 3.0, cycle * 0.17, cycle * 0.41, cycle * 0.63,
             cycle * 0.9, cycle - 0.05, cycle + 4.0]
    warm = plotter.build(args)
    bad = []
    for t in times:
        cold = plotter.build(args)(t, int(t * 20))
        got = driven(warm, t)
        if not np.array_equal(cold, got):
            bad.append("%.2f(%d px)" % (t, int((cold != got).any(axis=2).sum())))
    check("cold render(t) == the same t driven from zero", not bad,
          ", ".join(bad) or "%d timestamps" % len(times))

    # Backwards. A preview baker or a re-seek asks for an earlier t on a render
    # that has already run forwards, which is the case the snapshot restore
    # exists for; it is also the case a naive accumulator gets wrong.
    r = plotter.build(args)
    driven(r, cycle * 0.5)
    bad = []
    for t in (cycle * 0.5, cycle * 0.3, 2.0, 0.0, cycle * 0.45):
        cold = plotter.build(args)(t, int(t * 20))
        got = r(t, int(t * 20)).copy()
        if not np.array_equal(cold, got):
            bad.append("%.2f(%d px)" % (t, int((cold != got).any(axis=2).sum())))
    check("seeking backwards gives the same frame as a cold build", not bad,
          ", ".join(bad) or "5 seeks")

    # Two builds of the same seed must be the same drawing, or nothing above
    # proves anything.
    a = plotter.build(opts(seed=3))(30.0, 600)
    b = plotter.build(opts(seed=3))(30.0, 600)
    check("the same seed draws the same sheet", np.array_equal(a, b))
    c = plotter.build(opts(seed=4))(30.0, 600)
    check("a different seed draws a different one", not np.array_equal(a, c))


# --------------------------------------------------------------------------
# 2. The tour, against the geometry it came from.
# --------------------------------------------------------------------------

def test_tour():
    print("\nthe tour: times, kinds and what the pen is doing")
    r = plotter.build(opts())
    for P in r.pieces:
        nm = P["name"]
        t0, t1 = P["t0"], P["t1"]
        check("%s: move times are monotone and start at zero" % nm,
              float(t0[0]) == 0.0 and bool((np.diff(t1) > 0).all())
              and bool((t1 >= t0).all()),
              "%d moves, %.1f s" % (P["n"], P["dur_total"]))
        # Every ink move must be preceded by a pen-down somewhere and followed
        # by a pen-up; the simplest form of that is that the kinds alternate
        # correctly around every run of ink.
        k = P["kind"]
        runs = np.flatnonzero(np.diff((k == plotter.INK).astype(np.int8)) == 1)
        ok = all(k[i] == plotter.DROP for i in runs)
        check("%s: the pen is put down before every stroke" % nm, ok,
              "%d strokes" % (len(runs) + int(k[0] == plotter.INK)))
        ends = np.flatnonzero(np.diff((k == plotter.INK).astype(np.int8)) == -1)
        check("%s: and lifted after every one" % nm,
              all(k[i + 1] == plotter.LIFT for i in ends))

    hil = [P for P in r.pieces if P["name"] == "hilbert"]
    if hil:
        # One pen-down for the whole sheet. The only travel it is allowed is
        # the hop from the park position to the first point and back again,
        # which happens with the pen up and off the artwork.
        check("hilbert is one continuous stroke: the pen goes down once",
              int((hil[0]["kind"] == plotter.DROP).sum()) == 1
              and int((hil[0]["kind"] == plotter.TRAVEL).sum()) <= 2,
              "%d pen-downs, %d travel moves, %.0f px of it"
              % (int((hil[0]["kind"] == plotter.DROP).sum()),
                 int((hil[0]["kind"] == plotter.TRAVEL).sum()),
                 hil[0]["travel_len"]))

    # The travel optimiser has to actually help, or the ghosts are a cat's
    # cradle and the panel is a mess. Compared against the paths in the order
    # the generator produced them.
    args = opts()
    W, H = args.width, args.height
    lay = r.layout
    inset = 4
    aw = lay["SW"] - 2 * inset
    ah = lay["SH"] - 2 * inset - 6
    rng = np.random.default_rng(args.seed)
    for nm in ("flow", "truchet", "lissa"):
        paths = plotter.BUILDERS[nm](aw, ah, rng)
        start = (0.0, ah * 0.5)

        def travel(ps):
            cur = np.array(start, np.float32)
            tot = 0.0
            for p in ps:
                tot += float(np.hypot(p[0][0] - cur[0], p[0][1] - cur[1]))
                cur = p[-1]
            return tot

        raw = travel(paths)
        opt = travel(plotter.optimise(paths, start))
        # lissa is generated left to right and is already in a good order, so
        # the only thing to assert there is that the optimiser does not make it
        # worse -- which a greedy tour with a bad tie-break happily would.
        want = raw * (0.85 if nm != "lissa" else 1.0)
        check("%s: reordering does not lengthen the travel" % nm,
              opt <= want + 1e-6,
              "%.0f px -> %.0f px over %d paths" % (raw, opt, len(paths)))


# --------------------------------------------------------------------------
# 3. Ink: exactly the strokes, never the travel, and only ever added.
# --------------------------------------------------------------------------

def test_ink():
    print("\nthe ink on the sheet")
    args = opts()
    r = plotter.build(args)
    lay = r.layout
    sy0, sx0 = lay["sy0"], lay["sx0"]
    SH, SW = lay["SH"], lay["SW"]

    starts = [0.0]
    for P in r.pieces:
        starts.append(starts[-1] + P["span"])

    for pi, P in enumerate(r.pieces):
        t_end = starts[pi] + lay["feed"] + P["dur_total"] + 0.9
        frame = driven(r, t_end)
        sheet = frame[sy0:sy0 + SH, sx0:sx0 + SW]
        lit = sheet.max(axis=2)

        # Independent rasterisation of every ink segment, by the same stroke
        # routine but with no cache anywhere near it.
        ref = np.zeros((SH, SW), np.float32)
        k = P["kind"]
        for j in np.flatnonzero(k == plotter.INK):
            plotter.stroke(ref, P["x0"][j], P["y0"][j], P["x1"][j], P["y1"][j],
                           args.line * 0.5)
        want = ref > 0.5
        # The pen, its arm and the caption are drawn over the sheet, so the
        # comparison is one way: every pixel the reference says is inked must
        # be lit on the panel.
        got = lit > 40
        missing = int((want & ~got).sum())
        check("%s: every ink segment reached the paper" % P["name"],
              missing <= 4, "%d of %d inked pixels missing"
              % (missing, int(want.sum())))

        # And no ink outside the artwork's box. Asserted on the reference
        # rasterisation rather than on the panel, because the arm and the pen
        # are drawn *over* the sheet and legitimately cross the top margin --
        # a test that read the panel here would be testing the machine.
        margin = (int((ref[:2, :] > 0.02).sum())
                  + int((ref[:, :2] > 0.02).sum())
                  + int((ref[:, -2:] > 0.02).sum())
                  + int((ref[-7:, :] > 0.02).sum()))
        check("%s: no ink in the sheet's margins" % P["name"], margin == 0,
              "%d inked pixels outside the artwork box" % margin)

    # Ink only ever accumulates while a piece is being drawn.
    r2 = plotter.build(args)
    P = r2.pieces[0]
    prev = None
    drops = 0
    counts = []
    for i in range(int((lay["feed"] + P["dur_total"]) * 20)):
        f = r2(i / 20.0, i)
        n = int((f[sy0:sy0 + SH, sx0:sx0 + SW].max(axis=2) > 40).sum())
        counts.append(n)
        if prev is not None and n < prev - 30:
            drops += 1
        prev = n
    check("ink on the sheet only ever grows while it is being drawn",
          drops == 0, "%d -> %d lit pixels, %d drops"
          % (counts[0], counts[-1], drops))
    check("...and there is a lot more of it at the end than at the start",
          counts[-1] > counts[len(counts) // 2] > counts[2] + 50,
          "%d / %d / %d" % (counts[2], counts[len(counts) // 2], counts[-1]))


# --------------------------------------------------------------------------
# 4. Travel: visible while it happens, gone afterwards.
# --------------------------------------------------------------------------

def test_ghosts():
    print("\ntravel moves ghost and then evaporate")
    args = opts()
    r = plotter.build(args)
    lay = r.layout
    sy0, sx0, SH, SW = lay["sy0"], lay["sx0"], lay["SH"], lay["SW"]


    # Find a long travel move in the flow piece, which is the one with the most
    # of them, and look at the sheet during it and well after it.
    starts = [0.0]
    for P in r.pieces:
        starts.append(starts[-1] + P["span"])
    pi = [i for i, P in enumerate(r.pieces) if P["name"] == "flow"][0]
    P = r.pieces[pi]
    ti = P["trav_i"]
    L = np.hypot(P["x1"][ti] - P["x0"][ti], P["y1"][ti] - P["y0"][ti])
    m = int(ti[int(np.argmax(L))])
    base = starts[pi] + lay["feed"]

    during = driven(r, base + float(P["t0"][m] + P["t1"][m]) * 0.5)
    after = driven(r, base + float(P["t1"][m]) + 5.0)

    # Sample the travel line only where the finished artwork has no ink,
    # because the ends of a travel move are by definition the ends of two
    # strokes and the paper around them is covered. Without this the test says
    # "the ghost is still there" while looking at the drawing.
    ref = np.zeros((SH, SW), np.float32)
    for j in np.flatnonzero(P["kind"] == plotter.INK):
        plotter.stroke(ref, P["x0"][j], P["y0"][j], P["x1"][j], P["y1"][j],
                       args.line * 0.5 + 1.5)
    # Only the first part of the move: the frame is sampled halfway through
    # the travel, so the pen has only got halfway and the trail behind it is
    # all there is to look at. And it is dashed, so a bit over half of even
    # that is bare paper by design.
    u = np.linspace(0.05, 0.42, 60)
    xs = np.rint(P["x0"][m] + (P["x1"][m] - P["x0"][m]) * u).astype(int)
    ys = np.rint(P["y0"][m] + (P["y1"][m] - P["y0"][m]) * u).astype(int)
    free = ref[ys, xs] <= 0.02
    xs, ys = xs[free], ys[free]

    # 55, not 22: the dark sheet's own paper tone peaks around 30 with its
    # tooth on it, so a lower threshold reports the blank paper as a ghost and
    # the test passes at both ends of the decay.
    def lit_on(frame):
        return int((frame[ys + sy0, xs + sx0].max(axis=1) > 55).sum())

    n = len(xs)
    a = lit_on(during)
    b = lit_on(after)
    check("a travel move is visible as a trail while it happens",
          n >= 12 and a >= n * 0.35,
          "%d of %d bare-paper samples lit, over %.0f px" % (a, n, L.max()))
    check("...and five seconds later there is nothing on that line", b == 0,
          "%d of %d samples still lit" % (b, n))

    # --no-ghost has to actually turn it off.
    rg = plotter.build(opts(ghost=False))
    ng = driven(rg, base + float(P["t0"][m] + P["t1"][m]) * 0.5)
    check("--no-ghost leaves the travel invisible", lit_on(ng) == 0,
          "%d of %d samples lit" % (lit_on(ng), n))


# --------------------------------------------------------------------------
# 5. The panel: it says what it is drawing, and it never stalls.
# --------------------------------------------------------------------------

def test_panel():
    print("\nthe panel itself")
    args = opts()
    r = plotter.build(args)
    starts = [0.0]
    for P in r.pieces:
        starts.append(starts[-1] + P["span"])

    for pi, P in enumerate(r.pieces):
        t = starts[pi] + r.layout["feed"] + P["dur_total"] + 1.0
        f = driven(r, t)
        check("%s: the finished sheet is signed with its name" % P["name"],
              contains_text(f, P["name"]), P["name"])
        check("...and with the pen it was drawn in", contains_text(f, P["pen"]),
              P["pen"])

    # It must never hold the same frame: the pen is always somewhere new.
    r2 = plotter.build(args)
    prev = None
    run = best = 0
    for i in range(600):
        f = r2(i / 20.0, i)
        if prev is not None and np.array_equal(f, prev):
            run += 1
            best = max(best, run)
        else:
            run = 0
        prev = f.copy()
    check("the panel never holds a frame for a quarter of a second", best <= 4,
          "longest identical run %d frames" % best)

    # The pen colour has to change from sheet to sheet, or the cycle reads as
    # one very long drawing.
    pens = [P["pen"] for P in r.pieces]
    check("consecutive sheets use different pens",
          all(pens[i] != pens[i + 1] for i in range(len(pens) - 1)),
          " ".join(pens))


def test_sizes():
    print("\nother panel sizes and every option")
    for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                 (512, 128), (96, 32)):
        try:
            r = plotter.build(opts(width=w, height=h))
            f = None
            for i in range(0, 400, 7):
                f = r(i / 20.0, i)
            ok = f.shape == (h, w, 3) and f.dtype == np.uint8 and f.max() > 0
            detail = "%d pieces, %.0f s cycle" % (len(r.pieces), r.cycle)
        except Exception as e:                                  # noqa: BLE001
            ok, detail = False, repr(e)[:70]
        check("%dx%d renders" % (w, h), ok, detail)

    for kw in ({"piece": "hilbert"}, {"piece": "truchet"}, {"paper": "light"},
               {"paper": "blue"}, {"pen": "amber"}, {"rotate": 3},
               {"speed": 400.0}, {"line": 1.8}, {"ghost": False},
               {"hold": 0.0}):
        try:
            r = plotter.build(opts(**kw))
            f = None
            for i in range(0, 600, 3):
                f = r(i / 20.0, i)
            ok = f.max() > 0
            detail = "%.0f s cycle" % r.cycle
        except Exception as e:                                  # noqa: BLE001
            ok, detail = False, repr(e)[:70]
        check("%s renders" % kw, ok, detail)


def test_no_network():
    print("\nthe network promise")
    src = open(os.path.join(HERE, "plotter.py")).read()
    bad = [m for m in ("urllib", "http.client", "socket", "requests", "ssl",
                       "ftdata") if ("import " + m) in src]
    check("plotter.py imports nothing that talks to anybody", not bad,
          ",".join(bad))
    check("...and nothing that reads the clock either",
          "time.time" not in src and "monotonic" not in src)


def test_cost():
    print("\ncost, in strokes per frame -- the thing the Pi pays for")
    args = opts()
    real = plotter.stroke
    n = [0]

    def counted(*a, **k):
        n[0] += 1
        return real(*a, **k)

    plotter.stroke = counted
    try:
        r = plotter.build(args)
        per = []
        for i in range(int(r.cycle * 20)):
            n[0] = 0
            r(i / 20.0, i)
            per.append(n[0])
    finally:
        plotter.stroke = real
    per = np.array(per)
    check("mean strokes a frame is small", per.mean() < 12.0,
          "mean %.1f  p95 %.0f  max %d"
          % (per.mean(), np.percentile(per, 95), per.max()))
    check("and the worst frame is bounded", per.max() <= 26,
          "worst %d strokes" % per.max())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()

    test_no_network()
    test_purity()
    test_tour()
    test_ink()
    test_ghosts()
    test_panel()
    test_cost()
    test_sizes()

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
