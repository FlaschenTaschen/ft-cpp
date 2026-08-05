#!/usr/bin/env python3
"""Mode-7 floor.

A horizon across the top, a textured ground plane running away to it, and a
sky above. Every row below the horizon looks at the ground at one fixed
distance, so per row there is a single depth and a single constant step across
the texture -- the classic trick that let 16-bit hardware draw a perspective
plane without dividing per pixel.

All the per-row constants are built once. Each frame is then an add, a
truncate, a mask and a gather, plus one multiply-add for the haze.

Run:  python3 floor.py --host 127.0.0.1
      python3 floor.py --texture road --palette ice --speed 60
"""

import sys

import numpy as np

import demoscene as ds

TEX = 64                               # power of two, so wrapping is a mask
LODS = 7                               # filtered copies, 1 to TEX texels wide
BIG = 1 << 16                          # keeps texture coords positive


def make_texture(kind, dark, light):
    """A TEXxTEX RGB tile. v runs into the screen, u runs across."""
    v, u = np.mgrid[0:TEX, 0:TEX]
    idx = np.zeros((TEX, TEX), bool)
    if kind == "checker":
        idx = (((u >> 3) ^ (v >> 3)) & 1).astype(bool)
    elif kind == "grid":
        idx = ((u % 16) < 2) | ((v % 16) < 2)
    else:                              # road
        # Lanes 16 texels wide: a solid line on the boundary and a dashed one
        # up the middle. Both periods divide TEX exactly, or the markings jump
        # every time the tile wraps. 16 rather than one road per tile because
        # the near edge of this panel is only ~50 texels across, so a wider
        # lane would put the only marking off screen half the time.
        idx = (u % 16) < 2
        idx |= (np.abs((u % 16) - 8) < 1) & ((v % 16) < 8)
    out = np.empty((TEX, TEX, 3), ds.f32)
    out[:] = np.asarray(dark, ds.f32)
    out[idx] = np.asarray(light, ds.f32)
    return out


def mip_stack(tile, levels):
    """(lv, lu, TEX, TEX, 3): the tile box-filtered 2^lv deep by 2^lu across.

    Separate levels for the two axes because the footprint here is wildly
    anisotropic: a row near the horizon covers hundreds of texels of depth
    while still stepping only a few across, so one isotropic level would
    either leave the depth aliasing or smear away the convergence lines that
    are the whole point. Rolling the average keeps it seamless, which a plain
    box filter over the edges would not.
    """
    def chain(a, axis):
        out, cur, shift = [a], a, 0
        for k in range(1, levels):
            s = 1 << (k - 1)
            cur = 0.5 * (cur + np.roll(cur, -s, axis=axis))
            shift += s                      # the window grew rightwards
            out.append(np.roll(cur, shift // 2, axis=axis))
        return out
    rows = chain(tile, 0)
    return np.stack([np.stack(chain(r, 1)) for r in rows]).astype(ds.f32)


def make_sky(rows, width, lut, sun):
    """A width-periodic sky strip, doubled so a wrapping slice is seamless."""
    if rows <= 0:
        return np.zeros((0, 2 * width, 3), np.uint8), np.asarray(lut[150], ds.f32) * 0.7
    y = np.linspace(0.0, 1.0, rows, dtype=ds.f32)[:, None]
    top = np.asarray(lut[30], ds.f32)
    bottom = np.asarray(lut[165], ds.f32)
    strip = top + (bottom - top) * (y ** 2.0)[..., None]
    strip = np.repeat(strip, width, axis=1)
    if sun > 0:
        # Sits on the horizon, centred in the period so it wraps cleanly.
        x = np.arange(width, dtype=ds.f32) - width / 2.0
        yy = (np.arange(rows, dtype=ds.f32) - (rows - 1))[:, None]
        r = np.hypot(x[None, :] / max(sun, 1e-6), yy / max(sun * 0.35, 1e-6))
        glow = np.clip(1.0 - r, 0.0, 1.0)[..., None] ** 1.5
        strip += (np.asarray(lut[250], ds.f32) - strip) * glow
    strip = np.clip(strip, 0, 255)
    # The ground fades into a slightly darker haze than the sky it meets, so
    # the horizon stays a visible edge instead of the two washing together.
    haze = strip[-1, 0] * ds.f32(0.7)
    return np.concatenate([strip, strip], axis=1).astype(np.uint8), haze


def add_arguments(ap):
    ds.palette_argument(ap, "ice")
    ap.add_argument("--texture", default="checker",
                    choices=["checker", "grid", "road"])
    ap.add_argument("--speed", type=float, default=45.0, help="forward texels/sec")
    ap.add_argument("--steer", type=float, default=0.42,
                    help="peak heading swing, radians")
    ap.add_argument("--steer-rate", type=float, default=0.055,
                    help="steering oscillations/sec")
    # A little under half the panel. On 320x64 that leaves ~37 rows of ground,
    # which is already few enough that pushing the horizon lower costs more
    # perspective than the extra sky is worth.
    ap.add_argument("--horizon", type=float, default=0.42,
                    help="horizon height as a fraction of the panel")
    ap.add_argument("--cam-height", type=float, default=6.0,
                    help="eye height in texels; larger = smaller tiles")
    # A fraction of the width, not pixels: the field of view then stays put
    # when the panel does not, and the whole depth ramp comes out the same
    # shape on 128x32 as on 320x64 instead of collapsing into haze halfway up.
    ap.add_argument("--focal", type=float, default=0.2,
                    help="focal length as a fraction of the width; "
                         "larger = flatter recession")
    ap.add_argument("--fog", type=float, default=1.0,
                    help="how completely the ground fades into haze, 0..1")
    ap.add_argument("--fog-scale", type=float, default=55.0,
                    help="depth at which the ground is half hazed")
    ap.add_argument("--no-mip", dest="mip", action="store_false",
                    help="sample the raw tile; crunchier, and it shimmers")
    ap.add_argument("--sun", type=float, default=0.15,
                    help="sun radius as a fraction of the width (0 = none)")


def build(args):

    W, H = args.width, args.height
    focal = args.focal * W
    lut = ds.named_palette(args.palette)
    tex = mip_stack(make_texture(args.texture, lut[25], lut[215]), LODS)

    hy = args.horizon * H                       # horizon, in rows
    sky_rows = int(min(max(round(hy), 0), H - 1))
    sky, haze = make_sky(sky_rows, W, lut, args.sun * W)

    # Per row: how far away the ground is, and how far across the texture one
    # screen pixel steps. Both follow from the row's distance below the
    # horizon, so they are constants.
    y = np.arange(sky_rows, H, dtype=ds.f32) + 0.5
    dy = np.maximum(y - hy, 0.5)[:, None]       # (rows, 1)
    z = args.cam_height * focal / dy       # depth, texels
    # (x - W/2) * z / focal, with the focal length cancelling out.
    u0 = (np.arange(W, dtype=ds.f32) - W / 2.0)[None, :] * (args.cam_height / dy)
    u0 = u0 + ds.f32(BIG)                       # so the int cast never truncates
                                                # toward zero across u = 0

    # Which filtered copy of the tile each row wants: one screen pixel spans
    # z/focal texels across and one screen row spans z/dy texels of depth.
    # Round up and then one further, never to nearest: a box filter only just
    # as wide as the sample spacing still lets a thin lane marking fall
    # between two samples, and a line that flickers in and out along its
    # length reads as a field of dashes rather than as a line getting fainter.
    top = LODS - 1 if args.mip else 0

    def lod(f):
        k = np.where(f <= 1.0, 0.0, np.ceil(np.log2(np.maximum(f, 1.0))) + 1.0)
        return np.clip(k, 0, top).astype(np.int32)

    lu = lod(z / focal)
    lv = lod(z / dy)

    # Fog on top of that: shading on depth, not on the screen row, so it does
    # not need retuning when the panel changes shape -- the same lesson
    # tunnel.py learned about radius on a 320-wide display.
    lit = args.fog_scale / (args.fog_scale + z)
    lit = ((1.0 - args.fog) + args.fog * lit).astype(ds.f32)[..., None]
    haze_add = (np.asarray(haze, ds.f32) * (1.0 - lit)).astype(ds.f32)

    out = np.zeros((H, W, 3), np.uint8)
    ground = np.empty((H - sky_rows, W, 3), ds.f32)
    w = 2.0 * np.pi * args.steer_rate

    def render(t, frame):
        theta = args.steer * np.sin(w * t)
        # Heading and sideways drift are the same motion: driving forward at an
        # angle is what slides you across the plane, so the lateral offset is
        # the integral of speed * theta rather than a second free wobble.
        cam_x = -(args.speed * args.steer / w) * np.cos(w * t)
        cam_z = args.speed * t

        # theta * z is the yaw: far rows swing further than near ones, which is
        # what makes the vanishing point drift instead of the whole plane
        # sliding rigidly.
        u = u0 + (z * ds.f32(theta) + ds.f32(cam_x))
        ui = u.astype(np.int32) & (TEX - 1)
        vi = (z + ds.f32(cam_z + BIG)).astype(np.int32) & (TEX - 1)

        np.multiply(tex[lv, lu, vi, ui], lit, out=ground)
        np.add(ground, haze_add, out=ground)
        out[sky_rows:] = ground.astype(np.uint8)

        if sky_rows:
            # Infinitely distant things sit at focal * heading off centre,
            # the same place the plane's own vanishing point goes, so the sun
            # and the convergence swing together rather than against.
            off = int(theta * focal) % W
            out[:sky_rows] = sky[:, off:off + W]
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
