#!/usr/bin/env python3
"""Water ripples.

Drops fall on a pool, rings spread out and interfere, and the surface
refracts whatever is painted on the bottom.

The surface is a damped wave equation on a grid, integrated with the classic
two-buffer scheme: the new height at a cell is a weighted average of its
neighbours minus the *previous* height at that cell, scaled by a damping
factor a shade below 1. No velocity buffer is needed -- the previous frame
carries it.

Drawing the height directly looks flat, so it is not drawn at all. What you
see is the *slope*: the local gradient bends the lookup into a background
image, and a specular term lights the tilted facets. Straight lines make
refraction legible, which is why the checkerboard is the default background.

Run:  python3 water.py --host 127.0.0.1
      python3 water.py --background rings --palette magma --drops 12
"""

import sys

import numpy as np

import demoscene as ds


def add_arguments(ap):
    ds.palette_argument(ap, "ice")
    ap.add_argument("--background", default="checker",
                    choices=["checker", "ramp", "rings", "grid"],
                    help="what the water refracts")
    ap.add_argument("--drops", type=float, default=9.0,
                    help="drops per second; high enough that rings overlap")
    ap.add_argument("--splash", type=float, default=0.12,
                    help="fraction of drops that are a big splash")
    ap.add_argument("--damping", type=float, default=0.991,
                    help="per step energy retention, must stay below 1")
    ap.add_argument("--steps", type=int, default=2,
                    help="simulation steps per frame; also the wave speed")
    ap.add_argument("--refract", type=float, default=28.0,
                    help="pixels of background offset per unit of slope")
    ap.add_argument("--gloss", type=float, default=0.85, help="specular strength")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


# The stencil. h_new = 2h - h_prev + c^2 * laplacian(h), with the isotropic
# nine point laplacian (4*orthogonal + diagonal - 20h)/6 and c^2 = 1/2. That
# collapses to (4*orth + diag + 4h)/12 - h_prev, which is what the step below
# computes. The nine point form is the whole reason the rings come out round:
# the cheap four neighbour laplacian is anisotropic on a square grid and grows
# visible diamonds within a couple of hundred steps. It is also the safer of
# the two -- the four neighbour version at c^2 = 1/2 sits exactly on the
# stability limit for the checkerboard mode, while this one keeps margin.
def _step(cur, prev, damping):
    """One wave step. cur/prev are padded (H+2, W+2); returns the new field."""
    orth = (cur[:-2, 1:-1] + cur[2:, 1:-1] + cur[1:-1, :-2] + cur[1:-1, 2:])
    diag = (cur[:-2, :-2] + cur[:-2, 2:] + cur[2:, :-2] + cur[2:, 2:])
    nxt = prev                                  # reuse the older buffer
    # Boundaries: the pad ring is never written, so it stays zero forever --
    # a fixed edge, which reflects a ripple back into the pool (inverted, like
    # a rope tied to a post). np.roll would have been shorter but wraps, and
    # on a 320x64 panel a ring reaches the top and bottom edges almost
    # immediately, so a ripple reappearing from the far side would be the most
    # visible thing on screen. A pool has walls.
    nxt[1:-1, 1:-1] = ((4.0 * orth + diag + 4.0 * cur[1:-1, 1:-1]) * (1.0 / 12.0)
                       - prev[1:-1, 1:-1]) * damping
    return nxt


def _bump(radius):
    """A raised cosine disc, the shape a drop stamps into the surface."""
    y, x = np.mgrid[-radius:radius + 1, -radius:radius + 1].astype(ds.f32)
    d = np.sqrt(x * x + y * y) / radius
    return np.where(d < 1.0, 0.5 * (1.0 + np.cos(np.pi * d)), 0.0).astype(ds.f32)


def _background(name, W, H, lut):
    """The image under the water, as (H, W, 3) uint8."""
    yy, xx = np.mgrid[0:H, 0:W]
    if name == "checker":
        # 8px squares: big enough to survive the panel, small enough that a
        # ring crossing one visibly kinks the edges.
        mask = ((xx // 8) + (yy // 8)) & 1
        return np.where(mask[..., None] > 0, lut[210], lut[45]).astype(np.uint8)
    if name == "grid":
        line = ((xx % 10 == 0) | (yy % 10 == 0))
        return np.where(line[..., None], lut[235], lut[25]).astype(np.uint8)
    if name == "rings":
        cx, cy = W * 0.5, H * 0.5
        r = np.sqrt(((xx - cx) / 1.6) ** 2 + (yy - cy) ** 2)
        return lut[(np.sin(r * 0.30) * 110 + 130).astype(np.uint8)]
    # ramp: depth, dark at the top of the frame like a pool seen at an angle.
    v = (yy * (235.0 / max(H - 1, 1)) + 12).astype(np.uint8)
    return lut[v]


def build(args):

    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    lut = ds.named_palette(args.palette)
    bg = _background(args.background, W, H, lut)
    bg_flat = bg.reshape(-1, 3)

    # One pad cell all round holds the fixed zero boundary, so the interior
    # step is pure slicing with no index arithmetic.
    cur = np.zeros((H + 2, W + 2), ds.f32)
    prev = np.zeros((H + 2, W + 2), ds.f32)

    drop = _bump(2)
    splash = _bump(5)
    damping = ds.f32(args.damping)

    yy, xx = np.mgrid[0:H, 0:W].astype(np.int32)
    out = np.empty((H, W, 3), np.uint8)

    # Light from the upper left, the direction that reads as overhead on a
    # panel that is much wider than it is tall.
    lx, ly, lz = ds.f32(-0.45), ds.f32(-0.55), ds.f32(0.70)

    state = {"next": 0.0}

    def spawn():
        big = rng.random() < args.splash
        k = splash if big else drop
        r = k.shape[0] // 2
        # Keep the whole stamp inside the pad ring.
        y = int(rng.integers(r + 1, H - r + 1))
        x = int(rng.integers(r + 1, W - r + 1))
        # Negative: a drop punches the surface down, and the rebound is what
        # the wave equation gives you for free.
        amp = ds.f32(-(2.6 if big else 1.0))
        cur[y - r:y + r + 1, x - r:x + r + 1] += k * amp

    def render(t, frame):
        nonlocal cur, prev

        # Poisson arrivals, so drops clump instead of ticking like a metronome
        # -- overlapping ring sets are the whole point of the effect.
        if state["next"] == 0.0:
            state["next"] = t
        rate = max(args.drops, 1e-3)
        while t >= state["next"]:
            spawn()
            state["next"] += float(rng.exponential(1.0 / rate))

        for _ in range(args.steps):
            prev, cur = cur, _step(cur, prev, damping)

        # Central differences on the padded field: the zero ring supplies the
        # edge samples, so no separate edge case.
        gx = (cur[1:-1, 2:] - cur[1:-1, :-2]) * ds.f32(0.5)
        gy = (cur[2:, 1:-1] - cur[:-2, 1:-1]) * ds.f32(0.5)

        # Refraction: shift the lookup into the background by the slope.
        sx = np.clip(xx + (gx * args.refract).astype(np.int32), 0, W - 1)
        sy = np.clip(yy + (gy * args.refract).astype(np.int32), 0, H - 1)
        lit = bg_flat[sy * W + sx].astype(ds.f32)

        # Surface normal (-gx, -gy, 1), scaled so a typical ripple tilts it
        # usefully far, then a lambert term for shape and a tight power of it
        # for the glint off the steep faces.
        nx = gx * ds.f32(-9.0)
        ny = gy * ds.f32(-9.0)
        inv = 1.0 / np.sqrt(nx * nx + ny * ny + 1.0)
        ndl = np.clip((nx * lx + ny * ly + lz) * inv, 0.0, 1.0)
        spec = ndl ** 20 * ds.f32(255.0 * args.gloss)

        lit *= (0.55 + 0.75 * ndl)[..., None]
        lit += spec[..., None]
        np.clip(lit, 0, 255, out=lit)
        out[:] = lit.astype(np.uint8)
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
