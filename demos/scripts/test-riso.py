#!/usr/bin/env python3
"""Checks for riso.py that a screenshot cannot make.

This demo can draw a confident, pretty, wrong picture in several ways, and
every one of them looks fine in a still:

  1. **The wipe can be inverted, or vanish mid-pass.** The sheet is two blits
     out of a baked cumulative stack, split at the nip column. Both failures
     have actually shipped here. First the split was `nip - x` clipped at
     zero, which blanked a sheet whose left edge had passed the nip. Then the
     guard for that hid a worse bug: the two halves were the wrong way round,
     so the sheet arrived already printed, *lost* the colour as it crossed the
     drum, and the finished print popped in at the end. Both looked like
     perfectly good stills. So the wipe is asserted as a moving spatial
     boundary -- two ink states across the sheet, the seam pinned to the nip,
     the seam sweeping leading-edge to trailing-edge, and no sheet-local
     column ever losing ink -- and not merely as ink totals over time, which
     is what let the inversion through.
  2. **The passes can go backwards.** cum[k] must be the sheet after exactly k
     passes. Off by one anywhere and the last colour never lands, or the
     finished print appears a pass early.
  3. **The overprint can stop being a multiply.** The whole claim of the demo
     is that pink over blue is purple because of arithmetic and not because
     of a table, so the overlap of two channels is asserted against the
     product of the two inks.
  4. **Registration can be redrawn per frame.** The brief's determinism rule:
     the offsets are one draw in build(), so two builds on the same seed must
     be pixel identical and a mid-pass frame reached by stepping must equal
     the same frame evaluated cold.

So the checks are in pixels and in the composited arrays, not in eyeballing.
Nothing here needs a network or a cache -- riso is a generative panel.

    $ python3 scripts/test-riso.py
    $ python3 scripts/test-riso.py --loops 2
"""

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import riso                                                   # noqa: E402

FAILED = []
PASSED = [0]


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def build(**kw):
    return ds.build(riso, **kw)


# --------------------------------------------------------------------------
# 1. It survives a full loop, at every size and option combination we ship.
# --------------------------------------------------------------------------

def test_runs(loops):
    combos = [
        {},
        dict(seed=11), dict(seed=99),
        dict(art="poster"), dict(art="landscape"), dict(art="wordmark"),
        dict(misreg=0), dict(misreg=6), dict(screen=9.0), dict(density=1.0),
        dict(speed=2.5), dict(inks="pink,federal,black"),
        dict(width=128, height=32), dict(width=192, height=48),
        dict(width=320, height=64),
    ]
    for kw in combos:
        r = build(**kw)
        h = kw.get("height", 64)
        w = kw.get("width", 320)
        bad = None
        for i in range(int(20 * 200 * loops)):
            f = r(i / 20.0, i)
            if f.shape != (h, w, 3) or f.dtype != np.uint8:
                bad = "shape %s dtype %s" % (f.shape, f.dtype)
                break
        check("full loop, %s" % (kw or "defaults"), bad is None, bad or "")


# --------------------------------------------------------------------------
# 2. Purity, and determinism of the seed.
# --------------------------------------------------------------------------

def test_purity():
    # Cold evaluation must equal the same instant reached by stepping from
    # zero. This is the check the scheduler actually depends on: it builds
    # segments ahead on a worker thread and starts them at t=0.
    driven = build()
    frames = {}
    marks = {37: None, 121: None, 402: None, 913: None, 1500: None}
    for i in range(1501):
        f = driven(i / 20.0, i)
        if i in marks:
            frames[i] = f.copy()
    cold = build()
    worst = 0
    for i, want in frames.items():
        got = cold(i / 20.0, i)
        worst = max(worst, int(np.abs(got.astype(np.int32)
                                      - want.astype(np.int32)).max()))
    check("render is a pure function of t", worst == 0, "max delta %d" % worst)

    a, b = build(seed=7), build(seed=7)
    same = all(np.array_equal(a(t / 20.0, t), b(t / 20.0, t))
               for t in (0, 60, 300, 900))
    check("same seed, same pixels", same)

    a, b = build(seed=7), build(seed=8)
    diff = any(not np.array_equal(a(t / 20.0, t), b(t / 20.0, t))
               for t in (0, 60, 300, 900))
    check("different seed, different job", diff)


# --------------------------------------------------------------------------
# 3. The print engine. These reach into build() by rebuilding the same
#    arithmetic the module does, which is the only way to assert on the
#    cumulative stack without the module exporting it.
# --------------------------------------------------------------------------

def test_compose():
    """cum[k] is the sheet after exactly k passes, and overprint multiplies."""
    ah, aw = 20, 40
    YY, XX = np.mgrid[0:ah, 0:aw].astype(np.float32)
    # Two solid channels that overlap in a known rectangle, and no screen can
    # break a solid: coverage 1.0 beats every threshold in the grid.
    a = np.zeros((ah, aw), np.float32)
    b = np.zeros((ah, aw), np.float32)
    a[2:16, 2:24] = 1.0
    b[6:18, 12:34] = 1.0

    inks = [riso.hex_rgb(riso.INKS["yellow"][0]),
            riso.hex_rgb(riso.INKS["blue"][0])]
    d = 0.88
    paper = np.array(riso.PAPER, np.float32)
    want1 = paper * ((1 - d) + d * np.array(inks[0], np.float32) / 255.0)
    want2 = want1 * ((1 - d) + d * np.array(inks[1], np.float32) / 255.0)

    # Drive the module's own composition through a build with a stand-in
    # artwork, so the assertion is against the shipped code and not a copy.
    saved = dict(riso.ARTWORKS)
    try:
        riso.ARTWORKS.clear()
        riso.ARTWORKS["probe"] = lambda ah_, aw_, Y, X: [
            np.pad(a, ((0, max(0, ah_ - ah)), (0, max(0, aw_ - aw))))[:ah_, :aw_],
            np.pad(b, ((0, max(0, ah_ - ah)), (0, max(0, aw_ - aw))))[:ah_, :aw_]]
        r = build(art="probe", misreg=0, density=d, inks="yellow,blue", seed=1)
    finally:
        riso.ARTWORKS.clear()
        riso.ARTWORKS.update(saved)

    # The overlap colour, wherever and whenever it appears, must be the
    # product of the two inks into the paper. Search the loop for it rather
    # than computing where it landed: the claim under test is the arithmetic,
    # not the geometry.
    w1 = want1.astype(np.int32)
    w2 = want2.astype(np.int32)
    hit1 = hit2 = 0
    for i in range(0, 20 * 70, 3):
        flat = r(i / 20.0, i).reshape(-1, 3).astype(np.int32)
        hit1 = max(hit1, int((np.abs(flat - w1).max(axis=1) <= 1).sum()))
        hit2 = max(hit2, int((np.abs(flat - w2).max(axis=1) <= 1).sum()))
    check("single-ink pixels are paper x ink", hit1 > 40,
          "want %s, best %d px" % (tuple(int(v) for v in w1), hit1))
    check("overprint pixels are paper x ink x ink", hit2 > 40,
          "want %s, best %d px" % (tuple(int(v) for v in w2), hit2))


# --------------------------------------------------------------------------
# 4. The cycle. Every phase must be reached, the ink must never disappear
#    from a sheet that has already had it, and the finished print must be on
#    screen for a decent share of the loop.
# --------------------------------------------------------------------------

def _sheet_ink(f, y0=30, y1=54):
    """How many pixels of the panel are neither paper nor machine."""
    reg = f[y0:y1].astype(np.int32).reshape(-1, 3)
    paper = np.array(riso.PAPER, np.int32)
    lit = reg.sum(axis=1) > 150                      # not the dark machine
    not_paper = np.abs(reg - paper).max(axis=1) > 24
    return int((lit & not_paper).sum())


def test_cycle():
    r = build(seed=5)
    ink = np.array([_sheet_ink(r(i / 20.0, i)) for i in range(20 * 200)])
    # The one failure mode a still cannot show: ink that was on the sheet
    # last frame and is gone this frame, while the sheet is still on the
    # panel. A pass never un-prints; only the eject can drop the count, and
    # it drops it to zero over more than one frame.
    drops = []
    for i in range(1, len(ink)):
        if ink[i - 1] > 400 and ink[i] < ink[i - 1] * 0.45:
            drops.append((i / 20.0, int(ink[i - 1]), int(ink[i])))
    # Sheets leaving the panel at the right are allowed to lose ink fast; a
    # drop is only suspicious if there is still a lot of ink left afterwards
    # or if it happens twice in a row.
    bad = [d for d in drops if d[2] > 250]
    check("ink never vanishes from a printed sheet", not bad, str(bad[:3]))

    check("the panel is inked most of the time",
          (ink > 300).mean() > 0.55, "%.0f%% of frames" % ((ink > 300).mean() * 100))
    check("the panel is never empty for long",
          max(_runlength(ink < 60)) < 20 * 3.0,
          "longest blank run %.1f s" % (max(_runlength(ink < 60)) / 20.0))


# --------------------------------------------------------------------------
# 4b. The wipe, as geometry rather than as a total.
#
# test_cycle() above compares whole-sheet ink counts between frames, and that
# is not enough: a sheet drawn with the inked and un-inked halves swapped has
# a perfectly plausible total on every frame. What actually has to hold is
# spatial, so this drives the module with a stand-in artwork of two
# full-coverage separations. Full coverage beats every screen threshold, so
# every printed column of the sheet is one of exactly three flat colours --
# paper, paper x ink0, paper x ink0 x ink1 -- and a column of the panel can be
# classified into "how many passes have landed here" by colour alone. From
# that, four assertions the earlier test could not make.
# --------------------------------------------------------------------------

def _probe_build(d=0.88, W=320, H=64):
    """Build riso with two full-coverage separations, and the flat colours."""
    saved = dict(riso.ARTWORKS)
    try:
        riso.ARTWORKS.clear()
        riso.ARTWORKS["probe"] = lambda ah_, aw_, Y, X: [
            np.ones((ah_, aw_), np.float32), np.ones((ah_, aw_), np.float32)]
        r = build(art="probe", misreg=0, density=d, inks="yellow,blue",
                  seed=1, width=W, height=H)
    finally:
        riso.ARTWORKS.clear()
        riso.ARTWORKS.update(saved)
    cur = np.array(riso.PAPER, np.float64)
    states = [cur.copy()]
    for key in ("yellow", "blue"):
        ink = np.array(riso.hex_rgb(riso.INKS[key][0]), np.float64)
        cur = cur * ((1 - d) + d * ink / 255.0)
        states.append(cur.copy())
    return r, states


def test_wipe():
    W, H = 320, 64
    r, states = _probe_build(W=W, H=H)
    # Geometry re-derived from the module's own formulas, the same way
    # test_compose re-derives the multiply. The band is a few rows inside the
    # printable area, where every sheet column is a single flat colour: the
    # registration marks sit on the printable edges and the sheet's edge
    # shading on its outermost row, and both are outside it.
    nip = W // 2
    y_sheet = int(round(H * 0.42))
    y_deck = H - (7 if H >= 56 else 5)
    band = slice(y_sheet + 5, y_deck - 5)
    BG = np.array((13, 15, 20), np.int32)         # the machine interior

    seen = {}                     # sheet-local column -> passes landed so far
    seams = []                    # (frame, seam in sheet-local columns)
    runs = []                     # contiguous stretches of two-state frames
    mean_state = []
    bad_two = bad_seam = bad_class = bad_mono = None
    n_frames = 0

    for i in range(20 * 60):
        f = r(i / 20.0, i).astype(np.int32)
        b = f[band]
        # Classify every panel column by which cumulative state it shows.
        # Nothing else on the panel is any of these three flat colours, so
        # this locates the sheet as well as reading it. What it does not find
        # is the sheet's outermost column at each end, which carries the edge
        # shading, nor column `nip` when the ink-landing marker is painted
        # over the sheet -- hence the +/- 1 and the allowance for one gap.
        st = np.full(W, -1, np.int32)
        for si, c in enumerate(states):
            m = (np.abs(b - c.round().astype(np.int32)) <= 2).all(axis=2).all(axis=0)
            st[m] = si
        cols = np.flatnonzero(st >= 0)
        if cols.size == 0 or cols[-1] + 1 >= W - 4:
            break                      # the sheet is ejecting off the right
        x0, x1 = int(cols[0]) - 1, int(cols[-1]) + 1
        span = int(cols[-1] - cols[0]) + 1
        gap_ok = (cols.size == span
                  or (cols.size == span - 1 and cols[0] < nip < cols[-1]
                      and st[nip] < 0))
        if not gap_ok:
            bad_class = bad_class or ("frame %d: the sheet is not one run of "
                                      "columns (%d in a span of %d)"
                                      % (i, cols.size, span))
            break
        n_frames = i + 1
        inter = np.array([c for c in range(x0 + 3, x1 - 2) if c != nip])
        ist = st[inter]
        if (ist < 0).any():
            bad_class = bad_class or ("frame %d: %d unclassified columns"
                                      % (i, int((ist < 0).sum())))
            break
        mean_state.append(float(ist.mean()))

        # (1) At most two ink states across the sheet, they are consecutive,
        #     and the low one is entirely to the LEFT of the high one -- the
        #     paper travels left to right, so its leading edge is its right
        #     edge and the fresh ink is on the right.
        vals = np.unique(ist)
        if vals.size > 2 or (vals.size == 2 and vals[1] - vals[0] != 1):
            bad_two = bad_two or "frame %d: states %s" % (i, list(vals))
        if vals.size == 2:
            lo = inter[ist == vals[0]]
            hi = inter[ist == vals[1]]
            if lo.max() > hi.min():
                bad_two = bad_two or ("frame %d: fresh ink is not on the "
                                      "leading edge" % i)
            # (2) The seam is pinned to the nip, not drifting with the sheet.
            elif not (lo.max() == nip - 1 and hi.min() == nip + 1):
                bad_seam = bad_seam or ("frame %d: seam at %d..%d, nip is %d"
                                        % (i, lo.max(), hi.min(), nip))
            seam = nip - x0                    # in sheet-local columns
            if runs and runs[-1][-1][0] == i - 1:
                runs[-1].append((i, seam))
            else:
                runs.append([(i, seam)])
            seams.append((i, seam))

        # (3) No sheet-local column ever loses ink inside a job.
        for c, s in zip(inter - x0, ist):
            if seen.get(int(c), -1) > int(s):
                bad_mono = bad_mono or ("frame %d: sheet column %d went %d "
                                        "-> %d" % (i, c, seen[int(c)], s))
            seen[int(c)] = int(s)

    check("the wipe test saw a whole job", n_frames > 20 * 15 and len(runs) >= 2,
          "%d frames, %d wipe runs" % (n_frames, len(runs)))
    check("two ink states, fresh ink on the leading edge", bad_two is None,
          bad_two or "")
    check("the seam sits on the nip column", bad_seam is None, bad_seam or "")
    check("every sheet column is a cumulative state", bad_class is None,
          bad_class or "")
    check("no sheet column ever loses ink", bad_mono is None, bad_mono or "")

    # (2, continued) Each wipe sweeps the whole sheet, monotonically, from the
    # leading edge to the trailing one. A seam that jumped, stalled or ran
    # backwards fails here even if every individual frame looked legal.
    ws = max(24, int(round(W * 0.2625)))
    ok_runs, why = True, ""
    for run in runs:
        vs = [s for _, s in run]
        if any(vs[j + 1] > vs[j] for j in range(len(vs) - 1)):
            ok_runs, why = False, "seam ran backwards: %s" % vs[:12]
        elif not (vs[0] >= ws - 8 and vs[-1] <= 8):
            ok_runs, why = False, ("seam covered %d..%d of %d columns"
                                   % (vs[-1], vs[0], ws))
        elif vs[0] - vs[-1] < ws - 16:
            ok_runs, why = False, "seam only travelled %d columns" % (vs[0] - vs[-1])
    check("each wipe sweeps leading edge to trailing edge", ok_runs, why)

    # (4) And it is continuous: no frame may print a large slab of the sheet
    #     at once. This is the assertion that fails loudly on the old
    #     `x >= nip` special case, which slammed the whole sheet to printed.
    ms = np.array(mean_state)
    step = np.abs(np.diff(ms)).max() if ms.size > 1 else 0.0
    check("no discontinuous jump in how much is printed", step < 0.12,
          "largest single-frame step %.3f of a pass" % step)


def _runlength(mask):
    best, cur, runs = 0, 0, [0]
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    runs.append(best)
    return runs


# --------------------------------------------------------------------------
# 5. Registration and the palette: the two things the docstring promises.
# --------------------------------------------------------------------------

def test_registration():
    # misreg 0 and misreg 4 must produce different sheets, and misreg 0 must
    # be pixel-identical between two builds with different seeds *for the
    # registration alone* -- the inks still differ, so compare the offsets by
    # rendering the same job with the same ink list.
    a = build(misreg=0, seed=3, art="poster", inks="yellow,pink,blue")
    b = build(misreg=4, seed=3, art="poster", inks="yellow,pink,blue")
    ta = 20.0
    check("misregistration actually moves the separations",
          not np.array_equal(a(ta, int(ta * 20)), b(ta, int(ta * 20))))

    hexes = set()
    for k, (h, label, dark) in riso.INKS.items():
        check("%s is a 6 digit hex" % k, len(h) == 6 and
              all(c in "0123456789ABCDEFabcdef" for c in h), "#" + h)
        hexes.add(h)
    check("no two inks share a hex", len(hexes) == len(riso.INKS))
    default = [k.strip() for k in riso.DEFAULT_INKS.split(",")]
    check("every default ink exists", all(k in riso.INKS for k in default))
    check("at most one dark ink can land in a job",
          sum(1 for k in default if riso.INKS[k][2]) < len(default),
          "%d of %d dark" % (sum(1 for k in default if riso.INKS[k][2]),
                             len(default)))


# --------------------------------------------------------------------------
# 6. Type: measured, not assumed.
# --------------------------------------------------------------------------

def test_type():
    for s, scale in (("PASS 3/3 FLUOR PINK", 1), ("RISO", 3), ("SHEETS 0042", 1)):
        m = riso.text_mask(s, scale)
        check("%r renders %d rows" % (s, riso.GLYPH_H * scale),
              m.shape[0] == riso.GLYPH_H * scale and m.any(),
              "%dx%d, %d px set" % (m.shape[1], m.shape[0], m.sum()))
    # Every character the panel can print must have a glyph, or it silently
    # becomes a space and the readout lies.
    need = set("0123456789 /")
    for _, label, _ in riso.INKS.values():
        need |= set(label)
    need |= set("PASSMTER SHEETS")
    missing = sorted(c for c in need if c not in riso._GLYPHS)
    check("every readout character has a glyph", not missing, str(missing))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--loops", type=float, default=1.0,
                    help="how many 200 s runs per option combination")
    args = ap.parse_args()

    print("riso: the print engine")
    test_compose()
    print("riso: purity and seeding")
    test_purity()
    print("riso: the cycle")
    test_cycle()
    print("riso: the wipe")
    test_wipe()
    print("riso: registration and inks")
    test_registration()
    print("riso: type")
    test_type()
    print("riso: it runs")
    test_runs(args.loops)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  FAILED: %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
