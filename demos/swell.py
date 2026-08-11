#!/usr/bin/env python3
"""What the Pacific is actually doing, drawn side-on at the speed it is doing it.

`tide.py` next door draws a prediction: a harmonic fit computed years ago that
would print the same curve for this afternoon if every buoy in the ocean were
switched off. This panel draws a **measurement**. Eighteen nautical miles west
of the Golden Gate there is a three-metre discus hull, NDBC station 46026,
which every ten minutes reports how high the sea around it is, how long between
crests, and which way they are running. The wall shows that -- as water, seen
from the side, moving at the speed the water is moving.

**Side-on, because a profile has a vertical axis and a plan view does not.**
The first version of this panel drew the sea from above and encoded wave height
as *contrast*: a big sea swung further through the blue ramp. That is an
encoding, and an encoding needs a legend, and a legend on a 64-row panel is a
sentence nobody walking past will read. In profile the encoding disappears --
the height of the water on the wall **is** the height of the water, against a
fixed scale, with the significant height bracketed and labelled at the left
edge. Nobody needs told what 5FT means when five feet is drawn.

**The rhythm is still the point.** A crest passes any given point on the wall
once every T seconds, T being the period the buoy measured, because the train
is drawn at the wavelength deep water gives it (L = gT^2/2pi) and moved at the
speed that follows (c = L/T). Nine seconds is a rhythm somebody can feel
without reading a digit, and the headline says the number in words -- 5FT WAVES
EVERY 9 SEC -- with a bar across the top of the water marking one crest to the
next so the sentence and the picture are visibly the same claim.

**The surface is a sum, because a sea surface is a sum.** The `.spec` sidecar
splits the sea state into a swell part and a windsea part, each with its own
height, period and direction, and the profile adds them: long groundswell with
short chop riding on its back. That superposition is what "clean" and "blown
out" actually look like, and in profile it is legible in a way the same two
trains crossing in plan view never were. Each part is drawn as three components
a few per cent either side of its measured period rather than as one pure
sinusoid -- a real spectrum has width, and width is why no two crests in the
ocean are the same size. **The individual waves are therefore a rendering, not
a record**: significant height is a statistic (roughly the mean of the highest
third), the buoy never published a list of waves, and this panel does not
pretend it did. What is measured is the height, the rhythm and the split; the
irregularity between one crest and the next is the model saying "and it is not
a sine wave".

**The section is cut along the way the waves are going.** The panel's x axis is
the path of the longest train -- the swell -- and the zoom is a fixed number of
its wavelengths across, so the chop is drawn at its true size *relative to* it
rather than blown up to fill the wall. The chop is projected onto that line,
which lengthens its apparent wavelength by 1/cos of the angle between them
without touching its period: correct for a section across a crossing sea, and
the reason a cross swell shows up as a slow heave under fast chop. Direction has
no natural home in a profile, so it gets an inset: a north tick and an arrow
pointing the way the water is running, with the bearing spelled out beside it.

**The strip along the bottom is the trend**, twenty-four hours of significant
height as a filled area with the dominant period dotted over it, because "5FT"
says nothing about whether that is a swell building for tomorrow or the end of
one. Gaps in it are real: the buoy misses samples, and a line drawn across a
six-hour hole is a claim nobody measured.

**Nothing here touches the network.** `ftdata.py` fetches on a timer in a
process of its own and leaves a 2.7 kB JSON record in a cache; this reads that
and does not import a HTTP library. It has to be that way round -- the
scheduler builds the next segment on a worker thread, Python threads share the
GIL, and a `build()` blocked on a socket does not merely wait, it stops the
render loop getting the interpreter back. The buoy's file is 600 kB and the
fetcher takes the newest sixteen of them with a ranged GET; see ftdata.py.

    $ python3 ftdata.py --once --only ndbc-46026
    $ python3 swell.py --host 127.0.0.1
    $ python3 swell.py --rate 6            # a minute of ocean in ten seconds
    $ FT_DATA_CACHE=/tmp/empty python3 swell.py       # the no-data card
    $ python3 scripts/test-swell.py

**Age is part of the data, twice over.** The fetch age says whether the fetcher
is alive; the observation age says whether the *buoy* is. They are different
failures and they are shown separately, because a buoy can go quiet for a week
while the fetcher happily downloads its silence every ten minutes -- station
46237 on the San Francisco bar was doing exactly that while this was written.
Past an hour and a half the observation age is called out in warning colour;
past half a day the water is not drawn at all, because animating a stale sea
state at a rhythm the ocean is no longer keeping is the one lie this panel
could tell.

**Frame budget.** Everything is baked in `build()`: the header, the trend
strip, the overlay marks, and one row of phase per component. A frame is five
calls to turn the components into a one-dimensional surface -- all of them on
(6, 320) arrays -- four to turn that surface into a band of water through a
depth lookup table, and one scatter for the overlay. Ten numpy calls, none of
which allocate, and the header and strip are never touched at all because they
live in the same buffer and do not change between fetches. Numpy costs tens of
microseconds a call on the wall's Pi whatever the array size, so the call count
is the budget and not the pixel count.
"""

import math
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

STATION = "46026"                       # 18 nm west of the Golden Gate
PRODUCT = "ndbc-"

M_FT = 3.28084
G = 9.80665

# Deep-water dispersion: L = g T^2 / 2pi. The buoy sits in 1400 m of water and
# the panel is a section of open ocean, so the deep-water form is the right one
# -- nothing here is in water shallow enough to shoal.
DEEP = G / (2.0 * math.pi)              # 1.5613 m per second squared

# Full scale for the vertical axis, in significant metres. Four metres would be
# a hurricane; three is a proper storm off this coast and is the number the
# scale is built around. Fixed, never fitted to the day: a panel whose axis
# normalises itself cannot be compared with yesterday's, and comparing is most
# of what anybody wants from it.
HS_FULL = 3.0

# The surface is the sum of two trains, so the tallest crest a full-scale sea
# can reach is not Hs/2 but the sum of the two half-heights, which on a real
# record runs about a fifth over. That is the headroom the vertical scale
# leaves, so a storm fills the band instead of running off the top of it, and
# an ordinary two-metre afternoon still has waves worth looking at.
VERT_HEADROOM = 1.15

# Rows over which the water darkens from the surface to the deep. Fixed rather
# than stretched to the band, because a fade whose length depends on the panel
# size reads as a filled area chart on a tall one: what makes the band look
# like water rather than like a bar is that the light stops a fixed short
# distance under the surface.
DEPTH_FADE = 13

# How many wavelengths of the longest train to fit across the panel. The
# alternative -- a fixed patch of ocean in metres -- draws a twenty-second
# groundswell as one vast crest filling the whole wall and pulsing, which is
# honest and useless. Fixing the *number* of wavelengths instead keeps the
# picture legible at every period and keeps the thing that matters exactly
# right: at n wavelengths across, a crest still passes any given point once
# every T seconds. The bar across the top says what one of them is worth in
# seconds, which is the only horizontal unit this panel claims.
WAVES_ACROSS = 3.4

# Spectral width. A partition is drawn as three components at the measured
# period and a few per cent either side, weighted 1:2:1, which is what stops
# every crest being the same size without moving the rhythm: the carrier still
# crosses any point every T seconds and the sidebands only beat slowly against
# it. Windsea spectra really are broader than swell spectra, hence two numbers.
SPREAD_SWELL = 0.07
SPREAD_SEA = 0.16
SUB_WEIGHTS = (0.15, 0.70, 0.15)

# Beyond this the observation is called out in warning colour; beyond DEAD the
# water is not drawn at all. Ninety minutes rather than an hour because NDBC's
# own pipeline is routinely half an hour behind the buoy -- the file 46026
# serves has been observed forty minutes stale with nothing wrong at either end
# -- and a panel that cries stale every afternoon is a panel nobody believes on
# the day it matters.
OBS_WARN = 5400.0
OBS_DEAD = 12 * 3600.0

# How long a hole in the trend to bridge, in samples. The buoy publishes wave
# height on a ten-minute grid and the dominant period on a thirty-minute one,
# both with drops; joining across half an hour of a quantity that changes over
# hours is fair, and joining across six is the lie this bound exists to stop.
GAP_FILL = 3

C_TEXT = (198, 210, 222)
C_DIM = (86, 98, 112)
C_WARN = (255, 96, 72)
C_SWELL = (120, 208, 255)               # the cold blue everything wet is drawn in
C_WSEA = (255, 186, 96)                 # windsea and period: warm, so it separates
C_HT = (96, 190, 240)
C_HT_FILL = (20, 68, 108)
C_GRID = (26, 32, 42)
C_AXIS = (44, 52, 64)
C_INK = (236, 246, 255)                 # overlay marks on the water
C_INK_DIM = (128, 156, 180)             # the still-water line, which is reference
C_SHADOW = (0, 4, 10)                   # their halo, so they read over foam

# The vertical section, as a lookup table indexed by how far a pixel is below
# the surface. Above the water is night, with two rows of glow so the crest has
# an edge rather than a cut; the surface itself is one bright row, because a
# line is what makes a wave shape readable at this size; below it the water
# falls away from the blue an LED panel is actually good at into near-black, so
# the band reads as depth and not as a filled bar chart.
C_SKY = (2, 4, 10)
C_GLOW = ((5, 14, 28), (14, 44, 76))
C_CREST = (188, 232, 252)
WATER_RAMP = [(0.00, (70, 166, 214)), (0.10, (34, 110, 168)),
              (0.30, (16, 64, 116)), (0.62, (8, 34, 72)),
              (1.00, (3, 12, 28))]
SURF_OFF = 3                            # LUT index of the surface row itself

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, propagation, sort and tide
# draw with: five rows a glyph, each row an octal digit whose three bits are
# the three columns. Anything from a real typeface is mush at five pixels, and
# the Pi does not have the same faces installed as the machine this was written
# on. The height is measured off the mask everywhere below rather than assumed
# -- a five that is written down twice is a five that gets changed once. The
# font has no comma either, and draws a space for one silently, so there are
# none in any string on this panel.
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

GLYPH_H = _GLYPHS[" "].shape[0]
GLYPH_W = _GLYPHS[" "].shape[1]
LINE_H = GLYPH_H + 1                    # one blank row between stacked lines


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((GLYPH_H * scale, 1), bool)
    out = np.zeros((GLYPH_H, len(s) * (GLYPH_W + 1) - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * (GLYPH_W + 1):i * (GLYPH_W + 1) + GLYPH_W] = \
            _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def text_width(s, scale=1):
    n = len(str(s))
    return max(1, (n * (GLYPH_W + 1) - 1) * scale) if n else 1


def text_height(scale=1):
    return GLYPH_H * scale


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
# Units and words. Everything here is a phrase somebody who has never thought
# about wave periods can read: feet rather than metres and feet, whole feet
# rather than tenths, minutes said as minutes.
# --------------------------------------------------------------------------

POINTS = ("N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW").split()


def compass(deg):
    """A 16-point compass name for a bearing, or '' for nothing."""
    if deg is None:
        return ""
    return POINTS[int((float(deg) % 360.0) / 22.5 + 0.5) % 16]


def feet(metres, digits=None):
    """A wave height in feet, the way a surf report says it.

    Whole feet, because a tenth of a foot of swell is below both the buoy's
    honest resolution and anybody's interest; the only exception is a sea small
    enough that rounding it would print 0FT.
    """
    ft = float(metres) * M_FT
    if digits is None:
        digits = 1 if ft < 0.95 else 0
    return "%.*fFT" % (digits, ft)


def ago(seconds):
    """An age as a phrase. 'OBS 64M' was two ambiguities in seven characters:
    it did not say it was an age and it did not say the M was minutes."""
    if seconds is None:
        return "AGE UNKNOWN"
    s = max(0.0, float(seconds))
    if s < 90:
        return "JUST NOW"
    if s < 5400:
        return "%d MIN AGO" % int(s / 60)
    if s < 129600:
        return "%d HR AGO" % int(s / 3600)
    return "%d DAYS AGO" % int(s / 86400)


def wavelength(period):
    return DEEP * float(period) * float(period)


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises. What can still be
# wrong after that is wrong about *content*, and there are three separate ways
# for that to happen here, which the demo has to tell apart: no record at all,
# a record whose fetch is old (the fetcher is down), and a record that fetched
# fine but whose newest observation is old (the buoy is down). The last one is
# not hypothetical -- see the module docstring.
# --------------------------------------------------------------------------

def _num(payload, key):
    v = payload.get(key)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _train(payload, key):
    """A wave train out of the record: height, period, direction, or None."""
    d = payload.get(key)
    if not isinstance(d, dict):
        return None
    try:
        h, p = float(d["h"]), float(d["p"])
    except (KeyError, TypeError, ValueError):
        return None
    if p <= 0.5 or h < 0.0:
        return None
    deg = d.get("dir")
    return {"h": h, "p": p, "dir": None if deg is None else float(deg),
            "pt": str(d.get("pt") or compass(deg)),
            "t": float(d.get("t") or 0.0)}


def bridge(v, span=GAP_FILL):
    """Fill holes up to `span` samples wide from the nearer neighbour.

    The buoy drops samples constantly -- the day this was written it reported a
    wave height on 87 of 156 ten-minute slots and a dominant period on 42 --
    and drawn faithfully that is not a trend line, it is a comb, which reads as
    a barcode rather than as a sea building. So short holes are bridged and
    long ones are left as holes, which keeps the one thing that matters: an
    outage still looks like an outage.
    """
    out = v.copy()
    for d in range(1, int(span) + 1):
        for shift in (d, -d):
            need = np.isnan(out)
            if not need.any():
                return out
            src = np.roll(v, shift)
            # Wrapped ends are not neighbours, they are the other end of the
            # day; a value from yesterday teatime under this morning's hole is
            # exactly the kind of quiet nonsense this file is trying to avoid.
            if shift > 0:
                src[:shift] = np.nan
            else:
                src[shift:] = np.nan
            out[need] = src[need]
    return out


def _series(hist, key, n):
    """One history column as float32 with NaN for the holes, or None."""
    if not isinstance(hist, dict):
        return None
    raw = hist.get(key)
    if not raw:
        return None
    # A list with None in it will not go through np.asarray as a float array in
    # one step on every numpy, so the substitution is explicit.
    out = np.full(n, np.nan, f32)
    for i, v in enumerate(raw[:n]):
        if v is not None:
            out[i] = v
    return out


def read_buoy(cache_dir, station):
    """(state, error). `state` is everything the drawing needs, or None."""
    got = ftdata.load(PRODUCT + station, cache_dir)
    if got is None:
        return None, "no cached record for buoy %s" % station
    payload, age = got
    if not isinstance(payload, dict):
        return None, "buoy record is malformed"

    hs = _num(payload, "wvht")
    dpd = _num(payload, "dpd")
    mwd = _num(payload, "mwd")
    obs_t = _num(payload, "wvht_t") or 0.0

    swell = _train(payload, "swell")
    windsea = _train(payload, "windsea")
    # The standard file's own numbers are the fallback for the swell train, and
    # they are also the sanity floor: if the spectral file says nothing, the
    # dominant period and mean direction still describe a wave train, just one
    # with the chop folded into it.
    if swell is None and hs is not None and dpd:
        swell = {"h": hs, "p": dpd, "dir": mwd, "pt": compass(mwd),
                 "t": obs_t}
    if hs is None and swell is not None:
        hs = swell["h"]

    hist = payload.get("hist") or {}
    n = int(hist.get("n") or 0)
    state = {
        "station": str(payload.get("station", station)),
        "name": str(payload.get("name") or station).upper(),
        "hs": hs, "dpd": dpd, "mwd": mwd, "apd": _num(payload, "apd"),
        "swell": swell, "windsea": windsea,
        "steepness": str(payload.get("steepness") or ""),
        "wtmp": _num(payload, "wtmp"), "atmp": _num(payload, "atmp"),
        "wspd": _num(payload, "wspd"), "wdir": _num(payload, "wdir"),
        "gst": _num(payload, "gst"),
        "obs_t": obs_t,
        "obs_age": max(0.0, time.time() - obs_t) if obs_t else None,
        "age": age,
        # Not `ftdata.is_fresh()`: that answers "yes" for a product it has never
        # heard of, and a station only registered through FT_BUOYS in the
        # fetcher's environment is exactly that here. The TTL is the buoy's
        # either way, so it is taken from the module rather than the registry.
        "fresh": age <= (ftdata.ttl_for(PRODUCT + station) or ftdata.NDBC_TTL),
        "t0": float(hist.get("t0") or 0.0),
        "step": float(hist.get("step") or 600.0),
        "hs_hist": _series(hist, "wvht", n),
        "dpd_hist": _series(hist, "dpd", n),
    }
    if state["hs"] is None or state["swell"] is None:
        return state, "buoy %s reports no wave height" % station
    return state, None


def partitions(state, with_windsea=True):
    """The trains to draw, longest period first.

    The first one sets both the section and the zoom, and it is the *longest*
    train rather than the biggest for a reason worth writing down. Keying the
    zoom on whichever train happened to be biggest drew a four-second windsea
    as three wide smooth bands across the wall -- the same picture as a
    groundswell, only faster -- because "n wavelengths across" makes every
    train look alike. Keying it on the swell instead draws the chop at its true
    size *relative to the swell*, which is the comparison the panel exists to
    make: a blown-out day is short steep chop with a long slow heave under it,
    and it looks nothing like a clean one.
    """
    out = [t for t in (state["swell"],
                       state["windsea"] if with_windsea else None)
           if t is not None and t["p"] > 0.5]
    out.sort(key=lambda t: -t["p"])
    return out


def dominant(trains):
    """The train the eye follows: the one carrying the most height.

    Its period is the rhythm somebody watching the wall will actually count, so
    it is the number the headline says and the number the bar across the top of
    the water measures. That is not always the swell -- on a blown-out day the
    chop is what you see -- and the two agreeing is why the sentence in the
    header and the picture under it are the same claim.
    """
    return max(trains, key=lambda t: t["h"]) if trains else None


def verdict(state):
    """Two words for the shape of the sea: is this swell, or is it slop?

    The ratio of windsea height to swell height is the whole of it. Under a
    half and the surface is a groundswell with a texture on it; over one and
    whatever is arriving from far away is buried under what the local wind is
    making. It is the single most useful thing the spectral file says, and it
    is the reason that file is fetched at all.

    The word on its own -- CLEAN -- had nothing to lean on: clean compared with
    what? So it comes with the comparison it is making, in plain words, and
    with a shorter form for when the header runs out of room.
    """
    sw, ws = state["swell"], state["windsea"]
    if sw is None or ws is None or sw["h"] <= 0.05:
        return "", "", "", C_DIM
    r = ws["h"] / sw["h"]
    if r < 0.5:
        return "CLEAN", "MOSTLY SWELL", "SWELL", C_SWELL
    if r < 1.0:
        return "MIXED", "SWELL AND CHOP", "BOTH", C_TEXT
    return "CHOPPY", "MOSTLY CHOP", "CHOP", C_WSEA


# --------------------------------------------------------------------------
# Layout. Three bands: the words, the water, the day.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.head_h = 2 * LINE_H if h >= 44 else (LINE_H if h >= 24 else 0)
        # The strip gets a third, bounded: under about fourteen rows there is
        # no room for a plot and a row of hour labels under it, and past
        # twenty-four rows it starts stealing from the only part of the panel
        # that moves.
        strip = int(round(h * 0.33))
        strip = max(0, min(24, min(strip, h - self.head_h - 10)))
        self.strip_h = strip if strip >= LINE_H + 8 else 0
        self.head_y = 0
        self.sea_y = self.head_h
        self.sea_h = h - self.head_h - self.strip_h
        self.strip_y = h - self.strip_h


# --------------------------------------------------------------------------
# The header: one sentence, and the small print beside it.
# --------------------------------------------------------------------------

def _fit_columns(dst, x0, x1, ladder, gap=6):
    """Draw the first layout in `ladder` that fits between x0 and x1.

    A ladder of progressively shorter layouts rather than clipping, as tide.py's
    header does: what falls off the right of this line is the small print, and
    the panel should choose which small print it loses rather than losing half
    a word of it. Each layout is a list of columns and each column is a list of
    (text, colour) lines.
    """
    for cols in ladder:
        widths = [max([text_width(s) for s, _ in lines] or [0])
                  for lines in cols]
        need = sum(widths) + gap * max(0, len(cols) - 1)
        if need > x1 - x0 and cols is not ladder[-1]:
            continue
        x = x0
        for lines, wd in zip(cols, widths):
            if x + wd > x1:
                break
            for i, (s, rgb) in enumerate(lines):
                if s:
                    blit_text(dst, i * LINE_H, x, s, rgb)
            x += wd + gap
        return x
    return x0


def draw_header(dst, lay, state, trains):
    """A sentence in the biggest type the panel has and the caveats beside it.

    The old header printed eleven numbers, four of them the same fact twice --
    height in feet and in metres, direction as a point and as a bearing -- and
    the two that mattered most, the period and the age, in a shorthand only a
    surf forecaster reads. This one says the height and the rhythm in words,
    and everything the profile below already draws has been taken off it.
    """
    dst[:] = 0
    if lay.head_h <= 0:
        return
    w = lay.w
    scale = 2 if lay.head_h >= 2 * LINE_H and w >= 200 else 1
    big_y = max(0, (lay.head_h - text_height(scale)) // 2)

    # The height is the whole sea state -- significant height, the number a surf
    # report leads with -- and the period belongs to the train whose crests the
    # eye follows and whose rhythm the animation keeps. Quoting the standard
    # file's dominant period here instead would be a second number that means
    # nearly the same thing and disagrees with the picture on the days it does
    # not.
    lead = dominant(trains) or state["swell"]
    period = int(round(lead["p"])) if lead else 0
    head = feet(state["hs"], 0)
    if period:
        head += " WAVES EVERY %d SEC" % period
    else:
        head += " WAVES"
    blit_text(dst, big_y, 1, head, C_TEXT, scale)
    x = 1 + text_width(head, scale)

    # The right-hand block is reserved before anything else is laid out: it
    # carries where the numbers came from and how old they are, and they are
    # what makes the rest believable. Identity first, age under it, so the two
    # read as one phrase downwards.
    obs = state["obs_age"]
    rights = [(state["name"][:12], C_DIM) if state["fresh"] else
              ("STALE %s" % ftdata.describe_age(state["age"]).upper(), C_WARN),
              (ago(obs), C_WARN if obs is not None and obs >= OBS_WARN
               else C_DIM)]
    rw = max(text_width(s) for s, _ in rights)
    rx = w - rw - 1
    for i, (s, rgb) in enumerate(rights):
        blit_text(dst, i * LINE_H, w - text_width(s) - 1, s, rgb)

    word, phrase, short, wcol = verdict(state)
    sw, ws = state["swell"], state["windsea"]
    ladder = []
    if ws is not None:
        # The two halves of the sea, one per line and in the same shape, so the
        # comparison is a glance down the column rather than a reading -- and
        # the verdict beside them says which half won, which is the sentence
        # that gives the split a point.
        full = [("SWELL %s %d SEC" % (feet(sw["h"]), round(sw["p"])), C_SWELL),
                ("CHOP %s %d SEC" % (feet(ws["h"]), round(ws["p"])), C_WSEA)]
        brief = [("SWELL %s" % feet(sw["h"]), C_SWELL),
                 ("CHOP %s" % feet(ws["h"]), C_WSEA)]
        vd_full = [(word, wcol), (phrase, wcol)]
        vd_short = [(word, wcol), (short, wcol)]
        extra = []
        if state["wtmp"] is not None:
            extra = [[("WATER %.0fF" % (state["wtmp"] * 9.0 / 5.0 + 32.0),
                       C_DIM), ("", C_DIM)]]
        ladder = [[full, vd_full] + extra, [full, vd_full], [brief, vd_full],
                  [brief, vd_short], [brief]]
    elif state["steepness"]:
        ladder = [[[(state["steepness"], C_DIM), ("SEA", C_DIM)]]]
    if ladder:
        _fit_columns(dst, x + 4, rx - 4, ladder)


# --------------------------------------------------------------------------
# The trend strip: a day of significant height, with the period over it.
# --------------------------------------------------------------------------

def draw_strip(dst, lay, state, hours):
    """Twenty-four hours of sea state, newest at the right edge.

    The right edge is the newest *sample*, not the wall clock: if the buoy went
    quiet at breakfast the trace stops where the data stopped and the gap is
    visible, which is the whole reason a strip is worth having next to a
    headline number. It used to be labelled `9FT` at one end and `20S` at the
    other with nothing saying what it was or how long it ran, so the two axis
    maxima now name their quantity in the colour of their trace and the strip
    says in words that it is a day.
    """
    dst[:] = 0
    if lay.strip_h <= 0:
        return None
    h, w = dst.shape[:2]
    hs = state["hs_hist"]
    if hs is None or not np.isfinite(hs).any():
        blit_text(dst, max(0, (h - GLYPH_H) // 2), 1, "NO TREND YET", C_DIM)
        return None

    hs = bridge(hs)
    n = len(hs)
    step = state["step"] or 600.0
    span = hours * 3600.0
    t1 = state["t0"] + (n - 1) * step
    t0 = t1 - span

    plot_t = 1
    plot_b = h - LINE_H - 3
    axis_y = h - LINE_H - 2
    label_y = h - LINE_H

    # Column -> sample. Nearest, not interpolated: the samples are ten minutes
    # apart and the panel has three columns per sample, so interpolation would
    # invent a slope inside every hole rather than showing it.
    tc = t0 + (np.arange(w, dtype=np.float64) + 0.5) * span / w
    idx = np.clip(np.round((tc - state["t0"]) / step), 0, n - 1).astype(np.intp)
    # A column whose nearest sample is more than one step away is off the end
    # of the record, not a hole in it.
    off = np.abs(state["t0"] + idx * step - tc) > step
    hv = hs[idx]
    hv[off] = np.nan

    hmax = max(1.0, float(np.nanmax(hs)) * 1.15)
    good = np.isfinite(hv)
    rows = np.where(good, plot_b - hv / hmax * (plot_b - plot_t), plot_b)
    ri = np.clip(np.round(rows), plot_t, plot_b).astype(np.intp)

    yy = np.arange(h)[:, None]
    fill = (yy >= ri[None, :]) & (yy <= plot_b) & good[None, :]
    dst[fill] = C_HT_FILL
    dst[ri[good], np.flatnonzero(good)] = C_HT

    # Six-hourly gridlines behind nothing -- they are drawn first so the trace
    # covers them -- and the hour labels under the axis. Never at the very left
    # edge: the label there would sit on top of the strip's own title, and
    # "-24H" is the one tick the axis does not need, being the end of an axis
    # whose length the title states.
    title = "PAST %d HOURS" % max(1, int(round(hours)))
    title_w = text_width(title) + 3
    for k in range(1, int(hours // 6) + 1):
        c = int(round(w * (1.0 - k * 6.0 / hours)))
        lab = "-%dH" % (k * 6)
        if not (0 <= c < w):
            continue
        col = dst[plot_t:plot_b + 1, c]
        np.maximum(col, np.array(C_GRID, np.uint8), out=col)
        lx = max(0, c - text_width(lab) // 2)
        if lx >= title_w:
            blit_text(dst, label_y, lx, lab, C_DIM)
    dst[axis_y, :] = C_AXIS

    # The period, dotted over the fill on its own fixed scale. Fixed and not
    # fitted to the day: the point of it is that fourteen seconds means the same
    # thing on the panel this afternoon as it did last week.
    pv = state["dpd_hist"]
    pmin, pmax = 4.0, 20.0
    if pv is not None:
        p = bridge(pv)[idx]
        p[off] = np.nan
        ok = np.isfinite(p)
        if ok.any():
            # Only the valid columns go through the arithmetic: clip() of a NaN
            # is a NaN and casting one to an index is a warning on numpy 2 and
            # a garbage row number on any numpy.
            pv_ok = np.clip(p[ok], pmin, pmax)
            pr = plot_b - (pv_ok - pmin) / (pmax - pmin) * (plot_b - plot_t)
            pri = np.clip(np.round(pr), plot_t, plot_b).astype(np.intp)
            dst[pri, np.flatnonzero(ok)] = C_WSEA

    blit_text(dst, label_y, 1, title, C_DIM)
    blit_text(dst, plot_t, 1, "HEIGHT %.0fFT" % (hmax * M_FT), C_HT)
    top = "PERIOD %d SEC" % int(pmax)
    blit_text(dst, plot_t, w - text_width(top) - 3, top, C_WSEA)
    now = "NOW"
    blit_text(dst, label_y, w - text_width(now) - 1, now, C_TEXT)
    dst[plot_t:plot_b + 1, w - 1] = C_TEXT
    return hmax


# --------------------------------------------------------------------------
# The water, in section.
#
# The surface is one number per column: the sum of a handful of travelling
# sinusoids. Their phases along x are baked once into a (component, width)
# array, so a frame is an add of one scalar per component, a sine, a scale and
# a sum -- five calls on a small array -- and then four more to turn the
# resulting surface into a band of pixels: subtract it from every row to get a
# depth, clip, cast to an index, and look the colour up.
#
# Nothing is computed per pixel per frame and nothing is allocated.
# --------------------------------------------------------------------------

def surface_lut(depth_rows):
    """Colour by depth below the surface. Index 0 is sky; SURF_OFF is the
    surface row; everything past the end is the bottom of the band."""
    n = SURF_OFF + 1 + max(2, depth_rows)
    lut = np.zeros((n, 3), np.uint8)
    lut[0] = C_SKY
    lut[1] = C_GLOW[0]
    lut[2] = C_GLOW[1]
    lut[SURF_OFF] = C_CREST
    body = ds.gradient(WATER_RAMP, DEPTH_FADE, np.uint8)
    tail = n - SURF_OFF - 1
    if tail <= DEPTH_FADE:
        lut[SURF_OFF + 1:] = body[:tail]
    else:
        lut[SURF_OFF + 1:SURF_OFF + 1 + DEPTH_FADE] = body
        lut[SURF_OFF + 1 + DEPTH_FADE:] = body[-1]
    return np.ascontiguousarray(lut, np.uint8)


def components(trains, mpp, px_per_m, rate):
    """(phase, omega, amplitude) rows for every component to be summed.

    `trains[0]` sets the section: its own crests are drawn at their true
    spacing and it travels left to right. Every other train is projected onto
    that line, which multiplies its wavenumber by the cosine of the angle
    between them -- lengthening its apparent wavelength, reversing it if it is
    running the other way, and leaving its *period* alone, which is exactly
    what a section across a crossing sea does.
    """
    if not trains:
        return [], [], []
    axis = trains[0]["dir"]
    ph, om, amp = [], [], []
    for j, tr in enumerate(trains):
        if tr["h"] <= 0.0:
            continue
        delta = 0.0
        if j and axis is not None and tr["dir"] is not None:
            delta = math.radians(float(tr["dir"]) - float(axis))
        along = math.cos(delta)
        spread = SPREAD_SWELL if j == 0 else SPREAD_SEA
        a0 = 0.5 * tr["h"] * px_per_m
        for wgt, ds_ in zip(SUB_WEIGHTS, (1.0 + spread, 1.0, 1.0 - spread)):
            p = tr["p"] * ds_
            lam = wavelength(p)                     # metres, deep water
            # Radians per pixel along the section. A train nearly at right
            # angles to the section has almost no spatial structure along it
            # and simply heaves the surface up and down at its own period,
            # which is the truth and not a special case.
            ph.append(2.0 * math.pi * mpp * along / max(1e-3, lam))
            om.append(2.0 * math.pi * rate / p)
            amp.append(wgt * a0)
    return ph, om, amp


def _line(mask, y0, x0, y1, x1, width=0.9):
    """Stamp a segment into a boolean mask. Baked once; not a hot path."""
    h, w = mask.shape
    lo_y = max(0, int(min(y0, y1) - width - 1))
    hi_y = min(h, int(max(y0, y1) + width + 2))
    lo_x = max(0, int(min(x0, x1) - width - 1))
    hi_x = min(w, int(max(x0, x1) + width + 2))
    if hi_y <= lo_y or hi_x <= lo_x:
        return
    py = np.arange(lo_y, hi_y, dtype=f32)[:, None] + (0.5 - y0)
    px = np.arange(lo_x, hi_x, dtype=f32)[None, :] + (0.5 - x0)
    dy, dx = y1 - y0, x1 - x0
    ll = max(dy * dy + dx * dx, 1e-6)
    tp = np.clip((py * dy + px * dx) / ll, 0.0, 1.0)
    d = np.hypot(py - tp * dy, px - tp * dx)
    mask[lo_y:hi_y, lo_x:hi_x] |= d <= width


def _arrow(mask, y, x, deg, length, width=0.9):
    """An arrow from (y,x) pointing along a compass bearing."""
    b = math.radians(float(deg) % 360.0)
    dx, dy = math.sin(b), -math.cos(b)
    hy, hx = y + dy * length, x + dx * length
    _line(mask, y, x, hy, hx, width)
    bl = max(3.0, length * 0.34)
    for s in (1, -1):
        _line(mask, hy, hx, hy - dy * bl + s * dx * bl * 0.6,
              hx - dx * bl - s * dy * bl * 0.6, width)


def _pad_mask(m, hgt, wid, y, x):
    """A small mask placed into a (hgt, wid) one, clipped."""
    out = np.zeros((hgt, wid), bool)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(hgt, y + gh), min(wid, x + gw)
    if y1 > y0 and x1 > x0:
        out[y0:y1, x0:x1] = m[y0 - y:y1 - y, x0 - x:x1 - x]
    return out


def _dilate(a):
    o = a.copy()
    o[1:] |= a[:-1]
    o[:-1] |= a[1:]
    o[:, 1:] |= a[:, :-1]
    o[:, :-1] |= a[:, 1:]
    return o


def build_overlay(hgt, wid, state, trains, still, px_per_m, lam_px):
    """The three legends that go on the water, plus the compass inset.

    Returned as flat pixel indices and colours rather than as an image, because
    that makes drawing them one scatter into the frame instead of a blend over
    the whole band -- and, unlike a maximum, it can draw a dark halo, which is
    the only thing that keeps a caption legible over a lit crest.

      * the still-water line, dashed, so a flat calm still has an axis;
      * a bracket at the left spanning the significant height and labelled with
        it, which is the vertical scale and the answer to "5FT of what";
      * a bar across the top one crest long, labelled with the seconds between
        crests, which is the horizontal scale and the tie between the sentence
        in the header and the rhythm of the animation;
      * an inset in the corner: north, an arrow the way the water is running,
        and the bearing it is running from, because a profile has no compass.
    """
    ink = np.zeros((hgt, wid), bool)
    faint = np.zeros((hgt, wid), bool)
    lead = dominant(trains)

    # Still water. Dashed rather than solid so it reads as a reference and not
    # as a horizon, and dim so the water crosses it rather than the other way.
    faint[still, ::4] = True

    # The vertical scale: significant height, drawn as the distance it is.
    half = max(1.0, 0.5 * float(state["hs"]) * px_per_m)
    by0, by1 = still - half, still + half
    if hgt >= 16 and by0 >= 0 and by1 < hgt:
        _line(ink, by0, 4, by1, 4, 0.6)
        _line(ink, by0, 2, by0, 7, 0.6)
        _line(ink, by1, 2, by1, 7, 0.6)
        lab = feet(state["hs"], 0)
        ly = int(round(still - GLYPH_H * 0.5))
        ink |= _pad_mask(text_mask(lab), hgt, wid, max(0, ly), 10)

    # The horizontal scale, in seconds rather than metres. Metres would be a
    # second quantity ending in M on a panel that already has an age in it, and
    # would claim a horizontal scale the eye cannot use anyway; seconds between
    # crests is the same fact said in the unit the animation is keeping.
    if lead is not None and hgt >= 20 and 8 <= lam_px <= wid - 8:
        y = 2
        lab = "%d SEC BETWEEN CRESTS" % round(lead["p"])
        lw = text_width(lab)
        # A four-second chop is only twenty pixels of crest to crest, so the
        # caption cannot always live under the bar. When it does not fit it goes
        # alongside, and the whole legend is centred as one object.
        under = lw <= lam_px
        total = lam_px if under else lam_px + 3 + lw
        x0 = int(round((wid - total) * 0.5))
        x1 = int(round(x0 + lam_px))
        _line(ink, y, x0, y, x1, 0.6)
        _line(ink, y, x0, y + 3, x0, 0.6)
        _line(ink, y, x1, y + 3, x1, 0.6)
        if under:
            ink |= _pad_mask(text_mask(lab), hgt, wid, y + 4,
                             int(x0 + (lam_px - lw) * 0.5))
        else:
            ink |= _pad_mask(text_mask(lab), hgt, wid, y - 2, x1 + 3)

    # The compass inset. The section is cut along the way the water is running,
    # so the arrow is the reciprocal of the bearing in the label -- "from NW"
    # and an arrow pointing southeast are the same statement, and every forecast
    # in the world quotes the first one.
    if lead is not None and lead["dir"] is not None and hgt >= 22:
        cy, cx = 8.0, wid - 12.0
        _arrow(ink, cy, cx, (lead["dir"] + 180.0) % 360.0, 6.0, 1.0)
        _line(faint, cy - 1, cx, cy - 6, cx, 0.6)
        ink |= _pad_mask(text_mask("N"), hgt, wid, 0, int(cx) - 1)
        lab = "FROM %s" % (lead["pt"] or compass(lead["dir"]))
        ink |= _pad_mask(text_mask(lab), hgt, wid, 3,
                         int(cx) - 8 - text_width(lab))

    faint &= ~ink
    lit = ink | faint
    halo = _dilate(lit) & ~lit
    idx = np.flatnonzero(lit | halo).astype(np.intp)
    flat_ink, flat_faint = ink.reshape(-1)[idx], faint.reshape(-1)[idx]
    col = np.tile(np.array(C_SHADOW, np.uint8), (len(idx), 1))
    col[flat_faint] = C_INK_DIM
    col[flat_ink] = C_INK
    return idx, np.ascontiguousarray(col, np.uint8)


# --------------------------------------------------------------------------
# The honest panels: no record, or a buoy that has stopped talking.
# --------------------------------------------------------------------------

def draw_card(dst, lay, lines):
    dst[:] = (5, 7, 12)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    total = 0
    for i, _ in enumerate(lines):
        total += text_height(scale if i == 0 else 1) + 3
    y = max(0, (lay.h - total) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        blit_text(dst, y, max(0, (lay.w - text_width(s, sc)) // 2), s, rgb, sc)
        y += text_height(sc) + 3
    return dst


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--station", default=STATION,
                    help="NDBC station whose record to draw")
    ap.add_argument("--hours", type=float, default=24.0,
                    help="hours of trend across the strip")
    ap.add_argument("--waves", type=float, default=WAVES_ACROSS,
                    help="wavelengths of the dominant train across the panel; "
                         "the zoom, and the bar at the top always says what "
                         "one of them is worth in seconds")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="wave speed multiplier; 1 is real time, which is the "
                         "entire point, so change it only for a screenshot")
    ap.add_argument("--full-scale", type=float, default=HS_FULL,
                    help="significant height in metres at the top of the "
                         "vertical scale")
    ap.add_argument("--no-windsea", action="store_true",
                    help="draw the swell alone, without the local chop")
    ap.add_argument("--reload", type=float, default=600.0,
                    help="seconds between re-reads of the cache (0 = never)")


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h)
    cache = args.cache_dir
    station = args.station

    frame = np.zeros((h, w, 3), np.uint8)
    lut = surface_lut(max(2, lay.sea_h))
    nlut = len(lut)

    # Everything the frame loop touches, rebuilt only when the record changes.
    cell = {"card": None, "loaded": -1e18, "state": None, "trains": [],
            "ph": None, "om": None, "amp": None, "tmp": None, "elev": None,
            "yyoff": None, "depth": None, "didx": None, "ovl": None,
            "sea": None, "sea_flat": None, "still": 0, "px_per_m": 1.0}

    def make_card(lines):
        cell["card"] = draw_card(np.zeros((h, w, 3), np.uint8), lay, lines)

    def prepare(state):
        """Bake the header, the strip, the component phases and the overlay."""
        cell["card"] = None
        trains = partitions(state, not args.no_windsea)
        cell["trains"] = trains
        if lay.head_h:
            draw_header(frame[:lay.head_h], lay, state, trains)
        if lay.strip_h:
            draw_strip(frame[lay.strip_y:], lay, state, args.hours)
        sea_h = lay.sea_h
        if sea_h <= 0 or not trains:
            cell["ph"] = None
            return
        sea = frame[lay.sea_y:lay.sea_y + sea_h]
        cell["sea"] = sea
        cell["sea_flat"] = sea.reshape(-1, 3)

        # Still water sits in the middle of the band, which is what makes the
        # whole band available to the waves: the crest of a full-scale storm
        # reaches the top row and the trough of it the bottom, and there is no
        # asymmetry to explain.
        still = int(round(sea_h * 0.5))
        cell["still"] = still
        # Pixels per metre of elevation. Fixed against `--full-scale`, with
        # headroom for the two trains summing, so a two-foot day and a
        # ten-foot day are drawn on the same axis and can be told apart from
        # the far end of the room.
        half_band = max(2, min(still, sea_h - 1 - still))
        px_per_m = half_band / (max(0.3, args.full_scale) * 0.5 * VERT_HEADROOM)
        cell["px_per_m"] = px_per_m

        # Metres per pixel along the section: `--waves` wavelengths of the
        # longest train across the panel. Everything else is then drawn at its
        # true size relative to that one.
        axis = trains[0]
        mpp = wavelength(axis["p"]) / (w / max(0.2, args.waves))

        # The bar at the top measures the train the eye follows, which may not
        # be the one the section is cut along, so its spacing on screen is its
        # own wavelength projected onto the section -- the same projection the
        # water itself is drawn with, or the legend would measure a crest
        # spacing the picture does not have.
        lead = dominant(trains)
        delta = 0.0
        if (lead is not axis and lead["dir"] is not None
                and axis["dir"] is not None):
            delta = math.radians(float(lead["dir"]) - float(axis["dir"]))
        lam_px = (wavelength(lead["p"]) / mpp
                  / max(0.08, abs(math.cos(delta))))

        ph, om, amp = components(trains, mpp, px_per_m, float(args.rate))
        n = max(1, len(ph))
        cell["ph"] = np.ascontiguousarray(
            np.asarray(ph, f32)[:, None] * np.arange(w, dtype=f32)[None, :])
        cell["om"] = np.asarray(om, f32)[:, None]
        cell["amp"] = np.asarray(amp, f32)[:, None]
        cell["tmp"] = np.empty((n, w), f32)
        cell["off"] = np.empty((n, 1), f32)
        cell["elev"] = np.empty(w, f32)
        # Row index into the LUT before the surface is added: how far this row
        # is below still water, offset so that the surface row lands on
        # SURF_OFF and the three entries above it are the glow and the sky.
        cell["yyoff"] = (np.arange(sea_h, dtype=f32)[:, None]
                         - still + SURF_OFF)
        cell["depth"] = np.empty((sea_h, w), f32)
        cell["didx"] = np.empty((sea_h, w), np.intp)
        cell["ovl"] = build_overlay(sea_h, w, state, trains, still, px_per_m,
                                    lam_px)

    def reload_data():
        # The reload timer is monotonic, never the wall clock: this is about
        # how long since *this process* last looked at the file, and a wall
        # clock that steps -- which is exactly what a Pi with no RTC does a
        # minute after it boots and finds an NTP server -- would either freeze
        # the re-read for hours or make it happen every frame.
        cell["loaded"] = time.monotonic()
        state, err = read_buoy(cache, station)
        cell["state"] = state
        if state is None:
            make_card([("NO BUOY DATA", C_WARN),
                       ("RUN  PYTHON3 FTDATA.PY --ONCE", C_TEXT),
                       ((err or "")[:52].upper(), C_DIM)])
            return
        obs = state["obs_age"]
        if err or obs is None or obs > OBS_DEAD:
            # The buoy, not the fetcher. Say which, and do not animate a sea
            # state that stopped being true half a day ago.
            make_card([("%s SILENT" % state["name"][:14], C_WARN),
                       ("LAST WAVE %s" % ago(obs), C_DIM),
                       ("FETCHED %s" % ago(state["age"]), C_DIM)])
            return
        prepare(state)

    reload_data()

    def render(t, i):
        # Wall clock only for the re-read, which cannot change what is drawn
        # unless the fetcher has written a new record; see the report.
        if args.reload and time.monotonic() - cell["loaded"] >= args.reload:
            reload_data()
        if cell["card"] is not None:
            return cell["card"]
        if cell["ph"] is None:
            return frame

        # The surface: one row per component, summed down to one number per
        # column. sin(kx - wt) travels towards +x, which is the way the
        # dominant train is going, because the section is cut along it.
        tmp, off = cell["tmp"], cell["off"]
        np.multiply(cell["om"], -float(t), out=off)
        np.add(cell["ph"], off, out=tmp)
        np.sin(tmp, out=tmp)
        np.multiply(tmp, cell["amp"], out=tmp)
        np.sum(tmp, axis=0, out=cell["elev"])

        # ... and the band of water under it: depth below the surface, clipped
        # into the lookup table, which puts sky above the crest and the bottom
        # of the band below the trough with no branch anywhere.
        depth = cell["depth"]
        np.add(cell["yyoff"], cell["elev"][None, :], out=depth)
        np.clip(depth, 0, nlut - 1, out=depth)
        np.copyto(cell["didx"], depth, casting="unsafe")
        np.take(lut, cell["didx"], axis=0, out=cell["sea"], mode="clip")
        ovl_idx, ovl_col = cell["ovl"]
        cell["sea_flat"][ovl_idx] = ovl_col
        return frame

    render.state = cell
    render.layout = lay
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "live Pacific swell in section at the measured period",
                  fps=20)


if __name__ == "__main__":
    main()
