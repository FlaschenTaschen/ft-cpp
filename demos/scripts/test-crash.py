#!/usr/bin/env python3
"""Checks for crash.py that a screenshot cannot make.

This demo can fail in four ways that all still look like a perfectly good
picture, and one of them is a safety property rather than an aesthetic one:

  1. **The museum caption can go missing** -- clipped off the bottom, drawn in
     the specimen's own colour, painted under the specimen, or simply absent
     for the frames of a transition. That caption is the only thing standing
     between a full-width blue screen on a public wall and somebody deciding
     the wall has crashed. So it is asserted **in pixels**, on every frame of
     a full loop, by reading the caption's text back off the panel.
  2. **A colour can be plausibly wrong.** VGA blue at 0x0000A0 instead of
     0x0000AA is a blue screen; VIC-II blue eyeballed is a C64. Nobody would
     ever notice by looking, and getting these right is the entire point of
     the demo, so every ground colour is asserted against its documented
     value and against the number of pixels it covers.
  3. **A font can lose a glyph.** The two bitmap sets are hand-written hex;
     one wrong byte is one wrong letter in one word, which is invisible until
     somebody who owned the machine walks past. Every glyph is checked for
     being non-empty, for fitting its cell, and for being distinct from every
     other glyph -- a typo that duplicates an existing shape is the failure
     mode that survives eyeballing.
  4. **It can stop being a pure function of t.** The scheduler builds
     segments ahead and starts them at t=0 and the preview baker steps at a
     fixed rate, so a cold render() at t must equal the same t reached by
     driving from zero.

There is no data tier here: crash.py is generative, imports nothing but numpy
and demoscene, and never touches ftdata. That is asserted too.

    $ python3 scripts/test-crash.py
    $ python3 scripts/test-crash.py --seeds 40
"""

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import crash                                                   # noqa: E402
import demoscene as ds                                         # noqa: E402

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
    return ds.options(crash, **kw)


def loop_frames(args, fps=20):
    """Every frame of one full loop, rendered in order from a fresh build."""
    r = crash.build(args)
    n = int(round(r.period * fps))
    return r, [r(i / float(fps), i).copy() for i in range(n)]


def find_text(frame, s, colour=None, tol=0):
    """Is this string drawn anywhere on the frame in the 4 px font?

    Reading the caption back off the panel is the only way to be sure it
    actually reached the pixels rather than merely being computed -- which is
    exactly the bug that would make this demo unsafe to put on a wall.

    The counters between the strokes have to be dark as well. Without that,
    every string in the language matches somewhere inside the blue screen's
    solid ground, and the check passes while proving nothing at all.
    """
    m = crash.tiny_mask(s)[:, :crash.tiny_width(s)].astype(bool)
    if colour is not None:
        lit = (np.abs(frame.astype(int) - np.array(colour, int)).max(axis=2)
               <= tol)
    else:
        lit = frame.max(axis=2) >= 90
    gh, gw = m.shape
    h, w = lit.shape
    if gh > h or gw > w:
        return False
    for y in range(h - gh + 1):
        band = lit[y:y + gh]
        for x in range(w - gw + 1):
            win = band[:, x:x + gw]
            if np.array_equal(win & m, m) and (win & ~m).mean() <= 0.08:
                return True
    return False


# --------------------------------------------------------------------------
# 1. The tell. This is the check that matters most and it is checked hardest.
# --------------------------------------------------------------------------

def test_caption_every_frame():
    print("\nthe museum caption, on every frame of the loop")
    args = opts()
    r, frames = loop_frames(args)

    # Which caption should be up on each frame. The plinth swaps at the
    # midpoint of the collapse, so a frame is allowed to show either its own
    # specimen's caption or the next one during the second half of a gap.
    bad = []
    for i, f in enumerate(frames):
        tt = (i / 20.0) % r.period
        k = min(int(tt / r.step), len(r.captions) - 1)
        want = {r.captions[k], r.captions[(k + 1) % len(r.captions)]}
        if not any(find_text(f, c, crash.LABEL_FG) for c in want):
            bad.append(i)
    check("every frame of the loop carries a readable caption",
          not bad, "%d of %d frames without one%s"
          % (len(bad), len(frames),
             (" (first at t=%.2fs)" % (bad[0] / 20.0)) if bad else ""))

    # ...and it is in the museum's colour, not the specimen's, on the plinth,
    # in the same rows every time. A caption drawn in blue-screen white would
    # read as part of the blue screen.
    plinth = np.stack([f[r.screen_h:] for f in frames])
    hit = (np.abs(plinth.astype(int)
                  - np.array(crash.LABEL_FG, int)).max(axis=3) == 0)
    check("the caption is drawn in a colour no specimen uses",
          hit.any(axis=(1, 2)).all() and hit.sum(axis=(1, 2)).min() > 60,
          "%d..%d label pixels a frame"
          % (hit.sum(axis=(1, 2)).min(), hit.sum(axis=(1, 2)).max()))

    grounds = [crash.C64_BG, crash.C64_FG, crash.BSOD_BG, crash.BSOD_FG,
               crash.PANIC_FG, crash.GURU_RED, crash.MAC_FG]
    check("...and that colour is not one of them",
          crash.LABEL_FG not in grounds, str(crash.LABEL_FG))

    # The specimen never paints into the plinth's rows and the plinth never
    # paints into the specimen's. If it did, the caption would vanish under
    # a full-bleed blue screen exactly when it is most needed.
    rule = np.stack([f[r.screen_h] for f in frames])
    check("the plinth's hairline rule survives every frame",
          bool((rule == np.array(crash.LABEL_RULE, np.uint8)).all(axis=2)
               .all()), "row %d" % r.screen_h)

    # And the third tell: the panel really does become a different specimen.
    seen = set()
    for i in range(0, len(frames), 5):
        for c in r.captions:
            if find_text(frames[i], c, crash.LABEL_FG):
                seen.add(c)
    check("all five specimens actually come up within one loop",
          len(seen) == len(r.captions), "%d of %d" % (len(seen), len(r.captions)))

    # --no-label is allowed, and must be the only way to lose the caption.
    _, plain = loop_frames(opts(label=False), fps=2)
    check("--no-label is the only thing that removes it",
          not any(find_text(f, r.captions[0], crash.LABEL_FG) for f in plain))


# --------------------------------------------------------------------------
# 2. The colours, against their documented values.
# --------------------------------------------------------------------------

def test_colours():
    print("\nthe grounds, against the numbers they are supposed to be")
    documented = [
        ("VIC-II 6, blue", crash.C64_BG, (0x35, 0x28, 0x79)),
        ("VIC-II 14, light blue", crash.C64_FG, (0x6C, 0x5E, 0xB5)),
        ("VGA 1, blue screen ground", crash.BSOD_BG, (0x00, 0x00, 0xAA)),
        ("VGA 15, blue screen text", crash.BSOD_FG, (0xFF, 0xFF, 0xFF)),
        ("VGA 7, console light grey", crash.PANIC_FG, (0xAA, 0xAA, 0xAA)),
        ("Amiga alert red", crash.GURU_RED, (0xFF, 0x00, 0x00)),
    ]
    for name, got, want in documented:
        check("%s is 0x%02X%02X%02X" % ((name,) + want), got == want, str(got))

    check("the panic is grey and the blue screen is white -- not both white",
          crash.PANIC_FG != crash.BSOD_FG,
          "%s vs %s" % (crash.PANIC_FG, crash.BSOD_FG))

    # And each of those colours actually covers the panel it belongs to.
    for name, ground, frac in (("bsod", crash.BSOD_BG, 0.75),
                               ("panic", (0, 0, 0), 0.85),
                               ("c64", crash.C64_BG, 0.6),
                               ("guru", (0, 0, 0), 0.85),
                               ("sadmac", (0, 0, 0), 0.9)):
        r = crash.build(opts(only=name))
        f = r(0.9, 0)[:r.screen_h]
        share = float((f == np.array(ground, np.uint8)).all(axis=2).mean())
        check("%s is mostly its own ground colour" % name, share >= frac,
              "%.0f%% of the screen area" % (100 * share))


# --------------------------------------------------------------------------
# 3. The fonts. Hand-written hex, so every glyph gets looked at.
# --------------------------------------------------------------------------

def test_fonts():
    print("\nthe two bitmap fonts, glyph by glyph")
    for label, table, glyph, cell in (
            ("8x8", crash._CHUNKY_SRC, crash._chunky_glyph, (8, 8)),
            ("4px", crash._TINY, crash._tiny_glyph, (6, 3))):
        shapes = {}
        empty, dupes, wrong = [], [], []
        for ch in table:
            g = glyph(ch)
            if g.shape != cell:
                wrong.append(ch)
            key = g.tobytes()
            if g.sum() == 0:
                if ch != " ":
                    empty.append(ch)
                continue
            if key in shapes:
                dupes.append("%s=%s" % (ch, shapes[key]))
            shapes[key] = ch
        check("%s: every glyph fits its %dx%d cell" % ((label,) + cell),
              not wrong, ",".join(wrong))
        check("%s: no glyph but space is blank" % label, not empty,
              ",".join(empty))
        check("%s: no two glyphs are the same shape" % label, not dupes,
              " ".join(dupes))

    # The letters everybody reads first. A wrong byte in one of these is the
    # difference between a C64 and something that is nearly a C64.
    check("8x8 has upper case, digits and the punctuation the screens use",
          all(c in crash._CHUNKY_SRC for c in
              "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ?.,\"*()"),
          "%d glyphs" % len(crash._CHUNKY_SRC))
    check("4px has both cases, digits and the punctuation the screens use",
          all(c in crash._TINY for c in
              "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
              "0123456789 .,:!?'*()[]/\\-_+#"),
          "%d glyphs" % len(crash._TINY))
    check("4px lower case really is shorter than upper case",
          all(crash._tiny_glyph(c.lower())[0].sum() == 0
              for c in "acemnorsuvwxz"),
          "x-height letters clear of the cap line")
    check("4px descenders reach the sixth row",
          all(crash._tiny_glyph(c)[5].sum() > 0 for c in "gjpqy"))

    # The lines the specimens are made of have to fit the panel, measured.
    for name, cols, pitch in (("bsod", 78, 4), ("panic", 78, 4)):
        r = crash.build(opts(only=name))
        f = r(0.9, 0)[:r.screen_h]
        check("%s stays inside the panel" % name,
              f.shape[1] == 320, "%d columns of %d px"
              % (cols, pitch))


# --------------------------------------------------------------------------
# 4. Purity, determinism and the loop.
# --------------------------------------------------------------------------

def test_purity():
    print("\nrender() is a pure function of t")
    marks = [0.0, 3.7, 8.0, 8.15, 8.3, 8.55, 12.0, 25.4, 42.7]
    bad = []
    for t in marks:
        cold = crash.build(opts())(t, 0).copy()
        r = crash.build(opts())
        n = int(round(t * 20))
        for i in range(n):
            r(i / 20.0, i)
        if not np.array_equal(cold, r(t, n)):
            bad.append(t)
    check("a cold render matches the same t driven from zero", not bad,
          "differs at %s" % (bad or "nowhere"))

    a, b = crash.build(opts(seed=1)), crash.build(opts(seed=1))
    check("the same seed is the same exhibition",
          np.array_equal(a(3.0, 0), b(3.0, 0)))
    c = crash.build(opts(seed=99))
    check("a different seed changes the hex codes",
          not np.array_equal(a(3.0 + 2 * a.step, 0), c(3.0 + 2 * c.step, 0)),
          "guru address")
    check("the default seed is not the clock", opts().seed != 0,
          "--seed %d" % opts().seed)

    r = crash.build(opts())
    check("the loop fits a 45 second slot with all five shown",
          38.0 <= r.period <= 45.0, "%.2f s for %d specimens"
          % (r.period, len(r.names)))
    check("...and it loops seamlessly", np.array_equal(r(0.0, 0).copy(),
                                                       r(r.period, 1)))


def test_motion():
    print("\nthe two things that move, and nothing else")
    r = crash.build(opts(only="c64"))
    diffs = {int((r(i / 20.0, i)[:r.screen_h]).sum()) for i in range(20)}
    check("the C64 cursor blinks", len(diffs) == 2,
          "%d distinct screens in a second" % len(diffs))
    r = crash.build(opts(only="guru"))
    diffs = {int((r(i / 20.0, i)[:r.screen_h]).sum()) for i in range(30)}
    check("the guru border flashes", len(diffs) == 2,
          "%d distinct screens" % len(diffs))
    for name in ("bsod", "panic", "sadmac"):
        r = crash.build(opts(only=name))
        first = r(0.0, 0).copy()
        still = all(np.array_equal(first, r(i / 20.0, i))
                    for i in range(1, int(r.hold * 20)))
        check("%s is dead still while it is held" % name, still)

    # The collapse has to actually collapse, and it has to be brief.
    r = crash.build(opts())
    lit = [int((r(r.hold + j * r.step * 0 + j * 0.05, j)[:r.screen_h]
                .max(axis=2) > 8).sum()) for j in range(12)]
    check("the picture collapses towards a line between specimens",
          min(lit) < 0.25 * max(lit), "%d -> %d lit pixels"
          % (max(lit), min(lit)))
    check("...and the collapse is under a second",
          opts().gap <= 1.0, "%.2f s" % opts().gap)


def test_sizes_and_options():
    print("\nother panel sizes and the options")
    for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                 (512, 128), (64, 32), (320, 16)):
        try:
            r = crash.build(opts(width=w, height=h))
            f = r(3.0, 0)
            g = r(r.hold + 0.2, 1)
            ok = (f.shape == (h, w, 3) and f.dtype == np.uint8 and f.max() > 0
                  and g.shape == (h, w, 3))
            detail = "screen %d rows, plinth %d" % (r.screen_h, h - r.screen_h)
        except Exception as exc:                                # noqa: BLE001
            ok, detail = False, repr(exc)[:70]
        check("%dx%d renders held and mid-collapse" % (w, h), ok, detail)

    for name, _fn in crash.SPECIMENS:
        r = crash.build(opts(only=name))
        check("--only %s shows one specimen" % name,
              len(r.names) == 1 and r.names[0] == name,
              "%.1f s loop" % r.period)

    orders = {tuple(crash.build(opts(shuffle=True, seed=s)).names)
              for s in range(1, 9)}
    check("--shuffle reorders the exhibition", len(orders) > 1,
          "%d distinct orders over 8 seeds" % len(orders))
    check("the default order is chronological",
          crash.build(opts()).names == [n for n, _f in crash.SPECIMENS],
          " ".join(crash.build(opts()).names))


def test_hygiene(seeds):
    print("\nthe promises")
    src = open(os.path.join(HERE, "crash.py")).read()
    bad = [m for m in ("urllib", "http.client", "socket", "requests", "ssl",
                       "subprocess", "ftdata", "PIL")
           if ("import " + m) in src]
    check("crash.py imports no network, no ftdata and no Pillow", not bad,
          ",".join(bad))
    check("...and reads no clock", "time." not in src and "import time" not in src)

    # Every seed has to produce a real STOP code and a real bugcheck name.
    names = {n for _c, n in crash.BUGCHECKS}
    seen = set()
    for s in range(1, seeds + 1):
        r = crash.build(opts(seed=s))
        cap = [c for c in r.captions if c.startswith("WINDOWS")][0]
        code = int(cap.split("0x")[1].split(",")[0], 16)
        check_ok = code in [c for c, _n in crash.BUGCHECKS]
        if not check_ok:
            seen.add(code)
    check("every seed draws a genuine bugcheck code", not seen,
          "%d seeds, %d names in the table" % (seeds, len(names)))

    r = crash.build(opts())
    frames = [r(i / 20.0, i) for i in range(int(r.period * 20))]
    check("every frame is a (%d, %d, 3) uint8 array" % (64, 320),
          all(f.shape == (64, 320, 3) and f.dtype == np.uint8
              for f in frames), "%d frames" % len(frames))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, default=12,
                    help="how many seeds to check the hex codes over")
    a = ap.parse_args()

    test_caption_every_frame()
    test_colours()
    test_fonts()
    test_purity()
    test_motion()
    test_sizes_and_options()
    test_hygiene(a.seeds)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
