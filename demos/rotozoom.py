#!/usr/bin/env python3
"""Rotozoomer.

Rotate and scale a tiled texture by working backwards: for each pixel on
screen, apply the inverse transform to find where it lands in the texture, and
read that. Doing it in this direction means every pixel gets exactly one
sample, with no gaps to fill in -- the reason the effect was cheap enough for
hardware that could not multiply quickly.

The texture wraps, so the plane is infinite and the zoom can run as far out as
you like.

Run:  python3 rotozoom.py --host 127.0.0.1
      python3 rotozoom.py --texture plasma --spin 0.35 --zoom-min 0.4
"""

import numpy as np

import demoscene as ds

TEX = 256                              # power of two, so wrapping is a mask


def make_texture(kind, lut):
    ty, tx = np.mgrid[0:TEX, 0:TEX].astype(ds.f32)
    if kind == "checker":
        idx = np.where(((tx.astype(int) >> 5) ^ (ty.astype(int) >> 5)) & 1, 235, 20)
    elif kind == "plasma":
        # Every term must complete a whole number of cycles across the
        # texture, or the tiling shows as hard diagonal seams once it rotates.
        # That rules out the usual radial term, which cannot tile at all.
        k = 2.0 * np.pi / TEX
        p = (np.sin(k * 3 * tx) + np.sin(k * 5 * ty)
             + np.sin(k * 2 * (tx + ty)) + np.sin(k * 4 * (tx - ty)))
        idx = (p + 4.0) * (255.0 / 8.0)
    elif kind == "grid":
        line = ((tx.astype(int) % 32) < 2) | ((ty.astype(int) % 32) < 2)
        idx = np.where(line, 255, 40)
    else:                              # xor
        idx = (tx.astype(int) ^ ty.astype(int)) & 0xFF
    return lut[np.clip(idx, 0, 255).astype(np.uint8)]


def main():
    ap = ds.parser(__doc__.split("\n", 1)[0])
    ds.palette_argument(ap, "rainbow")
    ap.add_argument("--texture", default="plasma",
                    choices=["plasma", "xor", "checker", "grid"])
    ap.add_argument("--spin", type=float, default=0.12, help="turns/sec")
    ap.add_argument("--zoom-min", type=float, default=0.55)
    ap.add_argument("--zoom-max", type=float, default=2.6)
    ap.add_argument("--breathe", type=float, default=0.11,
                    help="zoom cycles/sec")
    ap.add_argument("--drift", type=float, default=14.0,
                    help="texels/sec the plane slides sideways")
    args = ap.parse_args()

    W, H = args.width, args.height
    lut = ds.named_palette(args.palette)
    tex = make_texture(args.texture, lut)

    yy, xx = np.mgrid[0:H, 0:W].astype(ds.f32)
    cx = xx - W / 2.0
    cy = yy - H / 2.0

    zmid = (args.zoom_max + args.zoom_min) / 2.0
    zamp = (args.zoom_max - args.zoom_min) / 2.0

    def render(t, frame):
        angle = 2.0 * np.pi * args.spin * t
        # Zoom moves on a sine so it eases at both ends rather than snapping
        # around when it reaches the limit.
        zoom = zmid + zamp * np.sin(2.0 * np.pi * args.breathe * t)
        ca = ds.f32(np.cos(angle) * zoom)
        sa = ds.f32(np.sin(angle) * zoom)
        ox = ds.f32(args.drift * t)
        oy = ds.f32(args.drift * 0.6 * t)

        u = (ca * cx - sa * cy + ox).astype(np.int32) & (TEX - 1)
        v = (sa * cx + ca * cy + oy).astype(np.int32) & (TEX - 1)
        return tex[v, u]

    ds.run(render, args)


if __name__ == "__main__":
    main()
