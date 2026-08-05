#!/usr/bin/env python3
"""Nyan Cat.

The 2011 meme: a pop-tart cat flying through space, trailing a six band
rainbow, past twinkling stars.

The sprite is source, not an asset. The body -- tart, head and face -- is a
grid of characters with a palette mapping each to an RGB, and the parts that
move (the tail, at four angles, and a paw stamp lifted to three heights) are
separate little grids composed over it. build() bakes the six animation frames
out of those, scales them with np.repeat, and render() is then an index into
the frame table plus three blits.

The animation runs on its own clock. The original loops at about ten frames a
second, which is far slower than the display refresh, so --cat-fps drives the
sprite and --fps drives the wire; tying them together makes the cat look like
it is having a fit.

The rainbow is a strip baked once, wider than the panel, with the leading edge
stepped up and down in a square wave; scrolling it is a slice at an offset
rather than any per frame work. Stars twinkle through a grow-and-shrink cycle of four
shapes -- dot, plus, star, sparkle and back -- and drift at three speeds for
parallax.

Run:  python3 nyancat.py --host 127.0.0.1
      python3 nyancat.py --scale 2 --cat-fps 12 --speed 90
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The sprite, as data.
# --------------------------------------------------------------------------

# '.' is transparent. Everything else indexes PALETTE.
PALETTE = {
    "K": (58, 58, 58),        # cat outline, a dark grey rather than black
    "G": (176, 176, 176),     # fur
    "C": (255, 199, 128),     # pop-tart crust
    "P": (255, 153, 204),     # pop-tart icing
    "S": (255, 51, 153),      # sprinkles
    "E": (0, 0, 0),           # eyes
    "M": (0, 0, 0),           # mouth
    "R": (255, 122, 170),     # cheeks
}

# 42 x 21. The tart runs cols 10..32 rows 4..16; the head sits over its front
# at cols 31..41; rows 15..20 carry the paws, and cols 0..11 the tail. Head and
# tart deliberately overlap -- the head pokes out of the front of the tart, it
# is not parked next to it, and the ears break its top edge.
BODY = (
    "..........................................",
    "..........................................",
    ".................................K.....K..",
    "................................KGK...KGK.",
    "...........CCCCCCCCCCCCCCCCCCCCCKKKKKKKKK.",
    "..........CCCCCCCCCCCCCCCCCCCCCKGGGGGGGGGK",
    "..........CCPPPPPPPPPPPPPPPPPPPKGGGGGGGGGK",
    "..........CCPPPSPPPPPSPPPPPSPPPKGEEGGGEEGK",
    "..........CCPPPPPPPPPPPPPPPPPPPKGEEGGGEEGK",
    "..........CCPPPPPPSPPPPPSPPPPSPKGGGGGGGGGK",
    "..........CCPPPPPPPPPPPPPPPPPPPKRRGMGMGRRK",
    "..........CCPSPPPPPSPPPPPSPPPPPKRRGMMMGRRK",
    "..........CCPPPPPPPPPPPPPPPPPPPKGGGGGGGGGK",
    "..........CCPPPPSPPPPPSPPPPPSPPKGGGGGGGGGK",
    "..........CCPPPPPPPPPPPPPPPPPPPCKKKKKKKKK.",
    "..........CCCCCCCCCCCCCCCCCCCCCCC.........",
    "...........CCCCCCCCCCCCCCCCCCCCC..........",
    "..........................................",
    "..........................................",
    "..........................................",
    "..........................................",
)

# The tail, four angles, as a staircase three columns to the step, pasted over
# the body at (0, 0) -- so these are absolute rows and the blank lines above
# and below each pose are load bearing.
TAIL_UP = (
    "............",
    "............",
    "............",
    "KKK.........",
    "GGG.........",
    "GGGKKK......",
    "KKKGGG......",
    "...GGGKKK...",
    "...KKKGGG...",
    "......GGGKKK",
    "......KKKGGG",
    ".........GGG",
    ".........KKK",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
)
TAIL_MID_UP = (
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "KKK.........",
    "GGGKKK......",
    "GGGGGGKKK...",
    "KKKGGGGGGKKK",
    "...KKKGGGGGG",
    "......KKKGGG",
    ".........KKK",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
)
TAIL_MID_DOWN = (
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    ".........KKK",
    "......KKKGGG",
    "...KKKGGGGGG",
    "KKKGGGGGGKKK",
    "GGGGGGKKK...",
    "GGGKKK......",
    "KKK.........",
    "............",
    "............",
    "............",
    "............",
    "............",
)
TAIL_DOWN = (
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    "............",
    ".........KKK",
    ".........GGG",
    "......KKKGGG",
    "......GGGKKK",
    "...KKKGGG...",
    "...GGGKKK...",
    "KKKGGG......",
    "GGGKKK......",
    "GGG.........",
    "KKK.........",
    "............",
    "............",
)

TART_TOP, TART_BOTTOM = 4, 16          # rows of the tart, for the trail
TART_LEFT = 10                         # its left edge, where the trail meets it

# Six frame loop over the four angles: up, half up, half down, down and back.
TAILS = (TAIL_UP, TAIL_MID_UP, TAIL_MID_DOWN, TAIL_DOWN)
TAIL_CYCLE = (0, 1, 2, 3, 2, 1)

# One paw, stamped four times along the underside of the tart.
PAW = (
    "GGG",
    "GGG",
    "GGG",
    "KKK",
)
PAW_X = (13, 18, 23, 28)
PAW_Y = 15                             # topmost row a fully lifted paw reaches
# How far each paw is lifted, 2 = tucked up, 0 = extended. One walk cycle,
# each leg a frame and a half behind the one before it.
PAW_LIFT = (
    (0, 0, 1, 2),
    (1, 0, 0, 1),
    (2, 1, 0, 0),
    (2, 2, 1, 0),
    (1, 2, 2, 1),
    (0, 1, 2, 2),
)

# The whole cat rises and falls a little over the loop, in sprite pixels.
BOB = (0.0, 0.0, 0.5, 1.0, 0.5, 0.0)

# Top to bottom, the classic six.
RAINBOW = ((255, 0, 0), (255, 153, 0), (255, 255, 0),
           (51, 255, 0), (0, 153, 255), (102, 51, 255))

# The star grows and shrinks through these. Drawn at 1x by default so it stays
# smaller than the cat's head; --star-scale overrides.
STAR_SHAPES = (
    (
        "X",
    ),
    (
        ".X.",
        "XXX",
        ".X.",
    ),
    (
        "..X..",
        "..X..",
        "XX.XX",
        "..X..",
        "..X..",
    ),
    (
        "...X...",
        "...X...",
        "..X.X..",
        "XX...XX",
        "..X.X..",
        "...X...",
        "...X...",
    ),
)
STAR_CYCLE = (0, 1, 2, 3, 2, 1)


# --------------------------------------------------------------------------
# Turning the grids into arrays.
# --------------------------------------------------------------------------

def paste(grid, art, y=0, x=0):
    """Stamp a list-of-lists of characters over `grid`, ignoring '.'."""
    for r, row in enumerate(art):
        for c, ch in enumerate(row):
            if ch != ".":
                grid[y + r][x + c] = ch
    return grid


def rasterize(grid, scale):
    """(rows of chars) -> ((h,w,3) uint8, (h,w) bool), scaled up."""
    h, w = len(grid), len(grid[0])
    rgb = np.zeros((h, w, 3), np.uint8)
    mask = np.zeros((h, w), bool)
    for r in range(h):
        for c in range(w):
            ch = grid[r][c]
            if ch != ".":
                rgb[r, c] = PALETTE[ch]
                mask[r, c] = True
    if scale > 1:
        rgb = np.repeat(np.repeat(rgb, scale, 0), scale, 1)
        mask = np.repeat(np.repeat(mask, scale, 0), scale, 1)
    return rgb, mask


def cat_frames(scale):
    """The six animation frames, each (rgb, mask) at `scale`."""
    frames = []
    for k in range(6):
        grid = [list(row) for row in BODY]
        paste(grid, TAILS[TAIL_CYCLE[k]])
        for i, x in enumerate(PAW_X):
            paste(grid, PAW, PAW_Y + (2 - PAW_LIFT[k][i]), x)
        frames.append(rasterize(grid, scale))
    return frames


def rainbow_strip(width, band, amp, chunk):
    """The trail, baked wide enough to scroll by slicing.

    Six bands, and a leading edge that steps: the whole stack of bands is
    displaced vertically by `amp` over alternating `chunk` wide columns. That
    square wave is the thing that makes the trail look emitted rather than
    drawn -- a plain rectangle reads as a coloured bar.
    """
    h = 6 * band + amp
    strip = np.zeros((h, width, 3), np.uint8)
    mask = np.zeros((h, width), bool)
    cols = np.arange(width)
    up = ((cols // chunk) % 2) == 0
    for i, colour in enumerate(RAINBOW):
        y0 = i * band
        for off, sel in ((0, up), (amp, ~up)):
            strip[y0 + off:y0 + off + band, sel] = colour
            mask[y0 + off:y0 + off + band, sel] = True
    return strip, mask


def star_frames(scale):
    """The twinkle cycle, as boolean stamps, all centred on a common origin."""
    out = []
    for shape in STAR_SHAPES:
        a = np.array([[c == "X" for c in row] for row in shape], bool)
        if scale > 1:
            a = np.repeat(np.repeat(a, scale, 0), scale, 1)
        out.append(a)
    return out


# --------------------------------------------------------------------------

def blit(dst, src, mask, y, x):
    """Composite src over dst at (y, x), clipped to dst."""
    H, W = dst.shape[:2]
    h, w = mask.shape
    sy0, sx0 = max(0, -y), max(0, -x)
    sy1, sx1 = h - max(0, y + h - H), w - max(0, x + w - W)
    if sy0 >= sy1 or sx0 >= sx1:
        return
    d = dst[y + sy0:y + sy1, x + sx0:x + sx1]
    m = mask[sy0:sy1, sx0:sx1]
    np.copyto(d, src[sy0:sy1, sx0:sx1], where=m[..., None])


def color_arg(text):
    parts = [int(v) for v in text.replace(" ", "").split(",")]
    if len(parts) != 3 or not all(0 <= v <= 255 for v in parts):
        raise ValueError("expected three values 0..255, got %r" % text)
    return tuple(parts)


def add_arguments(ap):
    ap.add_argument("--scale", type=int, default=2,
                    help="sprite magnification (auto-reduced if it will not fit)")
    ap.add_argument("--cat-x", type=float, default=0.62,
                    help="where the cat sits across the panel, 0..1")
    ap.add_argument("--cat-fps", type=float, default=10.0,
                    help="sprite animation rate; the original is ~10, and it is "
                         "deliberately not --fps")
    ap.add_argument("--speed", type=float, default=70.0,
                    help="trail and star scroll, pixels/sec")
    ap.add_argument("--stars", type=int, default=0,
                    help="how many stars (0 = one per ~430 pixels of panel)")
    ap.add_argument("--star-scale", type=int, default=0,
                    help="star magnification (0 = half the sprite scale)")
    ap.add_argument("--no-stars", action="store_true")
    ap.add_argument("--no-trail", action="store_true")
    ap.add_argument("--space", type=color_arg, default=(0, 6, 34),
                    help="background colour")
    ap.add_argument("--seed", type=int, default=7)


def build(args):
    W, H = args.width, args.height
    sh, sw = len(BODY), len(BODY[0])

    # The sprite has to fit the panel, so a 128x32 wall quietly drops to 1x
    # rather than drawing a cat taller than the display.
    scale = max(1, args.scale)
    while scale > 1 and (sh * scale > H or sw * scale > W):
        scale -= 1
    frames = cat_frames(scale)
    fh, fw = frames[0][1].shape

    cat_x = int(round(np.clip(args.cat_x, 0.0, 1.0) * W - fw * 0.5))
    cat_x = max(0, min(cat_x, W - fw))
    # Sit the cat so the bob has headroom at both ends.
    bob_max = int(round(max(BOB) * scale))
    cat_y = max(0, (H - fh - bob_max) // 2)
    bob = [int(round(b * scale)) for b in BOB]

    # --- trail -----------------------------------------------------------
    # Its height matches the tart, not the whole sprite, and six bands have to
    # divide evenly or the top and bottom stripes come out different widths.
    band = max(1, ((TART_BOTTOM - TART_TOP) * scale) // 6)
    amp = max(1, int(round(1.5 * scale)))
    chunk = max(2, 4 * scale)
    trail_right = cat_x + (TART_LEFT + 2) * scale        # tucked under the tart
    trail_w = max(0, min(trail_right, W))
    period = 2 * chunk
    strip, strip_mask = rainbow_strip(trail_w + period, band, amp, chunk)
    trail_y = cat_y + TART_TOP * scale - amp // 2
    trail_y = max(0, min(trail_y, H - strip.shape[0]))

    # --- stars -----------------------------------------------------------
    star_scale = args.star_scale or max(1, scale // 2)
    stars = star_frames(star_scale)
    big = stars[-1].shape[0]
    rng = np.random.default_rng(args.seed)
    # Scaled to the panel: a fixed count that suits 320x64 buries a 128x32 one
    # in sparkles.
    n = args.stars if args.stars > 0 else max(4, (W * H) // 430)
    span = W + big
    star_x = rng.uniform(0, span, n)
    star_y = rng.integers(0, max(1, H - big), n)
    star_v = rng.choice(np.array([0.45, 0.75, 1.15], f32), n)
    star_phase = rng.integers(0, len(STAR_CYCLE), n)
    # Blitting is a Python loop over the stars, so precompute the per-star
    # offsets that centre each twinkle shape on a common point.
    centres = [(big - s.shape[0]) // 2 for s in stars]
    white = np.full((big, big, 3), 255, np.uint8)

    bg = np.empty((H, W, 3), np.uint8)
    bg[:] = np.array(args.space, np.uint8)
    buf = np.empty((H, W, 3), np.uint8)

    def render(t, frame):
        np.copyto(buf, bg)

        if not args.no_stars and n:
            step = int(t * args.cat_fps)
            xs = np.mod(star_x - args.speed * star_v * t, span).astype(np.int32) - big
            for i in range(n):
                j = STAR_CYCLE[(step + star_phase[i]) % len(STAR_CYCLE)]
                blit(buf, white, stars[j],
                     int(star_y[i]) + centres[j], int(xs[i]) + centres[j])

        if not args.no_trail and trail_w:
            off = int(t * args.speed) % period
            sl = (slice(off, off + trail_w),)
            src = strip[(slice(None),) + sl]
            m = strip_mask[(slice(None),) + sl]
            d = buf[trail_y:trail_y + strip.shape[0], 0:trail_w]
            np.copyto(d, src, where=m[..., None])

        k = int(t * args.cat_fps) % 6
        rgb, mask = frames[k]
        blit(buf, rgb, mask, cat_y + bob[k], cat_x)
        return buf

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
