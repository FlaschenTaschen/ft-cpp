#!/usr/bin/env python3
"""Six hours of raw ground motion from a seismometer ten miles away.

`quake.py` draws earthquakes after the fact: located, magnitudes assigned,
plotted as dots on a map by a pipeline that has already decided what was an
earthquake. This panel is the other end of the same pipeline -- one
seismometer, one channel, the ground going up and down, before anybody has
decided anything about it.

The form is the drum recorder, which is one of the most recognisable
scientific pictures there is and which happens to fit a 5:1 letterbox exactly:
six stacked traces, an hour each, oldest at the top, newest at the bottom, the
pen at the leading edge. On paper the drum turns once an hour and creeps
sideways so that a day comes out as a stack of hour-long lines; this is that,
at 320 by 64.

**The quiet is the data.** Most of the time this is a flat line with a fuzz on
it, and the fuzz is not noise in the instrument -- it is the microseism, the
whole Pacific coast ringing at five to eight seconds from swell hitting the
continental shelf, which is a real thing about the actual planet that you can
watch get louder when there is weather offshore. The panel is scaled so that
this background fills about two fifths of a trace lane -- enough that it is
visibly ragged and not a drawn line, with room above it for something to
happen. Then, occasionally, something does, and the trace runs off the top of
its lane into its neighbours.

**The clipping is deliberate.** A real helicorder overwrites the line above it
when the ground moves harder than the paper has room for, and every seismology
department in the world has a framed one on the wall where the traces have
scribbled over each other. Rescaling to fit would be worse in every way: it
would flatten the background to a hairline on the rare days it mattered, and
it would make a magnitude 5 look exactly like a magnitude 2 with a different
axis. So the scale is fixed against the *background* level and an event is
allowed to overrun -- into a warmer colour outside its own lane, capped at one
and a half lanes so that one big local quake cannot black out the whole panel.
That cap is reached often: the M5.6 in the README screenshot is five hundred
times the background, which is two hundred and sixty lanes of trace asking for
twenty rows of panel. Any local M5 saturates this, and that is the correct
thing for it to do.

**Where the numbers come from.** BK.BRK, Byerly Vault, an STS-2 broadband
seismometer under the UC Berkeley campus, 17 km north-east of the wall, on
Berkeley's own network and served by NCEDC. ftdata.py fetches it as miniSEED,
decodes Steim2, reduces each 12 seconds to a minimum and a maximum -- which is
literally what a pen does -- and stores 1800 of those pairs, one per panel
column. The demo does no network I/O and no decompression; it reads one 22 kB
JSON file.

**The events are borrowed, not refetched.** If `quake-usgs` is in the cache,
any located earthquake within 300 km that falls in the window is marked on the
trace, offset from its origin time by the P-wave travel time so the mark lands
where the wiggle actually starts rather than where the earthquake started.
That record belongs to `quake.py`; this panel reads it and degrades to no
marks at all if it is missing. The largest one in the window gets its
magnitude and place in the header.

**Vertical scale is stated in real units.** The response is in the record --
2.53e9 counts per metre per second for this vault, and it has changed eight
times since 1996, so the epoch covering the data is the one used -- and the
axis strip says what one full lane is worth in microns per second peak to
peak. A wiggle nobody can put a number on is decoration.

**Motion.** The drum draws itself in reading order when the segment starts,
line by line, which is six hours replayed in a couple of seconds and is the
one animation this subject actually asks for. After that a slow sheen crosses
the paper, and the pen at the leading edge pulses. All three are functions of
the segment's own `t`, so a preview baker and the wall see the same thing.

**Frame budget.** Everything is baked in `build()`: traces, gridlines, hour
labels, quake marks, the header. `render()` copies one frame, does either two
slice restores (during the reveal) or one multiply-and-add over a 40 column
window (the sheen), and writes the pen. Six to eight numpy calls, and numpy on
the wall's Pi costs tens of microseconds a call whatever the array size, so
the call count is the budget. Measured here over a full loop: see the README.

Run:  python3 ftdata.py --once --only helicorder-bk
      python3 helicorder.py --host 127.0.0.1
      FT_DATA_CACHE=/tmp/empty python3 helicorder.py      # the no-data card
      python3 scripts/test-helicorder.py
"""

import math
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

PRODUCT = "helicorder-bk"
QUAKE_PRODUCT = "quake-usgs"

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, as caiso, tide and propagation use it: five rows
# a glyph, each row an octal digit whose three bits are the three columns.
# Measured rather than assumed -- text_mask() builds the mask and everything
# else asks it how wide the result is.
# --------------------------------------------------------------------------

_GLYPHS = {}
for _ch, _rows in defcon._FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g

GLYPH_H = 5


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((GLYPH_H * scale, 1), bool)
    out = np.zeros((GLYPH_H, len(s) * 4 - 1), bool)
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
# Colour. Two trace colours alternating down the drum, which is not decoration:
# when a trace clips into the lane above it, the only thing that says which
# line the scribble belongs to is that it is a different colour from the line
# it is scribbling over. Real multi-colour drum plots do this for exactly that
# reason.
# --------------------------------------------------------------------------

C_TRACE_A = (86, 200, 176)           # cool green, even lanes
C_TRACE_B = (150, 162, 236)          # periwinkle, odd lanes
C_CLIP = (198, 68, 52)               # the part that has left its own lane
C_PAPER = (13, 16, 21)               # the lane's own zero line: chart paper
C_GRID = (20, 25, 32)                # ten-minute rules
C_GAP = (72, 26, 26)                 # no data in this column
C_TEXT = (198, 210, 222)
C_DIM = (84, 96, 110)
C_LABEL = (120, 134, 150)
C_SEP = (16, 20, 26)
C_NOW = (255, 246, 214)
C_NOW_DIM = (80, 74, 56)
C_QUAKE = (96, 26, 22)               # the bar behind the trace
C_QUAKE_HI = (255, 138, 64)          # its cap, above and below the lane
C_WARN = (255, 96, 72)

# How often the pen pulses, in hertz.
PULSE_HZ = 1.1

# Which located events get a mark: magnitude against distance, not magnitude
# alone. The USGS catalogue is complete to about M1 in the Bay Area and a
# quiet week has a couple of hundred events inside 300 km, nearly all of them
# far too small to have reached this vault -- an M2.5 at Willits is 190 km of
# rock away and is not on this trace at any scale. Marking it would be the
# panel claiming something the picture does not show, which is the one thing a
# raw-data panel must not do. A magnitude an even hundred kilometres is close
# to a single-station detection threshold and is deliberately conservative:
# every mark on this drum has a wiggle under it.
MARK_MAG0 = 2.0
MARK_MAG_PER_KM = 0.01

# Kilometres a second for the P wave through the crust. Used only to slide a
# mark from origin time to the moment the ground here started moving, which at
# 200 km is thirty seconds -- two and a bit columns. Not a travel-time model:
# a straight line at a plausible speed, which is the right precision for a
# 12-second column.
P_KM_S = 6.1


# --------------------------------------------------------------------------
# Layout. Five rows of header, five of minute axis, and the rest is drum:
# six lanes of nine rows, which is the least a trace can be and still have a
# shape in it. The gutter is wide enough for '11P'.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h, lanes):
        self.w, self.h = w, h
        self.head_y, self.head_h = 0, GLYPH_H if h >= 40 else 0
        self.axis_y = self.head_h
        self.axis_h = GLYPH_H if h >= 50 else 0
        self.chart_y = self.axis_y + self.axis_h
        self.lanes = max(1, min(lanes, (h - self.chart_y) // 6))
        self.lane_h = (h - self.chart_y) // self.lanes
        self.chart_h = self.lane_h * self.lanes
        # Anything left over from the division goes back to the axis strip, so
        # the drum stays bottom-aligned and the lanes stay equal.
        self.chart_y = h - self.chart_h
        self.gutter = 15 if w >= 200 else 0
        self.x0 = self.gutter
        self.x1 = w - 5
        self.trace_w = self.x1 - self.x0

    def lane_y(self, i):
        return self.chart_y + i * self.lane_h

    def lane_mid(self, i):
        return self.lane_y(i) + self.lane_h // 2


# --------------------------------------------------------------------------
# Reading what ftdata left behind. Three states have to be drawable: a good
# record, a stale one, and nothing at all. Only the third stops the drum.
# --------------------------------------------------------------------------

def read_drum(cache_dir):
    """(record, age, problem). `record` is None if there is nothing to draw."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached seismogram"
    payload, age = got
    try:
        t0 = float(payload["t0"])
        bin_s = float(payload["bin_s"])
        cols = int(payload["cols"])
        trace_cols = int(payload["trace_cols"])
        lo_raw, hi_raw = payload["lo"], payload["hi"]
        if len(lo_raw) != cols or len(hi_raw) != cols:
            return None, age, "seismogram record is the wrong length"
        have = np.array([v is not None for v in lo_raw], bool)
        lo = np.array([0.0 if v is None else float(v) for v in lo_raw], f32)
        hi = np.array([0.0 if v is None else float(v) for v in hi_raw], f32)
        station = dict(payload.get("station") or {})
        filled_to = float(payload.get("filled_to", t0))
    except Exception:                                        # noqa: BLE001
        return None, age, "seismogram record is malformed"

    if not have.any() or bin_s <= 0 or trace_cols <= 0:
        return None, age, "seismogram record has no samples in it"

    # Counts per metre per second. Absent means the units line cannot be
    # drawn -- which is a missing label, not a missing picture.
    scale = float(station.get("scale") or 0.0)
    noise = float(payload.get("noise") or 0.0)
    if noise <= 0:
        noise = float(np.median((hi - lo)[have]))
    return {
        "t0": t0, "bin_s": bin_s, "cols": cols, "trace_cols": trace_cols,
        "lanes": max(1, cols // trace_cols),
        "lo": lo, "hi": hi, "have": have, "age": age,
        "filled_to": filled_to,
        "n_filled": int(np.clip(round((filled_to - t0) / bin_s), 0, cols)),
        "station": station, "counts_per_ms": scale,
        "noise": max(1.0, noise), "peak": float(payload.get("peak") or 0.0),
        "km": float(payload.get("km") or 0.0),
        "bearing": float(payload.get("bearing") or 0.0),
    }, age, None


def compass(bearing):
    """Eight points is as fine as a three-character label can be honest to."""
    return ("N", "NE", "E", "SE", "S", "SW", "W",
            "NW")[int((bearing % 360.0) / 45.0 + 0.5) % 8]


def micron_s(counts, rec):
    """Counts to microns per second, or None if the response is unknown."""
    if not rec["counts_per_ms"]:
        return None
    return counts / rec["counts_per_ms"] * 1e6


def read_quakes(rec, cache_dir):
    """Located events inside the window, from quake.py's record. Never raises.

    This deliberately does not fetch anything and does not touch that record:
    it is somebody else's product and it is already in the cache. A missing or
    changed one costs the marks and nothing else.
    """
    out = []
    try:
        got = ftdata.load(QUAKE_PRODUCT, cache_dir)
        if got is None:
            return out
        events = ((got[0] or {}).get("local") or {}).get("events") or []
        t_end = rec["t0"] + rec["cols"] * rec["bin_s"]
        for ev in events:
            mag = ev.get("mag")
            t = ev.get("t")
            if mag is None or t is None:
                continue
            km = float(ev.get("km") or 0.0)
            if float(mag) < MARK_MAG0 + MARK_MAG_PER_KM * km:
                continue
            arrival = float(t) + km / P_KM_S
            if not (rec["t0"] <= arrival < t_end):
                continue
            out.append({"t": arrival, "mag": float(mag), "km": km,
                        "place": str(ev.get("place") or ""),
                        "bearing": float(ev.get("bearing") or 0.0)})
        out.sort(key=lambda e: e["mag"], reverse=True)
    except Exception:                                        # noqa: BLE001
        return []
    return out


def place_words(place):
    """'11 km N of Redwood Valley, CA' -> 'REDWOOD VALLEY'.

    The feed's own leading distance is dropped: this panel has already stated
    a distance, from the wall, and two different ones on one line is worse
    than none.
    """
    s = str(place or "")
    if " of " in s:
        s = s.split(" of ", 1)[1]
    s = s.split(",")[0]
    return "".join(c for c in s.upper() if c.isalnum() or c in " -.").strip()


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--gain", type=float, default=2.5,
                    help="lane heights per peak-to-peak of background noise; "
                         "higher makes a quiet day fill less of its lane")
    ap.add_argument("--clip-lanes", type=float, default=1.5,
                    help="how far outside its own lane a trace may run")
    ap.add_argument("--reveal", type=float, default=2.6,
                    help="seconds the drum takes to draw itself in (0 = off)")
    ap.add_argument("--sweep", type=float, default=7.0,
                    help="seconds between sheen passes (0 = off)")
    ap.add_argument("--sweep-width", type=int, default=40,
                    help="columns the sheen is wide")
    ap.add_argument("--sweep-gain", type=float, default=0.5,
                    help="how far towards white the sheen lifts what it crosses")
    ap.add_argument("--no-quakes", action="store_true",
                    help="do not mark located events from the quake record")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--reload", type=float, default=300.0,
                    help="seconds between re-reads of the cache (0 = never)")


# --------------------------------------------------------------------------
# Baking the picture. All of this happens once per cache read.
# --------------------------------------------------------------------------

def hour_label(epoch, h24):
    lt = time.localtime(epoch)
    if h24:
        return "%02d" % lt.tm_hour
    h = lt.tm_hour % 12 or 12
    return "%d%s" % (h, "A" if lt.tm_hour < 12 else "P")


def column_pos(lay, rec, col):
    """(lane, x) for a column of the record, clamped onto the panel.

    Clamped because the layout is allowed to have fewer lanes than the record
    has hours -- a 32 row panel fits five, not six -- and the arithmetic that
    is exact on the wall then runs off the right-hand edge. It did, on the
    first 320x32 render.
    """
    per_lane = rec["trace_cols"]
    lane = min(lay.lanes - 1, max(0, col) // per_lane)
    c = min(per_lane - 1, max(0, col) - lane * per_lane)
    return lane, min(lay.x1 - 1, lay.x0 + (c * lay.trace_w) // per_lane)


def draw_paper(dst, lay, rec, h24, full_scale):
    """The chart paper: ten-minute rules, hour labels, minute ticks, scale.

    Drawn before the traces and never over them, so the picture reads as ink
    on ruled paper rather than as a plot with a grid on top of it.
    """
    w = lay.w
    per_lane = rec["trace_cols"]
    px = lay.trace_w / float(per_lane)          # pixels per column

    # Ten-minute rules straight down the drum. On paper these are printed and
    # the pen goes over them; same here.
    for minute in range(0, 60, 10):
        x = lay.x0 + int(round(minute / 60.0 * lay.trace_w))
        if lay.x0 <= x < lay.x1:
            dst[lay.chart_y:lay.chart_y + lay.chart_h, x] = C_GRID

    # Each lane's own zero line, dotted. It is what a gap in the data is
    # visible against, and it keeps an empty future lane from being a hole.
    for i in range(lay.lanes):
        y = lay.lane_mid(i)
        dst[y, lay.x0:lay.x1:3] = C_PAPER

    if lay.gutter:
        for i in range(lay.lanes):
            t = rec["t0"] + i * per_lane * rec["bin_s"]
            s = hour_label(t, h24)
            tw = text_width(s)
            blit_text(dst, lay.lane_mid(i) - GLYPH_H // 2,
                      max(0, lay.gutter - 2 - tw), s, C_LABEL)

    if not lay.axis_h:
        return
    y = lay.axis_y
    dst[max(0, y - 1)] = C_SEP
    # Minutes past the hour, at twenty-minute intervals. Every ten would
    # collide with itself at this width; the rules through the drum are every
    # ten and the two numbers say which is which. The colon is what makes them
    # read as minutes rather than as some other quantity entirely.
    for minute in (20, 40):
        x = lay.x0 + int(round(minute / 60.0 * lay.trace_w))
        blit_text(dst, y, x + 2, ":%02d" % minute, C_DIM)

    # What one lane is worth, in the units the sensor is calibrated in, in the
    # gutter above the hour labels where it belongs. The whole picture is
    # amplitude, so an unlabelled amplitude is the one thing this panel must
    # not ship. Left rather than right because the age sits at the right of
    # the header directly above, and two right-aligned strings on adjacent
    # rows read as one wrapped line.
    um = micron_s(full_scale, rec)
    if um is not None:
        s = "LANE %s UM/S" % (("%.2f" % um) if um < 10 else ("%.0f" % um))
        if text_width(s) < lay.x0 + int(round(20 / 60.0 * lay.trace_w)):
            blit_text(dst, y, 1, s, C_DIM)


def draw_traces(dst, lay, rec, rows_per_count, clip_rows, n_cols):
    """The ink. One vertical run per column, from that column's min to its max.

    A mask per lane over the whole chart region rather than a column loop:
    nine hundred columns of Python costs more than the whole rest of build().
    The run is drawn twice -- once in the clip colour over its full extent,
    then again in the lane's own colour clipped to the lane -- so the part
    that has left its lane is a different colour without a second pass over
    the geometry.
    """
    for i in range(lay.lanes):
        if not draw_lane(dst, lay, rec, i, rows_per_count, clip_rows, n_cols):
            break


def draw_lane(dst, lay, rec, i, rows_per_count, clip_rows, n_cols):
    """One hour of ink. False if this lane has no data in it yet.

    Split out from draw_traces() because the reveal needs the drum in the
    states it passes through -- see build(): a lane's ink can overrun into the
    lane below it, which has not been written yet, so "the drum as it was
    after line three" is not something that can be reconstructed by masking
    the finished picture.
    """
    per_lane = rec["trace_cols"]
    top, bot = lay.chart_y, lay.chart_y + lay.chart_h
    reg = dst[top:bot]
    rows = lay.chart_h
    yy = np.arange(rows)[:, None]

    # Column left edges in panel pixels, and the width each one is drawn.
    xs = lay.x0 + (np.arange(per_lane) * lay.trace_w) // per_lane
    xe = lay.x0 + ((np.arange(per_lane) + 1) * lay.trace_w) // per_lane
    width = int(max(1, np.min(xe - xs)))

    a = i * per_lane
    b = min(a + per_lane, rec["cols"], max(0, n_cols))
    if b <= a:
        return False
    m = rec["have"][a:b]
    mid = lay.lane_mid(i) - top
    r_hi = mid - rec["hi"][a:b] * rows_per_count
    r_lo = mid - rec["lo"][a:b] * rows_per_count
    r_hi = np.clip(r_hi, mid - clip_rows, mid + clip_rows)
    r_lo = np.clip(r_lo, mid - clip_rows, mid + clip_rows)
    # Rounded, not floor-and-ceil. Widening the run outward at both ends adds
    # up to a whole row to every column, which on a background that is two and
    # a bit rows tall is a fifty per cent fatter trace -- and a fat flat
    # background is exactly the look this panel is trying not to have. A
    # column whose two ends round to the same row still draws one row, because
    # `r_hi <= yy <= r_lo` is inclusive at both ends.
    r_hi = np.clip(np.round(r_hi), 0, rows - 1)
    r_lo = np.clip(np.round(r_lo), 0, rows - 1)

    # One boolean over the whole chart region rather than a column loop:
    # eighteen hundred columns of Python costs more than the rest of build()
    # put together. `span` is the whole run from a column's minimum to its
    # maximum, `own` the part still inside this lane; painting the first in
    # the clip colour and the second over it in the lane's own colour gives
    # the overrun a different colour for one extra assignment.
    span = (yy >= r_hi[None, :]) & (yy <= r_lo[None, :]) & m[None, :]
    inside = ((yy >= lay.lane_y(i) - top)
              & (yy < lay.lane_y(i) + lay.lane_h - top))
    own = span & inside
    colour = C_TRACE_A if i % 2 == 0 else C_TRACE_B

    # Widen to the panel's columns. On a 320 panel with 300 columns an hour
    # this is one pixel and the loop runs once; the loop is here so that a
    # different panel width still draws every column.
    xw = xs[:b - a]
    for k in range(width):
        sub = reg[:, xw + k]
        sub[span] = C_CLIP
        sub[own] = colour
        reg[:, xw + k] = sub

    # Columns inside the fetched window with no samples in them: a real gap in
    # the record, drawn as a red dash on the zero line rather than as a flat
    # line, which would be a claim that the ground was still.
    gap = ~m
    if gap.any():
        reg[mid, xw[gap]] = C_GAP
    return True


def draw_quakes(dst, lay, rec, events, n_cols):
    """Marks for located events, behind nothing and over the paper.

    A dark bar the height of the lane, with a bright cap above and below it.
    The bar is drawn *before* the traces so the ink stays readable over it;
    the caps are outside the lane and are drawn after, so they survive.
    """
    per_lane = rec["trace_cols"]
    for ev in events:
        col = int((ev["t"] - rec["t0"]) / rec["bin_s"])
        if not (0 <= col < min(rec["cols"], max(0, n_cols))):
            continue
        lane = col // per_lane
        if lane >= lay.lanes:
            continue
        x = lay.x0 + ((col % per_lane) * lay.trace_w) // per_lane
        y = lay.lane_y(lane)
        dst[y:y + lay.lane_h, x:x + 2] = C_QUAKE
        if y - 1 >= lay.chart_y:
            dst[y - 1, x:x + 2] = C_QUAKE_HI
        if y + lay.lane_h < lay.chart_y + lay.chart_h:
            dst[y + lay.lane_h, x:x + 2] = C_QUAKE_HI


def header_text(state, w=ds.WIDTH):
    """Station, headline, age -- widest set that fits, in that order of loss.

    The ladder is the same shape as caiso's and tide's: clipping the line
    would drop whatever fell off the right, and what falls off the right of
    this one is the part that says how old the data is.
    """
    rec = state["rec"]
    if rec is None:
        return "NO SEISMOGRAM", "", C_WARN, ""

    st = rec["station"]
    who = "%s.%s %s" % (st.get("net", "BK"), st.get("sta", "BRK"),
                        st.get("cha", "BHZ"))
    lefts = ["%s %dKM %s" % (who, round(rec["km"]), compass(rec["bearing"])),
             who, st.get("sta", "BRK")]

    ev = state["event"]
    if ev is not None:
        tag = "M%.1f" % ev["mag"]
        where = place_words(ev["place"])
        mids = ["%s %s" % (tag, where), "%s %dKM %s" % (tag, round(ev["km"]),
                                                        compass(ev["bearing"])),
                tag]
        midc = C_QUAKE_HI
    else:
        um = micron_s(rec["peak"], rec)
        midc = C_LABEL
        if um is None:
            mids = ["%d COUNTS PEAK" % round(rec["peak"]), ""]
        else:
            n = ("%.2f" % um) if um < 10 else ("%.0f" % um)
            mids = ["PEAK %s UM/S" % n, "%s UM/S" % n, ""]

    age = ftdata.describe_age(rec["age"])
    rights = ["DATA " + age, age, ""]
    if state["stale"]:
        rights = [("STALE " + r) if r else "STALE" for r in rights]

    gap = 5
    for left in lefts:
        for right in rights:
            for mid in mids:
                need = text_width(left) + text_width(right) + 2
                if mid:
                    need += text_width(mid) + 2 * gap
                if need <= w:
                    return left, mid, midc, right
    return lefts[-1], "", C_DIM, ""


def draw_header(dst, lay, state):
    if not lay.head_h:
        return
    left, mid, midc, right = header_text(state, lay.w)
    dst[:lay.head_h] = 0
    blit_text(dst, 0, 1, left, C_TEXT)
    rw = text_width(right) if right else 0
    if right:
        blit_text(dst, 0, lay.w - rw - 1, right,
                  C_WARN if state["stale"] else C_DIM)
    if mid:
        mw = text_width(mid)
        mx = min(lay.w - rw - 4 - mw,
                 max(text_width(left) + 5, (lay.w - mw) // 2))
        blit_text(dst, 0, mx, mid, midc)


def draw_nodata(dst, lay, lines):
    """The honest panel. No paper, no trace, no implied ground motion."""
    dst[:] = (6, 6, 8)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.h // 2 - (len(lines) * (6 * scale + 2)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        blit_text(dst, y, x, s, rgb, sc)
        y += GLYPH_H * sc + 3
    return dst


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    cache = args.cache_dir

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)     # the drum, drawn
    base = np.zeros((h, w, 3), np.uint8)       # paper and header, no ink

    # The sheen, exactly as caiso does it: a raised cosine window that lifts
    # what it crosses *towards white* rather than multiplying it, because four
    # of the colours here are already saturated in a channel and a multiply is
    # invisible over precisely the part of the panel worth animating. `delta`
    # is baked, and is already zero everywhere the panel is black, so the
    # per-frame cost is one multiply and one add over the window.
    sw = max(2, int(args.sweep_width))
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, 2 * math.pi, sw, dtype=f32))
    ramp = (ramp * float(args.sweep_gain)).astype(f32)[None, :, None]
    delta = np.zeros((h, w, 3), f32)
    sheen = np.empty((h, sw, 3), f32)

    lay = Layout(w, h, 6)

    cell = {"rec": None, "problem": None, "loaded": -1e18, "stale": False,
            "event": None, "events": [], "n_filled": 0,
            "pen": None, "reveal_cols": 0, "lay": lay, "stack": []}

    def state_of():
        return {"rec": cell["rec"], "stale": cell["stale"],
                "event": cell["event"], "events": cell["events"]}

    def reload_data(now):
        rec, _age, problem = read_drum(cache)
        cell["rec"], cell["problem"], cell["loaded"] = rec, problem, now
        if rec is None:
            cell["stale"] = False
            cell["events"] = []
            cell["event"] = None
            return

        lay = Layout(w, h, rec["lanes"])
        cell["lay"] = lay
        cell["stale"] = not ftdata.is_fresh(PRODUCT, rec["age"])
        cell["events"] = [] if args.no_quakes else read_quakes(rec, cache)
        cell["event"] = cell["events"][0] if cell["events"] else None

        # The scale. One lane peak-to-peak is `gain` times the background's
        # own peak-to-peak, so a quiet hour fills about two fifths of its lane
        # and the number in the axis strip is a real velocity. Fixed against
        # the background and not against the day's maximum: an axis that
        # rescales when something happens makes every event look the same
        # size, which is the one thing a helicorder is for.
        full_scale = max(1.0, float(args.gain) * rec["noise"])
        half = lay.lane_h / 2.0
        rows_per_count = half / (full_scale * 0.5)
        clip_rows = max(half, float(args.clip_lanes) * lay.lane_h)

        n_filled = min(rec["n_filled"], rec["cols"])
        cell["n_filled"] = n_filled
        # The reveal never runs past the last lane the panel actually has.
        cell["reveal_cols"] = max(1, min(n_filled,
                                         lay.lanes * rec["trace_cols"]))

        base[:] = 0
        draw_paper(base, lay, rec, args.h24, full_scale)
        draw_header(base, lay, state_of())
        # The event bars are part of the paper, drawn under the ink.
        draw_quakes(base, lay, rec, cell["events"], n_filled)

        # The drum in every state it passes through: `stack[i]` is the paper
        # with lines 0..i written on it, and `stack[-1]` is the paper alone.
        # This exists because a line's ink can overrun into the line *below*
        # it, which has not been written yet -- so the half-drawn drum is not
        # something the finished picture can be masked back into, and the
        # reveal without this shows a big event bleeding upward out of a lane
        # that is still blank. Seven frames of 61 kB, built once.
        stack = cell["stack"]
        del stack[:]
        work = base.copy()
        for i in range(lay.lanes):
            draw_lane(work, lay, rec, i, rows_per_count, clip_rows, n_filled)
            stack.append(work.copy())
        stack.append(base)
        static[:] = work
        # Caps again, over the ink: they live outside the lane and are what
        # says "this mark belongs to that line".
        for ev in cell["events"]:
            col = int((ev["t"] - rec["t0"]) / rec["bin_s"])
            if not (0 <= col < n_filled) or col // rec["trace_cols"] >= lay.lanes:
                continue
            lane, x = column_pos(lay, rec, col)
            y = lay.lane_y(lane)
            if y - 1 >= lay.chart_y:
                static[y - 1, x:x + 2] = C_QUAKE_HI
            if y + lay.lane_h < lay.chart_y + lay.chart_h:
                static[y + lay.lane_h, x:x + 2] = C_QUAKE_HI

        # Where the pen is: the last column that has data, in panel pixels.
        cell["pen"] = column_pos(lay, rec, n_filled - 1) if n_filled else None

        np.subtract(255.0, static, out=delta, dtype=f32)
        np.multiply(delta, static.max(axis=2, keepdims=True) > 0, out=delta)

    def render(t, i):
        now = time.time()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        lay = cell["lay"]
        if cell["rec"] is None:
            lines = [("NO SEISMOGRAM", C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --LOOP 300", C_TEXT)]
            if cell["problem"]:
                lines.append((str(cell["problem"]).upper()[:52], C_DIM))
            return draw_nodata(frame, lay, lines)

        # The reveal: the drum draws itself in reading order, line by line.
        # Two whole-frame slices and no mask. Everything left of the pen is
        # the drum with lines 0..L on it; everything right of it is the drum
        # with lines 0..L-1, which is precisely "this line has not been
        # written here yet" *including* whatever this line was going to
        # scribble into its neighbours. Cutting the picture at the pen's
        # column rather than at the lane's rows is what makes that come out
        # right in one assignment instead of four.
        pen = cell["pen"]
        drawing = False
        stack = cell["stack"]
        if args.reveal > 0 and t < args.reveal and cell["reveal_cols"] and stack:
            frac = t / args.reveal
            idx = int(frac * cell["reveal_cols"])
            lane, x = column_pos(lay, cell["rec"], idx)
            before = stack[lane - 1] if lane else stack[-1]
            frame[:] = stack[lane]
            if x < w:
                frame[lay.chart_y:, x:] = before[lay.chart_y:, x:]
            pen = (lane, x)
            drawing = True
        else:
            frame[:] = static
            if args.sweep > 0:
                # The sheen, once the drum is up: a soft bright window
                # travelling across the paper, off-screen either side so that
                # it enters and leaves rather than appearing in the middle.
                phase = ((t - args.reveal) % args.sweep) / args.sweep
                x0 = int(phase * (w + 2 * sw)) - sw
                a, b = max(0, x0), min(w, x0 + sw)
                if b > a:
                    buf = sheen[:, :b - a]
                    np.multiply(delta[:, a:b], ramp[:, a - x0:b - x0], out=buf)
                    np.add(buf, static[:, a:b], out=buf)
                    frame[:, a:b] = buf

        # The pen. Bright while the drum is drawing itself, breathing after
        # that -- and it is the one thing guaranteed to move in every frame,
        # which matters because the sheen spends a moment off the right-hand
        # edge between passes and a panel that holds a single frame for half a
        # second reads as a crash on a wall between two animated demos.
        if pen is not None:
            lane, x = pen
            y = lay.lane_y(lane)
            k = 1.0 if drawing else 0.45 + 0.55 * abs(
                math.sin(t * math.pi * PULSE_HZ))
            frame[y:y + lay.lane_h, x] = tuple(int(c * k) for c in C_NOW)
            if x + 1 < lay.x1:
                frame[y:y + lay.lane_h, x + 1] = C_NOW_DIM
        return frame

    reload_data(time.time())
    render.state = cell               # tests reach in here; nothing else does
    render.layout = lambda: cell["lay"]
    render.static = static
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "six hours of ground motion from BK.BRK, as a drum recorder",
                  fps=20)


if __name__ == "__main__":
    main()
