#!/usr/bin/env python3
"""Checks for hackfilm.py that a screenshot cannot make.

This panel fails in ways that still look like a filmstrip, and there are five
of them:

  1. **It has to be a pure function of t.** The scheduler builds a segment on a
     worker thread and starts it at t=0, the preview baker steps at a fixed
     rate, and the wall's loop drifts. The weave is the trap here: it looks
     exactly like something you would implement by calling the RNG once a
     frame, and a version that did would drift against the preview and still
     look completely correct on a desktop.
  2. **The pan has to land.** Every hold must show a frame *exactly* centred,
     with the caption's baseline in the same place every time. An ease that
     overshoots and does not settle, or an off-by-one in the wrap padding,
     gives you a strip that creeps a pixel a cycle and is unreadable after a
     minute -- and that is invisible in any single frame.
  3. **The wrap has to be seamless.** The strip is padded with a copy of its
     own first panel width. If that padding is wrong the last frame of the
     cycle has a black band or a torn cell in it, once every forty seconds.
  4. **Every caption has to be fully drawn.** The 3x5 font is measured, not
     assumed; this asserts the measurement by finding every pixel of the name
     and the grove in the baked strip, including the last row of each. This is
     the check that would have caught the bug that once clipped the bottom off
     every capital E on this wall.
  5. **No frame may claim a grove that does not exist.** The point of the panel
     is that it is true about the space. A typo'd grove is a lie told in 3x5
     type to the people who run it.

Plus the arithmetic of the strip -- cell width, band heights, the panel being
exactly two cells wide -- because every layout constant in the module is
derived from those and a change to one silently reflows the rest.

    $ python3 scripts/test-hackfilm.py
    $ python3 scripts/test-hackfilm.py --seed 9 --hold 2 --advance 0.4

Nothing here touches the network or the data cache; hackfilm is generative.
"""

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import hackfilm                                               # noqa: E402

FAILED = []
PASSED = [0]


def check(cond, what, detail=""):
    if cond:
        PASSED[0] += 1
        print("  ok   %s" % what)
    else:
        FAILED.append(what)
        print("  FAIL %s %s" % (what, detail))


def section(title):
    print("\n== %s" % title)


# --------------------------------------------------------------------------

def test_layout():
    section("layout arithmetic")
    m = hackfilm
    check(m.BAND_H + m.ART_H + m.CAP_H + m.BAND_H == ds.HEIGHT,
          "the four bands fill exactly 64 rows")
    check(m.IMG_X0 * 2 + m.IMG_W == m.CELL_W,
          "the image is centred in its cell with equal frame lines")
    check(ds.WIDTH == 2 * m.CELL_W,
          "the panel is exactly two cells wide",
          "%d vs %d" % (ds.WIDTH, 2 * m.CELL_W))
    check(all(0 <= x and x + m.PERF_W <= m.CELL_W for x in m.PERF_X),
          "every perforation is inside its cell")
    # The caption has to fit two measured lines of type with a gap.
    need = 1 + m.FONT_H + 1 + m.FONT_H
    check(need <= m.CAP_H, "two measured text lines fit the caption band",
          "needs %d rows, has %d" % (need, m.CAP_H))


def test_content(strip):
    section("what the strip claims")
    m = hackfilm
    check(len(m.FRAMES) >= 4, "at least four frames",
          "%d frames" % len(m.FRAMES))
    for name, grove, draw, page in m.FRAMES:
        check(grove in m.GROVES, "%s: grove %r is a real grove" % (name, grove))
        check(bool(page), "%s: names the wiki page it came from" % name)
        w = hackfilm.text_mask(name).shape[1]
        check(w <= m.IMG_W - 4, "%s: name fits the frame" % name,
              "%d px of %d" % (w, m.IMG_W))
        w = hackfilm.text_mask(grove).shape[1]
        check(w <= m.IMG_W - 4, "%s: grove fits the frame" % name,
              "%d px of %d" % (w, m.IMG_W))
    names = [f[0] for f in m.FRAMES]
    check(len(set(names)) == len(names), "no frame appears twice")
    # No two adjacent frames share a grove -- three of seven are Electronics,
    # and two Electronics cells side by side would read as one long frame.
    groves = [f[1] for f in m.FRAMES]
    adj = [i for i in range(len(groves)) if groves[i] == groves[i - 1]]
    check(not adj, "no two adjacent frames share a grove", str(adj))


def test_type_is_whole(strip):
    """Every pixel of every caption, including the bottom row of every glyph."""
    section("captions are fully drawn")
    m = hackfilm
    for i, (name, grove, _draw, _page) in enumerate(m.FRAMES):
        cell = strip[:, i * m.CELL_W:(i + 1) * m.CELL_W]
        cap = cell[m.CAP_Y0:m.CAP_Y0 + m.CAP_H, m.IMG_X0:m.IMG_X0 + m.IMG_W]
        for label, row, rgb in ((name, 1, m.C_NAME),
                                (grove, 1 + m.FONT_H + 1, m.GROVES[grove])):
            mask = hackfilm.text_mask(label)
            gh, gw = mask.shape
            x = (m.IMG_W - gw) // 2
            sub = cap[row:row + gh, x:x + gw]
            check(sub.shape[:2] == (gh, gw),
                  "%s / %r: the whole glyph box is on the cell" % (name, label))
            drawn = np.all(sub == np.asarray(rgb, np.uint8), axis=-1)
            missing = int(np.sum(mask & ~drawn))
            check(missing == 0,
                  "%s / %r: every lit pixel is at full colour" % (name, label),
                  "%d missing (last row lit: %d)"
                  % (missing, int(mask[-1].sum())))


def test_purity(args):
    section("render is a pure function of t")
    cold = ds.build(hackfilm, seed=args.seed, hold=args.hold,
                    advance=args.advance)
    warm = ds.build(hackfilm, seed=args.seed, hold=args.hold,
                    advance=args.advance)
    period = args.hold + args.advance
    cycle = len(hackfilm.FRAMES) * period
    probes = [0.0, period * 0.5, args.hold + args.advance * 0.5,
              period * 2.7, cycle - 0.05, cycle + 1.3, cycle * 2 + 0.4]
    # Drive `warm` frame by frame from zero, sampling it at the probe times
    # when it gets there, so any state carried between calls shows up.
    n = int(round(max(probes) * 20)) + 1
    seen = {}
    for i in range(n + 1):
        t = i / 20.0
        f = warm(t, i)
        for p in probes:
            if abs(t - p) < 1e-9:
                seen[p] = f.copy()
    bad = []
    for p in probes:
        c = cold(p, 0).copy()
        w = seen.get(p)
        if w is None:                      # not on the 20 fps grid; drive to it
            w = warm(p, n).copy()
        if not np.array_equal(c, w):
            bad.append(p)
    check(not bad, "cold render matches the same t reached frame by frame",
          str(bad))

    a = ds.build(hackfilm, seed=args.seed)(7.0, 0).copy()
    b = ds.build(hackfilm, seed=args.seed)(7.0, 0).copy()
    check(np.array_equal(a, b), "two builds with one seed are identical")
    c = ds.build(hackfilm, seed=args.seed + 101)(7.0, 0).copy()
    check(not np.array_equal(a, c), "a different seed draws differently")


def test_holds_are_centred(strip, args):
    """The whole point of the pan: each hold parks a cell exactly in the gate."""
    section("the pan lands square")
    m = hackfilm
    r = ds.build(hackfilm, seed=args.seed, hold=args.hold,
                 advance=args.advance, weave=0.0)
    period = args.hold + args.advance
    off = (ds.WIDTH - m.CELL_W) // 2
    bad = []
    for i in range(len(m.FRAMES)):
        # Sample late in the hold, past the settle, and on a second cycle too.
        for lap in (0, 1, 2):
            t = lap * len(m.FRAMES) * period + i * period + args.hold * 0.9
            f = r(t, 0)
            want = strip[:, i * m.CELL_W:(i + 1) * m.CELL_W]
            if not np.array_equal(f[:, off:off + m.CELL_W], want):
                bad.append((i, lap))
    check(not bad, "every hold shows its cell centred, every lap", str(bad))

    # And the caption sits on the same rows in every frame, which is what makes
    # it readable while walking past.
    rows = set()
    for i in range(len(m.FRAMES)):
        t = i * period + args.hold * 0.9
        f = r(t, 0)
        cap = f[m.CAP_Y0:m.CAP_Y0 + m.CAP_H, off + m.IMG_X0:off + m.IMG_X0 + m.IMG_W]
        lit = np.where(cap.max(axis=(1, 2)) > 40)[0]
        rows.add((int(lit.min()), int(lit.max())))
    check(len(rows) == 1, "the caption occupies the same rows in every frame",
          str(sorted(rows)))


def test_no_seam(args):
    """A black band or a torn cell at the wrap, once every cycle."""
    section("the wrap has no seam")
    r = ds.build(hackfilm, seed=args.seed, hold=args.hold,
                 advance=args.advance, weave=0.0)
    period = args.hold + args.advance
    cycle = len(hackfilm.FRAMES) * period
    dark = []
    for i in range(int(cycle * 20) + 41):
        t = i / 20.0
        f = r(t, i)
        # Any wholly black column inside the image rows means a cell edge fell
        # off the end of the padding.
        band = f[hackfilm.ART_Y0:hackfilm.ART_Y0 + hackfilm.ART_H]
        cols = band.max(axis=(0, 2))
        # Frame lines are legitimately near-black; a *run* of them is not.
        run = 0
        for v in cols:
            run = run + 1 if v < 6 else 0
            if run > 2 * hackfilm.IMG_X0 + 2:
                dark.append(round(t, 2))
                break
    check(not dark, "no run of dead columns anywhere in the cycle",
          str(dark[:6]))

    # The panel at the very end of the cycle must equal the panel at the start.
    a = r(0.0, 0).copy()
    b = r(cycle, 0).copy()
    check(np.array_equal(a, b), "t=0 and t=cycle draw the same panel")


def test_cost(args):
    section("cost per frame")
    r = ds.build(hackfilm, seed=args.seed)
    import time
    ts = []
    for i in range(600):
        t = i / 20.0
        a = time.perf_counter()
        r(t, i)
        ts.append((time.perf_counter() - a) * 1e3)
    ts = np.array(ts)
    print("  desktop mean %.4f  p95 %.4f  max %.4f ms"
          % (ts.mean(), np.percentile(ts, 95), ts.max()))
    # The whole design is one slice per frame. If this ever rises it is because
    # somebody moved drawing into render(), which is the thing to catch.
    check(ts.mean() < 0.5, "a frame is still one copy, not a redraw",
          "%.4f ms" % ts.mean())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--hold", type=float, default=5.0)
    ap.add_argument("--advance", type=float, default=0.85)
    args = ap.parse_args()

    strip = hackfilm.bake_strip(np.random.RandomState(args.seed))
    print("strip %s, %d frames" % (strip.shape, len(hackfilm.FRAMES)))

    test_layout()
    test_content(strip)
    test_type_is_whole(strip)
    test_purity(args)
    test_holds_are_centred(strip, args)
    test_no_seam(args)
    test_cost(args)

    print("\n%d passed, %d failed" % (PASSED[0], len(FAILED)))
    for f in FAILED:
        print("  - %s" % f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
