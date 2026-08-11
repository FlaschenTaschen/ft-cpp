#!/usr/bin/env python3
"""Checks for helicorder.py and its Steim2 decoder that a screenshot cannot make.

This panel can draw a beautiful, confident, wrong picture in at least five
ways, and every one of them looks exactly like a helicorder:

  1. **The decoder can be subtly wrong.** Steim2 packs its differences
     right-aligned against bit 0, and shifting down from bit 31 instead --
     which is the obvious thing to write, and what the first draft of this did
     -- decodes every record into a plausible-looking wiggle of entirely
     invented numbers. So the decoder is round-tripped here against an encoder
     written for the test, over a series that deliberately uses all seven
     packings, and separately against the reverse integration constant, which
     is the field the format carries for exactly this purpose.
  2. **The drum can be upside down.** Six traces are six traces; newest at the
     top is as pretty as oldest at the top and is a different six hours.
  3. **The scale can be arbitrary.** If the vertical scale followed the data's
     own maximum, a magnitude 5 would look identical to a quiet afternoon and
     the panel would be a picture of nothing. The background must fill a
     predictable fraction of a lane and a burst must clip out of it.
  4. **A gap can draw as a flat line**, which is the panel claiming the ground
     was still when in fact nobody was listening.
  5. **The pen can sit at the right-hand edge** whatever the data says, which
     is a chart drawn to the edge when the fetcher stopped an hour ago.

Two things about how these are run, both learned the hard way in this tree.
Every check renders frames **sequentially from a fresh `build()`**, because
the demo reveals itself over the first few seconds. And `ftdata.CACHE_DIR`
binds at import, so the three data states -- fresh, stale, absent -- are each
run in a **separate process** with FT_DATA_CACHE set, at the bottom of this
file.

    $ python3 scripts/test-helicorder.py
    $ python3 scripts/test-helicorder.py --cache-dir /tmp/c

The live cache is only needed for the checks against real data; everything
else builds its own cache directory and needs nothing. Populate it with
`python3 ftdata.py --once --only helicorder-bk`.
"""

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402
import helicorder as hc                                       # noqa: E402

FAILED = []
PASSED = [0]

BIN_S = 12.0
TRACE_COLS = 300
LANES = 6
COLS = LANES * TRACE_COLS


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(hc, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build. Never sample sparsely."""
    r = hc.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def settled(args, n=200):
    """A frame from after the reveal has finished, still rendered in order."""
    return frames(args, n)


def contains_text(frame, s, thresh=90, scales=(1, 2), bg_max=0.25):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure the honest
    message reached it rather than merely being computed. The counters between
    the strokes have to be dark too: this panel has solid blocks of lit colour
    in it where a trace has clipped, and a matcher that only asks "are the
    strokes on" says yes to every string in the language somewhere inside one.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = hc.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                win = row[:, x:x + gw]
                if not np.array_equal(win & m, m):
                    continue
                if (win & ~m).mean() <= bg_max:
                    return True
    return False


def lane_rows(lay, i):
    return lay.lane_y(i), lay.lane_y(i) + lay.lane_h


def count_colour(frame, rgb, y0, y1, x0=0, x1=None):
    seg = frame[y0:y1, x0:(frame.shape[1] if x1 is None else x1)]
    return int((seg == np.array(rgb, np.uint8)).all(axis=2).sum())


# --------------------------------------------------------------------------
# 1. A Steim2 encoder, written only so the decoder can be round-tripped.
#
# Nothing in the tree encodes miniSEED and nothing should; this exists to
# produce a record whose correct decoding is known by construction, over a
# series chosen so that every one of the seven packings is exercised. The
# packing rule under test is the placement: for c=1 four 8-bit differences
# fill all 32 bits, but for c=2 and c=3 the top two bits are the dnib and the
# differences are right-aligned in what is left -- so seven 4-bit differences
# occupy bits 0-27 and bits 28-29 are simply unused.
# --------------------------------------------------------------------------

PACKINGS = [(1, 0, 4, 8), (2, 1, 1, 30), (2, 2, 2, 15), (2, 3, 3, 10),
            (3, 0, 5, 6), (3, 1, 6, 5), (3, 2, 7, 4)]


def _pack_word(nib, dnib, n, bits, diffs):
    word = 0
    for k, d in enumerate(diffs):
        v = int(d) & ((1 << bits) - 1)
        word |= v << (bits * (n - 1 - k))
    if nib != 1:
        word |= (dnib & 3) << 30
    return word & 0xFFFFFFFF


def steim2_record(samples, start, rate=40.0, reclen=512, seq=1):
    """A single Steim2 miniSEED record carrying `samples`. Big endian."""
    samples = [int(v) for v in samples]
    # The first difference in the stream is the step from the previous
    # record's last sample and is meaningless; the decoder skips it and so
    # must this.
    diffs = [0] + [samples[i] - samples[i - 1] for i in range(1, len(samples))]

    words, nibbles, i, p = [], [], 0, 0
    while i < len(diffs):
        for _ in range(len(PACKINGS)):
            nib, dnib, n, bits = PACKINGS[p % len(PACKINGS)]
            p += 1
            lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
            group = diffs[i:i + n]
            if len(group) == n and all(lo <= d <= hi for d in group):
                break
        else:                                                # pragma: no cover
            raise ValueError("no packing fits")
        group = diffs[i:i + n]
        if len(group) < n:
            group = group + [0] * (n - len(group))
        words.append(_pack_word(nib, dnib, n, bits, group))
        nibbles.append(nib)
        i += n

    # Frames of sixteen words: word 0 is the map, and in frame 0 words 1 and 2
    # are the two integration constants rather than data.
    frames_out, k = [], 0
    first = True
    while k < len(words) or first:
        slots = 13 if first else 15
        take = words[k:k + slots]
        nibs = nibbles[k:k + slots]
        body = ([samples[0], samples[-1]] if first else []) + list(take)
        nmap = ([0, 0] if first else []) + list(nibs)
        body += [0] * (15 - len(body))
        nmap += [0] * (15 - len(nmap))
        w0 = 0
        for j, nb in enumerate(nmap):
            w0 |= (nb & 3) << (30 - 2 * (j + 1))
        frames_out.append(struct.pack(">16I", w0,
                                      *[v & 0xFFFFFFFF for v in body]))
        k += slots
        first = False

    payload = b"".join(frames_out)
    lt = time.gmtime(start)
    ticks = int(round((start - math.floor(start)) * 1e4))
    hdr = (b"%06d" % seq) + b"D" + b" "
    hdr += b"BRK  " + b"00" + b"BHZ" + b"BK"
    hdr += struct.pack(">HHBBBBH", lt.tm_year, lt.tm_yday, lt.tm_hour,
                       lt.tm_min, lt.tm_sec, 0, ticks)
    hdr += struct.pack(">HhhBBBB", len(samples), int(rate), 1, 0, 0, 0, 1)
    hdr += struct.pack(">iHH", 0, 64, 48)
    assert len(hdr) == 48, len(hdr)
    blk = struct.pack(">HHBBBB", 1000, 0, 11, 1, int(math.log2(reclen)), 0)
    rec = hdr + blk + b"\0" * (64 - 48 - len(blk)) + payload
    return rec + b"\0" * (reclen - len(rec))


def test_steim():
    print("\nthe Steim2 decoder")
    rng = np.random.default_rng(20260810)
    # A series with steps of every size, so every packing gets used: tiny
    # wiggles for the 4-bit words, one enormous jump for the 30-bit one.
    s = np.cumsum(rng.integers(-6, 7, 400)).astype(np.int64) + 100000
    s[200] += 900000
    s[201] -= 900000
    rec = steim2_record(s, time.time() - 600)

    got, x0, xn = ftdata._steim_decode(rec[64:], len(s), 2)
    check("round trip: every sample", np.array_equal(np.asarray(got), s),
          "%d samples, %d differ" % (len(s), int((np.asarray(got) != s).sum())))
    check("forward integration constant is the first sample", x0 == int(s[0]),
          "%d vs %d" % (x0, s[0]))
    check("reverse integration constant is the last sample", xn == int(s[-1]),
          "%d vs %d" % (xn, s[-1]))

    segs, rate = ftdata._mseed_series(rec)
    check("_mseed_series reads the record", len(segs) == 1 and rate == 40.0,
          "%d segments at %g sps" % (len(segs), rate))
    check("and its samples survive the header walk",
          np.array_equal(segs[0][1], s.astype(np.int32)))

    # Corrupt the reverse constant: the decoder must refuse rather than store
    # a plausible wrong wiggle. This is the whole reason the field exists.
    bad = bytearray(rec)
    bad[64 + 8:64 + 12] = struct.pack(">i", int(s[-1]) + 1)
    try:
        ftdata._mseed_series(bytes(bad))
        ok = False
    except ValueError:
        ok = True
    check("a wrong reverse constant raises", ok)

    # All seven packings really were used, or the round trip proved less than
    # it looks like it proved.
    w = np.frombuffer(rec[64:], ">u4").reshape(-1, 16)
    nib = (w[:, :1] >> (30 - 2 * np.arange(1, 16, dtype=np.uint32))) & 3
    dnib = (w[:, 1:] >> 30) & 3
    used = set()
    for n, d, _cnt, _bits in PACKINGS:
        if ((nib == n) & (dnib == d)).any():
            used.add((n, d))
    check("the test series used all seven packings", len(used) == 7,
          "used %d" % len(used))


# --------------------------------------------------------------------------
# 2. A drum we invented, so every answer is known before it is drawn.
# --------------------------------------------------------------------------

SCALE_COUNTS = 2531350000.0            # counts per m/s, BK.BRK as of 2011


def synthetic(cache_dir, fetched_ago=60.0, noise=4000, burst=None,
              gaps=(), n_filled=COLS, t0=None, mangle=None):
    """Write a helicorder-bk record by hand. Returns (path, truth dict).

    `noise` is the peak-to-peak of the flat background, in counts, and it is
    *exact* in every column, so a check can say "this many rows" and mean it.
    `burst` is (column, peak-to-peak) and is the earthquake.
    """
    os.makedirs(cache_dir, exist_ok=True)
    if t0 is None:
        t0 = math.floor(time.time() / 3600.0) * 3600.0 - (LANES - 1) * 3600.0
    half = noise // 2
    lo = [-half] * COLS
    hi = [half] * COLS
    if burst is not None:
        col, amp = burst
        for c in range(col, min(COLS, col + 12)):
            lo[c], hi[c] = -amp // 2, amp // 2
    for c in gaps:
        lo[c] = hi[c] = None
    for c in range(n_filled, COLS):
        lo[c] = hi[c] = None

    payload = {
        "station": {"net": "BK", "sta": "BRK", "loc": "00", "cha": "BHZ",
                    "lat": 37.87352, "lon": -122.260986, "elev": 49.4,
                    "instrument": "STS-2,Velocity Sensor,STRECKEISEN",
                    "scale": SCALE_COUNTS, "scale_units": "M/S", "rate": 40.0,
                    "meta_at": time.time()},
        # BRK from the wall: recomputed with ftdata._quake_km_bearing() when the
        # site moved 273 m west, which is 0.2 km and half a degree of it.
        "site": [37.7624929274026, -122.39969356310202],
        "km": 17.3, "bearing": 45,
        "t0": t0, "t1": t0 + COLS * BIN_S,
        "filled_to": t0 + n_filled * BIN_S,
        "bin_s": BIN_S, "cols": COLS, "trace_cols": TRACE_COLS,
        "span_h": LANES,
        "lo": lo, "hi": hi,
        "n_have": sum(1 for v in lo if v is not None),
        "noise": float(noise),
        "peak": float(max([abs(v) for v in lo + hi if v is not None] or [0])),
        "peak_t": t0,
    }
    if mangle:
        mangle(payload)
    rec = {"name": "helicorder-bk", "fetched_at": time.time() - fetched_ago,
           "source": "synthetic", "ttl": 1800, "payload": payload}
    path = os.path.join(cache_dir, "helicorder-bk.json")
    with open(path, "w") as fh:
        json.dump(rec, fh)
    return path, {"t0": t0, "noise": noise, "n_filled": n_filled}


def write_quakes(cache_dir, events):
    """A quake-usgs record with just the fields helicorder.py reads."""
    rec = {"name": "quake-usgs", "fetched_at": time.time(), "source": "synthetic",
           "ttl": 3600,
           "payload": {"site": [37.7624929274026, -122.39969356310202],
                       "span_h": 168.0,
                       "local": {"radius_km": 300.0, "n": len(events),
                                 "events": events},
                       "world": {"min_mag": 4.5, "n": 0, "events": []},
                       "baseline": None}}
    with open(os.path.join(cache_dir, "quake-usgs.json"), "w") as fh:
        json.dump(rec, fh)


# --------------------------------------------------------------------------
# 3. The picture.
# --------------------------------------------------------------------------

def test_lane_order():
    print("\noldest at the top, newest at the bottom")
    tmp = tempfile.mkdtemp(prefix="heli-order")
    try:
        def spill(frame, lay):
            """Clipped pixels per lane. A burst shows up in its neighbours."""
            return [count_colour(frame, hc.C_CLIP, *lane_rows(lay, i))
                    for i in range(lay.lanes)]

        # One burst, in the first hour and nowhere else. It is far too big for
        # its lane, so it scribbles into the lane *below* -- and, if the drum
        # were upside down, into the lane above the bottom one instead.
        synthetic(tmp, burst=(40, 400000))
        r, f = settled(opts(cache_dir=tmp, sweep=0))
        sp = spill(f, r.state["lay"])
        check("a burst in the first hour scribbles into the second lane",
              sp[1] > 20, str(sp))
        check("and reaches nowhere near the bottom of the drum",
              sum(sp[3:]) == 0, str(sp))

        shutil.rmtree(tmp, ignore_errors=True)
        # And the mirror image, so this cannot pass by drawing everywhere.
        synthetic(tmp, burst=(COLS - 60, 400000))
        r, f = settled(opts(cache_dir=tmp, sweep=0))
        sp = spill(f, r.state["lay"])
        check("a burst in the last hour scribbles into the one above it",
              sp[LANES - 2] > 20, str(sp))
        check("and reaches nowhere near the top of the drum",
              sum(sp[:LANES - 3]) == 0, str(sp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scale():
    print("\nvertical scale: fixed against the background, not the maximum")
    tmp = tempfile.mkdtemp(prefix="heli-scale")
    try:
        # The gain is lane-heights per background peak-to-peak, so at gain 4
        # the quiet trace is a quarter of a nine row lane: two or three rows.
        synthetic(tmp)
        r, f = settled(opts(cache_dir=tmp, sweep=0, gain=4.0))
        lay = r.state["lay"]
        col = lay.x0 + 20
        y0, y1 = lane_rows(lay, 0)
        lit = int((f[y0:y1, col].max(axis=1) > 40).sum())
        check("a quiet hour is a thin ribbon inside its lane",
              2 <= lit <= 4, "%d of %d rows lit" % (lit, lay.lane_h))
        check("and never leaves it",
              count_colour(f, hc.C_CLIP, lay.chart_y,
                           lay.chart_y + lay.chart_h) == 0)

        # A hundredfold burst is not drawn a hundred times taller -- it is
        # capped -- but it must reach outside its lane and into the next.
        shutil.rmtree(tmp, ignore_errors=True)
        synthetic(tmp, burst=(TRACE_COLS + 100, 400000))
        r, f = settled(opts(cache_dir=tmp, sweep=0, gain=4.0, clip_lanes=1.5))
        lay = r.state["lay"]
        spill_up = count_colour(f, hc.C_CLIP, *lane_rows(lay, 0))
        spill_dn = count_colour(f, hc.C_CLIP, *lane_rows(lay, 2))
        check("a big event overruns into the lane above", spill_up > 0,
              "%d pixels" % spill_up)
        check("and into the lane below", spill_dn > 0, "%d pixels" % spill_dn)
        reach = count_colour(f, hc.C_CLIP, lay.chart_y,
                             lay.chart_y + lay.chart_h)
        check("but the cap keeps it off most of the panel",
              reach < lay.chart_h * lay.trace_w * 0.25, "%d pixels" % reach)

        # The same event against a background four times louder must be
        # *smaller* on the panel, which is the whole point of scaling against
        # the background: the picture is a ratio, not an absolute. A moderate
        # burst rather than the huge one above, because two events that both
        # hit the clip cap are the same size by definition and would make this
        # check pass whatever the scaling did.
        shutil.rmtree(tmp, ignore_errors=True)
        synthetic(tmp, noise=4000, burst=(TRACE_COLS + 100, 60000))
        r1, f1 = settled(opts(cache_dir=tmp, sweep=0, gain=4.0, clip_lanes=1.5))
        quiet_bg = count_colour(f1, hc.C_CLIP, r1.state["lay"].chart_y,
                                r1.state["lay"].chart_y
                                + r1.state["lay"].chart_h)
        shutil.rmtree(tmp, ignore_errors=True)
        synthetic(tmp, noise=16000, burst=(TRACE_COLS + 100, 60000))
        r2, f2 = settled(opts(cache_dir=tmp, sweep=0, gain=4.0, clip_lanes=1.5))
        loud = count_colour(f2, hc.C_CLIP, r2.state["lay"].chart_y,
                            r2.state["lay"].chart_y + r2.state["lay"].chart_h)
        check("a fifteenfold burst leaves its lane", quiet_bg > 0,
              "%d clipped pixels" % quiet_bg)
        check("the same burst against a louder background does not",
              loud == 0, "%d clipped pixels" % loud)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gaps_and_pen():
    print("\ngaps, and where the pen is")
    tmp = tempfile.mkdtemp(prefix="heli-gap")
    try:
        gaps = tuple(range(500, 530))
        synthetic(tmp, gaps=gaps)
        r, f = settled(opts(cache_dir=tmp, sweep=0))
        lay = r.state["lay"]
        lane = gaps[0] // TRACE_COLS
        y0, y1 = lane_rows(lay, lane)
        x = lay.x0 + ((gaps[10] % TRACE_COLS) * lay.trace_w) // TRACE_COLS
        col = f[y0:y1, x]
        check("a gap is not drawn as a trace",
              int((col.max(axis=1) > 40).sum()) <= 1,
              "%d rows lit" % int((col.max(axis=1) > 40).sum()))
        check("a gap is marked", count_colour(f, hc.C_GAP, y0, y1) >= 20,
              "%d gap pixels" % count_colour(f, hc.C_GAP, y0, y1))

        # The pen belongs at the end of the data, not at the edge of the panel.
        shutil.rmtree(tmp, ignore_errors=True)
        synthetic(tmp, n_filled=TRACE_COLS * 3 + 90)
        r, f = settled(opts(cache_dir=tmp, sweep=0))
        lay = r.state["lay"]
        lane, x = r.state["pen"]
        want = lay.x0 + (89 * lay.trace_w) // TRACE_COLS
        check("the pen is at the end of the data", lane == 3 and x == want,
              "lane %d x %d, wanted lane 3 x %d" % (lane, x, want))
        y0, y1 = lane_rows(lay, 4)
        check("and the hour after it is blank",
              int((f[y0:y1, lay.x0:lay.x1].max(axis=2) > 60).sum()) == 0)
        check("while the hour before it is not",
              int((f[lay.lane_y(2):lay.lane_y(3),
                     lay.x0:lay.x1].max(axis=2) > 60).sum()) > 100)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_quake_marks():
    print("\nborrowed earthquake marks")
    tmp = tempfile.mkdtemp(prefix="heli-quake")
    try:
        _p, truth = synthetic(tmp)
        t0 = truth["t0"]
        at = t0 + 4000.0
        write_quakes(tmp, [
            # Big and near: marked, and slid by the P travel time.
            {"id": "a", "t": at, "mag": 5.0, "km": 122.0, "bearing": 0,
             "place": "11 km N of Redwood Valley, CA"},
            # Small and far: below the distance-scaled threshold, not marked.
            {"id": "b", "t": t0 + 9000.0, "mag": 2.4, "km": 200.0,
             "bearing": 180, "place": "5 km NW of Pinnacles, CA"},
        ])
        r, f = settled(opts(cache_dir=tmp, sweep=0))
        evs = r.state["events"]
        check("one event is marked, not both", len(evs) == 1,
              "%d marked" % len(evs))
        check("the mark is slid by the P travel time",
              abs(evs[0]["t"] - (at + 122.0 / hc.P_KM_S)) < 0.01,
              "%+.1f s" % (evs[0]["t"] - at))
        check("the header names it", contains_text(f, "M5.0 REDWOOD VALLEY"))
        check("the mark is drawn",
              count_colour(f, hc.C_QUAKE_HI, 0, f.shape[0]) > 0)

        # No quake record at all: no marks, no traceback, still a drum.
        os.unlink(os.path.join(tmp, "quake-usgs.json"))
        r2, f2 = settled(opts(cache_dir=tmp, sweep=0))
        check("a missing quake record costs the marks and nothing else",
              r2.state["events"] == [] and f2.max() > 0
              and contains_text(f2, "BK.BRK BHZ"))
        check("and the header falls back to the peak amplitude",
              contains_text(f2, "UM/S"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_labels():
    print("\nthe things a person has to be able to read")
    tmp = tempfile.mkdtemp(prefix="heli-label")
    try:
        _p, truth = synthetic(tmp)
        args = opts(cache_dir=tmp, sweep=0)
        r, f = settled(args)
        check("the station is named", contains_text(f, "BK.BRK BHZ"))
        check("the vertical scale is in real units", contains_text(f, "UM/S"))
        check("the minute axis is labelled", contains_text(f, ":20"))
        lt = time.localtime(truth["t0"])
        want = ("%d%s" % (lt.tm_hour % 12 or 12,
                          "A" if lt.tm_hour < 12 else "P"))
        check("the first lane carries its own hour", contains_text(f, want),
              want)
        # A lane's full height in microns per second, computed here from the
        # record's own response, must be the number on the panel.
        um = truth["noise"] * args.gain / SCALE_COUNTS * 1e6
        s = ("%.2f" % um) if um < 10 else ("%.0f" % um)
        check("and the number is the right number", contains_text(f, s), s)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_motion_and_purity():
    print("\nmotion, and render() as a function of t alone")
    tmp = tempfile.mkdtemp(prefix="heli-move")
    try:
        synthetic(tmp)
        args = opts(cache_dir=tmp, reload=0)
        r = hc.build(args)
        seen = [r(i / 20.0, i).copy() for i in range(240)]
        diffs = sum(1 for a, b in zip(seen, seen[1:])
                    if not np.array_equal(a, b))
        check("every frame differs from the last", diffs == len(seen) - 1,
              "%d of %d" % (diffs, len(seen) - 1))
        drawn = [int((f[-20:].max(axis=2) > 60).sum()) for f in seen]
        check("the drum fills in as it reveals", drawn[5] < drawn[-1],
              "%d -> %d lit pixels in the last lane" % (drawn[5], drawn[-1]))

        # Purity: a cold build asked for one moment must equal the same moment
        # reached frame by frame from zero. With --reload 0 nothing in here
        # reads a clock.
        cold = hc.build(opts(cache_dir=tmp, reload=0))
        want = cold(9.35, 187).copy()
        check("cold render(t) == the same t driven from zero",
              np.array_equal(want, seen[187]) if len(seen) > 187 else False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_degraded():
    print("\nrecords that are wrong rather than merely old")
    tmp = tempfile.mkdtemp(prefix="heli-bad")
    try:
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        r, f = frames(opts(cache_dir=empty), 8)
        check("an empty cache says NO SEISMOGRAM",
              contains_text(f, "NO SEISMOGRAM"), str(r.state["problem"])[:40])

        short = os.path.join(tmp, "short")
        synthetic(short, mangle=lambda p: p["lo"].pop())
        r, f = frames(opts(cache_dir=short), 8)
        check("a truncated record says so, and draws no drum",
              contains_text(f, "NO SEISMOGRAM") and r.state["rec"] is None,
              str(r.state["problem"])[:40])

        allgap = os.path.join(tmp, "allgap")
        synthetic(allgap, n_filled=0)
        r, f = frames(opts(cache_dir=allgap), 8)
        check("a record with no samples in it draws the card",
              contains_text(f, "NO SEISMOGRAM"), str(r.state["problem"])[:40])

        junk = os.path.join(tmp, "junk")
        os.makedirs(junk)
        with open(os.path.join(junk, "helicorder-bk.json"), "w") as fh:
            fh.write("{not json at all")
        r, f = frames(opts(cache_dir=junk), 8)
        check("so does a corrupt one", contains_text(f, "NO SEISMOGRAM"),
              str(r.state["problem"])[:40])

        # A stale record still draws: six hours of ground motion does not stop
        # being six hours of ground motion, and every lane is labelled with the
        # hour it belongs to.
        old = os.path.join(tmp, "old")
        synthetic(old, fetched_ago=4 * 3600.0)
        r, f = settled(opts(cache_dir=old, sweep=0))
        check("a stale record still draws its drum, flagged",
              r.state["rec"] is not None and contains_text(f, "STALE"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. Fresh, stale and absent, each in a process of its own. ftdata.CACHE_DIR
# is read at import, so reloading the module in one process tests the state of
# its own import machinery and not the state of the cache.
# --------------------------------------------------------------------------

def _one_state(state, cache_dir):
    args = opts()                       # note: no cache_dir, so CACHE_DIR wins
    r = hc.build(args)
    out = None
    for i in range(200):
        out = r(i / 20.0, i)
    card = contains_text(out, "NO SEISMOGRAM")
    stale = contains_text(out, "STALE")
    drew = r.state["rec"] is not None
    verdict = {
        "fresh": (drew and not card and not stale, "drew a drum, no flags"),
        "stale": (drew and not card and stale, "drew a drum with STALE on it"),
        "absent": (not drew and card, "drew the no-data card"),
    }[state]
    print("RESULT %s %s cache=%s drew=%s card=%s stale=%s"
          % (state, "ok" if verdict[0] else "FAIL",
             os.path.basename(cache_dir), drew, card, stale))
    return 0 if verdict[0] else 1


def test_states_in_separate_processes():
    print("\nfresh, stale and absent -- one process each, FT_DATA_CACHE set")
    tmp = tempfile.mkdtemp(prefix="heli-proc")
    try:
        fresh = os.path.join(tmp, "fresh")
        synthetic(fresh, fetched_ago=120.0)
        stale = os.path.join(tmp, "stale")
        synthetic(stale, fetched_ago=4 * 3600.0)
        absent = os.path.join(tmp, "absent")
        os.makedirs(absent)

        for state, d in (("fresh", fresh), ("stale", stale),
                         ("absent", absent)):
            env = dict(os.environ, FT_DATA_CACHE=d, FT_DATA_BLOBS=d)
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--state", state, "--cache-dir", d],
                env=env, capture_output=True, text=True, timeout=300)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT")]
            check("%s cache, in its own process" % state,
                  proc.returncode == 0 and bool(line),
                  (line[0][7:] if line
                   else (proc.stderr.strip().splitlines() or ["no output"])[-1]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 5. Other panel sizes, the network promise, and the live cache.
# --------------------------------------------------------------------------

def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="heli-size")
    try:
        synthetic(tmp)
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, f = frames(opts(cache_dir=tmp, width=w, height=h), 60)
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                lay = r.state["lay"]
                detail = "%d lanes of %d rows" % (lay.lanes, lay.lane_h)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("helicorder-bk", tempfile.mkdtemp(prefix="heli-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "helicorder.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in src]
    check("helicorder.py does not import one either", not imported,
          ",".join(imported))


def test_live(cache_dir):
    print("\nthe live cache (%s)" % cache_dir)
    got = ftdata.load("helicorder-bk", cache_dir)
    if got is None:
        print("  --   no helicorder-bk record; run "
              "python3 ftdata.py --once --only helicorder-bk")
        return
    payload, age = got
    rec, _age, problem = hc.read_drum(cache_dir)
    check("the live record parses", rec is not None, str(problem))
    if rec is None:
        return
    check("it is the six hours it claims to be",
          rec["cols"] == rec["lanes"] * rec["trace_cols"]
          and abs(rec["cols"] * rec["bin_s"] - 6 * 3600.0) < 1.0,
          "%d columns of %gs" % (rec["cols"], rec["bin_s"]))
    check("the response is a plausible broadband sensitivity",
          1e8 < rec["counts_per_ms"] < 1e10,
          "%.3g counts per m/s" % rec["counts_per_ms"])
    um = hc.micron_s(rec["noise"], rec)
    check("the background is microns a second, not millimetres",
          um is not None and 0.05 < um < 50.0, "%.2f um/s peak to peak" % um)
    check("the record is small", os.path.getsize(
        ftdata.record_path("helicorder-bk", cache_dir)) < 64000,
        "%d bytes" % os.path.getsize(
            ftdata.record_path("helicorder-bk", cache_dir)))
    check("it is not older than its own window",
          age < 6 * 3600.0, ftdata.describe_age(age))
    r, f = settled(opts(cache_dir=cache_dir))
    check("and it draws", f.max() > 0 and f.shape == (64, 320, 3),
          "%d events marked" % len(r.state["events"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--state", default="",
                    choices=("", "fresh", "stale", "absent"),
                    help="internal: run one data state and print RESULT")
    a = ap.parse_args()
    if a.state:
        return _one_state(a.state, a.cache_dir)

    print("cache: %s" % a.cache_dir)
    test_no_network()
    test_steim()
    test_lane_order()
    test_scale()
    test_gaps_and_pen()
    test_quake_marks()
    test_labels()
    test_motion_and_purity()
    test_degraded()
    test_states_in_separate_processes()
    test_sizes()
    test_live(a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
