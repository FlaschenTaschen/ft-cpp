#!/usr/bin/env python3
"""Mario.

A self-playing 8-bit platformer: a little plumber runs right forever through a
level that generates itself, jumping gaps and pipes, over a parallax sky. No
input -- the character reads the level ahead of it and decides when to jump.

Everything is source, not an asset. The plumber, the tiles, the pipe, the coin
and the goomba are grids of characters with a palette mapping each to an RGB,
composed at startup and scaled with np.repeat, exactly as nyancat.py does. The
run cycle is three leg grids pasted over one body grid, plus a distinct jump
pose, and it runs on --run-fps rather than the display rate.

Tile size: 8 pixels, with a two-tile (16 px) character. A 320x64 panel is only
four rows of *classic* 16 px tiles -- ground plus one jumpable pipe leaves
under a tile of headroom and the jump arc has nowhere to go. At 8 px the panel
is eight rows: one of ground, two of character and five of air, which is what
makes a jump read as a jump and what lets a pipe be three tiles tall and still
be cleared. --scale 2 gets the authentic 16 px tiles, and demonstrates the
problem: on 64 rows the arc no longer clears a 16 px pipe, so build() drops
pipes from the generator and you are left with gaps.

The level cannot become uncompletable. build() derives the jump's airtime T
and its horizontal reach D = speed * T from the physics, then bounds the
generator by them: gaps are never wider than 0.6*D minus the character's own
width, pipes only as tall as the arc actually is over their span, and flat
ground between obstacles is always at least 1.3*D, so the character has landed
and is grounded again well before it must commit to the next jump. Every jump
is triggered by the same formula the generator plans with, so what it draws is
what the character can do.

Run:  python3 mario.py --host 127.0.0.1
      python3 mario.py --speed 120 --density 0.8 --seed 5
      python3 mario.py --scale 2 --no-parallax
"""

import math
import sys
from types import SimpleNamespace

import numpy as np

import demoscene as ds


# --------------------------------------------------------------------------
# The art, as data. '.' is transparent, everything else indexes PALETTE.
# --------------------------------------------------------------------------

PALETTE = {
    "R": (216, 40, 32),       # cap and shirt
    "B": (32, 72, 204),       # overalls
    "S": (252, 188, 144),     # skin
    "K": (72, 40, 12),        # hair, moustache, outline
    "Y": (140, 76, 20),       # boots
    "W": (255, 255, 255),
    "E": (16, 16, 16),

    "g": (196, 116, 52),      # ground, top course
    "G": (140, 72, 24),       # ground, fill
    "h": (92, 44, 12),        # ground speckle

    "n": (88, 44, 12),        # brick mortar
    "b": (200, 108, 44),      # brick face
    "o": (228, 148, 40),      # question block
    "y": (120, 68, 0),        # question block edge

    "d": (0, 84, 16),         # pipe, dark
    "D": (0, 120, 24),        # pipe, shadow side
    "m": (0, 168, 44),        # pipe, body
    "L": (152, 232, 152),     # pipe, highlight

    "C": (252, 224, 80),      # coin
    "c": (196, 140, 20),

    "u": (168, 96, 32),       # goomba
    "U": (84, 44, 12),
}

# The plumber: 12 wide, 16 tall. Rows 0..11 are the body -- cap, face with a
# moustache, shirt and overalls -- and rows 12..15 are supplied per frame by
# one of the leg grids below, so the run cycle is one grid deep rather than
# four copies of the same torso.
BODY = (
    "...RRRRRR...",
    "..RRRRRRRRR.",
    "..KKKSSSSSS.",
    ".KKKSSSKSSS.",
    ".KKKSSSKSSS.",
    ".KKSSSSSSSS.",
    "..SSKKKKKS..",
    "...SSSSSS...",
    "..RRRRRRRR..",
    ".SRRBRRBRRS.",
    ".SRBBBBBBRS.",
    ".SBBBBBBBBS.",
    "............",
    "............",
    "............",
    "............",
)

# Three legs, pasted at row 12: contact, passing, stride. Three frames is what
# an 8-bit run cycle actually is, and at 12 pixels wide a fourth reads as
# noise.
LEGS = (
    (
        "..BBBBBBBB..",
        "..BB....BB..",
        ".YYY....YYY.",
        ".YYY....YYY.",
    ),
    (
        "..BBBBBBBB..",
        "...BBB.BB...",
        "..YYYY.YY...",
        "..YYYY.YY...",
    ),
    (
        "..BBBBBBBB..",
        ".BB.....BB..",
        "YYY......YY.",
        "YYY.....YYY.",
    ),
)

# The jump pose: legs tucked and split, and both arms thrown up. The arms are
# a separate two-row stamp over the shoulders because the torso underneath is
# unchanged.
LEGS_JUMP = (
    "..BBBBBBBB..",
    ".BB.....BBB.",
    "YYY.......YY",
    "YYY......YYY",
)
ARMS_JUMP = (
    "SS........SS",
    "SS........SS",
)
ARMS_JUMP_Y = 7

LEGS_ROW = 12

# --- tiles, 8x8 -----------------------------------------------------------

GROUND_TOP = (
    "gggggggg",
    "gGGGGGGh",
    "gGhGGGGh",
    "gGGGGhGh",
    "gGGhGGGh",
    "gGGGGGGh",
    "ghGGGhGh",
    "hhhhhhhh",
)
BRICK = (
    "nnnnnnnn",
    "bbbnbbbn",
    "bbbnbbbn",
    "nnnnnnnn",
    "bnbbbnbb",
    "bnbbbnbb",
    "nnnnnnnn",
    "bbbnbbbn",
)
QUESTION = (
    "yyyyyyyy",
    "yooooooy",
    "yoWWWooy",
    "yoooWWoy",
    "yooWWooy",
    "yooooooy",
    "yooWWooy",
    "yyyyyyyy",
)

# The pipe is 16 wide: an eight row cap with a rim that overhangs, then shaft
# rows repeated below it. A pipe of h tiles is h*8 pixels tall, cap included.
PIPE_CAP = (
    "dddddddddddddddd",
    "dLLmmmmmmmmmmmDd",
    "dLLmmmmmmmmmmmDd",
    "dLLmmmmmmmmmmmDd",
    "dLLmmmmmmmmmmmDd",
    "dLLmmmmmmmmmmmDd",
    "dddddddddddddddd",
    "..dLLmmmmmmmDd..",
)
PIPE_SHAFT = "..dLLmmmmmmmDd.."

# The coin spins by narrowing: wide, half, edge on, half.
COINS = (
    (
        "..cccc..",
        ".cCCCCc.",
        ".cCccCc.",
        ".cCccCc.",
        ".cCccCc.",
        ".cCccCc.",
        ".cCCCCc.",
        "..cccc..",
    ),
    (
        "...cc...",
        "..cCCc..",
        "..cCCc..",
        "..cCCc..",
        "..cCCc..",
        "..cCCc..",
        "..cCCc..",
        "...cc...",
    ),
    (
        "...c....",
        "...C....",
        "...C....",
        "...C....",
        "...C....",
        "...C....",
        "...C....",
        "...c....",
    ),
)
COIN_CYCLE = (0, 1, 2, 1)

GOOMBA = (
    "..UUUU..",
    ".UuuuuU.",
    "UuWuuWuU",
    "UuEuuEuU",
    "UuuuuuuU",
    ".UuuuuU.",
    "........",
    "........",
)
GOOMBA_FEET = (
    (
        ".UU..UU.",
        "UU....UU",
    ),
    (
        "UU....UU",
        ".UU..UU.",
    ),
)
GOOMBA_FEET_Y = 6
GOOMBA_FLAT = (
    "........",
    "........",
    "........",
    "........",
    "........",
    "..UUUU..",
    ".UuuuuU.",
    "UUUUUUUU",
)

# 3x5 digits for the coin counter.
DIGITS = {
    "0": ("XXX", "X.X", "X.X", "X.X", "XXX"),
    "1": (".X.", "XX.", ".X.", ".X.", "XXX"),
    "2": ("XXX", "..X", "XXX", "X..", "XXX"),
    "3": ("XXX", "..X", "XXX", "..X", "XXX"),
    "4": ("X.X", "X.X", "XXX", "..X", "..X"),
    "5": ("XXX", "X..", "XXX", "..X", "XXX"),
    "6": ("XXX", "X..", "XXX", "X.X", "XXX"),
    "7": ("XXX", "..X", "..X", "..X", "..X"),
    "8": ("XXX", "X.X", "XXX", "X.X", "XXX"),
    "9": ("XXX", "X.X", "XXX", "..X", "XXX"),
}


# --------------------------------------------------------------------------
# Turning grids into arrays.
# --------------------------------------------------------------------------

def paste(grid, art, y=0, x=0):
    """Stamp rows of characters over `grid`, ignoring '.'."""
    for r, row in enumerate(art):
        for c, ch in enumerate(row):
            if ch != ".":
                grid[y + r][x + c] = ch
    return grid


def rasterize(grid, scale=1, palette=PALETTE):
    """(rows of chars) -> ((h,w,3) uint8, (h,w) bool), scaled up."""
    h, w = len(grid), len(grid[0])
    for row in grid:
        if len(row) != w:
            raise ValueError("ragged sprite: %r is %d wide, expected %d"
                             % ("".join(row), len(row), w))
    rgb = np.zeros((h, w, 3), np.uint8)
    mask = np.zeros((h, w), bool)
    for r in range(h):
        for c in range(w):
            ch = grid[r][c]
            if ch != ".":
                rgb[r, c] = palette[ch]
                mask[r, c] = True
    if scale > 1:
        rgb = np.repeat(np.repeat(rgb, scale, 0), scale, 1)
        mask = np.repeat(np.repeat(mask, scale, 0), scale, 1)
    return rgb, mask


def hero_frames(scale):
    """Three run frames plus the jump pose, each (rgb, mask)."""
    out = []
    for legs in LEGS:
        grid = [list(row) for row in BODY]
        paste(grid, legs, LEGS_ROW)
        out.append(rasterize(grid, scale))
    grid = [list(row) for row in BODY]
    paste(grid, LEGS_JUMP, LEGS_ROW)
    paste(grid, ARMS_JUMP, ARMS_JUMP_Y)
    out.append(rasterize(grid, scale))
    return out


def goomba_frames(scale):
    out = []
    for feet in GOOMBA_FEET:
        grid = [list(row) for row in GOOMBA]
        paste(grid, feet, GOOMBA_FEET_Y)
        out.append(rasterize(grid, scale))
    out.append(rasterize([list(r) for r in GOOMBA_FLAT], scale))
    return out


def pipe_sprite(tiles, scale):
    """A pipe `tiles` tall: the cap, then shaft rows under it."""
    grid = [list(r) for r in PIPE_CAP]
    for _ in range((tiles - 1) * 8):
        grid.append(list(PIPE_SHAFT))
    return rasterize(grid, scale)


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


def scroll_blit(dst, src, mask, off, y):
    """Draw a strip wider than the panel at a wrapping horizontal offset."""
    H, W = dst.shape[:2]
    h, ws = mask.shape
    off = int(off) % ws
    x = 0
    while x < W:
        n = min(W - x, ws - off)
        d = dst[y:y + h, x:x + n]
        np.copyto(d, src[:h, off:off + n][:d.shape[0]],
                  where=mask[:h, off:off + n][:d.shape[0], ..., None])
        x += n
        off = (off + n) % ws


# --------------------------------------------------------------------------
# Parallax scenery. Hills and clouds are shapes, not sprites, so they are
# drawn into wide strips once and scrolled by slicing.
# --------------------------------------------------------------------------

def cloud_strip(width, height, rng, scale):
    rgb = np.zeros((height, width, 3), np.uint8)
    mask = np.zeros((height, width), bool)
    yy, xx = np.mgrid[0:height, 0:width]
    n = max(2, width // (70 * scale))
    for _ in range(n):
        cx = rng.integers(0, width)
        cy = rng.integers(2 * scale, max(3 * scale, height - 4 * scale))
        r = rng.integers(3 * scale, 6 * scale)
        blob = np.zeros((height, width), bool)
        for dx, dy, k in ((-r, 0, 0.7), (0, 0, 1.0), (r, 0, 0.75),
                          (-r // 2, -r // 2, 0.8), (r // 2, -r // 2, 0.7)):
            rr = max(1.0, r * k)
            blob |= (((xx - cx - dx) ** 2 + ((yy - cy - dy) * 1.4) ** 2)
                     <= rr * rr)
        mask |= blob
        rgb[blob] = (252, 252, 252)
        # A shaded underside keeps a white blob from reading as a hole.
        low = blob & np.roll(~blob, -max(1, scale), axis=0)
        rgb[low] = (198, 214, 244)
    return rgb, mask


def hill_strip(width, height, rng, scale, colors, count_div):
    """Rounded mounds along the bottom of a strip."""
    rgb = np.zeros((height, width, 3), np.uint8)
    mask = np.zeros((height, width), bool)
    yy, xx = np.mgrid[0:height, 0:width]
    body, rim = colors
    n = max(2, width // (count_div * scale))
    for _ in range(n):
        cx = rng.integers(0, width)
        r = rng.integers(max(3, height // 2), max(4, height + 1))
        shape = ((((xx - cx) / float(r)) ** 2
                  + ((yy - height) / float(r)) ** 2) <= 1.0) & (yy <= height)
        mask |= shape
        rgb[shape] = body
        top = shape & ~np.roll(shape, 1, axis=0)
        rgb[top] = rim
    return rgb, mask


# --------------------------------------------------------------------------
# The level.
# --------------------------------------------------------------------------

class Level:
    """Endless procedural level, generated column by column ahead of the
    camera and trimmed behind it.

    The generator is bounded by the jump the character actually has. Nothing
    it emits can strand him:

      * a gap is at most `gap_max` tiles, where gap_max*TS + character width
        is under 0.6 of the jump's horizontal reach D -- so the takeoff point
        the character picks always leaves it landing well past the far lip;
      * a pipe is only as tall as the arc is high over a span of its width
        plus the character, tested against the real trajectory in build(), so
        two or three tiles at the default physics and none at all if the
        panel is too short for the arc;
      * consecutive obstacles are separated by at least 1.3*D of flat ground,
        which is more than the 0.5*D he needs to land plus the 0.5*D of
        run-up before the next one. Two obstacles therefore never combine
        into one that is wider than either.

    Decorations are placed with the same margins: floating blocks stay 1.2*D
    clear of both ends of a flat run and goombas patrol no closer than 1.5*D,
    so neither ever lands inside a jump arc.
    """

    def __init__(self, geo, args, rng):
        self.geo = geo
        self.args = args
        self.rng = rng
        self.base = 0                  # world column index of solid[0]
        self.next_col = 0              # first column not yet generated
        self.solid = []
        self.pipe = []
        self.blocks = []               # [x_px, y_px, kind]
        self.coins = []                # [x_px, y_px, alive]
        self.goombas = []              # [x_px, dir, lo, hi, squash_timer]

    # -- generation --------------------------------------------------------

    def ensure(self, upto_col):
        while self.next_col < upto_col:
            self._segment()

    def _segment(self):
        geo = self.geo
        # Flat run: the minimum is the safety bound above; --density stretches
        # the random part, so a low density means long empty stretches. Two in
        # five runs are stretched much longer again -- partly for pacing, and
        # partly because the decoration margins below need a run several jumps
        # long before there is anywhere safe to put a block or a goomba.
        span = geo.run_min * (1.0 / max(self.args.density, 0.05) - 1.0)
        run_px = geo.run_min + self.rng.random() * max(0.0, span)
        if self.rng.random() < 0.4:
            run_px *= 2.5 + 1.5 * self.rng.random()
        n = max(2, int(round(run_px / geo.TS)))
        x0 = self.next_col * geo.TS
        self.solid.extend([True] * n)
        self.pipe.extend([0] * n)
        self.next_col += n
        x1 = self.next_col * geo.TS
        self._decorate(x0, x1)

        choices = []
        if geo.gap_max >= 1:
            choices.append("gap")
        if geo.pipe_heights:
            choices.append("pipe")
        if not choices:
            return
        kind = choices[int(self.rng.integers(0, len(choices)))]
        if kind == "gap":
            w = 1 + int(self.rng.integers(0, geo.gap_max))
            a = self.next_col * geo.TS
            self.solid.extend([False] * w)
            self.pipe.extend([0] * w)
            self.next_col += w
            b = self.next_col * geo.TS
        else:
            h = geo.pipe_heights[int(self.rng.integers(0, len(geo.pipe_heights)))]
            a = self.next_col * geo.TS
            self.solid.extend([True, True])
            self.pipe.extend([h, h])
            self.next_col += 2
            b = self.next_col * geo.TS
        self._arc_coins(a - geo.cw * 0.5, b + geo.cw * 0.5)

    def _decorate(self, x0, x1):
        geo = self.geo
        rng = self.rng
        # Blocks: a short cluster in the middle of the run, out of arc reach.
        lo, hi = x0 + 1.2 * geo.D, x1 - 1.2 * geo.D
        room = int((hi - lo) // geo.TS)
        if geo.block_y is not None and room >= 2 and rng.random() < 0.7:
            k = min(2 + int(rng.integers(0, 4)), room)
            start = int(rng.integers(int(lo), int(hi) - k * geo.TS + 1))
            start = (start // geo.TS) * geo.TS
            for i in range(k):
                kind = 1 if (i == k // 2 and k >= 3) else 0
                self.blocks.append([start + i * geo.TS, geo.block_y, kind])
                if kind == 1:
                    # A coin hovering over the question block, at head height
                    # so he collects it just by running under it.
                    self.coins.append([start + i * geo.TS,
                                       geo.ground_top - geo.ch, True])

        # Goombas: patrol strictly inside the run.
        lo, hi = x0 + 1.5 * geo.D, x1 - 1.5 * geo.D
        if hi - lo > 3 * geo.TS and rng.random() < 0.55:
            x = float(rng.uniform(lo, hi))
            self.goombas.append([x, -1.0, lo, hi, 0.0])

    def _arc_coins(self, a, b):
        """Coins strung along the trajectory the character will actually fly.

        The generator knows the takeoff point because it is the same formula
        the character uses, so the coins trace the jump -- which is also the
        clearest visual check that the arc is where it should be.
        """
        geo = self.geo
        d = 0.5 * (geo.D - (b - a))
        if d <= 0:
            return
        x_take = a - d
        for k in (0.25, 0.4, 0.5, 0.6, 0.75):
            u = k * geo.D
            y = geo.ground_top - geo.height_at(u) - geo.ch - 2 * geo.scale
            self.coins.append([x_take + u, y, True])

    # -- queries -----------------------------------------------------------

    def surface(self, col):
        """Top of the solid at a world column, or None over a hole."""
        i = col - self.base
        if i < 0:
            return self.geo.ground_top
        if i >= len(self.solid):
            self.ensure(col + 8)
            i = col - self.base
        if not self.solid[i]:
            return None
        return self.geo.ground_top - self.pipe[i] * self.geo.TS

    def obstacle_ahead(self, xc, feet_y):
        """The next span the character must be airborne over, as (a, b).

        Both edges are already widened by half the character, so the span is
        in the coordinates of his centre: he must leave the ground before `a`
        and land after `b`.
        """
        geo = self.geo
        col = int(math.floor(xc / geo.TS))
        for j in range(col, col + geo.look_cols):
            i = j - self.base
            if i < 0:
                continue
            if i >= len(self.solid):
                self.ensure(j + 8)
                i = j - self.base
            if not self.solid[i]:
                k = j
                while self.surface(k) is None:
                    k += 1
                return (j * geo.TS - geo.cw * 0.5, k * geo.TS + geo.cw * 0.5)
            if self.pipe[i] > 0:
                top = geo.ground_top - self.pipe[i] * geo.TS
                if top >= feet_y - 1:
                    continue            # already level with it or above
                k = j
                while True:
                    up = self.surface(k)
                    if up is None or up >= geo.ground_top:
                        break
                    k += 1
                return (j * geo.TS - geo.cw * 0.5, k * geo.TS + geo.cw * 0.5)
        return None

    def trim(self, cam_x):
        geo = self.geo
        drop = int(cam_x // geo.TS) - 4 - self.base
        if drop > 0:
            del self.solid[:drop]
            del self.pipe[:drop]
            self.base += drop
        cut = cam_x - 4 * geo.TS
        self.blocks = [b for b in self.blocks if b[0] > cut]
        self.coins = [c for c in self.coins if c[0] > cut and c[2]]
        self.goombas = [g for g in self.goombas if g[0] > cut]


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--scale", type=int, default=1,
                    help="pixel scale: 1 = 8px tiles and a 12x16 character, "
                         "2 = classic 16px tiles (auto-reduced if it will not fit)")
    ap.add_argument("--speed", type=float, default=90.0,
                    help="how fast the world scrolls, pixels/sec")
    ap.add_argument("--run-fps", type=float, default=11.0,
                    help="run cycle rate; deliberately not --fps")
    ap.add_argument("--gravity", type=float, default=300.0,
                    help="downward acceleration, pixels/sec^2")
    ap.add_argument("--jump", type=float, default=138.0,
                    help="takeoff velocity, pixels/sec (clamped so his head "
                         "cannot leave the panel)")
    ap.add_argument("--density", type=float, default=0.5,
                    help="obstacle frequency, 0..1; 1 packs them as close as "
                         "the jump allows")
    ap.add_argument("--hero-x", type=float, default=0.28,
                    help="where he sits across the panel, 0..1")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--no-parallax", action="store_true",
                    help="drop the clouds and hills")
    ap.add_argument("--no-hud", action="store_true", help="no coin counter")


def build(args):
    W, H = args.width, args.height

    # --- geometry ---------------------------------------------------------
    # A character is 16 units tall and a tile 8, so he needs 2 tiles of his
    # own plus ground plus air. 28 units is that minimum with half a tile of
    # ground and half a tile of headroom; below it, scale down.
    scale = max(1, args.scale)
    while scale > 1 and H < 28 * scale:
        scale -= 1
    TS = 8 * scale
    cw, ch = 12 * scale, 16 * scale

    # One tile of ground: 8 of 64 rows is the same eighth of the screen the
    # original spends on it, and every row not spent on dirt is a row the
    # jump arc gets. Thinner still if the panel is short -- headroom for the
    # arc matters more than a solid looking floor.
    ground_px = min(TS, max(TS // 2, H - ch - 3 * TS))
    ground_px = max(2, min(ground_px, H - ch - 2))
    ground_top = H - ground_px

    sp = args.speed * scale
    grav = max(20.0, args.gravity * scale)
    # Clamp the takeoff so the top of his cap never reaches row 0.
    max_rise = max(2.0, ground_top - ch - 1.0)
    v0 = min(args.jump * scale, math.sqrt(2.0 * grav * max_rise))
    T = 2.0 * v0 / grav
    D = sp * T                                   # horizontal reach of a jump

    def height_at(u):
        """Height above the ground u pixels after takeoff."""
        tt = u / sp
        return v0 * tt - 0.5 * grav * tt * tt

    # Gaps: 0.6*D of the reach at most, character width included, so the
    # takeoff distance the chase logic picks is always comfortably positive.
    gap_max = int((0.6 * D - cw) // TS)
    gap_max = max(0, min(gap_max, 4))

    # Pipes: two tiles wide, and only the heights whose top clears the real
    # trajectory at *both* edges of that span -- the apex is over the middle,
    # so the edges are the tight part. Two tiles is the shortest pipe that
    # still reads as a pipe rather than a green box, so anything under that
    # means no pipes at all on this panel.
    pipe_heights = []
    for h in (2, 3):
        width = 2 * TS + cw
        d = 0.5 * (D - width)
        if d > 0 and height_at(d) >= h * TS + 3 * scale:
            pipe_heights.append(h)
    # Flat ground between obstacles: half a reach to land, half to run up.
    run_min = 1.3 * D + TS

    # Floating blocks sit four tiles up, which is the classic height; None if
    # the panel simply has no room above his head.
    block_y = ground_top - 4 * TS
    if block_y < 0 or block_y + TS > ground_top - ch:
        block_y = None

    geo = SimpleNamespace(
        TS=TS, cw=cw, ch=ch, scale=scale, ground_top=ground_top,
        D=D, height_at=height_at, gap_max=gap_max, pipe_heights=pipe_heights,
        run_min=run_min, block_y=block_y,
        look_cols=int(D // TS) + 4)

    # --- sprites ----------------------------------------------------------
    hero = hero_frames(scale)
    goombas = goomba_frames(scale)
    coins = [rasterize([list(r) for r in c], scale) for c in COINS]
    tiles = {
        "brick": rasterize([list(r) for r in BRICK], scale),
        "quest": rasterize([list(r) for r in QUESTION], scale),
    }
    pipes = {h: pipe_sprite(h, scale) for h in pipe_heights}

    # The ground is one tiled band, baked a tile wider than the panel, so a
    # frame is a slice at an offset rather than forty tile blits.
    top_rgb, _ = rasterize([list(r) for r in GROUND_TOP], scale)
    band_w = W + TS
    band = np.tile(top_rgb, (1, band_w // TS + 1, 1))[:ground_px, :band_w]
    # What a hole shows. Sky, as the original does, would be the lightest
    # colour on the panel here and a gap would read as a puddle; a dark shaft
    # is unambiguous at this size.
    pit = ds.gradient([(0.0, (56, 34, 18)), (0.45, (18, 12, 10)),
                       (1.0, (6, 5, 6))], max(2, ground_px))[:ground_px, None, :]

    # --- scenery ----------------------------------------------------------
    rng = np.random.default_rng(args.seed)
    sky = np.empty((H, W, 3), np.uint8)
    ramp = ds.gradient([(0.0, (56, 108, 232)), (0.7, (108, 168, 250)),
                        (1.0, (170, 212, 255))], max(2, ground_top))
    sky[:ground_top] = ramp[:, None, :]
    sky[ground_top:] = ramp[-1]

    strip_w = max(W * 2, 512)
    cloud_h = max(4, min(ground_top // 2, 22 * scale))
    clouds = cloud_strip(strip_w, cloud_h, rng, scale)
    cloud_y = max(0, min(2 * scale, ground_top - cloud_h))

    hill_h = max(3, min(ground_top // 2, 20 * scale))
    # Distant hills are darker and duller than anything in the level, which
    # is what puts them behind it -- at the same green as a pipe they read as
    # scenery in the same plane and the pipe vanishes into them.
    hills = hill_strip(strip_w, hill_h, rng, scale,
                       ((0, 92, 56), (36, 140, 84)), 90)
    bush_h = max(2, min(ground_top // 3, 7 * scale))
    bushes = hill_strip(strip_w + 97, bush_h, rng, scale,
                        ((0, 140, 60), (72, 192, 104)), 40)

    # --- state ------------------------------------------------------------
    level = Level(geo, args, rng)
    level.ensure(int(W / TS) + geo.look_cols + 8)

    hero_x = int(np.clip(args.hero_x, 0.05, 0.8) * W)
    st = SimpleNamespace(x=4.0 * TS, feet=float(ground_top), vy=0.0,
                         air=False, coins=0, last_t=None, acc=0.0,
                         rescues=0)

    back = np.empty((H, W, 3), np.uint8)
    buf = np.empty((H, W, 3), np.uint8)
    STEP = 1.0 / 120.0

    def physics(dt):
        st.x += sp * dt
        col = int(math.floor(st.x / TS))
        surf = level.surface(col)

        if st.air:
            st.vy += grav * dt
            st.feet += st.vy * dt
            if st.vy > 0 and surf is not None and st.feet >= surf:
                st.feet = float(surf)
                st.vy = 0.0
                st.air = False
        else:
            if surf is None:
                st.air = True           # ran off a lip: fall, do not freeze
                st.vy = 0.0
            elif st.feet < surf - 0.5:
                st.air = True           # stepped off a pipe
                st.vy = 0.0
            elif st.feet > surf:
                # Safety net, and the reason he can never wedge into a pipe:
                # solid ahead is stepped onto rather than blocking him.
                st.feet = float(surf)
            else:
                nxt = level.obstacle_ahead(st.x, st.feet)
                if nxt is not None:
                    a, b = nxt
                    d_take = 0.5 * (D - (b - a))
                    if a - st.x <= max(1.0, d_take):
                        st.vy = -v0
                        st.air = True

        if st.feet > H + 4 * TS:        # should be unreachable; never stall
            st.feet = float(ground_top)
            st.vy = 0.0
            st.air = False
            st.rescues += 1

        # goombas. A squashed one holds its flat pose for a moment and then
        # is gone for good -- the timer must latch, or it walks again.
        for g in level.goombas:
            if g[4] < 0.0:
                continue
            if g[4] > 0.0:
                g[4] -= dt
                if g[4] <= 0.0:
                    g[4] = -1.0
                continue
            g[0] += g[1] * 18.0 * scale * dt
            if g[0] < g[2]:
                g[0], g[1] = g[2], 1.0
            elif g[0] > g[3]:
                g[0], g[1] = g[3], -1.0

        # coins and stomps, against his bounding box
        cx, cy = st.x, st.feet - ch * 0.5
        for c in level.coins:
            if c[2] and abs(c[0] + 4 * scale - cx) < cw * 0.5 + 4 * scale \
                    and abs(c[1] + 4 * scale - cy) < ch * 0.5 + 5 * scale:
                c[2] = False
                st.coins += 1
        for g in level.goombas:
            if g[4] != 0.0:
                continue
            if abs(g[0] + 4 * scale - cx) < cw * 0.5 + 3 * scale \
                    and st.feet > ground_top - ch * 0.7:
                g[4] = 0.45
                st.coins += 1
                if not st.air or st.vy > 0:
                    st.vy = -0.45 * v0   # a small hop, well under the blocks
                    st.air = True

    def render(t, frame):
        # Fixed-step physics off the wall clock: the same arc whatever the
        # frame rate, and no tunnelling if a frame is late.
        if st.last_t is None:
            st.last_t = t
        dt = t - st.last_t
        st.last_t = t
        st.acc += max(0.0, min(dt, 0.25))
        steps = 0
        while st.acc >= STEP and steps < 32:
            physics(STEP)
            st.acc -= STEP
            steps += 1

        cam = st.x - hero_x
        level.ensure(int((cam + W) / TS) + geo.look_cols + 8)
        level.trim(cam)

        # --- background, slowest layers first ----------------------------
        np.copyto(back, sky)
        if not args.no_parallax:
            scroll_blit(back, clouds[0], clouds[1], cam * 0.12, cloud_y)
            scroll_blit(back, hills[0], hills[1], cam * 0.35,
                        ground_top - hill_h)
            scroll_blit(back, bushes[0], bushes[1], cam * 0.62,
                        ground_top - bush_h)
        np.copyto(buf, back)

        # --- ground, one slice, then holes cut back out ------------------
        off = int(cam) % TS
        buf[ground_top:, :] = band[:, off:off + W]
        c0 = int(math.floor(cam / TS))
        c1 = int(math.ceil((cam + W) / TS)) + 1
        for j in range(c0, c1):
            i = j - level.base
            if 0 <= i < len(level.solid) and not level.solid[i]:
                x0 = max(0, int(j * TS - cam))
                x1 = min(W, int(j * TS - cam) + TS)
                if x1 > x0:
                    buf[ground_top:, x0:x1] = pit

        # --- pipes -------------------------------------------------------
        for j in range(c0, c1):
            i = j - level.base
            if not (0 <= i < len(level.pipe)) or level.pipe[i] == 0:
                continue
            if i > 0 and level.pipe[i - 1] == level.pipe[i]:
                continue                     # second column of the same pipe
            h = level.pipe[i]
            rgb, mask = pipes[h]
            blit(buf, rgb, mask, ground_top - h * TS, int(j * TS - cam))

        # --- blocks ------------------------------------------------------
        for bx, by, kind in level.blocks:
            x = int(bx - cam)
            if -TS < x < W:
                rgb, mask = tiles["quest" if kind else "brick"]
                blit(buf, rgb, mask, by, x)

        # --- coins -------------------------------------------------------
        k = COIN_CYCLE[int(t * args.run_fps * 1.4) % len(COIN_CYCLE)]
        crgb, cmask = coins[k]
        for cxp, cyp, alive in level.coins:
            if not alive:
                continue
            x = int(cxp - cam)
            if -TS < x < W:
                blit(buf, crgb, cmask, int(cyp), x)

        # --- goombas -----------------------------------------------------
        gk = int(t * args.run_fps * 0.6) % 2
        for g in level.goombas:
            x = int(g[0] - cam)
            if -TS < x < W:
                if g[4] < 0.0:
                    continue                 # squashed and gone
                rgb, mask = goombas[2] if g[4] > 0.0 else goombas[gk]
                blit(buf, rgb, mask, ground_top - 8 * scale, x)

        # --- the plumber --------------------------------------------------
        if st.air:
            rgb, mask = hero[3]
        else:
            rgb, mask = hero[int(t * args.run_fps) % 3]
        blit(buf, rgb, mask, int(round(st.feet)) - ch,
             int(round(st.x - cam)) - cw // 2)

        # --- coin counter -------------------------------------------------
        if not args.no_hud:
            text = "%d" % min(st.coins, 999)
            dw = 4 * scale
            x = W - 2 * scale - len(text) * dw
            blit(buf, crgb, cmask, 2 * scale, x - TS - scale)
            # Drawn twice: a dark copy offset by a pixel, then the white one.
            # Without it the counter disappears whenever a cloud drifts under
            # the corner, which on a sky this bright is most of the time.
            for off, col in ((scale, hud_dark), (0, hud_rgb)):
                xx = x
                for ch_ in text:
                    blit(buf, col, DIGIT_MASKS[ch_], 4 * scale + off, xx + off)
                    xx += dw
        return buf

    DIGIT_MASKS = {d: rasterize([list(r.replace("X", "W")) for r in art],
                                scale)[1]
                   for d, art in DIGITS.items()}
    hud_rgb = np.full((5 * scale, 3 * scale, 3), 255, np.uint8)
    hud_dark = np.zeros((5 * scale, 3 * scale, 3), np.uint8)

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
