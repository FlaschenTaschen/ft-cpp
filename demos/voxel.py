#!/usr/bin/env python3
"""A hang glider over the Golden Gate, in voxel space.

Comanche-style terrain: for every screen column, march a ray out along the
ground, look up the height of the real San Francisco Bay under it, and work
out how far up the screen that lands. The nearest thing wins. It is the oldest
trick for drawing landscape in real time and it is still the right one for a
320x64 panel, because the cost is set by the number of columns and the depth
budget rather than by any amount of geometry, and because a heightmap of the
Bay Area is a 200 kB file.

The terrain is not noise. It is USGS 3DEP elevation, 45 m cells over a 35 km
square holding Mount Tamalpais, the Marin Headlands, the strait, San Francisco
out to Twin Peaks, Angel Island and the Berkeley hills -- baked into
`voxel-dem.npz` by `scripts/make-voxel-dem.py`, which is where the provenance
is written down. Sea level is stored as exactly zero, so the Bay and the
Pacific are a comparison rather than a second map.

Nothing here is a helicopter. The flier is a glider circling a thermal off
Hawk Hill: a long lazy loop with the bank swinging either way, the horizon
tilting behind a wing that stays where it is, other birds working the same
lift, and the light low and warm. No instruments and nothing to aim at.

Run:  python3 voxel.py --host 127.0.0.1
      python3 voxel.py --light dusk --fog 1.4
      python3 voxel.py --loop 90 --radius 550 --altitude 520
      python3 voxel.py --no-wing --birds 0 --steps 64
"""

import math
import os
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

DEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voxel-dem.npz")

# --------------------------------------------------------------------------
# The palette is one flat table and every pixel on screen is one index into
# it. The layout is arithmetic rather than a lookup:
#
#   surface:  (class * NSHADE + shade) * NFOG + haze band
#   sky:      SKY0 + rows above the horizon
#
# so brightening a water pixel one step is +NFOG, hazing anything is a change
# of the low digits, and the bridge is a class like any other. Nothing in the
# frame ever computes an RGB value; the last thing it does is one gather.
# --------------------------------------------------------------------------

NSHADE = 12                 # hillshade levels per surface class
NFOG = 20                   # distance-haze levels
# The sky ramp is indexed by how far a pixel is above the horizon, in *thirds*
# of a row. Whole rows was the obvious thing and it was visibly wrong: the
# horizon shears with the bank, so the index steps by one somewhere along the
# width, and a one-entry step in a gradient this smooth draws a vertical seam
# right down the panel. At a third of a row the seams are finer than the
# dither and disappear.
SKY_SUB = 3
NSKY = 3 * 176              # sky ramp entries
SKY_MID = 3 * 112           # which entry of that ramp sits on the horizon
SKY_SPAN = 3 * 40.0         # thirds of a row from the horizon to the top colour

# Surface classes. Water is class 0 and must stay there: the per-frame water
# treatment is the single compare `index < NSHADE * NFOG`, which only works
# because water owns the bottom of the table.
CLS_WATER, CLS_LOW, CLS_MID, CLS_HIGH = 0, 1, 2, 3
CLS_TOWER, CLS_DECK, CLS_CABLE, CLS_BIRD = 4, 5, 6, 7
NCLS = 8

# Water shades are not a hillshade. Shade 0 is the flat surface, 1 the chop,
# 2 and 3 the sun's glitter -- reserved so the per-frame bump can be an
# integer add that cannot climb out of the class.
W_FLAT, W_CHOP, W_GLINT = 0, 1, 2

SKY0 = NCLS * NSHADE * NFOG

# Times of day. The sun azimuth and elevation feed the hillshade *and* where
# the sun is drawn, so the long shadows and the glare always agree.
LIGHTS = {
    # The default, and what the demo is for: an hour before sunset, sun out
    # over the Pacific and a little south, which is the light that picks out
    # the west faces of the Headlands and lays the glitter across the strait.
    "golden": dict(
        sky=[(0.00, (16, 42, 112)), (0.40, (70, 92, 158)),
             (0.72, (196, 138, 122)), (1.00, (250, 190, 128))],
        haze=(196, 152, 120), sun=(255, 232, 168), glow=(255, 168, 96),
        sun_az=254.0, sun_el=13.0,
        water=(20, 32, 58), chop=(28, 41, 69), glint=(255, 214, 150),
        land=[(46, 54, 40), (68, 70, 46), (94, 88, 58)],
        ambient=0.42, diffuse=0.92, warm=(1.20, 0.96, 0.68)),
    # First light from behind Diablo: cold, pink, the water nearly black.
    "dawn": dict(
        sky=[(0.00, (8, 14, 48)), (0.40, (36, 42, 94)),
             (0.74, (130, 94, 130)), (1.00, (214, 142, 132))],
        haze=(146, 110, 116), sun=(255, 208, 186), glow=(226, 118, 110),
        sun_az=74.0, sun_el=11.0,
        water=(12, 18, 40), chop=(28, 36, 62), glint=(226, 176, 168),
        land=[(30, 36, 42), (44, 48, 50), (62, 62, 62)],
        ambient=0.36, diffuse=0.86, warm=(1.14, 0.88, 0.86)),
    # Flat overhead light: the least interesting and the most legible, which
    # is why it is here. It is the one to switch to when the geometry looks
    # wrong, because nothing is hiding in a shadow.
    "noon": dict(
        sky=[(0.00, (24, 72, 166)), (0.45, (70, 124, 198)),
             (0.80, (148, 182, 216)), (1.00, (196, 212, 224))],
        haze=(162, 182, 200), sun=(255, 255, 240), glow=(220, 232, 245),
        sun_az=196.0, sun_el=58.0,
        water=(30, 62, 104), chop=(46, 80, 122), glint=(240, 248, 255),
        land=[(58, 74, 46), (84, 92, 52), (112, 108, 66)],
        ambient=0.46, diffuse=0.72, warm=(1.02, 1.02, 1.00)),
    # Just after the sun has gone. Everything is silhouette and the water
    # holds the last of the light -- the prettiest of the four on an LED wall
    # and the hardest to keep out of the mud.
    "dusk": dict(
        sky=[(0.00, (6, 12, 40)), (0.38, (30, 32, 82)),
             (0.72, (108, 60, 104)), (1.00, (206, 104, 88))],
        haze=(138, 84, 82), sun=(255, 176, 120), glow=(232, 108, 78),
        sun_az=282.0, sun_el=2.0,
        water=(14, 18, 44), chop=(28, 30, 58), glint=(238, 152, 120),
        land=[(24, 26, 34), (36, 36, 42), (54, 50, 54)],
        ambient=0.34, diffuse=0.74, warm=(1.22, 0.82, 0.64)),
}

# --------------------------------------------------------------------------
# The Golden Gate Bridge, in metres converted from the feet goldengate.py
# works in: 4200 ft of main span between the towers, 1125 ft of side span
# either side, 526 ft of tower over a deck 220 ft above the water. Same
# numbers, same International Orange. What differs is that here it is an
# object standing in a landscape and has to be depth-tested against it.
# --------------------------------------------------------------------------

MAIN_SPAN = 1280.2
SIDE_SPAN = 342.9
DECK_H = 67.1
TOWER_H = DECK_H + 160.3
BRIDGE_LEN = 2 * SIDE_SPAN + MAIN_SPAN
S_TOWER = SIDE_SPAN / BRIDGE_LEN                # tower position along s
TOWER_THICK = 16.0                              # along the deck, metres
ANCHOR_H = 26.0

ORANGE = (198.0, 70.0, 38.0)
ORANGE_LIT = (232.0, 104.0, 56.0)
ORANGE_DECK = (126.0, 52.0, 30.0)

# Midspan and bearing of the real bridge. The strait runs roughly east-west
# and the bridge crosses it a few degrees off true north, the Marin end very
# slightly west of the Fort Point end.
BRIDGE_LAT, BRIDGE_LON, BRIDGE_BEARING = 37.8199, -122.4783, 353.0

# Where the glider circles: over the water at the mouth of the strait, the
# Headlands to the north, the bridge and the city to the east, Mount
# Tamalpais on the northwest horizon and the Pacific behind. Every heading
# from here has something in it, which is the entire reason for this spot.
LOOP_LAT, LOOP_LON = 37.8215, -122.4990

_BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], f32)


def add_arguments(ap):
    ap.add_argument("--light", default="golden", choices=sorted(LIGHTS),
                    help="time of day: sun position, haze and palette")
    ap.add_argument("--fog", type=float, default=1.0,
                    help="haze density, 0 clear to ~2 socked in")
    ap.add_argument("--steps", type=int, default=96,
                    help="depth samples per column; the whole cost knob")
    ap.add_argument("--far", type=float, default=13000.0,
                    help="far plane, metres")
    ap.add_argument("--near", type=float, default=120.0,
                    help="near plane, metres")
    ap.add_argument("--fov", type=float, default=96.0,
                    help="horizontal field of view, degrees")
    ap.add_argument("--altitude", type=float, default=430.0,
                    help="mean height above sea level, metres")
    ap.add_argument("--climb", type=float, default=95.0,
                    help="how far the thermals lift and drop you, metres")
    ap.add_argument("--loop", type=float, default=72.0,
                    help="seconds for one circuit of the flight path")
    ap.add_argument("--radius", type=float, default=390.0,
                    help="radius of that circuit, metres")
    ap.add_argument("--bank", type=float, default=1.0,
                    help="how far the horizon tilts in the turns")
    ap.add_argument("--phase", type=float, default=0.0,
                    help="where in the circuit to start, 0..1")
    ap.add_argument("--no-bridge", dest="bridge", action="store_false",
                    help="the Gate without the bridge over it")
    ap.add_argument("--no-wing", dest="wing", action="store_false",
                    help="no glider wing in the frame")
    ap.add_argument("--birds", type=int, default=3,
                    help="other birds working the same thermal")
    ap.add_argument("--dither", type=float, default=1.0,
                    help="ordered dither depth in LSBs (0 = off)")
    ap.add_argument("--seed", type=int, default=1937,
                    help="seed for the water noise and the birds")


# --------------------------------------------------------------------------
# Terrain.
# --------------------------------------------------------------------------

def load_dem():
    """Heights in metres, the water mask, metres per cell, and the bbox.

    The file stores the horizontal *difference* of the integer heights, which
    is what gets a 768x768 grid into 200 kB: terrain is smooth, so the
    differences are small numbers around zero and DEFLATE eats them. One
    cumulative sum puts it back, and costs a millisecond once.
    """
    d = np.load(DEM)
    hgt = np.cumsum(d["dh"].astype(np.int32), axis=1).astype(f32)
    shape = tuple(int(v) for v in d["shape"])
    sea = np.unpackbits(d["sea"])[:shape[0] * shape[1]].reshape(shape).astype(bool)
    mx, my = (float(v) for v in d["metres"])
    return hgt, sea, mx, my, tuple(float(v) for v in d["bbox"])


def world_of(lat, lon, bbox, mx, my, shape):
    """A place on the earth as metres east and metres south of the map corner.

    Everything in the demo is metres in this frame: u runs east, v runs
    south, because the DEM's rows run north to south the way it came off the
    server, and turning it over to make the axes tidy would only mean two
    more sign errors somewhere else.
    """
    lon0, lat0, lon1, lat1 = bbox
    u = (lon - lon0) / (lon1 - lon0) * shape[1] * mx
    v = (lat1 - lat) / (lat1 - lat0) * shape[0] * my
    return u, v


def hillshade(hgt, mx, my, az_deg, el_deg):
    """Cosine of the angle between the surface normal and the sun, 0..1.

    Central differences rather than forward ones: a forward difference shifts
    the whole shading half a cell downhill, which at 45 m cells and a sun this
    low puts the lit edge visibly off the ridge line -- and the ridge lines
    are the only thing at this size that says which hill you are looking at.
    """
    dzdu = np.zeros_like(hgt)
    dzdv = np.zeros_like(hgt)
    dzdu[:, 1:-1] = (hgt[:, 2:] - hgt[:, :-2]) / (2.0 * mx)
    dzdv[1:-1] = (hgt[2:] - hgt[:-2]) / (2.0 * my)
    az, el = math.radians(az_deg), math.radians(el_deg)
    lu, lv, lz = (math.cos(el) * math.sin(az), -math.cos(el) * math.cos(az),
                  math.sin(el))
    ndot = -dzdu * lu - dzdv * lv + lz
    ndot /= np.sqrt(dzdu * dzdu + dzdv * dzdv + 1.0)
    return np.maximum(ndot, 0.0)


def terrain_index(hgt, sea, mx, my, light):
    """One uint8 (class, shade) per map cell.

    Resolving every lighting decision into a small integer here is what makes
    the frame affordable: which face is lit, how high the ground is and
    whether it is water are all settled at build time, and render() only ever
    gathers the answer.
    """
    lit = hillshade(hgt, mx, my, light["sun_az"], light["sun_el"])
    shade = np.rint(lit * (NSHADE - 1)).astype(np.uint8)
    # Three land bands by height, and the boundaries are not arbitrary: 70 m
    # is about where the Headlands stop being beach and start being hill, and
    # 260 m is the top of the coastal scrub, above which Tam is grass and
    # rock. They are also far enough apart to read at 64 rows.
    cls = np.where(hgt > 260.0, CLS_HIGH,
                   np.where(hgt > 70.0, CLS_MID, CLS_LOW)).astype(np.uint8)
    idx = cls * NSHADE + shade
    idx[sea] = CLS_WATER * NSHADE + W_FLAT
    return idx.astype(np.uint8)


def pad_sea(a, fill):
    """Ring the map with one cell of sea, so a ray that runs off it finds ocean.

    The alternative -- clamping the sample to the last real cell -- smears
    that cell outward forever, and the Berkeley hills become a ridge running
    to the horizon. One border cell costs nothing and makes the edge of the
    world open water, which on three of the four sides is what is there.
    """
    out = np.full((a.shape[0] + 2, a.shape[1] + 2), fill, a.dtype)
    out[1:-1, 1:-1] = a
    return out


# --------------------------------------------------------------------------
# Colour tables.
# --------------------------------------------------------------------------

def ramp(stops, x):
    """Colour stops interpolated at positions `x`, kept in float.

    Not ds.gradient(), for the reason sunset.py sets out: gradient() rounds to
    eight bits, and ordered dithering a value that has already been rounded
    adds noise and nothing else. The fraction has to survive to the last cast.
    """
    pos = np.array([p for p, _ in stops], f32)
    cols = np.array([c for _, c in stops], f32)
    return np.stack([np.interp(x, pos, cols[:, ch]) for ch in range(3)],
                    axis=-1).astype(f32)


def build_palette(light, fog_gain):
    """The one table every pixel indexes. See the layout note at the top."""
    haze = np.array(light["haze"], f32)
    warm = np.array(light["warm"], f32)
    base = np.zeros((NCLS, NSHADE, 3), f32)

    # Land: the class colour lifted by the hillshade, and warmed as it lights,
    # because a low sun does not only make a slope brighter, it makes it a
    # different colour from the slope beside it in shadow.
    amb, dif = light["ambient"], light["diffuse"]
    s = np.linspace(0.0, 1.0, NSHADE, dtype=f32)[:, None]
    for c in (CLS_LOW, CLS_MID, CLS_HIGH):
        col = np.array(light["land"][c - 1], f32)[None, :]
        base[c] = col * (amb + dif * s) * (1.0 + (warm - 1.0) * s)

    # Water: four fixed shades rather than a ramp -- flat, chop, and two
    # levels of glitter, which is what the per-frame integer bump moves
    # between.
    water = np.array(light["water"], f32)
    chop = np.array(light["chop"], f32)
    glint = np.array(light["glint"], f32)
    base[CLS_WATER, W_FLAT] = water
    base[CLS_WATER, W_CHOP] = chop
    # The two glitter steps are mixed well back towards the water. Taken at
    # full strength they came out as pale blotches the size of Alcatraz
    # rather than as light on a moving surface: at 64 rows there is no room
    # for a highlight to be both bright and small, so it has to be small.
    base[CLS_WATER, W_GLINT] = glint * 0.30 + water * 0.70
    base[CLS_WATER, W_GLINT + 1:] = glint * 0.62 + water * 0.38

    # The bridge is a class like any other, which is the whole trick that
    # makes it cost nothing: painting it is writing an integer into the index
    # image before the gather, so it picks up the right amount of haze for
    # its distance without a single colour operation.
    for c, rgb in ((CLS_TOWER, ORANGE), (CLS_DECK, ORANGE_DECK),
                   (CLS_CABLE, ORANGE_LIT), (CLS_BIRD, (18.0, 16.0, 20.0))):
        base[c] = np.array(rgb, f32)[None, :]

    # Haze. Every colour above, faded towards the horizon colour over NFOG
    # steps, so distance costs the frame nothing at all: the depth step picks
    # a band and the band is already the right colour.
    #
    # The curve matters more than the density. A linear fade lays a grey veil
    # over the near ground; what real haze does is almost nothing for the
    # first kilometre and then everything, which is the exponent below.
    f = (np.linspace(0.0, 1.0, NFOG, dtype=f32) ** 1.8)[:, None]
    np.minimum(f * fog_gain, 1.0, out=f)
    pal = np.empty((SKY0 + NSKY, 3), f32)
    flat = base.reshape(-1, 3)
    for i in range(flat.shape[0]):
        pal[i * NFOG:(i + 1) * NFOG] = flat[i] + (haze - flat[i]) * f

    # Sky, indexed by how many rows above the horizon a pixel is. Below the
    # horizon it holds at the haze colour, which is also what a pixel gets if
    # the depth budget ran out before the ray hit anything.
    above = (SKY_MID - np.arange(NSKY, dtype=f32)) / SKY_SPAN
    np.clip(above, 0.0, 1.0, out=above)
    pal[SKY0:] = ramp(light["sky"], 1.0 - above)
    return np.clip(pal, 0.0, 253.0).astype(f32)


def haze_band(z, far):
    """Which haze band a depth falls in. Same curve as build_palette()."""
    return np.rint(np.clip(z / far, 0.0, 1.0) * (NFOG - 1)).astype(np.int32)


def value_noise(rng, h, w, cy, cx):
    """Tileable value noise as uint8, for the water surface.

    Separate cell counts per axis, and they are nothing like equal: water seen
    from above is banded, not spotted, and a square-celled noise stretched
    across a 5:1 panel gives round blobs the size of Alcatraz.
    """
    g = rng.random((cy, cx)).astype(f32)

    def axis(n, cells):
        ff = np.arange(n, dtype=f32) * (cells / float(n))
        i0 = np.floor(ff).astype(np.int32) % cells
        tt = ff - np.floor(ff)
        return i0, (i0 + 1) % cells, (tt * tt * (3.0 - 2.0 * tt)).astype(f32)

    y0, y1, ty = axis(h, cy)
    x0, x1, tx = axis(w, cx)
    top = g[y0][:, x0] * (1 - tx) + g[y0][:, x1] * tx
    bot = g[y1][:, x0] * (1 - tx) + g[y1][:, x1] * tx
    v = top * (1 - ty)[:, None] + bot * ty[:, None]
    v -= v.min()
    v /= max(float(v.max()), 1e-6)
    return np.rint(v * 255.0).astype(np.uint8)


# --------------------------------------------------------------------------
# The glider, drawn once, because it does not move.
#
# That is not a shortcut, it is the physics: in a coordinated turn the pilot
# and the wing keep the same relationship and it is the *world* that tilts. So
# the wing is a static screen-space overlay costing one composite a frame, and
# the horizon rolling behind something nailed to the frame is exactly what
# banking looks like from underneath a sail.
# --------------------------------------------------------------------------

def build_wing(W, H):
    """The leading edges, and only as much of the sail as they carry in with them.

    An earlier version filled the whole top of the frame with sail, and at 64
    rows that does not read as a wing at all -- it reads as a lens vignette,
    a dark arch over the picture. What reads is the *lines*: two straight
    spars going back and out to the tips, entering the frame only in the outer
    thirds, so the middle of the panel -- where the horizon, the sun and the
    skyline are -- is left completely alone. The nose is ahead of you and
    above the top edge, which is why the spars leave through the sides.
    """
    sy = H / 64.0
    x = np.arange(W, dtype=f32)
    y = np.arange(H, dtype=f32)[:, None]
    cx = 0.5 * W
    d = np.abs(x - cx) / (0.5 * W)
    edge = (-8.0 + d * 20.0) * sy
    spar = max(1.0, 1.2 * sy)
    cov = np.clip(np.minimum(y + 0.5, edge + spar) - np.maximum(y - 0.5, edge),
                  0.0, 1.0)
    # A sliver of sail above each spar rather than a filled corner. Backlit
    # dacron at this hour is not black, so it is a warm grey a couple of steps
    # off the sky rather than a silhouette.
    sail = np.clip(edge - y + 0.5, 0.0, 1.0) * np.clip(1.0 - (edge - y) / (5.0 * sy),
                                                       0.0, 1.0) * 0.85
    col = np.zeros((H, W, 3), f32)
    a = np.zeros((H, W), f32)
    for rgb, m in ((np.array((58.0, 52.0, 54.0), f32), sail),
                   (np.array((126.0, 116.0, 110.0), f32), cov)):
        col *= (1.0 - m)[..., None]
        col += rgb * m[..., None]
        a += (1.0 - a) * m
    # One front wire each side, a single dim pixel wide, running down and in
    # from the spar. Without them the spars read as two scratches; with them
    # they read as something you are hanging underneath.
    for sign in (-1, 1):
        for k in range(int(9 * sy)):
            xx = int(cx + sign * (0.34 * W - k * 1.6))
            yy = int(1 + k * 1.05 * sy)
            if 0 <= xx < W and 0 <= yy < H:
                col[yy, xx] = (92.0, 84.0, 82.0)
                a[yy, xx] = max(float(a[yy, xx]), 0.7)
    return (col * a[..., None]).astype(f32), (1.0 - a[..., None]).astype(f32)


def build_sun(light, H):
    """A small additive disc and halo, blitted where the sky shows through."""
    r = max(1.6, 0.075 * H)
    n = 2 * int(math.ceil(r * 3.6)) + 1
    c = n // 2
    yy = (np.arange(n, dtype=f32) - c)[:, None]
    xx = (np.arange(n, dtype=f32) - c)[None, :]
    # A low sun is refracted wider than it is tall, which is also what stops
    # something this small reading as one stray bright pixel.
    # The edge is deliberately soft and the disc deliberately short of full
    # scale. Drawn hard and bright it saturates every channel over an area
    # the eye can measure, and a flat white ellipse with a crisp rim reads as
    # a sprite pasted onto the sky; most of what makes it read as the sun is
    # the halo bleeding into the sky around it, not the disc.
    d = np.sqrt((xx / 1.45) ** 2 + yy ** 2) / r
    disc = np.clip((1.0 - d) * 2.4, 0.0, 1.0) ** 1.4 * 0.86
    halo = np.exp(-(d * 0.72) ** 2) * 0.62
    spr = (np.array(light["sun"], f32) * disc[..., None]
           + np.array(light["glow"], f32) * halo[..., None])
    return spr.astype(f32), c


# At this size a bird is five pixels, and its only distinguishing feature is
# that the wings are not level with the body. The shallow M is the whole read
# and the deep one is the downstroke; anything more detailed is a smudge.
BIRD_POSES = [(" X X ",
               "X   X"),
              ("XX XX",
               "  X  ")]


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    light = LIGHTS[args.light]

    hgt, sea, mx, my, bbox = load_dem()
    shape = hgt.shape
    cmap = pad_sea(terrain_index(hgt, sea, mx, my, light),
                   np.uint8(CLS_WATER * NSHADE + W_FLAT))
    hmap = pad_sea(hgt, f32(0.0))
    del hgt, sea
    MH, MW = hmap.shape
    hflat = np.ascontiguousarray(hmap.reshape(-1))
    cflat = np.ascontiguousarray(cmap.reshape(-1).astype(np.int32))
    # Sample coordinates are metres from the padded corner, so the border
    # cell is already in the arithmetic and the clamp below is a plain clamp
    # to the array.
    inv_mx, inv_my = f32(1.0 / mx), f32(1.0 / my)

    loop_u, loop_v = world_of(LOOP_LAT, LOOP_LON, bbox, mx, my, shape)
    bri_u, bri_v = world_of(BRIDGE_LAT, BRIDGE_LON, bbox, mx, my, shape)
    loop_u += mx
    loop_v += my                                  # into padded coordinates
    bri_u += mx
    bri_v += my

    # --- camera -------------------------------------------------------------
    focal = 0.5 * W / math.tan(0.5 * math.radians(
        min(max(args.fov, 20.0), 150.0)))
    colf = np.arange(W, dtype=f32) + 0.5
    colx = ((colf - 0.5 * W) / focal).astype(f32)   # ray shear per column
    colidx = np.arange(W, dtype=np.int32)

    # --- depth schedule ------------------------------------------------------
    # Geometric, because screen space is. Linear steps put nearly all the
    # samples out in the far field where a whole kilometre of depth is one
    # row, and none of them near you where the ground is going past.
    N = max(16, int(args.steps))
    near = max(20.0, args.near)
    far = max(near * 4.0, args.far)
    Z = (near * (far / near) ** np.linspace(0.0, 1.0, N, dtype=f32)).astype(f32)
    invZ = (f32(focal) / Z)[:, None]
    Zbuf = np.concatenate([Z, [1e9]]).astype(f32)   # sentinel: sky is infinite
    fogb = np.concatenate([haze_band(Z, far), [NFOG - 1]]).astype(np.int32)
    Zcol = Z[:, None]

    pal = build_palette(light, max(args.fog, 0.0))
    dith = (np.tile((_BAYER8 + 0.5) / 64.0, (H // 8 + 1, W // 8 + 1))[:H, :W, None]
            .astype(f32) * f32(args.dither))

    # --- water ---------------------------------------------------------------
    # Two tileable noise fields stored doubled, so scrolling them is a plain
    # contiguous slice rather than a modulo gather. They are read in *screen*
    # space, not world space: a depth-scrolled water texture cannot win at 64
    # rows -- tight enough to show crests near you and it aliases to hash
    # across the whole strait, which is the lesson sunset.py paid for.
    nh, nw = max(16, H), max(32, W)
    tex1 = np.tile(value_noise(rng, nh, nw, 20, 104), (2, 2))
    tex2 = np.tile(value_noise(rng, nh, nw, 26, 104), (2, 2))

    # --- sun ------------------------------------------------------------------
    saz, sel = math.radians(light["sun_az"]), math.radians(light["sun_el"])
    sun_dir = (math.cos(sel) * math.sin(saz), -math.cos(sel) * math.cos(saz),
               math.sin(sel))
    sun_spr, sun_c = build_sun(light, H)
    sun_h, sun_w = sun_spr.shape[:2]

    # The sun's glitter path, baked as the threshold the water noise has to
    # beat, three panels wide with the sun at the middle. Per frame it is a
    # *slice* taken at the sun's column and nothing else, and because it is a
    # field rather than one number per column it can do what a real glitter
    # path does -- narrow out at the horizon, fanning towards you -- since a
    # row further down the panel is water that is nearer.
    gx = (np.arange(3 * W, dtype=f32) - 1.5 * W)[None, :]
    gw = (5.0 + 0.85 * np.arange(H, dtype=f32) * (64.0 / H))[:, None]
    glint_field = np.clip(252.0 - 56.0 * np.exp(-(gx / gw) ** 2),
                          188.0, 255.0).astype(np.uint8)
    glint_off = np.full((H, W), 255, np.uint8)     # sun behind: nothing lights

    # --- bridge ---------------------------------------------------------------
    br = math.radians(BRIDGE_BEARING)
    bu, bv = math.sin(br), -math.cos(br)          # towards the Marin end
    half = 0.5 * BRIDGE_LEN
    br_ax, br_ay = bri_u - bu * half, bri_v - bv * half     # s = 0, Fort Point
    br_ex, br_ey = 2.0 * bu * half, 2.0 * bv * half         # the whole span
    ss = np.linspace(0.0, 1.0, 129, dtype=f32)
    deck_t = (DECK_H - 11.0 * (2.0 * ss - 1.0) ** 2).astype(f32)
    q = np.abs(ss - 0.5) / (0.5 - S_TOWER)
    # Between the towers the main cable is a parabola whose vertex sits *on*
    # the deck at midspan. That one detail is most of what makes a silhouette
    # read as this bridge rather than any suspension bridge, and it is the
    # same call goldengate.py makes. Outside them it falls to the anchorages.
    outer = np.clip((q - 1.0) / (0.5 / (0.5 - S_TOWER) - 1.0), 0.0, 1.0)
    cable_t = np.where(q <= 1.0,
                       deck_t + (TOWER_H - deck_t) * np.clip(q, 0.0, 1.0) ** 2,
                       TOWER_H + (ANCHOR_H - TOWER_H) * outer ** 0.8).astype(f32)
    br_tower = np.array([S_TOWER, 1.0 - S_TOWER], f32)
    br_pidx = np.array([CLS_TOWER, CLS_DECK, CLS_CABLE], np.int32) * (NSHADE * NFOG)

    # --- birds ----------------------------------------------------------------
    bird_masks = [np.array([[ch != ' ' for ch in r] for r in p], bool)
                  for p in BIRD_POSES]
    nbirds = max(0, int(args.birds))
    # They work the same lift, a few hundred metres off and lower down, which
    # is what makes them a depth cue rather than decoration: you look down on
    # them against the water and they cross in front of the headland.
    bird_r = rng.uniform(180.0, 460.0, nbirds)
    bird_ph = rng.uniform(0.0, 2.0 * math.pi, nbirds)
    bird_dz = rng.uniform(-150.0, -40.0, nbirds)
    bird_rate = rng.uniform(0.055, 0.085, nbirds)
    bird_flap = rng.uniform(1.6, 2.6, nbirds)
    bird_pix = np.int32(CLS_BIRD * NSHADE * NFOG)

    # --- wing -----------------------------------------------------------------
    wing_pre, wing_inv = build_wing(W, H) if args.wing else (None, None)
    if args.wing:
        # Three real planes rather than an (H,W,1) broadcast: on this hardware
        # broadcasting a trailing 1 against three channels is about four times
        # slower than the same arithmetic on a full array, which karl.py found
        # and is worth a quarter of a megabyte to avoid.
        wing_inv = np.ascontiguousarray(np.repeat(wing_inv, 3, axis=2))
        wing_dith = (wing_pre + dith).astype(f32)

    # --- scratch, all of it owned --------------------------------------------
    su = np.empty((N, W), f32)
    sv = np.empty((N, W), f32)
    tt = np.empty((N, W), f32)
    hs = np.empty((N, W), f32)
    mi = np.zeros((N + 1, W), np.int32)           # row N is the sky sentinel
    miv = mi[:N]
    miflat = mi.reshape(-1)
    tmpi = np.empty((N, W), np.int32)
    bkey = np.empty((N, W), np.int32)
    horiz = np.empty(W, f32)
    hrowf = np.empty(W, f32)
    hrow = np.empty(W, np.int32)
    skyoff = np.empty(W, np.int32)
    idx = np.empty((H, W), np.int32)
    pidx = np.empty((H, W), np.int32)
    gath = np.empty((H, W), np.int32)
    # A second index scratch purely so that no np.take() has the same array as
    # both its indices and its destination. numpy happens to survive that here
    # -- an int32 index array is not intp, so it gets copied on the way in --
    # but it is undefined, and it would stop being true the day this ran on a
    # 32-bit Pi where int32 *is* intp.
    cell = np.empty((H, W), np.int32)
    bump = np.empty((H, W), np.int32)
    aux8 = np.empty((H, W), bool)
    mask = np.empty((H, W), bool)
    rows_i3 = np.arange(H, dtype=np.int32)[:, None] * SKY_SUB
    zbuf = np.empty((H, W), f32)
    buf = np.empty((H, W, 3), f32)
    out = np.empty((H, W, 3), np.uint8)
    nbins = (H + 1) * W
    watermax = np.int32(NSHADE * NFOG)
    need_z = bool(args.bridge or nbirds)

    period = max(args.loop, 8.0)
    radius = max(args.radius, 40.0)

    def camera(t):
        """Where the glider is and which way it is pointing.

        A closed curve rather than an integrated heading, so the path is
        exactly periodic: a segment that overruns the loop point lands back
        where it started instead of drifting off the map. Two harmonics -- one
        circuit per loop plus a smaller three-per-loop wobble -- because a
        constant-bank circle is what a thermal actually is and is dull to
        watch, whereas this steepens, shallows and briefly reverses.

        Heading and bank come from the first and second derivatives of that
        same curve rather than being animated separately, so the wing is
        always banked into the turn it is really making. All of it is `math`
        on plain floats: a float32 numpy scalar costs about thirty times more
        per operation, which fsn.py measured the hard way.
        """
        w = 2.0 * math.pi / period
        a = w * t + args.phase * 2.0 * math.pi
        s1, c1 = math.sin(a), math.cos(a)
        s3, c3 = math.sin(3.0 * a + 0.9), math.cos(3.0 * a + 0.9)
        r2 = 0.24 * radius
        u = loop_u + radius * s1 + r2 * s3
        v = loop_v + 0.82 * radius * c1 + 0.7 * r2 * c3
        du = w * (radius * c1 + 3.0 * r2 * c3)
        dv = w * (-0.82 * radius * s1 - 3.0 * 0.7 * r2 * s3)
        ddu = w * w * (-radius * s1 - 9.0 * r2 * s3)
        ddv = w * w * (-0.82 * radius * c1 - 9.0 * 0.7 * r2 * c3)
        sp2 = max(du * du + dv * dv, 1e-6)
        psi = math.atan2(dv, du)
        kappa = (du * ddv - dv * ddu) / sp2       # rate of turn
        # Thermals, on periods that do not share a factor with the circuit,
        # so the two never line up into an obvious cycle.
        z = (args.altitude
             + args.climb * (0.72 * math.sin(w * t * 0.63 + 1.1)
                             + 0.34 * math.sin(w * t * 1.47 + 0.2)))
        dz = args.climb * w * (0.4536 * math.cos(w * t * 0.63 + 1.1)
                               + 0.4998 * math.cos(w * t * 1.47 + 0.2))
        return u, v, z, psi, kappa, math.atan2(dz, math.sqrt(sp2))

    # ----------------------------------------------------------------------
    # The bridge, composited into the *index* image rather than into colour.
    #
    # A heightmap cannot have sky under a road deck, so this is an object, and
    # an object in front of a raycast has to be depth-tested against it or a
    # headland stops occluding it. Both come out cheap here. The ray for each
    # column is intersected with the vertical plane of the bridge -- a 2x2
    # solve, vectorised across the whole width -- which gives that column's
    # distance along the deck and its distance from the eye in one step; and
    # because the raycast already left a depth per pixel, hiding the bridge
    # behind Lime Point is one compare.
    #
    # Then it is painted as class numbers, so the towers pick up exactly the
    # haze their distance earns with no colour arithmetic at all.
    # ----------------------------------------------------------------------

    def draw_bridge(camu, camv, camz, fu, fv, ru, rv):
        dx = colx * f32(ru) + f32(fu)
        dy = colx * f32(rv) + f32(fv)
        det = br_ex * dy - br_ey * dx
        qx, qy = br_ax - camu, br_ay - camv
        with np.errstate(divide="ignore", invalid="ignore"):
            tz = (br_ex * qy - br_ey * qx) / det
            sp = (qy * dx - qx * dy) / det
        ok = (np.abs(det) > 1e-9) & (tz > near) & (sp >= 0.0) & (sp <= 1.0)
        nz = np.nonzero(ok)[0]
        if len(nz) < 2:
            return
        c0, c1 = int(nz[0]), int(nz[-1]) + 1
        sl = slice(c0, c1)
        sp, tz, ok = sp[sl], tz[sl], ok[sl]
        n = c1 - c0
        sc = f32(focal) / tz
        deck = np.interp(sp, ss, deck_t)
        cable = np.interp(sp, ss, cable_t)
        base = horiz[sl] + camz * sc
        r_deck = base - deck * sc
        r_cable = base - cable * sc
        r_top = base - TOWER_H * sc
        # A tower is only 16 m thick along the deck, which from a kilometre
        # out is well under a pixel. Widen it to whatever covers a column and
        # a half, the same call goldengate.py makes for the cables: at this
        # size the silhouette is the whole point and a tower that keeps
        # dropping out between columns reads as a flicker, not as accuracy.
        dsdc = np.abs(np.diff(sp, append=sp[-1] if n > 1 else 0.0))
        hw = np.maximum(TOWER_THICK / BRIDGE_LEN, 1.5 * dsdc)
        istow = ((np.abs(sp - br_tower[0]) < hw)
                 | (np.abs(sp - br_tower[1]) < hw)) & ok
        # Suspenders every third column, which at any distance this bridge is
        # ever seen from is denser than the real 50 ft spacing would resolve.
        issus = ok & ((colidx[sl] % 3) == 0)

        r0 = int(max(0, math.floor(min(float(np.min(r_top[istow]))
                                       if istow.any() else 1e9,
                                       float(np.min(r_cable[ok]))))))
        r1 = int(min(H, math.ceil(float(np.max(r_deck[ok]))) + 2))
        if r1 <= r0:
            return
        yy = np.arange(r0, r1, dtype=f32)[:, None]
        vis = zbuf[r0:r1, sl] > tz[None, :]
        dst = pidx[r0:r1, sl]
        fb = np.rint(np.clip(tz / far, 0.0, 1.0) * (NFOG - 1)).astype(np.int32)
        thick = np.maximum(1.0, 11.0 * sc)

        def paint(top, bot, cls, sel):
            m = (yy >= top[None, :]) & (yy <= bot[None, :]) & sel[None, :] & vis
            np.putmask(dst, m, (br_pidx[cls] + fb)[None, :])

        paint(r_cable, r_deck, 2, issus)                      # the curtain
        paint(r_top, base + 2.0 * sc, 0, istow)               # towers and piers
        paint(r_deck, r_deck + thick, 1, ok)                  # roadway
        paint(r_cable - 0.5, r_cable + 0.5, 2, ok)            # main cable

    def draw_birds(t, camu, camv, camz, fu, fv, ru, rv):
        pose = bird_masks[int(t * 6.0) % len(bird_masks)]
        bh, bw = pose.shape
        for i in range(nbirds):
            a = 2.0 * math.pi * bird_rate[i] * t + bird_ph[i]
            bu_ = loop_u + bird_r[i] * math.sin(a) * 1.6
            bv_ = loop_v + bird_r[i] * math.cos(a) * 1.3
            bz = (args.altitude + bird_dz[i]
                  + 22.0 * math.sin(a * 3.0 + bird_flap[i]))
            du, dv = bu_ - camu, bv_ - camv
            zc = du * fu + dv * fv
            if zc < near * 0.4 or zc > 3500.0:
                continue
            xs = 0.5 * W + focal * (du * ru + dv * rv) / zc
            # Same projection the terrain uses, horizon shear and all, or a
            # bird sits at a different attitude from the world behind it.
            ys = (float(horiz[min(W - 1, max(0, int(xs)))])
                  - focal * (bz - camz) / zc)
            x0, y0 = int(round(xs)) - bw // 2, int(round(ys)) - bh // 2
            if not (0 <= x0 and x0 + bw <= W and 0 <= y0 and y0 + bh <= H):
                continue
            if float(np.min(zbuf[y0:y0 + bh, x0:x0 + bw])) <= zc:
                continue
            fb = int(round(min(zc / far, 1.0) * (NFOG - 1)))
            np.putmask(pidx[y0:y0 + bh, x0:x0 + bw], pose, bird_pix + fb)

    def render(t, frame):
        # Local aliases for every scratch buffer this function writes through
        # with an augmented assignment. `buf += x` on a closure name does not
        # write through the buffer, it *rebinds the name* -- and because the
        # name is then local for the whole function, the first read of it
        # earlier in the frame raises instead of quietly doing the wrong
        # thing. Which is the good outcome; the bad one is when it does not.
        a_su, a_sv, a_tt, a_mi = su, sv, tt, miv
        a_bk, a_pi, a_ga, a_bp = bkey, pidx, gath, bump
        a_hz, a_buf = horiz, buf

        camu, camv, camz, psi, kappa, pitch = camera(t)
        fu, fv = math.cos(psi), math.sin(psi)
        ru, rv = -fv, fu                          # right hand, looking along f

        # Bank and pitch are a shear and a shift of the horizon rather than a
        # rotation of the rays. At a 27 degree vertical field that is well
        # inside where the approximation shows, and it makes both of them
        # free: every projection below already adds this per-column number.
        # A coordinated turn at this radius and speed really is banked about
        # eighteen degrees. Eighteen degrees is unusable here: the panel is
        # five times wider than it is tall, so the horizon crosses it with a
        # rise of five pixels per degree of roll and leaves through the corner
        # before you get to ten. What has to read is *which way* you are
        # banking and that it keeps changing, so the tilt is scaled to about a
        # sixth of true and clamped at six degrees -- which still walks the
        # horizon a third of the way up the panel from one edge to the other.
        roll = math.atan(max(min(kappa * 22.0 * args.bank, 0.105), -0.105))
        h0 = 0.5 * H + math.tan(pitch) * focal
        np.multiply(colf - 0.5 * W, f32(math.tan(roll)), out=horiz)
        a_hz += f32(h0)
        np.multiply(a_hz, f32(SKY_SUB), out=hrowf)
        np.rint(hrowf, out=hrowf)
        np.copyto(hrow, hrowf, casting="unsafe")
        np.subtract(SKY_MID + SKY0, hrow, out=skyoff)

        # Where the sun is on screen, worked out before anything is drawn
        # because two different things need it: the disc itself, and the
        # columns the glitter path is allowed to light. A sun behind you does
        # neither, and `fdot` is the whole test.
        fdot = sun_dir[0] * fu + sun_dir[1] * fv
        sun_x = sun_y = 0.0
        if fdot > 0.05:
            sun_x = 0.5 * W + focal * (sun_dir[0] * ru + sun_dir[1] * rv) / fdot
            sun_y = (float(horiz[min(W - 1, max(0, int(sun_x)))])
                     - focal * sun_dir[2] / fdot)
            # Slide the baked glitter path so its centre lands on the sun's
            # column. No arithmetic: the whole thing is one slice.
            goff = max(0, min(2 * W, int(round(1.5 * W - sun_x))))
            gsl = glint_field[:, goff:goff + W]
        else:
            gsl = glint_off

        # ---- the march -----------------------------------------------------
        # Every column at once, and not even a loop over depth: the whole
        # (steps x columns) grid is built in one go and the painter's ordering
        # falls out of a running minimum. A Python loop over depth steps would
        # be a couple of thousand numpy calls a frame, and on a Pi 3 a numpy
        # call costs about 80 us whatever size the array is.
        np.multiply(Zcol, colx * f32(ru) + f32(fu), out=su)
        a_su += f32(camu)
        np.multiply(Zcol, colx * f32(rv) + f32(fv), out=sv)
        a_sv += f32(camv)
        a_su *= inv_mx
        a_sv *= inv_my
        np.maximum(su, 0.0, out=su)
        np.minimum(su, f32(MW - 1.001), out=su)
        np.maximum(sv, 0.0, out=sv)
        np.minimum(sv, f32(MH - 1.001), out=sv)
        np.copyto(tmpi, sv, casting="unsafe")     # truncation, which is a floor
        np.multiply(tmpi, MW, out=miv)
        np.copyto(tmpi, su, casting="unsafe")
        a_mi += tmpi
        np.take(hflat, miv, out=hs)

        # Screen row of every sample, then the highest reached so far.
        np.subtract(hs, f32(camz), out=tt)
        a_tt *= invZ
        np.subtract(horiz, tt, out=tt)
        np.minimum.accumulate(tt, axis=0, out=tt)

        # Which depth step is visible in each pixel, without ever looping over
        # rows. Down the depth axis `tt` only ever decreases, so the number of
        # steps whose ceiling is still below a row *is* the index of the first
        # step that covers it -- and that count, for every row at once, is a
        # histogram of the ceilings followed by a cumulative sum. This is the
        # whole reason the effect fits in a frame.
        np.ceil(tt, out=tt)
        np.maximum(tt, 0.0, out=tt)
        np.minimum(tt, f32(H), out=tt)
        np.copyto(bkey, tt, casting="unsafe")
        a_bk *= W
        a_bk += colidx
        hist = np.bincount(bkey.reshape(-1), minlength=nbins)[:nbins]
        cum = np.cumsum(hist.reshape(H + 1, W)[:H], axis=0)
        np.subtract(N, cum, out=cum)
        np.copyto(idx, cum, casting="unsafe")

        # ---- surface, haze and water ----------------------------------------
        np.multiply(idx, W, out=gath)
        a_ga += colidx
        np.take(miflat, gath, out=cell)           # the map cell under the pixel
        np.take(cflat, cell, out=pidx)            # its class and shade
        a_pi *= NFOG
        np.take(fogb, idx, out=gath)              # its haze band
        a_pi += gath

        # Water, in the same integer. A brighter shade is +NFOG on the index,
        # so the chop and the sun's glitter are an integer add on the pixels
        # that are water rather than any colour arithmetic -- and they pick up
        # the right haze for their distance for free.
        #
        # Both tests are made against the uint8 noise where it lies, with a
        # uint8 threshold, so nothing is widened to int32 on the way: the
        # glitter's per-column threshold is a uint8 array of 255s outside the
        # sun's column, and 255 is a number the noise cannot beat.
        ox, oy = int(t * 11.0) % nw, int(t * 3.0) % nh
        ox2, oy2 = int(t * 5.0) % nw, int(t * 2.0) % nh
        np.greater(tex1[oy:oy + H, ox:ox + W], gsl, out=mask)
        np.greater(tex2[oy2:oy2 + H, ox2:ox2 + W], np.uint8(206), out=aux8)
        np.copyto(bump, mask)
        a_bp *= 2                                 # glitter is two shades up
        a_bp += aux8
        a_bp *= NFOG
        np.less(pidx, watermax, out=mask)
        a_bp *= mask
        a_pi += bump

        # Sky where the march found nothing: the same table, indexed by how
        # far above the horizon the pixel is, so it is one gather for the
        # whole frame and there is no compositing pass anywhere.
        np.add(rows_i3, skyoff, out=gath)
        np.maximum(gath, SKY0, out=gath)
        np.minimum(gath, SKY0 + NSKY - 1, out=gath)
        np.equal(idx, N, out=mask)
        # putmask, not copyto(where=): three times quicker for the same
        # traffic, because copyto's masked path is a scalar loop. Shapes
        # match, so there is no repeat to reason about.
        np.putmask(pidx, mask, gath)

        if need_z:
            np.take(Zbuf, idx, out=zbuf)
        if args.bridge:
            draw_bridge(camu, camv, camz, fu, fv, ru, rv)
        if nbirds:
            draw_birds(t, camu, camv, camz, fu, fv, ru, rv)

        # Re-derive what is still sky now that the bridge and the birds have
        # been written in, because the sun is drawn through that mask and a
        # sun shining through the roadway is the sort of thing nobody notices
        # until they see it once. Only worth a pass if something was drawn.
        if need_z:
            np.greater_equal(pidx, SKY0, out=mask)
        np.take(pal, pidx, axis=0, out=buf)

        # ---- the sun, seen through the sky -----------------------------------
        # `mask` is still the sky mask, which is exactly the depth test the sun
        # needs: it is at infinity, so anything at all in front of it wins --
        # including the bridge deck, which is the point.
        if fdot > 0.05:
            x0, y0 = int(round(sun_x)) - sun_c, int(round(sun_y)) - sun_c
            cx0, cy0 = max(0, x0), max(0, y0)
            cx1, cy1 = min(W, x0 + sun_w), min(H, y0 + sun_h)
            if cx1 > cx0 and cy1 > cy0:
                sub = buf[cy0:cy1, cx0:cx1]
                sub += (sun_spr[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
                        * mask[cy0:cy1, cx0:cx1, None])
                # The only thing in the frame that can go over full scale, so
                # it is also the only thing that gets clipped. Clamping the
                # whole frame instead was two more passes over 61440 floats
                # for pixels that were already in range by construction: the
                # palette is built clipped and nothing else here adds.
                np.minimum(sub, 254.0, out=sub)

        # The wing, and the dither, in two passes rather than three: the
        # dither is folded into the wing's premultiplied colour at build time,
        # since `buf * inv + pre + dither` is `buf * inv + (pre + dither)`.
        if args.wing:
            a_buf *= wing_inv
            a_buf += wing_dith
        else:
            a_buf += dith
        np.copyto(out, buf, casting="unsafe")     # truncates, as dither expects
        return out

    return render


def main():
    # 30 fps. Nothing here moves fast enough to want more, and it doubles the
    # per-frame budget on the Pi this has to fit inside.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
