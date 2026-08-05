#!/usr/bin/env python3
"""Karl the Fog pouring over the San Francisco hills, with Sutro Tower.

The calmest thing in this collection, and deliberately so: the ridgeline of
Twin Peaks and Mount Sutro against a dim evening sky, the city's lights in the
valley below, and the fog coming over the top — thickening until only the
tower's three prongs are left standing out of it, thinning again until the
whole ridge is clear. A full cycle takes minutes, not seconds.

**It is not a simulation.** A fluid solver, or any per-pixel relaxation, is
exactly the thing that does not fit on the Raspberry Pi driving this wall — a
whole-frame blur alone costs a large fraction of the frame budget there. So
the fog is two tileable value-noise textures baked in `build()` at different
scales, scrolled past each other at different rates and weighted differently
by row. The billowing comes from *domain warping*: the coarse layer's value
displaces the sample coordinate of the detail layer, so the fine structure is
dragged around by the large one and curls the way real fog does. Everything
per frame is a gather and a multiply, and nothing carries state between
frames — which also means the density can be driven straight off the clock
instead of emerging from an integration you have to sit and wait for.

Density is a slow function of time, not of the scroll. Fog that only scrolls
reads as a moving texture rather than as weather, so the *height* of the fog
surface and its *opacity* are both driven by a sum of sines with periods of a
hundred to two hundred and fifty seconds, saturated at the ends so it dwells
buried for a while and then clear for a while instead of sweeping smoothly
through the interesting part. The surface also drapes over the terrain: the
fog top is pulled up where the ridge is high, which is what makes it look
poured over the hills rather than laid across them like a ruler.

The other hardware constraint is the panel's 8 bits of PWM. This demo is
almost entirely soft grey gradient in the darker half of the range, which is
the worst case for banding, so every frame gets an 8x8 ordered Bayer dither
added before the truncation to uint8 — one add over the frame, and the steps
become noise fine enough that the eye integrates it away. The two big smooth
ramps, the sky and the fog colour, are also built in float rather than with
ds.gradient(): a ramp already snapped to whole 8-bit steps row by row has
nothing left for the dither to break up. Measured over the top 24 rows,
undithered leaves 16 of them a single flat value — visible stripes on the
wall — and dithered leaves 5. `--no-dither` is there to look at the
difference.

Costs 0.25 ms/frame here, which against the calibration demos scales to
roughly 11 ms on the wall's Pi 3 — between `floor` and `tunnel`. Runs at 24
fps by default; nothing in it moves fast enough to want more.

Run:  python3 karl.py --host 127.0.0.1
      python3 karl.py --density 1.5 --speed 2      # thick, and hurry it along
      python3 karl.py --no-tower --lights 0        # just the fog
      python3 karl.py --no-dither                  # the banding it avoids
"""

import sys

import numpy as np

import demoscene as ds

f32 = np.float32

# Noise texture size. Both powers of two so the wrap is a bitwise and rather
# than a modulo, which matters: the wrap happens per pixel, every frame.
TW = 512
TH = 64


def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=1.0,
                    help="multiplies every rate; slow is the entire point")
    ap.add_argument("--density", type=float, default=1.0,
                    help="how thick it gets; 0.6 is a clear night, 1.6 buries it")
    ap.add_argument("--no-tower", dest="tower", action="store_false",
                    help="leave Sutro Tower off the ridge")
    ap.add_argument("--lights", type=int, default=80,
                    help="city lights in the valley; 0 for none")
    ap.add_argument("--no-dither", dest="dither", action="store_false",
                    help="skip the ordered dither, to see the banding it fixes")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


# --------------------------------------------------------------------------
# Noise. Periodic in both axes, so scrolling it never shows a seam.
# --------------------------------------------------------------------------

def _upsample(g, h, w):
    """Smoothstep-interpolated upsample of a periodic lattice to (h, w)."""
    gh, gw = g.shape

    def axis(n, gn):
        u = (np.arange(n, dtype=f32) + f32(0.5)) * (f32(gn) / f32(n))
        i0 = np.floor(u).astype(np.int32)
        fr = (u - i0).astype(f32)
        return i0 % gn, (i0 + 1) % gn, fr * fr * (f32(3.0) - f32(2.0) * fr)

    y0, y1, fy = axis(h, gh)
    x0, x1, fx = axis(w, gw)
    top = g[y0][:, x0] * (1.0 - fx) + g[y0][:, x1] * fx
    bot = g[y1][:, x0] * (1.0 - fx) + g[y1][:, x1] * fx
    return (top * (1.0 - fy)[:, None] + bot * fy[:, None]).astype(f32)


def _fbm(rng, h, w, cy, cx, octaves=3, gain=0.55):
    """Fractal noise: octaves of lattice noise, each twice as fine and quieter."""
    out = np.zeros((h, w), f32)
    amp, norm = 1.0, 0.0
    for o in range(octaves):
        g = rng.random((max(2, cy << o), max(2, cx << o))).astype(f32)
        out += amp * _upsample(g, h, w)
        norm += amp
        amp *= gain
    out /= norm
    out -= out.min()
    m = float(out.max())
    if m > 1e-6:
        out /= m
    return out


# --------------------------------------------------------------------------
# The scene.
# --------------------------------------------------------------------------

def _ridge(W, H, rng):
    """Per-column skyline height, and the column of the Sutro Tower peak.

    Twin Peaks are two adjacent humps; Mount Sutro is the higher one to their
    right, and it is the one that carries the tower. The rest is low rolling
    ground so the panel does not read as one symmetric hill.
    """
    x = np.arange(W, dtype=f32) / f32(W)
    base = f32(0.62) * H                       # the ordinary ridge line
    y = np.full(W, base, f32)

    def bump(centre, width, height):
        d = (x - centre) / width
        y[:] -= height * H * np.exp(-d * d).astype(f32)

    bump(0.245, 0.052, 0.135)                  # Twin Peaks, north
    bump(0.320, 0.055, 0.145)                  # Twin Peaks, south
    sutro = 0.615
    # Mount Sutro is the tall one, but not so tall that the tower standing on
    # it runs out of panel: crest plus tower has to fit inside H.
    bump(sutro, 0.075, 0.20)
    bump(0.86, 0.13, 0.075)                    # a long shoulder falling away east
    bump(0.05, 0.10, 0.055)

    # A little roughness so the humps are not pure gaussians.
    y += (f32(0.018) * H) * np.sin(x * f32(37.0) + f32(1.3))
    y += (f32(0.011) * H) * np.sin(x * f32(91.0) + f32(4.1))
    y += (f32(0.008) * H) * rng.standard_normal(W).astype(f32)
    return np.clip(y, 1.0, H - 2.0), int(round(sutro * W))


def _tower_sprite(h):
    """Sutro Tower as a small mask: three prongs, two platforms, three legs.

    At this size the thing has to be built from its silhouette cues rather
    than drawn accurately — the centre prong standing taller than the two
    beside it, the pair of horizontal platforms under them, and the legs
    splaying out below. That reads as Sutro Tower and nothing else.
    """
    h = max(9, int(h))
    w = max(7, int(round(h * 0.72)) | 1)
    m = np.zeros((h, w), bool)
    cx = w // 2
    s = max(2, int(round(w * 0.24)))               # prong / platform half width
    prong_bot = max(3, int(round(h * 0.32)))
    side_top = max(1, int(round(h * 0.11)))

    m[0:prong_bot, cx] = True                      # centre prong, the tallest
    m[side_top:prong_bot, cx - s] = True
    m[side_top:prong_bot, cx + s] = True

    gap = max(1, int(round(h * 0.09)))
    for r in (prong_bot, min(h - 1, prong_bot + gap)):
        m[r, max(0, cx - s - 1):min(w, cx + s + 2)] = True

    body = min(h - 2, prong_bot + gap + 1)
    rows = np.arange(body, h)
    frac = (rows - body) / max(1, h - 1 - body)
    half = s + (max(s + 1, int(round(w * 0.48))) - s) * frac
    left = np.clip(np.round(cx - half).astype(int), 0, w - 1)
    right = np.clip(np.round(cx + half).astype(int), 0, w - 1)
    m[rows, left] = True
    m[rows, right] = True
    m[body:h, cx] = True
    for k in range(0, len(rows), max(4, h // 5)):  # lattice crossbars
        m[rows[k], left[k]:right[k] + 1] = True
    return m


def _ramp(stops, n):
    """Interpolate colour stops, in float and *not* rounded to uint8.

    ds.gradient() quantises, which is fine for a lookup table indexed by a
    noisy field but not for the two big smooth ramps here: a sky already
    snapped to whole 8-bit steps row by row has nothing left for the dither
    to break up, and bands whatever you do downstream.
    """
    pos = np.array([p for p, _ in stops], f32)
    cols = np.array([c for _, c in stops], f32)
    x = np.linspace(0.0, 1.0, n, dtype=f32)
    return np.stack([np.interp(x, pos, cols[:, ch]) for ch in range(3)],
                    axis=-1).astype(f32)


def _bayer(n=8):
    """The n x n ordered dither matrix, values in (0, 1)."""
    m = np.zeros((1, 1), f32)
    while m.shape[0] < n:                        # the usual recursive doubling
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + f32(0.5)) / f32(m.size)


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    dens = max(0.05, float(args.density))
    spd = float(args.speed)

    # --- terrain, tower, lights: all static, all baked into one background --
    ridge, sutro_x = _ridge(W, H, rng)
    rows = np.arange(H, dtype=f32)[:, None]

    sky = _ramp([(0.00, (2.5, 4.0, 11.0)), (0.45, (9.5, 13.0, 26.0)),
                 (0.80, (22.0, 26.0, 43.0)), (1.00, (33.0, 35.0, 51.0))], H)
    bg = np.repeat(sky[:, None, :], W, axis=1)

    # A handful of faint stars, only high up where the fog rarely reaches.
    for _ in range(max(4, W // 26)):
        sy = int(rng.integers(0, max(2, int(0.28 * H))))
        sx = int(rng.integers(0, W))
        bg[sy, sx] += f32(rng.uniform(18, 46))

    # Fractional coverage rather than a hard row test: a one-pixel staircase
    # across a 320 wide panel is the difference between a hill and a sawblade.
    cover = np.clip(rows - ridge[None, :] + 1.0, 0.0, 1.0)
    rock = np.array([7, 8, 13], f32)
    bg *= (1.0 - cover)[:, :, None]
    bg += rock * cover[:, :, None]

    # The foreground: nearer ground along the bottom, darker still, which puts
    # the city lights in a valley between two silhouettes.
    fore_y = H - (f32(0.10) * H
                  + f32(0.035) * H * np.sin(np.arange(W, dtype=f32) * f32(0.021) + 2.0)
                  + f32(0.02) * H * np.sin(np.arange(W, dtype=f32) * f32(0.077)))
    fcov = np.clip(rows - fore_y[None, :] + 1.0, 0.0, 1.0)
    bg *= (1.0 - fcov)[:, :, None]
    bg += np.array([3, 3, 6], f32) * fcov[:, :, None]

    # --- city lights -------------------------------------------------------
    if args.lights > 0:
        n = int(args.lights)
        lx = rng.integers(0, W, n)
        # Clustered towards the valley floor, thinning as they climb the hill.
        span = np.maximum(fore_y[lx] - ridge[lx] - 2.0, 1.0)
        ly = np.clip((ridge[lx] + 2.0 + span * rng.random(n) ** f32(0.65)),
                     0, H - 1).astype(np.int32)
        warm = np.array([(255, 214, 150), (255, 190, 110), (210, 220, 255),
                         (255, 236, 200)], f32)
        lcol = warm[rng.integers(0, len(warm), n)] * rng.uniform(
            0.28, 1.0, (n, 1)).astype(f32) * f32(0.72)
        lphase = rng.uniform(0, 6.283, n).astype(f32)
        lrate = rng.uniform(0.15, 0.5, n).astype(f32)
    else:
        lx = ly = lcol = lphase = lrate = None

    # --- Sutro Tower -------------------------------------------------------
    beacon = None
    tower_top = None
    if args.tower:
        spr = _tower_sprite(max(9, int(0.34 * H)))
        th, tw = spr.shape
        top = int(round(ridge[sutro_x])) - th + 1
        tower_top = float(top)
        x0 = sutro_x - tw // 2
        ys, xs = np.nonzero(spr)
        ys = ys + top
        xs = xs + x0
        keep = (ys >= 0) & (ys < H) & (xs >= 0) & (xs < W)
        ys, xs = ys[keep], xs[keep]
        # Aviation banding: alternating orange-red and white, dimmed to an
        # evening exposure so it sits in the scene instead of glaring out.
        band = max(2, th // 7)
        red = np.array([158, 48, 30], f32)
        white = np.array([176, 178, 184], f32)
        which = ((ys - top) // band) % 2
        bg[ys, xs] = np.where(which[:, None] == 0, red, white)
        beacon = (max(0, top), sutro_x)          # the red light on the mast

    # PLANAR, not interleaved. The composite is the most expensive thing in
    # the frame and it is all broadcasts — background times a coverage, plus a
    # per-row colour times a coverage. numpy walks a (H,W,1) broadcast against
    # an (H,W,3) array element by element and it is four times slower than the
    # same arithmetic done three times on contiguous (H,W) planes: 0.20 ms
    # against 0.054 here, which is a third of the whole frame. Only the final
    # store back into the interleaved uint8 buffer is strided.
    bg_planes = np.ascontiguousarray(np.transpose(bg, (2, 0, 1)))

    # --- fog textures ------------------------------------------------------
    # Two scales. The coarse one is the shape of the bank; the detail one,
    # four octaves deep, is everything from the billows down to pixel grain,
    # and it is the one that gets warped. A third scrolling layer was tried
    # and cut: an extra octave baked into the detail texture is free, where a
    # third layer costs a gather and two passes on every frame forever.
    coarse = _fbm(rng, TH, TW, 2, 3,
                  octaves=2, gain=0.5)[np.arange(H, dtype=np.int32) % TH]
    detail = _fbm(rng, TH, TW, 3, 6, octaves=4, gain=0.55)
    # Both textures carry a duplicate of column 0 on the right. The x wrap is
    # then only needed on the *base* index, and the +1 of the interpolation
    # never has to be masked back into the row — which is two whole-frame
    # integer passes saved, every frame.
    coarse = np.concatenate([coarse, coarse[:, :1]], axis=1)
    detail = np.concatenate([detail, detail[:, :1]], axis=1)
    detail = np.ascontiguousarray(detail)
    stride = TW + 1
    # The detail layer also falls, slowly, which is the fog pouring over the
    # ridge. Shifting it by whole rows would step every 0.7 s and, because it
    # steps everywhere at once, read as the whole panel twitching — so the two
    # straddling rows are blended once per frame into a scratch texture. That
    # is four passes over the *texture* (33k) rather than two more gathers
    # over the frame, and it is the cheaper end of the trade.
    fall_tex = np.empty_like(detail)
    tex_rows = np.arange(TH, dtype=np.int32)
    row_index = (np.arange(H, dtype=np.int32) % TH)[:, None] * stride

    # Fog colour by row: cooler grey up in the sky, warmed towards the bottom
    # by the city underneath it. This is the whole palette of the demo.
    # Dim on purpose. This is the thing the wall shows late in the evening,
    # and a full panel of bright grey is neither restful nor what fog at dusk
    # actually looks like.
    fog_row = _ramp([(0.00, (40, 45, 58)), (0.40, (65, 70, 83)),
                     (0.72, (86, 86, 92)), (1.00, (101, 95, 86))], H)
    fog_col = [np.ascontiguousarray(fog_row[:, c])[:, None] for c in range(3)]

    # The layers are weighted by row, so the bank is built out of the coarse
    # shape up where its edge is against the sky and out of the finer one down
    # in the body — which is what reads as two layers of fog at different
    # heights rather than one texture stretched over the panel.
    #
    # CONTRAST is folded into the weights rather than applied afterwards.
    # Averaged octaves pile up around the middle, and a field that never
    # approaches 0 or 1 gives fog with no holes and no solid parts; stretching
    # it about 0.5 fixes that, and doing it here costs nothing at all.
    yn = (np.arange(H, dtype=f32) / max(H - 1, 1))[:, None]
    CONTRAST = f32(1.45)
    w_coarse = (f32(0.62) - f32(0.26) * yn) * CONTRAST
    w_detail = (f32(0.34) + f32(0.28) * yn) * CONTRAST
    # ...leaving `dfield` as CONTRAST*raw, so the field the rest of the code
    # wants is dfield - BIAS. That offset is folded into the two constants
    # downstream instead of costing a pass of its own.
    BIAS = f32(0.5 * (CONTRAST - 1.0))

    xcol = np.arange(W, dtype=f32)
    xnorm = xcol * f32(2.0 * np.pi / max(W, 1))

    # How high the fog can get: `lo` buries even the tower's prongs, `hi` is
    # below the bottom edge, i.e. gone.
    crest = float(ridge.min())
    lo = (tower_top if tower_top is not None else crest) - 0.04 * H
    hi = H * 1.12
    drape = f32(0.5)
    # Karl comes in off the ocean, so the bank stands higher at the west end
    # of the panel and falls away to the east. Without the tilt the fog is a
    # level sea and has no direction to it.
    tilt = (f32(0.5) - xcol / max(W - 1, 1)) * (f32(0.15) * H)
    soft = f32(max(4.0, 0.36 * H))               # how far the surface fades in
    yrow_soft = (np.arange(H, dtype=f32) / soft)[:, None]

    ba = _bayer(8)
    bayer = np.ascontiguousarray(
        np.tile(ba, (H // 8 + 1, W // 8 + 1))[:H, :W].astype(f32))
    if not args.dither:
        bayer = f32(0.5)                         # plain rounding instead

    # Perturbation of the fog edge by the density field, and the shading of
    # the fog body by it: both want `dfield - BIAS`, so both constants carry
    # the correction.
    WISP = f32(1.55)
    EDGE_C = f32(WISP * (0.5 + BIAS))
    SHADE = f32(0.95)
    SHADE_C = f32(0.42 - SHADE * BIAS)

    # The composite is left unclipped, which is worth one whole pass of the
    # frame. It is safe by construction: the background is clamped here, the
    # fog term cannot exceed max(fog_row) * (SHADE_C + SHADE * max(dfield)),
    # and the result of mixing two things below 255 with the dither's single
    # count on top stays inside a byte.
    np.clip(bg_planes, 0.0, 250.0, out=bg_planes)

    # --- per-frame buffers -------------------------------------------------
    dfield = np.empty((H, W), f32)
    alpha = np.empty((H, W), f32)
    lit = np.empty((H, W), f32)
    warp = np.empty((H, W), f32)
    whole = np.empty((H, W), f32)
    gidx = np.empty((H, W), np.int32)
    acc = np.empty((H, W), f32)
    tmp = np.empty((H, W), f32)
    buf = np.empty((H, W, 3), np.uint8)
    state = {"level": 0.0, "body": 0.0}

    def render(t, idx):
        # Every one of these is only ever mutated in place; the declaration is
        # what lets `+=` mean that rather than binding a fresh local.
        nonlocal dfield, alpha, lit, warp, gidx, fall_tex
        t = t * spd

        # --- density and height, driven straight off the clock -------------
        # Three incommensurate periods, so the pattern never repeats within
        # any watch; saturating the sum is what gives the plateaus where it
        # sits buried, or clear, instead of sweeping through.
        wave = (0.55 * np.sin(t * (2.0 * np.pi / 97.0) + 0.4)
                + 0.30 * np.sin(t * (2.0 * np.pi / 163.0) + 2.1)
                + 0.15 * np.sin(t * (2.0 * np.pi / 251.0) + 5.0))
        u = float(np.clip((0.5 + 0.78 * wave) * dens, 0.0, 1.0))
        level = hi + (lo - hi) * u
        body = f32(0.42 + 0.58 * u)

        # --- the fog surface ------------------------------------------------
        # Draped over the terrain, and ragged: a straight edge would read as a
        # wipe rather than as a fog bank.
        surf = (level + drape * (ridge - crest) - tilt
                + (0.030 * H) * np.sin(xnorm * 3.0 - t * 0.055)
                + (0.022 * H) * np.sin(xnorm * 7.0 + t * 0.031)
                + (0.014 * H) * np.sin(xnorm * 13.0 - t * 0.087))

        # --- layer 1: the coarse bank, a column gather with a fractional lerp
        # Integer scrolling at this speed ratchets visibly — a couple of
        # pixels per second is one jump every several frames, and the jump is
        # of the whole field at once — so it interpolates between columns.
        xa = xcol + f32((t * 2.6) % TW)
        i0 = xa.astype(np.int32) & (TW - 1)
        fa = (xa - np.floor(xa)).astype(f32)
        a0 = coarse[:, i0]
        np.multiply(coarse[:, i0 + 1] - a0, fa, out=dfield)
        dfield += a0
        dfield *= w_coarse

        # --- layer 2: detail, its sample point dragged around by layer 1 ----
        # This is the domain warp, and it is the only reason the thing looks
        # like fog rather than like a scrolling cloud photo: the fine
        # structure is carried along by the coarse field, so it stretches and
        # curls where the big shape moves.
        np.multiply(dfield, f32(29.0), out=warp)
        # The scrolls are taken modulo the texture width rather than left to
        # grow: after a few hours of uptime a float32 that large has lost the
        # fraction the interpolation depends on, and the fog would start to
        # step a pixel at a time.
        warp += xcol + f32((t * 6.5) % TW)
        np.floor(warp, out=whole)
        np.subtract(warp, whole, out=warp)        # the fraction
        np.copyto(gidx, whole, casting="unsafe")  # ...and the whole part
        gidx &= TW - 1
        gidx += row_index

        # The fall, interpolated between two texture rows (see fall_tex).
        yf = (t * 1.4) % TH
        y0 = int(yf)
        r0 = (tex_rows + y0) % TH
        np.subtract(detail[(r0 + 1) % TH], detail[r0], out=fall_tex)
        fall_tex *= f32(yf - y0)
        fall_tex += detail[r0]
        detail_flat = fall_tex.reshape(-1)

        b0 = detail_flat[gidx]
        np.subtract(detail_flat[gidx + 1], b0, out=alpha)
        alpha *= warp
        alpha += b0
        alpha *= w_detail
        dfield += alpha

        # --- surface into opacity -------------------------------------------
        # Depth below the fog top, softened over a band, with the density
        # field perturbing the *depth* rather than the colour — that is what
        # tears holes in the edge and lets wisps stand above the surface.
        # Written as three broadcasts so no full-frame temporary is built:
        # alpha = WISP*d + y/soft - surf/soft.
        np.multiply(dfield, WISP, out=alpha)
        alpha += yrow_soft
        alpha -= surf * (f32(1.0) / soft) + EDGE_C
        np.clip(alpha, 0.0, 1.0, out=alpha)
        alpha *= body

        # Shade the fog by its own density so the interior is not a flat wash:
        # a uniform grey field is the failure mode this whole layered scheme
        # exists to avoid, and once the depth term saturates the density is
        # all that is left to carry structure.
        np.multiply(dfield, SHADE, out=lit)
        lit += SHADE_C
        lit *= alpha

        # --- background touch-ups (a few hundred pixels, not a frame) -------
        if lx is not None:
            tw_ = f32(0.72) + f32(0.28) * np.sin(lphase + t * lrate)
            bg_planes[:, ly, lx] = (lcol * tw_[:, None]).T
        if beacon is not None:
            pulse = 0.35 + 0.65 * max(0.0, np.sin(t * 1.1)) ** 6
            bg_planes[:, beacon[0], beacon[1]] = (f32(70) + f32(150) * pulse,
                                                  f32(20), f32(14))

        # --- composite, one contiguous plane at a time -----------------------
        # The dither is added before the truncation to uint8; without it this
        # much soft grey gradient shows as contour steps. See --no-dither.
        np.subtract(f32(1.0), alpha, out=alpha)
        for c in range(3):
            np.multiply(lit, fog_col[c], out=tmp)
            np.multiply(bg_planes[c], alpha, out=acc)
            np.add(acc, tmp, out=acc)
            np.add(acc, bayer, out=acc)
            np.copyto(buf[:, :, c], acc, casting="unsafe")
        state["level"] = level
        state["body"] = float(body)
        return buf

    render.state = state
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=24)


if __name__ == "__main__":
    main()
