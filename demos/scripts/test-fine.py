#!/usr/bin/env python3
"""Checks for fine.py that a screenshot cannot make.

Four things about this demo are load-bearing and none of them are visible in a
still frame:

  1. **It has to be a pure function of t.** The whole reason the fire is a
     scrolled noise field instead of the fire.py simulation is that the
     scheduler builds segments ahead on a worker thread and starts them at
     t=0. A refactor that reintroduces one accumulated variable produces a
     panel that still looks exactly like fire and desyncs on the wall.
  2. **The noise texture has to wrap.** It is scrolled by slicing a doubled
     copy, so if the tile does not join top to bottom there is a seam that
     crosses the panel once per tile and looks like a scanline glitch. The
     tile height is derived from the upsampling factors precisely so this
     cannot drift; the check is here because "derived" is a comment and this
     is a test.
  3. **The dog must not move.** That is the joke. Every pixel of the dog is
     identical in every frame except the two eye rows, and the eyes are only
     ever shut for a blink or a sip. A change that makes the dog jitter, or
     lets the flame composite over its face, is not a bug you would notice in
     a screenshot -- it is a bug you would notice as the panel not being
     funny.
  4. **The punchline has to be fully drawn.** The 3x5 font is measured rather
     than assumed, and this asserts the measurement: every pixel of the glyph
     mask, including the last row, is actually white in the frame at full
     alpha. This is the check that would have caught the bug that once clipped
     the bottom off every capital E on this wall.

Plus the arc itself: the room has to get monotonically worse, and the schedule
of sips and blinks has to be spaced, seeded and identical across builds.

    $ python3 scripts/test-fine.py
    $ python3 scripts/test-fine.py --seed 3 --cycle 20

Nothing here touches the network or the data cache; fine.py is generative.
"""

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import fine                                                   # noqa: E402

FAILED = []
PASSED = [0]

FPS = 20.0


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(fine, **kw)


def frames(args, t0, t1):
    """Every frame in [t0, t1), driven in order from a fresh build().

    Sampling render() at scattered timestamps is how you fail to notice that
    a demo is stateful, so nothing in this file ever does it.
    """
    r = fine.build(args)
    out = []
    i = 0
    while i / FPS < t1:
        f = r(i / FPS, i)
        if i / FPS >= t0:
            out.append(f.copy())
        i += 1
    return out


# --------------------------------------------------------------------------

def test_purity(args):
    """A cold render(t) must equal the same t reached frame by frame."""
    cyc = args.cycle
    for tv in (0.0, 0.35 * cyc, 0.71 * cyc, 0.80 * cyc, 0.98 * cyc,
               1.4 * cyc, 3.0 * cyc):
        cold = fine.build(opts(**vars_of(args)))(tv, 0).copy()
        r = fine.build(opts(**vars_of(args)))
        i = 0
        while (i + 1) / FPS <= tv + 1e-9:
            r(i / FPS, i)
            i += 1
        hot = r(tv, i)
        check("pure at t=%.2f" % tv, np.array_equal(cold, hot),
              "" if np.array_equal(cold, hot)
              else "%d px differ" % int((cold != hot).any(axis=2).sum()))


def test_determinism(args):
    """Same seed, same pixels; a different seed, different pixels."""
    kw = vars_of(args)
    a = fine.build(opts(**kw))(0.4 * args.cycle, 0)
    b = fine.build(opts(**kw))(0.4 * args.cycle, 0)
    check("same seed reproduces the frame", np.array_equal(a, b))
    kw2 = dict(kw)
    kw2["seed"] = kw["seed"] + 1
    c = fine.build(opts(**kw2))(0.4 * args.cycle, 0)
    check("a different seed is a different fire", not np.array_equal(a, c))


def test_noise_wraps(args):
    """The flame texture must join top to bottom, or the scroll has a seam.

    Measured, not eyeballed: the mean absolute step across the wrap has to
    look like the mean absolute step anywhere else in the tile. A tile that
    does not wrap is a hard edge and comes out an order of magnitude worse.
    """
    rng = np.random.default_rng(args.seed)
    n, tile_h = fine.turbulence(64, 320, rng)
    check("tile height is a multiple of the vertical upsampling",
          tile_h % 6 == 0, "tile_h=%d" % tile_h)
    inner = float(np.abs(np.diff(n, axis=0)).mean())
    seam = float(np.abs(n[0] - n[-1]).mean())
    check("noise tile wraps top to bottom", seam < inner * 2.0,
          "seam %.4f vs typical step %.4f" % (seam, inner))


def dog_geometry():
    """Where the dog is, derived the same way build() derives it."""
    W, H = 320, 64
    floor_y = int(round(H * 0.69))
    dog_w = len(fine.HAT[0])
    dog_x = int(round(W * 0.30)) - dog_w // 2
    dog_h = len(fine.HAT) + len(fine.HEAD) + len(fine.BODY)
    dog_y = floor_y - 3 + 2 - dog_h
    return dog_y, dog_x, dog_h, dog_w, floor_y


def test_dog_is_still(args):
    """The dog does not move. Its own pixels are constant all cycle.

    Only the dog's *own* pixels are compared -- the intersection of the four
    sprite variants' masks -- because the transparent margins of its bounding
    box are room and flame, which of course flicker. The rows the cup passes
    through are excluded, and the two eye rows are asserted to change, because
    a dog that never blinks is a cardboard cutout and the gag needs it alive.

    Run at --stages 4, because the baked lighting is a step function and each
    step legitimately repaints the dog. Four stages means at most 3 steps up
    and 3 back down over the cycle, plus the hat catching and un-catching --
    eight instants, out of nine hundred frames. Anything animated shows up
    immediately as hundreds.
    """
    kw = vars_of(args)
    kw["embers"] = 0                 # sparks legitimately fly over the dog
    kw["stages"] = 4
    fs = frames(opts(**kw), 0.0, args.cycle)
    dog_y, dog_x, dog_h, dog_w, _ = dog_geometry()
    box = np.array([f[dog_y:dog_y + dog_h, dog_x:dog_x + dog_w] for f in fs])

    common = np.ones((dog_h, dog_w), bool)
    for shut in (False, True):
        for singed in (False, True):
            common &= fine.rasterize(fine.dog_grid(shut, singed))[1]
    hat_rows = len(fine.HAT)
    eye_rows = list(range(hat_rows + fine.EYES_ROW,
                          hat_rows + fine.EYES_ROW + 2))
    # The cup travels from the table up to the muzzle, so everything from the
    # muzzle down is legitimately covered at some point in the cycle.
    common[hat_rows + 5:] = False

    still = common.copy()
    still[eye_rows] = False
    a = box[:, still].reshape(len(box), -1)
    # Two states over the whole cycle and no more: hat intact, and hat singed
    # once the room has gone. Anything else in here -- a jitter, a lean, a
    # reaction -- shows up as a third state or as extra transitions.
    changed = (a[1:] != a[:-1]).any(axis=1)
    limit = 2 * (max(2, kw["stages"]) - 1) + 2
    check("the dog never moves", int(changed.sum()) <= limit,
          "%d changes in %d frames (at most %d: light steps and the singe)"
          % (int(changed.sum()), len(a), limit))
    # Two adjacent repaints are legal -- a light step and the singe can land
    # one frame apart -- but three running is animation, which is the thing
    # this demo must not have.
    run3 = bool((changed[2:] & changed[1:-1] & changed[:-2]).any())
    check("and never three frames running", not run3,
          "no run of consecutive repaints")

    # The eyes are the one thing that does move, so they must change strictly
    # more often than the rest of the dog -- comparing against frame 0 would
    # only prove the lighting stepped.
    e = box[:, common & ~still].reshape(len(box), -1)
    blinked = int((e[1:] != e[:-1]).any(axis=1).sum())
    check("the dog does blink", blinked > int(changed.sum()) + 6,
          "%d eye changes vs %d elsewhere" % (blinked, int(changed.sum())))


def test_arc_gets_worse(args):
    """More of the room is on fire at every beat than at the one before."""
    cyc = args.cycle
    beats = (0.06, 0.30, 0.55, 0.85)
    kw = vars_of(args)
    kw["embers"] = 0
    r = fine.build(opts(**kw))
    hot = []
    i = 0
    want = [b * cyc for b in beats]
    while i / FPS < cyc:
        f = r(i / FPS, i)
        if want and i / FPS >= want[0]:
            # "on fire" = saturated warm pixels, which is the flame ramp's
            # bright end and nothing else in the room reaches.
            hot.append(int(((f[:, :, 0] > 200) & (f[:, :, 2] < 120)).sum()))
            want.pop(0)
        i += 1
    check("the room gets steadily worse", all(a < b for a, b in zip(hot, hot[1:])),
          "burning px at the four beats: %s" % hot)
    check("it starts small", hot[0] < 0.06 * 320 * 64, "%d px" % hot[0])
    check("it ends engulfed", hot[-1] > 0.18 * 320 * 64, "%d px" % hot[-1])


def test_punchline(args):
    """The line is drawn once, whole, and not over the dog."""
    cyc = args.cycle
    kw = vars_of(args)
    kw["embers"] = 0
    scale = max(1, args.text_scale)
    m = fine.text_mask(args.text, scale)
    th, tw = m.shape
    check("the font is measured, not assumed",
          th == 5 * scale and tw == (len(args.text) * 4 - 1) * scale,
          "mask is %dx%d for %d chars at scale %d"
          % (tw, th, len(args.text), scale))
    check("the punchline fits the panel", tw <= 320 and th <= 64,
          "%dx%d" % (tw, th))

    r = fine.build(opts(**kw))
    # Ask the demo where the bubble goes rather than re-deriving it here.
    # The banner's placement formula used to be copied into this test, which
    # is how a demo and its test drift apart while both stay green.
    lay = fine.bubble_layout(args.text, scale, 320, 64)
    ty, tx = lay["ty"], lay["tx"]
    cth, ctw = lay["th"], lay["tw"]
    on = []
    i = 0
    best = None
    while i / FPS < cyc:
        f = r(i / FPS, i)
        sub = f[ty:ty + cth, tx:tx + ctw]
        # The bubble is a light ground with dark ink on it, so presence is
        # the fill, and the type is the ink *inside* the fill.
        pale = (sub.min(axis=2) > 200)
        if pale.sum() > 0.5 * m.sum():
            on.append(i / FPS)
            if pale.sum() > (best[0] if best else 0):
                best = (pale.sum(), sub.copy())
        i += 1
    check("the punchline appears", bool(on),
          "" if not on else "%.1fs .. %.1fs" % (on[0], on[-1]))
    check("it appears once, in one run",
          bool(on) and (on[-1] - on[0]) < len(on) / FPS + 0.2,
          "%d frames spanning %.1fs" % (len(on), (on[-1] - on[0]) if on else 0))
    check("it lands late in the cycle", bool(on) and on[0] > 0.6 * cyc,
          "first at %.0f%% of the cycle" % (100.0 * on[0] / cyc if on else 0))
    # Every pixel of the glyph mask is lit, including the bottom row. This is
    # the clipped-E check.
    if best:
        sub = best[1]
        dark = (sub.max(axis=2) < 120)          # ink, in card coordinates
        gm = lay["glyphs"]
        missing = int((gm & ~dark).sum())
        check("every glyph pixel is drawn", missing == 0,
              "%d of %d mask pixels missing" % (missing, int(gm.sum())))
        # The clipped-E check, now against the glyph block's own last row.
        rows = np.where(gm.any(axis=1))[0]
        last = int(rows[-1]) if len(rows) else 0
        check("the last glyph row is drawn",
              gm[last].sum() == 0 or bool((gm[last] & dark[last]).any()),
              "bottom row: %d of %d" % (int((gm[last] & dark[last]).sum()),
                                        int(gm[last].sum())))
        # It is a bubble, not a banner: the type sits on its own light
        # ground, and that ground fully surrounds it.
        fill = lay["fill"]
        check("the type sits on the bubble's own ground",
              bool((gm & ~fill).sum() == 0),
              "%d glyph pixels outside the fill" % int((gm & ~fill).sum()))
        pale = (sub.min(axis=2) > 200)
        halo_ok = int((fill & ~gm & ~pale).sum())
        check("the bubble reads as a light object",
              halo_ok < 0.10 * int((fill & ~gm).sum()) + 1,
              "%d of %d fill pixels are not pale"
              % (halo_ok, int((fill & ~gm).sum())))
        # And it has a tail -- the thing that makes it speech rather than a
        # caption. The tail is fill below the bubble's own solid block.
        solid = np.where(fill.all(axis=1))[0]
        if len(solid):
            below = fill[int(solid[-1]) + 1:]
            check("the bubble has a tail", bool(below.any()),
                  "%d tail pixels" % int(below.sum()))
    # ...and it does not land on the dog's hat.
    dog_top = int(round(64 * 0.69)) - 3 + 2 \
        - (len(fine.HAT) + len(fine.HEAD) + len(fine.BODY))
    # A rectangle test, not a vertical one. At large --text-scale the bubble
    # cannot fit above his head and moves alongside him instead, which is
    # still clear of him; a vertical-only check calls that a failure.
    dog_w = len(fine.HAT[0])
    dog_left = int(round(320 * 0.30)) - dog_w // 2
    dog_right = dog_left + dog_w
    clear = (ty + cth <= dog_top or tx >= dog_right or tx + ctw <= dog_left)
    check("the punchline clears the dog", clear,
          "bubble rows %d..%d cols %d..%d, dog rows %d.. cols %d..%d"
          % (ty, ty + cth, tx, tx + ctw, dog_top, dog_left, dog_right))

    off = fine.build(opts(no_text=True, **kw))
    quiet = True
    i = 0
    while i / FPS < cyc:
        f = off(i / FPS, i)
        sub = f[ty:ty + cth, tx:tx + ctw]
        if (sub.min(axis=2) > 200).sum() > 0.5 * m.sum():
            quiet = False
        i += 1
    check("--no-text really means no text", quiet)


def test_schedule(args):
    """Sips are spaced. Two sips a second apart would be a reaction."""
    cyc = args.cycle
    kw = vars_of(args)
    kw["embers"] = 0
    r = fine.build(opts(**kw))
    dog_y, dog_x, dog_h, dog_w, floor_y = dog_geometry()
    # The cup at rest sits to the right of the dog on the table; if it is not
    # there, it is up at the muzzle.
    #
    # "Is the cup here" is asserted on the cup's *structure*, not on its
    # brightness: a bright rim over a dark row of coffee, at a ratio of more
    # than two. Brightness would have been the obvious test and it is wrong,
    # because the room's ambient light falls by two thirds across the cycle
    # and takes the cup's white with it -- the first version of this check
    # reported one sip that started at 14 seconds and never ended.
    cy, cx = floor_y - 3 - len(fine.CUP), dog_x + dog_w + 2
    lifted = []
    i = 0
    while i / FPS < cyc:
        f = r(i / FPS, i)
        v = f[cy:cy + len(fine.CUP),
              cx:cx + len(fine.CUP[0])].min(axis=2).astype(float)
        lifted.append(not (v[0].min() > 2.0 * (v[1, 1:4].max() + 1.0)))
        i += 1
    runs = []
    start = None
    for i, up in enumerate(lifted):
        if up and start is None:
            start = i
        elif not up and start is not None:
            if (i - start) / FPS >= 0.8:      # a stray flame is not a sip
                runs.append((start / FPS, i / FPS))
            start = None
    if start is not None and (len(lifted) - start) / FPS >= 0.8:
        runs.append((start / FPS, len(lifted) / FPS))
    # One sip is a rise, a hold and a fall, and the cup clips the corner of
    # its own resting box on the way past; merge anything that close before
    # calling it two sips.
    merged = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < 1.2:
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    runs = merged
    want = int(min(3, max(1, round(cyc / 16.0))))
    check("the dog sips as often as the cycle allows", len(runs) == want,
          "%d sips (wanted %d): %s"
          % (len(runs), want, ["%.1f" % a for a, _ in runs]))
    gaps = [b[0] - a[1] for a, b in zip(runs, runs[1:])]
    check("sips never overlap or crowd", all(g > 3.0 for g in gaps),
          "gaps: %s" % ["%.1f" % g for g in gaps])
    lens = [b - a for a, b in runs]
    check("every sip is the same length", not lens
          or max(lens) - min(lens) < 0.3,
          "%s s" % ["%.1f" % x for x in lens])


def test_contract(args):
    """The frame the wall is actually handed."""
    kw = vars_of(args)
    r = fine.build(opts(**kw))
    f = r(0.5 * args.cycle, 10)
    check("shape and dtype", f.shape == (64, 320, 3) and f.dtype == np.uint8,
          "%s %s" % (f.shape, f.dtype))
    # A whole cycle plus change, with no exception and nothing out of range.
    i = 0
    lo, hi = 255, 0
    while i / FPS < args.cycle * 1.3:
        f = r(i / FPS, i)
        lo = min(lo, int(f.min()))
        hi = max(hi, int(f.max()))
        i += 1
    check("a full loop renders", True, "%d frames, values %d..%d" % (i, lo, hi))
    check("it uses the top of the range", hi > 240, "max %d" % hi)


def vars_of(args):
    return {"cycle": args.cycle, "seed": args.seed, "text": args.text,
            "text_scale": args.text_scale, "stages": args.stages,
            "scroll": args.scroll, "embers": args.embers}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    d = ds.options(fine)
    ap.add_argument("--cycle", type=float, default=d.cycle)
    ap.add_argument("--seed", type=int, default=d.seed)
    ap.add_argument("--text", default=d.text)
    ap.add_argument("--text-scale", type=int, default=d.text_scale)
    ap.add_argument("--stages", type=int, default=d.stages)
    ap.add_argument("--scroll", type=float, default=d.scroll)
    ap.add_argument("--embers", type=int, default=d.embers)
    args = ap.parse_args()

    for name, fn in (("the contract", test_contract),
                     ("purity", test_purity),
                     ("determinism", test_determinism),
                     ("the flame texture", test_noise_wraps),
                     ("the dog", test_dog_is_still),
                     ("the arc", test_arc_gets_worse),
                     ("the schedule", test_schedule),
                     ("the punchline", test_punchline)):
        print("\n%s" % name)
        fn(args)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for f in FAILED:
        print("  - %s" % f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
