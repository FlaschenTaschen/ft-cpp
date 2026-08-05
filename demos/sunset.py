#!/usr/bin/env python3
"""Driving west into a San Francisco sunset.

The Great Highway out towards Ocean Beach: a road running away to a vanishing
point over the Pacific, the sun going down into the water with its glitter
path pointing back at you, Sutro Tower silhouetted on the ridge off to the
left, and Karl the Fog rolling in off the ocean to swallow it.

Built the way floor.py is. Every screen row below the horizon looks at the
water at one fixed depth, so depth, the step across the wave texture, the
road's width in pixels, the dashes' visibility and the distance haze are all
per-row constants worked out once. The sky is stronger still: it only changes
when the sun has sunk a quarter of a pixel or the ridge has drifted a whole
one, so it is baked and re-used for a second or two at a time. A frame is
then one gather, a few whole-array multiply-adds, and a cast.

Run:  python3 sunset.py --host 127.0.0.1
      python3 sunset.py --speed 24 --fog 1.4 --sun 0.7
      python3 sunset.py --no-fog --no-tower --dither 0
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


def make_backdrop(W, sky_rows, sun_x, args, rng):
    """Ridge and tower as a W-periodic alpha strip, doubled so it wraps.

    Everything at the horizon that is neither sky nor water lives in here, so
    the parallax layer costs a slice at an offset rather than a redraw.
    """
    a = np.zeros((sky_rows, W), f32)
    rgb = np.zeros((sky_rows, W, 3), f32)
    beacons = []
    if sky_rows < 4:
        return np.tile(a, 2), np.tile(rgb, (1, 2, 1)), beacons

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

    if args.tower:
        spr = scale_sprite(SUTRO, int(round(args.tower_h * sky_rows)))
        th_full = spr.shape[0]
        # Anchor the base on the ridge and let the top leave the frame, rather
        # than shrinking to fit. Clamping y0 up to 0 would pin the *tip* to the
        # top of the panel and cut the legs off instead, which reads as a
        # floating aerial.
        y0 = int(round(top[int(cx) % W])) + 1 - th_full
        crop = max(0, -y0)
        if crop:
            spr = spr[crop:]
        y0 = max(0, min(y0 + crop, sky_rows - spr.shape[0]))
        th, tw = spr.shape
        cols = (np.arange(tw) + int(cx) - tw // 2) % W
        sub = a[y0:y0 + th]
        sub[:, cols] = np.maximum(sub[:, cols], spr.astype(f32))
        tcol = np.empty((th, tw, 3), f32)
        tcol[:] = TOWER
        # Rim light on whichever side faces the sun: the *outermost* member of
        # each row, not every member with a gap beside it -- on a one-pixel
        # lattice that second reading lights the whole tower and it stops
        # being a silhouette at all. One lit edge separates it from a bright
        # sky far better than flat black, and is what the real tower does at
        # this hour.
        for r in range(th):
            lit = np.nonzero(spr[r])[0]
            if len(lit):
                tcol[r, lit[-1] if sun_x > cx else lit[0]] = TOWER_RIM
        m = spr[..., None].astype(f32)
        dst = rgb[y0:y0 + th]
        dst[:, cols] = dst[:, cols] * (1.0 - m) + tcol * m
        # One light on the tip, one on each end of the top platform -- and on
        # a small panel only the tip. Four red pixels on a seven-pixel-wide
        # tower stop being warning lights and become the whole object.
        # Beacon rows are positions in the *uncropped* sprite, so shift them by
        # the crop and drop any that went off the top with the spires.
        for r in (BEACON_ROWS if th_full >= 14 else BEACON_ROWS[:1]):
            ry = min(th_full - 1, (r * th_full) // len(SUTRO)) - crop
            if ry < 0 or ry >= th:
                continue
            lit = np.nonzero(spr[ry])[0]
            if not len(lit):
                continue
            for c in ((lit[len(lit) // 2],) if r == 0 else (lit[0], lit[-1])):
                beacons.append((y0 + ry, int(cols[c])))

    # Noise along the base so the headland does not read as a cut-out; it is
    # fifteen kilometres away across the water.
    a *= 0.82 + 0.18 * noise2(rng, sky_rows, W, (24, 9, 3))
    return np.tile(a, 2), np.tile(rgb, (1, 2, 1)), beacons


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

    # ---- backdrop and sun --------------------------------------------------
    bd_a, bd_rgb, beacons = make_backdrop(W, sky_rows, sun_x, args, rng)
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
                px = buf[by, (bx - off) % W]
                px += (BEACON - px) * blink

        np.add(buf, dith, out=buf)
        np.copyto(out, buf, casting="unsafe")   # truncates, as dither expects
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
