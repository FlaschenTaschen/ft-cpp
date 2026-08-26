#!/usr/bin/env python3
"""BART as a Marey stringline: distance down, time across, every train a diagonal.

This is the notation railways have drawn since the 1880s, and it is still the
clearest picture of a transit line anybody has invented. Distance along the line
runs **down** the panel -- stations as horizontal gridlines, spaced by real
track kilometres rather than evenly, so the crush of eight stations inside San
Francisco really is a crush. Time runs **across**, an hour and a half of it,
with now as a bright vertical line. Every train is one diagonal.

Once you see it that way the whole diagram reads without a legend:

  * the **slope** of a line is the train's speed. Steep is fast. A train
    standing at a platform is flat. The Concord-to-Rockridge run through the
    Berkeley hills is visibly steeper than the crawl from Embarcadero to
    Balboa Park, because it is.
  * trains going the **other way** slope the other way, so the two directions
    are two families of lines leaning against each other.
  * where two lines **cross**, two trains passed each other. Those crossings are
    drawn in white, because "where did they meet" is the question a stringline
    was invented to answer.
  * **headway** is the horizontal gap between parallel lines, and bunching --
    two lines converging -- is the thing you can see coming half an hour before
    anybody at a platform can.

The wall already has three dot-on-a-map panels (`adsb`, `quake`, `sats`). This
one deliberately is not a fourth. There is no route map here on purpose: a map
of BART tells you where the stations are, which never changes, and a stringline
tells you what the trains are doing, which is the only part worth a panel.

**Why the Yellow line.** It is the busiest of the five -- 26 of the 83 trains
BART had running when this was written -- it is the longest at 100 km, and it is
the only one that crosses the whole picture: Antioch, the Berkeley hills, the
Transbay Tube, Market Street, and out to SFO and Millbrae. A hundred kilometres
in ninety minutes against ninety minutes of panel width means a full end-to-end
run is very nearly the diagonal of the screen, which is exactly the scale a
Marey diagram wants. `--line` takes any of yellow, orange, green, red or blue;
the others are shorter and leave more of the panel empty.

**Past and future are drawn differently, because they are different.** Left of
the now-line the trains are drawn solid: those are the times BART's own feed
gave for stops as the trains reached them, frozen by the fetcher as each stop
dropped out of the feed behind the train. Right of the now-line they are dashed
and dimmed: that is prediction, and it is BART's prediction, not ours. The right
edge also thins out honestly -- a train that has not left Antioch yet is not in
the feed at all, so nothing is drawn for it rather than a schedule pretending to
be a forecast.

**Where the data comes from.** BART publishes GTFS-Realtime TripUpdates with no
API key and no signup, which is why it is BART here and not Muni (511.org wants
a key; verified 401 without one). `ftdata.py` fetches that protobuf once a
minute, decodes it with a hand-rolled sixty-line wire reader rather than
dragging the `protobuf` package onto the Pi, and keeps a rolling ninety minutes
of merged history. This module reads the cache and never touches the network.

    $ python3 ftdata.py --once --only bart-stringline

**Station order and track distance are baked**, in `stringline-lines.npz`, out
of BART's static GTFS -- an 892 KB zip that must never be on the fetch timer.
The distances are true track kilometres: each station is projected onto the
line's own GTFS shape polyline and its cumulative arc length taken, so the tube
under the bay is as long on this panel as it is under the bay. Re-bake it when
BART publishes a new schedule:

    $ python3 stringline.py --bake-lines https://www.bart.gov/dev/schedules/google_transit.zip

The asset also carries a trip_id -> line table from that schedule, which is what
lets a train be identified the first minute the fetcher sees it rather than
after it has been watched from its origin. It is checked against the live stop
list before it is believed, so a stale table after a schedule change degrades to
the slower station-set match instead of lying.
"""

import math
import os
import sys
import time

import numpy as np

import demoscene as ds
import ftdata

f32 = np.float32

ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "stringline-lines.npz")

PRODUCT = "bart-stringline"

# Colour carries direction, brightness carries certainty.
#
# Direction gets the colour rather than the line's own livery because at this
# scale a stringline leans about seventeen degrees off horizontal, and telling
# +17 from -17 across a room on a one-pixel line is not something anybody should
# have to do. The pair is the same warm/cool pair tide.py uses for flood and
# ebb, which keeps the wall's vocabulary consistent: warm goes one way, cool
# comes back. Which is which is stated by the terminal labels in the gutter,
# each drawn in the colour of the trains heading towards it.
C_DOWN = (255, 176, 60)                 # towards the far terminal (SFO/Millbrae)
C_UP = (96, 206, 255)                   # towards the first (Antioch)
C_CROSS = (255, 252, 235)               # two trains passing

C_GRID_ST = (21, 23, 30)                # every station
C_GRID_ST_MAJ = (30, 34, 44)            # the ones that got a label
C_GRID_T = (17, 18, 25)                 # fifteen minutes
C_GRID_T_HOUR = (34, 37, 50)
C_AXIS = (44, 48, 60)
C_TEXT = (196, 214, 228)
C_DIM = (96, 110, 124)
C_FAINT = (58, 66, 78)
C_NOW = (255, 244, 210)
C_WARN = (255, 96, 72)

# How much the prediction side is knocked back, and its dash. Six columns lit
# and two dark, which at eighteen seconds a column is a dash about two minutes
# long -- long enough to still read as a line rather than as a row of dots, and
# short enough that the gaps are unmistakable next to the solid past.
FUTURE_GAIN = 0.50
DASH_ON, DASH_OFF = 6, 2


# --------------------------------------------------------------------------
# A 3x5 pixel font, the same one tide.py, defcon.py and sort.py use: five rows
# a glyph, each row an octal digit whose three bits are the three columns.
# Anything built from a real typeface is mush at five pixels.
# --------------------------------------------------------------------------

_FONT = {
    "0": "75557", "1": "26227", "2": "71747", "3": "71717", "4": "55711",
    "5": "74717", "6": "74757", "7": "71222", "8": "75757", "9": "75717",
    "A": "25755", "B": "65656", "C": "34443", "D": "65556", "E": "74647",
    "F": "74644", "G": "34553", "H": "55755", "I": "72227", "J": "11152",
    "K": "55655", "L": "44447", "M": "57755", "N": "65555", "O": "25552",
    "P": "65644", "Q": "25573", "R": "65655", "S": "34216", "T": "72222",
    "U": "55557", "V": "55552", "W": "55775", "X": "55255", "Y": "55222",
    "Z": "71247", " ": "00000", "-": "00700", ".": "00002", ":": "02020",
    "/": "11244", "+": "02720", "'": "22000", "*": "05250", "!": "22202",
    "?": "71602", "(": "12221", ")": "42224", ",": "00021", "=": "07070",
}

_GLYPHS = {}
for _ch, _rows in _FONT.items():
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
    s = s.upper()
    if not s:
        return np.zeros((GLYPH_H * scale, 1), bool)
    out = np.zeros((GLYPH_H, len(s) * 4 - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def text_width(s, scale=1):
    return max(1, (len(s) * 4 - 1) * scale)


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


def hhmm(epoch, ampm=True):
    """A compact local-time label: '6:55P' or '18:55'."""
    lt = time.localtime(epoch)
    if not ampm:
        return time.strftime("%H:%M", lt)
    h = lt.tm_hour % 12 or 12
    return "%d:%02d%s" % (h, lt.tm_min, "A" if lt.tm_hour < 12 else "P")


# --------------------------------------------------------------------------
# The baked line asset.
#
# Everything geographic is in one npz so that build() opens exactly one file
# and does no parsing worth the name. The arrays are flat with per-line
# offsets rather than one entry per line, because numpy has no ragged array and
# a dict of arrays in an npz is a dict of files.
# --------------------------------------------------------------------------

class Line(object):
    """One BART line: its stations in order, their track kilometres, colours."""

    __slots__ = ("key", "name", "rgb", "codes", "labels", "names", "km",
                 "n", "span")

    def __init__(self, key, name, rgb, codes, labels, names, km):
        self.key = key
        self.name = name
        self.rgb = rgb
        self.codes = codes
        self.labels = labels
        self.names = names
        self.km = km
        self.n = len(codes)
        self.span = float(km[-1]) if self.n else 1.0


def load_lines(path=ASSET):
    """Read the baked asset. Returns an ordered dict-alike of key -> Line."""
    with np.load(path, allow_pickle=False) as z:
        keys = [str(k) for k in z["line_key"]]
        names = [str(k) for k in z["line_name"]]
        rgb = z["line_rgb"]
        off = z["line_off"]
        code, label, full, km = (z["st_code"], z["st_label"],
                                 z["st_name"], z["st_km"])
        out = []
        for i, key in enumerate(keys):
            a, b = int(off[i]), int(off[i + 1])
            out.append((key, Line(
                key, names[i], tuple(int(v) for v in rgb[i]),
                [str(c) for c in code[a:b]], [str(c) for c in label[a:b]],
                [str(c) for c in full[a:b]], np.asarray(km[a:b], f32))))
    return out


# --------------------------------------------------------------------------
# Reading what ftdata left behind. Same contract as every other data panel:
# `load()` hands back a payload and an age and never raises, so everything
# still wrong after that is wrong about *content* and is caught here.
# --------------------------------------------------------------------------

class Trip(object):
    """One train's path: absolute times against kilometres along the line."""

    __slots__ = ("tid", "down", "t", "km", "delay")

    def __init__(self, tid, down, t, km, delay):
        self.tid = tid
        self.down = down                # True: station index increasing
        self.t = t                      # float64 epoch seconds, increasing
        self.km = km                    # float32, matching
        self.delay = delay              # seconds, + is late


def read_trips(cache_dir, line_key, lines):
    """(trips, payload, age, problem). Never raises on a bad record."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return [], None, None, "no cached BART data"
    payload, age = got
    try:
        keys = [str(k) for k in payload["lines"]]
        idx = keys.index(line_key)
    except Exception:                                        # noqa: BLE001
        return [], payload, age, "record does not carry the %s line" % line_key
    line = dict(lines)[line_key]
    out = []
    for tr in payload.get("trips", []):
        try:
            if int(tr["l"]) != idx:
                continue
            s = tr["s"]
            t0 = float(tr["t0"])
            t = np.asarray(tr["a"], np.float64) + t0
            if len(s) != len(t) or len(s) < 2:
                continue
            # These are the x axis of an np.interp, and numpy does not check
            # that an x axis increases: it quietly returns nonsense instead.
            # The fetcher already enforces this; doing it again costs one pass
            # over twenty floats and means a hand-edited or older record cannot
            # draw a train that goes backwards in time.
            np.maximum.accumulate(t, out=t)
            si = np.asarray(s, np.int32)
            if si.min() < 0 or si.max() >= line.n:
                continue
            km = line.km[si]
            out.append(Trip(str(tr.get("i", "")),
                            bool(int(tr.get("d", 0)) == 0), t, km,
                            float(tr.get("y", 0.0))))
        except Exception:                                    # noqa: BLE001
            # One malformed trip is one train missing, not a dead panel.
            continue
    return out, payload, age, None


# --------------------------------------------------------------------------
# Layout. The panel is a 5:1 letterbox and this is what it gets divided into:
#
#   rows 0..5    header text
#   row  6       hairline
#   rows 7..     the plot, distance down
#   then         one row of time ticks and five of clock labels
#
#   cols 0..     station labels, right aligned
#   col          the distance axis
#   then         the plot, time across
#
# The ruler lives *inside* the sliding strip rather than on the static frame,
# because the clock labels have to move with the trains -- they are marks in
# absolute time, not decoration.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h, gutter):
        self.w, self.h = w, h
        self.head_h = min(6, max(0, h // 8))
        self.gutter = max(0, min(gutter, w // 3))
        self.axis_x = self.gutter
        self.plot_x = self.axis_x + 1
        self.plot_w = w - self.plot_x
        self.chart_y = self.head_h + 1
        # Ticks plus five rows of digits, if the panel is tall enough to spare
        # them; a short panel gives the whole thing to the trains.
        self.ruler_h = GLYPH_H + 1 if h >= 40 else 0
        self.plot_y = self.chart_y
        self.plot_h = max(4, h - self.chart_y - self.ruler_h)
        self.strip_h = self.plot_h + self.ruler_h


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--line", default="yellow",
                    help="which BART line: yellow, orange, green, red, blue")
    ap.add_argument("--flip", action="store_true",
                    help="put the far terminal at the top instead")
    ap.add_argument("--past", type=float, default=40.0,
                    help="minutes of observed history left of now")
    ap.add_argument("--ahead", type=float, default=50.0,
                    help="minutes of prediction right of now")
    ap.add_argument("--gutter", type=int, default=29,
                    help="pixels of station label down the left")
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--asset", default=ASSET, help="baked line geometry")
    ap.add_argument("--reload", type=float, default=45.0,
                    help="seconds between re-reads of the cache (0 = never)")
    ap.add_argument("--rebuild", type=float, default=120.0,
                    help="seconds between forced redraws of the sliding strip")
    ap.add_argument("--margin", type=float, default=420.0,
                    help="seconds of strip baked either side, so the window "
                         "can slide between rebuilds without running off it")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second")


def parse_when(s):
    """'now', an epoch, or 'YYYY-MM-DD HH:MM' in local time."""
    if not s or s == "now":
        return None
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    raise ValueError("cannot read a time out of %r" % s)


def clock(at=None, rate=1.0):
    """A callable returning the demo's present moment in epoch seconds."""
    base = time.time() if at is None else float(at)
    start = time.monotonic()
    if at is None and rate == 1.0:
        return time.time
    return lambda: base + (time.monotonic() - start) * rate


# --------------------------------------------------------------------------
# Station labels down the gutter.
#
# Twenty-eight stations into fifty rows is under two rows each, and a 3x5 font
# needs six. So most stations get a gridline and no name, which is the right
# trade: the gridlines carry the geometry and the labels only have to say
# roughly where on the line you are looking. The two terminals are always
# labelled -- they are what makes the axis mean anything -- and after that the
# choice is greedy from a list of the stations somebody would actually use to
# orient themselves, taking each only if it clears the last by a full label.
# --------------------------------------------------------------------------

# Interchanges, terminals and the places people name when they say where they
# are. Order is priority; anything not here is only ever a gridline.
LANDMARKS = ("EMBR", "MCAR", "DALY", "12TH", "CONC", "BALB", "WOAK", "POWL",
             "COLS", "BAYF", "FRMT", "RICH", "DBRK", "HAYW", "PITT", "SFIA",
             "CIVC", "MONT", "24TH", "GLEN", "LAFY", "ORIN", "WCRK", "ROCK",
             "19TH", "PHIL", "SANL", "ASHB", "UCTY", "MLPT", "CAST", "SSAN",
             "FTVL", "LAKE", "PLZA", "NCON", "PCTR", "SHAY", "WARM", "COLM")


def pick_labels(line, rows, min_gap=GLYPH_H + 1):
    """{station index: row} for the stations that get a name in the gutter."""
    n = line.n
    placed = {0: int(rows[0]), n - 1: int(rows[-1])}
    order = [line.codes.index(c) for c in LANDMARKS if c in line.codes]
    for i in order:
        r = int(rows[i])
        if all(abs(r - v) >= min_gap for v in placed.values()):
            placed[i] = r
    return placed


# --------------------------------------------------------------------------
# The sliding strip. This is the whole performance story.
#
# A stringline is a picture in absolute time: a train that passed MacArthur at
# 5:42 passed it at 5:42 no matter when you look. So the picture does not have
# to be redrawn as the clock moves -- it has to be *slid*. Everything is
# rasterised once into a strip a few minutes wider than the panel, and each
# frame takes a slice of it. Two slices, in fact: solid to the left of the now
# column and dashed to the right, which works out to a fixed split because the
# now column never moves.
#
# The cost of that is about ten numpy calls a frame regardless of how many
# trains are running, against the several hundred line segments a naive
# redraw would need. The cost of the rebuild is one np.interp per train, since
# a train is a *function* of time -- it is in one place at any moment -- so its
# whole diagonal comes out of a single interpolation onto the strip's columns
# rather than out of a segment-by-segment rasteriser.
#
# `ink` is the trains as a 2-bit field, 1 for one direction and 2 for the other
# and 3 where they overlap, OR-ed rather than overwritten so a crossing paints
# itself. Colour is a lookup through a four-entry palette afterwards, which is
# also how the same geometry becomes both the solid and the dashed strip
# without being rasterised twice: two palettes, one field.
# --------------------------------------------------------------------------

def bake_grid(strip_h, sw, t0, spp, rows, labelled, ruler_h, h24):
    """The gridlines and the clock ruler, in strip coordinates."""
    g = np.zeros((strip_h, sw, 3), np.uint8)
    plot_h = strip_h - ruler_h
    # Stations. Every one gets a line; the labelled ones get a brighter one, so
    # the gutter names have something to attach to across 290 columns.
    r = np.clip(np.round(rows).astype(np.int32), 0, plot_h - 1)
    g[r] = C_GRID_ST
    maj = np.array(sorted(set(labelled.values())), np.int32)
    if len(maj):
        g[np.clip(maj, 0, plot_h - 1)] = C_GRID_ST_MAJ

    # Time. Ticks every quarter hour, a brighter line on the half hour, and a
    # clock label under it -- in local time, because the person reading this is
    # standing in the same city as the trains.
    quarter = 900.0
    first = math.ceil(t0 / quarter) * quarter
    tick = first
    while tick < t0 + sw * spp:
        c = int(round((tick - t0) / spp))
        if 0 <= c < sw:
            half = (int(tick) % 1800) == 0
            g[:plot_h, c] = C_GRID_T_HOUR if half else C_GRID_T
            if ruler_h:
                g[plot_h, c] = C_DIM if half else C_FAINT
                if half:
                    s = hhmm(tick, not h24)
                    x = c - text_width(s) // 2
                    blit_text(g[plot_h + 1:], 0, x, s, C_DIM)
        tick += quarter
    return g


def bake_ink(strip_h, sw, t0, spp, trips, kmscale, kmoff, plot_h):
    """Rasterise every train into the 2-bit direction field. One interp each.

    `kmscale` and `kmoff` carry the distance axis, sign included, so that
    --flip is one negated scale here rather than a second code path: nothing
    below knows or cares which terminal is at the top.
    """
    ink = np.zeros((strip_h, sw), np.uint8)
    yy = np.arange(plot_h, dtype=np.int32)[:, None]
    drawn = 0
    for tr in trips:
        # The columns this train exists in, clipped to the strip.
        c0 = int(math.ceil((tr.t[0] - t0) / spp))
        c1 = int(math.floor((tr.t[-1] - t0) / spp))
        c0, c1 = max(0, c0), min(sw - 1, c1)
        if c1 <= c0:
            continue
        cols = np.arange(c0, c1 + 1, dtype=np.float64)
        km = np.interp(t0 + cols * spp, tr.t, tr.km)
        ri = np.round(km * kmscale + kmoff).astype(np.int32)
        np.clip(ri, 0, plot_h - 1, out=ri)
        # Filled between neighbouring columns rather than plotted point by
        # point: a diagonal that moves more than one row per column is a dotted
        # line otherwise, and a dotted stringline reads as noise. Same trick
        # tide.py uses on the tide curve.
        top = np.minimum(ri[:-1], ri[1:])
        bot = np.maximum(ri[:-1], ri[1:])
        sub = ink[:plot_h, c0:c1]
        sub |= ((yy >= top) & (yy <= bot)).astype(np.uint8) * (1 if tr.down else 2)
        drawn += 1
    return ink, drawn


def strip_palettes():
    """(solid, dashed) 4-entry colour tables for the direction field."""
    solid = np.zeros((4, 3), np.uint8)
    solid[1] = C_DOWN
    solid[2] = C_UP
    solid[3] = C_CROSS
    future = np.clip(solid.astype(f32) * FUTURE_GAIN, 0, 255).astype(np.uint8)
    future[0] = 0
    return solid, future


def bake_strips(grid, ink, solid, future, dash):
    """Colour the ink twice: solid for the past, dashed and dimmed ahead."""
    past = np.maximum(grid, solid[ink])
    ahead = np.maximum(grid, future[ink * dash])
    return past, ahead


# --------------------------------------------------------------------------
# The static overlay: header, gutter, axis. Everything that does not slide.
# --------------------------------------------------------------------------

def draw_gutter(dst, lay, line, rows, labelled):
    """Station names right-aligned into the gutter, terminals in their colour.

    The terminals are coloured by the direction of travel *towards* them, and
    that is the whole legend: a train drawn warm is a train heading for the
    terminal whose name is written in warm. Which is deliberately not the same
    thing as "warm goes downwards" -- --flip turns the panel over and the
    colours stay attached to the destinations, where the meaning is.
    """
    n = line.n
    for i, r in sorted(labelled.items()):
        s = line.labels[i]
        rgb = C_DIM
        if i == 0:
            rgb = C_UP
        elif i == n - 1:
            rgb = C_DOWN
        y = int(np.clip(lay.plot_y + r - GLYPH_H // 2, lay.chart_y,
                        lay.plot_y + lay.plot_h - GLYPH_H))
        blit_text(dst, y, max(0, lay.axis_x - 1 - text_width(s)), s, rgb)
    dst[lay.chart_y:lay.plot_y + lay.plot_h, lay.axis_x] = C_AXIS


def header_text(state, line, h24, w):
    """Left, middle and right of the status line, widest set that fits.

    Same ladder as the other data panels: each of the three has shorter forms
    and the widest combination that fits is what gets drawn, because simply
    clipping loses the right-hand end -- which is the part that says how old the
    data is, and that is the last thing that should go quietly missing.
    """
    age = state["age"]
    lefts = ["BART %s %dKM" % (line.name, round(line.span)),
             "BART %s" % line.name, line.name]
    n = state["running"]
    if state["problem"]:
        mids = [state["problem"].upper()[:34], "NO DATA"]
    elif n == 0:
        mids = ["NO TRAINS RUNNING", "NO TRAINS"]
    else:
        d = state["delay"]
        if d is None:
            word = "%d RUNNING" % n
        elif d >= 60:
            word = "%d RUNNING  +%dM LATE" % (n, int(round(d / 60.0)))
        elif d <= -60:
            word = "%d RUNNING  %dM EARLY" % (n, int(round(-d / 60.0)))
        else:
            word = "%d RUNNING  ON TIME" % n
        mids = [word, "%d RUNNING" % n, "%d" % n]
    clockstr = hhmm(state["now"], not h24)
    rights = ["%s  DATA %s" % (clockstr, ftdata.describe_age(age))
              if age is not None else clockstr, clockstr, ""]
    if state["stale"]:
        rights = ["STALE " + r if r else "STALE" for r in rights]
    gap = 5
    for left in lefts:
        for right in rights:
            for mid in mids:
                need = text_width(left) + (text_width(right) if right else 0) + 2
                if mid:
                    need += text_width(mid) + 2 * gap
                if need <= w:
                    return left, mid, right
    return lefts[-1], "", ""


def draw_header(dst, lay, state, line, h24):
    left, mid, right = header_text(state, line, h24, lay.w)
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
        # A missing record is a fault and is red. No trains is not: BART is
        # shut for six hours every night, and an alarm colour over a railway
        # that is simply closed teaches people to ignore the alarm colour.
        if state["problem"]:
            colour = C_WARN
        elif state["running"] == 0:
            colour = C_DIM
        else:
            colour = C_TEXT
        blit_text(dst, 0, mx, mid, colour)
    if lay.head_h:
        dst[lay.head_h - 1] = (12, 14, 18)


def draw_nodata(dst, lay, lines):
    """The honest panel: no grid, no strings, no implication of a service."""
    dst[:] = (5, 5, 7)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.h // 2 - (len(lines) * (GLYPH_H * scale + 3)) // 2)
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
    lay = Layout(w, h, args.gutter)
    now_of = clock(parse_when(args.at), args.rate)

    lines = load_lines(args.asset)
    keyed = dict(lines)
    key = args.line.strip().lower()
    if key not in keyed:
        raise SystemExit("--line must be one of %s"
                         % ", ".join(k for k, _ in lines))
    line = keyed[key]

    # Distance axis. Rows are true track kilometres, which is the whole point:
    # eight stations inside San Francisco share the four rows they are worth
    # rather than getting an eighth of the panel each.
    kmscale = (lay.plot_h - 1) / max(line.span, 1e-6)
    kmoff = 0.0
    if args.flip:
        kmscale, kmoff = -kmscale, float(lay.plot_h - 1)
    rows = line.km * kmscale + kmoff
    labelled = pick_labels(line, rows)

    # Time axis. spp is seconds per column; nowc is the column the present
    # moment sits in, and it never moves, which is what makes the per-frame
    # composite two fixed slices.
    span = max(60.0, (args.past + args.ahead) * 60.0)
    spp = span / max(1, lay.plot_w)
    nowc = int(round(args.past * 60.0 / spp))
    nowc = int(np.clip(nowc, 1, lay.plot_w - 2))
    margin_cols = max(4, int(round(args.margin / spp)))
    sw = lay.plot_w + 2 * margin_cols

    solid_pal, future_pal = strip_palettes()
    # The dash is a column mask folded into the palette lookup, so dimming and
    # dashing the prediction side costs one multiply at bake time and nothing
    # per frame. It is anchored in absolute time along with everything else, so
    # the dashes travel with the trains instead of crawling under them.
    dash = ((np.arange(sw) % (DASH_ON + DASH_OFF)) < DASH_ON).astype(np.uint8)

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)

    # The tag on the now-line, pre-rendered rather than blitted per frame: it
    # has to go on *after* the sliding strip, since the strip covers the whole
    # chart, and one np.maximum of a ready-made patch is cheaper than measuring
    # and rasterising three glyphs thirty times a second.
    now_tag = np.zeros((GLYPH_H, text_width("NOW"), 3), np.uint8)
    blit_text(now_tag, 0, 0, "NOW", C_NOW)
    tag_x = lay.plot_x + nowc + 2
    if tag_x + now_tag.shape[1] > w:
        tag_x = lay.plot_x + nowc - 2 - now_tag.shape[1]
    tag_x = max(0, tag_x)
    tag_w = min(now_tag.shape[1], w - tag_x)
    pulse_cache = {}

    cell = {"trips": [], "payload": None, "age": None, "problem": None,
            "loaded": -1e18, "strip_t0": None, "past": None, "ahead": None,
            "ink": None, "baked": -1e18, "head_key": None, "head_at": None,
            "running": 0, "delay": None, "stamp": None, "drawn": 0}

    def state_of(now):
        age = cell["age"]
        return {"now": now, "age": age, "problem": cell["problem"],
                "running": cell["running"], "delay": cell["delay"],
                "stale": age is not None and not ftdata.is_fresh(PRODUCT, age)}

    def reload_data(now):
        trips, payload, age, problem = read_trips(args.cache_dir, key, lines)
        cell["trips"], cell["payload"] = trips, payload
        cell["age"], cell["problem"] = age, problem
        cell["loaded"] = now
        stamp = None if payload is None else payload.get("t")
        if stamp != cell["stamp"]:
            cell["stamp"] = stamp
            cell["baked"] = -1e18            # new data: the strip is wrong now

    def summarise(now):
        """How many trains are on the line right now, and how late they are.

        Counted from the trip paths rather than from the record's own totals,
        because the record covers five lines and this panel shows one of them.

        The ten minutes of slack on the near end is not fudge. A TripUpdate only
        carries the stops a train has *not* reached, so a train that has just
        left MacArthur has a first known stop a few minutes in the future, and
        until the fetcher has watched it long enough to have a stop behind it,
        the strictly-inside test would say it is not running. It is running. Ten
        minutes is comfortably more than a BART interstation and comfortably
        less than a headway.
        """
        n = 0
        delays = []
        for tr in cell["trips"]:
            if tr.t[0] - 600.0 <= now <= tr.t[-1]:
                n += 1
                delays.append(tr.delay)
        cell["running"] = n
        if delays:
            delays.sort()
            cell["delay"] = delays[len(delays) // 2]
        else:
            cell["delay"] = None

    def rebake(now):
        t0 = now - (nowc + margin_cols) * spp
        grid = bake_grid(lay.strip_h, sw, t0, spp, rows, labelled,
                         lay.ruler_h, args.h24)
        ink, drawn = bake_ink(lay.strip_h, sw, t0, spp, cell["trips"],
                              kmscale, kmoff, lay.plot_h)
        past, ahead = bake_strips(grid, ink, solid_pal, future_pal, dash)
        cell["strip_t0"], cell["ink"] = t0, ink
        cell["past"], cell["ahead"] = past, ahead
        cell["baked"] = now
        cell["drawn"] = drawn

    def rebuild_static(state):
        static[:] = 0
        draw_header(static, lay, state, line, args.h24)
        draw_gutter(static, lay, line, rows, labelled)

    def render(t, i):
        now = now_of()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)
            summarise(now)

        if cell["payload"] is None:
            msg = [("NO BART DATA", C_WARN),
                   ("RUN  PYTHON3 FTDATA.PY --LOOP 60", C_TEXT)]
            if cell["problem"]:
                msg.append((cell["problem"].upper()[:52], C_DIM))
            return draw_nodata(frame, lay, msg)

        # The strip is rebaked when the data changes, when it has been a while,
        # or when the window has slid far enough to reach its margin. Everything
        # else is two slices.
        off = 0 if cell["strip_t0"] is None else \
            int(round((now - cell["strip_t0"]) / spp))
        if (cell["past"] is None or now - cell["baked"] >= args.rebuild
                or off < nowc or off + lay.plot_w - nowc > sw):
            summarise(now)
            rebake(now)
            off = nowc + margin_cols
            cell["head_key"] = None

        state = state_of(now)
        # Formatting the header costs a third of a millisecond and its content
        # changes about once a minute, so it is asked three times a minute.
        if int(now // 20) != cell["head_at"] or cell["head_key"] is None:
            cell["head_at"] = int(now // 20)
            hk = header_text(state, line, args.h24, w)
            if hk != cell["head_key"]:
                cell["head_key"] = hk
                rebuild_static(state)

        frame[:] = static
        y0, y1 = lay.chart_y, lay.chart_y + lay.strip_h
        x0 = lay.plot_x
        frame[y0:y1, x0:x0 + nowc] = cell["past"][:, off - nowc:off]
        frame[y0:y1, x0 + nowc:x0 + lay.plot_w] = \
            cell["ahead"][:, off:off + lay.plot_w - nowc]

        # The now-line and the trains standing on it. Both come out of the same
        # column of the ink field, so the dots cannot disagree with the strings:
        # a dot *is* the place a string crosses the present moment.
        # Quantised to sixteen steps and cached, the way tide.py caches its
        # streak colours: building the palette from a float every frame is four
        # numpy calls on a twelve-element array to reproduce one of sixteen
        # answers, and on this machine the call is most of the cost whatever
        # its size.
        step = int((0.70 + 0.30 * math.sin(now * 2.0)) * 16.0)
        pair = pulse_cache.get(step)
        if pair is None:
            k = step / 16.0
            pair = pulse_cache[step] = (
                np.clip(solid_pal.astype(f32) * (0.55 + 0.45 * k),
                        0, 255).astype(np.uint8),
                np.array(tuple(int(c * 0.30 * k) for c in C_NOW), np.uint8))
        dots, guide = pair
        col = cell["ink"][:lay.plot_h, off]
        nx = x0 + nowc
        band = frame[lay.chart_y:lay.chart_y + lay.plot_h, nx]
        np.maximum(band, guide, out=band)
        hits = np.flatnonzero(col)
        if len(hits):
            frame[lay.chart_y + hits, max(0, nx - 1):nx + 2] = \
                dots[col[hits]][:, None, :]
        tag = frame[lay.chart_y + 1:lay.chart_y + 1 + GLYPH_H,
                    tag_x:tag_x + tag_w]
        np.maximum(tag, now_tag[:, :tag_w], out=tag)
        return frame

    reload_data(now_of())
    summarise(now_of())
    render.state = cell               # the test script reaches in here
    render.layout = lay
    render.line = line
    render.rows = rows
    render.labelled = labelled
    render.clock = now_of
    render.geometry = (spp, nowc, sw, margin_cols)
    return render


# --------------------------------------------------------------------------
# Baking `stringline-lines.npz` out of BART's static GTFS.
#
# This is an offline tool and never runs on the wall: it wants an 892 KB zip,
# csv and zipfile, and about a second of projection arithmetic. It lives here
# rather than in a script of its own so that the asset and the code that reads
# it cannot drift apart.
#
#     $ python3 stringline.py --bake-lines google_transit.zip
#     $ python3 stringline.py --bake-lines https://www.bart.gov/dev/schedules/google_transit.zip
#
# Three things come out of the schedule and nothing else does:
#
#   * the **station order** of each line, from its longest trip, collapsing the
#     consecutive duplicate that SFO's two platforms produce;
#   * the **track kilometres** of each station, by projecting it onto the
#     longest shape polyline either direction of the line uses and taking the
#     arc length to the nearest point. Great-circle hops station to station
#     would be a couple of per cent short through the Berkeley hills and the
#     tube; the shape is the real alignment. Every station on every line landed
#     within 69 m of its shape, and every line came out monotonic, which is the
#     check that the shape picked was the right one;
#   * a **trip_id -> line** table, used only as a hint and verified against the
#     live stop list before it is believed. See _line_of() in ftdata.py.
# --------------------------------------------------------------------------

# Seven characters is what the gutter holds. Mechanical truncation gives
# "SAN FRA" and "EL CERR", so the ones that matter are named by hand.
_SHORT = {
    "12TH": "12TH ST", "16TH": "16TH ST", "19TH": "19TH ST", "24TH": "24TH ST",
    "ANTC": "ANTIOCH", "ASHB": "ASHBY", "BALB": "BALBOA", "BAYF": "BAYFAIR",
    "BERY": "BERYESA", "CAST": "CASTROV", "CIVC": "CIVIC", "COLM": "COLMA",
    "COLS": "COLISEM", "CONC": "CONCORD", "DALY": "DALYCTY", "DBRK": "BERKLEY",
    "DELN": "ELCERRN", "DUBL": "DUBLIN", "EMBR": "EMBARC", "FRMT": "FREMONT",
    "FTVL": "FRUITVL", "GLEN": "GLENPRK", "HAYW": "HAYWARD", "LAFY": "LAFAYET",
    "LAKE": "LAKEMER", "MCAR": "MACARTH", "MLBR": "MILLBRA", "MLPT": "MILPITA",
    "MONT": "MONTGMY", "NBRK": "NBERKLY", "NCON": "NCONCRD", "OAKL": "OAKAIRP",
    "ORIN": "ORINDA", "PCTR": "PITTCTR", "PHIL": "PLSNTHL", "PITT": "PITTSBG",
    "PLZA": "ELCERRP", "POWL": "POWELL", "RICH": "RICHMND", "ROCK": "ROCKRDG",
    "SANL": "SANLEAN", "SBRN": "SANBRNO", "SFIA": "SFO", "SHAY": "SHAYWRD",
    "SSAN": "SOUTHSF", "UCTY": "UNIONCY", "WARM": "WARMSPR", "WCRK": "WLNUTCK",
    "WDUB": "WDUBLIN", "WOAK": "WOAKLND",
}

# (reference route, opposite route). The reference is the direction the panel
# calls "down": increasing station index and increasing kilometres.
_BAKE_LINES = (
    ("yellow", "YELLOW", (255, 236, 64), "1", "2"),
    ("orange", "ORANGE", (255, 153, 51), "4", "3"),
    ("green", "GREEN", (72, 200, 96), "5", "6"),
    ("red", "RED", (255, 72, 72), "7", "8"),
    ("blue", "BLUE", (72, 168, 236), "11", "12"),
)

_R_EARTH = 6371.0088


def bake_lines(source, out=ASSET):
    """Read a GTFS zip (path or URL) and write the baked asset. Offline tool."""
    import collections
    import csv
    import io
    import zipfile

    if "://" in source:
        import urllib.request
        with urllib.request.urlopen(source, timeout=120) as resp:
            blob = resp.read()
        zf = zipfile.ZipFile(io.BytesIO(blob))
    else:
        zf = zipfile.ZipFile(source)

    def table(name):
        return list(csv.DictReader(
            io.TextIOWrapper(zf.open(name), "utf-8-sig")))

    stops = {s["stop_id"]: s for s in table("stops.txt")}
    platform = {}
    for s in stops.values():
        if s.get("location_type") == "0":
            platform.setdefault(s["parent_station"], s)
    trips = table("trips.txt")
    by_id = {t["trip_id"]: t for t in trips}

    seq = collections.defaultdict(list)
    for r in table("stop_times.txt"):
        seq[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"]))
    for k in seq:
        seq[k].sort()

    shapes = collections.defaultdict(list)
    for r in table("shapes.txt"):
        shapes[r["shape_id"]].append((int(r["shape_pt_sequence"]),
                                      float(r["shape_pt_lat"]),
                                      float(r["shape_pt_lon"])))
    for k in shapes:
        shapes[k].sort()

    def stations(trip_id):
        """Parent stations in order, collapsing SFO's two platforms into one."""
        out_ = []
        for _, sid in seq[trip_id]:
            p = stops[sid]["parent_station"]
            if not out_ or out_[-1] != p:
                out_.append(p)
        return out_

    keys, names, rgbs, offs = [], [], [], [0]
    code_a, label_a, name_a, km_a = [], [], [], []
    line_index = {}
    for li, (key, label, rgb, ref, opp) in enumerate(_BAKE_LINES):
        pool = [t for t in trips if t["route_id"] in (ref, opp)]
        best = max(pool, key=lambda t: len(seq[t["trip_id"]]))
        order = stations(best["trip_id"])
        if best["route_id"] == opp:
            order = order[::-1]

        # The longest shape either direction uses is the one that covers the
        # whole line; the reference direction's own longest can stop short (the
        # Antioch-SFO shape has no Millbrae on it, and Millbrae is a terminal).
        sid = max(set(t["shape_id"] for t in pool if t["shape_id"]),
                  key=lambda s: len(shapes[s]))
        pts = shapes[sid]
        lat0 = sum(p[1] for p in pts) / len(pts)
        cosl = math.cos(math.radians(lat0))

        def xy(lat, lon):
            return (math.radians(lon) * cosl * _R_EARTH,
                    math.radians(lat) * _R_EARTH)

        poly = [xy(p[1], p[2]) for p in pts]
        cum = [0.0]
        for i in range(1, len(poly)):
            cum.append(cum[-1] + math.hypot(poly[i][0] - poly[i - 1][0],
                                            poly[i][1] - poly[i - 1][1]))

        def project(lat, lon):
            q = xy(lat, lon)
            bd, bs = 1e18, 0.0
            for i in range(len(poly) - 1):
                ax, ay = poly[i]
                dx, dy = poly[i + 1][0] - ax, poly[i + 1][1] - ay
                ll = dx * dx + dy * dy
                u = 0.0 if ll <= 0 else max(0.0, min(
                    1.0, ((q[0] - ax) * dx + (q[1] - ay) * dy) / ll))
                d = (q[0] - ax - u * dx) ** 2 + (q[1] - ay - u * dy) ** 2
                if d < bd:
                    bd, bs = d, cum[i] + u * math.sqrt(ll)
            return bs, math.sqrt(bd) * 1000.0

        dist, err = [], 0.0
        for code in order:
            s = platform[code]
            d, e = project(float(s["stop_lat"]), float(s["stop_lon"]))
            dist.append(d)
            err = max(err, e)
        # The shape may run the other way round; kilometres are always measured
        # from index zero of the station order.
        dist = ([d - dist[0] for d in dist] if dist[-1] > dist[0]
                else [dist[0] - d for d in dist])
        mono = all(dist[i] < dist[i + 1] for i in range(len(dist) - 1))
        print("%-7s %2d stations  %6.1f km  shape %-10s max off %4.0f m  %s"
              % (key, len(order), dist[-1], sid, err,
                 "monotonic" if mono else "NOT MONOTONIC"))
        if not mono:
            raise ValueError("%s: stations are not in order along %s"
                             % (key, sid))

        line_index[key] = (li, {c: i for i, c in enumerate(order)}, len(order))
        keys.append(key)
        names.append(label)
        rgbs.append(rgb)
        for i, code in enumerate(order):
            code_a.append(code)
            label_a.append(_SHORT.get(code, platform[code]["stop_name"]
                                      .upper()[:7]))
            name_a.append(platform[code]["stop_name"])
            km_a.append(dist[i])
        offs.append(len(code_a))

    # Platform stop id -> parent station code, for the fetcher: the realtime
    # feed talks in platforms ("M50-1") and everything here talks in stations.
    sid_a = [s["stop_id"] for s in stops.values() if s.get("location_type") == "0"]
    sid_code = [stops[s]["parent_station"] for s in sid_a]

    # trip_id -> line, direction and terminal station. A hint only; the fetcher
    # checks it against the live stops before believing it, so a table left over
    # from last quarter's schedule degrades rather than lies.
    t_id, t_line, t_dir, t_last = [], [], [], []
    for tid, t in by_id.items():
        st = stations(tid)
        if len(st) < 2:
            continue
        for key, (li, index, n) in line_index.items():
            if all(c in index for c in st):
                a, b = index[st[0]], index[st[-1]]
                if a == b:
                    continue
                t_id.append(tid)
                t_line.append(li)
                t_dir.append(0 if b > a else 1)
                t_last.append(b)
                break

    version = ""
    try:
        fi = table("feed_info.txt")
        version = "%s %s" % (fi[0].get("feed_version", ""),
                             fi[0].get("feed_start_date", ""))
    except Exception:                                        # noqa: BLE001
        pass

    np.savez_compressed(
        out,
        line_key=np.array(keys), line_name=np.array(names),
        line_rgb=np.array(rgbs, np.uint8), line_off=np.array(offs, np.int32),
        st_code=np.array(code_a), st_label=np.array(label_a),
        st_name=np.array(name_a), st_km=np.array(km_a, np.float32),
        sid=np.array(sid_a), sid_code=np.array(sid_code),
        trip_id=np.array(t_id), trip_line=np.array(t_line, np.int8),
        trip_dir=np.array(t_dir, np.int8), trip_last=np.array(t_last, np.int16),
        version=np.array([version]))
    print("wrote %s  (%d stations, %d platforms, %d trips, %.1f KB)  %s"
          % (out, len(code_a), len(sid_a), len(t_id),
             os.path.getsize(out) / 1024.0, version))


def main():
    # The bake is not a demo option: it has to happen instead of a frame loop,
    # not inside one, and megademo must never see it in add_arguments().
    if "--bake-lines" in sys.argv:
        i = sys.argv.index("--bake-lines")
        bake_lines(sys.argv[i + 1] if len(sys.argv) > i + 1 else
                   "https://www.bart.gov/dev/schedules/google_transit.zip")
        return
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
