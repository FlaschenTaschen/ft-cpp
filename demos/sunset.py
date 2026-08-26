#!/usr/bin/env python3
"""Driving west into a San Francisco sunset.

The Great Highway out towards Ocean Beach: a road running away to a vanishing
point over the Pacific, the sun going down into the water with its glitter
path pointing back at you, Sutro Tower silhouetted on the ridge off to the
left, and Karl the Fog rolling in off the ocean to swallow it.

It is a journey, not a backdrop. Two storey stucco houses and wind-cut trees
stand along both sides of the road, growing and sliding out of frame as you
come up on them; after a few blocks they thin out into gaps and cross
streets, then into dune grass and low sand, and then the sides open out
altogether and it is just the beach and the water under the sun. Keep
driving and the town comes round again. Sutro grows the whole way in.

Built the way floor.py is. Every screen row below the horizon looks at the
water at one fixed depth, so depth, the step across the wave texture, the
road's width in pixels, the dashes' visibility and the distance haze are all
per-row constants worked out once. The scenery is the same trick turned
ninety degrees: everything beside the road stands on one plane at a fixed
distance from the centreline, so every screen *column* looks at that plane at
one fixed depth, and the wall becomes a one-dimensional height texture read
once per column. The sky is stronger still: it only changes when the sun has
sunk a quarter of a pixel or the ridge has drifted a whole one, so it is
baked and re-used for a second or two at a time. A frame is then two gathers,
a few whole-array multiply-adds, and a cast.

Run:  python3 sunset.py --host 127.0.0.1
      python3 sunset.py --speed 24 --fog 1.4 --sun 0.7
      python3 sunset.py --journey 90 --setback 9
      python3 sunset.py --no-fog --no-tower --no-scenery --dither 0
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
BIG = 1 << 16                          # keeps texture coords positive

NOISE_H = 128                          # water noise; both powers of two, so
NOISE_W = 256                          # wrapping is a mask not a modulo
FOG_N = 512                            # fog's horizontal profile, wrapping

SUN_ASPECT = 1.5                       # a low sun is refracted wider than tall

# Sutro Tower: three prongs on a slim lattice body with flanged platforms,
# stepping out to a splayed tripod base. Those are the whole read -- without
# the trident top and the stepped taper this is any old transmitter mast, and
# with them it is recognisable at thirteen pixels wide, which is the entire
# reason for putting it on a wall in San Francisco.
#
# Drawn at twenty rows because that is what it comes out at on 320x64 with
# the default ridge height, so on the real panel it is used pixel for pixel.
# A sprite that always needs resampling always looks resampled.
SUTRO = [
    "      X      ",
    "   X  X  X   ",
    "   X  X  X   ",
    "   X  X  X   ",
    "   X  X  X   ",
    "  XXXXXXXXX  ",
    "   X  X  X   ",
    "   X  X  X   ",
    "  XXXXXXXXX  ",
    "   X  X  X   ",
    "   X  X  X   ",
    "  XXXXXXXXX  ",
    "  X   X   X  ",
    "  X   X   X  ",
    " XXXXXXXXXXX ",
    " X    X    X ",
    " X    X    X ",
    "XXXXXXXXXXXXX",
    "X     X     X",
    "X     X     X",
]
BEACON_ROWS = (0, 5)                   # the red aircraft warning lights

# Sky, top to horizon: deep blue through violet and magenta into orange and
# gold. Sampled with a gamma, so the warm end is squeezed into the last few
# rows above the water, which is where a real sunset keeps it.
SKY = [(0.00, (6, 10, 44)), (0.28, (44, 24, 88)), (0.52, (120, 38, 104)),
       (0.72, (204, 78, 78)), (0.88, (246, 138, 52)), (1.00, (255, 200, 104))]

# Water, near to far: near-black in the foreground, through a navy-violet mid
# field, into the warm reflection of the sky at the horizon.
WATER = [(0.00, (10, 12, 30)), (0.35, (22, 22, 58)), (0.65, (66, 40, 74)),
         (0.86, (150, 84, 66)), (1.00, (206, 132, 84))]

# Asphalt, near to far. Same idea: dark tarmac in front, picking up the sky
# as it recedes.
ROAD = [(0.00, (22, 20, 28)), (0.45, (36, 31, 44)), (0.80, (78, 56, 62)),
        (1.00, (140, 96, 84))]

# Karl, near to far. Warmer and brighter out over the water where the sun is
# still inside it, colder and thinner in the foreground.
FOGC = [(0.00, (96, 96, 112)), (0.45, (146, 140, 152)),
        (0.80, (198, 178, 176)), (1.00, (226, 198, 176))]

# Roadside scenery, near colours. Everything beside the road is between you
# and the sun, so it is all silhouette: these are the *unhazed* body colours
# and they are deliberately barely lighter than the foreground water. What
# makes a house read as a house is its shape against the sky and the lit
# roofline, not its facade -- at three pixels a storey there is no facade.
#
# Index 0 is "nothing", which never gets drawn because its height is zero.
SCN_BODY = [(0, 0, 0),
            (32, 24, 32), (40, 29, 34), (25, 20, 30),   # stucco, three tones
            (17, 21, 26),                               # cypress / pine
            (48, 35, 37), (26, 28, 30)]                 # dune, scrub
# The roofline catches the last of the sun. One warm pixel along the top edge
# of each silhouette is worth more than any amount of detail below it, and it
# is what actually separates a row of houses from one dark mass.
SCN_RIM = [(0, 0, 0),
           (176, 100, 74), (196, 116, 82), (150, 84, 68),
           (118, 66, 66),
           (214, 146, 100), (146, 92, 74)]
SCN_HOUSE, SCN_TREE, SCN_DUNE, SCN_SCRUB = 1, 4, 5, 6
NHAZE = 16                             # quantised distance-haze levels
SCN_SUB = 8                            # sub-pixel steps in the wall raster

SUN_TOP = np.array((255, 248, 206), f32)
SUN_BOT = np.array((240, 74, 58), f32)
GLOW = np.array((255, 148, 70), f32)      # halo bled into the sky
GLINT = np.array((255, 196, 120), f32)    # the glitter path on the water
MARK = np.array((250, 226, 190), f32)     # road paint, warmed by the sun
RIDGE = np.array((14, 12, 26), f32)
CREST = np.array((116, 62, 74), f32)
TOWER = np.array((10, 9, 20), f32)
# Deliberately close to the body. A bright rim turns the sunward leg into a
# separate object floating beside a black tower rather than an edge on it.
TOWER_RIM = np.array((88, 50, 56), f32)
BEACON = np.array((255, 46, 30), f32)


# --------------------------------------------------------------------------
# Tables, all built once.
# --------------------------------------------------------------------------

def bayer(n=8):
    """The classic recursive ordered-dither matrix, returned in [0, 1)."""
    m = np.zeros((1, 1), f32)
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return ((m + 0.5) / (n * n)).astype(f32)


def ramp(stops, x):
    """Interpolate colour stops at positions `x`, in float.

    Not ds.gradient(), and the difference is the whole dithering story.
    gradient() returns a uint8 table, so every colour taken from it has
    already been rounded to eight bits -- and adding a dither to a value that
    is already an integer does nothing except add noise. The fractional part
    has to survive all the way to the final cast for ordered dithering to have
    anything to work with.
    """
    pos = np.array([p for p, _ in stops], f32)
    cols = np.array([c for _, c in stops], f32)
    return np.stack([np.interp(x, pos, cols[:, ch]) for ch in range(3)],
                    axis=-1).astype(f32)


def _octave(rng, h, w, cell):
    """One octave of periodic value noise, smoothstep interpolated."""
    gh, gw = max(1, h // cell), max(1, w // cell)
    g = rng.random((gh, gw)).astype(f32)

    def axis(n, gn):
        p = np.arange(n, dtype=f32) * (gn / n)
        i0 = np.floor(p).astype(np.int32) % gn
        fr = p - np.floor(p)
        return i0, (i0 + 1) % gn, (fr * fr * (3.0 - 2.0 * fr)).astype(f32)

    y0, y1, fy = axis(h, gh)
    x0, x1, fx = axis(w, gw)
    top = g[y0][:, x0] * (1 - fx) + g[y0][:, x1] * fx
    bot = g[y1][:, x0] * (1 - fx) + g[y1][:, x1] * fx
    return top * (1 - fy)[:, None] + bot * fy[:, None]


def noise2(rng, h, w, cells=(32, 12, 5, 2)):
    """Wrapping fractal noise, normalised to 0..1."""
    out = np.zeros((h, w), f32)
    amp, total = 1.0, 0.0
    for c in cells:
        out += amp * _octave(rng, h, w, c)
        total += amp
        amp *= 0.5
    out /= total
    out -= out.min()
    return (out / max(out.max(), 1e-6)).astype(f32)


def sstep(a, b, x):
    """Smoothstep from 0 at a to 1 at b."""
    t = min(max((x - a) / max(b - a, 1e-6), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def _band(u, a, b, rise, fall):
    """A soft-edged 0..1 window: up over [a-rise, a], down over [b, b+fall]."""
    return min(sstep(a - rise, a, u), 1.0 - sstep(b, b + fall, u))


# The journey, as a function of how far west you have driven. u is position
# along the road as a fraction of one cycle, so these are *places*, not times:
# the same stretch of road looks the same whenever you drive through it, and
# what is on the horizon now is what is beside you in a few seconds. That is
# the whole reason to bake the progression into a spatial texture rather than
# fade a parameter -- a fade would change the houses you are already level
# with, and the eye catches that immediately.
#
# Laid out symmetrically about the town so that the loop closes on open
# beach at both ends and reads as driving out, turning round, and driving out
# again rather than as a jump cut.
def city_at(u):
    return _band(u, 0.20, 0.56, 0.10, 0.16)


def dune_at(u):
    return max(_band(u, 0.06, 0.15, 0.05, 0.09),     # inland from the beach
               _band(u, 0.70, 0.88, 0.10, 0.08))     # and back out again


def _put(hgt, cix, tpu, p0, width, height, shape, cidx):
    """Stamp one object's silhouette into the height/colour tracks."""
    n = hgt.shape[0]
    i0 = int(round(p0 * tpu))
    w = max(1, int(round(width * tpu)))
    xs = (np.arange(w, dtype=f32) + 0.5) / w
    v = (height * shape(xs)).astype(f32)
    idx = (i0 + np.arange(w)) % n
    hit = v > hgt[idx]
    idx = idx[hit]
    hgt[idx] = v[hit]
    cix[idx] = cidx


def _house_shape(k, bay):
    """A flat-topped box with a lower garage end and maybe a bay window."""
    def shape(xs):
        v = np.where(xs < k, f32(0.74), f32(1.0))
        if bay:
            v = np.where(np.abs(xs - 0.62) < 0.13, f32(1.13), v)
        return v
    return shape


def _blob(power):
    def shape(xs):
        return np.sin(np.pi * np.clip(xs, 0.0, 1.0)) ** power
    return shape


def journey_tracks(rng, n, cycle, args):
    """Build the two roadsides as 1-D height and colour tracks.

    One texel is a fraction of a world unit along the road; the wall renderer
    reads them once per screen column. Heights are in eye heights (the camera
    sits at 1.0), so a two storey house is about three.
    """
    tpu = n / cycle
    hgt = np.zeros((2, n), f32)
    cix = np.zeros((2, n), np.int32)
    for s in range(2):
        p = rng.uniform(0.0, 30.0)
        frontage = 0.0
        while p < cycle:
            u = p / cycle
            city, dune = city_at(u), dune_at(u)
            r = rng.random()
            if r < city:
                if rng.random() < 0.17:
                    # A cypress or a pine in the front garden. Taller than the
                    # roofs and rounder, which is the whole difference at this
                    # size; the Sunset's street trees are wind-cut blobs.
                    w = rng.uniform(2.6, 4.4)
                    _put(hgt[s], cix[s], tpu, p, w,
                         rng.uniform(3.4, 4.8) * args.storey,
                         _blob(rng.uniform(0.32, 0.55)), SCN_TREE)
                else:
                    w = rng.uniform(4.5, 8.5)
                    _put(hgt[s], cix[s], tpu, p, w,
                         rng.uniform(2.3, 3.2) * args.storey,
                         _house_shape(rng.uniform(0.18, 0.34),
                                      rng.random() < 0.4),
                         SCN_HOUSE + rng.integers(0, 3))
                frontage += w
                # Gaps: a driveway between houses while the town is dense,
                # opening into vacant lots as it thins.
                gap = rng.uniform(0.5, 1.5) + (1.0 - city) * rng.uniform(2.0, 22.0)
                if frontage > rng.uniform(38.0, 60.0):
                    gap += rng.uniform(8.0, 15.0)     # a cross street
                    frontage = 0.0
            elif r < city + dune:
                if rng.random() < 0.42:
                    w = rng.uniform(9.0, 26.0)
                    _put(hgt[s], cix[s], tpu, p, w, rng.uniform(0.5, 1.5),
                         _blob(rng.uniform(1.1, 1.7)), SCN_DUNE)
                else:
                    w = rng.uniform(1.2, 3.0)
                    _put(hgt[s], cix[s], tpu, p, w, rng.uniform(0.3, 0.8),
                         _blob(rng.uniform(0.4, 0.8)), SCN_SCRUB)
                gap = rng.uniform(1.0, 7.0)
            else:
                gap = rng.uniform(4.0, 12.0)
            p += gap
    return hgt, cix


def mip_tracks(hgt, levels):
    """Progressively blurred copies of the height tracks.

    Near the vanishing point one screen column spans a hundred texels, and
    point sampling that is the aliasing floor.py needs a mip chain for: whole
    houses would blink in and out as the road scrolled. Averaging instead
    turns the far blocks into the continuous low ridge of rooftops that they
    actually look like from half a mile back, for one extra gather.
    """
    n = hgt.shape[1]
    out = np.empty((levels,) + hgt.shape, f32)
    out[0] = hgt
    for k in range(1, levels):
        w = 1 << k
        pad = np.concatenate([hgt[:, -w:], hgt, hgt[:, :w]], axis=1)
        c = np.zeros((hgt.shape[0], pad.shape[1] + 1), np.float64)
        np.cumsum(pad, axis=1, out=c[:, 1:])
        # Centred boxcar of width w, as a difference of running sums.
        o = w - w // 2
        out[k] = ((c[:, o + w:o + w + n] - c[:, o:o + n]) / w).astype(f32)
    return out


def scale_sprite(rows, height):
    """OR-downsample the ASCII sprite to `height` rows, keeping its aspect.

    Max-pooling rather than point sampling: on a 32-row panel the tower is ten
    pixels tall, and a one-pixel lattice sampled point-wise drops whole
    crossarms -- exactly the detail that makes it Sutro rather than a mast.
    Pooling keeps every member, just fatter.
    """
    src = np.array([[c != ' ' for c in r] for r in rows], bool)
    sh, sw = src.shape
    height = max(4, height)
    width = max(3, int(round(sw * height / float(sh))))
    if width % 2 == 0:                 # odd, so the centre prong stays centred
        width += 1
    if height > sh:
        # Larger than the source art, for panels with more than 64 rows. Until
        # this existed the height was silently clamped to the sprite's own 20
        # rows, so --tower-h above about 0.7 did nothing at all on a 64 row
        # panel.
        #
        # Do not reach for it to make Sutro more legible here -- that was
        # tried, both ways, and both are worse. Drawing it large and letting
        # the spires run off the top crops away the three prongs, which are
        # the entire identity of the thing; what is left reads as a water
        # tower. And upscaling a one-pixel lattice by nearest neighbour
        # doubles the width of every member, so it gets chunkier rather than
        # bigger, losing the delicacy that reads as Sutro at a glance. A
        # genuinely larger tower needs source art with more rows, not a scale
        # factor. The default deliberately stays under 1.0.
        ri = np.minimum((np.arange(height) * sh) // height, sh - 1)
        ci = np.minimum((np.arange(width) * sw) // width, sw - 1)
        return src[ri][:, ci]
    ri = (np.arange(sh) * height) // sh
    ci = (np.arange(sw) * width) // sw
    out = np.zeros((height, width), bool)
    for yy in range(sh):
        for xx in range(sw):
            if src[yy, xx]:
                out[ri[yy], ci[xx]] = True
    return out


def make_backdrop(W, sky_rows, args, rng):
    """The ridge as a W-periodic alpha strip, doubled so it wraps.

    Everything at the horizon that is neither sky nor water lives in here, so
    the parallax layer costs a slice at an offset rather than a redraw. The
    tower used to be baked in with it; it is drawn per frame now, because it
    changes size as you approach and rebaking the whole strip for that would
    cost more than drawing a thirteen pixel sprite ever can.
    """
    a = np.zeros((sky_rows, W), f32)
    rgb = np.zeros((sky_rows, W, 3), f32)
    if sky_rows < 4:
        return np.tile(a, 2), np.tile(rgb, (1, 2, 1)), sky_rows - 1

    # A broad headland, tallest a fifth of the way across and running down to
    # the horizon at both ends, with a lower shoulder behind it. Heights are
    # fractions of the sky band, not pixels: on 128x32 there are fourteen rows
    # of sky in total, and a ridge measured in pixels is either invisible or
    # the entire scene.
    x = np.arange(W, dtype=f32)
    cx = args.tower_x * W
    d = np.clip((x - cx) / (0.19 * W), -1.0, 1.0)
    # Clamp before the power: cos() lands a hair below zero at the clipped
    # ends and a negative base to a fractional power is NaN, which then
    # poisons every blend downstream of it.
    hill = np.maximum(np.cos(d * (0.5 * np.pi)), 0.0) ** 1.6
    d2 = np.clip((x - cx - 0.17 * W) / (0.13 * W), -1.0, 1.0)
    hill = np.maximum(hill, 0.45 * np.cos(d2 * (0.5 * np.pi)) ** 2)
    # Off the ends of the headland the horizon must be open water, so the
    # ridge has to reach exactly zero rather than leave a one-pixel land line
    # right across the panel.
    top = sky_rows - hill * (args.hill * sky_rows)

    rows = np.arange(sky_rows, dtype=f32)[:, None]
    a[:] = np.clip(rows - top[None, :] + 1.0, 0.0, 1.0)
    rgb[:] = RIDGE
    # The crest catches the sun, so the ridge line stays an edge instead of
    # the hill and the dark end of the sky merging into one shape.
    crest = np.clip(1.0 - (rows - top[None, :]), 0.0, 1.0) * a
    rgb += (CREST - rgb) * (crest * 0.85)[..., None]

    # Noise along the base so the headland does not read as a cut-out; it is
    # fifteen kilometres away across the water.
    a *= 0.82 + 0.18 * noise2(rng, sky_rows, W, (24, 9, 3))
    return (np.tile(a, 2), np.tile(rgb, (1, 2, 1)),
            int(round(top[int(cx) % W])))


def tower_sprites(W, sun_x, cx, hmin, hmax):
    """Sutro at every whole pixel height from hmin to hmax.

    One sprite per row count, built once, because the tower approaches: its
    height is a continuous function of distance and it has to be resampled
    somewhere. Doing it here costs a millisecond of start-up; doing it per
    frame would cost a millisecond a frame, and on a Pi 3 that is a twentieth
    of the whole budget for one sprite.

    Each entry carries the mask, the already-lit colours and the beacon
    positions, so drawing one is a single masked copy plus two pixel writes.
    """
    table = []
    for h in range(hmin, hmax + 1):
        spr = scale_sprite(SUTRO, h)
        th, tw = spr.shape
        rgb = np.empty((th, tw, 3), f32)
        rgb[:] = TOWER
        # Rim light on whichever side faces the sun: the *outermost* member of
        # each row, not every member with a gap beside it -- on a one-pixel
        # lattice that second reading lights the whole tower and it stops
        # being a silhouette at all. One lit edge separates it from a bright
        # sky far better than flat black, and is what the real tower does at
        # this hour.
        for r in range(th):
            lit = np.nonzero(spr[r])[0]
            if len(lit):
                rgb[r, lit[-1] if sun_x > cx else lit[0]] = TOWER_RIM
        # One light on the tip, one on each end of the top platform -- and on
        # a small panel only the tip. Four red pixels on a seven-pixel-wide
        # tower stop being warning lights and become the whole object.
        beacons = []
        for r in (BEACON_ROWS if th >= 14 else BEACON_ROWS[:1]):
            ry = min(th - 1, (r * th) // len(SUTRO))
            lit = np.nonzero(spr[ry])[0]
            if not len(lit):
                continue
            for c in ((lit[len(lit) // 2],) if r == 0 else (lit[0], lit[-1])):
                beacons.append((ry, int(c)))
        # A contiguous three channel mask: np.copyto with a broadcast `where`
        # is twenty times slower than one with a real array, which is most of
        # a frame's budget on the Pi and nothing at all here.
        cols = (np.arange(tw) + int(cx) - tw // 2) % W
        table.append((th, tw, rgb, np.repeat(spr[..., None], 3, axis=2),
                      cols, beacons))
    return table


def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=16.0,
                    help="forward speed, world units/sec")
    ap.add_argument("--horizon", type=float, default=0.45,
                    help="horizon height as a fraction of the panel")
    # A fraction of the *sky band*, not of the width. Sized against the width,
    # the sun is taller than the panel has room for the moment the geometry
    # changes -- floor.py's focal-length lesson applied to the one object here
    # that has to fit inside thirty rows.
    # Off the vanishing point on purpose. Centred, the sun is directly behind
    # the road and its glitter path lands entirely on tarmac, which throws
    # away the best thing in the picture; put it to one side and the tower
    # gets the left of the panel and the glitter gets the right.
    ap.add_argument("--sun-x", type=float, default=0.70,
                    help="sun position as a fraction of the width")
    ap.add_argument("--sun", type=float, default=0.62,
                    help="sun radius as a fraction of the sky band")
    ap.add_argument("--sun-sink", type=float, default=0.85,
                    help="how far the sun settles, in sun radii")
    ap.add_argument("--fog", type=float, default=1.0,
                    help="fog density, 0..2ish")
    ap.add_argument("--no-fog", dest="fog_on", action="store_false",
                    help="clear evening, no Karl")
    ap.add_argument("--fog-period", type=float, default=47.0,
                    help="seconds for the fog to thicken and thin once")
    ap.add_argument("--no-tower", dest="tower", action="store_false",
                    help="leave Sutro Tower off the ridge")
    ap.add_argument("--tower-x", type=float, default=0.21,
                    help="ridge position as a fraction of the width")
    ap.add_argument("--tower-h", type=float, default=0.69,
                    help="tower height as a fraction of the sky band")
    ap.add_argument("--hill", type=float, default=0.24,
                    help="ridge height as a fraction of the sky band")
    ap.add_argument("--road", type=float, default=0.34,
                    help="road width at the bottom row, fraction of the width")
    ap.add_argument("--focal", type=float, default=0.35,
                    help="focal length as a fraction of the width")
    ap.add_argument("--glitter", type=float, default=1.0,
                    help="strength of the sun's glitter path")
    ap.add_argument("--parallax", type=float, default=0.4,
                    help="ridge drift in pixels/sec")
    ap.add_argument("--dither", type=float, default=1.0,
                    help="ordered dither depth in LSBs (0 = off)")
    # The journey. One cycle is houses, thinning, dunes, open beach and back
    # inland again, and it is a *distance*: the length of road it covers is
    # --journey times --speed, so winding the speed up drives past the same
    # town faster rather than rebuilding it.
    ap.add_argument("--journey", type=float, default=58.0,
                    help="seconds to drive one full cycle of the scenery")
    ap.add_argument("--journey-phase", type=float, default=0.0,
                    help="where in that cycle to start, 0..1")
    ap.add_argument("--no-scenery", dest="scenery", action="store_false",
                    help="empty roadsides, water all the way out")
    ap.add_argument("--setback", type=float, default=11.0,
                    help="distance from the centreline to the buildings, "
                         "in eye heights")
    ap.add_argument("--storey", type=float, default=1.0,
                    help="scale on building heights")
    # How small Sutro gets at its furthest, as a fraction of --tower-h. The
    # top of that range is all the room there is: see scale_sprite() on why
    # the sprite cannot usefully be drawn any bigger than its own artwork.
    ap.add_argument("--tower-far", type=float, default=0.30,
                    help="smallest tower height, fraction of --tower-h")
    ap.add_argument("--seed", type=int, default=7)


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)

    hy = float(np.clip(args.horizon * H, 2.0, H - 3.0))
    sky_rows = int(np.clip(round(hy), 2, H - 2))
    gnd_rows = H - sky_rows
    focal = max(args.focal * W, 4.0)
    sun_x = args.sun_x * W
    xf = np.arange(W, dtype=f32) + 0.5

    # ---- sky ------------------------------------------------------------
    ys = (np.arange(sky_rows, dtype=f32) + 0.5) / sky_rows
    sky0 = np.repeat(ramp(SKY, ys ** 1.7)[:, None, :], W, axis=1)

    # Thin stratus streaks over the Pacific. Half scenery, half tactics:
    # deliberate horizontal structure gives the eye something to read other
    # than the residual contour steps of a smooth gradient (see --dither).
    cn = noise2(rng, 8, W, (48, 17, 6))
    yy = np.arange(sky_rows, dtype=f32) + 0.5
    for k, (frac, thick, dark) in enumerate(
            ((0.46, 0.9, 0.30), (0.63, 0.7, 0.36),
             (0.77, 0.6, 0.34), (0.87, 0.5, 0.26))):
        prof = np.clip(1.0 - np.abs(yy - frac * sky_rows) / thick, 0.0, 1.0)
        band = (prof[:, None] * (0.35 + 0.65 * cn[k * 2]) * dark)[..., None]
        sky0 += (sky0 * f32(0.5) + np.array((70, 26, 40), f32) - sky0) * band

    horizon_col = ramp(SKY, np.array([1.0], f32))[0]

    # ---- the ground plane -------------------------------------------------
    y = np.arange(sky_rows, H, dtype=f32) + 0.5
    dy = np.maximum(y - hy, 0.5)[:, None]              # (gnd, 1)
    z = (focal / dy).astype(f32)                       # depth; eye height 1
    far = (z / (z + f32(9.0))).astype(f32)             # 0 near .. 1 at horizon
    water_c = ramp(WATER, far[:, 0])[:, None, :]
    road_c = ramp(ROAD, far[:, 0])[:, None, :]
    mark_c = np.repeat(MARK[None, None, :], gnd_rows, 0)

    # Distance haze, keyed to depth rather than to the screen row so it does
    # not need retuning when the panel changes shape. Folded into the surface
    # colours here rather than applied as a blend per frame.
    #
    # A high power on purpose. A gentle ramp spreads the sky's reflection over
    # the whole ground plane and the ocean turns into wet tarmac -- which is
    # what the first cut of this looked like. Real water at this hour is dark
    # nearly all the way out and then flares in a band a few rows deep at the
    # horizon, and it is that hard-edged band that makes it read as water.
    hz = (np.clip(far, 0.0, 1.0) ** f32(5.0) * f32(0.92))[..., None]
    haze_col = horizon_col * f32(0.82)
    water_c += (haze_col - water_c) * hz
    road_c += (haze_col - road_c) * hz
    mark_c += (haze_col - mark_c) * hz

    # Detail has to die off with distance: near the horizon one screen row
    # spans a hundred units of depth, so anything per-texel up there is pure
    # aliasing. Fading it is both cheaper and correct -- you cannot resolve a
    # single glint two kilometres out either.
    detail = (np.clip(1.0 - far, 0.0, 1.0) ** f32(1.6)) * (1.0 - hz[..., 0])

    # Wave texture. u across the row is world x at that depth, so the ripples
    # stretch towards the horizon on their own.
    u0 = ((xf - 0.5 * W)[None, :] * (z / focal) * f32(18.0)
          + f32(BIG)).astype(np.int32)
    wave_noise = noise2(rng, NOISE_H, NOISE_W, (40, 16, 6, 2)).ravel()
    wave_amp = (f32(1.1) * detail).astype(f32)         # (gnd, 1)
    wave_bias = (f32(1.0) - f32(0.5) * wave_amp).astype(f32)

    # Swell: broad bands of light and shade streaming in towards the beach.
    # This is where most of the water comes from, and it is a *per-row*
    # constant -- one sine of the row's depth -- because on a plane every row
    # is one depth. A depth-scrolled 2D texture cannot do this job here: keyed
    # tightly enough to show wave crests in the foreground it aliases to hash
    # across the whole mid field, which is the problem floor.py needs a whole
    # anisotropic mip chain to solve. Bands need no mip, only the same
    # can-this-row-resolve-it fade the dashes use.
    swell_p = f32(3.4)
    dz = np.abs(np.diff(z[:, 0], append=2 * z[-1, 0] - z[-2, 0]))[:, None]
    swell_amp = (np.clip(swell_p / (2.4 * np.maximum(dz, 1e-3)), 0.0, 1.0)
                 * detail * f32(0.6)).astype(f32)

    # Road. Its half width in pixels is linear in the distance below the
    # horizon, and the constant comes from how much of the panel the road
    # should fill at the bottom row -- so one number works on both geometries.
    half = ((args.road * W) / (2.0 * max(H - hy, 1.0)) * dy).astype(f32)
    linew = np.maximum(half * f32(0.06), f32(0.7))
    dashw = np.maximum(half * f32(0.05), f32(0.6))
    dash_period, dash_duty = f32(7.0), f32(0.42)

    # The road runs dead straight to a fixed vanishing point, which is both
    # the classic look and what makes the whole road layer a set of constants:
    # every mask below is built once here and only the dashes' on/off value
    # changes per frame. A steered road would have to rebuild them every
    # frame, and on a Pi 3 that alone is most of the budget.
    dx = np.abs(xf[None, :] - f32(0.5 * W))
    on = np.clip((half - dx) * f32(1.4), 0.0, 1.0).astype(f32)
    edge = np.clip((linew - np.abs(dx - half)) * f32(1.4), 0.0, 1.0)
    dash_shape = np.clip((dashw - dx) * f32(1.4), 0.0, 1.0)
    # Edge lines and centre dashes never overlap, so their masks can be summed
    # and each folded into its own premultiplied colour term.
    dmark = (mark_c - road_c) * on[..., None]
    inv_on = (1.0 - on)[..., None]
    water_pre = (water_c * inv_on).astype(f32)
    road_pre = (road_c * on[..., None] + dmark * edge[..., None]).astype(f32)

    # Dash periods per screen row. Where that exceeds about one the dashes
    # cannot be resolved and would strobe, so they fade to a continuous faint
    # line rather than flickering -- the same problem floor.py's mip chain
    # solves for its lane markings.
    dash_a = np.clip(dash_period / (2.0 * np.maximum(dz, 1e-3)), 0.0, 1.0)

    # Dashes and glitter each live in a column window, so their per-frame work
    # is a slice rather than the full width.
    def window(mask, pad):
        cols = np.nonzero(mask.max(axis=0) > 1e-3)[0]
        if not len(cols):
            return 0, 0
        return max(0, int(cols[0]) - pad), min(W, int(cols[-1]) + 1 + pad)

    dx0, dx1 = window(dash_shape, 1)
    dash_pre = (dmark[:, dx0:dx1] * dash_shape[:, dx0:dx1, None]).astype(f32)

    # The glitter path: broken highlights under the sun, narrow at the horizon
    # and fanning out towards the viewer, which is what a sun track on water
    # actually does.
    gw = f32(0.016) * W + f32(0.13) * W * (dy / max(H - hy, 1.0))
    gcol = np.exp(-((xf[None, :] - sun_x) / gw) ** 2).astype(f32)
    # Weighted towards the horizon, where the sun is, but hazed like anything
    # else out there. Deliberately *not* multiplied by `detail`: the glints
    # near the horizon do alias, and that reads as exactly the right kind of
    # twinkle rather than as an artefact.
    gcol *= (far ** f32(0.25)) * (f32(1.0) - hz[..., 0] * f32(0.6))
    gx0, gx1 = window(gcol - 0.02, 0)
    glint_pre = (GLINT * (gcol[:, gx0:gx1, None] * inv_on[:, gx0:gx1]
                          * f32(3.0 * args.glitter)))
    glint_thr = f32(0.58)

    # ---- fog --------------------------------------------------------------
    fog_col = np.empty((H, 1, 3), f32)
    fog_col[:sky_rows] = ramp(FOGC, np.array([1.0], f32))[0]
    fog_col[sky_rows:] = ramp(FOGC, far[:, 0])[:, None, :]
    # Depth weighting: full strength at the horizon, thinning towards the
    # viewer. Karl is out over the water, not in the car.
    fog_w = np.ones((H, 1), f32)
    fog_w[sky_rows:] = ((z / (z + f32(7.0))) ** f32(1.6)).astype(f32)
    fog_prof = np.stack([noise2(rng, 2, FOG_N, (37, 13, 5))[0],
                         noise2(rng, 2, FOG_N, (23, 9, 4))[0]])
    fog_patch = noise2(rng, 2, FOG_N, (60, 21, 8))[0]
    fog_x = np.arange(W, dtype=f32) * (FOG_N / float(W))
    fog_xp = np.arange(FOG_N, dtype=f32)
    # About five rows from clear to solid. Softer than that and the bank has
    # no top edge, so it stops being a bank rolling in and becomes a veil
    # someone laid over the whole picture.
    fog_soft = f32(6.0 / max(sky_rows, 1))
    rows_all = (np.arange(H, dtype=f32) + 0.5)[:, None]

    # ---- the roadside -------------------------------------------------------
    # Everything beside the road stands on one of two planes, parallel to the
    # road and --setback eye heights either side of the centreline. That one
    # decision is what makes the scenery affordable, because it turns the
    # whole approach-and-pass problem into per-column constants exactly the
    # way the water is per-row constants:
    #
    #   a plane at |world x| = S seen at screen column x is at depth
    #       z = S * focal / |x - W/2|,
    #   so the pixels-per-world-unit there is  |x - W/2| / S  and the foot of
    #   the wall is at row  hy + |x - W/2| / S.
    #
    # Both are fixed for the life of the demo. A column always looks at the
    # same distance down the road, so nothing has to be sorted, no object has
    # a size to choose, and an object's growth as it comes at you is not
    # animation at all -- it is the same silhouette read at a column further
    # from the vanishing point, one pixel at a time, which is as smooth as the
    # panel can be. What *does* change per frame is one number: how far along
    # the road you are.
    #
    # One cycle of the journey is a length of road rather than a stretch of
    # time, so winding --speed up drives past the same town faster.
    cycle = max(args.speed, 1.0) * max(args.journey, 4.0)
    scn = None
    if args.scenery and gnd_rows > 2:
        setback = max(args.setback, 1.5)
        dxx = np.maximum(np.abs(xf - f32(0.5 * W)), f32(0.75))
        scale_col = (dxx / setback).astype(f32)          # px per world unit
        base_col = (hy + scale_col).astype(f32)          # row of the wall foot
        z_col = (setback * focal / dxx).astype(f32)      # depth of that column

        # V is how far a pixel is above the foot of the wall in its column,
        # and it is the whole rasteriser: a pixel is inside the silhouette
        # exactly when 0 <= V < the height in pixels. Rows below the foot get
        # a sentinel instead, so that a single comparison covers both ends of
        # the span.
        #
        # Two decisions here are worth more than they look, and both were
        # measured. It is stored three channels wide -- wasteful on the face
        # of it -- because numpy's masked copy is twenty times slower given a
        # broadcast mask than a real one. And it is int16 in eighths of a
        # pixel rather than float, because the comparison against it is the
        # largest thing this whole feature adds to a frame and it is bound by
        # how fast the Pi can read it, so halving the bytes halves the cost.
        # An eighth of a pixel is finer than the panel can show.
        vv = base_col[None, :] - (np.arange(H, dtype=f32) + 0.5)[:, None]
        vv = np.where(vv >= 0.0, np.rint(vv * SCN_SUB), 32767.0)
        scn_v3 = np.repeat(vv[..., None], 3, axis=2).astype(np.int16)
        # Nothing below the furthest foot can ever be scenery, so the frame is
        # short by however many rows that is -- a fifth of it, typically.
        scn_r1 = int(min(H, np.ceil(base_col.max()) + 1))
        # Whether the foot of the wall can run off the bottom of the panel,
        # which is the only case in which a roofline row needs clamping.
        scn_clip = bool(base_col.max() >= scn_r1 - 1)

        ntex = 16384
        tpu = ntex / cycle                               # texels per unit
        hgt, cix = journey_tracks(rng, ntex, cycle, args)
        levels = 9
        htex = mip_tracks(hgt, levels)

        # Which blur to read is a per-column constant too: it is how many
        # texels of road that column spans, dz/dx = z/|x - W/2|.
        span = np.clip(z_col / dxx * tpu, 1.0, 1 << (levels - 1))
        lvl = np.clip(np.round(np.log2(span)), 0, levels - 1).astype(np.int32)
        sidec = (xf > 0.5 * W).astype(np.int32)
        scn_hbase = ((lvl * 2 + sidec) * ntex).astype(np.int32)
        scn_cbase = (sidec * ntex).astype(np.int32)
        scn_ztex = (z_col * tpu).astype(f32)
        scn_htex = np.ascontiguousarray(htex.reshape(-1))
        scn_ctex = np.ascontiguousarray(cix.reshape(-1))
        scn_mask = np.int32(ntex - 1)

        # Distance haze, and it does two jobs. It puts the far end of the
        # street *in* the air rather than on top of it, and it is the
        # anti-aliasing of last resort: by the point where one column covers a
        # whole house and the mip chain has run out, the silhouette has
        # already dissolved into the horizon, so whatever it does there cannot
        # be seen.
        #
        # Keyed to apparent size rather than to depth, which is not the
        # obvious choice and is the one that looks right. Fading towards the
        # colour of the horizon is only honest for something sitting on the
        # horizon: a half hazed house tall enough to reach up into the violet
        # part of the sky comes out lighter than the sky behind it and reads
        # as a sunlit cliff, which is exactly what the first cut of this did.
        # So nothing fades at all until it is down to about a storey a pixel.
        #
        # Sixteen quantised levels, folded into the palette rather than
        # applied to it, so a column's colour costs one gather and no
        # arithmetic whatsoever.
        hzw = np.clip(1.0 - (f32(3.0) * scale_col - f32(2.2)) / f32(7.0),
                      0.0, 1.0) ** f32(1.1)
        hzl = np.round(hzw * (NHAZE - 1)).astype(np.int32)
        ncol = len(SCN_BODY)
        scn_hzoff = (hzl * ncol).astype(np.int32)
        hzv = (np.arange(NHAZE, dtype=f32) / (NHAZE - 1))[:, None, None]
        far_col = horizon_col * f32(0.84)
        body = np.array(SCN_BODY, f32)[None]
        rimc = np.array(SCN_RIM, f32)[None]
        scn_body = (body + (far_col - body) * hzv).reshape(-1, 3).astype(f32)
        scn_rim = (rimc + (far_col - rimc) * hzv).reshape(-1, 3).astype(f32)

        scn = True

    # ---- backdrop and sun --------------------------------------------------
    bd_a, bd_rgb, ridge_row = make_backdrop(W, sky_rows, args, rng)
    tower_tab, tower_lo, tower_ratio = None, 0, 1.0
    if args.tower and sky_rows >= 6:
        # The one thing here that cannot be done by the same trick as the
        # rest. A tower is not a silhouette on a wall beside the road, it is
        # a sprite, and it has to be resampled to approach. It gets a depth
        # anyway -- a whole-pixel ladder of heights, walked one rung at a
        # time -- but the top of the ladder is fixed by the artwork rather
        # than by the geometry, so its "distance" is chosen to make the climb
        # smooth rather than to be metrically true. See scale_sprite().
        tower_hi = max(6, int(round(args.tower_h * sky_rows)))
        tower_lo = max(4, int(round(tower_hi * np.clip(args.tower_far,
                                                       0.15, 0.9))))
        tower_tab = tower_sprites(W, sun_x, args.tower_x * W,
                                  tower_lo, tower_hi)
        tower_ratio = float(tower_hi) / tower_lo

    ry = max(args.sun * sky_rows, 2.0)
    sun_q = (((xf - sun_x) / (ry * SUN_ASPECT)) ** 2).astype(f32)
    sun_y0 = hy - 0.18 * ry
    sink = args.sun_sink * ry

    # ---- dither ------------------------------------------------------------
    # A 320x30 sunset sky is the worst case for an 8-bit panel: long stretches
    # where the gradient climbs less than one code per pixel, which come out
    # as hard contour rings -- and worse on the wall than on a monitor,
    # because the panel's PWM makes its low codes the coarse ones. Ordered
    # dithering is the cheap, correct fix: one precomputed Bayer tile added
    # before the truncating cast turns each contour into a stipple the eye
    # integrates away, for one pass over the frame. The other half of the
    # answer is in the art rather than the numbers -- the sliced sun, the
    # stratus streaks and the banded water make the horizontal structure that
    # remains look deliberate instead of like a quantisation artefact.
    dith = np.tile(bayer(8), (H // 8 + 1, W // 8 + 1))[:H, :W, None] \
        .astype(f32) * f32(args.dither)

    # ---- buffers -----------------------------------------------------------
    buf = np.empty((H, W, 3), f32)
    out = np.empty((H, W, 3), np.uint8)
    sky_cache = np.empty((sky_rows, W, 3), f32)
    t3 = np.empty((H, W, 3), f32)
    ta = np.empty((H, W), f32)
    g1 = np.empty((gnd_rows, W), f32)
    gi = np.empty((gnd_rows, W), np.int32)
    gg = np.empty((gnd_rows, gx1 - gx0), f32)
    g3 = np.empty((gnd_rows, W, 3), f32)
    state = [None]
    if scn:
        s_pf = np.empty(W, f32)
        s_ti = np.empty(W, np.int32)
        s_gi = np.empty(W, np.int32)
        s_hw = np.empty(W, f32)
        s_hpx = np.empty(W, f32)
        s_top = np.empty(W, f32)
        s_h3 = np.empty((1, W, 3), np.int16)
        s_h3v = s_h3.reshape(W, 3)
        s_ci = np.empty(W, np.int32)
        s_col = np.empty((W, 3), f32)
        s_colb = s_col.reshape(1, W, 3)
        s_rimc = np.empty((W, 3), f32)
        s_m3 = np.empty((H, W, 3), bool)
        s_rt = np.empty(W, np.int32)
        s_ok = np.empty(W, bool)
        s_okb = np.empty(W, bool)

    def bake_sky(sy, off):
        """Sky, sun and ridge for one sun height and one parallax offset."""
        sc = sky_cache
        sc[:] = sky0
        if sky_rows > 1:
            r = np.sqrt(((yy - sy) / ry)[:, None] ** 2 + sun_q[None, :])
            glow = np.clip((f32(1.42) - r) * f32(2.6), 0.0, 1.0)
            sc += (GLOW - sc) * (glow * glow * 0.55)[..., None]
            # The retro sliced sun: horizontal gaps widening towards the
            # bottom. It is the right look, and on this panel it is also the
            # right engineering -- hard bands read cleanly at eight bits
            # where a smooth thirty-row disc would contour.
            s = np.clip((yy - sy) / ry * 0.5 + 0.5, 0.0, 1.0)
            slab = np.clip((((s ** 1.5 * 6.0) % 1.0) - (0.04 + 0.5 * s))
                           * 6.0, 0.0, 1.0)
            cov = np.clip((1.0 - r) * 7.0, 0.0, 1.0) * slab[:, None]
            disc = SUN_TOP + (SUN_BOT - SUN_TOP) * (s ** 0.72)[:, None]
            sc += (disc[:, None, :] - sc) * cov[..., None]
        a = bd_a[:, off:off + W, None]
        sc += (bd_rgb[:, off:off + W] - sc) * a
        np.clip(sc, 0.0, 253.0, out=sc)

    def render(t, frame):
        # -- sky: rebaked only when it has actually moved -------------------
        # Asymptotic sinking, quick at first and then settling just above the
        # water. A wall runs for hours; a sun that truly sets leaves a grey
        # rectangle for the rest of the evening.
        sy = sun_y0 + sink * (1.0 - np.exp(-t / 260.0))
        off = int(args.parallax * t) % W
        key = (int(sy * 4.0), off)
        if key != state[0]:
            state[0] = key
            bake_sky(sy, off)
        buf[:sky_rows] = sky_cache

        # -- water and road --------------------------------------------------
        np.add(u0, np.int32(t * 9.0), out=gi)
        np.bitwise_and(gi, NOISE_W - 1, out=gi)
        vi = ((z + f32(args.speed * t)) * f32(1.7)
              + f32(BIG)).astype(np.int32) & (NOISE_H - 1)
        np.add(gi, vi * NOISE_W, out=gi)
        np.take(wave_noise, gi, out=g1)

        gnd = buf[sky_rows:]
        # Glitter comes off the raw noise, before it is turned into shading:
        # the brightest crests in the column under the sun, and nothing else.
        np.subtract(g1[:, gx0:gx1], glint_thr, out=gg)
        np.clip(gg, 0.0, 1.0, out=gg)

        # Waves shade the water multiplicatively, so crests brighten and
        # troughs darken without shifting hue; road_pre is premultiplied by
        # the road mask, so surface, edge lines and haze are one add.
        np.multiply(g1, wave_amp, out=g1)
        np.add(g1, wave_bias, out=g1)
        swell = np.sin((z + f32(args.speed * t)) * f32(2.0 * np.pi) / swell_p)
        np.multiply(g1, f32(1.0) + swell * swell_amp, out=g1)
        np.multiply(water_pre, g1[..., None], out=g3)
        np.add(g3, road_pre, out=g3)
        np.copyto(gnd, g3)

        # Dashes: one value per row, streaming towards the viewer.
        phase = (z + f32(args.speed * t)) % dash_period
        dashv = np.where(phase < dash_period * dash_duty, f32(1.0), f32(0.0))
        dashv = dashv * dash_a + dash_duty * (f32(1.0) - dash_a)
        gnd[:, dx0:dx1] += dash_pre * dashv[..., None]

        gnd[:, gx0:gx1] += glint_pre * gg[..., None]
        np.clip(gnd, 0.0, 253.0, out=gnd)

        # How far west, and where that is in the journey. Reduced into one
        # cycle before it is scaled into texels: after an hour on the wall a
        # raw distance has lost enough of its float32 mantissa that the
        # roadside would visibly stutter.
        pos = (args.speed * t + args.journey_phase * cycle) % cycle
        u = pos / cycle

        # -- Sutro, coming up on you -----------------------------------------
        # Drawn before the roadside so that a house you are level with can
        # pass in front of it, and before Karl so that the fog can take its
        # legs and leave the prongs.
        beacons = ()
        if tower_tab is not None:
            grow = (sstep(0.04, 0.62, u) if u < 0.62
                    else 1.0 - sstep(0.62, 0.95, u))
            # Geometric in the growth, not linear: a rung a second early on
            # and a rung a second late is what "approaching steadily" looks
            # like, where equal *pixel* steps would crawl at the far end and
            # lurch at the near one.
            th_want = tower_lo * tower_ratio ** grow
            th, tw, trgb, tmask, tcols, tbea = tower_tab[
                min(len(tower_tab) - 1, max(0, int(round(th_want)) - tower_lo))]
            y0 = ridge_row + 1 - th
            crop = max(0, -y0)
            if crop < th:
                cols = (tcols - off) % W
                sub = buf[y0 + crop:y0 + th]
                reg = sub[:, cols]
                np.copyto(reg, trgb[crop:], where=tmask[crop:])
                sub[:, cols] = reg
                beacons = [(y0 + r, int(cols[c])) for r, c in tbea if r >= crop]

        # -- the roadside ------------------------------------------------------
        # Six gathers and one masked copy for both sides of the street, all
        # of it batched: there is no per-object anything here, because a
        # per-object Python loop over forty houses would cost more in numpy
        # call overhead alone than the entire rest of the frame.
        if scn:
            np.add(scn_ztex, f32(pos * tpu), out=s_pf)
            np.copyto(s_ti, s_pf, casting="unsafe")     # truncate to a texel
            np.bitwise_and(s_ti, scn_mask, out=s_ti)
            np.add(s_ti, scn_hbase, out=s_gi)           # ... at its blur level
            np.take(scn_htex, s_gi, out=s_hw)
            np.multiply(s_hw, scale_col, out=s_hpx)     # world units -> pixels
            np.subtract(base_col, s_hpx, out=s_top)
            # Nothing above the highest roofline on the panel can be scenery,
            # which is most of the frame once the town has thinned out.
            r0 = int(max(0.0, np.floor(s_top.min())))
            if r0 < scn_r1:
                np.multiply(s_hpx, f32(SCN_SUB), out=s_pf)
                np.minimum(s_pf, f32(30000.0), out=s_pf)
                np.copyto(s_h3v, s_pf[:, None], casting="unsafe")
                np.add(s_ti, scn_cbase, out=s_gi)
                np.take(scn_ctex, s_gi, out=s_ci)
                np.add(s_ci, scn_hzoff, out=s_ci)       # colour, hazed by depth
                np.take(scn_body, s_ci, axis=0, out=s_col)
                m = s_m3[r0:scn_r1]
                np.less(scn_v3[r0:scn_r1], s_h3, out=m)
                # putmask rather than copyto(where=): three times quicker for
                # the same traffic, because copyto's masked path is a scalar
                # loop. It repeats the source to fill the destination, and
                # one row of colours is exactly one row of the frame, so the
                # repeat lands each column's colour back on its own column.
                np.putmask(buf[r0:scn_r1], m, s_colb)

                # The lit roofline: one warm pixel on the topmost covered row
                # of every column, scattered in a single indexed store. It is
                # what turns a dark mass into a row of separate houses, and
                # it costs about as much as one row of the frame.
                # floor(top + 1/2) is the first covered row, and truncating
                # a positive float is a floor for free.
                np.add(s_top, f32(0.5), out=s_pf)
                np.maximum(s_pf, f32(0.0), out=s_pf)
                np.copyto(s_rt, s_pf, casting="unsafe")
                # Nothing gets a roofline if it is only a pixel or two high:
                # a bright line one pixel above the foot of the wall runs the
                # width of the panel and reads as a drawn line rather than as
                # dune grass. Nor if its roof is off the top of the panel --
                # s_pf went through a max() so that those columns land on row
                # zero rather than out of bounds, and this drops them again.
                #
                # Selecting the columns that do get one is worth its nonzero:
                # for most of the journey it is a minority of the panel, and
                # scattering all of them anyway measured slower.
                np.greater(s_hpx, f32(1.6), out=s_ok)
                np.greater_equal(s_top, f32(-0.5), out=s_okb)
                np.logical_and(s_ok, s_okb, out=s_ok)
                if scn_clip:            # only when the foot can leave the panel
                    np.less(s_rt, scn_r1, out=s_okb)
                    np.logical_and(s_ok, s_okb, out=s_ok)
                sel = np.nonzero(s_ok)[0]
                if len(sel):
                    np.take(scn_rim, s_ci[sel], axis=0, out=s_rimc[:len(sel)])
                    buf[s_rt[sel], sel] = s_rimc[:len(sel)]

        # -- Karl ------------------------------------------------------------
        if args.fog_on and args.fog > 0.0:
            dens = max(args.fog * (0.55 + 0.45 * np.sin(
                2.0 * np.pi * t / args.fog_period)), 0.0)
            # The bank's top edge is a drifting noise profile that rides up as
            # the fog thickens, which is how Karl takes the base of the tower
            # and leaves the prongs sticking out of the top.
            h = np.interp((fog_x + t * 6.0) % FOG_N, fog_xp, fog_prof[0])
            h += 0.55 * np.interp((fog_x * 0.41 - t * 2.3) % FOG_N,
                                  fog_xp, fog_prof[1])
            top = (hy + (0.55 - 0.75 * min(dens, 1.4)) * sky_rows
                   - (h - 0.75) * (0.36 * sky_rows)).astype(f32)
            # Patchiness across the width, drifting at its own rate: without
            # it the bank has one uniform density and looks painted on.
            patch = np.interp((fog_x * 0.63 + t * 3.1) % FOG_N,
                              fog_xp, fog_patch).astype(f32) * 0.7 + 0.45
            r0 = int(np.clip(np.floor(top.min()), 0, H))
            if r0 < H:
                a = ta[r0:]
                np.subtract(rows_all[r0:], top[None, :], out=a)
                np.multiply(a, fog_soft, out=a)
                np.clip(a, 0.0, 1.0, out=a)
                np.multiply(a, fog_w[r0:] * f32(min(dens, 1.25)), out=a)
                np.multiply(a, patch[None, :], out=a)
                # Never quite opaque. At peak density Karl should leave the
                # scene a ghost of itself, not a grey rectangle -- this runs
                # unattended and something has to stay on the wall.
                np.clip(a, 0.0, 0.9, out=a)
                v = buf[r0:]
                np.subtract(fog_col[r0:], v, out=t3[r0:])
                np.multiply(t3[r0:], a[..., None], out=t3[r0:])
                np.add(v, t3[r0:], out=v)

        # -- beacons, over the fog: they are meant to be seen through it -----
        if beacons:
            blink = f32(1.0) if (t % 2.1) < 0.55 else f32(0.16)
            for by, bx in beacons:
                px = buf[by, bx]
                px += (BEACON - px) * blink

        np.add(buf, dith, out=buf)
        np.copyto(out, buf, casting="unsafe")   # truncates, as dither expects
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
