#!/usr/bin/env python3
"""The ground, in a city that thinks about it.

Two maps and a number. On the left, the greater Bay Area at about 1.4 km to the
pixel, with the water filled, the eight strands of the plate boundary that run
through it, and every earthquake USGS located there in the last week. In the
middle, the same week out to 300 km, which is where Parkfield, the Mendocino
triple junction and the rest of the ground that shakes this city but has none
of its address live. On the right, the number the room actually wants: **how
many days since the last M4 within 100 km**, and which one that was.

**Most days this panel is nearly empty, and that is the answer.** A quiet week
is a scatter of one- and two-pixel dots around the Geysers and a long count of
days on the right, and that has to read as an instrument working rather than an
instrument broken. So the empty state is the designed one: the faults and the
coast are drawn whether or not anything happened on them, the count of days is
large type because a large number there is good news, the most recent event --
however small -- breathes once a second so the panel is visibly live, and the
word in the header is `QUIET`, which is a statement and not a shrug. There is
no state in which this demo shows nothing; there is only the state in which the
ground has done nothing, which looks quite different.

**And it has to become loud when something happens.** An M4+ within the last
six hours, an M5+ within a day, or an M6+ within three days takes the panel
over: the header turns red and blinks, the right-hand column becomes that one
earthquake -- magnitude in the largest type on the wall, place, distance and
bearing from this building, depth, how long ago -- and rings expand out of the
epicentre across whichever map it landed on. It is tested with a synthetic M5.8
written into a cache, because the alternative is waiting.

**Magnitude is logarithmic, and the marker is not scaled by it.** Scaling a
marker by magnitude directly gives an M6 twice the radius of an M3 for thirty
thousand times the energy, which is a lie in the flattering direction; scaling
by energy gives every M2 a radius of 10^-8 pixels, which is a lie in the other.
So the marker is not a symbol for the number at all -- it is drawn at **the size
of the ground that broke**, on the map's own scale. Wells & Coppersmith give
subsurface rupture length for a strike-slip event as log10(L) = 0.59 M - 2.44,
so the marker's radius is L/2 in kilometres, projected like everything else on
the map. An M7 is then 25 km across and dominates the panel, which is correct;
Loma Prieta comes out at 21 km of radius against a real rupture around 40 km
long, which is the right answer to within the width of a pixel. Below about
M4.5 the rupture is smaller than a pixel, so those get a floor of one pixel and
their magnitude is carried by **colour** instead -- a blue-to-red ramp -- and
their age by **brightness**, fading to about a quarter over the seven-day
window, with a white core on anything in the last hour. Three channels for
three quantities, and the one that is genuinely enormous is the one drawn to
scale.

**The Bay map is stretched, and there is no version of this that is not.** The
squash of a tile is fixed by arithmetic: it is the region's height-to-width
ratio times the tile's width-to-height ratio, so on a tile 155 by 57 a square
region is squashed 2.7 times whatever you do, and the only free choices are how
much ground to cover and where the distortion goes. The Bay tile covers
37.20-38.85 N, 121.15-123.55 W -- 210 km east to west, 183 north to south --
and comes out squashed 2.4 times vertically, at 1.36 km to the pixel across and
3.2 km down. Everything on it is squashed equally, including the rupture discs,
which is why a large event draws as a flat ellipse and not a circle; the ellipse
is the true shape on this projection. The 300 km tile is the opposite: 57 by 57
over a 620 x 600 km box, which is within 4% of true scale, and it carries range
rings at 100 and 300 km so the eye has something to measure against.

That extent is itself a trade. A tight crop of the Bay is a better-looking map
and, most weeks, an empty one: the busiest ground in northern California is the
Geysers geothermal field at 38.79 N, 60 km north of any crop that keeps the Bay
looking like the Bay, and it alone supplies half the week's local events.
Reaching up to it costs half a unit of squash and buys the map its earthquakes.

The geography is baked into this file rather than fetched or shipped as a
sidecar: a 1-bit water mask per tile, rasterised offline from the Natural Earth
1:10m ocean polygons, and eight fault traces from the USGS 2014 National Seismic
Hazard Model. See the note above them for why the water is a mask and not a
coastline -- the short version is that at this squash a drawn coastline has no
inside and no outside, and a filled bay is recognisable across a workshop.

**Nothing here touches the network.** `build()` calls `ftdata.load()` and
nothing else; `render()` touches neither disk nor socket. It has to be that way
round -- `ftsched` builds the next segment on a worker thread, Python threads
share the GIL, and a `build()` blocked on a socket stops the render loop getting
the interpreter back. Run the fetcher:

    $ python3 ftdata.py --loop 900

**Age is part of the data.** The fetch age is in the header in ftdata's short
form, it turns amber past the TTL, and past three TTLs the panel stops drawing
earthquakes altogether and says so -- an hour-old catalogue is fine, a day-old
one is a map of a city where nothing has happened since yesterday, which is a
different and much worse claim. No file at all gets the no-data card with the
command that fixes it.

**The frame budget is 50 ms**, which is what 20 fps costs on a Pi 3 held at
600 MHz. Almost nothing happens per frame: the maps, the type, the events and
the sparkline are rasterised in `build()` into one uint8 frame, and `render()`
copies it and repaints the pulse, the heartbeat and -- when there is one -- two
expanding rings. Measured over 400 sequential frames on this desktop that is
**p50 0.006 ms, p95 0.007 ms** quiet and **p50 0.035, p95 0.037** with an alert
on screen, the difference being the two ring masks over a whole tile. At the
76-114x this project keeps measuring between here and the wall, call it 0.8 ms
and 4 ms on the Pi. Neither is close to the budget and neither is the thing to
watch.

**The thing to watch is the rebake**, which is the one place this could still
stutter. Re-reading the cache and redrawing three hundred events across two
maps costs 3.4 ms here and therefore something like 350 ms on the wall -- seven
dropped frames, in one lump. Two things keep it off the panel. It only happens
when the record has actually changed, which is checked with an `os.stat` and
not by parsing 58 kB of JSON; on a ten-minute product that is once per ten
minutes at worst and usually never during a segment. And the biggest single
cost in it turned out to be `np.clip` on a scalar inside the colour lookup,
called six thousand times a rebake for 3.3 of the original 5.3 ms, which is now
a list index -- see MAG_TABLE.

Run:  python3 quake.py --host 127.0.0.1
      python3 quake.py --alert-demo       # the loud path, on the week's biggest
      FT_DATA_CACHE=/tmp/empty python3 quake.py    # the no-data card
      python3 scripts/test-quake.py
"""

import base64
import math
import os
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

PRODUCT = "quake-usgs"

# The wall's own address, matching ftdata's QUAKE_LAT/QUAKE_LON and wx.py's:
# Sequoia Fabrica, 1736 18th Street. It is drawn on both maps, because "how far
# from here" is the only question anybody asks of an earthquake map.
SITE_LAT, SITE_LON = 37.7627, -122.3966

# lat0, lat1, lon0, lon1.
#
# The Bay tile reaches from San Jose to Cloverdale rather than stopping at the
# Carquinez Strait, and that is a deliberate trade of proportion for content.
# A tight Bay crop is a better-looking map and on most weeks it is an *empty*
# one: the single most active piece of ground in northern California is the
# Geysers geothermal field at 38.79 N, which puts dozens of events a week on the
# panel and sits 60 km north of any crop that keeps the Bay looking like the
# Bay. Reaching up to it costs another 0.8 of vertical squash and buys the map
# most of its earthquakes. The region tile is 310 km from the wall in every
# direction, so the 300 km circle the product collects fits inside it with a
# pixel to spare, and its aspect is within 0.2% of true scale.
BAY_EXTENT = (37.20, 38.85, -123.55, -121.15)
REGION_EXTENT = (34.97, 40.55, -125.92, -118.87)

WEEK = 7 * 86400.0

# Wells & Coppersmith (1994), strike-slip subsurface rupture length:
# log10(L km) = a + b * M. The Bay Area's earthquakes are overwhelmingly
# strike-slip, which is the one place this demo gets to assume something.
WC_A, WC_B = -2.44, 0.59

# What makes the panel shout. Three thresholds rather than one because
# "recent" means something different at each size: an M4 is news for an
# afternoon, an M6 is news for days.
ALERT_STEPS = ((4.0, 6 * 3600.0), (5.0, 86400.0), (6.0, 3 * 86400.0))

# Past this many TTLs the catalogue stops being drawn at all. Same reasoning as
# propagation.py's: a little late is worth showing with a caveat, a day late is
# a picture of a different week.
STALE_MULTIPLE = 3.0

# --------------------------------------------------------------------------
# Colour. Two ramps and a small set of furniture.
#
# The magnitude ramp runs cold to hot because that is the direction every
# seismic map in the world runs, and because the panel's dark end is where the
# M1s live and blue survives being dim better than yellow does. The land is
# nearly black: on an LED wall a drawn coastline is worth more than any fill,
# and every pixel of fill is a pixel competing with an earthquake.
# --------------------------------------------------------------------------

C_BG = (3, 4, 6)
C_LAND = (22, 20, 17)                   # a shade up from black, warm
C_SEA = (2, 6, 18)                      # near black, and blue about it
C_COAST = (58, 92, 122)
C_FAULT = (78, 44, 16)                  # warm, so it never reads as coastline
C_RING = (22, 30, 40)
C_SITE = (60, 225, 160)                 # the building, and only the building
C_RULE = (18, 22, 30)
C_TEXT = (196, 214, 228)
C_DIM = (96, 110, 124)
C_LABEL = (76, 94, 116)
C_WARN = (255, 168, 40)
C_ALERT = (255, 62, 46)
C_GOOD = (110, 205, 150)

# M0 through M7+, sampled at whole magnitudes. Anything below M0 clamps to the
# first stop; the ANSS catalogue does contain negative magnitudes and they are
# real events, not errors.
MAG_STOPS = [(0.0, (46, 74, 150)), (1.0, (40, 118, 190)), (2.0, (0, 178, 200)),
             (3.0, (210, 200, 50)), (4.0, (255, 138, 24)),
             (5.0, (255, 62, 40)), (6.0, (255, 150, 140)),
             (7.0, (255, 245, 240))]

MAG_LUT = ds.gradient([(p / 7.0, c) for p, c in MAG_STOPS], 128)

# The same ramp again as plain Python tuples, and it is not redundancy. This is
# looked up six thousand times per rebake -- every event, on both tiles, plus
# the sparkline -- and the numpy version of that lookup cost 3.3 ms of the 5.3
# the rebake takes, nearly all of it in np.clip on a single scalar. A list of
# tuples and two builtins is the same answer for a twentieth of the time.
MAG_TABLE = [(int(r), int(g), int(b)) for r, g, b in MAG_LUT]


def mag_colour(mag):
    """The ramp, as a plain tuple. No numpy: see MAG_TABLE."""
    return MAG_TABLE[max(0, min(127, int(float(mag) * (127.0 / 7.0))))]


def scale_colour(rgb, k):
    return (int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k))


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 table, the same one propagation.py and tide.py use --
# five rows a glyph, each row an octal digit whose three bits are the three
# columns. A real typeface is mush at five pixels and the Pi does not have the
# same faces installed as the machine this was written on, so a baked font is
# the only one that is certainly there. Three glyphs are added for the things a
# seismic readout needs and a map of a nuclear exchange does not.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT.update({"+": "02720", "?": "71302", "!": "22202"})

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


def fit(s, width, scale=1):
    """Trim a string until it fits. What falls off the end is the least of it."""
    s = str(s)
    while s and text_width(s, scale) > width:
        s = s[:-1]
    return s


# --------------------------------------------------------------------------
# Words for numbers. All of these are formatted at bake time, never per frame.
# --------------------------------------------------------------------------

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(bearing):
    return COMPASS[int((float(bearing) % 360.0) / 22.5 + 0.5) % 16]


def since(seconds):
    """'8M', '3.2H', '4D'. Shorter than describe_age and in the same spirit."""
    if seconds < 90:
        return "%dS" % int(seconds)
    if seconds < 5400:
        return "%dM" % int(seconds / 60)
    if seconds < 172800:
        return "%.1fH" % (seconds / 3600.0)
    return "%.1fD" % (seconds / 86400.0)


def short_place(place):
    """'7 km ESE of Cloverdale, CA' -> 'CLOVERDALE'.

    The feed's own leading distance is dropped because the panel prints a
    distance from *this building* right next to it, and two different distances
    for one earthquake is the sort of detail that quietly destroys trust in a
    readout. The state suffix goes for room.
    """
    s = str(place or "")
    if " of " in s:
        s = s.split(" of ", 1)[1]
    s = s.split(",")[0]
    return s.strip().upper() or "UNKNOWN"


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises. Everything that
# can still be wrong afterwards is wrong about *content*, and is caught here.
# Three states have to be drawable and they are genuinely different: absent
# means no file, stale means a file too old to believe, and partial means the
# week's events arrived and the long baseline query did not. Only the first two
# stop the map.
# --------------------------------------------------------------------------

def record_stamp(cache_dir=None, product=PRODUCT):
    """(mtime, size) of the cache record, or None. A cheap 'has it changed?'.

    os.stat and nothing else: this is asked every reload interval and the point
    of asking is to avoid the expensive answer. Never raises -- a stat that
    fails means the same as a stamp that differs, which is 'go and look'.
    """
    try:
        path = ftdata.record_path(product, cache_dir)
        if path is None:
            return None
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


class Catalogue(object):
    """The week's earthquakes, or a reason there are none."""

    def __init__(self, cache_dir=None, now=None, product=PRODUCT):
        self.now = time.time() if now is None else float(now)
        self.problem = None
        self.age = None
        self.local = []
        self.world = []
        self.biggest = None
        self.baseline = None
        self.dropped = 0
        self.ttl = ftdata.ttl_for(product) or 3600.0

        got = ftdata.load(product, cache_dir)
        if got is None:
            self.problem = "no cached usgs catalogue"
            self.state = "absent"
            return
        payload, self.age = got
        self.state = ("fresh" if self.age <= self.ttl else
                      "aging" if self.age <= self.ttl * STALE_MULTIPLE
                      else "stale")
        try:
            local = payload["local"]
            self.dropped = int(local.get("non_earthquakes_dropped") or 0)
            self.local = [e for e in local["events"]
                          if e.get("mag") is not None and e.get("lat") is not None]
            world = payload.get("world") or {}
            self.world = [(float(t), float(m)) for t, m in
                          (world.get("events") or [])]
            self.biggest = world.get("biggest")
            self.baseline = payload.get("baseline")
        except Exception:                                    # noqa: BLE001
            self.problem = "usgs record is malformed"
            self.state = "absent"
            self.local, self.world = [], []
            return
        if self.state == "stale":
            # Not "dim it": drop it. A week-old catalogue drawn as if it were
            # this week's is the specific lie this panel could tell.
            self.problem = "catalogue is %s old" % ftdata.describe_age(self.age)
            self.local, self.world = [], []

    @property
    def usable(self):
        return self.state in ("fresh", "aging")

    def recent(self):
        """The most recent local event, or None. The list arrives sorted."""
        return self.local[0] if self.local else None

    def largest(self):
        if not self.local:
            return None
        return max(self.local, key=lambda e: e["mag"])

    def alert(self):
        """The event that should take the panel over, if any.

        Largest first, not most recent: during an aftershock sequence the
        newest event is a small one and the M5.8 forty minutes ago is still the
        news. Anything that clears one of the (magnitude, window) steps
        qualifies, and among those the biggest wins.
        """
        best = None
        for e in self.local:
            age = self.now - e["t"]
            if age < 0:
                continue
            for mag, window in ALERT_STEPS:
                if e["mag"] >= mag and age <= window:
                    if best is None or e["mag"] > best["mag"]:
                        best = e
                    break
        return best

    def days_since_baseline(self):
        """Days since the last M4+ within 100 km, or None if unknown.

        The week's events are consulted as well as the stored baseline: the
        FDSN query behind that field runs at fetch time and a local M4 that
        happened four minutes ago will be in the feed before it is in the
        answer. Taking the later of the two is what stops the panel saying
        '129 days' with the earthquake still on screen.
        """
        latest = None
        if self.baseline and self.baseline.get("t"):
            latest = self.baseline
        for e in self.local:
            if e["mag"] >= 4.0 and e["km"] <= 100.0:
                if latest is None or e["t"] > latest["t"]:
                    latest = e
        if latest is None:
            return None, None
        return max(0.0, (self.now - latest["t"]) / 86400.0), latest


# --------------------------------------------------------------------------
# Projection. Two tiles, each a plain equirectangular box; the anisotropy is
# whatever the box and the pixels work out to and is not hidden.
# --------------------------------------------------------------------------

class Tile(object):
    def __init__(self, x, y, w, h, extent, name):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.la0, self.la1, self.lo0, self.lo1 = extent
        self.name = name
        # Pixels per degree, and from those pixels per kilometre along each
        # axis. The two differ -- that is the squash -- and every radius drawn
        # on this tile has to carry both.
        self.px_lon = w / (self.lo1 - self.lo0)
        self.px_lat = h / (self.la1 - self.la0)
        mid = math.radians(0.5 * (self.la0 + self.la1))
        self.km_lon = 111.320 * math.cos(mid)
        self.km_lat = 111.132
        self.px_km_x = self.px_lon / self.km_lon
        self.px_km_y = self.px_lat / self.km_lat

    def project(self, lat, lon):
        """(row, col) in tile-local pixels, as floats. May be off the tile."""
        return ((self.la1 - float(lat)) * self.px_lat,
                (float(lon) - self.lo0) * self.px_lon)

    def holds(self, lat, lon, margin=0.0):
        r, c = self.project(lat, lon)
        return -margin <= r < self.h + margin and -margin <= c < self.w + margin

    def region(self, frame):
        return frame[self.y:self.y + self.h, self.x:self.x + self.w]


def polyline(dst, tile, pts, rgb):
    """Draw a lat/lon polyline into a tile region.

    One np.linspace per segment and a single fancy-index write, which is dear
    per call and irrelevant here: this runs once, over a couple of hundred
    points, in build().
    """
    h, w = dst.shape[:2]
    prev = None
    for lat_lon in pts:
        lon, lat = lat_lon
        r, c = tile.project(lat, lon)
        if prev is not None:
            r0, c0 = prev
            n = max(int(abs(r - r0)), int(abs(c - c0)), 1)
            rr = np.round(np.linspace(r0, r, n + 1)).astype(int)
            cc = np.round(np.linspace(c0, c, n + 1)).astype(int)
            ok = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
            if ok.any():
                dst[rr[ok], cc[ok]] = np.maximum(dst[rr[ok], cc[ok]],
                                                 np.array(rgb, np.uint8))
        prev = (r, c)


def ring(dst, tile, lat, lon, km, rgb):
    """A range ring at `km` from a point, drawn in the tile's own squash."""
    r0, c0 = tile.project(lat, lon)
    a = np.linspace(0.0, 2.0 * math.pi, 256)
    rr = np.round(r0 - np.sin(a) * km * tile.px_km_y).astype(int)
    cc = np.round(c0 + np.cos(a) * km * tile.px_km_x).astype(int)
    ok = ((rr >= 0) & (rr < dst.shape[0]) & (cc >= 0) & (cc < dst.shape[1]))
    if ok.any():
        dst[rr[ok], cc[ok]] = rgb


def rupture_km(mag):
    """Half the subsurface rupture length, in kilometres. See the docstring."""
    return 0.5 * 10.0 ** (WC_A + WC_B * float(mag))


def blob(dst, r0, c0, ry, rx, rgb):
    """An ellipse, max-blended so overlapping events brighten rather than
    overwrite. Below a pixel it is a pixel: everything gets drawn.

    The single-pixel case is separated out because it is nearly every event --
    a week within 300 km is three hundred earthquakes and four of them are
    bigger than a pixel -- and the general path costs five numpy calls where
    this costs one.
    """
    h, w = dst.shape[:2]
    col = np.array(rgb, np.uint8)
    if ry <= 0.75 and rx <= 0.75:
        r, c = int(round(r0)), int(round(c0))
        if 0 <= r < h and 0 <= c < w:
            np.maximum(dst[r, c], col, out=dst[r, c])
        return
    y0, y1 = int(math.floor(r0 - ry)), int(math.ceil(r0 + ry)) + 1
    x0, x1 = int(math.floor(c0 - rx)), int(math.ceil(c0 + rx)) + 1
    y0, x0 = max(0, y0), max(0, x0)
    y1, x1 = min(h, y1), min(w, x1)
    if y1 <= y0 or x1 <= x0:
        return
    dy = (np.arange(y0, y1, dtype=f32) + 0.5 - r0) / max(ry, 0.5)
    dx = (np.arange(x0, x1, dtype=f32) + 0.5 - c0) / max(rx, 0.5)
    m = (dy[:, None] ** 2 + dx[None, :] ** 2) <= 1.0
    if not m.any():
        m[(y1 - y0) // 2, (x1 - x0) // 2] = True
    # `sub[m] = np.maximum(sub[m], col)` and not `np.maximum(..., out=sub[m])`:
    # a boolean index produces a copy, so the out= form writes the answer into
    # a temporary and throws it away. It looks right and does nothing.
    sub = dst[y0:y1, x0:x1]
    sub[m] = np.maximum(sub[m], col)


def age_fade(age, span=WEEK):
    """1.0 for something that just happened, ~0.26 at the end of the window.

    A power law rather than an exponential: an exponential with a time constant
    short enough to make yesterday look different from today makes last Tuesday
    invisible, and the whole point of a seven-day window is that last Tuesday
    is on it.
    """
    k = min(1.0, max(0.0, age / span))
    return 1.0 - 0.74 * (k ** 0.6)


# --------------------------------------------------------------------------
# The geography, baked.
#
# **Water as a mask, not as a coastline.** The first version of this drew the
# Natural Earth coastline as a polyline and it was unreadable: at this squash
# the Bay, Suisun Bay and the Delta become a horizontal scribble of thin lines
# with no inside and no outside, and the eye cannot tell a channel from a
# fault. Filled water is recognisable at a glance and the shoreline falls out
# of it for free -- dilate the mask and subtract it, exactly as tide.py does.
#
# So each tile carries a 1-bit water mask, rasterised offline from the Natural
# Earth 1:10m ocean polygons at 8x8 supersampling and thresholded at half a
# pixel of water, packed with np.packbits and base64'd. The Bay tile's is
# 150x57 = 1069 bytes and the region tile's is 421; the pair costs about
# thirty lines of source, which is cheaper than a sidecar file that can go
# missing and much cheaper than a vector fill at run time. A canvas that is not
# 320x64 gets the mask nearest-resampled, which is the same thing every other
# demo here does with its geography.
#
# The faults are the eight principal strands of the USGS 2014 National Seismic
# Hazard Model fault sections (hazfaults2014) -- the seismogenic model rather
# than the Quaternary fault map: fewer, longer, and the ones that carry the
# slip. There are 74 sections in this box; drawing all of them is a grey smear
# and drawing these eight is a plate boundary. They are very nearly straight,
# so eight systems cost 42 points between them, as (lon, lat) in GeoJSON order.
# --------------------------------------------------------------------------

# Bay tile, 155 x 57, 32% water, 1105 bytes packed.
SEA_BAY = (
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAGAAAAAAAAAAAAAAAAAAAAAAAAAPgAAAAAAAAAAAAAAAAAAAAAAAAfwAAAAA"
    "AAAAAAAAAAAAAAAAAAA/4AAAAAAAAAAAAAAAAAAAAAAAB/4AAAAAAAAAAAAAAAAAAAAA"
    "AAD/8AAAAAAAAAAAAAAAAAAAAAAAH/+AAAAAAAAAAAAAAAAAAAAAAAP//AAAAAAAAAAA"
    "AAAAAAAAAAAAf//wAAAAAAAAAAAAAAAAAAAAAA///+AAAAAAAAAAAAAAAAAAAAAB////"
    "gAAAAAAAAAAAAAAAAAAAAD////wAAAAAAAAAAAAAAAAAAAAH////wAAAAAAAAAAAAAAA"
    "AAAAAP////wAAAAAAAAAAAAAAAAAAAAf////wAAAAAAAAAAAAAAAAAAAA/////gAAAAA"
    "AAAAAAAAAAAAAAB/////+AAAAAAAAAAAAAAAAAAAD/////+AAAAAAAAAAAAAAAAAAAH/"
    "////4AAAAAAAAAAAAAAAAAAAP/////9AAAAAAAAAAAAAAAAAAAf/////4gAAAAAAAAAA"
    "AAAAAAAA//////4gAAAAMAAAAAAAAAAAAB//////gQAAAP/gAB8AAEAAAAAD/////+AA"
    "AAAf/wAP4AAQBAAAAH/////4gAAAB//4R/fH4EcAAAAP/////jwAAAD//AIAAD5gQEAA"
    "Af/////P/AAAA+AAAAAAAAAAAAA////////gAAH4AAAAAAAAAAAAB////////gAAfAAA"
    "AAAAAAAAAAD////////4AAfgAAAAAAAAAAAAH/////////4BP+AAAAAAAAAAAAP/////"
    "////+B/+AAAAAAAAAAAAf//////////n/4AAAAAAAAAAAA///////////4HgAAAAAAAA"
    "AAAB///////////AP4AAAAAAAAAAAD//////////+AH8AAAAAAAAAAAH//////////+A"
    "/8AAAAAAAAAAAP//////////8B//gAAAAAAAAAAf//////////4B//gAAAAAAAAAA///"
    "////////wB//AAAAAAAAAAB///////////AD//AAAAAAAAAAD//////////+AAD/AAAA"
    "AAAAAAH//////////8AAA/AAAAAAAAAAP//////////4AAAfAAAAAAAAAAf/////////"
    "//AAADwAAAAAAAAA///////////+AAAAAAAAAAAAAB///////////+AAAAAAAAAAAAAD"
    "///////////+AAAAAAAAAAAAAH///////////+AAAAAAAAAAAAAP///////////8AAAA"
    "AAAAAAAAAf///////////4AAAAAAAAAAAAA////////////gAAAAAAAAAAAAB///////"
    "/////AAAAAAAAAAAAAD////////////AAAAAAAAAAAAAAA=="
)

# region tile, 57 x 57, 46% water, 407 bytes packed.
SEA_REGION = (
    "//AAAAAAAH/4AAAAAAA//gAAAAAAH/8AAAAAAA//wAAAAAAH//AAAAAAA//8AAAAAAH/"
    "/wAAAAAA//+AAAAAAH//wAAAAAA//+AAAAAAH//wAAAAAA//+AAAAAAH//wAAAAAA///"
    "AAAAAAH//4AAAAAA///AAAAAAH//4AAAAAA///wAAAAAH//+AAAAAA///4AAAAAH///g"
    "AAAAA///+AAAAAH///4AAAAA////AAAAAH///4QAAAA////gAAAAH////QAAAA////8A"
    "AAAH////oAAAA////4gAAAH////gAAAA////8AAAAH////gAAAA////+AAAAH////4AA"
    "AA/////AAAAH/////AAAA/////4AAAH/////AAAA/////wAAAH////+AAAA/////wAAA"
    "H/////AAAA/////8AAAH/////wAAA/////+AAAH/////4AAA//////gAAH/////8AAA/"
    "/////wAAH//////AAA//////+AAH//////wAA//////+AAH//////8AA///////gAAA="
)

FAULTS = (
    ("San Andreas", (
        (
            (-122.6651,37.8965), (-123.0799,38.3673),
            (-123.5901,38.8796),
        ),
        (
            (-122.0047,37.1763), (-122.3560,37.5149),
            (-122.5453,37.7231), (-122.6651,37.8965),
        ),
    )),
    ("Hayward", (
        (
            (-122.2161,37.8321), (-122.4130,38.0575),
            (-122.4740,38.1963),
        ),
        (
            (-121.8024,37.3971), (-121.9145,37.4831),
            (-122.2161,37.8321),
        ),
        (
            (-121.6638,37.2340), (-121.7247,37.2578),
            (-121.8024,37.3971),
        ),
    )),
    ("Rodgers Creek-Maacama", (
        (
            (-122.4400,38.1667), (-122.9963,38.7625),
        ),
        (
            (-122.6486,38.5004), (-122.8019,38.6810),
            (-122.9580,38.8157),
        ),
    )),
    ("Calaveras", (
        (
            (-122.0438,37.8456), (-121.9606,37.7555),
            (-121.8082,37.4451),
        ),
        (
            (-121.8164,37.4567), (-121.6050,37.1741),
        ),
    )),
    ("Concord-Green Valley", (
        (
            (-122.0890,38.0436), (-121.9907,37.9003),
        ),
        (
            (-122.2231,38.4154), (-122.0890,38.0436),
        ),
    )),
    ("Greenville", (
        (
            (-121.5417,37.5074), (-121.8017,37.8273),
            (-121.8753,37.8715),
        ),
        (
            (-121.4694,37.2544), (-121.5417,37.5074),
        ),
    )),
    ("San Gregorio", (
        (
            (-122.3152,37.1637), (-122.4410,37.4238),
            (-122.5148,37.5231), (-122.7020,37.9354),
        ),
    )),
    ("West Napa", (
        (
            (-122.2445,38.1644), (-122.3202,38.3075),
            (-122.4825,38.5069),
        ),
    )),
)


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--span", type=float, default=168.0,
                    help="hours of catalogue drawn; the product holds a week")
    ap.add_argument("--reload", type=float, default=600.0,
                    help="seconds between re-reads of the cache (0 = never)")
    ap.add_argument("--pulse-hz", type=float, default=0.75,
                    help="rate the most recent event breathes at; 0 holds it "
                         "lit, for a still photograph")
    ap.add_argument("--ring-period", type=float, default=1.9,
                    help="seconds an alert ring takes to reach 300 km")
    ap.add_argument("--alert-demo", action="store_true",
                    help="draw the alert layout against the strongest event in "
                         "the cache, whenever it happened; for looking at the "
                         "loud path without waiting for one")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")


def parse_when(s):
    """'now', an epoch, or 'YYYY-MM-DD HH:MM' in local time. As tide.py."""
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


# --------------------------------------------------------------------------
# Layout. Proportional with floors, so a canvas other than 320x64 gives
# something sane rather than an exception or a heap of overlapping type.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.head_h = 6 if h >= 40 else 5
        self.body_y = self.head_h + 1
        self.body_h = max(6, h - self.body_y)
        # The right-hand column wants 26 characters of 3x5 type; the region
        # tile wants to stay square-ish or its range rings stop being rings.
        self.col_w = int(min(112, max(48, round(w * 0.325))))
        self.reg_w = int(min(64, max(20, round(self.body_h))))
        self.bay_w = w - self.col_w - self.reg_w - 4
        if self.bay_w < 40:                     # a very narrow canvas
            self.reg_w = max(0, self.reg_w + min(0, self.bay_w - 40))
            self.bay_w = w - self.col_w - self.reg_w - 4
        self.bay_x = 0
        self.reg_x = self.bay_w + 2
        self.col_x = self.reg_x + self.reg_w + 2

    def tiles(self):
        bay = Tile(self.bay_x, self.body_y, self.bay_w, self.body_h,
                   BAY_EXTENT, "bay")
        reg = (Tile(self.reg_x, self.body_y, self.reg_w, self.body_h,
                    REGION_EXTENT, "region") if self.reg_w >= 12 else None)
        return bay, reg


# --------------------------------------------------------------------------
# The static picture.
# --------------------------------------------------------------------------

def unpack_sea(b64, w, h, out_w, out_h):
    """A baked water mask, nearest-resampled to the tile actually drawn.

    Nearest and not area-averaged, unlike tide.py's crop_sea: that one resamples
    a DEM many times finer than its target and has to average or it loses whole
    channels, whereas this mask was rasterised at exactly the geometry it is
    normally drawn at and the resample only happens on a canvas that is not
    320x64. Averaging there would blur a shoreline that is already one pixel.
    """
    bits = np.unpackbits(np.frombuffer(base64.b64decode(b64), np.uint8))
    m = bits[:w * h].reshape(h, w).astype(bool)
    if (out_h, out_w) == (h, w):
        return m
    rr = np.clip((np.arange(out_h) + 0.5) * h / out_h, 0, h - 1).astype(int)
    cc = np.clip((np.arange(out_w) + 0.5) * w / out_w, 0, w - 1).astype(int)
    return m[np.ix_(rr, cc)]


def shoreline(sea):
    """The land pixels touching water. Four slice-ORs and no library."""
    o = np.zeros_like(sea)
    o[1:] |= sea[:-1]
    o[:-1] |= sea[1:]
    o[:, 1:] |= sea[:, :-1]
    o[:, :-1] |= sea[:, 1:]
    return o & ~sea


def draw_geography(frame, tile, sea, faults, rings):
    """Water, land, shoreline, faults, range rings and this building.

    Once, in build(). The order is the order they have to be read in: the fill
    says which side of the line is which, the shoreline gives the shape its
    edge, and the faults go over the top because a fault that stops at a
    coastline has been drawn wrong.
    """
    reg = tile.region(frame)
    reg[:] = C_LAND
    reg[sea] = C_SEA
    reg[shoreline(sea)] = C_COAST
    for km in rings:
        ring(reg, tile, SITE_LAT, SITE_LON, km, C_RING)
    for name, lines in faults:
        for line in lines:
            polyline(reg, tile, line, C_FAULT)
    # The building last and brightest, a two-pixel cross rather than a dot:
    # a single lit pixel among three hundred earthquakes is another earthquake.
    r, c = tile.project(SITE_LAT, SITE_LON)
    r, c = int(round(r)), int(round(c))
    if 0 <= r < tile.h and 0 <= c < tile.w:
        reg[max(0, r - 1):r + 2, c] = C_SITE
        reg[r, max(0, c - 1):c + 2] = C_SITE


def draw_events(frame, tile, events, now, span):
    """Every event that lands on this tile, oldest first so new ones win.

    Oldest first matters. The blend is a maximum, so a bright fresh dot painted
    before a dim old one still wins on most channels -- but not all of them,
    and a fresh M1 half-overwritten by last Tuesday's M2 is exactly the kind of
    wrong that nobody notices.
    """
    reg = tile.region(frame)
    drawn = 0
    for e in reversed(events):
        age = now - e["t"]
        if age < 0 or age > span:
            continue
        r, c = tile.project(e["lat"], e["lon"])
        if not (-2 <= r < tile.h + 2 and -2 <= c < tile.w + 2):
            continue
        km = rupture_km(e["mag"])
        fade = age_fade(age, span)
        col = scale_colour(mag_colour(e["mag"]), fade)
        blob(reg, r, c, km * tile.px_km_y, km * tile.px_km_x, col)
        # Anything in the last hour gets a white core, which is the one thing
        # on this map that cannot be confused with anything else.
        if age <= 3600.0 and 0 <= int(round(r)) < tile.h and 0 <= int(round(c)) < tile.w:
            reg[int(round(r)), int(round(c))] = (255, 250, 245)
        drawn += 1
    return drawn


def draw_scale_bar(frame, tile, km, label):
    """A bar of known length in the tile's bottom left, with its length on it.

    Two maps at two scales on one panel is a good way to mislead somebody
    unless each says what it is, and a bar says it in a form that survives
    being photographed and cropped.
    """
    reg = tile.region(frame)
    n = int(round(km * tile.px_km_x))
    if n < 4 or n > tile.w - 6 or tile.h < 14:
        return
    y = tile.h - 2
    reg[y, 2:2 + n] = C_LABEL
    reg[y - 1:y + 1, 2] = C_LABEL
    reg[y - 1:y + 1, 2 + n - 1] = C_LABEL
    blit_text(reg, y - 7, 2, label, C_LABEL)


# --------------------------------------------------------------------------
# The header, and the right-hand column, which is where the panel does its
# talking. Both are baked; neither is laid out per frame.
# --------------------------------------------------------------------------

def header_text(cat, args):
    """(left, middle, middle colour, right). Middle is the state of the world.

    Each part has a ladder of shorter forms and the widest set that fits is
    drawn, as tide.py does it, because what falls off the end of a clipped
    status line is the age -- and the age is the last thing that should go
    quietly missing.
    """
    if not cat.usable:
        # Two different sentences, because they are two different problems. A
        # record we have and will not believe is not the same as no record, and
        # the person who has to fix them does different things about each.
        mid = ("CATALOGUE %s OLD -- NOT DRAWN" % ftdata.describe_age(cat.age)
               if cat.age is not None else "NO CATALOGUE")
        return "USGS ANSS", mid, C_ALERT, (
            ftdata.describe_age(cat.age) if cat.age is not None else "")

    alert = cat.alert()
    if alert is not None:
        mid = "M%.1f  %d KM %s  %s AGO" % (
            alert["mag"], round(alert["km"]), compass(alert["bearing"]),
            since(cat.now - alert["t"]))
        return "", mid, C_ALERT, ftdata.describe_age(cat.age)

    big = cat.largest()
    if big is None:
        mid = "NOTHING LOCATED IN 7 DAYS"
    else:
        mid = "QUIET   LARGEST 7D  M%.1f %s AGO" % (
            big["mag"], since(cat.now - big["t"]))
    return "USGS ANSS", mid, C_GOOD, ftdata.describe_age(cat.age)


def draw_header(dst, lay, cat, args, lit=True):
    """The status strip. `lit` is the blink phase; only alerts use it."""
    alert = cat.alert() if cat.usable else None
    dst[:lay.head_h] = C_BG
    left, mid, midc, right = header_text(cat, args)
    if alert is not None and not lit:
        midc = scale_colour(midc, 0.3)

    rw = 0
    if right:
        label = ("DATA " + right) if lay.w >= 240 else right
        rgb = C_TEXT if cat.state == "fresh" else C_WARN
        rw = text_width(label)
        blit_text(dst, 0, lay.w - rw - 1, label, rgb)
    if cat.state != "fresh" and cat.age is not None:
        # The word, not just an amber tint. A colour shift on a wall seen from
        # across a workshop is not a message.
        flag = "STALE" if cat.state == "stale" else "OLD"
        fw = text_width(flag)
        blit_text(dst, 0, lay.w - rw - 3 - fw, flag,
                  C_ALERT if not (alert is not None and not lit) else C_DIM)
        rw += fw + 2
    lw = text_width(left) if left else 0
    if left:
        blit_text(dst, 0, 1, left, C_DIM)
    if mid:
        mid = fit(mid, lay.w - lw - rw - 8)
        mx = max(lw + 4, min(lay.w - rw - 3 - text_width(mid),
                             (lay.w - text_width(mid)) // 2))
        blit_text(dst, 0, mx, mid, midc)
    dst[lay.head_h] = C_ALERT if (alert is not None and lit) else C_RULE


def draw_column(dst, lay, cat, args):
    """The right-hand column: one big number, or one big earthquake.

    `dst` is the column's own buffer, so everything here is in column-local
    coordinates and the whole thing can be repainted without touching the maps.
    """
    w, h = lay.col_w, lay.body_h
    dst[:] = C_BG
    alert = cat.alert() if cat.usable else None

    if not cat.usable:
        lines = [("NO USGS DATA", C_ALERT),
                 ("RUN", C_LABEL), ("FTDATA.PY", C_TEXT), ("--LOOP 900", C_TEXT)]
        y = max(0, h // 2 - len(lines) * 3)
        for s, rgb in lines:
            blit_text(dst, y, max(0, (w - text_width(s)) // 2), s, rgb)
            y += 6
        return

    if alert is not None:
        _draw_alert_column(dst, w, h, cat, alert)
        return

    # ---------------------------------------------------------------- quiet
    # The headline. A big number here is good news, which is the whole reason
    # it is the big number: the panel's loudest element on a normal day is a
    # count of how long nothing has happened.
    days, ev = cat.days_since_baseline()
    blit_text(dst, 0, 0, fit("DAYS SINCE M4 WITHIN 100KM", w), C_LABEL)

    scale = 3 if h >= 40 and w >= 60 else 2
    txt = "--" if days is None else ("%d" % int(days))
    while scale > 1 and text_width(txt, scale) > w - 2:
        scale -= 1
    blit_text(dst, 6, 0, txt, C_TEXT if days is not None else C_DIM, scale)
    if days is not None and days < 1.0:
        # Zero days is the one value that needs a word next to it, because a
        # bare 0 reads as a broken counter rather than as "today".
        blit_text(dst, 6 + 5 * scale - 5, text_width(txt, scale) + 4, "TODAY",
                  C_ALERT)
    y = 6 + 5 * scale + 1
    if ev is not None:
        blit_text(dst, y, 0, fit("M%.1f %s" % (ev["mag"], short_place(ev["place"])), w),
                  C_DIM)
    elif days is None:
        blit_text(dst, y, 0, fit("BASELINE QUERY FAILED", w), C_WARN)
    y += 6

    # The world strip owns the bottom eleven rows and everything above stops
    # there. Letting the rows run to `h` and then drawing the strip over them
    # is how the first version of this put "BIGGEST M4.4 COVELO" through the
    # middle of the sparkline's label -- text over text still looks like a
    # working panel from a distance, which is what makes it the worst failure.
    strip_h = 11 if (h >= 46 and cat.world) else 0
    limit = h - strip_h

    big = cat.largest()
    rec = cat.recent()
    rows = [("%d IN %dD WITHIN %dKM" % (len(cat.local),
                                       round(args.span / 24.0), 300), C_DIM)]
    if big is not None:
        rows.append(("BIGGEST M%.1f %s" % (big["mag"], short_place(big["place"])),
                     mag_colour(big["mag"])))
    if rec is not None:
        rows.append(("LATEST  M%.1f  %s AGO" % (rec["mag"],
                                                since(cat.now - rec["t"])),
                     C_TEXT))
    for s, rgb in rows:
        if y + 5 > limit:
            break
        blit_text(dst, y, 0, fit(s, w), rgb)
        y += 6

    if strip_h:
        _draw_world_strip(dst, w, h, limit, cat)


def _draw_alert_column(dst, w, h, cat, ev):
    """One earthquake, at the size the moment deserves."""
    blit_text(dst, 0, 0, fit("%s" % short_place(ev["place"]), w), C_ALERT)

    txt = "M%.1f" % ev["mag"]
    scale = 4
    while scale > 1 and text_width(txt, scale) > w:
        scale -= 1
    blit_text(dst, 6, 0, txt, C_ALERT, scale)
    y = 6 + 5 * scale + 2

    rows = [("%d KM %s OF HERE" % (round(ev["km"]), compass(ev["bearing"])),
             C_TEXT)]
    if ev.get("dep") is not None:
        rows.append(("DEPTH %.0f KM" % ev["dep"], C_DIM))
    rows.append(("%s AGO   %s" % (since(cat.now - ev["t"]),
                                  time.strftime("%H:%M", time.localtime(ev["t"]))),
                 C_TEXT))
    n = sum(1 for e in cat.local if e["t"] > ev["t"])
    if n:
        # The aftershock count, which is the second question everybody asks.
        rows.append(("%d SINCE" % n, C_WARN))
    for s, rgb in rows:
        if y + 5 > h:
            break
        blit_text(dst, y, 0, fit(s, w), rgb)
        y += 6


def _draw_world_strip(dst, w, h, top, cat):
    """The planet's week of M4.5+, as one tick per event.

    A world map at this size would be a smear -- 320 columns is not enough for
    an equirectangular Earth and ten rows is certainly not -- and a bare count
    says nothing about whether the week was an ordinary one. A time axis and a
    magnitude axis in five rows says both: the ticks bunch where a sequence
    happened and the tall ones are the ones that made the news. `top` is where
    the caller has already decided the strip begins; nothing here reaches up
    past it.
    """
    if not cat.world:
        return
    blit_text(dst, top, 0,
              fit("WORLD M4.5+ 7D  %d" % len(cat.world), w), C_LABEL)
    base = h - 1
    rows = base - (top + 6)
    if rows < 3:
        return
    top = top + 6
    dst[base, :w] = C_RULE
    t0 = cat.now - WEEK
    for t, m in cat.world:
        if t < t0:
            continue
        x = int((t - t0) / WEEK * (w - 1))
        # 4.5 to 7.5 across the strip's height; above that it pins to the top,
        # which has happened four times this century and would be obvious.
        k = min(1.0, max(0.0, (m - 4.5) / 3.0))
        n = max(1, int(round(k * rows)))
        col = dst[base - n:base, x]
        np.maximum(col, np.array(mag_colour(m), np.uint8), out=col)


# --------------------------------------------------------------------------
# The no-data card. Not a blank rectangle, and above all not a map with no
# earthquakes on it -- an empty map is this panel's *good* state and must never
# be what a missing file looks like.
# --------------------------------------------------------------------------

def draw_nodata(frame, lay, cat, cache_dir, lit=True):
    frame[:] = (6, 6, 8)
    edge = C_ALERT if lit else scale_colour(C_ALERT, 0.25)
    frame[0], frame[-1] = edge, edge
    frame[:, 0], frame[:, -1] = edge, edge

    title = "NO USGS DATA"
    scale = 3 if lay.w >= 200 else 2
    while scale > 1 and text_width(title, scale) > lay.w - 12:
        scale -= 1
    y = max(3, lay.h // 2 - 5 * scale)
    blit_text(frame, y, (lay.w - text_width(title, scale)) // 2, title,
              C_ALERT if lit else scale_colour(C_ALERT, 0.3), scale)
    y += 5 * scale + 4
    lines = [(cat.problem or "cache is empty").upper(),
             "RUN: PYTHON3 FTDATA.PY --LOOP 900",
             (cache_dir or ftdata.CACHE_DIR).upper()]
    for s in lines:
        s = fit(s, lay.w - 8)
        blit_text(frame, y, (lay.w - text_width(s)) // 2, s, C_LABEL)
        y += 7
        if y + 5 > lay.h - 2:
            break
    return frame


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h)
    bay, reg = lay.tiles()
    cache = args.cache_dir
    span = max(3600.0, float(args.span) * 3600.0)
    at = parse_when(args.at)

    sea_bay = unpack_sea(SEA_BAY, 155, 57, bay.w, bay.h)
    sea_reg = (unpack_sea(SEA_REGION, 57, 57, reg.w, reg.h)
               if reg is not None else None)

    def now_of():
        return time.time() if at is None else at

    static = np.zeros((h, w, 3), np.uint8)
    column = np.zeros((lay.body_h, lay.col_w, 3), np.uint8)
    frame = np.zeros((h, w, 3), np.uint8)
    # Two of each of the blinking pieces, baked. propagation.py's trick, and
    # for the same reason: an alert header re-laid-out every frame costs a
    # third of a millisecond here and thirty on the wall, which is most of the
    # frame budget spent redrawing the same eleven words.
    header = (np.zeros((lay.head_h + 1, w, 3), np.uint8),
              np.zeros((lay.head_h + 1, w, 3), np.uint8))
    nodata = (np.zeros((h, w, 3), np.uint8), np.zeros((h, w, 3), np.uint8))

    cell = {"cat": None, "loaded": -1e18, "alert": None, "pulse": None,
            "dist": None, "head_at": None, "nodata": False, "stamp": None}

    def bake_header(now):
        cell["head_at"] = int(now // 20)
        for lit, buf in zip((True, False), header):
            draw_header(buf, lay, cell["cat"], args, lit)

    def bake(now):
        """Everything that changes only when the cache does. Once per reload."""
        cat = Catalogue(cache, now)
        if args.alert_demo and cat.usable and cat.local:
            # Move the strongest event in the record to a few minutes ago and
            # let the ordinary alert path pick it up. Nothing else is faked,
            # so what appears on screen is the real layout with real numbers.
            hot = max(cat.local, key=lambda e: e["mag"])
            hot = dict(hot)
            hot["t"] = now - 480.0
            cat.local = [hot] + [e for e in cat.local if e is not hot]
            cat.local.sort(key=lambda e: e["t"], reverse=True)
        cell["cat"] = cat
        cell["loaded"] = now
        cell["stamp"] = record_stamp(cache)
        # Only a *missing* record gets the card. A stale-but-present one still
        # gets the maps: the geography did not expire, and drawing the coast
        # and the faults with no earthquakes on them and the word STALE in the
        # header is more honest than a card implying the fetcher is dead when
        # it is merely late -- and it keeps the card meaning exactly one thing.
        cell["nodata"] = cat.state == "absent"

        static[:] = C_BG
        bake_header(now)
        if cell["nodata"]:
            cell["alert"] = None
            cell["pulse"] = None
            draw_column(column, lay, cat, args)
            for lit, buf in zip((True, False), nodata):
                draw_nodata(buf, lay, cat, cache, lit)
            return

        draw_geography(static, bay, sea_bay, FAULTS, ())
        draw_scale_bar(static, bay, 50.0, "50KM")
        if reg is not None:
            draw_geography(static, reg, sea_reg, (), (100.0, 300.0))
            blit_text(static, lay.body_y + 1, reg.x + 1, "300KM", C_LABEL)

        draw_events(static, bay, cat.local, now, span)
        if reg is not None:
            draw_events(static, reg, cat.local, now, span)

        # The two hairlines between the three panes. Three abutting blocks read
        # as one picture without them, and a coastline that runs into a column
        # of type looks like a rendering bug.
        if lay.reg_x - 1 < w:
            static[lay.body_y:, lay.reg_x - 1] = C_RULE
        if lay.col_x - 1 < w:
            static[lay.body_y:, lay.col_x - 1] = C_RULE

        draw_column(column, lay, cat, args)

        # The pulse and the rings both need a place on a tile, and which tile
        # depends on where the earthquake was. Resolved once here rather than
        # per frame.
        alert = cat.alert()
        cell["alert"] = None
        cell["dist"] = None
        if alert is not None:
            tile = bay if bay.holds(alert["lat"], alert["lon"], -2) else reg
            if tile is not None and tile.holds(alert["lat"], alert["lon"], -1):
                r0, c0 = tile.project(alert["lat"], alert["lon"])
                # A distance field in kilometres over the tile, so a ring is a
                # ring on the ground rather than on the screen -- on the Bay
                # tile those are 2.4x different and the ellipse is the true one.
                yy = (np.arange(tile.h, dtype=f32) + 0.5 - r0) / tile.px_km_y
                xx = (np.arange(tile.w, dtype=f32) + 0.5 - c0) / tile.px_km_x
                cell["dist"] = (tile, np.hypot(yy[:, None], xx[None, :]))
                cell["alert"] = alert
                # A crosshair on the epicentre. The rings say "out from here"
                # but they are only ever a ring away from it; without a mark at
                # the centre the eye has to guess where "here" was, and on the
                # region tile at 11 km to the pixel it will guess wrong.
                sub = tile.region(static)
                r, c = int(round(r0)), int(round(c0))
                if 0 <= r < tile.h and 0 <= c < tile.w:
                    sub[r, max(0, c - 3):c + 4] = C_ALERT
                    sub[max(0, r - 3):r + 4, c] = C_ALERT
                    sub[r, c] = (255, 250, 245)

        rec = cat.recent()
        cell["pulse"] = None
        if rec is not None:
            tile = bay if bay.holds(rec["lat"], rec["lon"], -1) else reg
            if tile is not None and tile.holds(rec["lat"], rec["lon"], -1):
                r, c = tile.project(rec["lat"], rec["lon"])
                cell["pulse"] = (tile, int(round(r)), int(round(c)),
                                 mag_colour(rec["mag"]))

    pulse_hz = max(0.0, float(args.pulse_hz))
    ring_period = max(0.2, float(args.ring_period))

    def render(t, i=0):
        now = now_of() if at is None else at + t
        if args.reload and now - cell["loaded"] >= args.reload:
            # Stat before parse. Re-reading the record is 58 kB of JSON and
            # rebaking is three hundred events across two maps; on the wall
            # that is a couple of hundred milliseconds, and doing it on a file
            # that has not changed buys a visible hitch and nothing else. The
            # fetcher writes by rename, so a changed mtime is a changed record.
            stamp = record_stamp(cache)
            if stamp == cell["stamp"]:
                cell["loaded"] = now
            else:
                bake(now)

        lit = True if pulse_hz <= 0 else ((t * pulse_hz) % 1.0) < 0.55
        if cell["nodata"]:
            np.copyto(frame, nodata[0 if lit else 1])
            return frame

        # The header is re-laid-out three times a minute, not twenty times a
        # second: formatting it costs a third of a millisecond and the only
        # thing on it that moves faster than a minute is the blink, which is a
        # choice between two baked buffers and costs nothing.
        if int(now // 20) != cell["head_at"]:
            bake_header(now)

        np.copyto(frame, static)
        frame[:lay.head_h + 1] = header[0 if lit else 1]
        frame[lay.body_y:, lay.col_x:lay.col_x + lay.col_w] = column

        if cell["alert"] is not None and cell["dist"] is not None:
            tile, dist = cell["dist"]
            sub = tile.region(frame)
            phase = (t / ring_period) % 1.0
            for k, gain in ((phase, 1.0), ((phase + 0.5) % 1.0, 0.45)):
                # Rings travel out at a constant speed and fade as they go,
                # which is what a wave front does and what makes two of them
                # read as a sequence rather than as a target.
                r_km = k * 300.0
                m = np.abs(dist - r_km) < 3.0
                if not m.any():
                    continue
                col = scale_colour(C_ALERT, gain * (1.0 - k) ** 0.8)
                sub[m] = np.maximum(sub[m], np.array(col, np.uint8))

        if cell["pulse"] is not None and pulse_hz > 0:
            tile, r, c, rgb = cell["pulse"]
            k = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t * pulse_hz * 2 * math.pi))
            sub = tile.region(frame)
            sub[r, c] = scale_colour(rgb, k)
            # A one-pixel halo, so a pulse on a single dark dot is visible from
            # the far side of the room rather than only from in front of it.
            halo = scale_colour(rgb, k * 0.3)
            if r > 0:
                np.maximum(sub[r - 1, c], np.array(halo, np.uint8),
                           out=sub[r - 1, c])
            if r + 1 < tile.h:
                np.maximum(sub[r + 1, c], np.array(halo, np.uint8),
                           out=sub[r + 1, c])

        # The heartbeat. An instrument is allowed to be still; what it is not
        # allowed to be is *accidentally* still, and on a quiet week every
        # frame of this panel is otherwise identical.
        frame[0, -1] = C_SITE if lit else C_RULE
        return frame

    bake(now_of())
    render.state = cell               # the tests reach in here; nothing else
    render.layout = lay
    render.tiles = (bay, reg)
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "a week of earthquakes around San Francisco, from USGS",
                  fps=20)


if __name__ == "__main__":
    main()
