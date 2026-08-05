#!/usr/bin/env python3
"""Drifting through a sequoia grove.

Massive trunks passing in parallax, shafts of light angling down between them,
fog hanging in the gaps. A sequoia is three hundred feet tall and this panel is
sixty-four pixels, so there is no showing a whole tree: this is a slice at eye
level, trunks running off the top of the frame, which is what standing in a
grove actually looks like.

Everything is sprites. A trunk sits at a fixed depth, so its width, its
cylinder shading, its bark and how far the fog has eaten it are all constant --
only its screen x changes. So each trunk is baked once into an (H, w) premultiplied
RGBA sprite in build(), and a frame is a handful of clipped blits back to front.
Depth sets the drift rate, so near trunks sweep and far ones crawl.

The light shafts are the same trick: a shaft is a baked additive image with a
Gaussian across-profile, slanted by an offset per row. Nothing is blurred at
run time -- a whole-frame blur costs 40-200x more on the Pi than on a desktop,
which is what forced other effects out of the show. Soft edges come out of the
bake. Shafts carry a depth like everything else, so they composite in order and
a near trunk crossing one interrupts it.

Run:  python3 grove.py --host 127.0.0.1
      python3 grove.py --trunks 9 --shafts 4 --fog 1.2 --speed 0.5
"""

import sys

import numpy as np

import demoscene as ds

SUB = 4                                # baked subpixel phases per sprite

# Bark. A scalar 0..1 through a ramp rather than RGB per pixel: the palette
# carries the colour, and it keeps the sequoia cinnamon consistent between the
# lit side and the crevices.
BARK = [(0.00, (8, 5, 4)), (0.22, (28, 14, 10)), (0.45, (60, 27, 17)),
        (0.68, (98, 46, 27)), (0.86, (140, 71, 41)), (1.00, (178, 106, 66))]

FOG_RGB = (44, 52, 54)                 # cool grey-green the distance sinks into
SHAFT_RGB = (255, 186, 96)             # warm, low blue: it has to read gold
                                       # against a grey fog, and on the panel
                                       # blue is the channel that washes it out


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------

def bayer(n=8):
    """The classic recursive ordered-dither matrix, normalised to [0,1)."""
    m = np.zeros((1, 1), ds.f32)
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return m / m.size


def smooth1(x, sigma):
    """Gaussian blur of a 1-D array, wrapping. Build time only."""
    if sigma <= 0:
        return x
    # Clamped, or a sigma wider than the array (which a very thin trunk asks
    # for, since its fibre scale in angular steps is enormous) makes the
    # wrapped padding longer than the array and the slice comes back short.
    r = min(max(1, int(3 * sigma)), max(1, len(x) // 2))
    k = np.exp(-0.5 * (np.arange(-r, r + 1, dtype=ds.f32) / sigma) ** 2)
    k /= k.sum()
    return np.convolve(np.concatenate([x[-r:], x, x[:r]]), k, "same")[r:-r]


def boxblur(a, r, axis):
    """Wrapping box blur along one axis. Build time only."""
    r = int(r)
    if r < 1:
        return a
    a = np.moveaxis(a, axis, -1)
    n = a.shape[-1]
    pad = np.concatenate([a[..., -r:], a, a[..., :r + 1]], axis=-1)
    c = np.cumsum(pad, axis=-1, dtype=ds.f32)
    out = (c[..., 2 * r + 1:] - c[..., :n]) / (2 * r + 1)
    return np.moveaxis(out, -1, axis)


def fibres(rng, n, sigma, sharp):
    """A wrapping 1-D ridge pattern in 0..1: smoothed noise, contrast pushed."""
    v = smooth1(rng.standard_normal(n).astype(ds.f32), sigma)
    v /= max(float(np.abs(v).max()), 1e-6)
    return 0.5 + 0.5 * np.tanh(sharp * v)


def premul(rgb, alpha):
    """(rgb*a, a) with SUB subpixel phases baked in, one pad column.

    Interpolating a sprite between two whole-pixel positions has to happen in
    premultiplied alpha or the edge colour bleeds toward black; and it has to
    happen at build time, because doing it per frame is the same cost as the
    blit itself. A trunk two pixels a second across the panel steps once every
    thirty frames without this, which reads as a judder, not a drift.
    """
    rgb = np.concatenate([rgb, np.zeros_like(rgb[:, :1])], axis=1)
    alpha = np.concatenate([alpha, np.zeros_like(alpha[:, :1])], axis=1)
    p = rgb * alpha[..., None]
    out_p, out_a = [], []
    for s in range(SUB):
        f = ds.f32(s) / SUB
        out_p.append((1 - f) * p + f * np.roll(p, 1, axis=1))
        out_a.append((1 - f) * alpha + f * np.roll(alpha, 1, axis=1))
    return np.stack(out_p).astype(ds.f32), np.stack(out_a).astype(ds.f32)


# --------------------------------------------------------------------------
# Sprite bakery.
# --------------------------------------------------------------------------

def make_trunk(rng, H, half, base_y, fog, lut, dark):
    """One trunk as (rgb, alpha) floats, (H, w).

    `half` is the half width in pixels, `base_y` where the trunk meets the
    ground -- below the frame for near trunks, which is the point.

    The cylinder is the part that has to work. The surface coordinate is
    arcsin(x/half), the actual angle round the cylinder, so the bark fibres
    crowd together toward the silhouette exactly as they do on a real trunk;
    that compression plus the diffuse term is what makes it read as a column
    instead of a striped bar.
    """
    flare = max(4.0, half * 0.5)                # root flare near the base
    y = np.arange(H, dtype=ds.f32)[:, None]
    widen = 1.0 + 0.42 * np.exp(-np.maximum(base_y - y, 0.0) / flare)
    hw = half * widen                           # per-row half width

    # Width from the widest row actually on screen, not from the widest the
    # flare could ever be: a near trunk's flare is metres below the frame, and
    # padding every sprite for it would mean blitting columns that are always
    # transparent -- pure overdraw, on the most expensive layer there is.
    w = int(np.ceil(2.0 * float(hw.max()))) + 3
    cx = (w - 1) * 0.5
    x = np.arange(w, dtype=ds.f32)[None, :] - cx

    # Coverage, antialiased. A hard trunk edge on a 320 wide panel crawls.
    alpha = np.clip(hw - np.abs(x) + 0.5, 0.0, 1.0)
    alpha *= (y <= base_y + 0.5)                # cut off at the ground

    s = np.clip(x / hw, -1.0, 1.0)              # -1..1 across the cylinder
    c = np.sqrt(np.maximum(1.0 - s * s, 0.0))   # normal, toward the viewer

    # Sun is high and to the left, coming through the canopy, so the lit edge
    # sits left of centre and the terminator falls inside the silhouette.
    lx, lz = -0.66, 0.75
    diff = np.clip(s * lx + c * lz, 0.0, 1.0) ** 1.35
    rim = 0.20 * np.clip(s, 0.0, 1.0) ** 4      # fog bounce on the shadow edge
    shade = 0.09 + 0.98 * diff + rim

    # Fibre lookup. theta in 0..1 around the visible half, wobbling slowly with
    # height so the striations are not dead straight, plus horizontal plate
    # breaks: sequoia bark is furrowed vertically but scarred across.
    NU = 1024
    theta = (np.arcsin(s) / (0.5 * np.pi) + 1.0) * 0.5
    # Fibre scale is set in *pixels at the trunk centre*, converted to steps of
    # the angular coordinate, so bark grain stays the same size on screen
    # whatever the trunk's depth -- and still crowds toward the silhouette.
    per_px = NU / (2.0 * half)
    fine = fibres(rng, NU, max(0.55 * per_px, 0.8), 2.6)     # ridges, ~1.5 px
    coarse = fibres(rng, NU, max(1.9 * per_px, 2.0), 1.9)    # furrows, ~5 px
    wob = smooth1(rng.standard_normal(H).astype(ds.f32), 9.0)
    wob = (wob / max(float(np.abs(wob).max()), 1e-6) * 0.004)[:, None]

    idx = ((theta + wob) * NU).astype(np.int32) % NU
    bark = 0.5 * fine[idx] + 0.5 * coarse[idx]

    # Purely vertical striations would look extruded, and that is exactly what
    # a first pass looked like. A coarse 2-D field -- correlated far more down
    # the trunk than across it -- breaks the fibres into the plates and scars
    # that real bark has, without touching their verticality.
    grain = rng.random((H, NU)).astype(ds.f32)
    grain = boxblur(boxblur(grain, max(1, int(0.9 * per_px)), 1), 3, 0)
    grain = boxblur(grain, 2, 0)
    grain -= grain.mean()
    grain /= max(float(np.abs(grain).max()), 1e-6)
    bark = np.clip(bark * (1.0 + 0.55 * np.take_along_axis(grain, idx, 1)), 0.0, 1.2)

    # Detail contrast falls off with distance well before the colour does; a
    # far trunk with near-trunk bark is the classic depth-cue mistake.
    lum = shade * (1.0 - dark * (1.0 - bark))
    lum *= 0.62 + 0.38 * np.clip((base_y + 8.0 - y) / max(base_y + 8.0, 1.0), 0.2, 1.0)

    rgb = lut[np.clip(lum * 255.0, 0, 255).astype(np.int32)].astype(ds.f32)
    rgb += (np.asarray(FOG_RGB, ds.f32) - rgb) * fog
    # Fog is mixed in flat, which at the far end leaves a cardboard cut-out.
    # Putting a weak copy of the diffuse term back *over* the fog keeps the
    # cylinder readable at any depth -- distant trunks lose their bark, which
    # is right, but they must not lose their round.
    rgb *= (0.84 + 0.32 * diff)[..., None]
    return rgb, alpha.astype(ds.f32)


def make_shaft(H, width, slope, top, strength):
    """An additive light shaft: (H, w, 3) float, already soft edged.

    A Gaussian across the beam and a smooth falloff down it. Both are baked, so
    the softness costs nothing per frame -- this is the whole reason the shafts
    are sprites and not something blurred out of a mask.
    """
    lean = abs(slope) * H
    w = int(np.ceil(width * 5.5 + lean)) + 3   # out to where the halo dies
    y = np.arange(H, dtype=ds.f32)[:, None]
    x = np.arange(w, dtype=ds.f32)[None, :]

    # Shafts spread a little as they descend; the beam is a narrow cone.
    grow = 1.0 + 0.28 * (y / H)
    cx = (w - lean) * 0.5 + slope * y if slope > 0 else \
         (w + lean) * 0.5 + slope * y
    d = (x - cx) / (width * grow)
    beam = np.exp(-2.4 * d * d) + 0.30 * np.exp(-0.55 * d * d)   # core + halo

    # Down the beam: full strength coming out of the canopy at the top of the
    # frame, thinning as it goes but still reaching the floor, because a shaft
    # that stops in mid air reads as a puff of smoke rather than as light.
    v = np.clip((y - top) / max(H - top, 1.0), 0.0, 1.0)
    fall = (1.0 - 0.55 * v) * np.clip(v * 9.0, 0.0, 1.0)
    beam = beam * fall / grow ** 0.5

    # Motes: a faint shimmer along the shaft so it is not a dead gradient.
    mote = 1.0 + 0.09 * np.sin(y * 0.9 + x * 0.35) * np.sin(y * 0.31 - x * 0.17)
    beam = beam * mote

    return (beam[..., None] * np.asarray(SHAFT_RGB, ds.f32) * strength).astype(ds.f32)


def make_ferns(rng, W2, rows, count, colour, scale):
    """A wide alpha strip of fern / sorrel silhouettes along the floor."""
    a = np.zeros((rows, W2), ds.f32)

    def stamp(px, py, v):
        xi, yi = int(px), int(py)
        if 0 <= yi < rows:
            a[yi, xi % W2] = min(1.0, a[yi, xi % W2] + v)

    for _ in range(count):
        bx = rng.uniform(0, W2)
        h = scale * rng.uniform(0.45, 1.0)
        if rng.random() < 0.7:                  # fern: a few arching fronds
            for _f in range(rng.integers(2, 5)):
                fh = h * rng.uniform(0.6, 1.0)
                lean = rng.uniform(-1.3, 1.3)
                n = max(3, int(fh * 2.5))
                for i in range(n):
                    u = (i + 1) / n
                    px = bx + lean * fh * u * u
                    py = rows - 1 - fh * np.sin(u * 1.4)
                    stamp(px, py, 0.9)
                    # Pinnae: single pixels hanging off the rachis, densest at
                    # mid frond. More than one pixel and at this size the whole
                    # plant fuses into a blob.
                    if i % 2 == 0 and 0.15 < u < 0.9:
                        d = 1 + int(fh * 0.16)
                        stamp(px - d, py + 0.6, 0.75)
                        stamp(px + d, py + 0.6, 0.75)
        else:                                    # sorrel: a low trefoil
            py = rows - 1 - h * 0.5
            for dx in (-1.4, 0.0, 1.4):
                stamp(bx + dx, py + (0.9 if dx else 0.0), 0.85)
            for k in range(max(1, int(h * 0.5))):
                stamp(bx, rows - 1 - k, 0.7)

    rgb = np.broadcast_to(np.asarray(colour, ds.f32), (rows, W2, 3)).copy()
    return rgb, a


def make_background(rng, H, W2, horizon, fog):
    """The far wall of the grove: fog gradient plus dissolved distant trunks.

    Anything past the furthest real trunk is a suggestion, and suggestions are
    cheap: a couple of very soft low-contrast vertical bars painted into the
    same strip as the gradient, scrolled as one slice.
    """
    y = np.arange(H, dtype=ds.f32)[:, None]             # (H,1)
    x = np.arange(W2, dtype=ds.f32)[None, :]            # (1,W2)
    fogc = np.asarray(FOG_RGB, ds.f32)
    # Dark canopy above, the fog bank brightest around eye level, then the
    # ground shadow. All of this is a big smooth dark gradient, i.e. precisely
    # the thing the panel's 8 PWM bits will band -- see the dither at the end.
    v = np.clip((y - 1.0) / max(horizon - 1.0, 1.0), 0.0, 1.0)
    # The top of the frame is canopy shadow, but not black: a dead band across
    # a sixth of an LED panel is wasted, and a little fog glow up there also
    # gives the trunks something to be silhouetted against.
    lift = 0.17 + 0.60 * v ** 1.5
    lift = lift * np.clip(1.0 - 0.6 * np.clip((y - horizon) / 10.0, 0, 1), 0, 1)
    lift = np.broadcast_to(lift, (H, W2)).copy() * (0.55 + 0.45 * fog)

    # Low-frequency drift across the strip: a flat fog field between two trunks
    # reads as a lit rectangle, which is the giveaway that it is a backdrop.
    band = smooth1(rng.standard_normal(W2).astype(ds.f32), 26.0)
    band /= max(float(np.abs(band).max()), 1e-6)
    lift *= 1.0 + 0.30 * band[None, :]

    # Distant trunks: gaussian bars, low contrast, slightly darker than fog.
    # Two grades, because one width reads as a repeating motif.
    for wlo, whi, alo, ahi, n in ((1.5, 4.0, 0.10, 0.30, W2 // 12),
                                  (4.0, 11.0, 0.16, 0.40, W2 // 40)):
        for _ in range(max(4, n)):
            cx = rng.uniform(0, W2)
            wd = rng.uniform(wlo, whi)
            amt = rng.uniform(alo, ahi)
            d = np.minimum(np.abs(x - cx), W2 - np.abs(x - cx)) / wd
            bar = np.exp(-d * d)                        # (1,W2)
            top = rng.uniform(0.0, horizon * 0.4)
            vis = np.clip((y - top) / 6.0, 0.0, 1.0)    # (H,1)
            vis = vis * np.clip(1.0 - (y - horizon) / 6.0, 0.0, 1.0)
            lift *= 1.0 - amt * bar * vis
    return (lift[..., None] * fogc).astype(ds.f32)


def make_ground(rng, W2, rows, fog):
    """Duff and needle litter, one row per depth, so it can scroll per row."""
    y = np.arange(rows, dtype=ds.f32)[:, None]          # 0 at the horizon
    speck = rng.random((rows, W2)).astype(ds.f32)
    # Litter grains get bigger as they come near, or the floor reads as static
    # noise rather than as ground going past.
    speck = np.clip((speck - 0.62) / 0.38, 0.0, 1.0) ** 1.4
    near = (y / max(rows - 1, 1))
    out = (np.asarray((22, 18, 12), ds.f32) * (0.30 + 0.70 * near)[..., None]
           + speck[..., None] * np.asarray((26, 20, 10), ds.f32) * (0.4 + 0.6 * near)[..., None])
    # Fog eats the far edge of the floor, the strip nearest the horizon.
    k = (np.clip(1.0 - y / max(rows * 0.75, 1.0), 0.0, 1.0) ** 1.3 * fog)[..., None]
    out = out + (np.asarray(FOG_RGB, ds.f32) * 0.5 - out) * k
    return out.astype(ds.f32)


# --------------------------------------------------------------------------
# Demo.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=1.0,
                    help="walking pace; 1.0 is a slow amble")
    ap.add_argument("--trunks", type=int, default=7,
                    help="how many trunks are in the world at once")
    ap.add_argument("--fog", type=float, default=1.0,
                    help="how completely depth fades into the fog")
    ap.add_argument("--shafts", type=int, default=3,
                    help="light shafts angling down between the trunks")
    ap.add_argument("--seed", type=int, default=7, help="which grove")
    ap.add_argument("--no-dither", dest="dither", action="store_false",
                    help="skip the ordered dither; shows the panel's banding")
    ap.add_argument("--no-ferns", dest="ferns", action="store_false",
                    help="bare forest floor")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    lut = ds.gradient(BARK)

    # Eye level sits low so the floor is a strip and the trunks own the frame.
    horizon = H * 0.70
    focal = W * 0.22
    cam_h = 1.7                                   # eye height, metres-ish

    floor_rows = int(H - horizon)
    W2 = 2 * W                                    # wide strips, scrolled by slice

    # ---- the world: trunks and shafts, each at a depth -------------------
    layers = []

    # Depths are spaced geometrically: a linear spread puts everything in the
    # middle distance, where parallax differences are invisible.
    n_tr = max(args.trunks, 1)
    for i in range(n_tr):
        u = (i + rng.uniform(0.0, 0.85)) / n_tr
        z = 2.8 * (24.0 / 2.8) ** u                       # 2.8 .. 24 units
        radius = rng.uniform(0.85, 1.45)                  # sequoia, so: fat
        half = max(1.2, radius * focal / z)
        base_y = horizon + cam_h * focal / z
        fog = float(np.clip((1.0 - np.exp(-z / 11.0)) * args.fog, 0.0, 0.93))
        dark = float(np.clip(0.66 * np.exp(-z / 12.0), 0.08, 0.66))
        rgb, a = make_trunk(rng, H, half, base_y, fog, lut, dark)
        p, pa = premul(rgb, a)
        # Columns opaque over every row, in every subpixel phase. Over that
        # span the blend is just a copy, which is half the memory traffic --
        # and it is most of a near trunk, the widest sprite in the scene.
        solid = np.flatnonzero(pa.min(axis=(0, 1)) >= 0.999)
        core = ((int(solid[0]), int(solid[-1]) + 1) if solid.size
                and solid[-1] - solid[0] + 1 == solid.size else (0, 0))
        layers.append(dict(kind="trunk", z=z, p=p, core=core,
                           ia=(1.0 - pa[..., None]).astype(ds.f32), w=p.shape[2],
                           x0=(i + rng.uniform(0.0, 0.6)) / n_tr))

    for i in range(max(args.shafts, 0)):
        z = rng.uniform(3.2, 14.0)
        slope = rng.choice([-1.0, 1.0]) * rng.uniform(0.20, 0.55)
        width = rng.uniform(2.6, 6.0) * (5.0 / z + 0.75)
        strength = rng.uniform(0.34, 0.66) * (0.40 + 0.60 * (6.0 / z))
        img = make_shaft(H, width, slope, rng.uniform(0.0, H * 0.10), strength)
        add = np.stack([(1 - s / SUB) * img + (s / SUB) * np.roll(img, 1, axis=1)
                        for s in range(SUB)]).astype(ds.f32)
        layers.append(dict(kind="shaft", z=z, p=add, w=img.shape[1],
                           x0=rng.uniform(0.0, 1.0)))

    # Back to front. Everything else follows from this: a shaft is interrupted
    # simply because a nearer trunk is blitted over it afterwards.
    layers.sort(key=lambda L: -L["z"])
    for L in layers:
        L["rate"] = args.speed * 34.0 / L["z"]        # px/s: parallax, 1/depth
        L["period"] = W + L["w"] + 2.0
        L["x0"] = L["x0"] * L["period"]

    # ---- static-ish backdrop, floor, ferns --------------------------------
    bg = make_background(rng, H, W2, horizon, min(args.fog, 1.0))
    ground = make_ground(rng, W2, floor_rows, min(args.fog, 1.0))
    grow = np.arange(floor_rows)[:, None]
    gcol = np.arange(W, dtype=np.int64)[None, :]
    # Rows of the floor scroll at their own rate: near litter sweeps past,
    # the strip by the horizon barely moves. Same 1/depth law as the trunks.
    grate = args.speed * 34.0 / np.maximum(cam_h * focal /
                                           np.maximum(np.arange(floor_rows) + 0.5, 0.5), 1.2)

    fern_layers = []
    if args.ferns:
        for rows, count, col, sc, rate in (
                (max(4, floor_rows), W2 // 22, (18, 26, 15), floor_rows * 0.5, 9.0),
                (max(6, floor_rows + 3), W2 // 34, (6, 10, 6), floor_rows * 0.9, 20.0)):
            frgb, fa = make_ferns(rng, W2, rows, count, col, sc)
            fern_layers.append(dict(rgb=(frgb * fa[..., None]).astype(ds.f32),
                                    ia=(1.0 - fa[..., None]).astype(ds.f32),
                                    rows=rows, rate=args.speed * rate))

    # ---- dither -----------------------------------------------------------
    # The panel drives 8 PWM bits, and this scene is mostly large, smooth, dark
    # gradients -- fog bank, shaft falloff -- which is the exact case where a
    # quantised ramp shows as contour steps on the wall even though it looks
    # fine on a monitor. An 8x8 Bayer offset added before the uint8 truncation
    # trades those contours for a stable, invisible-at-distance pixel texture.
    # Channels use rolled copies of the matrix so the noise does not correlate
    # into coloured blotches.
    b = bayer(8)
    dith = np.stack([np.roll(b, k * 3, axis=1) for k in range(3)], axis=-1)
    dith = np.tile(dith, (H // 8 + 1, W // 8 + 1, 1))[:H, :W, :].astype(ds.f32)
    if not args.dither:
        dith = np.zeros_like(dith)

    acc = np.zeros((H, W, 3), ds.f32)
    out = np.zeros((H, W, 3), np.uint8)

    def render(t, frame):
        # Background: the fog wall, drifting slowest of anything.
        off = int(args.speed * 2.2 * t) % W
        np.copyto(acc, bg[:, off:off + W])

        # Floor, one gather: per-row offset, so each depth scrolls at its rate.
        if floor_rows > 0:
            goff = (grate * t).astype(np.int64) % W
            acc[H - floor_rows:] = ground[grow, gcol + goff[:, None]]

        # Trunks and shafts, back to front.
        for L in layers:
            xf = (L["x0"] + L["rate"] * t) % L["period"] - L["w"] - 1.0
            xi = int(np.floor(xf))
            ph = int((xf - xi) * SUB) & (SUB - 1)
            lo, hi = max(xi, 0), min(xi + L["w"], W)
            if hi <= lo:
                continue
            p = L["p"][ph]
            if L["kind"] != "trunk":
                acc[:, lo:hi] += p[:, lo - xi:hi - xi]
                continue
            ia = L["ia"][ph]
            c0, c1 = xi + L["core"][0], xi + L["core"][1]
            for a, b, blend in ((lo, min(hi, c0), True),      # left edge
                                (max(lo, c0), min(hi, c1), False),
                                (max(lo, c1), hi, True)):     # right edge
                if b <= a:
                    continue
                if blend:
                    dst = acc[:, a:b]
                    dst *= ia[:, a - xi:b - xi]   # 1-alpha, baked: no temporary
                    dst += p[:, a - xi:b - xi]
                else:
                    np.copyto(acc[:, a:b], p[:, a - xi:b - xi])

        # Ferns last: they are the nearest thing in the scene.
        for F in fern_layers:
            o = int(F["rate"] * t) % W
            reg = acc[H - F["rows"]:]
            reg *= F["ia"][:, o:o + W]
            reg += F["rgb"][:, o:o + W]

        # Dither, clamp, quantise -- all in place and straight into the output
        # buffer. Shafts are additive so the clamp is real work, not paranoia.
        np.add(acc, dith, out=acc)
        np.clip(acc, 0.0, 255.0, out=acc)
        np.copyto(out, acc, casting="unsafe")
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
