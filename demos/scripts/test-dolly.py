#!/usr/bin/env python3
"""Checks for dolly.py that a screenshot cannot make.

Four kinds of thing are asserted here.

**That the type fits.** This panel is 320 pixels wide and its headlines are
drawn at scale 2, which is 12 pixels a character, which is 26 characters and
not one more. A line that overruns does not crash and does not look broken in
a preview thumbnail -- it looks like a sentence, with the last two words
quietly off the right-hand edge. Every baked line is measured against the
panel it will be drawn on, at every panel size, because that is the failure
this file exists for: it happened, to `12 A YEAR, 60 BY AGE FIVE.`, and it was
invisible until somebody looked at the pixels.

**That it is one ramp.** Everything on the panel is a single intensity field
taken through one 256-entry uint8 table, so every pixel the demo can emit is
*exactly* one of those 256 colours. Set membership, no tolerance, over a whole
loop. A stray blend anywhere breaks it and nothing else would notice.

**That it is a pure function of t.** ftsched builds a segment on a worker
thread and starts it at t=0, the preview baker steps it at a fixed rate, and
the wall's own loop drifts. Two independently built callbacks asked for the
same t must produce the same bytes, and one asked for a t it has already
passed must reproduce it exactly.

**That the show is the show.** The gag is a clock whose colon blinks and whose
digits do not. A butterfly is a month, so twelve of them leave and twelve books
land, and the pile only ever grows. The closing card is up, at full strength,
in the last frame of the cue -- which is the frame the wall is holding when the
minute ends and the rotation cuts back in, and therefore the only frame in the
file that is guaranteed to be photographed.

    $ python3 scripts/test-dolly.py
"""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import demoscene as ds
import dolly

PASSED = [0]
FAILED = []


def check(name, ok, detail=""):
    if ok:
        PASSED[0] += 1
    else:
        FAILED.append(name)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name,
                         ("  -- " + detail) if detail and not ok else ""))


def frames(render, n=None, loop=None, fps=24.0):
    loop = loop or render.loop
    n = n or int(loop * fps)
    for i in range(n):
        yield i * loop / n, render(i * loop / n, i)


# --------------------------------------------------------------------------

def test_type_fits_every_panel():
    """No baked line may be wider than the panel it is centred on."""
    bad = []
    for w, h in [(320, 64), (384, 64)]:
        for text, scale in dolly.every_line():
            got = dolly.fit_scale(text, scale, w)
            if dolly.width(text, got) > w:
                bad.append("%dx%d: %r at x%d is %dpx"
                           % (w, h, text, got, dolly.width(text, got)))
    check("every line fits the wall", not bad, "; ".join(bad[:3]))
    # And that the step-down is real rather than a function nothing calls.
    narrow = [dolly.fit_scale(t, s, 256) < s for t, s in dolly.every_line()]
    check("headlines step down on a narrower panel", any(narrow))


def test_one_ramp():
    r = ds.build(dolly)
    ramp = {tuple(c) for c in ds.gradient(dolly.RAMP, 256, np.uint8)}
    stray = set()
    for t, f in frames(r, n=90):
        stray |= {tuple(c) for c in f.reshape(-1, 3)} - ramp
        if stray:
            break
    check("every pixel is a ramp entry", not stray, str(list(stray)[:2]))


def test_pure_in_t():
    a, b = ds.build(dolly), ds.build(dolly)
    ts = [0.0, 4.5, 13.1, 13.4, 22.0, 33.5, 40.0, 48.2, 59.9]
    same = all(np.array_equal(a(t, 0), b(t, 0)) for t in ts)
    check("two builds agree at the same t", same)
    a(59.0, 0)                                    # walk the playhead past it
    check("one build reproduces a t it has passed",
          np.array_equal(a(22.0, 0), b(22.0, 0)))


def test_shape_and_range():
    ok = True
    for w, h in [(320, 64), (128, 32), (512, 128)]:
        f = ds.build(dolly, width=w, height=h)(7.0, 0)
        ok = ok and f.shape == (h, w, 3) and f.dtype == np.uint8
    check("shape and dtype at every panel size", ok)


def test_nothing_is_blank():
    """A dissolve through the sky is fine. A dark frame is not."""
    r = ds.build(dolly)
    dark = [round(t, 2) for t, f in frames(r) if f.max() < 60]
    check("no frame goes dark", not dark, "at %s" % dark[:4])


def test_the_colon_blinks_and_the_digits_do_not():
    """The gag is a clock radio, and that is what a clock radio does."""
    r = ds.build(dolly)
    cx, cy, cw, ch = r.colon_box
    dx, dy, dw, dh = r.digits_box
    fs = [r(t, 0).astype(np.int16) for t in np.arange(0.0, 1.0, 1 / 24.)]
    colon = [f[cy:cy + ch, cx:cx + cw].mean() for f in fs]
    # The digits either side of the colon, never the colon itself.
    digits = [(f[dy:dy + dh, dx:cx].mean()
               + f[dy:dy + dh, cx + cw:dx + dw].mean()) / 2.0 for f in fs]
    colon_moves = max(colon) - min(colon)
    digits_move = max(digits) - min(digits)
    check("the colon blinks", colon_moves > 20.0, "%.1f" % colon_moves)
    # The rhinestones drift across the digits too, so this is "does not
    # blink", not "does not change at all".
    check("the digits do not blink", digits_move < colon_moves / 4.0,
          "digits %.1f against colon %.1f" % (digits_move, colon_moves))


def test_twelve_butterflies_twelve_books():
    r = ds.build(dolly)
    counts = [dolly.landed_at(r, t) for t in np.arange(0.0, 21.0, 0.25)]
    check("the pile only grows", all(b >= a for a, b in zip(counts, counts[1:])))
    check("twelve butterflies land", max(counts) == 12, "got %d" % max(counts))
    check("the pile is complete before the figure that explains it",
          counts[int(14.0 / 0.25)] == 12, "%d at 14s" % counts[int(14.0 / 0.25)])


def test_the_closing_card_holds():
    """The last frame of the cue carries the ask, the domain and the dates."""
    r = ds.build(dolly)
    last = r(r.loop - 1e-3, 0)
    ok = True
    for text, scale, y in [("GIVE A KID A BOOK", 2, 12),
                           ("IMAGINATIONLIBRARY.COM", 1, 30),
                           (dolly.DATES, 1, last.shape[0] - 9)]:
        mask = dolly.bake(text, scale)
        x = dolly.centre(text, scale, last.shape[1])
        band = last[y:y + mask.shape[0], x:x + mask.shape[1]].mean(axis=2)
        on = band[mask].mean()
        off = band[~mask].mean() if (~mask).any() else 0.0
        ok = ok and on > off + 40.0
    check("the closing card is up in the last frame", ok)


def test_the_dates_are_only_on_the_last_card():
    """A tribute line under the gag would be a different demo."""
    r = ds.build(dolly)
    mask = dolly.bake(dolly.DATES, 1)
    x = dolly.centre(dolly.DATES, 1, 320)
    y = 64 - 9
    seen = []
    for t, f in frames(r, n=120):
        band = f[y:y + mask.shape[0], x:x + mask.shape[1]].mean(axis=2)
        if band[mask].mean() > band[~mask].mean() + 40.0:
            seen.append(t)
    first_ask = dolly.ACTS[-1][1] * r.loop
    check("the dates appear only on the closing card",
          seen and min(seen) >= first_ask, "first at %.1f" % (seen[0] if seen else -1))


def test_the_figure_carries_its_year():
    """A number on a wall without a date on it is a number that will rot."""
    check("the source line names the year the figure is from",
          dolly.FIGURES_YEAR in dolly.SOURCE_LINE
          and dolly.BOOKS_TOTAL in "".join(t for t, _ in dolly.every_line()))


def test_frame_budget():
    r = ds.build(dolly)
    ts = np.arange(0.0, r.loop, 1 / 24.)
    costs = []
    for i, t in enumerate(ts):
        a = time.perf_counter()
        r(float(t), i)
        costs.append((time.perf_counter() - a) * 1000.0)
    costs = np.array(costs)
    print("     %.2f ms p50, %.2f p95, %.2f worst, on this machine"
          % (np.percentile(costs, 50), np.percentile(costs, 95), costs.max()))
    # A ceiling that catches a catastrophic regression rather than one that
    # asserts anything about the Pi, which is thirty times slower and is
    # measured on the Pi.
    check("inside the frame budget on this machine",
          np.percentile(costs, 95) < 12.0)


def main():
    for fn in [test_type_fits_every_panel, test_one_ramp, test_pure_in_t,
               test_shape_and_range, test_nothing_is_blank,
               test_the_colon_blinks_and_the_digits_do_not,
               test_twelve_butterflies_twelve_books,
               test_the_closing_card_holds,
               test_the_dates_are_only_on_the_last_card,
               test_the_figure_carries_its_year, test_frame_budget]:
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d passed, %d failed" % (PASSED[0], len(FAILED)))
    for f in FAILED:
        print("  FAIL " + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
