#!/usr/bin/env python3
"""The internet's routing table churning, as San Francisco's exchange hears it.

Fifteen minutes of the global default-free zone, second by second, drawn across
320 columns, with a ticker of the actual prefixes scrolling underneath. The
number in the corner is how many prefixes a second are being announced or
withdrawn to the RouteViews collector at SFMIX -- which is two miles from this
wall and is where the makerspace's own ISP hands its traffic off. The routes on
this panel are the ones the room's packets are steered by.

BGP never stops. Somewhere on earth a network is announcing or withdrawing a
prefix a few thousand times a second, most of it churn from a handful of
unstable origins and occasionally something that matters, and the shape of that
noise is the only thing this panel is trying to say: **a constant hiss with
structure in it.** A quarter of an hour of it is a chart with a floor of a
hundred-odd prefixes a second and spikes an order of magnitude above the floor,
and every spike is a real event -- a session that reset and re-sent its table,
a network that flapped, somebody's maintenance window.

**This is the one panel on the wall showing infrastructure its audience
operates**, and that makes the honesty load-bearing. There are five demos here
already in the green-on-black terminal register -- wardial, ansi, wopr, defcon,
sneakers -- and every one of them is a prop: invented numbers in a hacker-movie
typeface. This one deliberately borrows the same visual language and then has
to earn its way out of it. So the prefixes in the ticker are literal strings out
of the MRT dump, the AS numbers are real and lookupable, IPv6 is in there
because the real table is half IPv6, and the awkward numbers are on the screen
rather than smoothed off. Somebody who knows what 2a0a:280:2311::/48 is can
check this panel against their own looking glass and find it correct. That is
the test it is built to pass.

**Two registers, one idea.** The chart is the rate; the ticker is what the rate
is made of. They are the same fifteen minutes.

  * **The chart**, rows 6 to 29, is a stacked area over the window: withdrawals
    along the bottom in amber, announcements above them in green, so the total
    height is the churn rate and the amber sliver is the share of it that is a
    route going away. Withdrawals are about five per cent of prefix churn and
    are far more likely to be somebody's outage than an announcement is, which
    is why they get their own colour and the bottom of the stack rather than
    being folded into one line. Each column is 2.8 seconds of real time; the
    scale in the top left is the full height in prefixes a second.
  * **The ticker**, rows 36 to 63, scrolls one line a second: the mark, the
    prefix, the origin AS, and the last few hops of the AS path before it. Its
    lines are a *reservoir sample* of the whole window rather than the first
    forty-eight, because the front of a fifteen-minute window is regularly one
    peer dumping its table and forty-eight lines of the same router is not what
    the routing table looks like. Announcements and withdrawals go into the
    same reservoir at their true proportions, so most nights there is exactly
    one amber line in the loop, and that is correct.

**Why the data is a quarter of an hour old, on purpose.** RIPE's RIS Live
streams the whole DFZ over plain HTTP and would put this panel a second behind
the world. It was tried first and rejected twice over: unfiltered it delivers
78 MB in 25 seconds, which is not going near a Pi on shop wifi, and any
affordable use of it is *sampled* -- open the socket, read twenty seconds,
close it, and be blind for the other hundred and sixty. A one-minute burst
would simply not appear. RouteViews publishes the complete window instead: one
1.2 MB bzip2 file, a minute after the window closes, containing every update
the collector saw. Trading fifteen minutes of latency for a chart with nothing
missing from it is the right trade for a panel about texture, and the age is on
the screen in any case. See ftdata.py, which parses the MRT by hand.

**Nothing here touches the network.** `build()` calls `ftdata.load()`, which
reads one 11 kB JSON file. The fetcher is a separate process on a timer --
`ftsched` builds the next segment on a worker thread, Python threads share the
GIL, and a `build()` blocked on a socket stops the render loop getting the
interpreter back:

    $ python3 ftdata.py --loop 900

**Three states, all deliberate.** A fresh record is the panel above. A record
past its forty-five minute TTL still draws -- a picture of the routing table
from an hour ago is still a picture of the routing table -- with STALE and the
age in the header, because the one thing this panel must never do is imply that
a flat stretch is happening now. No record at all gets a no-data card.

**Frame budget.** Everything is baked in `build()`: the header, the chart, the
legend and the whole ticker are rasterised once into a static frame and one
tall strip. `render()` does one full-width copy of the top of the panel, one or
two slice copies of a moving window into the strip, and three short writes for
the pulse -- five or six numpy calls, and the cost model on the wall is calls
and not pixels. Measured over a full 60-second loop on the desktop this was
written on: see the README fragment. `build()` is a few milliseconds, once, on
the worker thread.

Run:  python3 ftdata.py --once --only bgp-sfmix
      python3 bgp.py --host 127.0.0.1
      FT_DATA_CACHE=/tmp/empty python3 bgp.py       # the no-data card
      python3 scripts/test-bgp.py
"""

import math
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

PRODUCT = "bgp-sfmix"

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, propagation, sort and tide
# draw with: five rows a glyph, each row an octal digit whose three bits are
# the three columns. At four pixels a character a 320-wide panel holds eighty
# of them, which is what makes a ticker line carrying a prefix, an origin and
# four hops of AS path fit at all. Nothing from a real typeface survives five
# pixels, and the Pi does not have the same faces installed as this machine.
#
# The font already carries `.`, `/` and `:` and the hex digits A-F, which
# between them is every character an IPv4 or IPv6 prefix can contain. Nothing
# has to be added for this demo, which is worth saying because the obvious
# assumption -- that a colon would be missing -- was wrong.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)

_GLYPHS = {}
for _ch, _rows in _FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((5 * scale, 1), bool)
    out = np.zeros((5, len(s) * 4 - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def text_width(s, scale=1):
    return max(1, (len(str(s)) * 4 - 1) * scale)


def blit_text(dst, y, x, s, rgb, scale=1):
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn."""
    m = text_mask(s, scale)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    dst[y0:y1, x0:x1][sub] = rgb
    return gw


# --------------------------------------------------------------------------
# Colour. The terminal register the wall's other green panels use, with one
# deliberate departure: withdrawals are amber, and they are the only warm thing
# on the panel. A route going away is the news here, and colour is how a person
# walking past finds out there was some without reading anything.
# --------------------------------------------------------------------------

C_ANN = (48, 232, 104)               # announcements: the hiss
C_ANN_EDGE = (140, 255, 170)         # the silhouette of the churn curve
C_WDR = (255, 150, 40)               # withdrawals: a route going away
C_WDR_EDGE = (255, 205, 130)
C_TEXT = (170, 255, 190)
C_DIM = (46, 118, 68)
C_DIMMER = (36, 96, 56)
C_GRID = (10, 40, 22)
C_SEP = (8, 30, 18)
C_NOW = (225, 255, 235)
C_WARN = (255, 88, 64)
C_BG = (0, 0, 0)

# How often the pulse runs up the data-edge column, in hertz. The ticker moves
# a whole pixel only six times a second, so between its steps the panel would
# otherwise hold one frame for 160 ms -- which on a wall between two animated
# demos reads as a crash rather than as a still. The pulse is the one thing
# here guaranteed to move in every single frame at twenty a second.
PULSE_HZ = 1.3


# --------------------------------------------------------------------------
# Reading what ftdata left behind. `load()` never raises, so everything that
# can still be wrong is wrong about content and is caught here.
# --------------------------------------------------------------------------

def read_churn(cache_dir):
    """(record, age, problem). `record` is None if there is nothing drawable."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached BGP record"
    payload, age = got
    try:
        ann = np.asarray(payload["ann_bins"], f32)
        wdr = np.asarray(payload["wdr_bins"], f32)
        bin_secs = float(payload["bin_secs"])
        t0, t1 = float(payload["t0"]), float(payload["t1"])
    except Exception:                                        # noqa: BLE001
        return None, age, "BGP record is malformed"
    if len(ann) < 4 or len(ann) != len(wdr) or bin_secs <= 0 or t1 <= t0:
        return None, age, "BGP record has no usable window"

    rec = {
        "ann_bins": ann, "wdr_bins": wdr, "bin_secs": bin_secs,
        "t0": t0, "t1": t1, "secs": float(payload.get("secs", t1 - t0)),
        "ann_s": float(payload.get("ann_s", 0.0)),
        "wdr_s": float(payload.get("wdr_s", 0.0)),
        "peak": int(payload.get("peak", 0)),
        "n_origins": int(payload.get("n_origins", 0)),
        "n_peers": int(payload.get("n_peers", 0)),
        "site": str(payload.get("site", "")),
        "truncated": bool(payload.get("truncated", False)),
        "samples": [s for s in (payload.get("samples") or [])
                    if isinstance(s, dict) and s.get("p")],
        "age": age,
    }
    return rec, age, None


def dedup(seq):
    """Collapse runs of the same ASN.

    AS path prepending is how a network tells the world it would rather not
    carry your traffic, and it produces paths like [16582]*9 that say one thing
    nine times. Nine identical numbers is thirty-six pixels of a ticker line
    spent on no information at all, so the run becomes one number.
    """
    out = []
    for v in seq:
        if isinstance(v, list):          # an AS_SET, drawn as its first member
            v = v[0] if v else None
        if v is None:
            continue
        if not out or out[-1] != v:
            out.append(int(v))
    return out


def ticker_line(s):
    """One ticker line as coloured segments: [(text, rgb), ...].

    Announcement:  A  1.2.3.0/24  AS64500  VIA 6939 1299 174
    Withdrawal:    W  1.2.3.0/24  WDR BY AS64500

    A withdrawal genuinely has no origin -- an UPDATE that withdraws a prefix
    carries no AS_PATH, because there is no longer a path to describe -- so the
    line names the peer that sent it instead, and says so. Inventing an origin
    for it from a previous announcement would be the exact kind of plausible
    lie this panel exists not to tell.
    """
    kind = s.get("k")
    pfx = str(s.get("p"))
    if kind == "W":
        peer = s.get("peer")
        out = [("W ", C_WDR), (pfx, C_WDR_EDGE)]
        if peer:
            out.append(("  WDR BY AS%d" % int(peer), C_WDR))
        return out
    path = dedup(s.get("path") or [])
    out = [("A ", C_ANN), (pfx, C_TEXT)]
    origin = s.get("o")
    if origin is None and path:
        origin = path[-1]
    if origin is not None:
        out.append(("  AS%d" % int(origin), C_ANN))
    via = path[-5:-1]
    if via:
        out.append(("  VIA " + " ".join(str(a) for a in via), C_DIM))
    return out


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--scroll", type=float, default=6.0,
                    help="ticker scroll speed in pixels/sec (6 = one line/sec)")
    ap.add_argument("--reveal", type=float, default=1.4,
                    help="seconds the chart takes to draw itself in (0 = off)")
    ap.add_argument("--peak", type=float, default=0.0,
                    help="chart full scale in prefixes/sec (0 = fit the window)")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--reload", type=float, default=600.0,
                    help="seconds between re-reads of the cache (0 = never)")


# --------------------------------------------------------------------------
# Layout. Four regions, and the split between chart and ticker is the whole
# design decision: the chart says how much, the ticker says what, and either
# one on its own is half a panel. What gives way on a shorter display is the
# legend row and then the ticker, because the chart is the demo.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.head_h = 6 if h >= 24 else 0
        self.legend_h = 5 if (h >= 48 and w >= 200) else 0
        # A ticker viewport shorter than two lines is a flicker, not a ticker.
        self.tick_h = 28 if h >= 60 else (18 if h >= 44 else 0)
        while self.tick_h and (h - self.head_h - self.legend_h
                               - (1 if self.tick_h else 0) - self.tick_h) < 10:
            self.tick_h -= 6
            if self.tick_h < 12:
                self.tick_h = 0
        self.chart_y = self.head_h
        used = self.head_h + self.legend_h + self.tick_h + (1 if self.tick_h else 0)
        self.chart_h = max(2, h - used)
        self.legend_y = self.chart_y + self.chart_h
        self.tick_y = h - self.tick_h if self.tick_h else h

    @property
    def chart_bot(self):
        return self.chart_y + self.chart_h - 1


# --------------------------------------------------------------------------
# Baking the picture.
# --------------------------------------------------------------------------

def column_rates(bins, bin_secs, w):
    """A per-bin count series onto `w` panel columns, as a rate per second.

    Via the cumulative sum rather than a per-column loop: the difference of the
    interpolated cumulative between two column edges is exactly the count that
    fell in the column, whether the column spans one bin or three, and it does
    not care that 450 bins over 320 columns is not a whole number. Getting this
    wrong in the obvious way -- nearest-bin sampling -- would drop a third of
    the bins on the floor, and the ones it dropped would preferentially be the
    single-bin spikes that are the entire point of the chart.
    """
    n = len(bins)
    cum = np.concatenate(([0.0], np.cumsum(bins, dtype=np.float64)))
    edges = np.arange(w + 1, dtype=np.float64) * (float(n) / w)
    at = np.interp(edges, np.arange(n + 1, dtype=np.float64), cum)
    width = np.diff(edges) * bin_secs
    return (np.diff(at) / np.maximum(width, 1e-9)).astype(f32)


def nice_scale(top):
    """Round a full scale up onto a 1/1.5/2/3/5/7 ladder.

    The axis has printed numbers on it, so they should be ones a person can
    divide by in their head. It also stops the chart rescaling itself by four
    per cent every quarter of an hour, which would make the same quiet window
    look different every time it was redrawn.

    The ladder has 1.5, 3 and 7 on it as well as the usual 1, 2 and 5, and that
    is not decoration. A 1/2/5 ladder rounds a 2760/s peak up to 5000, and on
    the square-root axis that leaves the tallest spike in the window at
    sqrt(2760/5000) -- three quarters of the height, with a quarter of the
    chart permanently empty above the biggest thing that happened. Halving the
    gaps in the ladder costs nothing anybody has to read differently and gets
    that back.
    """
    if top <= 0:
        return 10.0
    mag = 10.0 ** math.floor(math.log10(top))
    for step in (1.0, 1.5, 2.0, 3.0, 5.0, 7.0, 10.0):
        if top <= step * mag:
            return step * mag
    return 10.0 * mag


def bar_height(v, scale, rows):
    """Value to bar height, on a square-root axis. Vectorised.

    **The axis is not linear, and it has to not be.** BGP churn is a floor of
    a hundred-odd prefixes a second with spikes twenty times that, and a linear
    axis fitted to the spikes draws the floor -- which is the panel's whole
    subject, the constant hiss -- as one row of green along the bottom with
    nothing readable in it. Fitting the axis to the floor instead just clips
    every spike flat, and the spikes are the events.

    Square root splits the difference in the way that suits this data: the
    floor lands around a third of the height with its texture intact, and a
    twentyfold spike still reaches the top. Log would compress harder and is
    the usual answer for rates, but a stacked area cannot be drawn on a log
    axis -- a zero has nowhere to go, and half these columns have no
    withdrawals in them at all.

    A non-linear axis that does not say so is a lie, so it says so: there are
    two printed numbers on it, the full scale and the value at half height, and
    under this transform the half-height number is a *quarter* of the full
    scale. Anyone who reads both discovers the axis in one second, which is
    exactly the audience this panel is for.
    """
    return np.sqrt(np.clip(v, 0.0, None) / scale) * rows


def hhmm(epoch, ampm=True):
    """A compact local-time label: '655P' or '18:55'."""
    lt = time.localtime(epoch)
    if not ampm:
        return time.strftime("%H:%M", lt)
    h = lt.tm_hour % 12 or 12
    return "%d:%02d%s" % (h, lt.tm_min, "A" if lt.tm_hour < 12 else "P")


def draw_chart(dst, lay, rec, scale, ann_c, wdr_c):
    """The stacked area: withdrawals along the bottom, announcements above.

    Two boolean masks over the chart region rather than a column loop. Three
    hundred and twenty columns of Python is a tenth of a second on the wall's
    Pi even once, and this runs again every time the cache is re-read.

    Withdrawals are underneath because they are the smaller number and a
    two-pixel band floating on top of a moving surface cannot be read; sitting
    on the floor it has a straight edge to be measured against. Their band is
    given a one-row minimum wherever there were any at all, so that a column
    with real withdrawals in it is never rounded away to nothing -- an outage
    that vanishes because it was small is the failure mode that matters here.
    """
    top, bot = lay.chart_y, lay.chart_bot
    nrows = bot - top + 1
    reg = dst[top:bot + 1]
    # Everything below is in "height above the floor": a bar of height H fills
    # the rows with yy >= nrows - H. Expressing it that way rather than as a
    # pair of top edges is what makes the two bands meet exactly, with no
    # off-by-one seam and no special case for a column with no withdrawals.
    yy = np.arange(nrows)[:, None]
    floor = float(nrows)

    # The two boundaries are each the transform of a *cumulative* value, never
    # a sum of two transformed heights: under a non-linear axis those are
    # different numbers, and only the first one puts each boundary where the
    # axis says it belongs.
    tot_h = np.clip(bar_height(wdr_c + ann_c, scale, floor), 0.0, floor)
    wdr_h = np.clip(bar_height(wdr_c, scale, floor), 0.0, floor)
    # A one-row minimum wherever there were any withdrawals at all. An outage
    # that vanishes from the chart because it was small is precisely the
    # failure this panel must not have.
    wdr_h = np.where(wdr_c > 0, np.maximum(wdr_h, 1.0), 0.0)
    wdr_h = np.minimum(wdr_h, tot_h)

    reg[yy >= (floor - tot_h)[None, :]] = C_ANN
    reg[yy >= (floor - wdr_h)[None, :]] = C_WDR

    # The silhouette. A stacked area whose top edge is the same value as its
    # fill has no outline, and the outline is the churn curve -- the thing the
    # eye actually follows across the panel.
    cols = np.arange(lay.w)
    lit = tot_h > 0
    edge = np.clip(np.ceil(floor - tot_h), 0, nrows - 1).astype(int)
    reg[edge[lit], cols[lit]] = np.where(
        (wdr_h[lit] >= tot_h[lit])[:, None],
        np.array(C_WDR_EDGE, np.uint8), np.array(C_ANN_EDGE, np.uint8))


def draw_furniture(dst, lay, rec, scale, h24):
    """Gridlines, the scale label, the legend row and the separators."""
    top, bot = lay.chart_y, lay.chart_bot
    w = lay.w

    # A rule at every quarter of the window, so the eye can place a spike in
    # time without a printed axis. Drawn under the stack, which is why this
    # runs first.
    for k in range(1, 4):
        c = int(k * w / 4.0)
        if 0 <= c < w:
            dst[top:bot + 1, c] = C_GRID
    # Horizontal rules at half and quarter height. On the square-root axis
    # those are a quarter and a sixteenth of full scale, and the half-height
    # one is labelled -- see bar_height() on why the panel has to admit that.
    for frac in (0.5, 0.25):
        r = int(round(bot - frac * (bot - top)))
        if top < r <= bot:
            dst[r] = np.maximum(dst[r], np.array(C_GRID, np.uint8))

    if lay.head_h:
        dst[lay.head_h - 1] = C_SEP

    if lay.legend_h:
        y = lay.legend_y
        blit_text(dst, y, 1, "-%dM" % int(round(rec["secs"] / 60.0)), C_DIM)
        x = 34
        for label, rgb in (("ANN", C_ANN), ("WDR", C_WDR)):
            dst[y:y + 5, x:x + 3] = rgb
            blit_text(dst, y, x + 5, label, C_DIM)
            x += 5 + text_width(label) + 7
        # Context with meaning, right-aligned: how many distinct networks
        # originated the churn, and how many peers the collector heard it from.
        # "1568 origins" is the difference between one flapping router and the
        # whole table having a bad day, and it costs twenty characters.
        # ...and, at the right-hand end of the axis, the wall-clock time the
        # window closed. That is the calibration the header's age is relative
        # to, and it is the difference between "the table was quiet" and "the
        # table was quiet an hour ago", which are not the same claim.
        stamp = hhmm(rec["t1"], not h24)
        for tag in ("%s  %d ORIGINS  %d PEERS"
                    % (stamp, rec["n_origins"], rec["n_peers"]),
                    "%s  %d AS" % (stamp, rec["n_origins"]),
                    stamp):
            tw = text_width(tag)
            if x + 6 + tw <= w - 1:
                blit_text(dst, y, w - tw - 1, tag, C_DIMMER)
                break

    if lay.tick_h:
        dst[lay.tick_y - 1] = C_SEP

    # The y axis, such as it is: two numbers down the left-hand edge, the full
    # height and the half height. Inside the chart rather than beside it,
    # because five columns of margin is five columns of chart -- and they are
    # at the left because that is the oldest end of the window and therefore
    # the end least likely to have this quarter-hour's spike in it.
    blit_text(dst, top + 1, 1, "%d/S" % int(round(scale)), C_DIMMER)
    half = int(round(bot - 0.5 * (bot - top)))
    if half - 5 > top + 6:
        blit_text(dst, half - 5, 1, "%d" % int(round(scale / 4.0)), C_DIMMER)


# --------------------------------------------------------------------------
# The header, in the ladder-of-shorter-forms shape caiso and tide use: the
# widest set that fits is the one drawn, because clipping the line loses
# whatever falls off the end, and what falls off the end of this one is the
# part that says how old the data is.
# --------------------------------------------------------------------------

def header_text(rec, stale, w=ds.WIDTH):
    if rec is None:
        return "NO BGP DATA", "", C_WARN, ""

    rate = rec["ann_s"] + rec["wdr_s"]
    lefts = ["%d PFX/S" % int(round(rate)), "%d/S" % int(round(rate))]
    mids = ["PK %d/S" % rec["peak"], "PK %d" % rec["peak"], ""]

    age = ftdata.describe_age(rec["age"])
    site = (rec["site"] or "").split()[0] if rec["site"] else "BGP"
    rights = ["%s %s" % (site, age), age, ""]
    if stale:
        rights = ["STALE " + r if r else "STALE" for r in rights]

    gap = 5
    for left in lefts:
        for right in rights:
            for mid in mids:
                need = text_width(left) + text_width(right) + 2
                if mid:
                    need += text_width(mid) + 2 * gap
                if need <= w:
                    return left, mid, C_DIM, right
    return lefts[-1], "", C_DIM, ""


def draw_header(dst, lay, rec, stale):
    left, mid, midc, right = header_text(rec, stale, lay.w)
    dst[:lay.head_h] = 0
    blit_text(dst, 0, 1, left, C_TEXT)
    rw = text_width(right) if right else 0
    if right:
        blit_text(dst, 0, lay.w - rw - 1, right, C_WARN if stale else C_DIM)
    if mid:
        mw = text_width(mid)
        mx = min(lay.w - rw - 4 - mw,
                 max(text_width(left) + 5, (lay.w - mw) // 2))
        blit_text(dst, 0, mx, mid, midc)
    dst[lay.head_h - 1] = C_SEP


def bake_ticker(rec, w, tick_h, line_h=6):
    """Rasterise every sample line once into one tall strip.

    The strip is what makes the scroll cost two slice copies a frame instead of
    a re-render, and it is padded up so that it is always taller than the
    viewport -- with four samples and a 28-row window, a strip 24 rows tall
    could not be indexed with a wrap at all. Padding by repeating the lines is
    honest here in a way it would not be for the chart: the ticker is already a
    sample of the window and it already loops.
    """
    lines = rec["samples"]
    if not lines:
        return None
    n = len(lines)
    # At least one viewport of slack past the wrap point, so `strip[o:o+h]`
    # with o in [0, n*line_h) is always a contiguous read from a real row.
    reps = max(1, -(-(tick_h + line_h) // (n * line_h)))
    strip = np.zeros((n * line_h * (1 + reps), w, 3), np.uint8)
    for i, s in enumerate(lines):
        x = 1
        for text, rgb in ticker_line(s):
            if x >= w:
                break
            x += blit_text(strip, i * line_h, x, text, rgb)
    # Repeat the block rather than re-blitting it: same pixels, no more text.
    block = strip[:n * line_h]
    for r in range(reps):
        y = (r + 1) * n * line_h
        strip[y:y + n * line_h] = block
    return strip


def draw_nodata(dst, lay, lines):
    """The honest panel. No chart, no ticker, no implied routing table."""
    dst[:] = (4, 8, 5)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.h // 2 - (len(lines) * (6 * scale + 2)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        blit_text(dst, y, x, s, rgb, sc)
        y += 5 * sc + 3
    return dst


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

LINE_H = 6


def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h)
    cache = args.cache_dir

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)   # header + chart + legend, baked
    base = np.zeros((h, w, 3), np.uint8)     # the same with no chart on it

    cell = {"rec": None, "problem": None, "loaded": -1e18, "stale": False,
            "strip": None, "strip_h": 0, "scale": 100.0}

    def reload_data(now):
        rec, age, problem = read_churn(cache)
        cell["rec"], cell["problem"], cell["loaded"] = rec, problem, now
        if rec is None:
            cell["stale"] = False
            cell["strip"] = None
            return

        cell["stale"] = not ftdata.is_fresh(PRODUCT, age)
        ann_c = column_rates(rec["ann_bins"], rec["bin_secs"], w)
        wdr_c = column_rates(rec["wdr_bins"], rec["bin_secs"], w)
        scale = (args.peak if args.peak > 0
                 else nice_scale(float(np.max(ann_c + wdr_c))))
        cell["scale"] = scale

        base[:] = 0
        draw_furniture(base, lay, rec, scale, args.h24)
        if lay.head_h:
            draw_header(base, lay, rec, cell["stale"])
        static[:] = base
        draw_chart(static, lay, rec, scale, ann_c, wdr_c)
        # The furniture the chart must not bury: the scale label and the
        # separators are re-drawn on top, because a busy window fills the
        # top-left corner with solid green and the one number calibrating the
        # axis would be inside it.
        # The axis numbers again, on top of the stack. A busy window fills the
        # left-hand end of the chart with solid green and the two numbers that
        # calibrate the whole picture would be buried inside it.
        blit_text(static, lay.chart_y + 1, 1, "%d/S" % int(round(scale)), C_NOW)
        _half = int(round(lay.chart_bot - 0.5 * (lay.chart_bot - lay.chart_y)))
        if _half - 5 > lay.chart_y + 6:
            blit_text(static, _half - 5, 1, "%d" % int(round(scale / 4.0)), C_DIM)
        if lay.tick_h:
            static[lay.tick_y - 1] = C_SEP

        strip = bake_ticker(rec, w, lay.tick_h, LINE_H) if lay.tick_h else None
        if lay.tick_h and strip is None:
            # A record with a chart but no sample lines in it. Rare -- it means
            # the fetch found churn but the reservoir came back empty -- and the
            # wrong answer is a third of the panel going quietly black, which
            # reads as the demo having crashed halfway down. Say what happened
            # instead; the chart above it is still true.
            blit_text(static, lay.tick_y + 3, 1,
                      "NO SAMPLE LINES IN THIS WINDOW", C_DIM)
        cell["strip"] = strip
        # The wrap point is the height of *one* pass over the samples, not the
        # height of the padded strip: scrolling to the end of the padding and
        # jumping back would show the same lines twice and then skip.
        cell["strip_h"] = len(rec["samples"]) * LINE_H if strip is not None else 0

    def render(t, i):
        now = time.time()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        if cell["rec"] is None:
            lines = [("NO BGP DATA", C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --LOOP 900", C_TEXT)]
            if cell["problem"]:
                lines.append((str(cell["problem"]).upper()[:52], C_DIM))
            return draw_nodata(frame, lay, lines)

        # Only the top of the panel is copied: every row of the ticker viewport
        # is overwritten below, so copying it here would be a whole second
        # pass over a third of the frame for nothing. Unless there is no strip
        # to overwrite it with, in which case the whole static frame -- which
        # carries the explanation baked into that region -- is what goes out.
        scrolling = lay.tick_h and cell["strip"] is not None
        top_h = lay.tick_y if scrolling else h
        frame[:top_h] = static[:top_h]

        # The reveal, which is the chart being drawn left to right when the
        # segment starts. Two slice copies rather than a mask: the region that
        # has not arrived yet is restored from `base`, which already has the
        # gridlines on it, so the chart wipes in over a grid rather than over a
        # black hole.
        edge = w
        if args.reveal > 0 and t < args.reveal:
            edge = int(w * (t / args.reveal))
            frame[lay.chart_y:lay.chart_y + lay.chart_h, edge:] = \
                base[lay.chart_y:lay.chart_y + lay.chart_h, edge:]
            if edge < w:
                frame[lay.chart_y:lay.chart_y + lay.chart_h, edge] = C_NOW

        # The ticker: a moving window into the baked strip, wrapping. Driven by
        # the segment's own `t` and never by the wall clock, which is what makes
        # it the same animation on the wall and under a test harness rendering
        # a hundred frames in a millisecond.
        if scrolling:
            o = int(t * args.scroll) % max(1, cell["strip_h"])
            frame[lay.tick_y:] = cell["strip"][o:o + lay.tick_h]

        # The data edge, last, over everything. The right-hand column is the
        # end of the fifteen-minute window -- which is NOT now, and the header's
        # age is the difference. A short bright pulse runs up it, and it is the
        # only thing on this panel that moves in every frame: the ticker steps a
        # whole pixel six times a second and holds still in between.
        if edge >= w:
            col = w - 1
            top, bot = lay.chart_y, lay.chart_y + lay.chart_h
            blink = 0.5 + 0.5 * math.sin(t * 2.2)
            frame[top:bot, col] = tuple(int(c * blink) for c in C_NOW)
            py = top + int(((t * PULSE_HZ) % 1.0) * (bot - top))
            frame[max(top, py - 1):py + 2, col] = C_NOW
        return frame

    reload_data(time.time())
    render.state = cell               # tests reach in here; nothing else does
    render.layout = lay
    render.static = static
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "BGP churn at SFMIX: the routing table, second by second",
                  fps=20)


if __name__ == "__main__":
    main()
