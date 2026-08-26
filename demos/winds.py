#!/usr/bin/env python3
"""The wind over San Francisco Bay, drawn the way Windy draws it.

A coastline, and a few hundred particles blown across it by the actual
forecast wind field: streaks that lengthen and warm as the air speeds up, and
shorten to slow-moving embers where it goes calm. A speed scale along the
bottom and the numbers at the Golden Gate along the top, because a wind map
with no scale is decoration.

**The story this panel exists to tell is the sea-breeze jet.** The coast range
runs unbroken from Bodega to Big Sur except in one place, and that place is
the Golden Gate: a sea-level gap about three kilometres wide with a
metropolitan bay behind it. On a summer afternoon the Central Valley heats,
the pressure gradient tips inland, and the marine layer accelerates through
that gap and fans out over the water — twenty-five knots at the bridge, ten in
Berkeley, and a sailing club's entire schedule built around the difference. It
runs roughly east-west, which is the one thing this bay does that suits a
panel five times wider than it is tall.

**The extent, and what it costs.** 37.74–37.90 N, 122.28–122.68 W: nine
kilometres of open Pacific on the left, the Gate dead centre, the Bay and the
East Bay shoreline on the right. 35.2 km by 17.8 km, drawn across 320 columns
and 52 rows, so a column is 110 m and a row is 342 m and the picture is
stretched about **three times horizontally** — the same deal `tide.py` makes
and for the same reason. It is stated rather than hidden: at true scale a
strip 35 km wide fitted to 52 rows would be 5.7 km tall and would cut both the
ocean and the East Bay off the ends, leaving a slot through the Gate with no
gradient in it at all, which is the one thing the map is for.

The east edge is not a choice: `voxel-dem.npz` stops at 122.28 W, so downtown
Oakland is a few hundred metres off the right-hand side and the whole Delta
outflow the jet eventually feeds is out of frame. North and south are choices:
San Pablo Bay and the South Bay are both outside, and the northward half of
the fan-out past Angel Island is clipped. What the width buys is the whole
length of the jet in one picture, and the jet does not run the way you would
guess: the *fastest* air is not out over the ocean, it is **in the gap**,
where the coast range has squeezed it, and it gives most of that back within
ten kilometres. One real evening off this panel, six o'clock on a August
Sunday: 12.6 kt ten kilometres offshore, 17.9 at the bridge, 13.8 over
Alcatraz, 6.5 at the Bay Bridge, 4.9 off Berkeley. A crop that stopped at the
Gate would show the 17.9 and none of the rest of that sentence.

**The field is an interpolation of a coarse model grid. It is not a
simulation.** Open-Meteo answers a 7x11 grid of coordinates in one request;
the API snaps each to its model cell and says where it landed, and the cells
come back about 3 km apart, which is NOAA's HRRR. Seventy-seven points over an
area this size is a real mesoscale model at its real resolution — it does know
about the gap — but between those points there is nothing here but inverse
distance weighting on a smoothing length of about one grid cell. So the panel
can show you that the Gate is blowing and Berkeley is not. It cannot show you
the wind shadow behind Angel Island, the lift off Yellow Bluff, or the
convergence line that parks itself off Crissy Field on a good day, because
none of those are in the numbers it was given. Nothing here solves anything;
the coastline is drawn on top of the field, not into it.

**Direction is the bug that would look fine.** Meteorological wind direction
is the bearing the wind is coming **from** — 270° is a westerly, blowing
*towards* the east — so the velocity is

    u_east  = -speed * sin(dir)
    v_north = -speed * cos(dir)

and a field drawn without those two minus signs runs backwards, is entirely
plausible on a wall, and is entirely wrong. `scripts/test-winds.py` asserts it
against the fetched JSON and, separately, off the rendered pixels: it
cross-correlates consecutive frames in a window around a station and checks
that the dominant motion of the *picture* points where the bearing says it
should.

**Time: it animates the forecast, and says so in words.** Standing still on
the current hour throws away the best thing in the data, which is that the sea
breeze has a daily cycle you can watch: dead calm at dawn, filling in after
noon, howling at six, easing overnight. So the panel sweeps from now through
the next day and loops, one model hour at a time — no invented in-between
frames, each field is a real forecast hour. The first frame of each sweep is
*now*, interpolated between the two model hours either side of it, and it is
labelled `NOW`; every other frame is labelled `FCST +7H 2:00A` in amber. A
forecast that could be mistaken for an observation would be worse than no
panel. `--hours 0` pins it to now if you want a fixed picture.

**Data comes from the cache, never from the network.** `ftdata.py` fetches on
a timer in a process of its own; this demo reads the file it leaves behind and
does not import a HTTP library, because the scheduler builds the next segment
on a worker thread and a `build()` blocked on a socket stops the render loop
getting the interpreter back. Run the fetcher first:

    $ python3 ftdata.py --loop 900

And nothing here believes a file just because it parsed. A forecast keeps
telling the truth for a while, so the test is both the age of the fetch and
whether the payload's hours still cover now; if either fails badly enough
there is a card naming the command that fixes it, and no wind is drawn.
Yesterday's sea breeze on today's wall is confidently wrong.

The font, the DEM crop, the sea mask and the clock are `tide.py`'s, imported
rather than copied — the same way `propagation.py` borrows `defcon.py`'s
glyphs. That is deliberate beyond saving lines: the two demos are looking at
the same bay, and if they ever disagreed about where the coast is, one of them
would be lying.
"""

import math
import sys

import numpy as np

import demoscene as ds
import ftdata
import tide

f32 = np.float32

# tide.py's helpers, reused wholesale. See the docstring.
blit_text = tide.blit_text
text_mask = tide.text_mask
text_width = tide.text_width
clock = tide.clock
parse_when = tide.parse_when
hhmm = tide.hhmm
cell_of = tide.cell_of
crop_sea = tide.crop_sea
load_sea = tide.load_sea
extent_metres = tide.extent_metres
parse_extent = tide.parse_extent

# lat0, lat1, lon0, lon1. Ocean on the left, the Gate in the middle, the East
# Bay shoreline hard against the DEM's eastern edge. See the docstring.
EXTENT = (37.74, 37.90, -122.68, -122.28)

# Mid-span, Golden Gate Bridge: the reference point the header quotes, and the
# place the whole panel is about.
GATE = (37.8199, -122.4783)

KNOT_MS = 0.5144

# Knots, and what each is worth in the units the panel can show. Knots are the
# default because this is a bay and everyone who cares about the answer here
# owns a boat or a board.
UNITS = {"kn": (1.0, "KT"), "mph": (1.15078, "MPH"),
         "ms": (KNOT_MS, "M/S"), "kmh": (1.852, "KM/H")}

# The map, in tide.py's colours so the two agree about where the land is.
C_SEA = (4, 11, 20)
C_LAND = (30, 32, 38)
C_SHORE = (128, 140, 156)
C_BRIDGE_GG = (255, 92, 26)
C_BRIDGE_BAY = (120, 122, 132)
C_TEXT = (196, 214, 228)
C_DIM = (96, 110, 124)
C_WARN = (255, 96, 72)
C_NOW = (150, 255, 190)
C_FCST = (255, 186, 70)
C_RULE = (18, 22, 28)

# The speed ramp. The bottom of it is the part that takes thought: a scale
# that fades to black at zero makes calm air *invisible*, which on this bay is
# not nothing — it is the lee of the hills, and the reason one side of the
# panel is worth looking at is that the other side is quiet. So zero is a
# dim slate blue that still reads on the near-black water, and the ramp climbs
# through teal and green to amber and red rather than through brightness
# alone, so the picture survives being looked at from across a workshop.
SPEED_RAMP = [
    (0.00, (62, 76, 112)), (0.13, (48, 128, 172)), (0.28, (44, 198, 178)),
    (0.43, (104, 228, 116)), (0.58, (212, 226, 84)), (0.72, (255, 176, 58)),
    (0.86, (255, 102, 72)), (1.00, (255, 116, 196)),
]

# How much of the ramp is washed into the background. Enough that the gradient
# is there when you glance at a still frame, little enough that the particles
# are still the brightest thing moving.
WASH = 0.13

# Streak: four samples, brightest at the head, spaced by a fixed slice of
# *render* time. Because the spacing is a time and the field is a velocity,
# the streak is long where the wind is strong and collapses to a single lit
# pixel where it is calm, for free and without a branch.
STREAK_DT = 0.12
STREAK_BRIGHT = (0.20, 0.42, 0.68, 1.0)


# --------------------------------------------------------------------------
# Reading what ftdata left behind. No network, and no faith in the file.
# --------------------------------------------------------------------------

def read_wind(cache_dir):
    """Return (record, age, error). Any of the three may be None."""
    got = ftdata.load("wind-bay", cache_dir)
    if got is None:
        return None, None, "no cached wind grid"
    payload, age = got
    try:
        grid = payload["grid"]
        t0 = float(payload["t0"])
        step = float(payload["step"]) or 3600.0
        n = int(payload["n"])
        lat = np.array([float(g["lat"]) for g in grid], np.float64)
        lon = np.array([float(g["lon"]) for g in grid], np.float64)

        def column(key):
            out = np.full((len(grid), n), np.nan, f32)
            for i, g in enumerate(grid):
                v = g.get(key) or []
                for j, x in enumerate(v[:n]):
                    if x is not None:
                        out[i, j] = float(x)
            return out

        spd, drc, gst = column("speed"), column("dir"), column("gust")
    except Exception:                                        # noqa: BLE001
        # Missing keys, a payload from another product, a grid of strings.
        return None, age, "wind record is malformed"
    if n < 2 or len(grid) < 3:
        return None, age, "wind record has too little in it"

    # A station the model declined to answer for is dropped entirely; one with
    # a hole in the middle keeps its good hours and is weighted out of the bad
    # ones. Both are honest; silently interpolating across either is not.
    keep = np.isfinite(spd).any(axis=1) & np.isfinite(drc).any(axis=1)
    dropped = int(len(grid) - keep.sum())
    if int(keep.sum()) < 3:
        return None, age, "wind record has no usable stations"
    lat, lon = lat[keep], lon[keep]
    spd, drc, gst = spd[keep], drc[keep], gst[keep]

    # THE minus signs. Meteorological direction is where the wind comes FROM.
    th = np.radians(drc.astype(f32))
    u = (-spd * np.sin(th)).astype(f32)          # knots east
    v = (-spd * np.cos(th)).astype(f32)          # knots north
    return ({"lat": lat, "lon": lon, "u": u, "v": v, "spd": spd, "gust": gst,
             "t0": t0, "step": step, "n": n, "age": age, "dropped": dropped,
             "model": str(payload.get("model", "")),
             "span": (t0, t0 + step * (n - 1))}, age, None)


def covers(rec, t):
    return rec["span"][0] <= t <= rec["span"][1]


# --------------------------------------------------------------------------
# Interpolation.
#
# Inverse distance weighting, 1/(d^2 + e^2)^p. It is the cheapest thing that is
# smooth, close to exact at the data points, and monotone between them;
# kriging would be defensible and a Pi 3 at 600 MHz would not thank anyone for
# it. The weights depend only on geometry, so they are built once and every
# forecast hour is then a matrix-vector product on 77 numbers.
#
# e = 1.2 km and p = 1.5 were not guessed. Measured against the fetched grid
# itself -- interpolate, then read the field back at each station's own
# coordinates -- the softer settings this started with (e = 2.4 km, p = 1)
# came back 2.4 kt RMS low and turned a 20 kt jet at the Gate into a 14 kt
# one. Smoothing away the very peak the panel exists to show is not a
# cosmetic failure, it is the map lying about the number. These settings
# reproduce the stations to 0.4 kt RMS and still fall off smoothly between
# them; `scripts/test-winds.py` keeps checking both ends of that.
#
# The east and north components are interpolated separately, never the speed
# and the bearing: the mean of 350 and 10 degrees is 180, which is the wrong
# way up the map, and averaging speeds while averaging directions separately
# would invent air moving faster than anything in the model.
#
# It runs on a coarse lattice -- 48 x 12 over the crop, so a cell is about
# 730 x 1480 m -- and the panel-resolution field is a bilinear upsample of
# that. Doing the weighting at 320 x 52 directly would be a 16 640 x 77 matrix
# and forty times the arithmetic for a field whose shortest real wavelength is
# 3 km.
# --------------------------------------------------------------------------

def idw_weights(extent, lat, lon, ch, cw, smooth_km=1.2, power=1.5):
    """(ch*cw, nstations) float32, rows summing to one."""
    la0, la1, lo0, lo1 = extent
    mlat, mlon = tide.metres_per_degree(0.5 * (la0 + la1))
    gy = la1 - (np.arange(ch, dtype=np.float64) + 0.5) / ch * (la1 - la0)
    gx = lo0 + (np.arange(cw, dtype=np.float64) + 0.5) / cw * (lo1 - lo0)
    dy = (gy[:, None] - lat[None, :]) * mlat / 1000.0
    dx = (gx[:, None] - lon[None, :]) * mlon / 1000.0
    d2 = (dy[:, None, :] ** 2 + dx[None, :, :] ** 2).reshape(ch * cw, -1)
    w = 1.0 / np.power(d2 + smooth_km * smooth_km, power)
    return (w / w.sum(axis=1, keepdims=True)).astype(f32)


def apply_weights(w, values):
    """w @ values, but weighted around any NaN in `values`.

    A hole in one station for one hour is common enough -- the model does not
    always have a 10 m wind everywhere -- and `w @ nan` poisons the entire
    field. Renormalising over the finite stations is the same interpolation
    with that station simply not present, which is what it is.
    """
    ok = np.isfinite(values)
    if ok.all():
        return w.dot(values)
    good = np.where(ok, values, 0.0).astype(f32)
    norm = w.dot(ok.astype(f32))
    return w.dot(good) / np.maximum(norm, 1e-6)


def upsampler(n_out, n_in):
    """(i0, i1, frac) for a bilinear resample of `n_in` samples to `n_out`."""
    x = np.clip((np.arange(n_out) + 0.5) / n_out * n_in - 0.5, 0.0, n_in - 1.0)
    i0 = np.floor(x).astype(np.int32)
    i1 = np.minimum(i0 + 1, n_in - 1)
    return i0, i1, (x - i0).astype(f32)


def upsample(c, rows, cols):
    """Bilinear (ch, cw) -> (len(rows[0]), len(cols[0])) float32."""
    r0, r1, rf = rows
    c0, c1, cf = cols
    a = c[r0] * (1.0 - rf)[:, None] + c[r1] * rf[:, None]
    return a[:, c0] * (1.0 - cf)[None, :] + a[:, c1] * cf[None, :]


def nice_step(span, want=4):
    """A round tick interval giving roughly `want` intervals across `span`."""
    raw = max(span, 1e-6) / max(want, 1)
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= m * mag:
            return m * mag
    return 10.0 * mag


def compass(deg):
    """A bearing as one of sixteen points. 'W', 'WNW', 'NNE'."""
    names = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return names[int((deg % 360.0) / 22.5 + 0.5) % 16]


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--extent", default=",".join("%g" % v for v in EXTENT),
                    help="map crop as lat0,lat1,lon0,lon1")
    ap.add_argument("--units", default="kn", choices=sorted(UNITS),
                    help="speed units on the scale and in the numbers")
    ap.add_argument("--speed-max", type=float, default=30.0,
                    help="knots at the top of the colour ramp")
    ap.add_argument("--hours", type=float, default=24.0,
                    help="forecast hours to sweep through (0 = show now only)")
    ap.add_argument("--cycle", type=float, default=52.0,
                    help="seconds for one sweep from now to the last hour")
    ap.add_argument("--hour", type=float, default=-1.0,
                    help="pin to this many hours ahead instead of animating")
    ap.add_argument("--particles", type=int, default=380,
                    help="drifters on the map")
    ap.add_argument("--gain", type=float, default=320.0,
                    help="how much faster than real time the air moves; at 1 "
                         "a 20 kt wind crosses the panel in about an hour")
    ap.add_argument("--point", default="%.4f,%.4f" % GATE,
                    help="lat,lon the header quotes conditions for")
    ap.add_argument("--map-rows", type=int, default=0,
                    help="rows given to the map (0 = whatever is left over)")
    ap.add_argument("--coarse", type=int, default=48,
                    help="columns in the interpolation lattice")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second")
    ap.add_argument("--reload", type=float, default=900.0,
                    help="seconds between re-reads of the cache (0 = never)")


def parse_point(s):
    a, b = [float(x) for x in s.replace(" ", "").split(",")]
    return a, b


# --------------------------------------------------------------------------
# Layout.
# --------------------------------------------------------------------------

class Layout(object):
    """Two thin strips of chrome and everything else to the map.

    Six rows of header and six of scale is a fifth of a 64-row panel, which is
    a lot to give away; it buys the two things a map like this is useless
    without, which are a number and a legend. Both fold away on a short panel
    rather than shrinking into illegibility.
    """

    def __init__(self, w, h, map_rows=0):
        self.w, self.h = w, h
        self.head_h = 6 if h >= 30 else 0
        self.leg_h = 6 if h >= 42 and w >= 160 else 0
        self.map_y = self.head_h
        avail = h - self.head_h - self.leg_h
        self.map_rows = max(4, min(avail, map_rows) if map_rows > 0 else avail)
        self.leg_y = h - self.leg_h


def draw_base(reg, sea, extent):
    """Land, water, a lit shoreline and the two bridges."""
    reg[sea] = C_SEA
    reg[~sea] = C_LAND
    reg[tide._dilate(sea) & ~sea] = C_SHORE

    rows, cols = reg.shape[:2]

    def span(lat_a, lon_a, lat_b, lon_b, rgb):
        r0, c0 = cell_of(extent, (rows, cols), lat_a, lon_a)
        r1, c1 = cell_of(extent, (rows, cols), lat_b, lon_b)
        n = max(abs(r1 - r0), abs(c1 - c0), 1)
        rr = np.round(np.linspace(r0, r1, n + 1)).astype(int)
        cc = np.round(np.linspace(c0, c1, n + 1)).astype(int)
        ok = (rr >= 0) & (rr < rows) & (cc >= 0) & (cc < cols)
        reg[rr[ok], cc[ok]] = rgb

    # Two lines do more for recognising this place than any amount of
    # coastline, and the Gate one is the gap the whole panel is about.
    span(37.8060, -122.4783, 37.8327, -122.4784, C_BRIDGE_GG)
    span(37.7908, -122.3877, 37.8078, -122.3607, C_BRIDGE_BAY)


def draw_scale(dst, y, x0, pal_u8, smax, unit, rows=5):
    """The speed ramp as labelled segments: 0 [ramp] 10 [ramp] 20 ... KT.

    A conventional bar with the numbers underneath wants eight rows and there
    are five. Putting the numbers *in line* with the colour, so each stretch of
    ramp is bracketed by the two speeds it spans, says the same thing in five
    rows and reads better at a glance than tick marks do.
    """
    scale, name = UNITS[unit]
    top = smax * scale
    step = nice_step(top, 4)
    ticks = []
    v = 0.0
    while v < top - 0.35 * step:
        ticks.append(v)
        v += step
    ticks.append(top)

    bar_t, bar_b = y + 1, y + rows - 1
    x = x0
    for i, tv in enumerate(ticks):
        lab = "%g" % round(tv)
        if i == len(ticks) - 1:
            lab += "+"
        blit_text(dst, y, x, lab, C_DIM if i else C_TEXT)
        x += text_width(lab) + 2
        if i == len(ticks) - 1:
            break
        seg = 22
        if x + seg > dst.shape[1]:
            break
        f = np.linspace(tv / scale, ticks[i + 1] / scale, seg, dtype=f32) / smax
        idx = np.clip(f * 255.0, 0, 255).astype(np.uint8)
        dst[bar_t:bar_b, x:x + seg] = pal_u8[idx][None, :, :]
        x += seg + 2
    x += 1
    blit_text(dst, y, x, name, C_DIM)
    return x + text_width(name)


def fit_right(dst, y, x_from, ladder, rgb):
    """Right-align the widest of `ladder` that fits after x_from."""
    for s in ladder:
        wdt = text_width(s)
        if s and x_from + 4 + wdt <= dst.shape[1]:
            blit_text(dst, y, dst.shape[1] - wdt - 1, s, rgb)
            return
    return


def header_line(cond, unit, h24):
    """Left, centre and right of the status line, as (text, colour) pairs.

    Three ladders of shorter forms; the widest set that fits is what gets
    drawn. Clipping instead would lose the right-hand end, and the right-hand
    end is the part that says how old the data is.
    """
    scale, name = UNITS[unit]
    spd = cond["speed"] * scale
    gst = cond["gust"] * scale
    deg = cond["dir"]
    pt = cond["label"]
    lefts = ["%s %s%03d %.0f%s G%.0f" % (pt, compass(deg), deg, spd, name, gst),
             "%s %s%03d %.0f%s" % (pt, compass(deg), deg, spd, name),
             "%s%03d %.0f" % (compass(deg), deg, spd)]
    if not np.isfinite(cond["gust"]):
        lefts = lefts[1:]
    return lefts


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    extent = parse_extent(args.extent)
    lay = Layout(w, h, args.map_rows)
    cache = args.cache_dir
    now_of = clock(parse_when(args.at), args.rate)
    smax = max(1.0, float(args.speed_max))

    pal_f = ds.gradient(SPEED_RAMP, 256, dtype=f32)
    pal_u8 = pal_f.astype(np.uint8)
    # Three dimmed copies of the ramp plus the full one, one per streak
    # sample. These are only used to bake the per-hour colour table below.
    pal_lv = np.stack([(pal_f * k).astype(np.uint8) for k in STREAK_BRIGHT])

    sea_full, bbox = load_sea()
    sea_map = crop_sea(sea_full, bbox, extent, w, lay.map_rows)
    wm, hm = extent_metres(extent)

    base = np.zeros((lay.map_rows, w, 3), np.uint8)
    draw_base(base, sea_map, extent)
    base_f = base.astype(f32)
    # The wash is dimmed over land. The wind is over the land too and the
    # gradient there is half the story, so it is not switched off -- but at
    # 342 m a row the coastline is a one-pixel line, and if the fill either
    # side of it is the same brightness the map stops reading as this bay and
    # starts reading as a green rectangle. Dimming the land is what makes the
    # shoreline visible without turning the shoreline itself into a fence.
    wash_gain = np.where(sea_map, WASH, WASH * 0.55).astype(f32)[:, :, None]
    shore = tide._dilate(sea_map) & ~sea_map
    # The coastline is redrawn over the particles every frame. It is the only
    # thing on the panel that says *where* any of this is, and a drifter
    # sitting on top of Angel Island for four frames is enough to lose it. As
    # a list of flat indices this is one scatter of about fifteen hundred
    # pixels -- a single numpy call, which on this machine is the unit of cost.
    shore_idx = np.flatnonzero(shore.reshape(-1)).astype(np.int32)

    # Metres of map per pixel, and thence knots to pixels per second. The
    # horizontal stretch lives here and nowhere else: a north wind moves about
    # three times fewer pixels per second than an east wind of the same speed,
    # which is not a bug, it is what an affine squash of the geography does and
    # it is why the streaks stay geometrically honest.
    px_e = (w / wm) * KNOT_MS * args.gain
    px_n = (lay.map_rows / hm) * KNOT_MS * args.gain

    ch = max(4, int(round(args.coarse * (hm / wm))))
    cw = max(8, int(args.coarse))
    rows_up = upsampler(lay.map_rows, ch)
    cols_up = upsampler(w, cw)
    # The header quotes one place, and it has to be a place that is on the
    # map -- a number labelled GATE taken from the nearest cell inside a crop
    # that does not contain the Gate would be a quiet lie. If --point lands
    # outside, the middle of the crop is quoted instead and says so.
    gate = parse_point(args.point)
    on_map = (extent[0] <= gate[0] <= extent[1]
              and extent[2] <= gate[1] <= extent[3])
    # Named only when it really is the Gate; %g in the default string used
    # to round the longitude and quietly relabel the Gate as "PT".
    point_label = "GATE" if (abs(gate[0] - GATE[0]) < 5e-4
                             and abs(gate[1] - GATE[1]) < 5e-4) else "PT"
    if not on_map:
        gate = (0.5 * (extent[0] + extent[1]), 0.5 * (extent[2] + extent[3]))
        point_label = "MID"
    gr, gc = cell_of(extent, (ch, cw), *gate)
    gr = int(np.clip(gr, 0, ch - 1))
    gc = int(np.clip(gc, 0, cw - 1))

    npart = max(1, int(args.particles))
    rng = np.random.default_rng(0x5EA82EE2)

    # Particle state, row-then-column in one (2, N) array: every per-particle
    # operation is then one numpy call rather than two, and on a 600 MHz Pi
    # the call is most of the cost whatever size it is.
    pos = np.empty((2, npart), f32)
    bounds = np.array([[lay.map_rows - 1.0], [w - 1.0]], f32)
    page = rng.random(npart, dtype=f32) * 5.0
    plife = 2.5 + rng.random(npart, dtype=f32) * 4.5
    ipos = np.empty((2, npart), np.int32)
    clipped = np.empty((2, npart), f32)
    streak = np.empty((len(STREAK_BRIGHT), 2, npart), f32)
    STREAK_K = (np.arange(len(STREAK_BRIGHT), dtype=f32)[::-1]
                * STREAK_DT).reshape(-1, 1, 1)

    frame = np.zeros((h, w, 3), np.uint8)
    legend = np.zeros((max(1, lay.leg_h), w, 3), np.uint8)
    header = np.zeros((max(1, lay.head_h), w, 3), np.uint8)

    cell = {"rec": None, "err": None, "loaded": -1e18, "hours": [],
            "key": None, "cur": -1, "age_key": None, "pending": None,
            "stale": False, "sig": None}

    def respawn(mask, n):
        pos[0, mask] = rng.random(n, dtype=f32) * (lay.map_rows - 1.0)
        pos[1, mask] = rng.random(n, dtype=f32) * (w - 1.0)
        page[mask] = 0.0

    respawn(np.ones(npart, bool), npart)

    # ---------------------------------------------------------------- baking

    def station_at(rec, t):
        """Station u, v, speed, gust linearly interpolated to a moment."""
        x = (t - rec["t0"]) / rec["step"]
        i = int(np.clip(math.floor(x), 0, rec["n"] - 2))
        f = f32(np.clip(x - i, 0.0, 1.0))
        out = []
        for key in ("u", "v", "spd", "gust"):
            a = rec[key]
            out.append(a[:, i] * (1.0 - f) + a[:, i + 1] * f)
        return out

    def moments(rec, now):
        """(time, hours_ahead, is_now) for every frame of the sweep.

        Frame zero is now, interpolated between the model hours either side of
        it. Every other frame is a model hour exactly as published -- no
        invented in-between fields, so what is on the wall is a forecast that
        exists rather than a smoothing of two that do.
        """
        out = [(now, 0.0, True)]
        if args.hours > 0:
            end = now + args.hours * 3600.0
            k = int(math.floor((now - rec["t0"]) / rec["step"])) + 1
            while k < rec["n"]:
                t = rec["t0"] + k * rec["step"]
                if t > end:
                    break
                if t > now:
                    out.append((t, (t - now) / 3600.0, False))
                k += 1
        return out

    def bake(rec, wts, t):
        """One forecast moment: the field, the wash, and the header numbers."""
        u_st, v_st, s_st, g_st = station_at(rec, t)
        uc = apply_weights(wts, u_st).reshape(ch, cw)
        vc = apply_weights(wts, v_st).reshape(ch, cw)
        gc_ = apply_weights(wts, g_st).reshape(ch, cw)

        ue = upsample(uc, rows_up, cols_up)          # knots east
        vn = upsample(vc, rows_up, cols_up)          # knots north
        spd = np.hypot(ue, vn)
        idx = np.clip(spd * (255.0 / smax), 0, 255).astype(np.uint8)

        # Pixels per second, row-then-column, and north is up so it is minus.
        field = np.stack([(-vn * px_n).reshape(-1),
                          (ue * px_e).reshape(-1)]).astype(f32)

        flat_idx = idx.reshape(-1)
        # Every pixel's colour at every streak brightness, baked. Looking it
        # up at run time is `pal[level][speed_index[pixel]]`, which is two
        # gathers per level per frame; a gather is three times the price of a
        # whole-array pass on this machine and the frame loop does four of
        # them. Precomputed it is one gather. 200 kB an hour, 5 MB for the
        # day, and about a millisecond a frame.
        colour = pal_lv[:, flat_idx]
        wash = base_f + (pal_f[flat_idx].reshape(lay.map_rows, w, 3)
                         * wash_gain)
        static = np.clip(wash, 0, 255).astype(np.uint8)
        static[shore] = C_SHORE          # the coastline stays a coastline

        gu, gv = float(uc[gr, gc]), float(vc[gr, gc])
        cond = {"speed": math.hypot(gu, gv),
                # Back to a FROM bearing, which is what a wind report is.
                "dir": math.degrees(math.atan2(-gu, -gv)) % 360.0,
                "gust": float(gc_[gr, gc]), "label": point_label}
        return {"field": field, "idx": flat_idx, "colour": colour,
                "static": static, "cond": cond}

    def hour_label(t, ahead, is_now):
        if is_now:
            return "NOW " + hhmm(t, not args.h24), C_NOW
        return "FCST +%dH %s" % (round(ahead), hhmm(t, not args.h24)), C_FCST

    def draw_header(mo, cond, stale, age):
        header[:] = 0
        if not lay.head_h:
            return
        t, ahead, is_now = mo
        left = header_line(cond, args.units, args.h24)
        mid, midc = hour_label(t, ahead, is_now)
        right = ("STALE " + age) if stale else (("DATA " + age) if age else "")
        rights = [right, age if not stale else "STALE", ""]

        # Widest left that leaves room for the centre and the right.
        rw = max((text_width(s) for s in rights if s), default=0)
        chosen = left[-1]
        for s in left:
            if text_width(s) + text_width(mid) + rw + 14 <= w:
                chosen = s
                break
        blit_text(header, 0, 1, chosen, C_TEXT)
        lw = text_width(chosen)
        mw = text_width(mid)
        mx = min(w - rw - 5 - mw, max(lw + 6, (w - mw) // 2))
        if mx > lw + 2:
            blit_text(header, 0, mx, mid, midc)
        fit_right(header, 0, lw + mw + 8, rights,
                  C_WARN if stale else C_DIM)
        header[lay.head_h - 1] = C_RULE

    def draw_legend():
        legend[:] = 0
        if not lay.leg_h:
            return
        legend[0] = C_RULE
        end = draw_scale(legend, 1, 2, pal_u8, smax, args.units, lay.leg_h - 1)
        # The one sentence that keeps the panel honest, and the one that keeps
        # the arrows readable: where the numbers came from, that they were
        # interpolated, and that a bearing is where the wind is *from*.
        fit_right(legend, 1, end, [
            "OPEN-METEO HRRR 3KM INTERPOLATED  DIR FROM",
            "HRRR 3KM INTERPOLATED  DIR FROM",
            "HRRR INTERPOLATED  DIR FROM",
            "HRRR INTERPOLATED", "INTERPOLATED"], C_DIM)

    def nodata(lines):
        frame[:] = (6, 6, 8)
        sc = 2 if h >= 32 and w >= 200 else 1
        y = max(0, h // 2 - (len(lines) * (6 * sc + 2)) // 2)
        for i, (s, rgb) in enumerate(lines):
            k = sc if i == 0 else 1
            blit_text(frame, y, max(0, (w - text_width(s, k)) // 2), s, rgb, k)
            y += 5 * k + 3
        return frame

    # ------------------------------------------------------------- (re)load

    def reload_data(now):
        rec, age, err = read_wind(cache)
        cell["age"] = age
        if rec is not None and not covers(rec, now):
            err = "wind forecast no longer covers now"
            rec = None
        cell["loaded"] = now
        if rec is None:
            cell["rec"], cell["err"], cell["hours"] = None, err, []
            cell["sig"] = None
            cell["key"] = None
            return
        cell["err"] = err
        cell["stale"] = not ftdata.is_fresh("wind-bay", rec["age"])
        sig = (round(rec["t0"]), rec["n"], len(rec["lat"]), int(now // 1800))
        if sig == cell["sig"] and cell["hours"]:
            cell["rec"] = rec
            return                      # same data, same hours: nothing to bake
        cell["sig"] = sig
        wts = idw_weights(extent, rec["lat"], rec["lon"], ch, cw)
        mos = moments(rec, now)
        if cell["hours"]:
            # Re-baking two dozen fields is a third of a second, which is four
            # dropped frames if it happens inside render(). So it happens one
            # moment per frame into a shadow list, and the old fields stay on
            # the wall until the new ones are all there.
            cell["pending"] = {"rec": rec, "wts": wts, "mos": mos,
                               "out": [], "i": 0}
        else:
            cell["rec"] = rec
            cell["mos"] = mos
            cell["hours"] = [bake(rec, wts, t) for t, _, _ in mos]
            cell["key"] = None

    def pump(pend):
        p = pend
        p["out"].append(bake(p["rec"], p["wts"], p["mos"][p["i"]][0]))
        p["i"] += 1
        if p["i"] >= len(p["mos"]):
            cell["rec"], cell["mos"], cell["hours"] = p["rec"], p["mos"], p["out"]
            cell["pending"] = None
            cell["key"] = None

    # ---------------------------------------------------------------- render

    def render(t, i):
        now = now_of()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)
        if cell["pending"] is not None:
            pump(cell["pending"])

        if not cell["hours"]:
            lines = [("NO WIND DATA", C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --LOOP 900", C_TEXT)]
            if cell["err"]:
                lines.append((cell["err"].upper()[:52], C_DIM))
            return nodata(lines)

        n = len(cell["hours"])
        if args.hour >= 0 or n == 1:
            k = int(np.clip(round(args.hour), 0, n - 1)) if n > 1 else 0
        else:
            # Wall-clock, not the data clock: the sweep is how the panel is
            # presented, and --rate is for moving the *data* under it.
            k = int((t / max(args.cycle, 1e-3)) * n) % n
        hr = cell["hours"][k]

        # The header is the only chrome that moves. Formatting and blitting it
        # is a millisecond, and it changes once an hour of the sweep and once
        # a minute for the age -- so it is asked only when one of those did.
        age_key = ftdata.describe_age(cell["age"]) if cell["age"] else ""
        if cell["key"] != (k, age_key, cell["stale"]):
            cell["key"] = (k, age_key, cell["stale"])
            draw_header(cell["mos"][k], hr["cond"], cell["stale"], age_key)
            if lay.head_h:
                frame[:lay.head_h] = header
        cell["cur"] = k

        reg = frame[lay.map_y:lay.map_y + lay.map_rows]
        np.copyto(reg, hr["static"])

        field = hr["field"]
        dt = min(0.2, 1.0 / max(args.fps, 1))
        np.clip(pos, 0.0, bounds, out=clipped)
        np.copyto(ipos, clipped, casting="unsafe")
        flat = ipos[0] * w
        flat += ipos[1]
        vel = field[:, flat]
        np.add(pos, vel * dt, out=pos)
        np.add(page, dt, out=page)

        # Out of the frame or out of life. Wind blows over land as well as
        # water, so unlike tide's drifters nothing here dies on a shoreline.
        # "Out of the frame" is asked as "does clipping move it?", which is
        # three numpy calls where four explicit comparisons and three ors
        # would be seven, and on this machine the call is the cost.
        np.clip(pos, 0.0, bounds, out=clipped)
        dead = (page > plife) | (clipped != pos).any(axis=0)
        nd = int(dead.sum())
        if nd:
            respawn(dead, nd)

        # The streak, tail first so the head lands on top. Its samples are the
        # particle's own velocity stepped back in time, so length is speed.
        np.multiply(vel, STREAK_K, out=streak)
        np.add(streak, pos, out=streak)
        np.clip(streak, 0.0, bounds, out=streak)
        si = streak.astype(np.int32)
        sf = si[:, 0] * w
        sf += si[:, 1]
        mv = reg.reshape(-1, 3)
        colour = hr["colour"]
        for j in range(len(STREAK_BRIGHT)):
            mv[sf[j]] = colour[j, sf[j]]
        mv[shore_idx] = C_SHORE
        return frame

    # The scale never changes, so it is drawn once and lives in the frame.
    draw_legend()
    if lay.leg_h:
        frame[lay.leg_y:] = legend

    reload_data(now_of())
    render.state = cell                 # tests reach in here; nothing else does
    render.layout = lay
    render.particles = pos
    render.extent = extent
    render.sea_map = sea_map
    render.clock = now_of
    render.px_per_kt = (px_e, px_n)
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "live wind field over San Francisco Bay, Windy style",
                  fps=30)


if __name__ == "__main__":
    main()
