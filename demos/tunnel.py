#!/usr/bin/env python3
"""Endless tunnel.

For every pixel, work out the angle around the centre and a depth that grows
as you approach it, then use those as texture coordinates. Scrolling the depth
coordinate flies you forward; scrolling the angle rolls the tunnel.

Both coordinates depend only on the pixel, never on time, so they are computed
once at startup. Each frame is then two integer adds, a mask and a gather --
cheap enough that the panel, not the Pi, sets the frame rate.

Run:  python3 tunnel.py --host 127.0.0.1
      python3 tunnel.py --texture checker --palette magma --roll -0.3
"""

import sys

import numpy as np

import demoscene as ds

TEX = 256                              # power of two, so wrapping is a mask


def make_texture(kind, lut):
    """Build a TEXxTEX RGB texture out of a palette."""
    ty, tx = np.mgrid[0:TEX, 0:TEX]
    if kind == "xor":
        # The XOR pattern: a demoscene staple, and its self-similar blocks
        # give the tunnel wall visible structure at every distance.
        idx = (tx ^ ty) & 0xFF
    elif kind == "checker":
        idx = np.where(((tx >> 5) ^ (ty >> 5)) & 1, 230, 25)
    else:                              # rings
        idx = ((np.hypot(tx - TEX / 2, ty - TEX / 2) * 8) % 256).astype(int)
    return lut[idx.astype(np.uint8)]


def add_arguments(ap):
    ds.palette_argument(ap, "magma")
    ap.add_argument("--texture", default="xor", choices=["xor", "checker", "rings"])
    ap.add_argument("--speed", type=float, default=60.0, help="forward texels/sec")
    ap.add_argument("--roll", type=float, default=0.15, help="turns/sec around the axis")
    ap.add_argument("--depth", type=float, default=900.0,
                    help="depth scale; larger = the walls rush past faster")
    ap.add_argument("--fog", type=float, default=1.0,
                    help="how dark the far end goes, 0..1")
    # Across this panel the depth runs from about 5 out at the sides to several
    # hundred at the centre, so the half-fade point has to sit low in that
    # range for the vanishing point to actually go dark.
    ap.add_argument("--fog-scale", type=float, default=35.0,
                    help="depth at which the walls are half faded")


def build(args):

    W, H = args.width, args.height
    lut = ds.named_palette(args.palette)
    tex = make_texture(args.texture, lut)

    yy, xx = np.mgrid[0:H, 0:W].astype(ds.f32)
    dx = xx - W / 2.0
    dy = yy - H / 2.0
    # Keep the very centre off the singularity, or depth blows up to infinity.
    r = np.maximum(np.hypot(dx, dy), 0.75)

    depth = args.depth / r
    depth0 = depth.astype(np.int32)
    angle0 = (((np.arctan2(dy, dx) / (2.0 * np.pi)) + 0.5) * TEX).astype(np.int32)

    # Shade on depth rather than radius. Radius has to be scaled against the
    # panel, and on one this wide nearly every pixel then counts as near, so
    # only a small disc around the centre darkens and the tunnel loses its
    # vanishing point. Depth is in texture units and needs no such scaling.
    lit = args.fog_scale / (args.fog_scale + depth)
    fog = ((1.0 - args.fog) + args.fog * lit).astype(ds.f32)

    def render(t, frame):
        u = (depth0 + int(t * args.speed)) & (TEX - 1)
        v = (angle0 + int(t * args.roll * TEX)) & (TEX - 1)
        return ds.shade(tex[u, v], fog)

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
