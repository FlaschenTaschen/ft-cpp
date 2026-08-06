#!/usr/bin/env python3
"""Bake a second pose for each space invader, from the sprite we already have.

The four invaders came off the old Pi as single stills, so the row of them sat
on the wall like a printed banner. In the arcade they animate, and the whole
trick is that it is only two frames: every invader in the formation flips
between them in lockstep, and that alone is what makes a grid of monochrome
shapes read as a swarm of things rather than as wallpaper.

The second frame is not redrawn here. These sprites are 64x43 renders with
antialiased edges, and hand-drawing a matching pose would mean matching that
antialiasing pixel by pixel; anything close but not exact would show up as a
flicker in the outline every time the pose changed. So instead the limbs are
cut out of the existing artwork and moved, which is what the original
animation is anyway -- the body never changes, the legs and arms do. The edges
therefore stay byte-identical between the two poses and only the parts that
are meant to move, move.

Each move is (rows, cols, dy, dx): lift that block out, leave background
behind, and put it down somewhere else. The blocks are one original pixel --
about four here -- because these are 4x upscales and a shift of anything else
would land the limb off its own grid.

Run:  python3 scripts/make-invader-poses.py            # writes *b.png
      python3 scripts/make-invader-poses.py --check    # contact sheet only
"""

import argparse
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, os.pardir, "pixelart")

BLOCK = 4                                    # one original pixel, upscaled

# (rows, cols, dy, dx) per sprite. Rows and cols are half-open.
MOVES = {
    # Squid. Below the body sit three pairs of tentacles; the outer pair tucks
    # in towards the middle while the inner pair stays put, so the silhouette
    # narrows and widens. The column groups stop short of the middle tentacle
    # on purpose -- a group that straddles a limb tears it in half.
    "space-invaders-1": [
        ((31, 34), (19, 23), 0, -BLOCK),     # outer stubs splay out
        ((31, 34), (41, 45), 0, +BLOCK),
        ((34, 39), (14, 19), 0, -BLOCK),     # outer feet follow them
        ((34, 39), (43, 49), 0, +BLOCK),
    ],
    # Crab. The one with arms, and the arms coming down is its whole
    # animation: antennae and both outer verticals drop by a pixel together
    # and the feet swing in underneath.
    "space-invaders-2": [
        ((5, 9), (18, 46), +BLOCK, 0),       # antennae
        ((9, 18), (9, 14), +BLOCK, 0),       # left arm
        ((9, 18), (49, 54), +BLOCK, 0),      # right arm
        ((34, 38), (14, 19), 0, +BLOCK),     # feet swing in
        ((34, 38), (45, 50), 0, -BLOCK),
    ],
    # Octopus. Squat and wide, and its legs splay outwards rather than in --
    # opposite to the squid, so the two never read as the same animation
    # happening twice in the same row.
    "space-invaders-3": [
        ((31, 36), (14, 27), 0, -BLOCK),
        ((31, 36), (36, 50), 0, +BLOCK),
    ],
    # The wide one. It is nearly panel-width already, so its outer legs only
    # tuck in; splaying them would run them off its own canvas.
    "space-invaders-4": [
        ((30, 37), (8, 21), 0, +BLOCK),
        ((30, 37), (44, 57), 0, -BLOCK),
    ],
}

BACKGROUND = 255                             # these are drawn on white


FRINGE = 2                                   # px of antialiasing around a limb


def pose_two(img, moves):
    """Apply the limb moves to a copy of the sprite."""
    out = np.array(img, np.uint8)
    lifted = []
    for (y0, y1), (x0, x1), dy, dx in moves:
        lifted.append((np.array(out[y0:y1, x0:x1]), y0 + dy, x0 + dx))
        out[y0:y1, x0:x1] = BACKGROUND       # leave background where it was
        # A limb's antialiasing spills a pixel or two outside the box it is
        # lifted in, and clearing only the box leaves that spill behind as a
        # grey outline of where the limb used to be. So sweep the ring around
        # the box too -- but only the soft pixels: anything solid there
        # belongs to a limb that is staying put, and its own fringe going
        # slightly softer is not visible on a panel this size.
        ry0, ry1 = max(0, y0 - FRINGE), min(out.shape[0], y1 + FRINGE)
        rx0, rx1 = max(0, x0 - FRINGE), min(out.shape[1], x1 + FRINGE)
        ring = out[ry0:ry1, rx0:rx1]
        soft = (ring.min(axis=2) >= 128) & (ring.min(axis=2) < BACKGROUND)
        ring[soft] = BACKGROUND
    for block, y, x in lifted:
        h, w = block.shape[:2]
        if y < 0 or x < 0 or y + h > out.shape[0] or x + w > out.shape[1]:
            raise SystemExit("move lands off the canvas at %d,%d" % (y, x))
        # Darken rather than overwrite. The block carries its own white
        # background, and painting that would punch a hole in whatever it now
        # overlaps; thresholding to ink instead would throw away the
        # antialiasing and leave the moved limb with harder edges than the
        # body it is attached to. Ink is dark on white, so a minimum keeps
        # both the grey edges and everything already underneath.
        np.minimum(out[y:y + h, x:x + w], block, out=out[y:y + h, x:x + w])
    return out


def contact_sheet(pairs, path, scale=4):
    """Both poses of each invader, side by side, for looking at."""
    rows = []
    for a, b in pairs:
        rows.append(np.concatenate([np.pad(a, ((0, 0), (0, 4), (0, 0)),
                                           constant_values=BACKGROUND), b], 1))
    h = max(r.shape[0] for r in rows)
    w = max(r.shape[1] for r in rows)
    sheet = np.concatenate([np.pad(r, ((0, h - r.shape[0] + 4),
                                       (0, w - r.shape[1]), (0, 0)),
                                   constant_values=BACKGROUND) for r in rows], 0)
    Image.fromarray(sheet).resize((sheet.shape[1] * scale,
                                   sheet.shape[0] * scale),
                                  Image.NEAREST).save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", metavar="PNG", nargs="?", const="poses.png",
                    help="write a contact sheet instead of the sprites")
    args = ap.parse_args()

    pairs = []
    for name, moves in sorted(MOVES.items()):
        src = os.path.join(ART, name + ".png")
        img = np.asarray(Image.open(src).convert("RGB"), np.uint8)
        two = pose_two(img, moves)
        if (two == img).all():
            raise SystemExit("%s: the moves changed nothing" % name)
        pairs.append((img, two))
        if not args.check:
            dst = os.path.join(ART, name + "b.png")
            Image.fromarray(two).save(dst, optimize=True)
            sys.stderr.write("wrote %s\n" % os.path.relpath(dst))

    if args.check:
        contact_sheet(pairs, args.check)
        sys.stderr.write("wrote %s\n" % args.check)


if __name__ == "__main__":
    main()
