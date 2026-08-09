#!/usr/bin/env python3
"""After Dark's flying toasters, crossing a black screen forever.

Berkeley Systems shipped this in 1989 and it became the thing people put on a
monitor to prove the monitor was theirs. Chrome toasters with flapping wings
and slices of toast, drifting across pure black at a pace that asks nothing of
you. It is not from a hacker film -- it is from the machines the films were
about -- and on a wall in a workshop it is the most immediately recognised
thing in this whole rotation.

**The angle is changed on purpose.** The original flies upper-right to
lower-left at roughly 45 degrees, which on a 320x64 panel means a toaster is on
screen for about a second and a half and the effect reads as rain. So the
slope here is set to exactly (H + sprite height) / (W + sprite width) -- the
panel's own diagonal -- which on this letterbox is about 1 in 4. A toaster
enters at the top right, crosses the whole panel, and leaves at the bottom
left, which is the shape of the original gesture even though it is not the
original angle.

**That slope is also what makes it loop.** Because a sprite covers exactly one
panel-width horizontally in the same time it covers one panel-height
vertically, both wrap together, and the whole field returns to its starting
arrangement every `--period` seconds. The flap rate is set to a whole number of
cycles in that period for the same reason. So the demo is exactly periodic and
render() is a pure function of t -- which is what ftsched, the preview baker
and the wall's drifting frame clock all need, and it also means the loop point
is invisible.

**The art is text.** Every sprite is a grid of characters with a palette
beside it, which is how the pixel-art demos in this directory are all written:
it diffs legibly, it can be edited without a tool, and a row of the wrong
length is caught by an assertion at build rather than by a smear on the wall.
The four wing positions are four such grids, played 0-1-2-3-2-1 so the
downstroke and the upstroke are the same drawing seen twice, which is both
half the art and how real animation cheats.

**Cost.** A frame is a black fill and one masked copy per sprite -- eleven
sprites, four array operations each. There is no per-pixel arithmetic anywhere
and nothing is scaled or rotated at run time, because both wings and bodies
were baked in build(). On the 600 MHz Pi 3 that drives this wall that matters
more than any cleverness would have.

Run:  python3 toasters.py --host 127.0.0.1
      python3 toasters.py --toasters 10 --toast 6 --period 22
      python3 toasters.py --flaps 2
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# Chrome is not grey: it is a bright top, a dark underside, and a hard
# highlight where the two meet. Three values and a near-black slot are enough
# to say "polished metal" at this size, and any more shading turns to mud.
PALETTE = {
    ".": None,                          # transparent
    "H": (245, 248, 255),               # highlight
    "B": (185, 195, 210),               # body
    "D": (95, 105, 125),                # shaded underside
    "S": (18, 20, 26),                  # the slot
    "L": (140, 150, 168),               # the lever
    "W": (250, 252, 255),               # wing, leading edge
    "w": (170, 180, 200),               # wing, trailing feathers
    "T": (215, 155, 75),                # toast
    "c": (150, 96, 38),                 # crust
}

TOASTER = (
    "..HHHHHHHHHHHHHH....",
    ".HBBBBBBBBBBBBBBH...",
    ".HBSSSSSSSSSSSSBH...",
    ".HBBBBBBBBBBBBBBH.L.",
    ".HBBBBBBBBBBBBBBHLL.",
    ".HBBBBBBBBBBBBBBH...",
    ".HBBBBBBBBBBBBBBH...",
    ".DBBBBBBBBBBBBBBD...",
    ".DDBBBBBBBBBBBBDD...",
    "..DDDDDDDDDDDDDD....",
    "...DD........DD.....",
    "....................",
)

# Four wing positions: fully raised, half down, level, fully down. Played
# 0-1-2-3-2-1, so a flap is six frames from four drawings.
WINGS = (
    ("....WW........",
     "...WWWW.......",
     "..WWWWWW......",
     "..WWWWWWW.....",
     ".WWWWWWWWW....",
     ".wwwwwwwwww...",
     "..wwwwwwww....",
     "...wwwwww.....",
     "....www.......",
     ".............."),
    ("..............",
     "...WW.........",
     "..WWWWW.......",
     "..WWWWWWW.....",
     ".WWWWWWWWWW...",
     ".wwwwwwwwwww..",
     "..wwwwwwwww...",
     "...wwwwww.....",
     "....ww........",
     ".............."),
    ("..............",
     "..............",
     "..WW..........",
     "..WWWWW.......",
     ".WWWWWWWWW....",
     ".wwwwwwwwwwww.",
     "..wwwwwwwwww..",
     "...wwwwww.....",
     "..............",
     ".............."),
    ("..............",
     "..............",
     "..............",
     "..WW..........",
     ".WWWWWW.......",
     ".wwwwwwwwww...",
     "..wwwwwwwwwww.",
     "...wwwwwwwww..",
     "....wwwwww....",
     ".............."),
)

FLAP_ORDER = (0, 1, 2, 3, 2, 1)

# A slice of bread is round at the top and square at the bottom. Rounding both
# ends -- which is what this was first drawn as -- produces a circle, and a
# small orange circle on a black panel is an orange.
TOAST = (
    "..ccccccc..",
    ".cTTTTTTTc.",
    "cTTTTTTTTTc",
    "cTTTTTTTTTc",
    "cTTTTTTTTTc",
    "cTTTTTTTTTc",
    "cTTTTTTTTTc",
    "cTTTTTTTTTc",
    "cTTTTTTTTTc",
    "ccccccccccc",
)

# Where the wing sits on the composite sprite, and the body under it. The body
# hides only the wing's last row, so the shoulder is tucked behind the chrome
# but the whole downstroke still clears the bodywork -- put the body any higher
# and the wing vanishes for two frames out of six.
SPRITE_W, SPRITE_H = 22, 21
WING_AT = (4, 0)
BODY_AT = (1, 9)


def add_arguments(ap):
    ap.add_argument("--toasters", type=int, default=7)
    ap.add_argument("--toast", type=int, default=4,
                    help="slices of toast, which have no wings and never did")
    ap.add_argument("--period", type=float, default=16.0,
                    help="seconds for the field to return to its starting "
                         "arrangement; also how long a slow toaster takes to "
                         "cross the whole panel")
    ap.add_argument("--flaps", type=float, default=3.0,
                    help="wing beats per second. Rounded to a whole number of "
                         "beats per period so the loop stays exact")
    ap.add_argument("--seed", type=int, default=3,
                    help="the scatter of starting positions and speeds")


def _sprite(rows, name):
    """A character grid -> (RGB uint8, mask). Rows must all be the same length."""
    width = len(rows[0])
    for i, row in enumerate(rows):
        if len(row) != width:
            raise ValueError("%s row %d is %d characters, expected %d"
                             % (name, i, len(row), width))
    h = len(rows)
    rgb = np.zeros((h, width, 3), np.uint8)
    mask = np.zeros((h, width), bool)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            colour = PALETTE[ch]
            if colour is None:
                continue
            rgb[y, x] = colour
            mask[y, x] = True
    return rgb, mask


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)

    # ------------------------------------------------------------- the sprites
    body_rgb, body_mask = _sprite(TOASTER, "TOASTER")
    toast_rgb, toast_mask = _sprite(TOAST, "TOAST")

    frames = []
    for n, wing in enumerate(WINGS):
        wing_rgb, wing_mask = _sprite(wing, "WINGS[%d]" % n)
        rgb = np.zeros((SPRITE_H, SPRITE_W, 3), np.uint8)
        mask = np.zeros((SPRITE_H, SPRITE_W), bool)
        for src_rgb, src_mask, (ax, ay) in ((wing_rgb, wing_mask, WING_AT),
                                            (body_rgb, body_mask, BODY_AT)):
            sh, sw = src_mask.shape
            tile_rgb = rgb[ay:ay + sh, ax:ax + sw]
            tile_mask = mask[ay:ay + sh, ax:ax + sw]
            tile_rgb[src_mask] = src_rgb[src_mask]
            tile_mask |= src_mask
        frames.append((rgb, mask))

    # The two kinds of flier, and the box each sweeps through. Wrapping over
    # (W + sprite width) rather than W is what lets a sprite leave the panel
    # completely before it comes back on the other side.
    kinds = []
    for rgb, mask in frames:
        kinds.append((rgb, mask))
    toast_kind = (toast_rgb, toast_mask)

    span_x = float(W + SPRITE_W)
    span_y = float(H + SPRITE_H)

    n_toaster = max(0, int(args.toasters))
    n_toast = max(0, int(args.toast))
    n = n_toaster + n_toast
    is_toast = np.zeros(n, bool)
    is_toast[n_toaster:] = True

    period = max(float(args.period), 1.0)
    # Speeds are whole multiples of one panel-crossing per period, which is
    # what keeps every sprite's wrap commensurate with every other one's and
    # the whole field exactly periodic. Two speeds is enough variety; three
    # starts to look like depth, which the original did not have.
    laps = rng.choice((1.0, 2.0), size=n, p=(0.72, 0.28))
    vx = laps * span_x / period
    vy = laps * span_y / period

    x0 = rng.uniform(0.0, span_x, n)
    y0 = rng.uniform(0.0, span_y, n)
    # Whole beats per period, so the wings come back to the same position at
    # the loop point too.
    beats = max(1.0, round(float(args.flaps) * period))
    flap_hz = beats / period
    flap_phase = rng.uniform(0.0, 1.0, n)

    out = np.zeros((H, W, 3), np.uint8)

    def render(t, frame):
        out[:] = 0
        # Right to left and downwards, the way the original flew, and both
        # coordinates wrap over their own span.
        xs = (x0 - vx * t) % span_x - SPRITE_W
        ys = (y0 + vy * t) % span_y - SPRITE_H
        ph = (flap_phase + flap_hz * t) % 1.0
        wing_frame = (ph * len(FLAP_ORDER)).astype(np.int32)

        for i in range(n):
            if is_toast[i]:
                rgb, mask = toast_kind
            else:
                rgb, mask = kinds[FLAP_ORDER[int(wing_frame[i])]]
            sh, sw = mask.shape
            x = int(xs[i])
            y = int(ys[i])
            # Clip to the panel. A sprite is always either fully inside its
            # wrap box or hanging off exactly one edge, so one clipped copy is
            # always enough -- there is no need to draw it twice.
            sx0 = max(0, -x)
            sy0 = max(0, -y)
            sx1 = min(sw, W - x)
            sy1 = min(sh, H - y)
            if sx0 >= sx1 or sy0 >= sy1:
                continue
            dx0, dy0 = x + sx0, y + sy0
            sub_mask = mask[sy0:sy1, sx0:sx1]
            dst = out[dy0:dy0 + (sy1 - sy0), dx0:dx0 + (sx1 - sx0)]
            dst[sub_mask] = rgb[sy0:sy1, sx0:sx1][sub_mask]
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
