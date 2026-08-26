#!/usr/bin/env python3
"""Golden Gate Bridge in the fog.

Two International Orange towers with their stepped Art Deco setbacks, the main
cables in a catenary that comes down to touch the deck at midspan, suspenders
dropping every few pixels, and a fog bank that rolls across the whole thing.
The proportions are the real ones -- 4200 ft of main span, 1125 ft of side
span either side, towers 526 ft over a deck 220 ft over the water -- which is
almost exactly 5:1, so a 320x64 panel is the shape this picture wants.

The fog is layered noise, not a simulation. Two tileable noise textures are
baked once and then scrolled across each other at different rates and scales;
their sum is a density, which is windowed by a rolling top edge and composited
with a depth weight so the bridge sits in front of the sky rather than behind
it. That is gathers, multiplies and one lerp per pixel -- a fluid step or a
whole-frame blur costs 40-200x more on the Pi 3 this runs on. The density is
computed on a half-size grid and the sky and bridge are cached per step of the
day cycle, which between them take the frame from 1.0 ms to 0.24 ms here,
around 10 ms on that Pi.

Run:  python3 goldengate.py --host 127.0.0.1
      python3 goldengate.py --time-of-day 18.5 --day-cycle 0
      python3 goldengate.py --fog 1.4 --fog-speed 0.5
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# The bridge in feet, which is where the proportions come from. Everything on
# screen is these numbers scaled by one pixels-per-foot; only the pieces that
# come out under a pixel wide -- the cables, the tower legs -- are widened to
# something the panel can actually show.
MAIN_SPAN = 4200.0
SIDE_SPAN = 1125.0
TOWER_ABOVE_DECK = 526.0
DECK_ABOVE_WATER = 220.0
TOTAL = 2 * SIDE_SPAN + MAIN_SPAN

# International Orange, and the shadowed side of it. The real paint is close
# to (198, 65, 36); on an LED panel the lit face wants to stay under full red
# or the tower blooms and loses its edges.
ORANGE = (198.0, 70.0, 38.0)
ORANGE_DARK = (116.0, 40.0, 22.0)
ORANGE_LIT = (232.0, 104.0, 56.0)

# Sky, fog and light through a day. hour, sky top, sky horizon, fog body,
# bridge tint, night level (drives the deck lamps and the tower beacons).
DAY = [
    (0.0,  (3, 5, 16),    (10, 14, 34),   (26, 30, 46),    (0.30, 0.34, 0.55), 1.00),
    (4.5,  (8, 11, 30),   (40, 34, 62),   (46, 46, 64),    (0.40, 0.40, 0.60), 0.90),
    (6.2,  (28, 34, 82),  (200, 108, 72), (124, 100, 108), (0.86, 0.58, 0.46), 0.38),
    (7.8,  (40, 82, 158), (208, 178, 168) , (172, 168, 172), (1.00, 0.86, 0.80), 0.06),
    (11.0, (26, 72, 150), (150, 186, 216), (200, 206, 212), (1.00, 1.00, 1.00), 0.00),
    (16.0, (32, 76, 148), (168, 190, 210), (202, 202, 202), (1.00, 0.96, 0.90), 0.00),
    (18.6, (46, 60, 130), (240, 152, 70),  (206, 166, 140), (1.00, 0.78, 0.54), 0.12),
    (20.0, (18, 20, 56),  (178, 72, 56),   (112, 82, 88),   (0.70, 0.46, 0.42), 0.52),
    (21.6, (6, 8, 22),    (22, 22, 46),    (36, 38, 54),    (0.33, 0.35, 0.56), 0.95),
    (24.0, (3, 5, 16),    (10, 14, 34),    (26, 30, 46),    (0.30, 0.34, 0.55), 1.00),
]

# 8x8 Bayer. The panel is 8 bit PWM per channel, and a fog bank over a sky
# wash is one big smooth gradient in the dark half of that range, which is
# exactly where the quantiser draws visible contour steps -- worse on the wall
# than on a monitor, because the LEDs have no dither of their own. One LSB of
# ordered noise added before the cast breaks the contours into a stipple that
# the eye integrates back into a smooth ramp.
BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], f32)


def add_arguments(ap):
    ap.add_argument("--fog", type=float, default=1.0,
                    help="fog density, 0 clear to ~1.6 socked in")
    ap.add_argument("--fog-speed", type=float, default=1.0,
                    help="how fast the bank drifts and breathes")
    ap.add_argument("--no-fog", action="store_true", help="bridge with no fog at all")
    ap.add_argument("--time-of-day", type=float, default=17.5,
                    help="starting hour, 0-24")
    ap.add_argument("--day-cycle", type=float, default=900.0,
                    help="seconds for a full 24h sky cycle (0 = hold the hour)")
    ap.add_argument("--seed", type=int, default=1937, help="fog noise seed")
    ap.add_argument("--no-dither", action="store_true",
                    help="quantise without ordered dither (shows the banding)")


# --------------------------------------------------------------------------
# Coverage primitives. Every part of the bridge is an axis-aligned span in one
# axis, so exact pixel-area coverage is one clip of an overlap -- which gives
# antialiasing for free, and matters a lot here: a 36 inch cable is a fifth of
# a pixel wide at this scale, and the catenary spends most of its length
# between rows.
# --------------------------------------------------------------------------

def span(v, lo, hi):
    """Fraction of each pixel centred at v that falls inside [lo, hi]."""
    return np.clip(np.minimum(v + 0.5, hi) - np.maximum(v - 0.5, lo), 0.0, 1.0)


def value_noise(rng, h, w, cy, cx, octaves=3):
    """Tileable value noise in 0..1, summed over `octaves` doubling scales."""
    out = np.zeros((h, w), f32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        out += amp * _upsample(rng.random((cy << o, cx << o)).astype(f32), h, w)
        total += amp
        amp *= 0.5
    out /= total
    return out


def _upsample(g, h, w):
    """Wrapping bilinear resize with a smoothstep on the weights."""
    def axis(n, m):
        f = (np.arange(n, dtype=f32) + 0.5) * (m / float(n)) - 0.5
        i0 = np.floor(f).astype(np.int64)
        t = (f - i0).astype(f32)
        t = t * t * (3.0 - 2.0 * t)
        return i0 % m, (i0 + 1) % m, t
    iy0, iy1, ty = axis(h, g.shape[0])
    ix0, ix1, tx = axis(w, g.shape[1])
    a = g[iy0][:, ix0] * (1 - tx) + g[iy0][:, ix1] * tx
    b = g[iy1][:, ix0] * (1 - tx) + g[iy1][:, ix1] * tx
    return a * (1 - ty)[:, None] + b * ty[:, None]


def noise_1d(rng, n, cells, octaves=3):
    """Tileable 1D value noise in 0..1."""
    return value_noise(rng, 1, n, 1, cells, octaves)[0]


# --------------------------------------------------------------------------
# The bridge. Built once into a premultiplied colour layer plus a coverage
# mask; render() only ever tints and composites it.
# --------------------------------------------------------------------------

def build_bridge(W, H):
    sx, sy = W / 320.0, H / 64.0
    ft = W / TOTAL                                  # pixels per foot

    x_l = SIDE_SPAN * ft                            # left tower
    x_r = (SIDE_SPAN + MAIN_SPAN) * ft              # right tower
    cx = 0.5 * (x_l + x_r)
    half = x_r - cx

    # Heights use the same feet-per-pixel as the widths, which is the happy
    # accident this demo exists for: at 320 px wide the waterline sits at 83%
    # of a 64 px panel, the deck lands on row 42 and the tower tops on row 16,
    # leaving a quarter of the frame as sky. Nothing is stretched.
    water = H * 0.83
    deck0 = water - DECK_ABOVE_WATER * ft
    top_y = max(1.0, deck0 - TOWER_ABOVE_DECK * ft)
    anchor = deck0 + 0.30 * (water - deck0)

    X = np.arange(W, dtype=f32)[None, :]
    Y = np.arange(H, dtype=f32)[:, None]

    # Deck: a shallow camber, high at midspan, sloping away to the approaches.
    camber = 2.0 * sy
    deck = deck0 - camber * (1.0 - ((X[0] - cx) / (0.5 * W)) ** 2)

    # Main cable: a parabola from tower top to tower top whose vertex sits on
    # the deck. On the real bridge the cable really does come down to the
    # roadway at midspan, and getting that right is half of what makes the
    # silhouette read as this bridge and not a generic suspension bridge.
    mid = deck[int(cx)] - 0.9 * sy
    cable = mid + (top_y - mid) * ((X[0] - cx) / half) ** 2
    # Side spans: away from the tower the cable falls to the anchorages. The
    # exponent under 1 keeps it steep where it leaves the saddle, so the slope
    # roughly matches the main span across the tower instead of kinking.
    u = np.clip((x_l - X[0]) / x_l, 0.0, None)
    left = top_y + (anchor - top_y) * u ** 0.8
    cable = np.where(X[0] < x_l, left, cable)
    u = np.clip((X[0] - x_r) / (W - 1 - x_r), 0.0, None)
    right = top_y + (anchor - top_y) * u ** 0.8
    cable = np.where(X[0] > x_r, right, cable)

    col = np.zeros((H, W, 3), f32)
    cov = np.zeros((H, W), f32)

    def paint(c, rgb):
        """Painter's algorithm over the layer, back to front."""
        c = c[..., None] if c.ndim == 2 else c
        col[...] *= 1.0 - c
        col[...] += np.asarray(rgb, f32) * c
        cov[...] += (1.0 - cov) * c[..., 0]

    # --- the bay, first, so everything stands in front of it ---------------
    # The viewpoint is the one every photograph of this bridge is taken from:
    # a little above the deck, on the Marin side. That puts the sea horizon
    # just under the roadway and the water surface at the piers well below it,
    # so the strait fills the bottom of the frame -- and gives the fog a dark
    # backdrop to be seen against, which a band of pale sky down there did not.
    sea_y = deck0 + 0.22 * (water - deck0)
    depth_ramp = np.clip((Y - sea_y) / max(H - sea_y, 1.0), 0.0, 1.0)
    sea = (np.asarray((30, 44, 58), f32) + np.asarray((-20, -30, -38), f32)
           * depth_ramp[..., None])
    # A little static chop, so the water is not a flat wash under the dither
    # and the fog has something with texture to sit on top of.
    chop = 1.0 + 0.16 * np.sin(Y * 2.3 + np.sin(X * 0.11) * 1.7)
    paint(span(Y, sea_y, H + 1.0) * np.ones_like(X), sea * chop[..., None])

    # --- suspenders --------------------------------------------------------
    # Real spacing is 50 ft, which is 2.5 px here and would fill solid, so
    # they are thinned to every ~6 px. They are the one element that wants to
    # stay a hard single pixel: this panel resolves it crisply, and a smeared
    # suspender just reads as dirt.
    step = max(3, int(round(6 * sx)))
    cols = np.zeros(W, f32)
    cols[step // 2::step] = 1.0
    cols[int(round(x_l))] = 0.0                     # not through the towers
    cols[int(round(x_r))] = 0.0
    sus = np.where(cols[None, :] > 0, 1.0, 0.0) * span(Y, cable[None, :], deck[None, :])
    paint(sus * 0.85, (150, 62, 34))

    # --- main cable --------------------------------------------------------
    paint(span(Y, cable[None, :] - 0.7, cable[None, :] + 0.7), ORANGE_LIT)

    # --- towers ------------------------------------------------------------
    for xc in (x_l, x_r):
        _tower(paint, X, Y, xc, top_y, deck0, water, sx, sy)

    # --- deck --------------------------------------------------------------
    thick = max(1.0, 1.6 * sy)
    paint(span(Y, deck[None, :], deck[None, :] + thick), (128, 52, 30))
    # The stiffening truss under the roadway: one darker row, which is what
    # gives the deck any thickness at all at this scale.
    paint(span(Y, deck[None, :] + thick, deck[None, :] + thick + 0.9 * sy) * 0.8,
          (58, 26, 16))

    # --- night lighting ----------------------------------------------------
    # Additive, applied per frame scaled by how dark it is. The deck lamps are
    # the sodium string along the roadway; the beacons are the red aircraft
    # warning lights on the tower tops.
    lamps = np.zeros((H, W, 3), f32)
    lstep = max(4, int(round(11 * sx)))
    ly = np.clip((deck - 1.5 * sy).astype(int), 0, H - 1)
    for x in range(lstep // 2, W, lstep):
        lamps[ly[x], x] += np.asarray((255, 186, 96), f32)
        if ly[x] + 1 < H:
            lamps[ly[x] + 1, x] += np.asarray((70, 46, 20), f32)
    beacons = [(max(0, int(top_y) - 1), int(round(xc))) for xc in (x_l, x_r)]

    return {"col": col, "cov": cov, "lamps": lamps, "beacons": beacons,
            "deck": deck0, "top": top_y, "water": water, "sea": sea_y}


def _tower(paint, X, Y, xc, top_y, deck_y, water_y, sx, sy):
    """One tower: two tapering legs, portal braces, a pier, a saddle.

    The Art Deco detail that makes a Golden Gate tower recognisable is the
    stepped setback -- the shafts get narrower in discrete jumps as they rise,
    rather than tapering smoothly -- plus the ladder of horizontal braces
    between the legs, whose openings get shorter towards the top.
    """
    height = deck_y - top_y
    gap = max(1.0, 2.0 * sx)                        # half the gap between legs
    hw0 = max(gap + 1.0, 5.2 * sx)                  # outer half width at deck

    # Four setbacks above the deck. Each one steps the outer face in; the gap
    # between the legs stays put, so the legs themselves thin out with height.
    steps = [(0.00, 1.00), (0.34, 0.90), (0.58, 0.79), (0.78, 0.68), (0.93, 0.58)]
    for i, (frac, scale) in enumerate(steps):
        y0 = deck_y - height * (steps[i + 1][0] if i + 1 < len(steps) else 1.0)
        y1 = deck_y - height * frac
        hw = hw0 * scale
        yc = span(Y, y0, y1)
        for sign, rgb in ((-1, ORANGE), (1, ORANGE_DARK)):
            lo, hi = sorted((sign * gap, sign * hw))
            paint(yc * span(X, xc + lo, xc + hi), rgb)

    # Portal braces. Spacing shrinks going up, which is how the real openings
    # sit; the lowest one is at deck level where the roadway passes through.
    bt = max(1.0, 1.1 * sy)
    for frac in (0.0, 0.30, 0.55, 0.75, 0.90, 1.0):
        y = deck_y - height * frac
        hw = hw0 * (1.0 - 0.42 * frac)
        paint(span(Y, y - bt, y) * span(X, xc - hw, xc + hw), (170, 60, 32))

    # Below the deck the legs continue, wider, into a pier at the waterline.
    sub = water_y - deck_y
    for sign, rgb in ((-1, ORANGE), (1, ORANGE_DARK)):
        lo, hi = sorted((sign * gap, sign * (hw0 + 0.4 * sx)))
        paint(span(Y, deck_y, water_y - 2.0 * sy) * span(X, xc + lo, xc + hi), rgb)
    paint(span(Y, deck_y + 0.45 * sub, deck_y + 0.45 * sub + bt)
          * span(X, xc - hw0, xc + hw0), (150, 54, 30))
    paint(span(Y, water_y - 2.2 * sy, water_y + 0.6 * sy)
          * span(X, xc - hw0 - 1.2 * sx, xc + hw0 + 1.2 * sx), (86, 78, 72))

    # The cable saddle capping each leg.
    paint(span(Y, top_y - 1.0 * sy, top_y) * span(X, xc - hw0 * 0.5, xc + hw0 * 0.5),
          ORANGE_LIT)


# --------------------------------------------------------------------------

def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    bridge = build_bridge(W, H)
    sy = H / 64.0

    # Day cycle as a 512 entry lookup over 24 hours. The frame only needs an
    # index, and -- more usefully -- everything that depends on the hour and
    # nothing else can be rebuilt once per index instead of once per frame.
    # 512 steps over a 900 s cycle is a step every 1.8 s, and a step moves the
    # sky by about one code, which the dither hides.
    n = 512
    hours = np.array([d[0] for d in DAY], f32)
    grid = np.linspace(0.0, 24.0, n, endpoint=False, dtype=f32)

    def lut(sel, dim):
        vals = np.array([np.atleast_1d(d[sel]) for d in DAY], f32)
        return np.stack([np.interp(grid, hours, vals[:, c]) for c in range(dim)], 1)

    sky_top, sky_hor = lut(1, 3), lut(2, 3)
    fog_lut, tint_lut = lut(3, 3), lut(4, 3)
    night_lut = lut(5, 1)[:, 0]

    # Sky gradient: top colour down to horizon colour at the sea horizon,
    # which is just under the deck. Below that the bay is opaque, so the sky
    # only has to be right down to that line.
    yy = np.arange(H, dtype=f32)
    vsky = np.clip(yy / max(bridge["sea"], 1.0), 0.0, 1.0)[:, None] ** 1.4
    # Fog body brightness falls off downwards: less light gets into the deep
    # part of the bank, and the gradient keeps it from looking like flat paint.
    vfog = np.clip(1.06 - 0.30 * (yy / max(H - 1.0, 1.0)), 0.0, None)[:, None, None]

    gain = 0.0 if args.no_fog else max(args.fog, 0.0)
    fog_on = gain > 0.0
    speed = args.fog_speed
    top_y = bridge["top"]

    # A thin permanent haze low down, so the scene never looks cut-out-clean
    # even when the bank has pulled back. It does not move, so it is folded
    # into the cached scene rather than paid for every frame.
    hz = (np.clip((yy - top_y) / max(H - top_y, 1.0), 0.0, 1.0)
          * (0.09 * gain))[:, None, None]

    # --- the fog field, at half resolution --------------------------------
    # Fog has no detail finer than a couple of pixels, so the density is
    # computed on a half-size grid and expanded with a nearest 2x2 repeat.
    # That is a 4x saving on the most expensive part of the frame, and the
    # only visible cost is a 2 px staircase on the top edge, which the soft
    # ramp and the dither together bury. Everything below is sized in this
    # half grid.
    fh, fw = (H + 1) // 2, (W + 1) // 2
    tw = 2 * fw
    # Two tiles, one broad and one fine, each baked at twice the (half) panel
    # width so the scroll takes a long time to repeat, then doubled in both
    # axes so any wrapped window is a plain contiguous slice -- no modulo
    # gather per pixel, just a view plus one lerp for sub-pixel motion.
    t1 = np.tile(value_noise(rng, fh, tw, 2, 5, 3), (2, 2))
    t2 = np.tile(value_noise(rng, fh, tw, 4, 11, 3), (2, 2))
    edge = np.tile(noise_1d(rng, tw, 3, 3), 2)      # shape of the fog's top
    bank = np.tile(noise_1d(rng, tw, 2, 2), 2)      # where the bank is thick

    # Depth weight: the bridge is in front of the fog and the sky is behind
    # all of it, so at the same density the bridge keeps more of itself.
    # Written so it still reaches full opacity -- a socked-in tower has to
    # disappear. Max-pooled into the half grid, or the one pixel wide
    # suspenders would fall between the samples.
    cov = bridge["cov"]
    pool = np.zeros((fh, fw), f32)
    for dy in (0, 1):
        for dx in (0, 1):
            s = cov[dy::2, dx::2]
            pool[:s.shape[0], :s.shape[1]] = np.maximum(pool[:s.shape[0], :s.shape[1]], s)
    depth = 1.0 - 0.24 * pool
    idepth = 1.0 - depth
    Yf = (np.arange(fh, dtype=f32) * 2.0 + 0.5)[:, None]     # in full-res rows

    # Ordered dither, offsets in (0,1) rather than (-0.5,0.5): the cast
    # truncates, so a mean of half an LSB is what turns truncation into
    # dithered rounding. See BAYER8.
    dither = np.tile((BAYER8 + 0.5) / 64.0,
                     ((H + 7) // 8, (W + 7) // 8))[:H, :W, None].astype(f32)
    if args.no_dither:
        dither = np.full((1, 1, 1), 0.5, f32)

    col_pre = bridge["col"] * cov[..., None]
    inv_cov = (1.0 - cov)[..., None]
    lamps, beacons = bridge["lamps"], bridge["beacons"]

    out = np.empty((H, W, 3), np.uint8)
    buf = np.empty((H, W, 3), f32)
    mix = np.empty((H, W, 3), f32)
    scene = np.empty((H, W, 3), f32)                # everything but the fog
    fogcol = np.empty((H, 1, 3), f32)
    n1 = np.empty((fh, fw), f32)                    # the fog scratch pair
    n2 = np.empty((fh, fw), f32)
    state = {"k": -1}

    def refresh(k):
        """Rebuild everything that depends only on the hour."""
        sc, fc = scene, fogcol      # local aliases: these buffers are written
        np.copyto(sc, (sky_top[k] + (sky_hor[k] - sky_top[k]) * vsky)[:, None, :])
        np.multiply(sc, inv_cov, out=sc)
        sc += col_pre * tint_lut[k]
        nite = night_lut[k]
        if nite > 0.01:
            sc += lamps * nite
        np.copyto(fc, fog_lut[k] * vfog)
        np.multiply(sc, 1.0 - hz, out=sc)           # static haze
        sc += fc * hz
        np.clip(sc, 0.0, 255.0, out=sc)

    def window(tex, ox, oy, dst):
        """A fw-wide window of tex at sub-pixel offset ox, integer row oy."""
        i, j = int(ox) % tw, int(oy) % fh
        np.subtract(tex[j:j + fh, i + 1:i + 1 + fw], tex[j:j + fh, i:i + fw], out=dst)
        dst *= f32(ox - np.floor(ox))
        dst += tex[j:j + fh, i:i + fw]

    def render(t, frame):
        # Local aliases for the scratch buffers: an augmented assignment to a
        # closure name would rebind the name instead of writing through it.
        b, p, q, m = buf, n1, n2, mix
        hour = args.time_of_day
        if args.day_cycle > 0:
            hour += t * 24.0 / args.day_cycle
        k = int(hour / 24.0 * n) % n
        if k != state["k"]:
            refresh(k)
            state["k"] = k

        np.copyto(b, scene)
        nite = night_lut[k]
        if nite > 0.01:
            # Aircraft warning lights on the tower tops, a slow double blink.
            bl = 0.5 + 0.5 * np.sin(t * 1.9)
            for y, x in beacons:
                b[y, x] += np.asarray((235, 40, 24), f32) * (bl * bl) * (0.3 + 0.7 * nite)

        if fog_on:
            ts = t * speed
            # Two densities crossing each other, plus a slow vertical drift on
            # the broader one so the bank churns instead of sliding rigidly.
            window(t1, ts * 2.2, ts * 0.3, p)
            window(t2, ts * 5.5, 0, q)
            p *= 0.62
            q *= 0.48
            p += q

            # Where the bank is thick right now, and how hard it is breathing.
            e = int(ts * 1.1) % tw
            env = 0.30 + 1.15 * bank[e:e + fw][None, :]
            breath = 0.95 + 0.45 * np.sin(ts * 0.0647) + 0.30 * np.sin(ts * 0.1523 + 1.3)
            # Top of the bank. Two slow sines summed rather than multiplied,
            # and over-scaled so the level clamps at both ends: the bank has
            # to actually reach over the towers sometimes and actually pull
            # back off the deck sometimes, not just wander around the middle.
            lvl = min(max(0.5 + 0.30 * np.sin(ts * 0.0455 + 0.7)
                          + 0.24 * np.sin(ts * 0.0261 + 2.1), 0.0), 1.0)
            base = top_y - 8.0 * sy + lvl * (bridge["water"] - top_y + 12.0 * sy)
            ei = int(ts * 1.6) % tw
            top = base - (14.0 * sy) * (edge[ei:ei + fw][None, :] - 0.5)

            # band: 0 above the fog top, ramping to 1 seven rows into it.
            np.subtract(Yf, top, out=q)
            q *= f32(1.0 / (7.0 * sy))
            np.clip(q, 0.0, 1.0, out=q)

            # A high bank is a thick one: tie density to level, or the fog
            # climbs over the towers as a transparent veil and nothing is ever
            # really lost in it.
            p *= 1.75
            p -= 0.40
            p *= f32(gain * breath * (0.55 + 0.95 * (1.0 - lvl)))
            p *= env
            p *= q
            np.clip(p, 0.0, 1.0, out=p)
            np.multiply(p, idepth, out=q)
            q += depth
            p *= q                                # the depth weight

            a = np.repeat(np.repeat(p, 2, 0), 2, 1)[:H, :W, None]
            np.subtract(fogcol, b, out=m)
            m *= a
            b += m

        # Dither before quantising -- see BAYER8.
        b += dither
        np.clip(b, 0.0, 255.0, out=b)
        np.copyto(out, b, casting="unsafe")
        return out

    return render


def main():
    # 30 fps: nothing here moves fast enough to want more, and it halves the
    # per-frame budget on the Pi.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
