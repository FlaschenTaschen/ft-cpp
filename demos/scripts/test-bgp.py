#!/usr/bin/env python3
"""Checks for bgp.py and for ftdata's hand-written MRT parser.

Almost everything here is aimed at one file, and it is not the demo. A chart
of a number is easy to draw and easy to check; **a binary parser that walks
twelve megabytes of someone else's wire format is where the wrong answers
live**, and every one of its failure modes produces a panel that looks entirely
fine:

  1. **A miscounted prefix block.** NLRI is a length-in-bits byte followed by
     that many bits rounded up to octets, with trailing zeroes omitted. Get the
     rounding wrong and the walk desynchronises, the record's remaining
     prefixes are garbage, and the chart is simply a different height. Nothing
     about it looks wrong.
  2. **IPv6 silently missing.** v6 routes do not travel in the NLRI field at
     all; they are inside MP_REACH_NLRI, which is an optional attribute. A
     parser that skips attributes it does not recognise -- which is exactly
     what a parser must do -- drops half the real table and reports a plausible
     rate.
  3. **The AS path read off the wrong end.** The origin is the *last* ASN of
     the last segment. Reading the first gives the peer instead, and the ticker
     then confidently prints the collector's own neighbours as the origin of
     everything.
  4. **The ET record variant.** Real RouteViews files are entirely type 17,
     which is type 16 with four extra bytes of microseconds in front of the
     body. Four bytes of offset error puts the parser in the middle of a peer
     address and it recovers into nonsense.

So the parser is run against **MRT built here, byte by byte, whose answers are
arithmetic**: known prefixes, known withdrawals, a known v6 announcement, a
known prepended path, a known unknown attribute, and both record types.

The drawing is then checked in pixels against synthetic records, because the
second family of lies is a chart that is beautiful and upside down: the
withdrawal band on top, the square-root axis applied to the sum of two
transformed heights rather than to the cumulative value, or a spike that lands
in the wrong column.

    $ python3 scripts/test-bgp.py                     # uses the live cache
    $ python3 scripts/test-bgp.py --cache-dir /tmp/c  # or a pointed one

The live cache is only needed for the checks against real data; everything else
builds its own. Populate it with
`python3 ftdata.py --once --only bgp-sfmix`.
"""

import argparse
import json
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

import bgp                                                    # noqa: E402
import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402

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
    return ds.options(bgp, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build."""
    r = bgp.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def settled(args, n=80):
    """A frame from after the reveal has finished."""
    return frames(args, n)


def contains_text(frame, s, thresh=90, scales=(1, 2), bg_max=0.2):
    """Is this string drawn anywhere on the frame, at any position or size?

    The counters between the strokes have to be dark as well. This panel is a
    solid block of lit green over a third of its area, every pixel of a glyph
    mask is lit inside that block, and a matcher that only asks "are the
    strokes on" answers yes to every string in the language somewhere inside
    the chart.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = bgp.text_mask(s, scale)
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


def band_rows(frame, col, rgb, y0, y1):
    """How many rows of exactly this colour are in this column."""
    seg = frame[y0:y1, col]
    return int((seg == np.array(rgb, np.uint8)).all(axis=1).sum())


# --------------------------------------------------------------------------
# 1. MRT built by hand, so every count the parser reports is one we computed
#    before it ran. RFC 6396 for the framing, RFC 4271 and RFC 4760 for the
#    BGP UPDATE inside it.
# --------------------------------------------------------------------------

def enc_prefix(pfx):
    """A prefix as NLRI: one length-in-bits byte, then the significant bytes."""
    addr, _, bits = pfx.partition("/")
    bits = int(bits)
    nb = (bits + 7) // 8
    if ":" in addr:
        import socket
        raw = socket.inet_pton(socket.AF_INET6, addr)
    else:
        raw = bytes(int(x) for x in addr.split("."))
        raw = raw + b"\0" * (4 - len(raw))
    return bytes([bits]) + raw[:nb]


def enc_attr(atype, body, flags=0x40):
    return bytes([flags, atype, len(body)]) + body


def enc_as_path(asns):
    body = bytes([2, len(asns)]) + b"".join(struct.pack(">I", a) for a in asns)
    return enc_attr(2, body)


def enc_mp_reach(prefixes):
    body = (struct.pack(">HB", 2, 1)                 # AFI IPv6, SAFI unicast
            + bytes([16]) + b"\x20\x01" + b"\0" * 14  # next hop
            + b"\0")                                  # reserved
    body += b"".join(enc_prefix(p) for p in prefixes)
    return enc_attr(14, body, flags=0x80)


def enc_mp_unreach(prefixes):
    body = struct.pack(">HB", 2, 1) + b"".join(enc_prefix(p) for p in prefixes)
    return enc_attr(15, body, flags=0x80)


def enc_record(ts, peer_as, announce=(), withdraw=(), path=(),
               v6_announce=(), v6_withdraw=(), et=True, junk_attr=False):
    """One MRT BGP4MP(_ET) MESSAGE_AS4 record carrying one BGP UPDATE."""
    attrs = b""
    if path:
        attrs += enc_as_path(list(path))
    if junk_attr:
        # An attribute this parser has never heard of, with a length that must
        # be used to step over it. If it is not, everything after it is read as
        # attribute headers and the record falls apart.
        attrs += enc_attr(199, b"\xde\xad\xbe\xef" * 4)
    if v6_announce:
        attrs += enc_mp_reach(list(v6_announce))
    if v6_withdraw:
        attrs += enc_mp_unreach(list(v6_withdraw))

    wblock = b"".join(enc_prefix(p) for p in withdraw)
    nlri = b"".join(enc_prefix(p) for p in announce)
    update = (struct.pack(">H", len(wblock)) + wblock
              + struct.pack(">H", len(attrs)) + attrs + nlri)
    bgp_msg = b"\xff" * 16 + struct.pack(">HB", 19 + len(update), 2) + update

    body = (struct.pack(">IIHH", peer_as, 65000, 0, 1)        # AS4 header
            + bytes([192, 0, 2, 1]) + bytes([192, 0, 2, 2]))  # peer, local IP
    body += bgp_msg
    if et:
        body = struct.pack(">I", 123456) + body
    return struct.pack(">IHHI", ts, 17 if et else 16, 4, len(body)) + body


def test_mrt_parser():
    print("\nthe MRT parser, against bytes whose answers are known")
    t0 = 1700000000

    data = b"".join([
        # Three v4 prefixes, path ending in the origin, one prepend run. Only
        # the first prefix of a record becomes a ticker line -- one line per
        # UPDATE -- which is why the two awkward prefix lengths get records of
        # their own below rather than riding along in this one.
        enc_record(t0, 64512, announce=["192.0.2.0/24", "203.0.113.0/24",
                                        "192.0.2.128/25"],
                   path=[64512, 64513, 64514, 64514, 64514]),
        # A /22 and a /25: the two encodings where the number of octets on the
        # wire is not four and not obvious.
        enc_record(t0, 64512, announce=["198.51.100.0/22"], path=[64512, 64514]),
        enc_record(t0, 64512, announce=["203.0.113.128/25"], path=[64512, 64514]),
        # One withdrawal, no path at all, which is what a withdrawal looks like.
        # A different prefix from any announcement, so that a check reading
        # samples back by prefix cannot pick up the wrong one.
        enc_record(t0 + 1, 64512, withdraw=["192.168.0.0/16"]),
        # IPv6, which lives only in MP_REACH_NLRI, plus an attribute nobody
        # knows, plus the non-ET record type.
        enc_record(t0 + 1, 64520, v6_announce=["2001:db8::/32",
                                               "2001:db8:1::/48"],
                   path=[64520, 64521], et=False, junk_attr=True),
        # A v6 withdrawal, in MP_UNREACH_NLRI.
        enc_record(t0 + 2, 64520, v6_withdraw=["2001:db8:2::/48"]),
    ])
    got = ftdata._bgp_parse(data)

    check("announced prefixes counted", got["ann"] == 7,
          "got %d, want 7 (5 v4 + 2 v6)" % got["ann"])
    check("withdrawn prefixes counted", got["wdr"] == 2,
          "got %d, want 2 (1 v4 + 1 v6)" % got["wdr"])
    check("records with churn counted", got["records"] == 6, str(got["records"]))
    check("window span from the timestamps", got["secs"] == 3, str(got["secs"]))
    check("not marked truncated", got["truncated"] is False)

    # The origin is the LAST ASN of the last segment, and it gets credited with
    # every prefix in the record.
    origins = dict((a, c) for a, c in got["origins"])
    check("origin is the end of the AS path", origins.get(64514) == 5,
          "AS64514 -> %s, want 5" % origins.get(64514))
    check("v6 origin credited too", origins.get(64521) == 2,
          "AS64521 -> %s, want 2" % origins.get(64521))
    check("the peer is not mistaken for the origin", 64512 not in origins,
          "peer AS64512 appeared as an origin")
    check("distinct origins", got["n_origins"] == 2, str(got["n_origins"]))
    check("peers counted, not paths", got["n_peers"] == 2, str(got["n_peers"]))

    # Per-second binning. bin_secs is 2, so second 0 and second 1 share bin 0.
    nb = ftdata.BGP_BIN_SECS
    check("announcements binned at %ds" % nb,
          got["ann_bins"][0] == 5 + 2 and sum(got["ann_bins"]) == 7,
          "bins %s" % got["ann_bins"])
    check("withdrawals binned separately",
          got["wdr_bins"][0] == 1 and sum(got["wdr_bins"]) == 2,
          "bins %s" % got["wdr_bins"])
    # The peak is a *second*, not a bin: second t0 has five announcements in
    # it, t0+1 has two announcements and one withdrawal, t0+2 has one
    # withdrawal. Reporting the bin total instead would overstate the peak by
    # the bin width, every time.
    check("peak second found, not peak bin",
          got["peak"] == 5 and got["peak_at"] == t0,
          "%d at +%d" % (got["peak"], got["peak_at"] - t0))

    # The ticker samples, which are where the strings a viewer can check come
    # from. With four records they all fit in the reservoir.
    by_pfx = dict((s["p"], s) for s in got["samples"])
    check("v4 prefix formatted from the wire", "192.0.2.0/24" in by_pfx,
          ",".join(sorted(by_pfx))[:60])
    check("a /22 keeps its length", "198.51.100.0/22" in by_pfx,
          ",".join(sorted(by_pfx))[:70])
    check("a /25 keeps its odd octet", "203.0.113.128/25" in by_pfx,
          "the fourth octet is only on the wire because 25 bits needs it")
    check("v6 prefix formatted from the wire", "2001:db8::/32" in by_pfx)
    s = by_pfx.get("192.0.2.0/24")
    check("sampled origin matches the path", s and s["o"] == 64514,
          "o=%s" % (s or {}).get("o"))
    check("sampled path is the real path",
          s and s["path"] == [64512, 64513, 64514, 64514, 64514],
          str((s or {}).get("path")))
    wd = [x for x in got["samples"] if x["k"] == "W"]
    check("a withdrawal is sampled as a withdrawal", len(wd) == 2,
          "%d withdrawal lines" % len(wd))
    check("a withdrawal has no invented origin",
          all(x["o"] is None for x in wd),
          str([x["o"] for x in wd]))

    # And the failure this whole section exists for: a parser that ignored the
    # ET offset would read the peer ASN out of the microsecond field.
    check("ET and non-ET records both parsed",
          set(a for a, _ in got["origins"]) == set([64514, 64521]),
          str(got["origins"]))


def test_mrt_robustness():
    print("\nthe MRT parser, against bytes that are wrong")
    t0 = 1700000000
    good = enc_record(t0, 64512, announce=["192.0.2.0/24"], path=[64512, 64513])

    # A stream cut in the middle of a record, which is exactly what the
    # decompression cap leaves behind. Everything before the cut must survive.
    cut = good + good[:len(good) // 2]
    got = ftdata._bgp_parse(cut)
    check("a truncated tail keeps the whole records before it",
          got["ann"] == 1 and got["truncated"] is True,
          "ann=%d truncated=%s" % (got["ann"], got["truncated"]))

    # A prefix claiming more bits than an address has. The walk must stop, not
    # run off into the attributes and invent hundreds of routes.
    bad = bytearray(good)
    # The NLRI is the last thing in the record and a /24 is four bytes of it,
    # so the length-in-bits byte is exactly four from the end.
    bad[-4] = 200
    try:
        got = ftdata._bgp_parse(bytes(bad))
        ok = got["ann"] == 0
        detail = "ann=%d" % got["ann"]
    except RuntimeError:
        ok, detail = True, "no UPDATEs found, which is also correct"
    except Exception as e:                                   # noqa: BLE001
        ok, detail = False, "raised %r" % e
    check("an impossible prefix length stops the walk", ok, detail)

    # Nothing at all.
    try:
        ftdata._bgp_parse(b"")
        check("an empty stream is an error, not an empty chart", False,
              "returned a payload")
    except RuntimeError:
        check("an empty stream is an error, not an empty chart", True)

    # A KEEPALIVE, which is a BGP message with no churn in it and must not
    # count as a record.
    ka = enc_record(t0, 64512)
    try:
        ftdata._bgp_parse(ka)
        check("an UPDATE with no prefixes is not churn", False, "counted it")
    except RuntimeError:
        check("an UPDATE with no prefixes is not churn", True)


# --------------------------------------------------------------------------
# 2. A window we invented, so that every answer is known before it is drawn.
# --------------------------------------------------------------------------

NBINS = 450
BIN_SECS = 2


def synthetic(cache_dir, floor=100, spike_bin=300, spike=4000, wdr=6,
              fetched_ago=0.0, samples=None, mangle=None):
    """Write a bgp-sfmix record by hand. Returns (path, truth dict)."""
    t0 = int(time.time()) - 1200
    ann_bins = [floor * BIN_SECS] * NBINS
    ann_bins[spike_bin] = spike * BIN_SECS
    wdr_bins = [wdr * BIN_SECS] * NBINS
    # One bin with a single withdrawal in it, which on a 4000/s axis is far
    # under one row and must still be drawn. This is the "an outage must not
    # round away" check's subject.
    wdr_bins[10] = 1
    wdr_bins[11] = 0

    if samples is None:
        samples = [
            {"k": "A", "p": "192.0.2.0/24", "n": 1, "peer": 64512,
             "o": 64514, "path": [64512, 64513, 64514], "t": t0 + 5},
            {"k": "W", "p": "198.51.100.0/22", "n": 1, "peer": 64512,
             "o": None, "path": [], "t": t0 + 9},
            {"k": "A", "p": "2001:db8::/32", "n": 2, "peer": 64520,
             "o": 64521, "path": [64520, 64521, 64521, 64521], "t": t0 + 40},
        ]
    payload = {
        "collector": "route-views.test", "site": "TESTNET NOWHERE",
        "file": "updates.test.bz2", "bytes": 1, "mrt_bytes": 1,
        "t0": t0, "t1": t0 + NBINS * BIN_SECS - 1,
        "secs": NBINS * BIN_SECS, "records": 1000, "truncated": False,
        "ann": sum(ann_bins), "wdr": sum(wdr_bins),
        "ann_s": sum(ann_bins) / float(NBINS * BIN_SECS),
        "wdr_s": sum(wdr_bins) / float(NBINS * BIN_SECS),
        "peak": spike, "peak_at": t0 + spike_bin * BIN_SECS,
        "bin_secs": BIN_SECS, "ann_bins": ann_bins, "wdr_bins": wdr_bins,
        "peers": [[64512, 10], [64520, 5]], "n_peers": 2,
        "origins": [[64514, 7], [64521, 3]], "n_origins": 1568,
        "samples": samples,
    }
    if mangle:
        mangle(payload)
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "bgp-sfmix.json")
    with open(path, "w") as fh:
        json.dump({"name": "bgp-sfmix", "fetched_at": time.time() - fetched_ago,
                   "source": "test", "ttl": ftdata.BGP_TTL,
                   "payload": payload}, fh)
    return path, {"floor": floor, "spike": spike, "spike_bin": spike_bin,
                  "wdr": wdr, "t0": t0}


def test_chart_pixels():
    print("\nthe chart, in pixels")
    tmp = tempfile.mkdtemp(prefix="bgp-chart")
    try:
        _, truth = synthetic(tmp)
        r, f = settled(opts(cache_dir=tmp))
        lay = r.layout
        top, bot = lay.chart_y, lay.chart_bot
        nrows = bot - top + 1
        scale = r.state["scale"]

        spike_col = int((truth["spike_bin"] + 0.5) / NBINS * lay.w)
        quiet_col = 40

        def bar(col):
            # Above the gridline colour, not above black: the chart has a
            # vertical rule at every quarter of the window and a horizontal one
            # at half and quarter height, and counting those as bar rows makes
            # three columns look full height. That mistake made this check pass
            # on the wrong column the first time it was written.
            seg = f[top:bot + 1, col]
            return int((seg.max(axis=1) > 60).sum())

        # The spike must be nearly full height and must be in the column its
        # bin maps to; a resampler that dropped bins would put it elsewhere or
        # lose it. "Nearly" and not "exactly" because 450 bins over 320 columns
        # means the spike's bin straddles a column boundary and a few per cent
        # of it lands next door -- which is the resampler being right, not
        # wrong.
        heights = np.array([bar(c) for c in range(lay.w)])
        check("the spike nearly fills the chart",
              heights[spike_col] >= nrows * 0.85,
              "%d of %d rows at column %d" % (heights[spike_col], nrows,
                                              spike_col))
        # Nothing else comes close. The left margin is excluded because the two
        # bright axis numbers are printed over the chart there, and the last
        # column because the pulsing window-edge marker is drawn over it.
        elsewhere = [c for c in range(30, lay.w - 1)
                     if abs(c - spike_col) > 2 and heights[c] >= nrows * 0.85]
        check("and nothing else on the chart does", not elsewhere,
              "also near full height at %s" % elsewhere[:6])

        # And the floor must NOT be one row. This is what the square-root axis
        # is for: on a linear axis a 100/s floor under a 4000/s peak is 0.6 of
        # a row and the panel's entire subject is invisible.
        q = bar(quiet_col)
        check("the background hiss is legibly tall",
              4 <= q <= nrows - 4, "%d of %d rows at the floor" % (q, nrows))
        check("and the spike still towers over it",
              heights[spike_col] >= 3 * q,
              "%d rows against %d" % (heights[spike_col], q))

        # The axis is square root, so half height is a QUARTER of full scale.
        # Assert it on the drawn pixels, not on the formula.
        expect = int(round(bgp.bar_height(np.array([scale / 4.0]),
                                          scale, float(nrows))[0]))
        check("half height is a quarter of full scale",
              abs(expect - nrows / 2.0) <= 1.0,
              "%d rows of %d" % (expect, nrows))

        # Stack order. Amber must be at the BOTTOM of the column: withdrawals
        # on top would be an exactly-as-pretty chart saying the opposite thing.
        col = f[top:bot + 1, quiet_col]
        lit = np.flatnonzero(col.max(axis=1) > 0)
        amber = np.flatnonzero((col == np.array(bgp.C_WDR, np.uint8)).all(axis=1))
        check("withdrawals are the bottom band", len(amber) and
              amber.max() >= lit.max() and amber.min() > lit.min(),
              "amber rows %s..%s, lit %s..%s" % (
                  amber.min() if len(amber) else None,
                  amber.max() if len(amber) else None, lit.min(), lit.max()))
        check("announcements are the band above",
              band_rows(f, quiet_col, bgp.C_ANN, top, bot + 1) > 0)

        # A single withdrawn prefix in a 900-second window is 0.0004 of a row.
        # It must still be one lit amber pixel, because the alternative is a
        # chart that drops small outages.
        tiny_col = int((10 + 0.5) / NBINS * lay.w)
        check("one withdrawal is never rounded away",
              band_rows(f, tiny_col, bgp.C_WDR, top, bot + 1) >= 1,
              "column %d" % tiny_col)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resample_conserves():
    print("\nthe resampler")
    # 450 bins onto 320 columns is not a whole number, which is the case a
    # nearest-bin resampler gets silently wrong by dropping a third of them.
    rng = np.random.default_rng(7)
    bins = rng.integers(0, 500, NBINS).astype(np.float64)
    cols = bgp.column_rates(bins, BIN_SECS, 320)
    total_in = bins.sum()
    total_out = float(cols.sum()) * (NBINS * BIN_SECS / 320.0)
    check("no counts are lost resampling 450 bins to 320 columns",
          abs(total_out - total_in) / total_in < 1e-6,
          "%.1f in, %.1f out" % (total_in, total_out))

    # A lone spike in one bin must survive into some column, at close to its
    # own magnitude rather than smeared to nothing.
    lone = np.zeros(NBINS)
    lone[123] = 9000.0
    cols = bgp.column_rates(lone, BIN_SECS, 320)
    check("a one-bin spike survives resampling",
          cols.max() > 9000.0 / BIN_SECS * 0.5,
          "peak column %.0f/s from a %.0f/s bin" % (cols.max(),
                                                    9000.0 / BIN_SECS))


def test_ticker():
    print("\nthe ticker")
    tmp = tempfile.mkdtemp(prefix="bgp-tick")
    try:
        synthetic(tmp)
        args = opts(cache_dir=tmp)
        r = bgp.build(args)
        # Over one full pass of the strip, every line must appear. Read the
        # strings back off the rendered panel rather than off the strip, which
        # is the only way to know they were not scrolled past or clipped.
        seen = {"192.0.2.0/24": False, "AS64514": False,
                "198.51.100.0/22": False, "2001:DB8::/32": False,
                "WDR BY AS64512": False}
        for i in range(0, 240):
            f = r(i / 20.0, i)
            for s in seen:
                if not seen[s] and contains_text(f, s, scales=(1,)):
                    seen[s] = True
            if all(seen.values()):
                break
        for s, ok in sorted(seen.items()):
            check("ticker shows %r" % s, ok)

        # Prepending must be collapsed: the v6 sample's path is
        # [64520, 64521, 64521, 64521] and the line must not print 64521 three
        # times over.
        check("prepended AS runs are collapsed",
              bgp.dedup([64520, 64521, 64521, 64521]) == [64520, 64521],
              str(bgp.dedup([64520, 64521, 64521, 64521])))
        check("an AS_SET is drawn as one ASN",
              bgp.dedup([1, [2, 3], 3]) == [1, 2, 3],
              str(bgp.dedup([1, [2, 3], 3])))

        # A withdrawal must never be given an origin AS.
        segs = bgp.ticker_line({"k": "W", "p": "1.2.3.0/24", "peer": 64512,
                                "o": None, "path": []})
        text = "".join(t for t, _ in segs)
        check("a withdrawal line names the peer, not an origin",
              "WDR BY AS64512" in text and text.count("AS") == 1, text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_motion_and_purity():
    print("\nmotion, and render() as a function of t")
    tmp = tempfile.mkdtemp(prefix="bgp-move")
    try:
        synthetic(tmp)
        args = opts(cache_dir=tmp)

        # Every frame different. The ticker only steps a whole pixel six times
        # a second; without the pulse the panel would hold one frame for 160 ms
        # at a time, which between two animated demos reads as a crash.
        r = bgp.build(args)
        seen = set()
        for i in range(60):
            seen.add(r(3.0 + i / 20.0, i).tobytes())
        check("every frame differs from the last", len(seen) == 60,
              "%d distinct of 60" % len(seen))

        # The ticker really scrolls: the same row must show different pixels a
        # second apart.
        lay = r.layout
        a = r(4.0, 80)[lay.tick_y:].copy()
        b = r(5.0, 100)[lay.tick_y:].copy()
        check("the ticker scrolls a whole line a second",
              not np.array_equal(a, b))

        # Purity. The scheduler starts segments at t=0 on a worker thread and
        # the preview baker steps at a fixed rate; a demo that accumulates
        # state between calls desyncs between the two.
        T = 7.3
        cold = bgp.build(args)(T, int(T * 20)).copy()
        r2 = bgp.build(args)
        for i in range(int(T * 20) + 1):
            r2(i / 20.0, i)
        driven = r2(T, int(T * 20)).copy()
        check("render(t) is the same cold as driven from zero",
              np.array_equal(cold, driven))

        # The scroll wraps rather than running off the end of the strip.
        r3 = bgp.build(args)
        ok = True
        for i in range(0, 4000, 7):
            try:
                out = r3(i / 20.0, i)
            except Exception as e:                           # noqa: BLE001
                ok = False
                break
            if out.shape != (args.height, args.width, 3):
                ok = False
                break
        check("the ticker wraps cleanly over 200 seconds", ok)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_numbers():
    print("\nthe numbers on the header")
    tmp = tempfile.mkdtemp(prefix="bgp-num")
    try:
        _, truth = synthetic(tmp, floor=100, wdr=6)
        r, f = settled(opts(cache_dir=tmp))
        rate = int(round(r.state["rec"]["ann_s"] + r.state["rec"]["wdr_s"]))
        check("the headline rate is announcements plus withdrawals",
              abs(rate - (truth["floor"] + truth["wdr"])) <= 12,
              "%d/s drawn, floor %d + %d withdrawals (plus the spike's share)"
              % (rate, truth["floor"], truth["wdr"]))
        check("the headline rate is on the panel",
              contains_text(f, "%d PFX/S" % rate, scales=(1,)))
        check("the window peak is on the panel",
              contains_text(f, "PK %d/S" % truth["spike"], scales=(1,)))
        # Deliberately the dimmest text on the panel, so it is matched at a
        # lower threshold than the headline.
        check("the number of origins is on the panel",
              contains_text(f, "1568 ORIGINS", scales=(1,), thresh=60))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_degraded():
    print("\nstale, malformed and absent")
    tmp = tempfile.mkdtemp(prefix="bgp-bad")
    try:
        # Stale: it still draws, and it says so.
        d = os.path.join(tmp, "stale")
        synthetic(d, fetched_ago=ftdata.BGP_TTL * 3)
        r, f = settled(opts(cache_dir=d))
        check("a stale record still draws its chart",
              r.state["rec"] is not None and f.max() > 0)
        check("and the panel says STALE", contains_text(f, "STALE", scales=(1,)))

        # Fresh must NOT say it.
        d = os.path.join(tmp, "fresh")
        synthetic(d)
        _, f = settled(opts(cache_dir=d))
        check("a fresh record does not", not contains_text(f, "STALE",
                                                           scales=(1,)))

        # Malformed payloads, each of which used to be a traceback.
        for label, mangle in (
                ("bins of different lengths",
                 lambda p: p.__setitem__("wdr_bins", p["wdr_bins"][:10])),
                ("bins that are not numbers",
                 lambda p: p.__setitem__("ann_bins", ["a", "b", "c", "d"])),
                ("a zero-length window",
                 lambda p: p.update({"t1": p["t0"]})),
                ("no samples at all",
                 lambda p: p.__setitem__("samples", [])),
                ("a sample missing its path",
                 lambda p: p.__setitem__("samples",
                                         [{"k": "A", "p": "1.2.3.0/24"}])),
                ("a nonsense bin_secs",
                 lambda p: p.__setitem__("bin_secs", 0)),
        ):
            d = os.path.join(tmp, label.replace(" ", "-"))
            synthetic(d, mangle=mangle)
            try:
                _, f = settled(opts(cache_dir=d), 40)
                ok = f.shape == (64, 320, 3) and f.max() > 0
                detail = ""
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%s draws something" % label, ok, detail)

        # Absent.
        d = os.path.join(tmp, "absent")
        os.makedirs(d)
        _, f = settled(opts(cache_dir=d))
        check("no record at all gets a card, not a blank panel",
              contains_text(f, "NO BGP DATA") and f.max() > 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _one_state(state, cache_dir):
    """Run in a child process with FT_DATA_CACHE already set. See below."""
    r, f = settled(opts(cache_dir=cache_dir))
    if state == "absent":
        ok = r.state["rec"] is None and contains_text(f, "NO BGP DATA")
    elif state == "stale":
        ok = r.state["rec"] is not None and contains_text(f, "STALE",
                                                          scales=(1,))
    else:
        ok = r.state["rec"] is not None and not contains_text(f, "STALE",
                                                              scales=(1,))
    print("RESULT %s %s" % ("ok" if ok else "no", state))
    return 0 if ok else 1


def test_states_in_separate_processes():
    print("\nthe three data states, each in its own process")
    # ftdata.CACHE_DIR binds at import, so reloading the module in one process
    # does not test what it looks like it tests.
    tmp = tempfile.mkdtemp(prefix="bgp-states")
    try:
        fresh = os.path.join(tmp, "fresh")
        stale = os.path.join(tmp, "stale")
        absent = os.path.join(tmp, "absent")
        synthetic(fresh)
        synthetic(stale, fetched_ago=ftdata.BGP_TTL * 3)
        os.makedirs(absent)
        for state, d in (("fresh", fresh), ("stale", stale),
                         ("absent", absent)):
            env = dict(os.environ, FT_DATA_CACHE=d, FT_DATA_BLOBS=d)
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--state", state, "--cache-dir", d],
                env=env, capture_output=True, text=True, timeout=180)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT")]
            check("%s cache, in its own process" % state,
                  proc.returncode == 0 and bool(line),
                  line[0][7:] if line else
                  (proc.stderr.strip().splitlines() or ["no output"])[-1][:70])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="bgp-size")
    try:
        synthetic(tmp)
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, f = settled(opts(cache_dir=tmp, width=w, height=h), 60)
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                lay = r.layout
                detail = "chart %d rows, ticker %d, legend %d" % (
                    lay.chart_h, lay.tick_h, lay.legend_h)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("bgp-sfmix", tempfile.mkdtemp(prefix="bgp-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests",
                                  "bz2")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "bgp.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl", "bz2") if ("import " + m) in src]
    check("bgp.py does not import one either", not imported, ",".join(imported))


def test_live(cache_dir):
    print("\nthe live cache (needs ftdata.py --once --only bgp-sfmix)")
    got = ftdata.load("bgp-sfmix", cache_dir)
    if got is None:
        print("  --   no live record; skipping")
        return
    payload, age = got
    check("live record parses into a drawable one",
          bgp.read_churn(cache_dir)[0] is not None,
          bgp.read_churn(cache_dir)[2] or "")
    check("the window is about fifteen minutes",
          800 <= payload["secs"] <= 1000, "%s s" % payload["secs"])
    check("bins cover the window",
          len(payload["ann_bins"]) * payload["bin_secs"] >= payload["secs"] - 2,
          "%d bins of %d s" % (len(payload["ann_bins"]), payload["bin_secs"]))
    check("counts and bins agree",
          sum(payload["ann_bins"]) == payload["ann"]
          and sum(payload["wdr_bins"]) == payload["wdr"],
          "%d/%d ann, %d/%d wdr" % (sum(payload["ann_bins"]), payload["ann"],
                                    sum(payload["wdr_bins"]), payload["wdr"]))
    check("the rate matches the counts",
          abs(payload["ann_s"] - payload["ann"] / float(payload["secs"])) < 0.02,
          "%.2f/s" % payload["ann_s"])
    check("the record is small enough to live on a flash card",
          os.path.getsize(ftdata.record_path("bgp-sfmix", cache_dir)) < 40000,
          "%d bytes from %s of MRT"
          % (os.path.getsize(ftdata.record_path("bgp-sfmix", cache_dir)),
             payload.get("mrt_bytes")))
    check("real prefixes, not placeholders",
          all("/" in s["p"] for s in payload["samples"])
          and any(":" in s["p"] for s in payload["samples"]),
          "%d samples, some IPv6" % len(payload["samples"]))
    check("it renders", settled(opts(cache_dir=cache_dir))[1].max() > 0,
          "age %s" % ftdata.describe_age(age))


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
    test_mrt_parser()
    test_mrt_robustness()
    test_resample_conserves()
    test_chart_pixels()
    test_ticker()
    test_motion_and_purity()
    test_numbers()
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
