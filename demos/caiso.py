#!/usr/bin/env python3
"""What California is running on, right now, and what it has run on today.

A room full of people at a makerspace pulling amps out of a wall socket, and a
panel above them saying where those amps came from. Across 320 columns, one
whole day of the California grid at five-minute resolution: a stacked area of
what generated the state's electricity from midnight to this minute, the
evening's forecast demand sketched in ahead of it, and three numbers big enough
to read from the far bench -- how much the state is drawing in gigawatts, what
fraction of it is carbon-free, and what a kilowatt-hour of it costs in grams of
CO2 at this moment.

The shape it draws is the duck. Solar comes up over the Central Valley around
six, climbs to a third of the state's supply by noon, and drops out again in
three hours at teatime while everybody gets home and turns things on -- and
something has to fill that hole. Watching what fills it is the entire point of
the panel, and on most evenings the answer is gas.

**Ten fuels in sixty-four rows is mush, so they are grouped -- five bands and a
lane.** CAISO publishes thirteen columns. Six of them are smaller than one row
of LED at this scale: geothermal is 1.4 rows, small hydro is half a row, biogas
is a third, coal and "other" are flat zero in California and biomass rounds to
nothing. Drawn faithfully they are a band of dither noise between two real
bands, and every one of them steals a boundary the eye has to resolve. So they
are grouped by the only thing that matters at a glance -- **is it clean, and
does it move** -- and the grouping is the one editorial decision in this file:

  * **NUC HYD GEO**, bottom, deep teal. Diablo Canyon, the hydro fleet and the
    geysers: carbon-free, and near enough flat over a day that it reads as the
    floor everything else is stacked on. Putting the steady thing at the bottom
    is what lets the eye read every band above it as a thickness rather than as
    a wobble.
  * **SOLAR**, lime. Its own band, always, because it is the story.
  * **WIND**, cyan. Its own band because it runs on a different clock from
    solar -- often strongest overnight -- and folding the two into one
    "renewables" band would hide exactly that.
  * **IMPORTS**, slate violet. Kept separate and deliberately colourless
    because *nobody knows what it is*: it is whatever the Pacific Northwest and
    the Southwest happened to be selling, hydro and coal and gas together, and
    counting it as clean or as dirty would both be inventions. It is counted in
    the denominator of the carbon-free figure and never in the numerator, which
    makes that figure a floor rather than a guess.
  * **BURNED**, rust orange, on top. Natural gas, plus coal, biomass, biogas
    and the unclassified remainder -- everything that is on fire. Warm against
    four cool bands, so "how much of this is combustion" is answered by colour
    from across the room before any number is read. It is on top because it is
    the swing: the band that thickens as the sun goes, which is the duck.
  * **BATTERY**, its own signed lane under the chart, because it is the only
    quantity here that goes negative and stacking a negative number is
    nonsense. Above the line the fleet is discharging, below it is charging.
    In 2026 this is the most interesting line on the panel -- California now
    soaks up several gigawatts of midday solar and hands it back at the peak,
    and that trade is visible as two lobes either side of the afternoon.

**Carbon-free share** is (solar + wind + geothermal + hydro + nuclear) over
everything supplying the state, imports included. Biomass and biogas are not in
the numerator: they are renewable, they are not carbon-free, and this panel
sits over a room that would rather be told the truth.

**Motion, because a still picture next to animated demos reads as a crash.**
Three things, all of them cheap and none of them decorative. The day *reveals*
left to right when the segment starts, which is the day being replayed at a
couple of seconds a day. A slow sheen travels across the drawn part every few
seconds, lifting what it crosses towards white and leaving black alone. And
the now-line breathes with a pulse running up it, which is the one thing on the
panel guaranteed to move in every single frame: the sheen spends half a second
off the right-hand edge between passes and the breath is an integer brightness
that rounds the same way several frames running, so without the pulse the panel
holds one frame for four hundred milliseconds twice a minute. Nothing here
re-lays out the picture; see the budget.

**The now-line is the edge of the data, not the wall clock**, and it carries
its own time label. If the fetcher stopped an hour ago the line stops an hour
short of where the clock says it should be, the gap is visible, and the header
says STALE. A chart drawn to the right edge when the data ran out at breakfast
is the exact lie this demo could tell.

**Nothing here touches the network.** `build()` calls `ftdata.load()`, which
reads one JSON file, and that is all. The fetcher is a separate process on a
timer -- `ftsched` builds the next segment on a worker thread, Python threads
share the GIL, and a `build()` blocked on a socket does not merely wait, it
stops the render loop getting the interpreter back:

    $ python3 ftdata.py --loop 900

The source is CAISO's "Today's Outlook", three keyless five-minute CSVs; see
ftdata.py for the endpoints and for why the timestamps are resolved against
America/Los_Angeles explicitly. Records past their hour-long TTL still draw --
this morning's curve is still this morning's curve -- with the age and a STALE
flag on the panel. A record from *yesterday*, a corrupt one, or none at all,
gets a no-data card and no chart, because a day-shaped picture of the wrong day
is worse than an empty rectangle.

**Frame budget.** A 20 fps segment is 50 ms a frame on the wall's Pi 3, which
is under-voltage throttled to 600 MHz -- half its rated clock, and 76-114x
slower than the desktop this was written on. Everything is baked in `build()`:
the stack, the battery lane, the gridlines, the legend, the forecast trace and
the now-line's label are rasterised once into two uint8 frames. `render()` does
one full-frame copy, a multiply and an add over a thirty-four column window for
the sheen, and writes three short columns. That is six or seven numpy calls,
and numpy costs 55-80 us a call on the Pi *whatever the array size*, so the
call count is the budget and not the pixel count. Measured over two thousand
frames here: **p50 0.022 ms, p95 0.026 ms, p99 0.032 ms**, worst frame
0.062 ms. At the measured 114x that is p95 3.0 ms on the Pi against a 50 ms
budget; even at 200x it would not be close. `build()` is 1.8-3.7 ms here, so
under half a second there, once, on the worker thread.

The one thing that had to be moved out of the frame loop was the header. Every
number on it comes out of the record, so it can only change when the record
does -- but finding that out means formatting four strings and walking the
ladder of shorter forms, which was a third of a millisecond a frame here and
would have been the most expensive thing in this file on the wall. It is baked
with the chart now, and `render()` never formats anything.

Run:  python3 ftdata.py --once --only caiso-mix
      python3 caiso.py --host 127.0.0.1
      python3 caiso.py --at '2026-08-09 19:30'      # pretend it is teatime
      FT_DATA_CACHE=/tmp/empty python3 caiso.py     # the no-data card
      python3 scripts/test-caiso.py
"""

import math
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

PRODUCT = "caiso-mix"

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one propagation, sort and tide draw
# with: five rows a glyph, each row an octal digit whose three bits are the
# three columns. Anything from a real typeface is mush at five pixels and the
# Pi does not have the same faces installed as the machine this was written on.
# Two glyphs are added for the two things a meter needs and a map of a nuclear
# exchange does not.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT.update({"%": "51245", "+": "02720"})

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
# The grouping. Bottom of the stack first; see the docstring on why these five.
#
# Every published column appears exactly once, including the ones that are zero
# in California today -- coal has been zero all year and "other" is the
# unclassified thermal remainder, but a column that quietly vanished from the
# stack the day CAISO started reporting it would be a silent lie about the
# total, and the total is the top edge of the chart.
# --------------------------------------------------------------------------

GROUPS = (
    ("firm", "NUC HYD GEO",
     ("nuclear", "geothermal", "large_hydro", "small_hydro"), (22, 104, 124)),
    ("solar", "SOLAR", ("solar",), (168, 226, 62)),
    ("wind", "WIND", ("wind",), (66, 196, 232)),
    ("imports", "IMPORTS", ("imports",), (118, 112, 152)),
    ("burned", "BURNED",
     ("natural_gas", "coal", "biomass", "biogas", "other"), (206, 92, 38)),
)

# Which groups count towards carbon-free. Not `not burned`: imports are
# excluded on purpose, because their mix is unknown and guessing it either way
# would be the one number on this panel that could not be defended.
CLEAN_GROUPS = ("firm", "solar", "wind")

C_BG = (0, 0, 0)
C_TEXT = (198, 210, 222)
C_DIM = (84, 96, 110)
C_GRID = (20, 26, 34)
C_GRID_NOON = (40, 50, 62)
C_SEP = (14, 18, 24)
C_FORECAST = (96, 88, 70)            # day-ahead demand: warm, unlit, a sketch
C_NOW = (255, 246, 214)
C_NOW_HALO = (60, 58, 48)
C_BATT_OUT = (150, 250, 200)         # discharging: giving back
C_BATT_IN = (86, 100, 216)           # charging: taking away
C_BATT_MID = (34, 40, 50)
C_WARN = (255, 96, 72)
C_CLEAN_HI = (140, 232, 120)
C_CLEAN_MID = (226, 206, 96)
C_CLEAN_LO = (232, 138, 70)
C_REVEAL = (222, 236, 255)

# How often the pulse runs up the now-line, in hertz. Slow enough to read as
# one thing travelling rather than as flicker, fast enough that it moves more
# than a row between frames at twenty a second.
PULSE_HZ = 1.3

# The clean-share thresholds the headline colour steps at. Round numbers rather
# than anything derived: this is a traffic light, and a traffic light whose
# boundaries move with the data is not one.
CLEAN_HI, CLEAN_MID = 0.60, 0.40


def clean_colour(share):
    if share is None:
        return C_DIM
    if share >= CLEAN_HI:
        return C_CLEAN_HI
    return C_CLEAN_MID if share >= CLEAN_MID else C_CLEAN_LO


# --------------------------------------------------------------------------
# Clock. Everything asks for `now` rather than reading the system clock, which
# is what makes a contact sheet across a whole day possible: --at moves the
# demo's idea of the present and --rate runs it fast. Same shape as tide.py's.
# --------------------------------------------------------------------------

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


def hhmm(epoch, ampm=True):
    """A compact local-time label: '655P' or '18:55'.

    Local to the display. The wall and the ISO are in the same zone, which is
    the case this exists for; where they are not, the time somebody standing in
    front of the panel can act on is the one on their own watch, and the record
    carries real epochs so the conversion is honest either way.
    """
    lt = time.localtime(epoch)
    if not ampm:
        return time.strftime("%H:%M", lt)
    h = lt.tm_hour % 12 or 12
    return "%d:%02d%s" % (h, lt.tm_min, "A" if lt.tm_hour < 12 else "P")


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises, so everything that
# can still be wrong is wrong about *content* and is caught here. Three states
# have to be drawable and they are different things: missing (no file, or one
# that does not parse as this product), out-of-day (a perfectly good record of
# a day that has ended), and stale (today's record, an hour behind). Only the
# first two stop the chart.
# --------------------------------------------------------------------------

def _fseries(mapping, key):
    values = mapping[key]
    # None means "has not happened yet" all the way through this file. NaN
    # carries that through np.interp without a branch, and every consumer
    # either masks it off or is drawing a column that is not on screen.
    return np.array([np.nan if v is None else float(v) for v in values], f32)


def read_mix(cache_dir):
    """(record, age, problem). `record` is None if there is nothing drawable."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached CAISO record"
    payload, age = got
    try:
        t = np.asarray(payload["t"], np.float64)
        day0, day1 = (float(x) for x in payload["day"])
        fuels = {k: _fseries(payload["fuels"], k)
                 for k in payload["fuel_order"]}
        date = str(payload.get("date", ""))
    except Exception:                                        # noqa: BLE001
        return None, age, "CAISO record is malformed"
    if len(t) < 2 or day1 <= day0:
        return None, age, "CAISO record has no usable series"

    groups = {}
    for name, _label, members, _rgb in GROUPS:
        # A column CAISO has not published today is absent rather than zero, and
        # summing over what did arrive is right: the total is then honestly the
        # sum of what is known, and no band is invented.
        cols = [fuels[m] for m in members if m in fuels]
        if not cols:
            groups[name] = np.zeros(len(t), f32)
            continue
        stack = np.nan_to_num(np.stack(cols), nan=0.0)
        # Negative generation is real and is not a mistake -- solar reads a few
        # tens of megawatts *negative* at night, which is plant auxiliary load
        # -- but a negative thickness in a stacked area is not a thing that can
        # be drawn, so the band is clamped and the number it clamps away is two
        # hundredths of a row.
        groups[name] = np.clip(stack.sum(0), 0.0, None)

    batt = np.nan_to_num(fuels.get("batteries", np.zeros(len(t), f32)), nan=0.0)

    demand = None
    dem = payload.get("demand")
    if isinstance(dem, dict) and dem.get("t"):
        try:
            demand = {
                "t": np.asarray(dem["t"], np.float64),
                "day_ahead": _fseries(dem["series"], "day_ahead_forecast"),
                "current": _fseries(dem["series"], "current_demand"),
            }
        except Exception:                                    # noqa: BLE001
            demand = None

    co2_total = None
    co2 = payload.get("co2")
    if isinstance(co2, dict) and co2.get("t"):
        try:
            rows = [_fseries(co2["series"], k) for k in co2["order"]]
            co2_total = {"t": np.asarray(co2["t"], np.float64),
                         "v": np.nan_to_num(np.stack(rows), nan=0.0).sum(0)}
        except Exception:                                    # noqa: BLE001
            co2_total = None

    return {"t": t, "day": (day0, day1), "date": date, "age": age,
            "groups": groups, "batt": batt, "fuels": fuels,
            "demand": demand, "co2": co2_total,
            "latest": float(t[-1])}, age, None


def clean_share(rec, i=-1):
    """Carbon-free fraction of everything supplying the state at sample `i`.

    Battery discharge is in the denominator and not the numerator, for the same
    reason imports are: the electricity in it came from somewhere, and that
    somewhere was several hours ago. Charging is excluded from both -- it is
    load, not supply, and putting a negative number in a denominator is how a
    share ends up over one hundred per cent on a sunny afternoon.
    """
    total = sum(float(rec["groups"][n][i]) for n, _l, _m, _c in GROUPS)
    total += max(0.0, float(rec["batt"][i]))
    if total <= 0.0:
        return None
    clean = sum(float(rec["groups"][n][i]) for n in CLEAN_GROUPS)
    return clean / total


def supply_mw(rec, i=-1):
    """Gross supply in MW at sample `i`: everything generated or imported.

    This is the top edge of the stack, and it is *not* demand -- on a sunny
    afternoon it runs several gigawatts above it, because the difference is
    going into batteries. That gap is the point of the lane underneath.
    """
    total = sum(float(rec["groups"][n][i]) for n, _l, _m, _c in GROUPS)
    return total + max(0.0, float(rec["batt"][i]))


def demand_mw(rec):
    """What the state is actually drawing, in MW, at the leading edge.

    CAISO's own figure where there is one, because it is the measurement and
    anything computed here is an arithmetic reconstruction of it. The fallback
    is that reconstruction -- supply less whatever the batteries are taking --
    and it lands within a couple of hundred megawatts, the difference being
    losses and the pump load the fuel mix does not itemise.
    """
    dem = rec["demand"]
    if dem is not None:
        v = dem["current"]
        ok = np.flatnonzero(np.isfinite(v))
        if len(ok):
            # The last *reported* value, which can be a sample or two behind
            # the fuel mix; that is CAISO's publishing order, not a fault.
            return float(v[ok[-1]])
    return sum(float(rec["groups"][n][-1]) for n, _l, _m, _c in GROUPS) \
        + float(rec["batt"][-1])


def intensity_g_kwh(rec, demand):
    """Grams of CO2 per kilowatt-hour being delivered right now, or None.

    CAISO publishes the emissions themselves, in metric tons an hour by source,
    so this is a division and not a model: tons an hour over megawatts is tons
    per megawatt-hour, and a ton per megawatt-hour is a thousand grams per
    kilowatt-hour. It is the one figure here in the units an electricity bill
    is in, and it moves the opposite way to the carbon-free share, which is a
    useful thing for two numbers on a wall to do.
    """
    if rec["co2"] is None or not demand or demand <= 0:
        return None
    v = rec["co2"]["v"]
    if not len(v):
        return None
    return float(v[-1]) / float(demand) * 1000.0


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--reveal", type=float, default=2.2,
                    help="seconds the day takes to draw itself in (0 = off)")
    ap.add_argument("--sweep", type=float, default=6.0,
                    help="seconds between sheen passes across the chart "
                         "(0 = off)")
    ap.add_argument("--sweep-width", type=int, default=34,
                    help="columns the sheen is wide")
    ap.add_argument("--sweep-gain", type=float, default=0.55,
                    help="how far towards white the sheen lifts what it crosses")
    ap.add_argument("--peak", type=float, default=0.0,
                    help="full scale in GW (0 = fit the day, rounded up)")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second")
    ap.add_argument("--reload", type=float, default=600.0,
                    help="seconds between re-reads of the cache (0 = never)")


# --------------------------------------------------------------------------
# Layout. Five regions down a 64-row panel, and every one of them earns its
# rows: without the legend the bands are five anonymous colours, and without
# the battery lane the most interesting thing on the California grid in 2026 is
# not on the panel at all. What gives way first on a shorter panel is the
# battery lane, then the legend, because the stack is the demo.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.head_h = 6 if h >= 24 else 0
        self.legend_h = 5 if (h >= 44 and w >= 220) else 0
        self.batt_h = 7 if h >= 52 else (5 if h >= 40 else 0)
        self.chart_y = self.head_h + (1 if self.head_h else 0)
        used = self.chart_y + self.batt_h + (self.legend_h + 1
                                             if self.legend_h else 0)
        self.chart_h = h - used
        while self.chart_h < 8 and (self.batt_h or self.legend_h):
            # Give the lane up before the legend: a five-band chart nobody can
            # name is worth less than a chart with no battery on it.
            if self.batt_h:
                self.batt_h = 0
            else:
                self.legend_h = 0
            used = self.chart_y + self.batt_h + (self.legend_h + 1
                                                 if self.legend_h else 0)
            self.chart_h = h - used
        self.chart_h = max(2, self.chart_h)
        self.batt_y = self.chart_y + self.chart_h
        self.legend_y = h - self.legend_h if self.legend_h else h

    @property
    def chart_bot(self):
        return self.chart_y + self.chart_h - 1


# --------------------------------------------------------------------------
# Baking the picture. All of this happens once per cache read.
# --------------------------------------------------------------------------

def column_times(day, w):
    """The epoch at the centre of each of `w` columns across the day."""
    day0, day1 = day
    return day0 + (np.arange(w, dtype=np.float64) + 0.5) * (day1 - day0) / w


def resample(t, v, col_t):
    """A 5-minute series onto panel columns. np.interp, clamped at the ends."""
    return np.interp(col_t, t, v).astype(f32)


def full_scale(rec, col_t, peak_gw=0.0):
    """Megawatts at the top of the chart.

    Rounded up to a whole five gigawatts so the horizontal gridlines land on
    round numbers, and so the axis does not creep by half a row every time the
    peak nudges up -- an axis that rescales itself every five minutes makes the
    morning look different at teatime for no reason.
    """
    if peak_gw > 0:
        return peak_gw * 1000.0
    total = sum(rec["groups"][n] for n, _l, _m, _c in GROUPS)
    top = float(np.nanmax(total)) if len(total) else 0.0
    if rec["demand"] is not None:
        fc = rec["demand"]["day_ahead"]
        if np.isfinite(fc).any():
            top = max(top, float(np.nanmax(fc)))
    return max(20000.0, math.ceil(top / 5000.0) * 5000.0)


def draw_furniture(dst, lay, rec, scale, col_t, h24):
    """Gridlines, the day-ahead forecast trace and the legend.

    Everything that is there whether or not the day has happened yet. This is
    the frame the reveal wipes *over*, so the panel is never an empty rectangle
    even in the first second of a segment.
    """
    w = lay.w
    top, bot = lay.chart_y, lay.chart_bot
    day0, day1 = rec["day"]

    # Vertical rules every three hours, with noon brighter. Three hours is the
    # coarsest spacing that still puts a line inside each lobe of the duck; six
    # would leave the whole afternoon unmarked.
    for hour in range(0, 25, 3):
        c = int(hour / 24.0 * w)
        if c >= w:
            continue
        dst[top:bot + 1, c] = C_GRID_NOON if hour == 12 else C_GRID

    # Horizontal rules every ten gigawatts. Unlabelled on purpose: the headline
    # carries the magnitude and five pixels of type inside the chart would land
    # on top of the stack half the day.
    for mw in range(10000, int(scale) + 1, 10000):
        r = int(round(bot - mw / scale * (bot - top)))
        if top <= r <= bot:
            dst[r] = np.maximum(dst[r], np.array(C_GRID, np.uint8))

    # The day-ahead forecast, dotted, across the whole day. It is the only
    # thing that knows what the evening looks like, and it is drawn behind the
    # stack so that where the day has happened the measurement wins.
    if rec["demand"] is not None:
        fc = resample(rec["demand"]["t"], rec["demand"]["day_ahead"], col_t)
        ok = np.isfinite(fc)
        rows = np.clip(np.round(bot - fc / scale * (bot - top)), top, bot)
        cols = np.arange(w)
        # Every other column: a solid line here would read as a second band
        # boundary, and the point of it is that it has not happened yet.
        sel = ok & (cols % 2 == 0)
        dst[rows[sel].astype(int), cols[sel]] = C_FORECAST

    if lay.head_h:
        dst[lay.head_h - 1] = C_SEP

    if lay.legend_h:
        draw_legend(dst, lay, rec, h24)


def draw_legend(dst, lay, rec, h24):
    """Colour chips and names, left, with the source and the date on the right.

    The chips are what make the five bands mean anything, and they are the
    reason the labels are words rather than the fuel names: 'NUC HYD GEO' says
    what is in the band, and a band called 'FIRM' says only that whoever drew it
    had a word for it.
    """
    y = lay.legend_y
    x = 1
    entries = [(label, rgb) for _n, label, _m, rgb in GROUPS]
    if lay.batt_h:
        entries.append(("BATTERY", None))
    for label, rgb in entries:
        need = 4 + 2 + text_width(label)
        if x + need > lay.w - 2:
            break
        if rgb is None:
            # One chip, two colours, because the lane has two: the split says
            # that up and down are different things without spending a second
            # entry on it.
            dst[y:y + 2, x:x + 4] = C_BATT_OUT
            dst[y + 3:y + 5, x:x + 4] = C_BATT_IN
        else:
            dst[y:y + 5, x:x + 4] = rgb
        blit_text(dst, y, x + 6, label, C_DIM)
        x += need + 6

    tag = "CAISO %s" % time.strftime("%-m/%-d", time.localtime(rec["day"][0]))
    tw = text_width(tag)
    if x + 6 + tw <= lay.w - 1:
        blit_text(dst, y, lay.w - tw - 1, tag, C_GRID_NOON)


def draw_stack(dst, lay, rec, scale, col_t, n_cols):
    """The stacked area, bottom band first, over the columns that have happened.

    One boolean mask per band over the chart region rather than a column loop:
    three hundred and twenty columns of Python is a tenth of a second on the Pi
    even once, and this runs again every time the cache is re-read.
    """
    w = lay.w
    top, bot = lay.chart_y, lay.chart_bot
    reg = dst[top:bot + 1]
    rows = bot - top
    yy = np.arange(rows + 1)[:, None]
    have = np.arange(w)[None, :] < n_cols

    cum = np.zeros(w, f32)
    lower = np.full(w, float(rows), f32)          # row of the running total
    for name, _label, _members, rgb in GROUPS:
        cum = cum + resample(rec["t"], rec["groups"][name], col_t)
        upper = np.clip(rows - cum / scale * rows, 0.0, rows)
        band = (yy >= upper[None, :]) & (yy < lower[None, :]) & have
        reg[band] = rgb
        lower = upper

    # The top edge, one row of the band's own colour at full brightness. A
    # stacked area whose top edge is the same value as its fill has no
    # silhouette, and the silhouette is the demand curve.
    edge = np.clip(np.round(lower), 0, rows).astype(int)
    cols = np.arange(w)[:n_cols]
    lit = (cum[:n_cols] > 0)
    reg[edge[:n_cols][lit], cols[lit]] = C_TEXT


def draw_battery(dst, lay, rec, col_t, n_cols):
    """The signed battery lane: discharge above the line, charge below it.

    Scaled to the day's own extreme rather than to the chart's full scale --
    six gigawatts of battery against a thirty gigawatt axis is two rows, and
    two rows cannot show a shape. The lane is therefore not to the same scale as
    the chart above it, which is why it is a separate lane with its own rule
    through the middle rather than a band in the stack.
    """
    if not lay.batt_h:
        return
    y0 = lay.batt_y
    mid = y0 + lay.batt_h // 2
    half = lay.batt_h // 2
    dst[mid] = C_BATT_MID

    v = resample(rec["t"], rec["batt"], col_t)
    peak = float(np.nanmax(np.abs(v))) if len(v) else 0.0
    peak = max(peak, 1000.0)
    n = np.clip(np.round(np.abs(v) / peak * half), 0, half).astype(int)

    yy = np.arange(lay.batt_h)[:, None] + y0
    have = np.arange(lay.w)[None, :] < n_cols
    up = (yy < mid) & (yy >= (mid - n)[None, :]) & have & (v > 0)[None, :]
    down = (yy > mid) & (yy <= (mid + n)[None, :]) & have & (v < 0)[None, :]
    reg = dst[y0:y0 + lay.batt_h]
    reg[up] = C_BATT_OUT
    reg[down] = C_BATT_IN


def draw_now(dst, lay, rec, col, h24):
    """The data's leading edge, and the time it stopped at.

    Baked, apart from the line itself, which breathes in render(). The label is
    the only calibration on the horizontal axis and it is deliberately the
    *data's* time rather than the clock's: when those differ, the difference is
    the point.
    """
    label = hhmm(rec["latest"], not h24)
    tw = text_width(label)
    x = col + 3
    if x + tw > lay.w - 1:
        x = col - 3 - tw
    x = max(1, min(x, lay.w - tw - 1))
    blit_text(dst, lay.chart_y + 1, x, label, C_DIM)


# --------------------------------------------------------------------------
# The header. Three numbers and an age, in the ladder-of-shorter-forms shape
# tide.py uses: the widest set that fits is the one drawn, because simply
# clipping the line loses whatever falls off the end, and what falls off the
# end of this one is the part that says how old the data is. The order the
# rungs are written in is the order things are given up in -- carbon intensity
# first, then the word CARBON, and the age last of all.
# --------------------------------------------------------------------------

def header_text(state, w=ds.WIDTH):
    rec = state["rec"]
    if rec is None:
        return "NO GRID DATA", "", C_WARN, ""

    gw = state["demand"] / 1000.0
    lefts = ["%.1f GW" % gw, "%.0fGW" % gw]
    if state.get("intensity") is not None:
        lefts.insert(0, "%.1f GW  %d G/KWH" % (gw, round(state["intensity"])))

    share = state["clean"]
    if share is None:
        mids, midc = ["", ""], C_DIM
    else:
        pct = int(round(share * 100.0))
        midc = clean_colour(share)
        mids = ["%d%% CARBON FREE" % pct, "%d%% CLEAN" % pct, "%d%%" % pct]

    age = ftdata.describe_age(rec["age"])
    rights = ["DATA " + age, age, ""]
    if state["stale"]:
        rights = ["STALE " + r if r else "STALE" for r in rights]

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
    dst[lay.head_h - 1] = C_SEP


def draw_nodata(dst, lay, lines):
    """The honest panel. No chart, no bands, no implied mix."""
    dst[:] = (6, 6, 8)
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

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h)
    cache = args.cache_dir
    now_of = clock(parse_when(args.at), args.rate)

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)      # the whole panel, day drawn
    base = np.zeros((h, w, 3), np.uint8)        # the same, with no day on it

    # The sheen. It lifts what it crosses *towards white*, and only where
    # something is already drawn -- which is the whole trick, and the second
    # attempt at it. The first multiplied by a gain, which is cheaper and looks
    # right in the arithmetic: black stays black, lit pixels get brighter.
    # Except that four of the five band colours are already saturated in at
    # least one channel, so multiplying them clips and the sweep was invisible
    # over exactly the part of the panel it exists to animate. Lifting towards
    # white moves a saturated orange as far as it moves a dim teal.
    #
    # `delta` is baked with the chart: the per-frame cost is then one multiply
    # and one add over a thirty-four column window, and no mask lookup, because
    # delta is already zero everywhere the panel is black.
    #
    # A raised cosine rather than a box: a hard-edged bright block travelling
    # across a chart reads as a rendering fault, and the same brightness with a
    # soft edge reads as a sweep.
    sw = max(2, int(args.sweep_width))
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0, 2 * math.pi, sw, dtype=f32)))
    ramp = (ramp * float(args.sweep_gain)).astype(f32)[None, :, None]
    delta = np.zeros((h, w, 3), f32)
    sheen = np.empty((h, sw, 3), f32)

    cell = {"rec": None, "problem": None, "loaded": -1e18, "stale": False,
            "supply": 0.0, "demand": 0.0, "clean": None, "intensity": None,
            "now_col": 0, "n_cols": 0, "scale": 20000.0}

    def reload_data(now):
        rec, age, problem = read_mix(cache)
        if rec is not None:
            day0, day1 = rec["day"]
            # A record of a day that has ended is not stale, it is *wrong*: the
            # axis is midnight to midnight and drawing yesterday's curve under
            # today's clock puts the evening peak where the morning should be.
            # An hour of slack past midnight, so the panel does not blank
            # itself in the minute before the first fetch of the new day.
            if not (day0 - 60.0 <= now <= day1 + 3600.0):
                problem = "RECORD IS FROM %s" % (rec["date"] or "ANOTHER DAY")
                rec = None
        cell["rec"], cell["problem"], cell["loaded"] = rec, problem, now
        if rec is None:
            cell["stale"] = False
            return

        cell["stale"] = not ftdata.is_fresh(PRODUCT, age)
        col_t = column_times(rec["day"], w)
        scale = full_scale(rec, col_t, args.peak)
        cell["scale"] = scale
        # Columns the data actually reaches. Everything to the right of this is
        # the rest of today and is left empty on purpose.
        span = rec["day"][1] - rec["day"][0]
        n_cols = int(np.clip(math.ceil((rec["latest"] - rec["day"][0])
                                       / span * w), 1, w))
        cell["n_cols"] = n_cols
        cell["now_col"] = min(w - 1, n_cols - 1)
        cell["supply"] = supply_mw(rec)
        cell["demand"] = demand_mw(rec)
        cell["intensity"] = intensity_g_kwh(rec, cell["demand"])
        cell["clean"] = clean_share(rec)

        base[:] = 0
        draw_furniture(base, lay, rec, scale, col_t, args.h24)
        # The header is baked here and not in the frame loop. Every number on
        # it comes out of the record, so it can only change when the record
        # does -- and finding that out costs a `%` format of four strings and a
        # walk down the ladder of shorter forms, which is a third of a
        # millisecond here and thirty on the Pi. Doing that thirty times a
        # second to discover nothing had changed was the most expensive thing
        # in this file before it was moved.
        if lay.head_h:
            draw_header(base, lay, state_of())
        static[:] = base
        draw_stack(static, lay, rec, scale, col_t, n_cols)
        draw_battery(static, lay, rec, col_t, n_cols)
        if lay.head_h:
            draw_now(static, lay, rec, cell["now_col"], args.h24)
        # Everything black stays black; everything lit has somewhere to go.
        # np.multiply(..., out=) and not `*=`: an augmented assignment to a
        # name from the enclosing scope makes it local to this function, which
        # is an UnboundLocalError several lines earlier and not an obvious one.
        np.subtract(255.0, static, out=delta, dtype=f32)
        np.multiply(delta, static.max(axis=2, keepdims=True) > 0, out=delta)

    def state_of():
        """What the header and the tests read. Cheap; it copies no arrays."""
        return {"rec": cell["rec"], "stale": cell["stale"],
                "supply": cell["supply"], "demand": cell["demand"],
                "clean": cell["clean"], "intensity": cell["intensity"]}

    def render(t, i):
        now = now_of()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        if cell["rec"] is None:
            lines = [("NO GRID DATA", C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --LOOP 900", C_TEXT)]
            if cell["problem"]:
                lines.append((str(cell["problem"]).upper()[:52], C_DIM))
            return draw_nodata(frame, lay, lines)

        frame[:] = static

        # The reveal. Two slice copies rather than a mask: the region that has
        # not arrived yet is restored from `base`, which already has the
        # gridlines and the forecast on it, so the day wipes in over a chart
        # rather than over a black hole.
        edge = w
        if args.reveal > 0 and t < args.reveal:
            edge = int(w * (t / args.reveal))
            frame[lay.chart_y:, edge:] = base[lay.chart_y:, edge:]
            if edge < w:
                frame[lay.chart_y:, edge] = C_REVEAL
        elif args.sweep > 0:
            # The sheen, once the day is up. Off-screen either side, so it
            # enters and leaves rather than appearing in the middle.
            phase = ((t - args.reveal) % args.sweep) / args.sweep
            x0 = int(phase * (w + 2 * sw)) - sw
            a, b = max(0, x0), min(w, x0 + sw)
            if b > a:
                buf = sheen[:, :b - a]
                np.multiply(delta[:, a:b], ramp[:, a - x0:b - x0], out=buf)
                np.add(buf, static[:, a:b], out=buf)
                frame[:, a:b] = buf

        # The now-line last, over everything. It breathes, and a short bright
        # pulse runs up it -- which is the only thing on the panel that is
        # guaranteed to move in *every* frame. The sheen spends about half a
        # second off the right-hand edge between passes, and the breath is an
        # integer brightness that at twenty frames a second rounds to the same
        # value for several frames running; without the pulse the panel holds a
        # single frame for four hundred milliseconds twice a minute, which on a
        # wall between two animated demos reads as a crash.
        # Both are driven by the segment's own `t` and not by the wall clock.
        # That is what makes them the same animation on the wall and under a
        # test harness rendering a hundred frames in a millisecond -- with
        # time.time() the harness sees a frozen panel and the wall does not,
        # which is a difference that would only ever be discovered the wrong
        # way round.
        col = cell["now_col"]
        if col < edge:
            blink = 0.55 + 0.45 * math.sin(t * 2.0)
            bot = lay.batt_y + lay.batt_h if lay.batt_h else lay.chart_bot + 1
            frame[lay.chart_y:bot, col] = tuple(int(c * blink) for c in C_NOW)
            if col + 1 < w:
                frame[lay.chart_y:bot, col + 1] = C_NOW_HALO
            py = lay.chart_y + int(((t * PULSE_HZ) % 1.0) * (bot - lay.chart_y))
            frame[max(lay.chart_y, py - 1):py + 2, col] = C_NOW
        return frame

    reload_data(now_of())
    render.state = cell               # tests reach in here; nothing else does
    render.layout = lay
    render.clock = now_of
    render.static = static
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "today's California fuel mix, demand and carbon-free share",
                  fps=20)


if __name__ == "__main__":
    main()
