#!/usr/bin/env python3
"""The Amiga twister, laid on its side.

A twister is a bar whose cross section rotates as you travel along it: slice it
anywhere and you get the same square, turned a little further than the slice
before. The 1990 version of this ran vertically because a 320x256 screen is
taller than it is wide. A 320x64 wall is neither, and turning the bar through
ninety degrees is the whole reason it fits -- the ribbon runs off both edges of
the panel, the twist is read along the *long* axis where there are 320 pixels
of it to look at, and 64 rows is plenty for a bar half that thick.

Per column x the cross section is at phase theta = k*x + w*t. Its n corners
project to y offsets of r*cos(theta + i*2pi/n), so the column is a handful of
vertical spans stacked between consecutive corners, one per face. A face is
towards you when the sine of its normal angle is positive, and its brightness
is that same |sin| -- which is also, exactly, how tall it projects, so a face
fades out as it goes edge on and has collapsed to nothing by the time it turns
away. That coupling is what makes the thing read as one solid object being
twisted rather than as a set of coloured stripes: the bright face is always the
wide one.

Everything above depends on (row, phase) and nothing else, so **the entire
demo is one baked table**: 1024 phase steps by the panel's rows, in RGB, built
once. A frame adds a scalar to a baked per-column phase index and does a single
gather through that table. Two numpy calls, no arithmetic over the frame at
all, which on the Pi that drives the wall is the difference between a demo you
can afford next to an expensive one and a demo you cannot.

Two slow modulations keep it from being merely a texture scrolling sideways.
--sway moves the ribbon up and down the panel, which is a row offset into the
table (baked at four sub-pixel positions, so the sway is smooth rather than
stepping a whole row at a time) and costs nothing at all. --breathe varies the
twist rate about the middle of the panel, so the ribbon winds tighter and
slacker; that one turns the per-column index into a multiply-add-and-wrap over
320 elements, three more numpy calls over nothing. Their periods are
deliberately unrelated to the rotation period and to each other.

The palette has to be *cyclic*, for the same reason cycle.py's does: colour is
indexed by the phase, and the phase wraps once a rotation. The named ramps run
dark to bright and do not close, so they are mirrored -- and their black end is
dropped first, since a ribbon whose colour goes to zero has an unlit stretch in
it whatever the lighting says.

Run:  python3 twister.py --host 127.0.0.1
      python3 twister.py --faces 3 --turns 1.5 --palette magma
      python3 twister.py --speed -0.35 --sway 0 --breathe 0
"""

import math
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * math.pi

PHASES = 1024       # phase steps in one full rotation of the cross section
SUBPIXEL = 4        # sub-pixel positions of the ribbon baked for --sway


# --------------------------------------------------------------------------
# Colour.
# --------------------------------------------------------------------------

def hue_table(name, size):
    """A cyclic colour table, indexed by phase.

    Colour comes off the phase, which wraps once per rotation, so entry -1 has
    to meet entry 0 or every wrap shows as a hard seam running along the
    ribbon. `rainbow` closes already; the ramps in demoscene do not, so they
    are played forward then backward. Their bottom third is dropped before
    that: those entries are nearly black, and a face carrying them is a dead
    stretch of ribbon no amount of lighting brings back.
    """
    if name == "rainbow":
        return ds.rainbow(size, dtype=f32)
    ramp = ds.gradient(ds.PALETTES[name], 512, dtype=f32)[160:]
    half = ramp[np.linspace(0, len(ramp) - 1, size // 2).round().astype(int)]
    return np.concatenate([half, half[::-1]], axis=0)[:size]


# --------------------------------------------------------------------------
# The table. This is the whole demo; render() only reads it.
# --------------------------------------------------------------------------

def bake(args, rows, yc, r, n, colour):
    """(rows, PHASES, 3) uint8: what a column looks like at each phase.

    One pass per face over the whole (row, phase) grid. Coverage is the overlap
    of the row's pixel with the face's projected span, which antialiases both
    the silhouette and the joint between two faces -- at 64 rows a hard edge on
    something this slow crawls visibly, and the joints are where the eye reads
    the shape.

    Front facing spans tile the silhouette exactly and never overlap, so the
    coverages can simply be summed.
    """
    p = np.arange(PHASES, dtype=f32) * f32(TAU / PHASES)
    yy = np.arange(rows, dtype=f32)[:, None]
    step = TAU / n
    acc = np.zeros((rows, PHASES, 3), f32)

    for i in range(n):
        ya = yc + r * np.cos(p + i * step)
        yb = yc + r * np.cos(p + (i + 1) * step)
        # The face's outward normal, at the angle halfway between its corners.
        # Positive sine is towards the viewer; |sine| is how face-on it is.
        s = np.sin(p + (i + 0.5) * step)

        cov = np.clip(np.minimum(np.maximum(ya, yb), yy + 0.5)
                      - np.maximum(np.minimum(ya, yb), yy - 0.5), 0.0, 1.0)
        cov *= s > 0.0

        # Lambert with the light at the viewer, which is the same |sin| again:
        # brightness and projected height rise and fall together, so the wide
        # face is always the lit one.
        shade = f32(args.ambient) + f32(1.0 - args.ambient) * np.abs(s)
        rgb = colour * shade[:, None]
        if args.specular > 0.0:
            # Only ever on the face pointing straight down the line of sight,
            # so it appears as a highlight travelling along the ribbon rather
            # than as a general brightening.
            rgb = rgb + f32(255.0 * args.specular) * np.maximum(s, 0.0)[:, None] ** f32(28.0)
        acc += cov[:, :, None] * rgb[None, :, :]

    return np.clip(acc, 0.0, 255.0).astype(np.uint8)


def add_arguments(ap):
    ds.palette_argument(ap, "rainbow")
    ap.add_argument("--faces", type=int, default=4,
                    help="sides of the prism being twisted (3-12)")
    ap.add_argument("--turns", type=float, default=2.0,
                    help="full twists across the panel; negative reverses the lay")
    ap.add_argument("--speed", type=float, default=0.22,
                    help="rotations per second; negative runs it the other way")
    ap.add_argument("--radius", type=float, default=0.38,
                    help="ribbon half-width, as a fraction of the panel height")
    ap.add_argument("--hue-cycles", type=int, default=1,
                    help="times the palette repeats per rotation (whole numbers only)")
    ap.add_argument("--ambient", type=float, default=0.10,
                    help="light on a face that has turned fully edge on, 0..1")
    ap.add_argument("--specular", type=float, default=0.42,
                    help="highlight on the face nearest the viewer (0 = off)")
    ap.add_argument("--sway", type=float, default=3.0,
                    help="vertical drift of the ribbon, pixels either side (0 = centred)")
    ap.add_argument("--sway-period", type=float, default=17.0,
                    help="seconds for one up-and-down of --sway")
    ap.add_argument("--breathe", type=float, default=0.3,
                    help="how far the twist rate winds up and slackens, 0..1 (0 = off)")
    ap.add_argument("--breathe-period", type=float, default=23.0,
                    help="seconds for one cycle of --breathe")


def build(args):
    W, H = args.width, args.height
    n = int(min(12, max(3, args.faces)))
    cycles = max(1, int(args.hue_cycles))

    # The ribbon plus its sway has to fit the panel; the ribbon wins if they
    # cannot both have what they asked for, since a twister that touches the
    # top and bottom rows has nowhere to show the twist.
    r = max(1.5, min(float(args.radius) * H, H / 2.0 - 1.0))
    sway = min(max(0.0, float(args.sway)), max(0.0, H / 2.0 - 1.0 - r))
    pad = int(math.ceil(sway))
    rows = H + 2 * pad + 2
    sub = SUBPIXEL if sway > 0.0 else 1

    # Colour per phase, from a table that closes on itself.
    pal = hue_table(args.palette, PHASES)
    colour = pal[(np.arange(PHASES) * cycles) % PHASES]

    # One table per sub-pixel position of the ribbon, each doubled along the
    # phase axis so a frame can add its scalar shift and index straight in
    # without a modulo.
    yc0 = pad + (H - 1) / 2.0
    tables = [np.concatenate([bake(args, rows, yc0 + s / float(sub), r, n, colour)] * 2,
                             axis=1) for s in range(sub)]

    # Phase per column, centred so --breathe winds the ribbon up about the
    # middle of the panel rather than pinning one end of it.
    kp = float(args.turns) * PHASES / max(W, 1)
    base_f = ((np.arange(W, dtype=f32) - W / 2.0) * f32(kp))
    base_i = np.mod(np.rint(base_f), PHASES).astype(np.intp)

    idx = np.empty(W, np.intp)
    work = np.empty(W, f32)
    out = np.empty((H, W, 3), np.uint8)

    breathe = float(args.breathe) != 0.0
    sway_w = TAU / max(1e-3, float(args.sway_period))
    breathe_w = TAU / max(1e-3, float(args.breathe_period))

    def render(t, frame):
        shift = int(args.speed * t * PHASES) % PHASES

        # --- where the ribbon sits, vertically ------------------------------
        # Slicing `row` rows off the top of the table moves the ribbon up by
        # `row`, and picking sub-pixel table `s` moves it down by s/sub, so
        # row - s/sub is the offset wanted. Both are free.
        row, s = pad, 0
        if sway > 0.0:
            q = int(round((pad - sway * math.sin(sway_w * t)) * sub))
            row = -(-q // sub)                          # ceil, so s >= 0
            s = row * sub - q
            row = min(max(row, 0), rows - H)

        # --- phase per column ------------------------------------------------
        if breathe:
            k = 1.0 + float(args.breathe) * math.sin(breathe_w * t)
            np.multiply(base_f, f32(k), out=work)
            np.add(work, f32(shift), out=work)
            np.mod(work, f32(PHASES), out=work)
            np.copyto(idx, work, casting="unsafe")
        else:
            np.add(base_i, shift, out=idx)

        return np.take(tables[s][row:row + H], idx, axis=1, out=out, mode="clip")

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
