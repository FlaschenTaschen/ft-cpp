#!/usr/bin/env python3
"""Flight through a starfield.

Stars sit at random points in a box ahead of the camera and drift towards it.
Perspective is the whole effect: dividing by depth means a star crawls while
it is far away and then sweeps past, so the same handful of points reads as
motion through space.

Each star is drawn several times along the distance it covered during the
frame, which is what makes the streaks at speed. Everything is one flat array
operation over all stars at once.

Run:  python3 starfield.py --host 127.0.0.1
      python3 starfield.py --stars 800 --speed 1.6 --warp 6 --palette ice
"""

import sys

import numpy as np

import demoscene as ds


def add_arguments(ap):
    ap.add_argument("--stars", type=int, default=260)
    ap.add_argument("--speed", type=float, default=1.1, help="depth units/sec")
    ap.add_argument("--warp", type=int, default=4,
                    help="samples drawn along each star's travel; 1 = dots")
    ap.add_argument("--spread", type=float, default=1.6,
                    help="field of view; larger = stars sweep out sooner")
    ap.add_argument("--bloom", type=float, default=0.3,
                    help="stars closer than this get a 2x2 core; 0 for all dots")
    ap.add_argument("--tint", default="none",
                    choices=["none", "ice", "fire", "toxic", "magma", "rainbow"],
                    help="colour stars from a palette instead of white")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def build(args):

    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    N = args.stars

    # Depth runs from just in front of the camera out to 1. Spreading the
    # initial depths evenly stops the field arriving as one pulse.
    x = rng.uniform(-1.0, 1.0, N).astype(ds.f32)
    y = rng.uniform(-1.0, 1.0, N).astype(ds.f32)
    z = rng.uniform(0.05, 1.0, N).astype(ds.f32)

    if args.tint == "none":
        star_col = np.full((N, 3), 255, np.uint8)
    else:
        lut = ds.named_palette(args.tint)
        star_col = lut[rng.integers(140, 256, N)]

    cx, cy = W / 2.0, H / 2.0
    sx = cx / args.spread
    sy = cy / args.spread
    frame_rgb = np.zeros((H, W, 3), np.uint8)
    last_t = [0.0]

    def render(t, idx):
        dt = max(0.0, t - last_t[0])
        last_t[0] = t
        travel = args.speed * dt

        z[:] -= travel                 # in place: `z -= ...` would rebind a local
        # Recycle stars that have gone past the camera.
        gone = z < 0.05
        n = int(gone.sum())
        if n:
            x[gone] = rng.uniform(-1.0, 1.0, n)
            y[gone] = rng.uniform(-1.0, 1.0, n)
            z[gone] = 1.0

        frame_rgb[:] = 0
        # Brightness carries the whole sense of distance, since every star is
        # one pixel. An exponent above 1 crushes the mid range into darkness
        # and the field stops reading as depth at all, so stay below it and
        # keep a floor so far stars are faint rather than absent.
        bright = 0.10 + 0.90 * np.clip(1.0 - z, 0.0, 1.0) ** 0.65

        def plot(pxs, pys, fades):
            """Draw a set of stars, brightest-wins where they overlap."""
            on = (pxs >= 0) & (pxs < W) & (pys >= 0) & (pys < H)
            if not on.any():
                return
            col = (star_col[on] * fades[on, None]).astype(np.uint8)
            # Brightest wins, so a close star stays on top of a distant one
            # instead of letting array order decide.
            np.maximum.at(frame_rgb, (pys[on], pxs[on]), col)

        for step in range(args.warp):
            # Sample back along where the star came from this frame.
            zs = z + travel * (step / max(1, args.warp))
            inv = 1.0 / zs
            px = (cx + x * inv * sx).astype(np.int32)
            py = (cy + y * inv * sy).astype(np.int32)
            # Trailing samples fade, so the streak tapers behind the star.
            fade = bright * (1.0 - 0.75 * step / max(1, args.warp))
            plot(px, py, fade)

            # A single pixel gives a near star no more weight than a far one,
            # and the field then reads as static rather than as motion. Widen
            # the closest ones so something is actually rushing past.
            if step == 0 and args.bloom > 0:
                near = z < args.bloom
                for ox, oy in ((1, 0), (0, 1), (1, 1)):
                    plot(np.where(near, px + ox, -1), py + oy, fade * 0.7)

        return frame_rgb

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
