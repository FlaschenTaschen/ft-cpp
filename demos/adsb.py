#!/usr/bin/env python3
"""Aircraft over the Bay, live, moving the way they are actually moving.

A map of the Bay Area with everything airborne within fifty nautical miles of
the wall drawn on it as a short comet: the head is where the aircraft is, the
tail is where it came from, the length is how fast it is going and the colour
is how high it is -- warm on the deck, cool in the flight levels. The two most
interesting of them are named. Underneath, the counts, the altitude ramp the
colours come off, and how old the data is.

**Sixty-second-old data that does not look sixty seconds old.** The fetcher
asks once a minute; sixty stationary dots that jump every minute would look
broken, and would also be a worse picture than the truth, because ADS-B carries
a groundspeed and a track and those are enough to say where an aircraft is
*now*. So `render()` dead-reckons: each aircraft advances along its own track
at its own groundspeed from the moment its position was last heard, which is a
per-aircraft number and not the age of the fetch -- a jet over the Gate reports
twice a second and something over the Diablo range may not have been heard for
half a minute. When a new record lands it does not snap; the correction is
eased in over about a second, so a fix that has moved a plane three pixels
looks like a plane moving three pixels rather than like the panel glitching.
Dead reckoning is extrapolation and it is only honest for so long, which is
what the TTL is for: past five minutes the aircraft stop being drawn.

**The crop, and why it is stretched three times.** 37.47-37.93 N,
122.94-121.86 W: 95 km by 51 km, with SFO, Oakland, Hayward, San Carlos and
Half Moon Bay on it, the Golden Gate at the top and the wall's own address in
the middle. Drawn across 320 columns and 57 rows that is a three-fold
horizontal stretch, the same one tide.py applies to the Gate corridor and for
the same reason: the Bay Area is roughly square and the panel is five times
wider than it is tall, so something has to give. San Jose is deliberately off
the bottom -- reaching it costs another quarter of stretch for an airport whose
traffic mostly never comes near this building. The stretch is applied to the
velocities as well as to the positions, so a mark points along the direction it
is really travelling *on this map*; it is not a compass rose and does not
pretend to be one.

**The coastline is `adsb-coast.npz`, and it is not `voxel-dem.npz`.** The DEM
the voxel and tide demos share stops at 37.635 N, and SFO is at 37.619 -- a mile
and a half south of it. A traffic panel whose map stops just short of the
airport is not worth drawing, so `scripts/make-adsb-coast.py` bakes a second,
much wider and much coarser mask: three times the area, an eighth of the
resolution, one bit a cell, 5 kB. Land is dim, water is darker, the shoreline
is lit, and nothing else on the map is bright, because the aircraft are the
only thing on it that is news.

**Nothing here touches the network.** `ftdata.py` fetches on a timer in a
process of its own; this demo reads the file it leaves behind and does not
import a HTTP library. It has to be that way round -- the scheduler builds the
next segment on a worker thread, Python threads share the GIL, and a `build()`
blocked on a socket stops the render loop getting the interpreter back. This
product wants a minute's cadence rather than fifteen:

    $ python3 ftdata.py --loop 60 --due --fast

**The frame budget is 50 ms**, which is a 20 fps segment on the wall's Pi 3 at
its throttled 600 MHz. Everything that can be baked is baked in `build()`: the
coastline, the airports, the scale bar, the status strip, the altitude colour
of every aircraft and the unit vector it flies along. A frame is one full-frame
copy, four array operations to advance every aircraft at once, four scatters to
draw them and two labels -- about thirty numpy calls whether there is one
aircraft or a hundred and twenty, which is the number that matters on a machine
where a bare numpy call costs 55-80 us regardless of the size of the array.
Measured on this desktop over 600 sequential frames: **p95 0.054 ms with 31
aircraft and 0.096 ms with the cap's full 120**, and `build()` is 3 ms cold
against voxel's 8.2 s. At the 76-114x this project keeps measuring between this
desktop and that Pi, the busy case is 7-11 ms, and the floor set by the call
count alone -- thirty calls at 80 us -- is 2.4 ms, so the two agree and neither
is close to 50. The standalone default is still 20 fps rather than 30: nothing
here moves faster than three pixels a second, so the other ten frames carry no
information.

Run:  python3 ftdata.py --loop 60 --due --fast &   # the fetcher, own process
      python3 adsb.py --host 127.0.0.1
      python3 adsb.py --rate 20            # twenty minutes of drift a minute
      FT_DATA_CACHE=/tmp/empty python3 adsb.py     # the no-data card
"""

import math
import os
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata
import ftsite

f32 = np.float32

COAST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "adsb-coast.npz")

PRODUCT = "adsb-bay"

# lat0, lat1, lon0, lon1. See the docstring on the three-fold stretch.
EXTENT = (37.47, 37.93, -122.94, -121.86)

# The wall. Distances on the panel are from here, and it is drawn on the map.
# demos/site.json, so this and ftdata's ADSB_LAT/ADSB_LON -- the centre of the
# query that produced the aircraft -- are by construction the same point.
HOME = ftsite.latlon()
HOME_LABEL = "HERE"

KNOT_MS = 0.5144                        # knots to metres per second
NM_M = 1852.0

# Airports inside the crop. The two with a label are the ones whose traffic
# this panel is mostly showing; the rest get a mark and no name, because five
# three-letter codes across a map this size is a page of type with a coastline
# behind it.
AIRPORTS = (
    ("SFO", 37.6189, -122.3750, True),
    ("OAK", 37.7213, -122.2208, True),
    ("HWD", 37.6592, -122.1215, False),
    ("SQL", 37.5119, -122.2495, False),
    ("HAF", 37.5133, -122.5011, False),
)

# Colours. The map is nearly monochrome on purpose: everything on it is a shade
# of slate, and every saturated colour on the panel belongs to an aircraft.
C_SEA = (2, 6, 13)
C_LAND = (26, 30, 37)
C_SHORE = (74, 86, 104)
C_AIRPORT = (120, 132, 150)
C_HOME = (255, 120, 200)                # the one pink thing, so it is findable
C_TEXT = (196, 214, 228)
C_DIM = (92, 104, 120)
C_WARN = (255, 96, 72)
C_LABEL = (255, 244, 210)

# Altitude, as colour. Warm on the deck through to cold and pale in the flight
# levels, which is the convention every traffic display uses and which a person
# reads without being told. The index is the *square root* of the altitude
# fraction rather than the fraction: two thirds of what is interesting here
# happens under 10,000 ft -- the approaches, the departures, the helicopters
# and the light singles over the hills -- and a linear ramp spends four fifths
# of its range on cruising jets that all look the same anyway.
ALT_CEIL = 45000.0
ALT_STOPS = [(0.00, (255, 64, 30)), (0.16, (255, 150, 24)),
             (0.34, (252, 232, 72)), (0.54, (96, 236, 110)),
             (0.76, (72, 198, 255)), (1.00, (206, 224, 255))]

# How much of a comet each streak sample gets. Head first: the head is the
# aircraft and the rest is where it has been, so the fall-off is steep.
TRAIL_FADE = (1.0, 0.52, 0.30, 0.17)

# The ring drawn round a labelled aircraft: a 7x7 box with the corners taken
# off, which at this size reads as a circle and costs one masked blit. Baked
# here rather than built per frame, like everything else on this panel.
RING_R = 3
_yy, _xx = np.mgrid[-RING_R:RING_R + 1, -RING_R:RING_R + 1]
RING = (np.maximum(np.abs(_yy), np.abs(_xx)) == RING_R) & (
    np.abs(_yy) + np.abs(_xx) < 2 * RING_R)


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same table tide.py and propagation.py draw
# with: five rows a glyph, each row an octal digit whose three bits are the
# three columns. Nothing built from a real typeface survives five pixels, and
# the Pi does not have the same faces installed as the machine this was written
# on. Digits, letters, space, dash, dot, slash and colon is the whole alphabet
# a traffic panel needs, and all of them are already in that table.
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
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn.

    Clipped rather than asserted: this panel is laid out for 320x64 and has to
    survive being asked for something else, and a demo that raises on a narrow
    canvas takes the whole rotation down with it.
    """
    m = text_mask(s, scale)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    dst[y0:y1, x0:x1][sub] = rgb
    return gw


def fill(dst, y0, x0, y1, x1, rgb):
    """Filled rectangle, clipped."""
    y0, x0 = max(0, int(y0)), max(0, int(x0))
    y1, x1 = min(dst.shape[0], int(y1)), min(dst.shape[1], int(x1))
    if y1 > y0 and x1 > x0:
        dst[y0:y1, x0:x1] = rgb


def scale_colour(rgb, k):
    return tuple(int(round(c * k)) for c in rgb)


# --------------------------------------------------------------------------
# Clock. Everything asks for `now` rather than reading the system clock, which
# is what lets --rate wind the dead reckoning forward fast enough to watch, and
# what lets the tests render a deterministic sequence.
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


# --------------------------------------------------------------------------
# Geography.
# --------------------------------------------------------------------------

def load_sea():
    """The baked land/water mask and its bounding box. See make-adsb-coast.py."""
    d = np.load(COAST)
    shape = tuple(int(v) for v in d["shape"])
    sea = np.unpackbits(d["sea"])[:shape[0] * shape[1]].reshape(shape)
    return sea.astype(bool), tuple(float(v) for v in d["bbox"])


def crop_sea(sea, bbox, extent, gw, gh, sub=3):
    """The mask resampled into a (gh, gw) grid over `extent`.

    Area-averaged rather than point-sampled. The panel is coarser than the mask
    by fifteen times in latitude, and a nearest-neighbour crop at that ratio
    drops every channel narrower than a row -- Raccoon Strait and the Carquinez
    blink in and out with the crop, and worse, the shoreline changes shape when
    somebody passes a different --height.
    """
    lon0, lat0, lon1, lat1 = bbox
    la0, la1, lo0, lo1 = extent
    rows, cols = sea.shape
    r0 = (lat1 - la1) / (lat1 - lat0) * rows
    r1 = (lat1 - la0) / (lat1 - lat0) * rows
    c0 = (lo0 - lon0) / (lon1 - lon0) * cols
    c1 = (lo1 - lon0) / (lon1 - lon0) * cols
    rr = np.linspace(r0, r1, gh * sub, endpoint=False).astype(int).clip(0, rows - 1)
    cc = np.linspace(c0, c1, gw * sub, endpoint=False).astype(int).clip(0, cols - 1)
    fine = sea[np.ix_(rr, cc)].astype(f32)
    return fine.reshape(gh, sub, gw, sub).mean((1, 3)) >= 0.5


def cell_of(extent, shape, lat, lon):
    """(row, col) as floats, for a lat/lon on a `shape` grid over `extent`.

    Floats rather than ints because the aircraft need sub-pixel positions --
    rounding a plane to a whole pixel before dead-reckoning it quantises the
    motion into visible little hops -- and the callers that want a cell round
    it themselves.
    """
    la0, la1, lo0, lo1 = extent
    rows, cols = shape
    return ((la1 - lat) / (la1 - la0) * rows,
            (lon - lo0) / (lo1 - lo0) * cols)


def metres_per_degree(lat):
    return 110574.0, 111320.0 * math.cos(math.radians(lat))


def extent_metres(extent):
    la0, la1, lo0, lo1 = extent
    mlat, mlon = metres_per_degree(0.5 * (la0 + la1))
    return (lo1 - lo0) * mlon, (la1 - la0) * mlat


def _dilate(a):
    out = a.copy()
    out[1:] |= a[:-1]
    out[:-1] |= a[1:]
    out[:, 1:] |= a[:, :-1]
    out[:, :-1] |= a[:, 1:]
    return out


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises; everything that
# can still be wrong after that is wrong about *content*, and is caught here.
# Three states have to be drawable and they are different: absent means no file
# at all, stale means a file whose fixes are too old to extrapolate from, and
# empty means a perfectly good record that says the sky is quiet.
# --------------------------------------------------------------------------

_COLUMNS = ("hex", "call", "type", "cat", "lat", "lon", "alt", "gs", "trk",
            "dst", "pa")


def read_traffic(cache_dir):
    """(record, age, error). The record is columns as numpy arrays."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached ads-b record"
    payload, age = got
    try:
        n = int(payload["n"])
        t = float(payload["t"])
        cols = {c: list(payload[c]) for c in _COLUMNS}
    except Exception:                                        # noqa: BLE001
        return None, age, "ads-b record is malformed"
    if any(len(v) != n for v in cols.values()):
        # A truncated column would silently pair one aircraft's altitude with
        # another's position, which is a picture that looks entirely fine.
        return None, age, "ads-b record columns disagree about length"
    rec = {
        "n": n, "t": t, "age": age,
        "n_air": int(payload.get("n_air", n)),
        "n_ground": int(payload.get("n_ground", 0)),
        "n_seen": int(payload.get("n_seen", n)),
        "radius_nm": float(payload.get("radius_nm", 0.0)),
        "capped": bool(payload.get("capped", False)),
        "source": str(payload.get("source", "")),
        "hex": [str(x or "") for x in cols["hex"]],
        "call": [str(x or "") for x in cols["call"]],
        "type": [str(x or "") for x in cols["type"]],
        "cat": [str(x or "") for x in cols["cat"]],
    }
    for c in ("lat", "lon", "alt", "gs", "trk", "dst", "pa"):
        rec[c] = np.asarray(cols[c], np.float64)
    return rec, age, None


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--extent", default=",".join("%g" % v for v in EXTENT),
                    help="map crop as lat0,lat1,lon0,lon1")
    ap.add_argument("--reload", type=float, default=15.0,
                    help="seconds between re-reads of the cache (0 = never)")
    ap.add_argument("--ease", type=float, default=1.1,
                    help="seconds to ease a new fix in over; 0 snaps, which "
                         "reads as the panel glitching once a minute")
    ap.add_argument("--labels", type=int, default=2,
                    help="how many aircraft to name: the nearest, then the "
                         "lowest")
    ap.add_argument("--loop-seconds", type=float, default=9.0,
                    help="seconds for one pass across the hour")
    ap.add_argument("--hold", type=float, default=2.5,
                    help="seconds to hold on the finished picture")
    ap.add_argument("--samples", type=int, default=16,
                    help="trail points sampled per three-minute segment")
    ap.add_argument("--trail", type=float, default=1.0,
                    help="comet length multiplier; 0 draws bare heads")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second; 20 makes the "
                         "drift visible at a glance")


def parse_extent(s):
    parts = [float(x) for x in s.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise ValueError("--extent wants lat0,lat1,lon0,lon1")
    la0, la1, lo0, lo1 = parts
    return (min(la0, la1), max(la0, la1), min(lo0, lo1), max(lo0, lo1))


# --------------------------------------------------------------------------
# Layout and the static raster.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h):
        self.w, self.h = w, h
        # The status strip is the one thing that may not be squeezed out: it
        # carries the age, and the age is what says whether the map is true.
        self.status_h = 7 if h >= 24 else 6
        self.map_rows = max(4, h - self.status_h)
        self.status_y = self.map_rows


def marker(dst, r, c, rgb, arm=2):
    """A small cross. Clipped, and never wraps round the edge of the frame."""
    r, c = int(round(r)), int(round(c))
    if not (0 <= r < dst.shape[0] and 0 <= c < dst.shape[1]):
        return
    fill(dst, r, c - arm, r + 1, c + arm + 1, rgb)
    fill(dst, r - arm, c, r + arm + 1, c + 1, rgb)


def draw_map(dst, lay, sea, extent):
    """Land, water, a lit shoreline, the airports, the wall and a scale bar.

    Baked once. None of it is data -- the coastline does not go stale -- so it
    is drawn on the no-data and stale panels too, which is what stops those
    reading as a crash rather than as a wall with nothing to say.

    Returns the rectangles of the type it drew, so that a moving aircraft label
    can be kept off them. A callsign plate over the word HERE erases the one
    landmark on the panel that says which building this is.
    """
    boxes = []
    reg = dst[:lay.map_rows]
    reg[sea] = C_SEA
    reg[~sea] = C_LAND
    reg[_dilate(sea) & ~sea] = C_SHORE

    shape = (lay.map_rows, lay.w)
    for code, lat, lon, named in AIRPORTS:
        r, c = cell_of(extent, shape, lat, lon)
        if not (0 <= r < lay.map_rows and 0 <= c < lay.w):
            continue
        marker(reg, r, c, C_AIRPORT, 2 if named else 1)
        if named and lay.map_rows >= 20:
            # Below and right of the mark, and pulled back inside the panel
            # rather than clipped: half a three-letter code is not a code.
            x = int(min(lay.w - text_width(code) - 1, c + 4))
            y = int(min(lay.map_rows - 6, r + 2))
            blit_text(reg, y, x, code, C_DIM)
            boxes.append((x - 1, y - 1, x + text_width(code) + 1, y + 6))

    r, c = cell_of(extent, shape, *HOME)
    if 0 <= r < lay.map_rows and 0 <= c < lay.w:
        marker(reg, r, c, C_HOME, 2)
        x = int(min(lay.w - text_width(HOME_LABEL) - 1, c + 4))
        y = int(max(0, min(lay.map_rows - 6, r - 7)))
        blit_text(reg, y, x, HOME_LABEL, C_HOME)
        boxes.append((x - 1, y - 1, x + text_width(HOME_LABEL) + 1, y + 6))

    # A scale bar, bottom left, over the Pacific where nothing else goes. On a
    # map stretched three times horizontally the eye has no way to judge
    # distance, and "how far away is that" is the second question anybody asks
    # after "what is it".
    wm, _ = extent_metres(extent)
    bar = int(round(10.0 * NM_M * lay.w / wm))
    if bar + 6 < lay.w and lay.map_rows >= 16:
        y = lay.map_rows - 3
        fill(reg, y, 3, y + 1, 3 + bar, C_DIM)
        fill(reg, y - 2, 3, y, 4, C_DIM)
        fill(reg, y - 2, 3 + bar - 1, y, 3 + bar, C_DIM)
        blit_text(reg, y - 8, 3, "10NM", C_DIM)
        boxes.append((2, y - 9, 3 + bar + 1, y + 1))
    return boxes


def alt_table(size=256):
    """The altitude ramp as a lookup table, indexable 0..size-1."""
    return ds.gradient(ALT_STOPS, size)


def alt_index(alt, size=256):
    """Where an altitude in feet lands on the ramp. See ALT_CEIL on the sqrt."""
    f = np.sqrt(np.clip(np.asarray(alt, f32), 0.0, ALT_CEIL) / ALT_CEIL)
    return np.clip((f * (size - 1)).astype(np.int32), 0, size - 1)


def draw_status(dst, lay, table, rec, age, state, drawing):
    """The bottom strip: the counts, the altitude ramp, and the age.

    Right to left, because the age is the field that may not be dropped and
    everything to its left is measured against where it ends. A status line
    that overruns is one whose last field is quietly a different field.

    `drawing` says whether any aircraft actually reached the map this frame,
    and it changes two things. The counts go to half brightness, because a
    count nobody can check against the picture is a weaker claim than one they
    can. And the altitude ramp is left off entirely -- it is the legend for a
    colour scale, and a legend with nothing on the panel to legend is furniture
    that makes an empty map look like a working one.
    """
    y0 = lay.status_y
    fill(dst, y0, 0, y0 + 1, lay.w, (12, 14, 18))
    fill(dst, y0 + 1, 0, lay.h, lay.w, (0, 0, 0))
    ty = y0 + 1 + max(0, (lay.h - y0 - 1 - 5) // 2)

    if age is None:
        right, acol = "NO FIX", C_WARN
    else:
        right = ("STALE " if state == "stale" else "FIX ") + ftdata.describe_age(age)
        acol = C_TEXT if state == "fresh" else C_WARN
    rx = lay.w - 1 - text_width(right)
    blit_text(dst, ty, rx, right, acol)

    dim = 1.0 if drawing else 0.5
    x = 1
    if rec is not None:
        # The count is of what was *airborne in range*, not of what got kept.
        # Those differ only when the cap bites, and when it does the panel says
        # so rather than quietly reporting 120 aircraft on a busy afternoon.
        x += blit_text(dst, ty, x, "%d AIRBORNE" % rec["n_air"],
                       scale_colour(C_TEXT, dim)) + 5
        extra = ("NEAREST %d SHOWN" % rec["n"] if rec["capped"]
                 else "%d ON GND" % rec["n_ground"])
        if x + text_width(extra) < rx - 4:
            x += blit_text(dst, ty, x, extra, scale_colour(
                C_WARN if rec["capped"] else C_DIM, dim)) + 5
    else:
        x += blit_text(dst, ty, x, "NO DATA", C_WARN) + 5

    # The ramp, with its two ends labelled. It is the legend for the only
    # colour on the panel that carries a number, and it is four dozen pixels.
    ramp_w = 44
    need = text_width("0") + 2 + ramp_w + 2 + text_width("45K FT")
    if drawing and x + need < rx - 4:
        x += blit_text(dst, ty, x, "0", C_DIM) + 2
        idx = alt_index(np.linspace(0.0, ALT_CEIL, ramp_w))
        dst[ty + 1:ty + 4, x:x + ramp_w] = table[idx][None, :, :]
        x += ramp_w + 2
        blit_text(dst, ty, x, "45K FT", C_DIM)


def draw_nodata(dst, lay, lines):
    """The honest panel. The map, and words over it. No aircraft."""
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.map_rows // 2 - (len(lines) * (5 * scale + 3)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        # A dark plate behind the words. Type this small over a coastline is
        # unreadable, and the alternative -- blanking the whole map -- throws
        # away the one part of the panel that is still true.
        fill(dst, y - 2, x - 3, y + 5 * sc + 2, x + text_width(s, sc) + 3,
             (0, 0, 0))
        blit_text(dst, y, x, s, rgb, sc)
        y += 5 * sc + 4
    return dst


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    """An hour of Bay Area traffic, replayed as a Doppler-style loop.

    The panel used to draw a live "now" and dead-reckon each aircraft forward
    from its own last fix. That is why it polled once a minute: three minutes
    of extrapolation is 25 nm at 500 kn, on a map that only reaches 32.

    This draws the hour the fetcher has actually kept instead -- twenty frames,
    three minutes apart -- and the change of subject is what lets the wall ask
    adsb.fi for a twentieth as much. It also removes the guessing entirely:
    every position between two frames is an *interpolation* between two
    measured fixes, bounded by real data on both sides, where the old panel
    extrapolated past the newest one and had nothing to bound it.

    What accumulates is the point. Each pass lays down the tracks as they
    happened, so the arrival streams into SFO and Oakland draw themselves as
    bundles of parallel threads, the holding patterns show up as loops, and
    the whole hour is on the panel by the end of the pass.
    """
    w, h = args.width, args.height
    lay = Layout(w, h)
    extent = parse_extent(args.extent)
    cache = args.cache_dir
    now_of = clock(parse_when(args.at), args.rate)
    ttl = ftdata.ttl_for(PRODUCT) or 1200.0

    sea_full, bbox = load_sea()
    sea_map = crop_sea(sea_full, bbox, extent, w, lay.map_rows)
    table = alt_table()

    loop_s = max(2.0, float(args.loop_seconds))
    hold_s = max(0.0, float(args.hold))
    samples = max(2, int(args.samples))
    reload_s = max(1.0, float(args.reload))

    static = np.zeros((h, w, 3), np.uint8)
    draw_map(static, lay, sea_map, extent)
    frame = np.zeros((h, w, 3), np.uint8)

    def lay_out(payload):
        """Frames -> screen-space tracks, once per record.

        Everything the loop needs is built here: an (nframes, naircraft) grid
        of positions with gaps where an aircraft was not heard, and a flat bag
        of sampled trail points each carrying the fractional frame index it
        belongs to. The render loop is then a comparison and a fancy index --
        no trigonometry, no per-aircraft Python.
        """
        frames = payload.get("frames") or []
        frames = [f for f in frames if isinstance(f, dict) and f.get("lat")]
        if not frames:
            return None
        # One column per aircraft seen anywhere in the hour.
        order, index = [], {}
        for f in frames:
            for hx in f["hex"]:
                if hx and hx not in index:
                    index[hx] = len(order)
                    order.append(hx)
        nf, na = len(frames), len(order)
        if not na:
            return None
        rows = np.full((nf, na), np.nan, f32)
        cols = np.full((nf, na), np.nan, f32)
        alts = np.zeros((nf, na), np.int32)
        for k, f in enumerate(frames):
            idx = np.array([index[hx] for hx in f["hex"] if hx], np.int32)
            if not len(idx):
                continue
            keep = [j for j, hx in enumerate(f["hex"]) if hx]
            la = np.asarray([f["lat"][j] for j in keep], np.float64)
            lo = np.asarray([f["lon"][j] for j in keep], np.float64)
            r, c = cell_of(extent, (lay.map_rows, w), la, lo)
            rows[k, idx] = r
            cols[k, idx] = c
            alts[k, idx] = np.asarray([f["alt"][j] for j in keep], np.int32)

        # Trail points. A segment exists only where an aircraft was heard in
        # both of two consecutive frames; anything else is a gap and stays a
        # gap, because joining across a missing frame invents a straight line
        # through wherever it actually went.
        pr, pc, pt, pa = [], [], [], []
        for k in range(nf - 1):
            ok = ~np.isnan(rows[k]) & ~np.isnan(rows[k + 1])
            if not ok.any():
                continue
            r0, c0 = rows[k][ok], cols[k][ok]
            r1, c1 = rows[k + 1][ok], cols[k + 1][ok]
            a0 = alts[k][ok]
            for sidx in range(samples):
                u = f32(sidx) / samples
                pr.append(r0 + (r1 - r0) * u)
                pc.append(c0 + (c1 - c0) * u)
                pt.append(np.full(r0.shape, k + u, f32))
                pa.append(a0)
        if pr:
            pr = np.concatenate(pr)
            pc = np.concatenate(pc)
            pt = np.concatenate(pt)
            pa = np.concatenate(pa)
            ri = np.rint(pr).astype(np.int32)
            ci = np.rint(pc).astype(np.int32)
            on = ((ri >= 0) & (ri < lay.map_rows) & (ci >= 0) & (ci < w))
            ri, ci, pt, pa = ri[on], ci[on], pt[on], pa[on]
            flat = ri * w + ci
            rgb = table[alt_index(pa.astype(np.float64))].astype(f32)
        else:
            flat = np.zeros(0, np.int32)
            pt = np.zeros(0, f32)
            rgb = np.zeros((0, 3), f32)

        span = float(frames[-1]["t"]) - float(frames[0]["t"])
        return {"nf": nf, "na": na, "rows": rows, "cols": cols, "alts": alts,
                "flat": flat, "ptime": pt, "prgb": rgb,
                "t0": float(frames[0]["t"]), "t1": float(frames[-1]["t"]),
                "span": span, "times": [float(f["t"]) for f in frames],
                "n_last": int(np.count_nonzero(~np.isnan(rows[-1])))}

    cell = {"rec": None, "art": None, "err": None, "at": -1e9, "age": None}

    def refresh(now):
        if now - cell["at"] < reload_s:
            return
        cell["at"] = now
        rec, age, err = read_traffic(cache)
        cell["age"] = age
        if rec is None:
            cell["err"] = err
            return
        got = ftdata.load(PRODUCT, cache)
        payload = got[0] if got else {}
        art = lay_out(payload)
        cell["rec"] = rec
        cell["art"] = art
        cell["err"] = None if art else "ads-b record carries no frames yet"

    def status(dst, art, rec, age, phase_t):
        y = lay.status_y + 1
        dst[lay.status_y:lay.status_y + 1, :] = (14, 18, 24)
        x = 2
        stale = age is not None and age > ttl
        # The clock is the frame's own time, not the wall's: the whole point is
        # that this is the past being replayed, and a live clock over a
        # forty-minute-old picture is the one lie this panel could tell.
        lt = time.localtime(phase_t)
        x += blit_text(dst, y, x, time.strftime("%H:%M", lt),
                       C_WARN if stale else C_LABEL) + 5
        mins = int(round(art["span"] / 60.0)) if art else 0
        x += blit_text(dst, y, x, "%d MIN" % mins, C_TEXT) + 5
        x += blit_text(dst, y, x, "%d AC" % art["n_last"], C_TEXT) + 5
        if art and art["nf"] < 4:
            # Honest about a loop that is not an hour yet: a wall restarted ten
            # minutes ago has three frames, and calling that "the last hour"
            # would be the same lie in a different place.
            x += blit_text(dst, y, x, "FILLING", C_WARN) + 5
        src = (rec.get("source") or "").upper() if rec else ""
        if src:
            # adsb.fi ask to be cited. This is the citation.
            w_src = text_width(src)
            if x + w_src < lay.w - 2:
                blit_text(dst, y, lay.w - w_src - 2, src, C_DIM)

    def render(t, i=0):
        now = now_of()
        refresh(now)
        art, rec = cell["art"], cell["rec"]
        np.copyto(frame, static)
        if art is None:
            draw_nodata(frame, lay, [("NO ADS-B", C_WARN),
                                     (cell["err"] or "no data", C_DIM)])
            return frame

        nf = art["nf"]
        # One pass across the hour, then a hold on the finished picture.
        cycle = loop_s + hold_s
        p = (t % cycle) / loop_s
        if p >= 1.0:
            p = 1.0
        fpos = p * max(1e-6, nf - 1)

        mv = frame[:lay.map_rows].reshape(-1, 3)
        if len(art["flat"]):
            shown = art["ptime"] <= fpos
            if shown.any():
                fl = art["flat"][shown]
                age_f = fpos - art["ptime"][shown]
                # Older track dims but never vanishes: the hour is the subject,
                # so what has already been drawn has to stay legible.
                # Shallow, with a high floor. A steeper fade looked more
                # like a radar sweep but undid the point: by the end of the
                # pass most of the hour had dimmed out of sight, and the hour
                # is the subject. Old track recedes; it does not leave.
                k = np.clip(1.0 - age_f / max(1.0, nf * 2.0), 0.55, 1.0)
                mv[fl] = (art["prgb"][shown] * k[:, None]).astype(np.uint8)

        # The heads: where each aircraft is at this instant of the replay,
        # interpolated between the two frames it sits between.
        k0 = min(nf - 2, int(fpos)) if nf > 1 else 0
        u = fpos - k0 if nf > 1 else 0.0
        r0, c0 = art["rows"][k0], art["cols"][k0]
        if nf > 1:
            r1, c1 = art["rows"][k0 + 1], art["cols"][k0 + 1]
            both = ~np.isnan(r0) & ~np.isnan(r1)
            rr = np.where(both, r0 + (r1 - r0) * u, r0)
            cc = np.where(both, c0 + (c1 - c0) * u, c0)
        else:
            rr, cc = r0, c0
        live = ~np.isnan(rr)
        if live.any():
            ri = np.rint(rr[live]).astype(np.int32)
            ci = np.rint(cc[live]).astype(np.int32)
            on = (ri >= 0) & (ri < lay.map_rows) & (ci >= 0) & (ci < w)
            if on.any():
                head = table[alt_index(art["alts"][k0][live][on]
                                       .astype(np.float64))]
                mv[ri[on] * w + ci[on]] = np.minimum(
                    255, head.astype(np.int32) + 70).astype(np.uint8)

        status(frame, art, rec, cell["age"], art["times"][min(nf - 1, int(round(fpos)))])
        return frame

    return render


def main():
    # 20 fps rather than 30. Nothing on this panel moves faster than an
    # aircraft crossing three pixels a second, so the extra ten frames carry no
    # information, and the budget on the wall's throttled Pi is 50 ms a frame
    # at 20 and 33 at 30. A sequencer that wants another rate can ask.
    ds.standalone(sys.modules[__name__],
                  "live ADS-B traffic over the San Francisco Bay", fps=20)


if __name__ == "__main__":
    main()
