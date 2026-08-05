#!/usr/bin/env python3
"""Doom-style fire.

The classic effect: a row of heat along the bottom, and every frame each cell
takes the heat of the cell below it, shifted sideways at random and cooled a
little. The randomness is what turns a smooth gradient into flames.

Done here as one vectorized step over the whole buffer rather than the usual
per-pixel loops, so a 320x64 panel costs a handful of numpy operations per
frame instead of twenty thousand iterations.

Run:  python3 fire.py --host 127.0.0.1
      python3 fire.py --palette ice --wind -0.4 --cool 4
"""

import numpy as np

import demoscene as ds


def main():
    ap = ds.parser(__doc__.split("\n", 1)[0])
    ds.palette_argument(ap, "fire")
    # The classic effect is tuned for a ~170 row screen. Over 64 rows the heat
    # has to fall roughly three times faster or it never reaches the top and
    # the panel is a solid sheet of orange.
    ap.add_argument("--cool", type=int, default=8,
                    help="maximum heat lost per row; higher = shorter flames")
    ap.add_argument("--wind", type=float, default=0.0,
                    help="sideways bias, -1..1; 0 blows straight up")
    ap.add_argument("--source", type=int, default=2,
                    help="rows of burning fuel at the bottom")
    ap.add_argument("--flicker", type=float, default=0.55,
                    help="how much the fuel varies along the width, 0..1")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")
    args = ap.parse_args()

    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    lut = ds.named_palette(args.palette)

    heat = np.zeros((H, W), np.int16)
    cols = np.arange(W)
    xs = np.arange(W, dtype=ds.f32)

    # The sideways shift is -1, 0 or +1 per cell. `wind` biases which of those
    # is likely, so the flames lean without ever moving more than a pixel per
    # row, which would tear them apart.
    lean = float(np.clip(args.wind, -1.0, 1.0))
    weights = np.array([1.0 - lean, 2.0, 1.0 + lean], np.float64)
    weights /= weights.sum()

    def render(t, frame):
        # Fuel: how hot the bottom burns varies along the width, on a few slow
        # travelling waves. White noise here would only make the whole sheet
        # shimmer; a smooth envelope is what gives separate tongues that
        # wander sideways, because a cooler column dies out lower down.
        wave = (np.sin(xs * 0.075 + t * 1.9)
                + np.sin(xs * 0.029 - t * 1.3)
                + np.sin(xs * 0.011 + t * 0.7)) / 3.0            # -1..1
        env = 0.5 + 0.5 * wave                                   # 0..1
        level = 255.0 * (1.0 - args.flicker * (1.0 - env))
        base = level[None, :] - rng.random((args.source, W)) * 30.0
        heat[H - args.source:] = np.clip(base, 0, 255).astype(np.int16)

        # Every row takes from the row below, displaced sideways and cooled.
        below = heat[1:]
        shift = rng.choice([-1, 0, 1], size=below.shape, p=weights)
        take = (cols[None, :] + shift) % W
        cooled = np.take_along_axis(below, take, axis=1)
        cooled -= rng.integers(0, args.cool + 1, size=below.shape, dtype=np.int16)
        np.maximum(cooled, 0, out=cooled)
        heat[:-1] = cooled

        return lut[heat.clip(0, 255)]

    ds.run(render, args)


if __name__ == "__main__":
    main()
