#!/usr/bin/env python3
"""The San Francisco tide, and which way the water is actually running.

Two things on one wide panel. Across the top, the predicted tide curve for a
day and a bit, with now marked, the extremes labelled, and the present height
called out -- the part somebody checks in three seconds on the way past.
Underneath, a map of the Golden Gate corridor with the flow drawn on it:
arrows that lengthen with the predicted current and reverse between flood and
ebb, and particles that drift along the channels at something like the real
speed. At slack the particles stop, because that is what slack is.

**The flow field is a schematic.** It has to be said plainly, because a map
with arrows on it looks like a model and this is not one. A real answer would
be SFBOFS, which is a gridded ocean model served as netCDF over THREDDS, and
which a Raspberry Pi at 600 MHz has no business downloading let alone
interpolating. So the picture is assembled from two much cheaper pieces:

  * the **pattern** comes from geometry. Solve Laplace's equation for a stream
    function over the sea mask, with the north shore held at one and the south
    shore at zero, and the velocity that falls out of it is divergence-free,
    exactly tangent to every shoreline, and crowds together where the channel
    narrows. That is most of what tidal flow in a bay looks like, and the Bay's
    circulation really is dominated by fixed bathymetry: what changes over six
    hours is essentially how hard it is running and which way.

  * the **amplitude and the sign** come from one number, the CO-OPS current
    prediction at the Golden Gate. Flood, and the field runs in the direction
    NOAA calls `meanFloodDir`; ebb, and it runs the other way; and the whole
    field scales with the predicted speed in knots.

What it therefore cannot tell you is anything a point prediction does not
know: eddies, the wind, the outflow after a wet week, or that the current on
one side of the channel leads the other. It is a picture of the phase, drawn
over real geography. It is not a forecast of the water in front of you.

**The corridor crop.** The Bay is long north to south, which is precisely the
wrong way round for a panel five times wider than it is tall. Squashing the
whole thing into 320x64 would put San Pablo Bay and the South Bay on screen at
a scale where every arrow is two pixels and means nothing -- lots of map, no
information. So the map is a slice: the Golden Gate, Alcatraz and the west
span of the Bay Bridge, which runs roughly east-west, fits the panel, and is
where the current is fastest and most worth looking at. The slice is stretched
about three times horizontally to fill the panel; at true scale a strip this
wide would be under two kilometres tall and would cut Alcatraz off the top.
`--extent` moves it.

The geography is `voxel-dem.npz`, the same georeferenced DEM the voxel demo
flies over, reused rather than sourced again: it is already committed, its
bounding box is already fitted against four known summits, and it already
carries the sea mask this needs.

**Data comes from the cache, never from the network.** `ftdata.py` fetches the
NOAA predictions on a timer in a process of its own; this demo reads the files
it leaves behind and does not import a HTTP library. Run the fetcher first:

    $ python3 ftdata.py --loop 900

Nothing here believes a file just because it parsed. Predictions are not
observations -- a record fetched yesterday morning is still telling the truth
-- so the test is not the age of the fetch but whether the payload's span
still covers now, and both are checked. If it does not, or if there is no file
at all, the panel says so in words instead of drawing a confident curve of the
wrong day. A tide clock showing yesterday's phase is worse than a blank one.
"""

import math
import os
import sys
import time

import numpy as np

import demoscene as ds
import ftdata

f32 = np.float32

DEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voxel-dem.npz")

# lat0, lat1, lon0, lon1. Out past the Gate on the west, Yerba Buena on the
# east; Alcatraz (37.8267) and the Bay Bridge west span (37.7983) both inside
# with a little margin. See the docstring on why this is not square.
EXTENT = (37.794, 37.836, -122.525, -122.365)

TIDE_STATION = "9414290"
CURRENT_STATION = "SFB1201"

KNOT_MS = 0.5144                        # knots to metres per second

# Colours. The water is nearly black so the flow reads as the only bright
# thing on it, and the land is a shade up from that with a lit shoreline --
# on an LED wall a drawn coastline is worth more than any amount of fill.
C_SEA = (4, 11, 20)
C_LAND = (26, 28, 32)
C_SHORE = (92, 100, 112)                # cool, so the warm flood barbs carry
C_BRIDGE_GG = (255, 92, 26)             # international orange, obviously
C_BRIDGE_BAY = (140, 142, 152)
C_FLOOD = (255, 156, 62)                # water coming in: warm
C_EBB = (72, 206, 255)                  # water going out: cold
C_SLACK = (120, 128, 140)
C_FUTURE = (128, 232, 255)
C_PAST = (58, 92, 110)
C_FILL = (8, 20, 29)
C_TEXT = (196, 214, 228)
C_DIM = (96, 110, 124)
C_MARK = (255, 244, 210)
C_WARN = (255, 96, 72)

# The faint vertical guide the now-marker rides on, kept as an array so
# compositing it is one call rather than an allocation every frame.
MARK_GUIDE = np.array((42, 50, 62), np.uint8)

# --------------------------------------------------------------------------
# A 3x5 pixel font, the same one defcon.py and sort.py use: five rows a glyph,
# each row an octal digit whose three bits are the three columns. Anything
# built from a real typeface is mush at five pixels, and the Pi does not have
# the same faces installed as the machine this was written on.
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


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = s.upper()
    if not s:
        return np.zeros((5 * scale, 1), bool)
    out = np.zeros((5, len(s) * 4 - 1), bool)
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


# --------------------------------------------------------------------------
# Clock. Everything downstream asks for `now` rather than reading the system
# clock, which is what makes a contact sheet across a whole tidal cycle
# possible: --at moves the demo's idea of the present and --rate runs it fast.
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

    Local to the *display*, not to the station. For a wall in the same city as
    the tide gauge -- which is the case this exists for -- those are the same
    thing, and when they are not, the time somebody standing in front of the
    panel can act on is the one on their own watch.
    """
    lt = time.localtime(epoch)
    if not ampm:
        return time.strftime("%H:%M", lt)
    h = lt.tm_hour % 12 or 12
    return "%d:%02d%s" % (h, lt.tm_min, "A" if lt.tm_hour < 12 else "P")


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises; everything that
# can still be wrong after that is wrong about *content*, and is caught here.
# The three states a demo has to be able to draw are missing, stale and
# partial, and they are different: missing means no file, stale means a file
# whose span has run out from under it, and partial means the tide arrived and
# the current did not. Only the first two stop the curve.
# --------------------------------------------------------------------------

class Series(object):
    """An evenly sampled series with linear interpolation, or None."""

    __slots__ = ("t0", "step", "v", "n", "_x", "_l")

    def __init__(self, packed):
        if "t0" in packed:
            self.t0 = float(packed["t0"])
            self.step = float(packed["step"])
            self.v = np.asarray(packed["v"], f32)
        else:
            # The fetcher fell back to explicit timestamps because the grid had
            # a hole in it. Resample onto a regular one rather than carrying
            # two code paths through the drawing.
            t = np.asarray(packed["t"], np.float64)
            v = np.asarray(packed["v"], f32)
            self.t0 = float(t[0])
            self.step = float(np.median(np.diff(t))) if len(t) > 1 else 360.0
            n = int((t[-1] - t[0]) / self.step) + 1
            grid = self.t0 + np.arange(n) * self.step
            self.v = np.interp(grid, t, v).astype(f32)
        self.n = len(self.v)
        self._x = np.arange(self.n)
        # A plain Python list for the scalar path. `at()` on one number costs
        # five numpy calls, and the render loop asks for three of them every
        # frame; indexing a list twice costs none, and this series is a
        # thousand floats.
        self._l = [float(x) for x in self.v]

    @property
    def t1(self):
        return self.t0 + (self.n - 1) * self.step

    def covers(self, a, b):
        return self.n >= 2 and self.t0 <= a and self.t1 >= b

    def at(self, t):
        """Interpolate at scalar or array `t`, clamped at the ends."""
        x = (np.asarray(t, np.float64) - self.t0) / self.step
        return np.interp(x, self._x, self.v).astype(f32)

    def value(self, t):
        """The same, for one moment, without touching numpy at all."""
        x = (float(t) - self.t0) / self.step
        if x <= 0.0:
            return self._l[0]
        if x >= self.n - 1:
            return self._l[-1]
        i = int(x)
        f = x - i
        return self._l[i] * (1.0 - f) + self._l[i + 1] * f


def read_tide(cache_dir, station):
    got = ftdata.load("tide-" + station, cache_dir)
    if got is None:
        return None, None, "no cached tide predictions"
    payload, age = got
    try:
        curve = Series(payload["curve"])
        extremes = [(float(e["t"]), float(e["v"]), e.get("type", ""))
                    for e in payload.get("extremes", [])]
    except Exception:                                        # noqa: BLE001
        return None, age, "tide record is malformed"
    if curve.n < 2:
        return None, age, "tide record has no curve"
    return {"curve": curve, "extremes": extremes, "age": age,
            "station": payload.get("station", station),
            "name": str(payload.get("name") or station),
            "units": payload.get("units", "ft"),
            "datum": payload.get("datum", "")}, age, None


def read_current(cache_dir, station):
    got = ftdata.load("currents-" + station, cache_dir)
    if got is None:
        return None, None, "no cached current predictions"
    payload, age = got
    try:
        vel = Series(payload["velocity"])
        flood = float(payload["flood_dir"])
        ebb = float(payload["ebb_dir"])
    except Exception:                                        # noqa: BLE001
        return None, age, "current record is malformed"
    if vel.n < 2:
        return None, age, "current record has no series"
    events = []
    for e in payload.get("events", []):
        try:
            events.append((float(e["t"]), str(e.get("type", "")),
                           float(e.get("v", 0.0))))
        except Exception:                                    # noqa: BLE001
            continue
    try:
        latlon = (float(payload["lat"]), float(payload["lon"]))
    except Exception:                                        # noqa: BLE001
        latlon = None
    return {"vel": vel, "flood_dir": flood, "ebb_dir": ebb, "events": events,
            "age": age, "station": payload.get("station", station),
            "name": str(payload.get("name") or station),
            "latlon": latlon}, age, None


def next_slack(events, now):
    for t, kind, _ in events:
        if kind == "slack" and t > now:
            return t
    return None


# --------------------------------------------------------------------------
# Geography: the sea mask, cropped out of the DEM the voxel demo already ships.
# --------------------------------------------------------------------------

def load_sea():
    d = np.load(DEM)
    shape = tuple(int(v) for v in d["shape"])
    sea = np.unpackbits(d["sea"])[:shape[0] * shape[1]].reshape(shape)
    return sea.astype(bool), tuple(float(v) for v in d["bbox"])


def crop_sea(sea, bbox, extent, gw, gh, sub=3):
    """The sea mask resampled into a (gh, gw) grid over `extent`.

    Area-averaged rather than sampled: the map grid is coarser than the DEM in
    latitude by a factor of two or three, and a nearest-neighbour crop drops
    every channel narrower than a cell -- which in this bay means Raccoon
    Strait blinking in and out with the crop, and worse, a shoreline that
    changes shape with `--height`.
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


def metres_per_degree(lat):
    return 111132.0, 111320.0 * math.cos(math.radians(lat))


def extent_metres(extent):
    la0, la1, lo0, lo1 = extent
    mlat, mlon = metres_per_degree(0.5 * (la0 + la1))
    return (lo1 - lo0) * mlon, (la1 - la0) * mlat


# --------------------------------------------------------------------------
# The flow field.
#
# Stream function psi on the sea, Laplace's equation, north shore held at one
# and south shore at zero. Velocity is (dpsi/dy, -dpsi/dx), which is
# divergence-free by construction, parallel to any line of constant psi -- so
# tangent to the shore, since the shore *is* a contour -- and fast wherever the
# contours crowd, which is exactly the constrictions. Islands are the only
# subtlety: a hole in the domain has an unknown constant on it rather than a
# known one, so each island's value is re-set to the mean of the water around
# it as the relaxation proceeds, which is the condition that no net flow
# circulates round it.
#
# Solved with red-black SOR up a three-level ladder, coarse first. Plain
# Jacobi over-relaxation diverges -- the whole point of over-relaxation is that
# a cell sees its neighbours' *new* values -- and a lattice this long converges
# far too slowly at full resolution to be worth trying directly.
# --------------------------------------------------------------------------

def _dilate(a):
    o = a.copy()
    o[1:] |= a[:-1]
    o[:-1] |= a[1:]
    o[:, 1:] |= a[:, :-1]
    o[:, :-1] |= a[:, 1:]
    return o


def _fill(land, seed):
    """Grow `seed` through `land`. The convergence test is the expensive part.

    One dilation is four cheap slice-ORs; `.sum()` is a full reduction, and on
    a grid whose longest run is a couple of hundred cells the test costs more
    than the work it is testing. So it runs eight dilations between checks.
    """
    comp = seed & land
    n = int(comp.sum())
    while True:
        for _ in range(8):
            comp = _dilate(comp) & land
        m = int(comp.sum())
        if m == n:
            return comp
        n = m


def _classify(sea):
    """Split land into the north shore, the south shore, and labelled islands.

    Connectivity, not a per-column rule: a spit that reaches down from the
    Marin side past the middle of the frame is still the north shore, and a
    per-column test would call it an island and let water run behind it.
    """
    land = ~sea
    top = np.zeros_like(land)
    top[0] = True
    north = _fill(land, top)
    bot = np.zeros_like(land)
    bot[-1] = True
    south = _fill(land, bot)
    islands = land & ~north & ~south
    labels = np.full(sea.shape, -1, np.int16)
    todo = islands.copy()
    k = 0
    while todo.any():
        seed = np.zeros_like(todo)
        seed.flat[int(np.argmax(todo))] = True
        comp = _fill(islands, seed)
        labels[comp] = k
        todo &= ~comp
        k += 1
    return north, south, labels, k


def _relax(sea, north, south, labels, nisl, sweeps, psi=None, omega=1.85):
    gh, gw = sea.shape
    if psi is None:
        psi = np.tile(np.linspace(1.0, 0.0, gh, dtype=f32)[:, None], (1, gw))
    psi = np.ascontiguousarray(psi, f32)
    psi[north] = 1.0
    psi[south] = 0.0
    ring = [_dilate(labels == i) & sea for i in range(nisl)]
    body = [labels == i for i in range(nisl)]
    rr, cc = np.indices(sea.shape)
    red = ((rr + cc) & 1).astype(bool)
    # Multiplying by a float mask rather than indexing with a boolean one. The
    # result is identical -- land is multiplied by zero and keeps its Dirichlet
    # value -- but a boolean index is a gather and a scatter, three times the
    # price of a whole-array pass, and this loop runs a thousand times.
    masks = ((sea & ~red).astype(f32) * omega, (sea & red).astype(f32) * omega)
    # The crop's north and south edges are walls: water may run along them but
    # not through, which is what makes the slice a channel. East and west are
    # open, so the field there is whatever the interior asks for.
    buf = np.empty_like(psi)
    for s in range(sweeps):
        for m in masks:
            buf[:, 1:] = psi[:, :-1]
            buf[:, 0] = psi[:, 0]
            buf[:, :-1] += psi[:, 1:]
            buf[:, -1] += psi[:, -1]
            buf[1:] += psi[:-1]
            buf[0] += 1.0                       # wall to the north
            buf[:-1] += psi[1:]                 # wall to the south is zero
            buf *= 0.25
            buf -= psi
            buf *= m
            psi += buf
        if s % 4 == 0:
            for i in range(nisl):
                if ring[i].any():
                    psi[body[i]] = psi[ring[i]].mean()
    return psi


def solve_flow(sea_full, bbox, extent, cols=96, sweeps=(240, 120, 60)):
    """Return (u_east, v_north, sea) in m/s per unit station speed.

    The solve grid is square in *metres*, not in pixels: the physics has to
    happen in the real bay, and the horizontal stretch that fits it on the
    panel is applied afterwards, when the field is mapped to screen. An affine
    squash preserves tangency, so a field that hugs the true shoreline still
    hugs the drawn one.
    """
    wm, hm = extent_metres(extent)
    gw = int(cols)
    gh = max(6, int(round(gw * hm / wm)))
    psi = None
    for lvl, sw in enumerate(sweeps):
        div = 1 << (len(sweeps) - 1 - lvl)
        w, h = max(8, gw // div), max(4, gh // div)
        s = crop_sea(sea_full, bbox, extent, w, h)
        n, so, lb, ni = _classify(s)
        if psi is not None:
            psi = np.repeat(np.repeat(psi, 2, 0), 2, 1)
            if psi.shape != (h, w):
                pad = ((0, max(0, h - psi.shape[0])), (0, max(0, w - psi.shape[1])))
                psi = np.pad(psi, pad, mode="edge")[:h, :w]
        psi = _relax(s, n, so, lb, ni, sw, psi)
    sea = crop_sea(sea_full, bbox, extent, gw, gh)
    dx, dy = wm / gw, hm / gh
    u = (-np.gradient(psi, axis=0) / dy) * sea       # metres east per second
    v = (-np.gradient(psi, axis=1) / dx) * sea       # metres north per second
    return u.astype(f32), v.astype(f32), sea


def cell_of(extent, shape, lat, lon):
    la0, la1, lo0, lo1 = extent
    gh, gw = shape
    r = int((la1 - lat) / (la1 - la0) * gh)
    c = int((lon - lo0) / (lo1 - lo0) * gw)
    return r, c


def field_bearing(u, v, sea, r, c, radius=2):
    """The compass bearing of the mean flow in a window, or None."""
    gh, gw = sea.shape
    w = (slice(max(0, r - radius), min(gh, r + radius + 1)),
         slice(max(0, c - radius), min(gw, c + radius + 1)))
    m = sea[w]
    if not m.any():
        return None
    ue, vn = float(u[w][m].mean()), float(v[w][m].mean())
    if ue == 0.0 and vn == 0.0:
        return None
    return math.degrees(math.atan2(ue, vn)) % 360.0


def angle_diff(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


# --------------------------------------------------------------------------
# Arrow sprites. The direction only ever takes two values -- along the flood
# axis or along the ebb one -- so both are baked once, at a handful of lengths,
# into whole-map alpha layers. Drawing the arrows at run time is then a single
# blend of one precomputed layer, rather than a rotation and a rasterisation
# per arrow per frame, and picking the layer by speed is what makes a slack
# tide show no arrows at all rather than short ones.
# --------------------------------------------------------------------------

def _line(alpha, y0, x0, y1, x1, width=1.0, value=1.0):
    """Stamp a thick segment into an alpha map, antialiased across its width."""
    h, w = alpha.shape
    lo_y, hi_y = int(min(y0, y1) - width - 1), int(max(y0, y1) + width + 2)
    lo_x, hi_x = int(min(x0, x1) - width - 1), int(max(x0, x1) + width + 2)
    lo_y, hi_y = max(0, lo_y), min(h, hi_y)
    lo_x, hi_x = max(0, lo_x), min(w, hi_x)
    if hi_y <= lo_y or hi_x <= lo_x:
        return
    # np.arange and broadcasting, not np.mgrid: mgrid materialises two full
    # coordinate grids through np.indices, and this runs a couple of hundred
    # times while the arrows are baked.
    py = np.arange(lo_y, hi_y, dtype=f32)[:, None] + (0.5 - y0)
    px = np.arange(lo_x, hi_x, dtype=f32)[None, :] + (0.5 - x0)
    dy, dx = y1 - y0, x1 - x0
    ll = dy * dy + dx * dx
    tpar = np.clip((py * dy + px * dx) / max(ll, 1e-6), 0.0, 1.0)
    d = np.hypot(py - tpar * dy, px - tpar * dx)
    a = np.clip(width - d + 0.5, 0.0, 1.0) * value
    np.maximum(alpha[lo_y:hi_y, lo_x:hi_x], a, out=alpha[lo_y:hi_y, lo_x:hi_x])


def bake_arrows(u_disp, v_disp, sea, anchors, lengths, width=1.1):
    """Alpha layers: `[direction][length bucket] -> (h, w) float32`.

    `anchors` are (row, col) in map pixels. Each grows an arrow along the local
    flow, so the barbs sit on the channel rather than on a lattice.
    """
    h, w = sea.shape
    out = [[np.zeros((h, w), f32) for _ in lengths] for _ in range(2)]
    for (r, c) in anchors:
        if not (0 <= r < h and 0 <= c < w) or not sea[r, c]:
            continue
        ux, vy = float(u_disp[r, c]), float(v_disp[r, c])
        n = math.hypot(ux, vy)
        if n < 1e-6:
            continue
        ux, vy = ux / n, vy / n
        for sgn in (0, 1):
            dx, dy = (ux, vy) if sgn == 0 else (-ux, -vy)
            for k, ln in enumerate(lengths):
                if ln <= 0:
                    continue
                a = out[sgn][k]
                # Grown from the anchor backwards to the head, so an arrow
                # getting longer keeps its tip in the same place and reads as
                # the same arrow rather than as a new one appearing.
                hx, hy = c + dx * ln * 0.5, r + dy * ln * 0.5
                tx, ty = c - dx * ln * 0.5, r - dy * ln * 0.5
                _line(a, ty, tx, hy, hx, width)
                bl = max(2.0, ln * 0.34)
                for s in (1, -1):
                    bx = hx - dx * bl + s * (-dy) * bl * 0.62
                    by = hy - dy * bl + s * dx * bl * 0.62
                    _line(a, hy, hx, by, bx, width)
    # A barb that runs up the beach reads as an arrow pointing at a hill, so
    # everything is clipped back to the water it is describing.
    for pair in out:
        for a in pair:
            a *= sea
    return out


def pick_anchors(sea, u, v, count, margin=2):
    """Anchor points spread along the channel, one per band of columns.

    In each band, the water cell where the flow is fastest -- so the barbs land
    in the channel and not in a backwater, and the Gate always gets one.
    """
    h, w = sea.shape
    speed = np.hypot(u, v) * sea
    out = []
    edges = np.linspace(margin, w - margin, count + 1).astype(int)
    for i in range(count):
        a, b = edges[i], edges[i + 1]
        band = speed[:, a:b]
        if not band.any():
            continue
        idx = int(np.argmax(band))
        out.append((idx // (b - a), a + idx % (b - a)))
    return out


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--tide-station", default=TIDE_STATION,
                    help="NOAA CO-OPS water level station")
    ap.add_argument("--current-station", default=CURRENT_STATION,
                    help="NOAA CO-OPS current prediction station")
    ap.add_argument("--extent", default=",".join("%g" % v for v in EXTENT),
                    help="map crop as lat0,lat1,lon0,lon1")
    ap.add_argument("--span", type=float, default=30.0,
                    help="hours of tide curve across the panel")
    ap.add_argument("--anchor", default="day", choices=("day", "now"),
                    help="'day' pins the window to the local day so the marker "
                         "traverses; 'now' slides the window under a fixed marker")
    ap.add_argument("--map-rows", type=int, default=0,
                    help="rows given to the map (0 = about half the panel)")
    ap.add_argument("--particles", type=int, default=170,
                    help="drifters on the map; few and long beats many and "
                         "small, which reads as static rather than flow")
    ap.add_argument("--flow-gain", type=float, default=7.0,
                    help="map pixels per second at one knot; the map is a "
                         "time lapse, real water would crawl")
    ap.add_argument("--arrows", type=int, default=6, help="flow barbs across the map")
    ap.add_argument("--solve-cols", type=int, default=96,
                    help="stream-function grid width; cost is roughly its square")
    ap.add_argument("--metric", action="store_true",
                    help="metres and metres per second instead of feet and knots")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second; 3600 runs a tidal "
                         "cycle in a few seconds")
    ap.add_argument("--reload", type=float, default=600.0,
                    help="seconds between re-reads of the cache (0 = never)")


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
    def __init__(self, w, h, map_rows):
        self.w, self.h = w, h
        if map_rows <= 0:
            map_rows = max(8, int(round(h * 0.53)))
        self.map_rows = min(h - 10, map_rows)
        self.map_y = h - self.map_rows
        self.head_y = 0
        self.head_h = 6 if h >= 40 else 6
        self.curve_y = self.head_h
        self.curve_h = max(6, self.map_y - self.head_h)
        # Room for a label beside an extreme without it hanging off the band.
        self.pad = 3 if self.curve_h >= 16 else 0


def draw_map(dst, lay, sea, extent, tide_ok):
    """Land, water and a lit shoreline, plus the two bridges as landmarks."""
    reg = dst[lay.map_y:lay.map_y + lay.map_rows]
    reg[sea] = C_SEA
    reg[~sea] = C_LAND
    shore = _dilate(sea) & ~sea
    reg[shore] = C_SHORE

    # The bridges are what make the picture legible as this bay rather than as
    # a generic estuary; two lines are worth more than any amount of coastline.
    def span(lat_a, lon_a, lat_b, lon_b, rgb):
        r0, c0 = cell_of(extent, (lay.map_rows, lay.w), lat_a, lon_a)
        r1, c1 = cell_of(extent, (lay.map_rows, lay.w), lat_b, lon_b)
        n = max(abs(r1 - r0), abs(c1 - c0), 1)
        rr = np.round(np.linspace(r0, r1, n + 1)).astype(int)
        cc = np.round(np.linspace(c0, c1, n + 1)).astype(int)
        ok = (rr >= 0) & (rr < lay.map_rows) & (cc >= 0) & (cc < lay.w)
        reg[rr[ok], cc[ok]] = rgb

    span(37.8060, -122.4783, 37.8327, -122.4784, C_BRIDGE_GG)      # Golden Gate
    span(37.7908, -122.3877, 37.8078, -122.3607, C_BRIDGE_BAY)     # Bay Bridge W


_WINDOW_CACHE = {}


def curve_window(now, span_h, anchor):
    """(t0, t1) for the horizontal axis.

    Pinned to the local day by default. The alternative -- a window that always
    starts a fixed number of hours before now -- freezes the marker in the
    middle of the panel and slides the whole curve underneath it, which reads
    as a scrolling graph rather than as a clock. Pinned, the marker crosses the
    panel once a day and the window steps at four in the morning, when nobody
    is looking at it.
    """
    span = span_h * 3600.0
    if anchor == "now":
        return now - span * 0.35, now + span * 0.65
    lt = time.localtime(now)
    key = (lt.tm_year, lt.tm_mon, lt.tm_mday)
    midnight = _WINDOW_CACHE.get(key)
    if midnight is None:
        # mktime is not free and the answer changes once a day; asking it every
        # frame was a fifth of a millisecond of pure superstition.
        midnight = time.mktime(key + (0, 0, 0, 0, 0, -1))
        _WINDOW_CACHE.clear()
        _WINDOW_CACHE[key] = midnight
    t0 = midnight - (span - 86400.0) * 0.5
    if now < t0:
        t0 -= 86400.0
    elif now > t0 + span:
        t0 += 86400.0
    return t0, t0 + span


def curve_band(lay):
    """(top row, bottom row) the curve is drawn between."""
    band_t = lay.curve_y + lay.pad
    band_b = lay.curve_y + lay.curve_h - 1 - lay.pad
    if band_b <= band_t:
        return lay.curve_y, lay.curve_y + lay.curve_h - 1
    return band_t, band_b


def draw_curve(dst, lay, tide, t0, t1, now, metric, h24):
    """The tide curve, its fill, the extreme labels and the hour ticks.

    Returns the height range it chose, so the marker can be placed on the same
    axis without resampling the whole curve again every frame.
    """
    w = lay.w
    band_t, band_b = curve_band(lay)

    ts = t0 + (np.arange(w, dtype=np.float64) + 0.5) * (t1 - t0) / w
    hv = tide["curve"].at(ts)
    scale = 0.3048 if metric else 1.0
    lo, hi = _band_range(hv)

    def row_of(v):
        f = (np.asarray(v, f32) - lo) / (hi - lo)
        return np.clip(band_b - f * (band_b - band_t), band_t, band_b)

    rows = row_of(hv)
    ri = np.round(rows).astype(int)
    now_col = int(np.clip((now - t0) / (t1 - t0) * w, 0, w - 1))

    # The curve as a filled band between neighbouring columns, so it stays
    # connected where it is steep. A one-pixel plot of a function that moves
    # three rows a column is a dotted line, and on a panel this size a dotted
    # tide curve reads as noise.
    nxt = np.empty_like(ri)
    nxt[:-1] = ri[1:]
    nxt[-1] = ri[-1]
    top = np.minimum(ri, nxt)
    bot = np.maximum(ri, nxt)
    yy = np.arange(lay.curve_y, lay.curve_y + lay.curve_h)[:, None]
    reg = dst[lay.curve_y:lay.curve_y + lay.curve_h]
    on = (yy >= top[None, :]) & (yy <= bot[None, :])
    under = (yy > bot[None, :])
    past = np.arange(w)[None, :] <= now_col

    # Filled behind now, hollow ahead of it. That, and not the colour, is
    # what makes the direction of time obvious across a room.
    reg[under & past] = C_FILL
    reg[on & past] = C_PAST
    reg[on & ~past] = C_FUTURE

    # Hour ticks along the bottom, taller at midnight. Cheap orientation: you
    # can see at a glance whether the marker is in the morning or the evening.
    step = 3600.0 * (3 if lay.curve_h >= 18 else 6)
    tick = math.ceil(t0 / step) * step
    while tick < t1:
        c = int((tick - t0) / (t1 - t0) * w)
        lt = time.localtime(tick)
        big = lt.tm_hour == 0
        n = 3 if big else 1
        dst[lay.curve_y + lay.curve_h - n:lay.curve_y + lay.curve_h, c] = \
            C_DIM if not big else C_TEXT
        tick += step

    # Extremes: labelled beside the peak, not above it, so the whole band is
    # available to the curve itself. On a narrow panel four of them in a day
    # do not fit side by side, so a label that would land on one already drawn
    # is dropped and its tick left to speak for it -- two overlapping times
    # are worse than one time and a mark.
    boxes = []
    for te, ve, kind in tide["extremes"]:
        if not (t0 <= te <= t1):
            continue
        c = int((te - t0) / (t1 - t0) * w)
        r = int(round(float(row_of(np.array([ve], f32))[0])))
        dim = te < now
        rgb = C_PAST if dim else C_MARK
        dst[max(lay.curve_y, r - 1):r + 2, c] = rgb
        if lay.curve_h < 14:
            # Nine rows of curve and a five-row label under every extreme is
            # not a graph, it is a wall of digits over a squiggle. The tick
            # stays; the label goes.
            continue
        label = "%s %.1f" % (hhmm(te, not h24), ve * scale)
        tw = text_width(label)
        lx = c + 3
        if lx + tw > w:
            lx = c - 3 - tw
        lx = max(0, min(lx, w - tw))
        ly = r - 6 if kind.upper().startswith("H") else r + 2
        ly = int(np.clip(ly, lay.curve_y, lay.curve_y + lay.curve_h - 5))
        box = (lx - 1, ly - 1, lx + tw + 1, ly + 6)
        if any(box[0] < b[2] and b[0] < box[2]
               and box[1] < b[3] and b[1] < box[3] for b in boxes):
            continue
        boxes.append(box)
        blit_text(dst, ly, lx, label, C_DIM if dim else C_TEXT)
    return lo, hi


def phase_words(v, thresh=0.15):
    if abs(v) < thresh:
        return "SLACK", C_SLACK
    return ("FLOOD", C_FLOOD) if v > 0 else ("EBB", C_EBB)


def header_text(state, metric, h24, w=ds.WIDTH):
    """The status line, left, centre and right, fitted to a panel `w` wide.

    Each of the three has a ladder of shorter forms, and the widest set that
    fits is the one that gets drawn. Simply clipping the line instead loses
    whatever falls off the end, and what falls off the end of this one is the
    part that says how old the data is -- which is the last thing that should
    go quietly missing.
    """
    tide, cur = state["tide"], state["current"]
    hscale, hunit = (0.3048, "M") if metric else (1.0, "FT")
    vscale, vunit = (KNOT_MS, "M/S") if metric else (1.0, "KN")

    if tide is None:
        lefts = ["NO TIDE DATA", "NO TIDE"]
    else:
        trend = "RISING" if state["dhdt"] > 0 else "FALLING"
        h = "%.1f%s" % (state["h"] * hscale, hunit)
        lefts = ["%s %s %s" % (tide["name"][:18], h, trend),
                 "%s %s" % (h, trend), h]

    if cur is None:
        mids, midc = ["NO CURRENT DATA", "NO CURRENT", ""], C_WARN
    else:
        word, midc = phase_words(state["v"])
        deg = cur["flood_dir"] if state["v"] > 0 else cur["ebb_dir"]
        nxt = hhmm(state["next_slack"], not h24) if state["next_slack"] else ""
        if word == "SLACK":
            # "SLACK  SLACK 3:51P" is two different slacks a word apart and
            # reads as a stutter. Say which one is now and which one is next.
            mids = ["SLACK NOW  NEXT %s" % nxt if nxt else "SLACK NOW",
                    "SLACK NOW", "SLACK"]
        else:
            full = "%s %.1f%s %03d" % (word, abs(state["v"]) * vscale,
                                       vunit, deg)
            mids = ["%s  SLACK %s" % (full, nxt) if nxt else full, full,
                    "%s %.1f" % (word, abs(state["v"]) * vscale), word]

    ages = [x["age"] for x in (tide, cur) if x is not None]
    age = ftdata.describe_age(max(ages)) if ages else ""
    rights = ["DATA " + age, age, ""] if age else [""]
    if state["stale"]:
        rights = ["STALE  " + r if r else "STALE" for r in rights]

    gap = 5
    for li, left in enumerate(lefts):
        for ri, right in enumerate(rights):
            for mid in mids:
                need = text_width(left) + text_width(right) + 2
                if mid:
                    need += text_width(mid) + 2 * gap
                if need <= w:
                    return left, mid, midc, right
    return lefts[-1], "", midc, ""


def draw_header(dst, lay, state, metric, h24):
    left, mid, midc, right = header_text(state, metric, h24, lay.w)
    dst[:lay.head_h] = 0
    blit_text(dst, 0, 1, left, C_TEXT)
    rw = text_width(right) if right else 0
    if right:
        blit_text(dst, 0, lay.w - rw - 1, right,
                  C_WARN if state["stale"] else C_DIM)
    if mid:
        mw = text_width(mid)
        mx = min(lay.w - rw - 4 - mw, max(text_width(left) + 5,
                                          (lay.w - mw) // 2))
        blit_text(dst, 0, mx, mid, midc)
    dst[lay.head_h - 1] = (14, 16, 20)


def draw_nodata(dst, lay, lines):
    """The honest panel. No curve, no arrows, no implication of a phase."""
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
    extent = parse_extent(args.extent)
    lay = Layout(w, h, args.map_rows)
    cache = args.cache_dir
    now_of = clock(parse_when(args.at), args.rate)

    sea_full, bbox = load_sea()
    sea_map = crop_sea(sea_full, bbox, extent, w, lay.map_rows)

    # Geometry first, and only once: the field does not depend on the tide.
    u_m, v_m, sea_solve = solve_flow(sea_full, bbox, extent, args.solve_cols)
    wm, hm = extent_metres(extent)

    # Normalise so the field is one metre per second where the current station
    # is; the prediction then multiplies straight through as a real speed. The
    # station's own coordinates come with the record, so pointing the demo at
    # another station moves the reference point with it -- and if that point is
    # not on the map, there is no honest flow to draw and none is drawn.
    cur0, _, _ = read_current(cache, args.current_station)
    latlon = cur0["latlon"] if cur0 else None
    on_map = latlon is not None and (
        extent[0] <= latlon[0] <= extent[1] and extent[2] <= latlon[1] <= extent[3])
    if latlon is None and args.current_station == CURRENT_STATION:
        latlon, on_map = (37.8106, -122.502), True     # the Gate, before first fetch
    sr, sc = cell_of(extent, sea_solve.shape,
                     *(latlon if latlon else (0.5 * (extent[0] + extent[1]),
                                              0.5 * (extent[2] + extent[3]))))
    sr = int(np.clip(sr, 0, sea_solve.shape[0] - 1))
    sc = int(np.clip(sc, 0, sea_solve.shape[1] - 1))
    ref = float(np.hypot(u_m, v_m)[max(0, sr - 2):sr + 3,
                                   max(0, sc - 2):sc + 3].max())
    if ref < 1e-9:
        ref = float(np.hypot(u_m, v_m).max()) or 1.0
    u_m = u_m / ref
    v_m = v_m / ref
    flood_bearing = field_bearing(u_m, v_m, sea_solve, sr, sc)

    # Into display pixels. x is stretched relative to y by the crop's aspect;
    # this is the one place that stretch is applied.
    gh, gw = sea_solve.shape
    ry = np.clip((np.arange(lay.map_rows) + 0.5) / lay.map_rows * gh,
                 0, gh - 1).astype(np.int32)
    rx = np.clip((np.arange(w) + 0.5) / w * gw, 0, gw - 1).astype(np.int32)
    u_disp = (u_m[np.ix_(ry, rx)] * (w / wm)).astype(f32)          # px east / s
    v_disp = (-v_m[np.ix_(ry, rx)] * (lay.map_rows / hm)).astype(f32)   # px down / s
    u_disp *= sea_map
    v_disp *= sea_map

    anchors = pick_anchors(sea_map, u_disp, v_disp, args.arrows)
    max_len = max(6.0, min(30.0, w / 14.0))
    lengths = [0.0] + [max_len * k / 5.0 for k in (2, 3, 4, 4.6, 5)]
    alpha = bake_arrows(u_disp, v_disp, sea_map, anchors, lengths)
    n_buckets = len(lengths)
    # Composited to finished uint8 layers here, so drawing them costs one
    # integer maximum instead of a float blend over the whole map every frame.
    # Brighter than the drifters and more saturated than the land, which is the
    # only way a barb wins a map this dark.
    arrows = [[(a[:, :, None] * np.array(col, f32)).astype(np.uint8)
               for a in pair]
              for pair, col in zip(alpha, ((255, 182, 86), (152, 228, 255)))]

    sea_flat = sea_map.reshape(-1)
    water_idx = np.flatnonzero(sea_flat).astype(np.int32)
    # Row-then-column throughout, and both axes in one array: a (2, N) layout
    # turns every per-particle operation into one numpy call instead of two,
    # and on this machine the call is most of the cost whatever its size.
    field = np.stack([v_disp.reshape(-1), u_disp.reshape(-1)]).astype(f32)
    # Unit direction, kept separately from the speed: the streak a particle
    # leaves has to be a fixed few pixels long whatever the tide is doing, or
    # it vanishes near slack exactly when the direction is hardest to read.
    heading = field / np.maximum(np.hypot(field[0], field[1]), 1e-9)

    rng = np.random.default_rng(0x71DE)
    npart = max(1, int(args.particles))
    pidx = water_idx[rng.integers(0, len(water_idx), npart)]
    pos = np.stack([(pidx // w).astype(f32), (pidx % w).astype(f32)])
    pos += rng.random((2, npart), dtype=f32)
    page = rng.random(npart, dtype=f32) * 6.0
    plife = 3.0 + rng.random(npart, dtype=f32) * 5.0
    bounds = np.array([[lay.map_rows - 1.0], [w - 1.0]], f32)
    ipos = np.empty((2, npart), np.int32)
    clipped = np.empty((2, npart), f32)
    hbuf = np.empty((2, npart), f32)
    streak = np.empty((3, 2, npart), f32)
    STREAK = np.array([4.4, 2.2, 0.0], f32).reshape(3, 1, 1)

    static = np.zeros((h, w, 3), np.uint8)
    frame = np.zeros((h, w, 3), np.uint8)
    header = np.zeros((lay.head_h, w, 3), np.uint8)

    cell = {"tide": None, "current": None, "problems": [], "loaded": -1e18,
            "on_map": False,
            "window": None, "static_key": None, "head_key": None,
            "band": None, "slack": None, "head_at": None}

    def reload_data(now):
        tide, tage, terr = read_tide(cache, args.tide_station)
        cur, cage, cerr = read_current(cache, args.current_station)
        problems = []
        if tide is not None and not tide["curve"].covers(now - 60, now + 60):
            terr = "tide predictions no longer cover now"
            tide = None
        if cur is not None and not cur["vel"].covers(now - 60, now + 60):
            cerr = "current predictions no longer cover now"
            cur = None
        if terr:
            problems.append(terr)
        if cerr:
            problems.append(cerr)
        cell["tide"], cell["current"] = tide, cur
        cell["problems"] = problems
        cell["loaded"] = now
        cell["static_key"] = None
        cell["slack"] = None

    def sample(now):
        tide, cur = cell["tide"], cell["current"]
        st = {"tide": tide, "current": cur, "h": 0.0, "dhdt": 0.0, "v": 0.0,
              "next_slack": cell["slack"], "stale": False}
        if tide is not None:
            st["h"] = tide["curve"].value(now)
            st["dhdt"] = (tide["curve"].value(now + 900)
                          - tide["curve"].value(now - 900))
            st["stale"] |= not ftdata.is_fresh("tide-" + args.tide_station,
                                               tide["age"])
        if cur is not None:
            st["v"] = cur["vel"].value(now)
            # The next slack only stops being the next slack when it arrives,
            # so walking the event list every frame is a scan for an answer
            # that changed six hours ago.
            if cell["slack"] is None or now >= cell["slack"]:
                cell["slack"] = st["next_slack"] = next_slack(cur["events"], now)
            st["stale"] |= not ftdata.is_fresh("currents-" + args.current_station,
                                               cur["age"])
        return st

    def arrow_bucket(state):
        """Which barb layer the present speed asks for, and which way."""
        if state["current"] is None or not cell["on_map"]:
            return None
        k = int(abs(state["v"]) / 3.0 * (n_buckets - 1) + 0.5)
        if k <= 0:
            return None
        return (0 if state["v"] > 0 else 1, min(k, n_buckets - 1))

    def rebuild_static(now, state, bucket):
        static[:] = 0
        draw_map(static, lay, sea_map, extent, state["tide"] is not None)
        # The barbs go in here rather than in the frame loop. Their length is
        # quantised and the tide takes ten minutes or so to move a bucket, so
        # compositing them thirty times a second was paying a per-frame price
        # for a picture that changes twice an hour.
        if bucket is not None:
            reg = static[lay.map_y:lay.map_y + lay.map_rows]
            np.maximum(reg, arrows[bucket[0]][bucket[1]], out=reg)
        t0, t1 = cell["window"]
        if state["tide"] is not None:
            cell["band"] = draw_curve(static, lay, state["tide"], t0, t1, now,
                                      args.metric, args.h24)
        else:
            msg = "NO TIDE DATA -- RUN FTDATA.PY --LOOP 900"
            blit_text(static, lay.curve_y + lay.curve_h // 2 - 2,
                      max(0, (w - text_width(msg)) // 2), msg, C_WARN)
        if state["current"] is None:
            msg = "NO CURRENT DATA"
            blit_text(static, lay.map_y + 2,
                      max(0, (w - text_width(msg)) // 2), msg, C_WARN)
        elif not cell["on_map"]:
            # The curve can follow any gauge in the country; the map cannot.
            # Scaling this bay's channels by another bay's prediction would be
            # a plausible-looking lie, so the flow simply is not drawn.
            msg = "%s IS OFF THIS MAP" % state["current"]["station"]
            blit_text(static, lay.map_y + 2,
                      max(0, (w - text_width(msg)) // 2), msg, C_WARN)

    cell["on_map"] = on_map
    band_t, band_b = curve_band(lay)
    streak_cols = {}

    def render(t, i):
        now = now_of()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)
        state = sample(now)

        if cell["tide"] is None and cell["current"] is None:
            lines = [("NO TIDE DATA", C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --LOOP 900", C_TEXT)]
            for p in cell["problems"][:2]:
                lines.append((p.upper()[:52], C_DIM))
            return draw_nodata(frame, lay, lines)

        t0, t1 = curve_window(now, args.span, args.anchor)
        bucket = arrow_bucket(state)
        key = (int(t0), state["tide"] is not None, state["current"] is not None,
               int(now // 300), bucket)
        if key != cell["static_key"]:
            cell["window"] = (t0, t1)
            rebuild_static(now, state, bucket)
            cell["static_key"] = key
        # The status line changes about once a minute; formatting it to find
        # out costs a third of a millisecond, so it is asked twice a minute.
        if int(now // 20) != cell["head_at"]:
            cell["head_at"] = int(now // 20)
            hk = header_text(state, args.metric, args.h24, w)
            if hk != cell["head_key"]:
                draw_header(header, lay, state, args.metric, args.h24)
                cell["head_key"] = hk

        frame[:] = static
        frame[:lay.head_h] = header

        v = state["v"]
        speed = abs(v)
        reg = frame[lay.map_y:lay.map_y + lay.map_rows]

        if state["current"] is not None and cell["on_map"]:
            # Particles. dt is the *displayed* elapsed time, so --rate speeds
            # the water up along with everything else.
            dt = min(0.2, (1.0 / max(args.fps, 1)) * args.rate)
            sgn = 1.0 if v >= 0 else -1.0
            np.clip(pos, 0.0, bounds, out=clipped)
            np.copyto(ipos, clipped, casting="unsafe")
            flat = ipos[0] * w
            flat += ipos[1]
            step = dt * args.flow_gain * speed * sgn
            # np.add(..., out=) rather than `+=`: an augmented assignment to a
            # name from the enclosing scope makes it local to render(), which
            # is a UnboundLocalError three lines earlier and not an obvious one.
            np.add(pos, field[:, flat] * step, out=pos)
            np.add(page, dt, out=page)
            dead = (page > plife) | ~sea_flat[flat]
            nd = int(dead.sum())
            if nd:
                pick = water_idx[rng.integers(0, len(water_idx), nd)]
                pos[0, dead] = (pick // w).astype(f32)
                pos[1, dead] = (pick % w).astype(f32)
                page[dead] = 0.0

            if speed > 0.02:
                # A three-sample streak, drawn tail first. Single lit pixels on
                # a dark map read as stars, not as water; a short comet with a
                # bright head reads as a direction even in a still frame.
                # Both the brightness and the *number* fall away with the
                # tide. Dimming alone leaves a full field of faint dots at
                # slack, which still reads as a texture; thinning them out is
                # what makes the water go quiet.
                # Floors, not zeroes. Slack should look like slack, but a map
                # with nothing at all on it looks like a demo that has crashed
                # rather than like water that has stopped, so a thin shimmer
                # stays behind.
                fade = min(1.0, max(0.24, speed / 2.0))
                shown = max(24, min(npart, int(npart * (0.2 + speed / 1.5))))
                # Quantised, so the three colour tuples come out of a dict
                # instead of being multiplied out twelve times a frame.
                ckey = (v > 0, int(fade * 12))
                cols = streak_cols.get(ckey)
                if cols is None:
                    b = C_FLOOD if v > 0 else C_EBB
                    cols = streak_cols[ckey] = [
                        tuple(int(c * kk * fade) for c in b)
                        for kk in (0.24, 0.52, 1.0)]
                mv = reg.reshape(-1, 3)
                np.multiply(heading[:, flat], -sgn, out=hbuf)
                np.multiply(hbuf, STREAK, out=streak)
                np.add(streak, pos, out=streak)
                np.clip(streak, 0.0, bounds, out=streak)
                si = streak.astype(np.int32)
                sf = si[:, 0] * w
                sf += si[:, 1]
                sf = sf[:, :shown]
                ok = sea_flat[sf]
                for j in (0, 1, 2):
                    mv[sf[j][ok[j]]] = cols[j]

        # The marker last, over everything, so it is never hidden by a barb.
        if state["tide"] is not None and cell["band"] is not None:
            col = int(min(w - 1, max(0, (now - t0) / (t1 - t0) * w)))
            band = frame[lay.curve_y:lay.curve_y + lay.curve_h, col]
            np.maximum(band, MARK_GUIDE, out=band)
            lo, hi = cell["band"]
            r = int(round(band_b - (state["h"] - lo) / (hi - lo)
                          * (band_b - band_t)))
            r = min(lay.curve_y + lay.curve_h - 1, max(lay.curve_y, r))
            blink = 0.72 + 0.28 * math.sin(now * 2.2)
            frame[max(lay.curve_y, r - 2):r + 3, max(0, col - 1):col + 2] = \
                tuple(int(c * blink * 0.45) for c in C_MARK)
            frame[max(lay.curve_y, r - 1):r + 2, col] = \
                tuple(int(c * blink) for c in C_MARK)
        return frame

    reload_data(now_of())
    render.state = cell               # tests reach in here; nothing else does
    render.particles = pos
    render.layout = lay
    render.flow = (u_m, v_m, sea_solve, flood_bearing, (sr, sc))
    render.sea_map = sea_map
    render.clock = now_of
    return render


def _band_range(hv):
    lo, hi = float(hv.min()), float(hv.max())
    if hi - lo < 0.5:
        hi = lo + 0.5
    pad = (hi - lo) * 0.12
    return lo - pad, hi + pad


def main():
    ds.standalone(sys.modules[__name__],
                  "live tide curve and a flow map of the Golden Gate corridor",
                  fps=30)


if __name__ == "__main__":
    main()
