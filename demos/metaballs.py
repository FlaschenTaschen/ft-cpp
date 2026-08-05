#!/usr/bin/env python3
"""Metaballs.

Each ball contributes a field that falls off with distance, the fields are
summed, and the total is coloured through a palette. Because the sum is
smooth, two balls approaching each other bulge and merge into one shape
instead of overlapping -- which is the whole point of the effect, and
something you cannot get by drawing circles.

The balls travel on Lissajous paths, so they never repeat the same
arrangement for a long time without needing any physics.

Run:  python3 metaballs.py --host 127.0.0.1
      python3 metaballs.py --balls 8 --palette toxic --contour 6
"""

import numpy as np

import demoscene as ds


def main():
    ap = ds.parser(__doc__.split("\n", 1)[0])
    ds.palette_argument(ap, "ice")
    ap.add_argument("--balls", type=int, default=6)
    ap.add_argument("--size", type=float, default=13.0, help="ball radius in px")
    ap.add_argument("--speed", type=float, default=0.35, help="path speed")
    ap.add_argument("--gain", type=float, default=1.0,
                    help="field brightness; higher = fatter blobs")
    ap.add_argument("--contour", type=int, default=0,
                    help="if >1, quantize into this many bands for a contour look")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")
    args = ap.parse_args()

    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    lut = ds.named_palette(args.palette)

    yy, xx = np.mgrid[0:H, 0:W].astype(ds.f32)

    # Each ball gets its own pair of frequencies and phases. Keeping the
    # frequencies incommensurate is what stops the group falling into a short
    # repeating cycle.
    n = args.balls
    fx = rng.uniform(0.7, 1.9, n).astype(ds.f32)
    fy = rng.uniform(0.7, 1.9, n).astype(ds.f32)
    px = rng.uniform(0, 2 * np.pi, n).astype(ds.f32)
    py = rng.uniform(0, 2 * np.pi, n).astype(ds.f32)
    weight = rng.uniform(0.7, 1.3, n).astype(ds.f32)

    # Keep the balls inside the panel, with a margin so they do not sit half
    # off the edge where the merge is invisible.
    ax, ay = W * 0.40, H * 0.36
    r2 = ds.f32(args.size ** 2)
    field = np.empty((H, W), ds.f32)

    def render(t, frame):
        field[:] = 0.0
        ph = ds.f32(t * args.speed)
        bx = W / 2.0 + ax * np.sin(fx * ph + px)
        by = H / 2.0 + ay * np.sin(fy * ph + py)
        for i in range(n):
            dx = xx - bx[i]
            dy = yy - by[i]
            # +1 keeps the centre finite instead of dividing by zero.
            # In place: `field += ...` would rebind a local.
            field[...] += weight[i] * r2 / (dx * dx + dy * dy + 1.0)

        v = field * (255.0 * args.gain / 3.0)
        if args.contour > 1:
            step = 256.0 / args.contour
            v = np.floor(v / step) * step
        return lut[np.clip(v, 0, 255).astype(np.uint8)]

    ds.run(render, args)


if __name__ == "__main__":
    main()
