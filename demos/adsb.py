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

f32 = np.float32

COAST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "adsb-coast.npz")

PRODUCT = "adsb-bay"

# lat0, lat1, lon0, lon1. See the docstring on the three-fold stretch.
EXTENT = (37.47, 37.93, -122.94, -121.86)

# The wall. Distances on the panel are from here, and it is drawn on the map.
HOME = (37.7627, -122.3966)
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
    w, h = args.width, args.height
    lay = Layout(w, h)
    extent = parse_extent(args.extent)
    cache = args.cache_dir
    now_of = clock(parse_when(args.at), args.rate)
    ttl = ftdata.ttl_for(PRODUCT) or 300.0

    sea_full, bbox = load_sea()
    sea_map = crop_sea(sea_full, bbox, extent, w, lay.map_rows)
    wm, hm = extent_metres(extent)

    # Metres per second to pixels per second, on each axis separately. This is
    # the one place the three-fold stretch is applied, and applying it to the
    # velocity as well as to the position is what keeps a mark pointing along
    # the track it is actually flying across this map.
    px_per_ms_x = w / wm
    px_per_ms_y = lay.map_rows / hm

    table = alt_table()
    trail = max(0.0, float(args.trail))
    nlab = max(0, int(args.labels))

    static = np.zeros((h, w, 3), np.uint8)
    frame = np.zeros((h, w, 3), np.uint8)
    # The map half of the frame, flattened. Every aircraft is drawn through
    # this view as a flat scatter, which is what turns a hundred and twenty
    # marks into four numpy calls instead of a hundred and twenty.
    mv = frame[:lay.map_rows].reshape(-1, 3)
    # Shape (2, 1) so it broadcasts against both a (2, N) set of positions and
    # a (K, 2, N) set of streak samples; the row/column axis is the second from
    # the end in each.
    bounds = np.array([[lay.map_rows - 1.0], [w - 1.0]], f32)

    # `now` lives in here rather than being closed over so that a test can
    # replace it with a clock it drives itself. These panels are stateful --
    # the ease and the reload both depend on what happened last frame -- so a
    # test has to render a *sequence*, and a sequence it cannot time is a
    # sequence it cannot make an assertion about.
    cell = {"rec": None, "age": None, "fetch_age": None, "err": None,
            "state": "absent", "loaded": -1e18, "sig": None, "arr": None,
            "labels": [], "ease_off": None, "ease_at": 0.0,
            "status_key": None, "now": now_of}

    def bake(rec):
        """Everything about a record that does not change between frames.

        Positions at the record's own timestamp, velocity in pixels a second,
        the comet's unit vector and length, and four finished uint8 colour
        arrays -- one a streak sample. The frame loop then never touches an
        altitude, a bearing or a palette again.
        """
        n = rec["n"]
        if n == 0:
            return None
        row, col = cell_of(extent, (lay.map_rows, w), rec["lat"], rec["lon"])
        trk = np.radians(rec["trk"])
        ms = rec["gs"] * KNOT_MS
        vel = np.empty((2, n), f32)
        vel[0] = -(ms * np.cos(trk)) * px_per_ms_y      # north is up the screen
        vel[1] = (ms * np.sin(trk)) * px_per_ms_x
        ref = np.empty((2, n), f32)
        ref[0] = row
        ref[1] = col
        # Each aircraft's fix is its own age old, so each is carried forward to
        # the record's timestamp here. After this the render loop has one
        # scalar dt for the whole array rather than a vector.
        ref += vel * rec["pa"].astype(f32)

        speed = np.hypot(vel[0], vel[1])
        unit = vel / np.maximum(speed, 1e-6)
        # The comet is longer for something quick, which is most of what tells
        # a jet from a Cessna at this size, but it is clamped at both ends: a
        # helicopter still needs a direction and an airliner must not become a
        # streak long enough to be mistaken for a track history.
        length = np.clip(2.0 + rec["gs"].astype(f32) / 90.0, 2.4, 7.0) * trail
        step = unit * (length / max(1, len(TRAIL_FADE) - 1))

        base = table[alt_index(rec["alt"])]
        cols = [(base.astype(f32) * k).astype(np.uint8) for k in TRAIL_FADE]

        k = len(TRAIL_FADE)
        return {
            "n": n, "vel": vel, "ref": ref, "step": step, "cols": cols,
            "index": {hx: i for i, hx in enumerate(rec["hex"]) if hx},
            "offsets": -np.arange(k, dtype=f32).reshape(k, 1, 1),
            "pos": np.empty((2, n), f32),
            "streak": np.empty((k, 2, n), f32),
            "clipped": np.empty((k, 2, n), f32),
            "flat": np.empty((k, n), np.int32),
        }

    def positions(arr, dt, ease, out):
        """Every aircraft's position at `dt` seconds past the record, eased."""
        np.multiply(arr["vel"], f32(dt), out=out)
        np.add(out, arr["ref"], out=out)
        if ease is not None:
            np.add(out, ease, out=out)
        return out

    def pick_labels(rec, arr):
        """The nearest, then the lowest -- of the ones that are on the map.

        Chosen when the record lands rather than every frame: which aircraft is
        nearest changes once a minute, and re-deciding it thirty times a second
        would also make the labels flicker between two aircraft a tenth of a
        mile apart.

        Restricted to the crop, which is not fussiness. The query reaches fifty
        nautical miles and the map reaches about thirty-two, so the lowest
        aircraft in range is quite often something on short final at San Jose
        or Napa, off the panel entirely -- and a name that has nowhere to be
        drawn is one label rather than two, every time.
        """
        if arr is None or not nlab:
            return []
        row, col = arr["ref"][0], arr["ref"][1]
        on = ((row >= 0) & (row < lay.map_rows) & (col >= 0) & (col < w))
        idx = np.flatnonzero(on)
        if not len(idx):
            return []
        out = [int(idx[np.argmin(rec["dst"][idx])])]
        if nlab > 1:
            low = int(idx[np.argmin(rec["alt"][idx])])
            if low not in out:
                out.append(low)
        return out[:nlab]

    def label_text(rec, i):
        parts = [rec["call"][i] or rec["hex"][i].upper() or "?"]
        if rec["type"][i]:
            parts.append(rec["type"][i])
        parts.append("%dFT" % int(round(rec["alt"][i] / 25.0) * 25))
        return " ".join(parts)

    def reload_data(now):
        rec, fetch_age, err = read_traffic(cache)
        sig = None if rec is None else (rec["t"], rec["n"])
        if sig is not None and sig == cell["sig"]:
            # The same file, read again. Keep the baked arrays -- rebuilding
            # them would restart the ease from a correction of zero and throw
            # away the label choice for nothing.
            cell["loaded"] = now
            return

        old, old_arr = cell["rec"], cell["arr"]
        arr = bake(rec) if rec is not None else None
        # The correction, eased. Where an aircraft was about to be drawn, minus
        # where the new fix says it is: add that difference back in and decay it
        # to zero, and a minute-old extrapolation that was three pixels out
        # slides three pixels over a second instead of jumping.
        ease = None
        if (arr is not None and old_arr is not None and args.ease > 0
                and old is not None):
            was = positions(old_arr, now - old["t"], None,
                            np.empty((2, old_arr["n"]), f32))
            now_pos = positions(arr, now - rec["t"], None,
                                np.empty((2, arr["n"]), f32))
            ease = np.zeros((2, arr["n"]), f32)
            for hx, i in arr["index"].items():
                j = old_arr["index"].get(hx)
                if j is not None:
                    ease[0, i] = was[0, j] - now_pos[0, i]
                    ease[1, i] = was[1, j] - now_pos[1, i]
            # A correction of tens of pixels is not a correction, it is an
            # aircraft that was lost and re-acquired somewhere else. Sliding it
            # across the panel would draw a streak through places it never was.
            too_far = np.hypot(ease[0], ease[1]) > 24.0
            ease[:, too_far] = 0.0
            if not ease.any():
                ease = None

        cell.update({"rec": rec, "fetch_age": fetch_age, "err": err,
                     "sig": sig, "arr": arr, "loaded": now,
                     "labels": pick_labels(rec, arr) if rec is not None else [],
                     "ease_off": ease, "ease_at": now, "status_key": None})

    def rebuild_status(rec, age, state, drawing):
        draw_status(static, lay, table, rec, age, state, drawing)

    def draw_ring(i, pos, colour, boxes):
        """The circle round a named aircraft. Returns its centre, or None.

        The ring is not decoration. Two labels on a map of forty aircraft are
        useless unless it is obvious which two they are, and a line drawn from
        the text to the mark would cross half the Bay. It is blitted as one
        clipped mask rather than as twenty pixel writes, because twenty numpy
        calls a label is two milliseconds a frame on the wall's Pi.

        Every ring is drawn before any text, and each ring's own rectangle goes
        into `boxes` -- so a label can neither be painted over a ring nor have
        its plate rub one out. Getting that order wrong put a black plate
        through the second aircraft's circle and left it pointing at nothing.
        """
        r, c = float(pos[0, i]), float(pos[1, i])
        if not (0 <= r < lay.map_rows and 0 <= c < lay.w):
            return None
        ri, ci = int(round(r)), int(round(c))
        y0, x0 = ri - RING_R, ci - RING_R
        yy0, xx0 = max(0, y0), max(0, x0)
        yy1 = min(lay.map_rows, y0 + RING.shape[0])
        xx1 = min(lay.w, x0 + RING.shape[1])
        if yy1 > yy0 and xx1 > xx0:
            sub = RING[yy0 - y0:yy1 - y0, xx0 - x0:xx1 - x0]
            frame[yy0:yy1, xx0:xx1][sub] = colour
        boxes.append((xx0, yy0, xx1, yy1))
        return ri, ci

    def draw_label(rec, i, at, colour, boxes):
        """The callsign, type and altitude, beside the ring it belongs to."""
        if at is None:
            return
        ri, ci = at
        text = label_text(rec, i)
        tw = text_width(text)
        # Right of the mark by default, flipped left near the edge, and never
        # allowed off the panel: a clipped callsign is a different callsign.
        # Then, if that lands on a label already drawn, the other side and then
        # a row above or below are tried -- and if none of them is clear the
        # text is dropped and the ring left to speak for it. Two callsigns
        # printed over each other are worse than one callsign and a circle.
        y = max(0, min(lay.map_rows - 6, ri - 2))
        tries = []
        for dy in (0, -8, 8):
            for x in (ci + 6, ci - 6 - tw):
                tries.append((max(0, min(lay.map_rows - 6, y + dy)),
                              max(1, min(x, lay.w - tw - 1))))
        for ly, lx in tries:
            box = (lx - 2, ly - 1, lx + tw + 2, ly + 6)
            if any(box[0] < b[2] and b[0] < box[2]
                   and box[1] < b[3] and b[1] < box[3] for b in boxes):
                continue
            boxes.append(box)
            fill(frame, box[1], box[0], box[3], box[2], (0, 0, 0))
            blit_text(frame, ly, lx, text, colour)
            return

    def render(t, i=0):
        now = cell["now"]()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        rec, arr = cell["rec"], cell["arr"]
        # The age that matters is the age of the *fixes*, measured against the
        # demo's own clock -- not how long ago the socket was read. Two reasons.
        # ftdata rewrites `fetched_at` even when a fetch changes nothing, so the
        # fetch age can understate the data age; and --at and --rate move the
        # demo's present moment, so an age taken from the wall clock would let
        # --rate 20 extrapolate twenty minutes while still calling the record
        # fresh. This is the same number the dead reckoning is driven by, which
        # is the point: the panel goes stale exactly when the extrapolation it
        # is drawing stops being defensible.
        age = None if rec is None else max(0.0, now - rec["t"])
        state = ("absent" if rec is None else
                 "fresh" if age <= ttl else "stale")
        cell["age"], cell["state"] = age, state
        drawing = arr is not None and state == "fresh"
        key = (state, drawing, None if age is None else int(age),
               None if rec is None else (rec["n"], rec["n_air"],
                                         rec["n_ground"], rec["capped"]))
        if key != cell["status_key"]:
            rebuild_status(rec, cell["age"], state, drawing)
            cell["status_key"] = key

        np.copyto(frame, static)

        if rec is None:
            return draw_nodata(frame, lay, [
                ("NO ADS-B DATA", C_WARN),
                ("RUN  PYTHON3 FTDATA.PY --LOOP 60 --DUE --FAST", C_TEXT),
                ((cell["err"] or "").upper()[:52], C_DIM)])
        if state == "stale":
            # Extrapolating a five-minute-old fix at 500 knots puts an aircraft
            # forty miles from where it is. There is no honest picture to draw,
            # so none is drawn.
            return draw_nodata(frame, lay, [
                ("ADS-B DATA STALE", C_WARN),
                ("LAST FIX %s AGO -- IS THE FETCHER RUNNING"
                 % ftdata.describe_age(cell["age"]).upper(), C_TEXT)])
        if arr is None:
            return draw_nodata(frame, lay, [
                ("NO AIRCRAFT AIRBORNE WITHIN %d NM" % rec["radius_nm"], C_DIM)])

        dt = age                    # the clamped one: never extrapolate backwards
        ease = cell["ease_off"]
        if ease is not None:
            k = 1.0 - (now - cell["ease_at"]) / max(1e-6, args.ease)
            if k <= 0.0:
                cell["ease_off"] = ease = None
            else:
                # Smoothstep, so the correction starts and finishes still. A
                # linear ease stops dead at the end, which is a small visible
                # kink at exactly the moment the eye is following the mark.
                ease = ease * f32(k * k * (3.0 - 2.0 * k))

        pos = positions(arr, dt, ease, arr["pos"])
        streak, clipped = arr["streak"], arr["clipped"]
        if trail > 0:
            np.multiply(arr["step"], arr["offsets"], out=streak)
            np.add(streak, pos, out=streak)
        else:
            np.copyto(streak, pos)
        np.clip(streak, 0.0, bounds, out=clipped)
        # Anything the clip moved was off the map, and is not drawn. Testing it
        # this way rather than with four comparisons is one call for every
        # sample of every aircraft at once.
        on = np.all(clipped == streak, axis=1)
        np.copyto(arr["flat"], clipped[:, 0], casting="unsafe")
        np.multiply(arr["flat"], w, out=arr["flat"])
        np.add(arr["flat"], clipped[:, 1].astype(np.int32), out=arr["flat"])

        # Tail first so the head paints over it, and the head last so an
        # aircraft is never a tail with something else's head on it.
        cols = arr["cols"]
        for k in range(len(cols) - 1, -1, -1):
            m = on[k]
            mv[arr["flat"][k][m]] = cols[k][m]

        boxes, rings = list(map_boxes), []
        for j, idx in enumerate(cell["labels"]):
            colour = C_LABEL if j == 0 else C_TEXT
            rings.append((idx, draw_ring(idx, pos, colour, boxes), colour))
        for idx, at, colour in rings:
            draw_label(rec, idx, at, colour, boxes)
        return frame

    map_boxes = draw_map(static, lay, sea_map, extent)
    reload_data(now_of())
    render.state = cell               # the tests reach in here; nothing else
    render.layout = lay
    render.extent = extent
    render.sea_map = sea_map
    render.clock = now_of
    render.ttl = ttl
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
