#!/usr/bin/env python3
"""A NOC weathermap for the San Francisco Metropolitan Internet Exchange.

The oldest picture in network operations: the fibre drawn where it actually
runs, each span coloured by how much traffic is on it, and the traffic moving
along it so the map is alive rather than a diagram. This one is about a
specific exchange in a specific bay -- five metros from San Francisco down to
San Jose, the five inter-metro trunks between them, and the number the whole
exchange is judged by, which is how many bits crossed it today and when the
peak was.

**The map is turned forty-five degrees, and that is the whole layout.** SFMIX's
footprint is a corridor: San Francisco and Oakland at the top, then Fremont,
Santa Clara and San Jose strung down the south bay. North up, that cloud is
56 x 64 km -- very nearly square, and a square drawn on a 5:1 letterbox wastes
three quarters of the wall. Turned 45 degrees it is 82 x 23 km, an aspect of
3.62 against the map pane's 3.6, and it fits at **true scale in both axes with
nothing stretched**. So the panel is a real map, isometric, just not north up;
the arrow bottom-right says which way north is and the bar bottom-left says how
far ten kilometres is. The alternative was the schematic subway diagram the
portal itself draws at low zoom, which is more legible in the abstract and
throws away the one thing this audience already knows by heart -- the shape of
their own bay. The coastline is the label that needs no text.

**Two strands per trunk, because in and out are different numbers.** A
weathermap has split every link into two half-arrows since MRTG, and the reason
is that a link is not one quantity: San Jose to Fremont was carrying 118 Gb/s
one way and 67 the other while this was being written, 19.6% and 11.2% of the
same 600 Gb/s of fibre. So each trunk is drawn as two parallel one-pixel tracks
either side of its route, each coloured by *its own* direction's load, with
light running along it in that direction at a speed proportional to it. The
counter-flow is visible from across the room, and the busier half is the
brighter one.

**The colour ramp is the portal's own, compressed four-fold, and the legend
says so.** SFMIX's own map colours 0-80% blue-green-yellow-orange-red, which
is the right scale for a map you lean into and can dismiss a link that is
about to melt. An exchange deliberately overbuilds its backbone, so on that
scale every trunk here is blue, all day, forever -- a dead panel that is also
uninformative. This one keeps the five hues and runs them **0 to 30 per cent**,
which is where the traffic actually lives: the quiet trunk is blue, the busy
one is orange, and 30% or more is red. The ramp is drawn bottom right with its
numbers on it, because a colour scale without its numbers is decoration. The
compression is honest in the direction that matters -- nothing here can look
calmer than it is.

**The planned link is drawn as planned.** San Francisco to Oakland exists in
the exchange's structure and carries nothing, because it is not lit yet. It is
dashed, in the portal's own slate blue, outside the utilisation ramp entirely,
and no light runs along it. Colouring an unlit fibre "0%, healthy blue" would
be the easiest lie on this panel.

**The right third is the aggregate.** Total traffic exchanged right now in one
number big enough to read from the far bench, today's 24-hour curve underneath
it, and the peak marked with the time it happened. Ingress and egress across
the whole exchange agree to two parts in ten thousand -- which is what an
exchange *is* -- so it is one curve and the word is "exchanged" rather than a
side picked arbitrarily.

**Nothing here touches the network.** `build()` calls `ftdata.load()` and reads
one JSON file. The fetcher is a separate process on a timer:

    $ python3 ftdata.py --once --only sfmix-ix

The record carries the portal's `generation` string, and the fetcher refuses to
build one where the geometry and the traffic disagree about it -- the cable ids
are rebuilt every time the portal re-runs its NetBox build, and traffic joined
onto the wrong generation would quietly colour trunks with other trunks'
numbers. Past its half-hour TTL the panel still draws, with the age and STALE
on it, because the routes and the day's curve are still true and only "now" has
gone soft. With no record at all it draws a no-data card.

**The coastline is `sfmix-map.npz`**, a 768x768 bit-packed land/sea mask over
lon -122.80..-121.60, lat 37.05..38.00, rasterised from the exchange's own
committed, public, coarse basemap water rings
(`portal/mapbuild/data/basemap-water.json`, OSM-derived) by an even-odd
scanline fill. 4.5 KB. It is deliberately not `adsb-coast.npz`, which stops at
37.4 N and therefore has no south bay -- which is most of this map.

**Frame budget.** Everything is baked in `build()`: the sea, the shoreline, the
trunk strands, the nodes and their captions, the header, the chart and the
legend all go into one uint8 frame. `render()` does a full-frame copy, six
arithmetic passes over a flat array of the 1170 pixels that carry flowing
light, one fancy-indexed write of those pixels, and a one-pixel dot on the
chart. That is ten numpy calls a frame, all of them into preallocated buffers,
and the call count is the budget on the wall rather than the pixel count --
numpy costs tens of microseconds a call there whatever the array size. Nothing
in the frame loop formats a string, allocates, or depends on how many comets
happen to be lit. Measured over 1200 frames on the development machine:
**mean 0.027 ms, p50 0.026, p95 0.029, p99 0.039**, worst frame 0.057 ms.
`build()` is 4.0-4.6 ms here, once, on the scheduler's worker thread; most of
that is resampling the coastline raster into the pane.

Run:  python3 ftdata.py --once --only sfmix-ix
      python3 sfmix.py --host 127.0.0.1
      FT_DATA_CACHE=/tmp/empty python3 sfmix.py     # the no-data card
      python3 scripts/test-sfmix.py
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

PRODUCT = "sfmix-ix"

COAST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "sfmix-map.npz")


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, tide and propagation draw
# with. Anything from a real typeface is mush at five pixels. One glyph is
# added, because a utilisation legend without a per-cent sign is a row of
# unlabelled numbers.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT["%"] = "51245"

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


def blit_mask(dst, y, x, m, rgb):
    """Stamp a boolean mask at (y, x), clipped to `dst`."""
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    dst[y0:y1, x0:x1][m[y0 - y:y1 - y, x0 - x:x1 - x]] = rgb
    return gw


def blit_text(dst, y, x, s, rgb, scale=1):
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn."""
    return blit_mask(dst, y, x, text_mask(s, scale), rgb)


def blit_label(dst, y, x, s, rgb, halo=(4, 8, 12), scale=1):
    """A string with a one-pixel dark outline around every stroke.

    The map captions sit on top of the trunks -- there is nowhere else on a
    64 row map to put them -- and five-pixel type in white over a yellow cable
    is unreadable in a way that a screenshot at 3x hides completely. The halo
    is what buys them a background without spending a solid box of it.
    """
    m = text_mask(s, scale)
    grown = np.zeros((m.shape[0] + 2, m.shape[1] + 2), bool)
    for dy in range(3):
        for dx in range(3):
            grown[dy:dy + m.shape[0], dx:dx + m.shape[1]] |= m
    blit_mask(dst, y - 1, x - 1, grown, halo)
    return blit_mask(dst, y, x, m, rgb)


# --------------------------------------------------------------------------
# Colour.
#
# The five utilisation stops are the exact hex the exchange's own map uses --
# Grafana's blue/green/yellow/orange/red -- so somebody who knows the portal
# reads this panel without being taught it. What differs is the axis they are
# spread over; see UTIL_FULL.
# --------------------------------------------------------------------------

RAMP = [(0.00, (87, 148, 242)),         # #5794F2 blue
        (0.25, (115, 191, 105)),        # #73BF69 green
        (0.50, (250, 222, 42)),         # #FADE2A yellow
        (0.75, (255, 152, 48)),         # #FF9830 orange
        (1.00, (242, 73, 92))]          # #F2495C red

# Where the top of the ramp is, in per cent of a trunk's capacity. See the
# docstring: the portal's 80 leaves this panel one flat colour. Fixed, not
# derived from the day's maximum -- a traffic light whose boundaries move with
# the traffic is not a traffic light.
UTIL_FULL = 30.0

C_WATER = (7, 15, 28)
C_SHORE = (20, 36, 56)
C_LAND = (0, 0, 0)
C_TEXT = (198, 210, 222)
C_DIM = (84, 96, 110)
C_FAINT = (44, 52, 64)
C_SEP = (16, 22, 30)
C_NODE = (236, 243, 250)
C_NODE_RING = (4, 8, 12)                # punched out of whatever is under it
C_PLANNED = (125, 156, 192)             # #7d9cc0, outside the ramp on purpose
C_NOTRAFFIC = (110, 116, 124)           # up, but nothing reported for it
C_WARN = (255, 96, 72)
C_CURVE = (120, 214, 226)
C_CURVE_FILL = (13, 44, 55)
C_AXIS = (96, 134, 150)                 # captions that sit on the chart's fill
C_PEAK = (255, 206, 92)
C_NOW = (255, 246, 214)

# The comet that runs along a strand is that strand's own colour lifted towards
# white, not a fixed bright colour: a pale dot looks identical on the yellow
# and orange ends of the ramp and would erase the very thing the colour says.
FLOW_LIFT = 0.72

# Comet spacing and tail length along a strand, in pixels. Fourteen apart on a
# 190 px trunk is about thirteen of them in flight, which reads as a stream;
# closer together and it reads as a dashed line that happens to move.
FLOW_PERIOD = 14.0
FLOW_TAIL = 5.0

# Pixels a second at zero and at full-scale utilisation. The floor is not zero
# because a trunk carrying a real but small load is not stopped, and a stopped
# strand next to a moving one reads as broken rather than as quiet.
FLOW_MIN_V = 3.0
FLOW_MAX_V = 34.0


def ramp_colour(x):
    """Interpolate RAMP at 0..1. Returns a (3,) float array."""
    x = min(1.0, max(0.0, float(x)))
    for i in range(1, len(RAMP)):
        if x <= RAMP[i][0]:
            p0, c0 = RAMP[i - 1]
            p1, c1 = RAMP[i]
            f = (x - p0) / (p1 - p0)
            return np.array(c0, f32) * (1 - f) + np.array(c1, f32) * f
    return np.array(RAMP[-1][1], f32)


# --------------------------------------------------------------------------
# Clock. Same shape as caiso.py's and tide.py's: everything asks for `now`
# rather than reading the system clock, which is what makes --at possible.
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
    base = time.time() if at is None else float(at)
    start = time.monotonic()
    if at is None and rate == 1.0:
        return time.time
    return lambda: base + (time.monotonic() - start) * rate


def hhmm(epoch, ampm=True):
    """A compact local-time label: '857P' or '20:57'."""
    lt = time.localtime(epoch)
    if not ampm:
        return time.strftime("%H:%M", lt)
    h = lt.tm_hour % 12 or 12
    return "%d:%02d%s" % (h, lt.tm_min, "A" if lt.tm_hour < 12 else "P")


def bits(mbps, places=0):
    """Megabits per second as a short human figure: '302G', '54.4G', '870M'."""
    if mbps is None:
        return "--"
    v = float(mbps)
    if v >= 1e6:
        # One more decimal in terabits than anywhere else. A whole-number
        # terabit figure moves in 1000 Gb/s steps, which on an exchange that
        # crossed its first terabit recently would be a number that never
        # changed all day.
        return "%.*fT" % (places + 1, v / 1e6)
    if v >= 1000:
        return "%.*fG" % (places, v / 1000.0)
    return "%.*fM" % (places, v)


# --------------------------------------------------------------------------
# Reading what ftdata left behind. `load()` never raises, so everything still
# wrong at this point is wrong about content and is caught here.
# --------------------------------------------------------------------------

def read_ix(cache_dir):
    """(record, age, problem). `record` is None if there is nothing drawable."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached SFMIX record"
    payload, age = got
    try:
        metros = payload["metros"]
        trunks = payload["trunks"]
        total = payload["total"]
        if not metros or not trunks:
            raise ValueError("empty")
        for t in trunks:
            t["path"] = [(float(p[0]), float(p[1])) for p in t["path"]]
            if len(t["path"]) < 2:
                raise ValueError("trunk with no route")
        curve = [None if v is None else float(v) for v in total["mbps"]]
        stamps = [float(x) for x in total["t"]]
        if len(curve) != len(stamps) or len(curve) < 2:
            raise ValueError("total curve does not line up")
    except Exception:                                            # noqa: BLE001
        return None, age, "SFMIX record is malformed"
    return {"generation": payload.get("generation"),
            "metros": metros, "trunks": trunks, "total": total,
            "curve": curve, "stamps": stamps,
            "backbone_links": int(payload.get("backbone_links") or 0),
            "sites": int(payload.get("sites") or 0),
            "age": age}, age, None


def strand_util(trunk, forward):
    """Per-direction utilisation, 0..1 of capacity. None if it cannot be known.

    `forward` is a_metro -> z_metro, which is the direction the record's `out`
    figure is in and the direction the trunk's route is stored in.
    """
    cap = float(trunk.get("cap_mbps") or 0.0)
    if cap <= 0 or not trunk.get("reporting"):
        return None
    v = trunk.get("out_mbps") if forward else trunk.get("in_mbps")
    if v is None:
        return None
    return max(0.0, float(v)) / cap


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--util-full", type=float, default=UTIL_FULL,
                    help="per cent utilisation at the top of the colour ramp")
    ap.add_argument("--flow", type=float, default=1.0,
                    help="speed of the light running along the trunks "
                         "(0 = still)")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second")
    ap.add_argument("--reload", type=float, default=300.0,
                    help="seconds between re-reads of the cache (0 = never)")


# --------------------------------------------------------------------------
# Layout. The map takes what it can use and the aggregate takes the rest; on a
# panel too narrow for both, the map wins, because a number with no map is a
# number and this is a weathermap.
# --------------------------------------------------------------------------

class Layout(object):
    PANE = 117

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.pane_w = self.PANE if (w >= 300 and h >= 56) else 0
        self.pane_x = w - self.pane_w
        self.map_w = self.pane_x - (3 if self.pane_w else 0)
        self.map_h = h
        # Inside the pane, top to bottom. Everything below whatever does not
        # fit is dropped rather than squeezed; five-pixel type does not scale.
        # Sixty-four rows is exactly enough for all five things and no more,
        # which is why the chart carries its own axis caption inside itself
        # rather than under it -- an axis label row cost the legend its own.
        self.chart_y, self.chart_h = 23, 23
        self.legend_y = 54


# --------------------------------------------------------------------------
# Projection. A rotated equirectangular, isometric in both axes -- see the
# docstring on why 45 degrees.
# --------------------------------------------------------------------------

ROTATE_DEG = 45.0


class Projection(object):
    def __init__(self, lonlat, box_w, box_h, pad=3, rotate=ROTATE_DEG):
        pts = np.asarray(lonlat, np.float64)
        self.lon0 = float(pts[:, 0].mean())
        self.lat0 = float(pts[:, 1].mean())
        self.kx = 111.32 * math.cos(math.radians(self.lat0))
        self.ky = 110.57
        a = math.radians(rotate)
        self.ca, self.sa = math.cos(a), math.sin(a)
        km = self._km(pts)
        lo, hi = km.min(axis=0), km.max(axis=0)
        span = np.maximum(hi - lo, 1e-6)
        # One scale for both axes. Anisotropic scaling would fill the box
        # exactly and would silently be a lie about distance; the slack goes
        # into margin instead.
        self.scale = float(min((box_w - 2 * pad) / span[0],
                               (box_h - 2 * pad) / span[1]))
        mid = (lo + hi) / 2.0
        self.ox = box_w / 2.0 - mid[0] * self.scale
        self.oy = box_h / 2.0 + mid[1] * self.scale

    def _km(self, pts):
        x = (pts[:, 0] - self.lon0) * self.kx
        y = (pts[:, 1] - self.lat0) * self.ky
        return np.stack([x * self.ca - y * self.sa,
                         x * self.sa + y * self.ca], axis=1)

    def __call__(self, lonlat):
        """[(lon, lat), ...] -> (N, 2) float pixel (x, y)."""
        km = self._km(np.asarray(lonlat, np.float64))
        return np.stack([self.ox + km[:, 0] * self.scale,
                         self.oy - km[:, 1] * self.scale], axis=1)

    def inverse(self, px, py):
        """Pixel arrays back to (lon, lat) arrays. Used to sample the coast."""
        xr = (px - self.ox) / self.scale
        yr = -(py - self.oy) / self.scale
        x = xr * self.ca + yr * self.sa
        y = -xr * self.sa + yr * self.ca
        return self.lon0 + x / self.kx, self.lat0 + y / self.ky

    def north(self):
        """Unit pixel vector pointing north. The arrow's whole job."""
        dx, dy = -self.sa, -self.ca
        return dx, dy


# --------------------------------------------------------------------------
# The coastline.
# --------------------------------------------------------------------------

def load_sea():
    """The baked land/sea mask and its bounding box, or None if unreadable.

    Missing is survivable and is not an error worth a card: a weathermap with
    no coastline behind it is still a weathermap. Failing to draw the traffic
    is the only thing here that means anything.
    """
    try:
        d = np.load(COAST)
        shape = tuple(int(v) for v in d["shape"])
        sea = np.unpackbits(d["sea"])[:shape[0] * shape[1]].reshape(shape)
        return sea.astype(bool), tuple(float(v) for v in d["bbox"])
    except Exception:                                            # noqa: BLE001
        return None


def sea_mask(proj, w, h, sub=2):
    """The baked mask resampled into a (h, w) grid under `proj`.

    Area-averaged over sub x sub samples a pixel rather than point-sampled: the
    panel is three times coarser than the raster, and nearest-neighbour at that
    ratio makes the shoreline change shape with the panel size, which is how
    a bay ends up with a different number of inlets at 320 wide than at 256.
    """
    got = load_sea()
    if got is None:
        return None
    sea, (lon0, lat0, lon1, lat1) = got
    rows, cols = sea.shape
    yy, xx = np.meshgrid((np.arange(h * sub) + 0.5) / sub,
                         (np.arange(w * sub) + 0.5) / sub, indexing="ij")
    lon, lat = proj.inverse(xx, yy)
    c = ((lon - lon0) / (lon1 - lon0) * cols).astype(int).clip(0, cols - 1)
    r = ((lat1 - lat) / (lat1 - lat0) * rows).astype(int).clip(0, rows - 1)
    fine = sea[r, c].astype(f32)
    return fine.reshape(h, sub, w, sub).mean((1, 3)) >= 0.5


# --------------------------------------------------------------------------
# Baking the map.
# --------------------------------------------------------------------------

def densify(px, step=0.5):
    """Resample a polyline of pixel coordinates to a fixed step along it.

    Walking the arclength rather than drawing segment by segment is what makes
    the flow phase mean the same thing everywhere on a trunk: the comets have
    to travel at a constant speed along a route whose stored vertices are
    hundreds of metres apart in places and metres apart in others.
    """
    d = np.diff(px, axis=0)
    seg = np.hypot(d[:, 0], d[:, 1])
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(s[-1])
    if total <= 0:
        return px[:1], np.zeros(1, f32)
    n = max(2, int(total / step) + 1)
    ss = np.arange(n, dtype=np.float64) * (total / (n - 1))
    return (np.stack([np.interp(ss, s, px[:, 0]),
                      np.interp(ss, s, px[:, 1])], axis=1),
            ss.astype(f32))


def strand_pixels(px, s, offset, w, h):
    """One offset track along a densified route, as unique pixels in order.

    The offset is perpendicular, one pixel either side, which is what puts the
    two directions of a trunk on their own tracks. The unique-in-order step
    matters twice over: it is what stops a slow-moving corner writing the same
    pixel eight times a frame, and it is what keeps the arclength array a
    strictly increasing phase for the comets.
    """
    d = np.gradient(px, axis=0)
    n = np.hypot(d[:, 0], d[:, 1])
    n[n == 0] = 1.0
    nx, ny = -d[:, 1] / n, d[:, 0] / n
    xs = np.round(px[:, 0] + nx * offset).astype(int)
    ys = np.round(px[:, 1] + ny * offset).astype(int)
    ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
    xs, ys, ss = xs[ok], ys[ok], s[ok]
    if not len(xs):
        return np.zeros(0, int), np.zeros(0, int), np.zeros(0, f32)
    flat = ys * w + xs
    keep = np.concatenate([[True], flat[1:] != flat[:-1]])
    return xs[keep], ys[keep], ss[keep]


def draw_node(dst, x, y, colour=C_NODE, ring=C_NODE_RING):
    """A metro station: a lit 2x2 core inside a dark ring, the portal's roundel.

    A single lit pixel disappears under a trunk crossing it and a 3x3 blob eats
    ten kilometres of map. Two by two with a ring around it is the smallest
    thing that still reads as a deliberate mark rather than as a stray pixel.
    """
    h, w = dst.shape[:2]
    y0, x0 = max(0, y - 2), max(0, x - 2)
    y1, x1 = min(h, y + 2), min(w, x + 2)
    if y1 <= y0 or x1 <= x0:
        return
    dst[y0:y1, x0:x1] = ring
    dst[max(0, y - 1):min(h, y + 1), max(0, x - 1):min(w, x + 1)] = colour


def draw_north(dst, x, y, proj):
    """A 9 px arrow pointing north, with an N at its head.

    Turning a map is only defensible if the panel says it has been turned.
    """
    dx, dy = proj.north()
    for k in range(9):
        px, py = int(round(x + dx * k)), int(round(y + dy * k))
        if 0 <= py < dst.shape[0] and 0 <= px < dst.shape[1]:
            dst[py, px] = C_DIM
    hx, hy = x + dx * 9, y + dy * 9
    for sgn in (-1, 1):
        # Two short barbs, each 55 degrees back off the shaft.
        a = math.atan2(dy, dx) + sgn * 2.4
        for k in range(1, 4):
            px = int(round(hx + math.cos(a) * k))
            py = int(round(hy + math.sin(a) * k))
            if 0 <= py < dst.shape[0] and 0 <= px < dst.shape[1]:
                dst[py, px] = C_DIM
    blit_text(dst, int(round(hy)) - 7, int(round(hx)) - 1, "N", C_TEXT)


def draw_scalebar(dst, x, y, proj, km=10):
    """A bar as long as `km` on this map, labelled. The map's only units."""
    n = int(round(km * proj.scale))
    if n < 6 or x + n >= dst.shape[1]:
        return
    dst[y, x:x + n] = C_DIM
    dst[y - 2:y + 1, x] = C_DIM
    dst[y - 2:y + 1, x + n - 1] = C_DIM
    blit_text(dst, y - 8, x, "%dKM" % km, C_DIM)


def label_side(reg, x, y, tw, w, h, taken):
    """Where a node's caption goes: the emptiest of right, left, above, below.

    Two rules, and the second one is the one that matters. A caption may not
    overlap another caption -- five three-letter codes on an eighty by twenty
    kilometre map will collide, and two overlapping words read as neither. And
    among the places left, it goes where the fewest pixels are already lit,
    which is what keeps San Jose's caption off the orange trunk that terminates
    on it. Scoring against what is already drawn rather than preferring a fixed
    side is the difference between a caption you can read and one you can only
    read in a screenshot at three times size.

    Deterministic: it scores four fixed candidates once and does not iterate,
    so a given metro's caption is in the same place every time.
    """
    best = None
    for rank, (ly, lx) in enumerate(((y - 2, x + 4), (y - 2, x - 4 - tw),
                                     (y - 9, x - tw // 2),
                                     (y + 4, x - tw // 2))):
        if lx < 1 or lx + tw > w - 1 or ly < 1 or ly + 5 > h - 1:
            continue
        box = (lx - 1, ly - 1, lx + tw + 1, ly + 6)
        if any(not (box[2] <= o[0] or o[2] <= box[0]
                    or box[3] <= o[1] or o[3] <= box[1]) for o in taken):
            continue
        # Threshold, not "any lit pixel": the water is 28 and the shoreline 56,
        # and a caption is perfectly legible over either. What it must not sit
        # on is a trunk or a node, and everything drawn in this map that is not
        # background clears 70 in at least one channel.
        busy = int((reg[box[1]:box[3], box[0]:box[2]].max(axis=2) > 70).sum())
        if best is None or busy < best[0]:
            best = (busy, rank, ly, lx, box)
    if best is None:
        return None, None
    taken.append(best[4])
    return best[2], best[3]


# --------------------------------------------------------------------------
# The right-hand pane: the aggregate.
# --------------------------------------------------------------------------

def draw_curve(dst, lay, rec, x0, y0, cw, ch, h24):
    """Today's exchange total, with the peak marked and now at the right edge.

    Filled rather than a bare line: a one-pixel trace 22 rows tall over a black
    pane is a scribble at a walking glance, and the filled area gives the eye a
    silhouette. The vertical axis starts at zero on purpose -- a traffic curve
    zoomed onto its own top few per cent is the classic way to make a flat day
    look like an event.
    """
    curve = rec["curve"]
    v = np.array([np.nan if c is None else c for c in curve], f32)
    if not np.isfinite(v).any():
        return
    top = float(np.nanmax(v))
    peak_mbps = rec["total"].get("peak_mbps")
    if peak_mbps:
        top = max(top, float(peak_mbps))
    # Twelve per cent of headroom above the day's own maximum, so the peak rule
    # lands a couple of rows inside the chart instead of exactly on its top
    # edge, where it is indistinguishable from a border.
    top = max(top, 1.0) * 1.12

    xs = np.linspace(0, len(v) - 1, cw)
    ys = np.interp(xs, np.arange(len(v)), np.nan_to_num(v, nan=0.0))
    rows = np.clip(np.round(y0 + ch - 1 - ys / top * (ch - 1)),
                   y0, y0 + ch - 1).astype(int)
    grid = np.arange(y0, y0 + ch)[:, None]
    fill = grid > rows[None, :]
    reg = dst[y0:y0 + ch, x0:x0 + cw]
    reg[fill] = C_CURVE_FILL
    reg[rows - y0, np.arange(cw)] = C_CURVE

    # The peak, as a dotted rule at its own height with the time on it. The
    # peak is the number an exchange is judged by; a curve without it marked
    # makes the viewer estimate it off a 22 pixel axis.
    if peak_mbps:
        pr = int(round(y0 + ch - 1 - float(peak_mbps) / top * (ch - 1)))
        if y0 <= pr < y0 + ch:
            dst[pr, x0:x0 + cw:3] = C_PEAK

    # The right-hand edge is now. Marked, because a chart that runs to the
    # edge of its box has not said where the present is -- and the time axis
    # is captioned along the bottom of the chart rather than under it, because
    # sixty-four rows had exactly one row spare and the legend needed it.
    dst[y0:y0 + ch, x0 + cw - 1] = C_FAINT
    blit_text(dst, y0 + ch - 6, x0 + 1, "-24H", C_AXIS)
    blit_text(dst, y0 + ch - 6, x0 + cw - text_width("NOW") - 1, "NOW", C_AXIS)


def draw_legend(dst, lay, x0, y0, util_full):
    """The utilisation ramp with its numbers. The ramp is the point of the map.

    Drawn as the ramp itself rather than as five chips: the trunks are coloured
    by interpolation, so a five-chip key would not answer "what is this green".
    """
    bw = min(lay.pane_w - 20, 84)
    bar = np.stack([ramp_colour(i / float(bw - 1)) for i in range(bw)])
    dst[y0:y0 + 4, x0:x0 + bw] = bar.astype(np.uint8)[None, :, :]
    blit_text(dst, y0 - 6, x0, "LINK LOAD", C_DIM)
    blit_text(dst, y0 + 5, x0, "0", C_DIM)
    mid = "%d" % int(round(util_full / 2.0))
    blit_text(dst, y0 + 5, x0 + bw // 2 - text_width(mid) // 2, mid, C_DIM)
    top = "%d%%" % int(round(util_full))
    blit_text(dst, y0 + 5, x0 + bw - text_width(top), top, C_DIM)
    return bw


def draw_nodata(dst, lay, lines):
    """The honest panel. No map, no trunks, no implied traffic."""
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
    util_full = max(0.5, float(args.util_full))

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)
    flat = frame.reshape(-1, 3)

    cell = {"rec": None, "problem": None, "loaded": -1e18, "stale": False,
            "n_flow": 0, "now_dot": None}

    # The flow arrays. Allocated once at their largest and used as prefixes, so
    # a cache reload never allocates in the frame loop's way.
    cap = 8192
    fl_idx = np.zeros(cap, np.int64)
    fl_phase = np.zeros(cap, f32)
    fl_speed = np.zeros(cap, f32)
    fl_base = np.zeros((cap, 3), f32)
    fl_delta = np.zeros((cap, 3), f32)
    work = np.zeros(cap, f32)
    buf = np.zeros((cap, 3), f32)

    def bake_map(rec):
        """The map pane: sea, shoreline, trunks, nodes, captions, furniture."""
        mw, mh = lay.map_w, lay.map_h
        if mw < 24 or mh < 16:
            return 0
        pts = [(m["lon"], m["lat"]) for m in rec["metros"].values()]
        for t in rec["trunks"]:
            pts.extend(t["path"])
        proj = Projection(pts, mw, mh)
        cell["proj"] = proj

        sea = sea_mask(proj, mw, mh)
        reg = static[:mh, :mw]
        if sea is not None:
            reg[sea] = C_WATER
            reg[~sea] = C_LAND
            # A one-pixel shoreline. Without it the water is a flat navy field
            # whose edge is a two-level step nobody sees from six feet away;
            # with it the bay has an outline and reads as a map.
            edge = np.zeros_like(sea)
            edge[:, :-1] |= sea[:, :-1] != sea[:, 1:]
            edge[:-1] |= sea[:-1] != sea[1:]
            reg[edge] = C_SHORE

        # Trunks, quietest first. Both strands of a trunk are laid down before
        # the next one starts, so where two routes share a corridor -- and they
        # do, because the peninsula is one corridor and three trunks are in it
        # -- the later one wins the shared pixels whole rather than dithering
        # with the earlier one. Sorting by load means the winner is the busiest
        # trunk, which is the one a weathermap exists to show. The loser is not
        # lost: it reappears the moment the routes diverge.
        n = 0
        order = sorted(rec["trunks"],
                       key=lambda tk: float(tk.get("util_pct") or 0.0))
        for t in order:
            px = proj(t["path"])
            dense, s = densify(px)
            planned = str(t.get("status", "up")).lower() != "up"
            for forward, offset in ((True, 1.0), (False, -1.0)):
                xs, ys, ss = strand_pixels(dense, s, offset, mw, mh)
                if not len(xs):
                    continue
                u = strand_util(t, forward)
                if planned:
                    # Dashed, and outside the ramp. It is not carrying zero
                    # because it is idle; it is not carrying anything because
                    # it is not in service yet.
                    xs, ys, ss = xs[::2], ys[::2], ss[::2]
                    col = np.array(C_PLANNED, f32)
                elif u is None:
                    col = np.array(C_NOTRAFFIC, f32)
                else:
                    col = ramp_colour(u / (util_full / 100.0))
                static[ys, xs] = col.astype(np.uint8)
                # No load, no light. A direction measured at zero is still
                # drawn -- zero is a fact and it gets the bottom of the ramp --
                # but running comets along it would say bits are moving when
                # the measurement says they are not.
                if planned or not u or args.flow <= 0:
                    continue
                # One comet stream per strand. Everything about it is baked
                # except the phase, which is the only thing render() computes.
                k = len(xs)
                if n + k > cap:
                    break
                sl = slice(n, n + k)
                fl_idx[sl] = ys.astype(np.int64) * w + xs
                # render() lights a pixel where (s + speed*t) mod PERIOD is
                # near zero, and `s` runs from the a end towards the z end. So
                # the lit position along the strand is s = -speed*t, and the
                # sign that sends a comet a->z is a *negative* speed. Only the
                # speed flips between the two strands: negating the phase as
                # well -- which looks like the symmetric thing to do, and was
                # written that way first -- is a no-op on direction and sends
                # both tracks of every trunk the same way.
                fl_phase[sl] = ss
                fl_speed[sl] = (FLOW_MIN_V + (FLOW_MAX_V - FLOW_MIN_V)
                                * min(1.0, u / (util_full / 100.0))) \
                    * (-1.0 if forward else 1.0) * float(args.flow)
                fl_base[sl] = col
                fl_delta[sl] = (np.array((255.0, 255.0, 255.0), f32) - col) \
                    * FLOW_LIFT
                n += k

        # Nodes and captions last, over the trunks: a station buried under a
        # cable is the one thing on this map that must never happen.
        taken = []
        placed = []
        for name, m in rec["metros"].items():
            q = proj([(m["lon"], m["lat"])])[0]
            x, y = int(round(q[0])), int(round(q[1]))
            if not (0 <= x < mw and 0 <= y < mh):
                continue
            placed.append((name, m, x, y))
        for _name, m, x, y in placed:
            draw_node(static, x, y)
        for _name, m, x, y in placed:
            code = str(m.get("code") or "?")
            ly, lx = label_side(reg, x, y, text_width(code), mw, mh, taken)
            if ly is not None:
                blit_label(static, ly, lx, code, C_TEXT)

        # Bottom left, over the Pacific, which is the one part of this map that
        # is guaranteed to have nothing on it.
        if mh >= 40 and mw >= 60:
            draw_scalebar(static, 3, mh - 3, proj)
            draw_north(static, mw - 10, mh - 2, proj)
        return n

    def bake_pane(rec, age, stale):
        """The aggregate: the number, today's curve, the peak, the legend."""
        if not lay.pane_w:
            # No room for the aggregate. The age still has to be on the panel
            # somewhere -- a weathermap that has quietly stopped updating looks
            # exactly like one that has not -- so it goes in the map's top
            # right corner, which is open water on every size this fits on.
            right = ftdata.describe_age(age)
            if stale:
                right = "STALE " + right
            if lay.w > text_width(right) + 4 and lay.h >= 16:
                blit_label(static, 1, lay.w - text_width(right) - 1, right,
                           C_WARN if stale else C_DIM)
            return
        x0 = lay.pane_x
        static[:, lay.pane_x - 2] = C_SEP
        total = rec["total"]

        blit_text(static, 0, x0, "SFMIX TOTAL", C_DIM)
        right = ftdata.describe_age(age)
        if stale:
            right = "STALE " + right
        blit_text(static, 0, x0 + lay.pane_w - text_width(right) - 1, right,
                  C_WARN if stale else C_DIM)

        # The headline. Two sizes on one baseline: the figure at 2x because it
        # is what the panel is for, the unit at 1x because nobody needs to read
        # "BIT/S" from the door.
        big = bits(total.get("now_mbps"))
        bw = blit_text(static, 6, x0, big, C_NOW, 2)
        blit_text(static, 11, x0 + bw + 3, "BIT/S", C_DIM)

        peak = total.get("peak_mbps")
        if peak:
            at = hhmm(total.get("peak_at", 0), not args.h24)
            blit_text(static, 17, x0, "PEAK %s %s" % (bits(peak), at), C_PEAK)

        if lay.chart_y + lay.chart_h <= lay.h:
            cw = lay.pane_w - 2
            draw_curve(static, lay, rec, x0, lay.chart_y, cw, lay.chart_h,
                       args.h24)
            # Where the comet on the chart's leading edge lives. Baked here so
            # render() writes one pixel and computes nothing.
            cell["now_dot"] = (lay.chart_y, lay.chart_h, x0 + cw - 1)

        if lay.legend_y + 10 <= lay.h:
            draw_legend(static, lay, x0, lay.legend_y, util_full)

    def reload_data(now):
        rec, age, problem = read_ix(cache)
        cell["rec"], cell["problem"], cell["loaded"] = rec, problem, now
        cell["n_flow"] = 0
        cell["now_dot"] = None
        if rec is None:
            cell["stale"] = False
            return
        cell["stale"] = not ftdata.is_fresh(PRODUCT, age)
        static[:] = 0
        cell["n_flow"] = bake_map(rec)
        bake_pane(rec, age, cell["stale"])

    def render(t, i):
        now = now_of()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        if cell["rec"] is None:
            lines = [("NO SFMIX DATA", C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --LOOP 300", C_TEXT)]
            if cell["problem"]:
                lines.append((str(cell["problem"]).upper()[:52], C_DIM))
            return draw_nodata(frame, lay, lines)

        frame[:] = static

        n = cell["n_flow"]
        if n:
            # phase = (s +/- v t) mod PERIOD, then a linear falloff over the
            # tail. Six passes over one flat array and one scattered write; no
            # allocation, no boolean indexing, nothing that depends on how many
            # comets happen to be lit this frame.
            ph = work[:n]
            np.multiply(fl_speed[:n], t, out=ph)
            np.add(ph, fl_phase[:n], out=ph)
            np.mod(ph, FLOW_PERIOD, out=ph)
            np.multiply(ph, -1.0 / FLOW_TAIL, out=ph)
            np.add(ph, 1.0, out=ph)
            np.clip(ph, 0.0, 1.0, out=ph)
            b = buf[:n]
            np.multiply(fl_delta[:n], ph[:, None], out=b)
            np.add(b, fl_base[:n], out=b)
            flat[fl_idx[:n]] = b

        # One pulse on the chart's leading edge. The comets already guarantee
        # motion, but the eye that went to the big number needs telling that
        # the right-hand end of the curve is *now* and not the end of a day.
        dot = cell["now_dot"]
        if dot is not None:
            cy, ch, cx = dot
            py = cy + ch - 1 - int(((t * 0.5) % 1.0) * (ch - 1))
            frame[py, cx] = C_NOW
        return frame

    reload_data(now_of())
    render.state = cell
    render.layout = lay
    render.clock = now_of
    render.static = static
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "SFMIX backbone weathermap and today's exchange total",
                  fps=20)


if __name__ == "__main__":
    main()
