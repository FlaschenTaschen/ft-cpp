#!/usr/bin/env python3
"""Colour-cycled plasma.

The image is computed exactly once, at startup, as a field of palette indices
-- one uint8 per pixel, and it never changes again. All the motion comes from
adding a growing offset to those indices before the palette lookup, so the
colours slide through the shape while the shape itself stands still. That is
the whole 90s trick: a frame is one add and one gather, no trigonometry, no
per-pixel maths that depends on time.

The one thing that has to be right is that the palette is *cyclic*: entry 255
must meet entry 0. Every wrap of the offset walks the whole table past every
pixel, and if the two ends do not match, that mismatch reads as a hard seam
sweeping across the panel once per cycle. `rainbow` closes on itself already;
the ramps in demoscene (fire, ice, ...) run dark to bright and do not, so they
are mirrored here -- played forward then backward -- which closes the loop at
the cost of halving the resolution of the ramp.

--bands repeats the palette several times across the index range. It costs
nothing (the field is just scaled before it is quantized) and it makes the
movement far more legible, since a band edge crosses the screen every cycle
instead of once per whole image.

--pulse breathes the brightness. That is still within the technique: it
modulates the *palette*, not the field, and is done by picking between a
handful of pre-dimmed copies of the table built at startup. Warping the field
per frame would defeat the point and is deliberately not offered.

Run:  python3 cycle.py --host 127.0.0.1
      python3 cycle.py --pattern spiral --arms 5 --speed -60
      python3 cycle.py --pattern marble --palette fire --bands 3 --pulse 0.2
"""

import sys

import numpy as np

import demoscene as ds

PULSE_STEPS = 48        # pre-dimmed palette copies; enough that the breathing
                        # reads as smooth without a visible step


# --------------------------------------------------------------------------
# The static index field. Each of these returns a float array in *palette
# turns*: 1.0 means one whole trip through the table. It is wrapped to a uint8
# afterwards, which is why a pattern may run over any range it likes.
# --------------------------------------------------------------------------

def make_field(args, H, W):
    yy, xx = np.mgrid[0:H, 0:W].astype(ds.f32)
    # Work in units where the *short* axis is 1.0 across, so a pattern keeps
    # its proportions on a 320x64 wall instead of being squashed flat.
    s = ds.f32(min(H, W))
    x = (xx - W / 2.0) / s
    y = (yy - H / 2.0) / s
    k = ds.f32(args.scale)

    if args.pattern == "plasma":
        # Several plane waves at unrelated angles and frequencies. The
        # interference is what stops it looking like stripes.
        v = (np.sin(k * 6.0 * x)
             + np.sin(k * 5.0 * (0.8 * x + 0.6 * y) + 1.3)
             + np.sin(k * 8.0 * (0.3 * x - 0.95 * y) + 2.7)
             + np.sin(k * 3.5 * (x + 1.7 * y) + 0.4))
        v = v * 0.25                                    # -> roughly -1..1

    elif args.pattern == "rings":
        # Ripples from a few off-centre sources, added like waves in water.
        v = np.zeros((H, W), ds.f32)
        for cx, cy, f in ((-0.9, -0.15, 9.0), (1.1, 0.2, 7.0), (0.1, 0.35, 12.0)):
            r = np.hypot(x - cx, y - cy)
            v += np.sin(k * f * r)
        v = v / 3.0

    elif args.pattern == "marble":
        # Turbulence: |sin| summed over octaves, each half the amplitude and
        # twice the frequency, then used to bend a smooth ramp. The absolute
        # value is what gives the creases that read as veining.
        turb = np.zeros((H, W), ds.f32)
        amp, freq = ds.f32(1.0), ds.f32(2.5)
        for _ in range(4):
            turb += amp * np.abs(np.sin(k * freq * (x + 0.7 * y + 0.31 * freq))
                                 * np.sin(k * freq * (y - 0.4 * x)))
            amp, freq = amp * ds.f32(0.5), freq * ds.f32(2.0)
        v = np.sin(k * 2.5 * (x + 1.2 * y) + 2.0 * turb)

    else:                                               # spiral
        # Angle times a whole number of arms, plus radius: cycling the palette
        # then makes the arms crawl inward or outward forever. The arm count
        # must be an integer or the -pi/+pi seam in the angle shows.
        theta = np.arctan2(y, x) / ds.f32(2.0 * np.pi)   # -0.5 .. 0.5
        r = np.hypot(x, y)
        return args.arms * theta + k * 4.0 * r           # already in turns

    # The wave patterns land in about -1..1 but not exactly; normalizing to
    # 0..1 means --bands is honest about how many times the palette repeats.
    lo, hi = float(v.min()), float(v.max())
    return (v - lo) / max(hi - lo, 1e-6)


# --------------------------------------------------------------------------
# The palette, which must close on itself.
# --------------------------------------------------------------------------

def cyclic_palette(name, size=256):
    """A lookup table whose last entry meets its first.

    rainbow is a full hue sweep and already wraps. The named ramps go dark to
    bright, so they are mirrored: the first half runs the ramp forward, the
    second half runs it back, and the join at each end is a repeat rather than
    a jump.
    """
    if name == "rainbow":
        return ds.rainbow(size)
    half = ds.named_palette(name, size // 2)
    return np.concatenate([half, half[::-1]], axis=0)


def add_arguments(ap):
    ds.palette_argument(ap, "rainbow")
    ap.add_argument("--pattern", default="plasma",
                    choices=["plasma", "rings", "marble", "spiral"])
    ap.add_argument("--speed", type=float, default=45.0,
                    help="palette entries per second; negative reverses")
    ap.add_argument("--bands", type=int, default=2,
                    help="times the palette repeats across the field")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="feature size; bigger is busier")
    ap.add_argument("--arms", type=int, default=4, help="spiral arms")
    ap.add_argument("--pulse", type=float, default=0.0,
                    help="palette brightness breathing, Hz (0 = off)")
    ap.add_argument("--pulse-depth", type=float, default=0.55,
                    help="how far --pulse dims at its darkest")


def build(args):
    H, W = args.height, args.width

    turns = make_field(args, H, W) * max(args.bands, 1)
    # One quantization, once. Wrapping here rather than clipping is what lets
    # --bands work at all, and is safe precisely because the palette is cyclic.
    field = np.mod(turns * 256.0, 256.0).astype(np.uint8)

    lut = cyclic_palette(args.palette)
    if args.pulse:
        depth = float(np.clip(args.pulse_depth, 0.0, 1.0))
        levels = 1.0 - depth * (0.5 - 0.5 * np.cos(
            np.linspace(0.0, 2.0 * np.pi, PULSE_STEPS, endpoint=False)))
        luts = [(lut.astype(ds.f32) * s).astype(np.uint8) for s in levels]
    else:
        luts = None

    # Reused between frames: the shifted index field and the output image.
    idx = np.empty((H, W), np.uint8)
    out = np.empty((H, W, 3), np.uint8)

    def render(t, frame):
        np.add(field, np.uint8(int(args.speed * t) & 0xFF), out=idx)
        table = lut
        if luts is not None:
            table = luts[int(args.pulse * PULSE_STEPS * t) % PULSE_STEPS]
        return np.take(table, idx, axis=0, out=out)

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
