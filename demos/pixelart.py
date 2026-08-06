#!/usr/bin/env python3
"""Sprite sheets from pixelart/, the way the C `grayscale` tool played them.

The Sequoia Fabrica wall has been showing this artwork for a couple of years:
a sequoia, space invaders, pacman and his ghosts, and an eight-frame sewing
machine. It was played by an external binary reading JSON files of hex
strings, which meant it could never blend into the effect either side of it,
could never be previewed, and had to be pointed at absolute paths on one Pi.
This is the same artwork as a demoscene module.

Two ways to lay a set of sprites out, which is the distinction the old tool
drew with its -D flag:

  strip      the sprites are joined side by side into one wide image and moved
             across the panel as a unit. Four invaders in a row, seven
             sequoias marching past.
  sequence   the sprites are frames of one animation, played in place. The
             sewing machine and pacman's chomp.

and a third that is both at once. --poses names alternative sheets of the same
slots, so a strip can hold four different sprites and still animate, every one
of them changing pose together. That is how an arcade cabinet did it and it is
what stops a row of invaders reading as a printed banner.

Then three ways to place the result: centred, bouncing, or scrolling. Bounce
is the one worth having on a 320x64 panel -- a strip wider than the screen
that turns around at each end reads as deliberate, where a strip that wraps
reads as a seam going past. A scroll needs --travel to match whichever way the
artwork faces.

Colour is either the artwork's own, or its brightness mapped through a palette.
The old tool defaulted to the latter and that is how the sequoia has always
looked on this wall: a rainbow-shaded tree, not a green one.

Run:  python3 pixelart.py --art sf-tree --mode center
      python3 pixelart.py --art sew1..8 --sequence-ms 120
      python3 pixelart.py --art pacman-32x32-1..6 --sequence-ms 50 \
              --mode scroll --travel right
      python3 pixelart.py --art space-invaders-1..4 --poses ,b \
              --sequence-ms 500 --mode bounce
      python3 pixelart.py --art "sf-tree*7" --mode bounce --render palette
"""

import os
import re
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pixelart")

# What the old tool called transparency: one colour in the artwork means "not
# drawn" rather than "draw this". These sprites were authored on a flat
# background, so it is a single exact colour, not a range.
TRANSPARENT = {"white": (255, 255, 255), "black": (0, 0, 0), "none": None}


def expand(spec):
    """`a,b` -> [a, b];  `sew1..8` -> [sew1..sew8];  `tree*7` -> [tree] * 7.

    The old rotation wrote these out in full, which for the bouncing sequoias
    meant naming the same file seven times and for the sewing machine meant
    eight paths of forty characters. Both are ranges, so let them be written
    as ranges.
    """
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        repeat = re.match(r"^(.*?)\*(\d+)$", item)
        if repeat:
            out.extend([repeat.group(1)] * int(repeat.group(2)))
            continue
        span = re.match(r"^(.*?)(\d+)\.\.(\d+)$", item)
        if span:
            stem, lo, hi = span.group(1), int(span.group(2)), int(span.group(3))
            step = 1 if hi >= lo else -1
            out.extend("%s%d" % (stem, n) for n in range(lo, hi + step, step))
            continue
        out.append(item)
    return out


def load(name):
    """Load one sprite as (H, W, 3) uint8.

    PNG is what ships in pixelart/ -- the same pixels the JSON held, at a
    fifteenth of the size. JSON is still read so a checkout can be pointed
    straight at an existing pixelart directory.
    """
    path = name if os.path.sep in name else os.path.join(ART_DIR, name)
    if not os.path.splitext(path)[1]:
        for ext in (".png", ".json"):
            if os.path.exists(path + ext):
                path = path + ext
                break
    if path.endswith(".json"):
        import json
        rows = json.load(open(path))
        w = max(len(r) for r in rows)
        out = np.zeros((len(rows), w, 3), np.uint8)
        for y, row in enumerate(rows):
            for x, hexv in enumerate(row):
                v = int(str(hexv).lstrip("#"), 16)
                out[y, x] = (v >> 16 & 255, v >> 8 & 255, v & 255)
        return out
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB"), np.uint8)


LUMA = np.array([0.299, 0.587, 0.114], f32)


def luminance(rgb):
    return rgb.astype(f32) @ LUMA


def has_colour(sprite):
    """Is there real chroma here, or is this greyscale line art?

    It matters because half this set is each. sf-tree, the invaders and the
    sewing machine are drawn in greys -- which is what the old tool's name is
    about -- and have to be given a colour or they are a white smudge. Pacman
    and the ghosts are full-colour images that only want drawing as they are.
    """
    span = sprite.max(axis=2).astype(np.int16) - sprite.min(axis=2)
    return float(span.mean()) > 8.0


def intensity(sprite, mask, key):
    """How strongly each pixel is 'drawn', 0..1.

    Distance from the transparent colour, not raw brightness: this artwork is
    dark ink on white, so brightness alone would make the ink the *dimmest*
    part and hand the panel to the background it is meant to drop.
    """
    lum = luminance(sprite)
    base = luminance(np.array(key, np.uint8).reshape(1, 1, 3))[0, 0] \
        if key is not None else 0.0
    d = np.abs(lum - base)
    peak = float(d[mask].max()) if mask.any() else 0.0
    return d / peak if peak > 1e-3 else np.zeros_like(d)


def add_arguments(ap):
    ap.add_argument("--art", default="sf-tree",
                    help="sprites: names, a,b,c, a1..8 for a range, a*7 to repeat")
    ap.add_argument("--mode", default="center",
                    choices=("center", "bounce", "scroll", "left", "right"),
                    help="how the strip is placed and moved")
    ap.add_argument("--sequence-ms", type=float, default=0.0,
                    help="ms per frame to play the sprites as an animation "
                         "(0 joins them side by side into one strip instead)")
    ap.add_argument("--poses", default="",
                    help="suffixes making each sprite a pose of the same slot, "
                         "e.g. ',b' plays name then nameb. The strip keeps its "
                         "layout and every sprite in it changes pose together")
    ap.add_argument("--reverse", action="store_true",
                    help="play the sequence backwards")
    ap.add_argument("--render", default="auto",
                    choices=("auto", "palette", "original"),
                    help="auto keeps the artwork's colours if it has any, and "
                         "paints it from the palette if it is greyscale line art")
    ds.palette_argument(ap, "rainbow")
    ap.add_argument("--hue-span", type=float, default=1.0,
                    help="palette sweeps across the strip this many times")
    ap.add_argument("--hue-drift", type=float, default=0.05,
                    help="palette travels along the strip, cycles/sec")
    ap.add_argument("--transparent", default="white",
                    choices=sorted(TRANSPARENT), help="colour meaning 'not drawn'")
    ap.add_argument("--speed", type=float, default=26.0,
                    help="px/sec for bounce and scroll")
    ap.add_argument("--travel", default="right", choices=("left", "right"),
                    help="which way a scroll goes; a sprite that faces one way "
                         "has to travel that way or it moonwalks")
    ap.add_argument("--gap", type=int, default=0,
                    help="blank columns between sprites in a strip")
    ap.add_argument("--background", default="000000",
                    help="hex colour behind the artwork")


def build(args):
    W, H = args.width, args.height

    slots = expand(args.art)
    if not slots:
        raise SystemExit("pixelart: --art named nothing")

    # A pose is a whole alternative sheet: the same slots in the same order,
    # every name carrying a suffix. The invaders need this because they are a
    # strip *and* an animation at once -- four different sprites side by side,
    # all changing pose together -- which neither "strip" nor "sequence" on
    # its own can express.
    poses = args.poses.split(",") if args.poses else [""]
    if len(poses) > 1 and args.sequence_ms <= 0:
        raise SystemExit("pixelart: --poses needs --sequence-ms to play them")
    sheets = [[load(n + suffix.strip()) for n in slots] for suffix in poses]

    key = TRANSPARENT[args.transparent]
    painted = (args.render == "palette" or
               (args.render == "auto" and
                not any(has_colour(s) for sheet in sheets for s in sheet)))
    palette = ds.named_palette(args.palette, 256).astype(f32) if painted else None

    def prepare(sprite):
        """-> (rgb-or-intensity, mask) at the sprite's own size."""
        mask = (np.ones(sprite.shape[:2], bool) if key is None
                else ~np.all(sprite == np.array(key, np.uint8), axis=2))
        if painted:
            return intensity(sprite, mask, key), mask
        return sprite.astype(np.uint8), mask

    sheets = [[prepare(s) for s in sheet] for sheet in sheets]

    # Scale anything taller than the panel down to fit, by whole pixels where
    # possible: these are pixel art and a fractional resample turns crisp
    # edges into mush. pac-man-ghosts is 248x64 and already fits; nothing in
    # the set needs more than a halving.
    def fit(art, mask):
        if art.shape[0] <= H:
            return art, mask
        step = -(-art.shape[0] // H)                 # ceil, so 1 means "fits"
        return art[::step, ::step], mask[::step, ::step]

    sheets = [[fit(art, mask) for art, mask in sheet] for sheet in sheets]

    # Slot geometry is measured across every pose, not within one, so a pose
    # whose artwork happens to be a pixel narrower cannot shuffle the sprites
    # to its right. The layout is fixed; only the pixels in it change.
    gap = max(0, args.gap)
    height = max(r.shape[0] for sheet in sheets for r, _ in sheet)
    widths = [max(sheet[i][0].shape[1] for sheet in sheets)
              for i in range(len(slots))]
    width = sum(widths) + gap * (len(slots) - 1)

    def strip(sheet):
        """The slots joined side by side into one wide image.

        Each sprite sits on the bottom of the strip rather than the top --
        these are objects standing on the ground, and top-aligning a short one
        leaves it hovering.
        """
        shape = (height, width) if painted else (height, width, 3)
        art = np.zeros(shape, sheet[0][0].dtype)
        mask = np.zeros((height, width), bool)
        x = 0
        for (r, m), slot_w in zip(sheet, widths):
            h, w = r.shape[:2]
            y = height - h
            art[y:y + h, x:x + w] = r
            mask[y:y + h, x:x + w] = m
            x += slot_w + gap
        return art, mask

    sequence = args.sequence_ms > 0
    if len(poses) > 1:
        frames = [strip(sheet) for sheet in sheets]        # a strip per pose
    elif sequence:
        frames = sheets[0]                                 # sprites are frames
    else:
        frames = [strip(sheets[0])]                        # one still strip
    if args.reverse:
        frames = frames[::-1]

    bg = int(args.background.lstrip("#"), 16)
    background = np.array((bg >> 16 & 255, bg >> 8 & 255, bg & 255), np.uint8)
    out = np.empty((H, W, 3), np.uint8)

    def place(width, t):
        """Left edge of the strip, in panel columns, at time t."""
        slack = W - width
        if args.mode == "center":
            return slack // 2
        if args.mode == "left":
            return 0
        if args.mode == "right":
            return slack
        if args.mode == "scroll":
            # Always travels; a strip narrower than the panel still crosses it.
            # The lap is width + W either way: the strip has to clear the far
            # edge completely before it comes back on at the near one.
            step = int(t * args.speed) % (width + W)
            return (step - width) if args.travel == "right" else (W - step)
        # bounce: a triangle wave over the travel, which is the slack if the
        # strip fits and the overhang if it does not. abs() of a sawtooth
        # rather than a sine, so the speed is constant and only the turn is
        # sudden -- a sine spends most of its time near the ends.
        travel = abs(slack)
        if travel == 0:
            return slack // 2
        phase = (t * args.speed) % (2 * travel)
        offset = travel - abs(phase - travel)
        return offset if slack >= 0 else -offset

    # In painted mode the colour comes from where a pixel is, not from how
    # bright it is: the artwork is greyscale, so mapping its brightness to hue
    # would turn every antialiased edge into a different colour and the shape
    # into confetti. A rainbow laid across the strip keeps the shape and gives
    # it colour, which is what "rainbow palette" has to have meant.
    widest = max(f[0].shape[1] for f in frames)
    ramp = None
    if painted:
        span = np.linspace(0.0, args.hue_span, widest, dtype=f32)
        ramp = np.clip(span * 255.0, 0, 255)

    def render(t, frame):
        art, mask = frames[int(t * 1000.0 / args.sequence_ms) % len(frames)] \
            if sequence else frames[0]
        h, w = art.shape[:2]
        out[:, :] = background

        x0 = int(place(w, t))                # bounce works in float time
        y0 = (H - h) // 2
        # Clip to the panel: bounce and scroll both run the strip off the edge
        # on purpose, and scroll starts it entirely off.
        sx, dx = max(0, -x0), max(0, x0)
        sy, dy = max(0, -y0), max(0, y0)
        cw, ch = min(w - sx, W - dx), min(h - sy, H - dy)
        if cw <= 0 or ch <= 0:
            return out

        sub = mask[sy:sy + ch, sx:sx + cw]
        piece = art[sy:sy + ch, sx:sx + cw]
        if painted:
            idx = (ramp[sx:sx + cw] + t * args.hue_drift * 255.0) % 256.0
            colour = palette[idx.astype(np.int32)]              # (cw, 3)
            lit = colour[None, :, :] * piece[:, :, None]
            out[dy:dy + ch, dx:dx + cw][sub] = \
                np.clip(lit, 0, 255).astype(np.uint8)[sub]
        else:
            out[dy:dy + ch, dx:dx + cw][sub] = piece[sub]
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
