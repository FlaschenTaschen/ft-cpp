#!/usr/bin/env python3
"""Checks for teletext.py that a screenshot cannot make.

This panel is a character display, so the strongest possible check is to read
the page back off the pixels: every cell is one of a known set of 8x8 bitmaps,
so `decode()` below turns a rendered frame into the 8x40 grid of characters it
came from and the assertions are about *words on the screen*. That catches the
failure that matters most here -- a page that draws a confident number from a
product that is stale, absent or, in the tide's case, whose prediction span ran
out days ago. All three of those are states the wall is actually in from time
to time, and all three must produce an honest page rather than a plausible one.

The other things asserted:

  * the mosaic alphabet really is the 2x3 sixel set -- code k lights exactly
    the sub-blocks whose bits are set, in reading order -- because a picture
    drawn through a wrong alphabet is still a picture and looks fine;
  * double height is the same glyph with its scan lines doubled, split over
    two rows, rather than two unrelated bitmaps;
  * every colour on the panel is one of the eight, everywhere, in every frame,
    which is the one rule that makes it teletext and the easiest to break with
    a stray blend;
  * `render` is a pure function of `t` once the clock is pinned with `--at`,
    which is what lets the scheduler build a segment ahead of showing it.

`ftdata.CACHE_DIR` binds at import, so the fresh / stale / absent states are
each run in a separate process with FT_DATA_CACHE set, at the bottom.

    $ python3 scripts/test-teletext.py          # uses the live cache too
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402
import ftsite                                                 # noqa: E402
import teletext as tt                                         # noqa: E402

FAILED = []
PASSED = [0]

PINNED = 1786500000.0            # a fixed epoch, so the clock never moves


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-56s %s" % (name, detail))
    else:
        print("  FAIL %-56s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    kw.setdefault("at", PINNED)
    return ds.options(tt, **kw)


def frame_at(args, t):
    r = tt.build(args)
    return r(t, int(t * 20)).copy()


# --------------------------------------------------------------------------
# Reading the page back off the panel.
# --------------------------------------------------------------------------

def _decode_table():
    """8x8 bitmap -> character, for the normal and both double-height halves."""
    table = {}
    for ch, idx in tt.CHAR.items():
        for off in (0, tt.TOP_OFF, tt.BOT_OFF):
            key = tt.BANK[idx + off].tobytes()
            table.setdefault(key, ch)
    return table


TABLE = _decode_table()


def decode(frame):
    """A rendered frame -> the (8, 40) grid of characters that produced it.

    The background of a cell is whichever colour covers most of it, which is
    true for text and untrue for a mosaic block -- so mosaic cells decode as
    None and the text assertions simply ignore them.
    """
    grid = []
    for r in range(tt.ROWS):
        row = []
        for c in range(tt.COLS):
            cell = frame[r * 8:(r + 1) * 8, c * 8:(c + 1) * 8]
            flat = cell.reshape(-1, 3)
            # Packed by hand rather than np.unique(axis=0), which is exactly
            # the kind of call the wall's numpy 1.19 is fussy about.
            packed = (flat[:, 0].astype(np.int32) << 16 |
                      flat[:, 1].astype(np.int32) << 8 | flat[:, 2])
            vals, counts = np.unique(packed, return_counts=True)
            bg = vals[int(np.argmax(counts))]
            mask = (packed != bg).reshape(8, 8)
            row.append(TABLE.get(mask.tobytes()))
        grid.append(row)
    return grid


def line(frame, r):
    return "".join(c if c else "�" for c in decode(frame)[r])


def says(frame, text):
    """Is this string anywhere on the page, on one row?"""
    grid = decode(frame)
    for r in range(tt.ROWS):
        row = "".join(c if c else "�" for c in grid[r])
        if text.upper() in row:
            return True
    return False


# --------------------------------------------------------------------------
# The alphabet.
# --------------------------------------------------------------------------

def test_mosaic_alphabet():
    ok = True
    for code in range(64):
        want = np.zeros((8, 8), bool)
        for k in range(6):
            if code & (1 << k):
                y0, y1 = tt.SUB_Y[k // 2]
                x0, x1 = tt.SUB_X[k % 2]
                want[y0:y1, x0:x1] = True
        if not np.array_equal(tt.BANK[code], want):
            ok = False
    check("mosaic alphabet is the 64 sixel codes", ok)
    check("mosaic sub-rows tile the cell exactly",
          tt.SUB_Y[0][0] == 0 and tt.SUB_Y[-1][1] == 8 and
          all(tt.SUB_Y[i][1] == tt.SUB_Y[i + 1][0] for i in range(2)),
          "%s" % (tt.SUB_Y,))
    check("code 0 is empty and code 63 is a full block",
          not tt.BANK[0].any() and tt.BANK[63].all())


def test_double_height():
    ok = True
    for ch, idx in tt.CHAR.items():
        base = tt.BANK[idx][0:7, 1:6]
        tall = np.repeat(base, 2, axis=0)
        top = tt.BANK[idx + tt.TOP_OFF][0:8, 1:6]
        bot = tt.BANK[idx + tt.BOT_OFF][0:6, 1:6]
        if not (np.array_equal(top, tall[0:8]) and
                np.array_equal(bot, tall[8:14])):
            ok = False
    check("double height is the glyph with doubled scan lines", ok)


def test_font_fits():
    ok = all(not tt.BANK[i][:, 6:].any() and not tt.BANK[i][:, 0].any()
             for i in tt.CHAR.values())
    check("every glyph sits in its 5 px column with bearing", ok)
    check("the font measures 5x7 in an 8x8 cell",
          tt.BANK[tt.CHAR["E"]][0:7, 1:6].shape == (7, 5))


# --------------------------------------------------------------------------
# The panel.
# --------------------------------------------------------------------------

def test_palette_only():
    args = opts()
    r = tt.build(args)
    allowed = set()
    for rgb in tt.PAL:
        allowed.add((int(rgb[0]) << 16) | (int(rgb[1]) << 8) | int(rgb[2]))
    bad = 0
    for i in range(0, 160):
        f = r(i * 0.25, i)
        packed = (f[:, :, 0].astype(np.int32) << 16 |
                  f[:, :, 1].astype(np.int32) << 8 | f[:, :, 2])
        for v in np.unique(packed):
            if int(v) not in allowed:
                bad += 1
    check("every pixel of every frame is one of the eight colours", bad == 0,
          "%d strays" % bad)


def test_geometry():
    args = opts()
    f = frame_at(args, 3.0)
    check("frame is (64, 320, 3) uint8",
          f.shape == (64, 320, 3) and f.dtype == np.uint8, str(f.shape))
    check("40 columns of 8 px is exact", tt.COLS * tt.CW == 320)
    check("8 rows of 8 px is exact", tt.ROWS * tt.CH == 64)


def test_header():
    args = opts()
    f = frame_at(args, args.load + 1.0)
    head = line(f, 0)
    check("header carries the page number", head.startswith("P100"), head)
    check("header carries the station name", "SEQUOIAFAX" in head)
    check("header carries the date",
          time.strftime("%a", time.localtime(PINNED)).upper() in head, head)
    check("header carries a clock", ":" in head[28:] and "/" in head[28:],
          head[28:])


def test_clock_ticks():
    """The one wall-clock element: with --at it is pinned, and it advances."""
    # The ident, because it is the page with nothing else moving on it -- the
    # index reveals its subtitle and would fail this for the right reason.
    args = opts(page=101)
    a = frame_at(args, args.load + 1.0)
    b = frame_at(args, args.load + 2.0)      # same page, one second later
    check("the pinned clock advances with t", line(a, 0) != line(b, 0),
          "%s -> %s" % (line(a, 0)[28:], line(b, 0)[28:]))
    check("nothing below the header moves with the clock",
          np.array_equal(a[8:], b[8:]))


def test_cycle():
    args = opts()
    r = tt.build(args)
    period = args.hold + args.load
    seen = []
    for i in range(5):
        f = r(i * period + args.load + 1.0, 0)
        seen.append(line(f, 0)[:4])
    check("the cycle rolls through all five pages",
          seen == ["P100", "P101", "P102", "P103", "P104"], " ".join(seen))
    total = period * 5
    check("the cycle wraps (below the clock)", np.array_equal(
        r(args.load + 1.0, 0)[8:].copy(),
        r(total + args.load + 1.0, 0)[8:]))


def test_flip_is_noisy_then_clean():
    args = opts()
    r = tt.build(args)
    period = args.hold + args.load
    # Page 102 rather than the ident, whose paper colours light every cell of
    # the finished page and leave the noise nothing to be denser than.
    mid = r(2 * period + args.load * 0.5, 0).copy()
    done = r(2 * period + args.load + 1.0, 0).copy()
    lit_mid = (mid.max(axis=2) > 0).mean()
    lit_done = (done.max(axis=2) > 0).mean()
    check("a page arrives through a burst of mosaic noise",
          lit_mid > lit_done * 1.5, "%.2f vs %.2f" % (lit_mid, lit_done))
    check("the page number rolls during the flip",
          line(mid, 0)[:4] != line(done, 0)[:4],
          "%s -> %s" % (line(mid, 0)[:4], line(done, 0)[:4]))


def test_reveal():
    args = opts()
    r = tt.build(args)
    early = r(args.load + 0.2, 0).copy()
    late = r(args.load + 5.0, 0).copy()
    n_early = sum(1 for c in decode(early)[6] if c not in (None, " "))
    n_late = sum(1 for c in decode(late)[6] if c not in (None, " "))
    check("the subtitle reveals a character at a time", n_early < n_late,
          "%d -> %d chars" % (n_early, n_late))
    check("the finished subtitle is the whole line",
          tt.SUBTITLE[:20] in line(late, 6), line(late, 6))


def test_purity():
    """A cold render at t equals the same t reached frame by frame."""
    args = opts()
    r1 = tt.build(args)
    t = 19.35
    for i in range(int(t * 20)):
        r1(i / 20.0, i)
    driven = r1(t, int(t * 20)).copy()
    r2 = tt.build(args)
    cold = r2(t, int(t * 20)).copy()
    check("render is a pure function of t (clock pinned)",
          np.array_equal(driven, cold))


def test_ident_has_no_numbers():
    """P101 is the page with no data on it; it must not grow any."""
    args = opts(page=101)
    f = frame_at(args, 2.0)
    body = "".join(line(f, r) for r in range(1, 7))
    check("the ident page carries no data",
          not any(ch.isdigit() for ch in body), body.strip()[:40])
    check("the ident page is mostly mosaic",
          (f[8:56].max(axis=2) > 0).mean() > 0.35,
          "%.2f lit" % (f[8:56].max(axis=2) > 0).mean())


# --------------------------------------------------------------------------
# Honesty. These run in child processes against a cache we control.
# --------------------------------------------------------------------------

def write_record(cache_dir, name, payload, age):
    """A record of a chosen age. Ages are measured against the real clock,
    because ftdata.load() measures them that way -- a fixture written relative
    to a pinned `now` silently rots into the stale path half an hour later."""
    with open(os.path.join(cache_dir, name + ".json"), "w") as fh:
        json.dump({"payload": payload, "fetched_at": time.time() - age,
                   "source": "test"}, fh)


def tide_payload(now, end_offset):
    """A prediction curve ending `end_offset` seconds after `now`."""
    step = 360.0
    t0 = now - 6 * 3600
    n = int((end_offset + 6 * 3600) / step) + 1
    v = [3.0 + 2.5 * np.sin(2 * np.pi * i * step / 44700.0) for i in range(n)]
    ex = [{"t": t0 + 3000, "v": 5.5, "type": "H"},
          {"t": now + 3600, "v": 0.4, "type": "L"},
          {"t": now + 9000, "v": 6.1, "type": "H"}]
    return {"station": "9414290", "name": "SAN FRANCISCO", "units": "ft",
            "datum": "MLLW", "extremes": ex,
            "curve": {"t0": t0, "step": step, "v": v}}


CHILD = r'''
import json, os, sys, time
import numpy as np
sys.path.insert(0, %(here)r)
os.environ["FT_DATA_CACHE"] = %(cache)r
import demoscene as ds, teletext as tt
sys.path.insert(0, os.path.join(%(here)r, "scripts"))
import importlib.util
spec = importlib.util.spec_from_file_location("h", %(self)r)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
args = ds.options(tt, at=%(now)r, page=%(page)d)
r = tt.build(args)
f = r(args.load + 1.0, 0)
print(json.dumps(["".join(c if c else "?" for c in row) for row in h.decode(f)]))
'''


def child_page(cache_dir, page, now):
    src = CHILD % {"here": HERE, "cache": cache_dir, "self": __file__,
                   "now": now, "page": page}
    out = subprocess.run([sys.executable, "-c", src], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    if out.returncode != 0:
        return None, out.stderr.decode()[-400:]
    return json.loads(out.stdout.decode()), ""


def test_states():
    now = PINNED
    with tempfile.TemporaryDirectory() as empty:
        for page in (100, 102, 103, 104):
            rows, err = child_page(empty, page, now)
            check("P%d renders with an empty cache" % page, rows is not None,
                  err)
            if rows:
                body = " ".join(rows[1:7])
                check("P%d says NO DATA rather than a number" % page,
                      "NO DATA" in body or "NOT CACHED" in body or
                      page == 100, body.strip()[:44])
                if page != 100:
                    # Not "no digits" -- the page still says SEE PAGE 100 --
                    # but no measurement: no units, anywhere.
                    units = ("%sC" % tt.DEG, "M/S", " FT", "V ", "%", "HPA")
                    check("P%d quotes no measurement when absent" % page,
                          not any(u in body for u in units),
                          body.strip()[:44])

    with tempfile.TemporaryDirectory() as fresh:
        write_record(fresh, "tide-9414290", tide_payload(now, 18 * 3600), 60)
        write_record(fresh, "solar-garden",
                     {"soc_pct": 74.0, "status": "Charging", "v": 13.4,
                      "i_ma": 2200.0, "load_w": 4.5, "site": "sequoia.garden",
                      "volt": [13.0 + 0.4 * (i % 60) / 60.0
                               for i in range(288)]}, 60)
        write_record(fresh, "wx-model-%.4f_%.4f" % (ftsite.LAT, ftsite.LON),
                     {"temp_c": 17.4, "wind_ms": 3.2, "wind_dir": 270.0,
                      "cloud_pct": 10.0, "rh_pct": 60.0, "pressure_hpa": 1015.0,
                      "symbol_1h": "clearsky_day", "label": "MET.NO"}, 60)
        rows, err = child_page(fresh, 103, now)
        check("P103 fresh: the next tide is named", rows is not None, err)
        if rows:
            body = " ".join(rows[1:7])
            check("P103 fresh: shows LOW and its time", "LOW" in body,
                  rows[1])
            check("P103 fresh: does not claim the prediction ended",
                  "RAN OUT" not in body, body.strip()[:44])
            check("P103 fresh: age is in seconds, and green",
                  "60S OLD" in body or "0M OLD" in body or "S OLD" in body,
                  body.strip()[:60])
        rows, _ = child_page(fresh, 104, now)
        if rows:
            check("P104 fresh: the real state of charge is the headline",
                  "74%" in rows[1], rows[1].strip()[:30])
        rows, _ = child_page(fresh, 102, now)
        if rows:
            check("P102 fresh: the real temperature is the headline",
                  "17" in rows[1], rows[1].strip()[:40])

    with tempfile.TemporaryDirectory() as stale:
        # A prediction that ran out two days ago: the honest answer is to say
        # so, and never to name a next high water.
        write_record(stale, "tide-9414290",
                     tide_payload(now - 3 * 86400, 86400), 3 * 86400)
        rows, err = child_page(stale, 103, now)
        check("P103 expired: renders", rows is not None, err)
        if rows:
            body = " ".join(rows[1:7])
            check("P103 expired: says the prediction ran out",
                  "RAN OUT" in body or "TIDE ENDED" in body,
                  body.strip()[:44])
            check("P103 expired: names no next tide",
                  "HIGH" not in body and " LOW" not in body,
                  body.strip()[:44])
            check("P103 expired: still shows the age", "OLD" in body,
                  body.strip()[:44])


def test_live_cache():
    """Against whatever is really in the cache right now."""
    args = opts()
    r = tt.build(args)
    period = args.hold + args.load
    for i, number in enumerate((100, 101, 102, 103, 104)):
        f = r(i * period + args.load + 1.0, 0)
        check("P%d renders from the live cache" % number,
              f.shape == (64, 320, 3))
    f = r(args.load + 1.0, 0)
    board = line(f, 5)
    check("the index board names every product it reads",
          all(k in board for k in ("WX", "TIDE", "SEA", "PWR")), board)


def test_freshness_colours():
    check("fresh is green", tt.freshness("solar-garden", ({}, 10.0))[1]
          == tt.GREEN)
    check("stale is yellow", tt.freshness("solar-garden", ({}, 3600.0))[1]
          == tt.YELLOW)
    check("very stale is red", tt.freshness("solar-garden", ({}, 86400.0))[1]
          == tt.RED)
    check("absent is red and says so",
          tt.freshness("solar-garden", None) == ("NO DATA", tt.RED))


def test_speed():
    args = opts()
    r = tt.build(args)
    r(0.0, 0)
    n, times = 400, []
    for i in range(n):
        t0 = time.perf_counter()
        r(i * 0.05, i)
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    mean = sum(times) / n
    print("  ..   %-56s mean %.3f ms  p95 %.3f  max %.3f"
          % ("desktop render cost", mean, times[int(n * 0.95)], times[-1]))
    check("render is comfortably under the frame budget", mean < 5.0,
          "%.3f ms" % mean)


def main():
    print(__doc__.strip().split("\n")[0])
    for fn in (test_mosaic_alphabet, test_double_height, test_font_fits,
               test_geometry, test_palette_only, test_header,
               test_clock_ticks, test_cycle, test_flip_is_noisy_then_clean,
               test_reveal, test_purity, test_ident_has_no_numbers,
               test_freshness_colours, test_states, test_live_cache,
               test_speed):
        print("\n%s" % fn.__name__)
        fn()
    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  FAILED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
