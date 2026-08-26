#!/usr/bin/env python3
"""This is fine.

A dog in a hat sits at a small table with a cup of coffee, in a room that is
slowly filling with fire. It blinks. Now and then it lifts the cup and takes a
sip. The room gets steadily worse. Nothing else happens, and that is the joke:
the whole gag is the *calm*, so the one rule this demo has is that the dog
never reacts. If it flinched, or if the cup rattled, the panel would be a dog
in trouble instead of a dog insisting everything is under control.

The scene is drawn here from a description, not copied: the dog, the hat, the
table and the cup are character grids with a palette, in the same way mario.py
and nyancat.py build their sprites, and the room -- back wall, doorway, picture
frame, shelf, floorboards -- is painted with rectangles at build time. Nothing
is traced and nothing is downloaded.

The fire is the engineering problem. The classic demoscene fire in fire.py is
*inherently* stateful: each frame takes the heat buffer left by the previous
one, shifts it up and cools it. A demo like that cannot be a pure function of
`t`, and the scheduler builds segments ahead on a worker thread and starts them
at t=0, so anything accumulating between render() calls desyncs. Re-running N
simulation steps from a fixed seed every frame would restore purity but costs N
times a whole-panel pass, which is out of the question on a Pi.

So the flame here is a *field*, not a simulation:

    heat(y, x, t) = clip(fuel(x, stage) * turbulence(x, y + scroll(t)) - b(y))

`turbulence` is one wrapping noise texture baked once in build(): a low
resolution random field, upsampled, sheared so its streaks lean, and blurred by
wrapping rolls so it tiles top to bottom. Scrolling it upward at a fixed rate
is what makes tongues rise; because it wraps, the scroll is a plain array slice
at `int(t * speed) % TILE_H` and costs nothing. `b(y)` is height above the floor
line, so subtracting it is what gives every column a flame that runs out at some
height; `fuel(x, stage)` is a per-column fuel profile, baked once per lighting
stage, which is what walks the fire from one corner to the whole room. The
result is a pure function of t, six whole-panel numpy calls, and it reads as
fire because the palette carries it -- exactly the reason fire.py maps a scalar
field through a ramp rather than computing RGB.

The room's lighting is the other half of "the room is on fire". It is baked, not
computed: build() paints the room twice, clean and charred, and then renders
--stages fully lit frames interpolating between them, each with the orange
bounce light and the ceiling smoke for that point in the arc. A frame picks one
by index, so the whole room costs a single copy. The dog and the cup are baked
the same way, lit from the same field sampled at the sprite's own rows, so the
character sits *in* the light rather than on top of it.

The arc, over --cycle seconds: a little flame in the corner; fire along the
walls and up through the doorway; the whole room, hat slightly singed; and then
the line, once, at the beat where it is funniest -- which is after you have had
long enough to notice the dog has not moved.

Run:  python3 fine.py --host 127.0.0.1
      python3 fine.py --cycle 30 --scroll 26
      python3 fine.py --no-text --cycle 20
"""

import sys

import numpy as np

import defcon
import demoscene as ds

f32 = np.float32


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, propagation, sort and tide
# draw with: five rows a glyph, each row an octal digit whose three bits are
# the three columns. Imported read-only rather than copied so the panels keep
# agreeing on what a letter looks like.
#
# The size is measured, never assumed: a glyph is 5 rows and 3 columns *at
# scale 1*, so the punchline's box is (5*scale) tall and (len*4-1)*scale wide,
# computed from the mask that is actually going to be drawn. Assuming a pixel
# size is what once clipped the bottom off every capital E on this wall.
# --------------------------------------------------------------------------

_GLYPHS = {}
for _ch, _rows in defcon._FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((5 * scale, 1), bool)
    out = np.zeros((5, len(s) * 4 - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


# --------------------------------------------------------------------------
# The art, as data. '.' is transparent, everything else indexes PALETTE.
# Same convention as mario.py and nyancat.py.
# --------------------------------------------------------------------------

PALETTE = {
    "y": (198, 162, 84),      # straw hat
    "Y": (232, 200, 122),     # hat, lit crown
    "k": (96, 62, 30),        # hat band
    "o": (44, 28, 16),        # outline / shadow line

    "d": (214, 176, 112),     # dog, fur
    "D": (138, 100, 56),      # dog, fur in shadow (ears, underside)
    "w": (244, 226, 194),     # muzzle, sclera
    "e": (26, 18, 12),        # eye, nose

    "c": (238, 236, 228),     # cup
    "C": (186, 182, 172),     # cup, shaded side
    "q": (74, 44, 24),        # coffee
}

# The hat: a low crown, a band, and a brim wide enough to read as a hat from
# three metres. Six rows is all it gets -- any taller and it stops being a hat
# and starts being a chef.
HAT = (
    "........yyyyyyyyyy........",
    ".......yYYYYYYYYYYy.......",
    ".......yYYYYYYYYYYy.......",
    ".......kkkkkkkkkkkk.......",
    "...yyyyyyyyyyyyyyyyyyyy...",
    "...oooooooooooooooooooo...",
)

# The head: twelve wide with a hanging ear either side. The eyes are two
# pixels of sclera with one dark pixel in them, which is the smallest thing
# that still reads as an eye rather than a smudge -- and it is the only moving
# part of the dog, so it has to.
HEAD = (
    ".......dddddddddddd.......",
    ".....DDddddddddddddDD.....",
    ".....DDddwwddddwwddDD.....",
    ".....DDddweddddwedddD.....",
    ".....DDdddddddddddddD.....",
    ".....DDddddwwwwwwdddD.....",
    ".....DDdddwwwwwwwwddD.....",
    "......Ddddwweewwwdddd.....",
    "......odddwwwwwwddddo.....",
    ".......dddwwwwwwddd.......",
    "........dddddddddd........",
)
# Eyes closed. Only the two eye rows differ, so the blink is a two-row patch
# rather than a second copy of the whole head.
EYES_SHUT = (
    ".....DDddddddddddddDD.....",
    ".....DDddooddddooddDD.....",
)
EYES_ROW = 2

# Shoulders. Most of this is behind the table; what shows is the slope of a
# dog sitting up straight, which is the posture the joke needs.
BODY = (
    ".......dddddddddddd.......",
    "......dddddddddddddd......",
    ".....dddddddddddddddd.....",
    "....dddddddddddddddddd....",
    "...dddddddddddddddddddd...",
    "...dddddddddddddddddddd...",
    "..dddddddddddddddddddddd..",
    "..dddddddddddddddddddddd..",
)

# The cup, with the paw under it, because a cup that floats is a poltergeist.
# Five rows: rim, coffee, body, body, paw.
CUP = (
    "ccccc",
    "cqqqc",
    "cCCCc",
    "cCCCc",
    ".DDD.",
)


def rasterize(grid, palette=PALETTE):
    """(rows of chars) -> ((h,w,3) float32, (h,w) bool)."""
    h, w = len(grid), len(grid[0])
    for row in grid:
        if len(row) != w:
            raise ValueError("ragged sprite: %r is %d wide, expected %d"
                             % (row, len(row), w))
    rgb = np.zeros((h, w, 3), f32)
    mask = np.zeros((h, w), bool)
    for r in range(h):
        for c in range(w):
            ch = grid[r][c]
            if ch != ".":
                rgb[r, c] = palette[ch]
                mask[r, c] = True
    return rgb, mask


def paste(grid, art, y=0, x=0):
    """Stamp rows of characters over a list-of-lists grid, ignoring '.'."""
    for r, row in enumerate(art):
        for c, ch in enumerate(row):
            if ch != ".":
                grid[y + r][x + c] = ch
    return grid


def dog_grid(shut, singed):
    """The whole dog as one character grid, in the four states it has."""
    rows = [list(r) for r in HAT] + [list(r) for r in HEAD] \
        + [list(r) for r in BODY]
    w = len(rows[0])
    for r in rows:
        if len(r) != w:
            raise ValueError("ragged dog row: %d, expected %d" % (len(r), w))
    for r in EYES_SHUT:
        if len(r) != w:
            raise ValueError("ragged blink row: %d, expected %d" % (len(r), w))
    if shut:
        paste(rows, EYES_SHUT, len(HAT) + EYES_ROW, 0)
    if singed:
        # Notches burnt out of the brim and a scorch up one side of the crown.
        # Damage, not distress: the hat is worse, the dog is not.
        for x in (4, 5, 9, 17, 21):
            rows[4][x] = "."
            rows[5][x] = "."
        for x in (16, 17, 18):
            rows[1][x] = "k"
        rows[2][17] = "k"
        rows[0][15] = "."
        rows[0][16] = "."
    return rows


# --------------------------------------------------------------------------
# The room. Painted with rectangles into a float image at build time, twice:
# clean, and charred. Every lit stage is a blend of the two, so "the shelf
# blackens" costs nothing at render time.
# --------------------------------------------------------------------------

def paint_room(W, H, geo, charred):
    """A (H, W, 3) float image of the room, clean or blackened."""
    img = np.zeros((H, W, 3), f32)
    k = 1.0 if charred else 0.0

    def mix(a, b, f):
        return tuple(a[i] + (b[i] - a[i]) * f for i in range(3))

    def rect(y0, y1, x0, x1, col):
        y0, y1 = max(0, y0), min(H, y1)
        x0, x1 = max(0, x0), min(W, x1)
        if y1 > y0 and x1 > x0:
            img[y0:y1, x0:x1] = col

    floor_y = geo["floor_y"]

    WALL = mix((124, 102, 84), (30, 23, 20), k)
    WALL_HI = mix((146, 122, 100), (38, 29, 24), k)
    RAIL = mix((186, 152, 108), (46, 34, 26), k)
    BOARD = mix((150, 108, 70), (40, 28, 20), k)
    BOARD_D = mix((116, 80, 48), (28, 20, 14), k)
    SOOT = (20, 15, 13)

    # Wall, slightly lighter at the top so the panel has somewhere for the
    # smoke to go and be seen going there.
    rect(0, floor_y, 0, W, WALL)
    rect(0, 10, 0, W, WALL_HI)
    # Picture rail: one bright line across the wall is what makes it a room
    # rather than a backdrop.
    rect(10, 11, 0, W, RAIL)

    # Floorboards. Horizontal courses, because the panel is seen straight on
    # and vanishing-point boards on 20 rows are just noise.
    rect(floor_y, H, 0, W, BOARD)
    for y in range(floor_y + 4, H, 6):
        rect(y, y + 1, 0, W, BOARD_D)
    rect(floor_y, floor_y + 1, 0, W, mix((196, 152, 104), (56, 42, 32), k))

    # Doorway, left. Dark opening with a frame; late in the cycle the fire
    # comes through it, which is why it is a hole and not a door.
    dx0, dx1, dy = geo["door"]
    rect(dy - 2, floor_y, dx0 - 2, dx1 + 2, mix((132, 104, 76), (44, 33, 25), k))
    rect(dy, floor_y, dx0, dx1, mix((14, 10, 10), (8, 6, 6), k))

    # Picture on the wall, hung a pixel out of true.
    px0, py0 = geo["picture"]
    rect(py0, py0 + 13, px0, px0 + 22, mix((150, 118, 70), (42, 32, 22), k))
    rect(py0 + 2, py0 + 11, px0 + 2, px0 + 20,
         mix((92, 132, 150), (24, 22, 20), k))
    rect(py0 + 7, py0 + 11, px0 + 2, px0 + 20,
         mix((70, 108, 82), (20, 18, 16), k))

    # Shelf, right, with three objects standing on it. They are the things
    # that read as "furniture catching" when they go black.
    sx0, sx1, sy = geo["shelf"]
    rect(sy, sy + 2, sx0, sx1, mix((128, 96, 62), (38, 28, 20), k))
    for i, (ox, ow, oh, col) in enumerate((
            (4, 4, 9, (176, 74, 60)), (10, 3, 12, (72, 108, 150)),
            (15, 5, 7, (150, 138, 96)), (23, 3, 10, (96, 140, 96)))):
        rect(sy - oh, sy, sx0 + ox, sx0 + ox + ow, mix(col, (30, 22, 18), k))

    # Table: a plank and two legs, standing in front of the floor line.
    tx0, tx1, ty = geo["table"]
    rect(ty, ty + 3, tx0, tx1, mix((150, 100, 58), (48, 33, 22), k))
    rect(ty, ty + 1, tx0, tx1, mix((206, 152, 96), (62, 45, 32), k))
    rect(ty + 2, ty + 3, tx0, tx1, mix((84, 54, 30), (26, 18, 12), k))
    # Legs kept lighter than the floor behind them: at this size a table is
    # only a table if you can see it standing on something.
    for lx in (tx0 + 3, tx1 - 7):
        rect(ty + 3, ty + 17, lx, lx + 4, mix((132, 90, 52), (40, 28, 19), k))
        rect(ty + 3, ty + 17, lx + 3, lx + 4, mix((78, 50, 28), (24, 17, 12), k))

    if charred:
        # Soot plumes up the wall above where things burn hardest: the corner,
        # the doorway, and the middle of the right-hand wall.
        xs = np.arange(W, dtype=f32)
        ys = np.arange(H, dtype=f32)
        plume = np.zeros(W, f32)
        for cx, wd, amp in ((W - 16, 26.0, 1.0), (30.0, 22.0, 0.85),
                            (W * 0.72, 40.0, 0.6), (W * 0.42, 46.0, 0.35)):
            plume += amp * np.exp(-((xs - cx) / wd) ** 2)
        np.clip(plume, 0.0, 1.0, out=plume)
        up = np.clip((floor_y - ys) / float(floor_y), 0.0, 1.0) ** 0.6
        f = (plume[None, :] * up[:, None] * 0.8)[:, :, None]
        img *= (1.0 - f)
        img += np.array(SOOT, f32) * f
    return img


def light_field(H, W, geo, s):
    """The (H, W, 3) multiplier the room is lit by at arc position s (0..1).

    Two glows, blended by s: one hotspot in the corner where it starts, and
    one broad wash off the whole floor once the room has gone. Ambient falls
    as the fire rises, because the only light left in the room is the fire.
    """
    ys = np.arange(H, dtype=f32)[:, None]
    xs = np.arange(W, dtype=f32)[None, :]
    floor_y = geo["floor_y"]

    corner = np.exp(-(((xs - (W - 14)) / 46.0) ** 2
                      + ((ys - floor_y) / 26.0) ** 2))
    wash = np.exp(-np.clip(floor_y - ys, 0.0, None) / 30.0) \
        * (0.55 + 0.45 * np.exp(-((xs - W * 0.62) / 150.0) ** 2))

    glow = (1.0 - s) * corner * 0.9 + s * wash * 1.35
    # Ambient falls as the fire rises: by the end the only light in the room
    # is the fire, which is what turns the walls orange without tinting them.
    amb = 0.95 - 0.66 * s
    warm = np.array((1.0, 0.55, 0.22), f32)
    out = amb + glow[:, :, None] * warm[None, None, :] * (0.55 + 0.85 * s)

    # Ceiling smoke: the top of the room loses light, hard, and it is the
    # single clearest signal that the room is filling rather than just lit.
    smoke = np.clip(1.0 - ys / (floor_y * 0.72), 0.0, 1.0) ** 1.4
    out *= (1.0 - 0.72 * s * smoke)[:, :, None]
    return out


def fuel_profile(W, geo, s):
    """Per-column fuel, 0..~1.2, at arc position s. This is the arc.

    Stage 1 is one hump in the far corner. Stage 2 adds the two walls and the
    doorway. Stage 3 fills the middle of the room. A shallow dip is kept over
    the dog for as long as it can be justified, so the silhouette survives --
    once the room is fully gone the dip is nearly closed and the dog is
    sitting in it.
    """
    xs = np.arange(W, dtype=f32)

    def hump(cx, wd):
        return np.exp(-((xs - cx) / wd) ** 2)

    def ramp(a, b):
        return float(np.clip((s - a) / (b - a), 0.0, 1.0))

    corner = hump(W - 14, 17.0)
    edges = hump(W - 2, 30.0) + hump(2.0, 24.0)
    door = hump(sum(geo["door"][:2]) * 0.5, 15.0)
    # A gentle swell rather than a full wave: at full engulf every column has
    # to be burning, so the trough of this term is the floor of the effect and
    # a sine that reaches zero leaves a cold patch in the middle of the room.
    broad = 0.72 + 0.28 * np.sin(xs * 0.013 + 1.3)

    # The opening beat has to be a *flame*, not a glow: if the first ten
    # seconds are just a warm corner then the panel reads as a lit room and
    # nothing is wrong yet, which loses the setup.
    fuel = (0.42 + 0.82 * ramp(0.0, 0.9)) * corner
    fuel += 0.85 * ramp(0.18, 0.62) * edges
    fuel += 0.95 * ramp(0.34, 0.78) * door
    fuel += 1.00 * ramp(0.42, 1.0) * broad
    fuel *= 1.0 - (0.62 - 0.50 * s) * hump(geo["dog_cx"], 32.0)
    return np.clip(fuel, 0.0, 1.25).astype(f32)


def turbulence(H, W, rng, rows=44):
    """A wrapping noise texture, vertically tiled so scrolling is a slice.

    Low-resolution random values, upsampled, sheared so the streaks lean the
    way a draught pushes flame, then blurred with wrapping rolls -- which is
    what makes it tile top to bottom, and therefore what makes the whole
    effect a pure function of t.

    The size is derived from the upsampling factors rather than rounded to a
    wanted height, because a tile that is not an exact multiple does not wrap,
    and a tile that does not wrap puts a seam through the fire once a loop.
    """
    nw = -(-W // 4)                       # 4x horizontally, 6x vertically
    n = rng.random((rows, nw)).astype(f32)
    # Two octaves: the coarse one gives separate tongues, the fine one gives
    # the ragged edge that stops them looking like ribbons.
    fine = rng.random((rows * 2, nw * 2)).astype(f32)
    n = np.repeat(np.repeat(n, 2, 0), 2, 1) * 0.68 + fine * 0.32
    n = np.repeat(np.repeat(n, 3, 0), 2, 1)
    tile_h = n.shape[0]
    n = n[:, :W]

    for _ in range(4):
        n = (n + np.roll(n, 1, 0) + np.roll(n, -1, 0)
             + np.roll(n, 1, 1) + np.roll(n, -1, 1)) / 5.0
    # Lean. The obvious thing is a linear shear -- roll row y by y/6 columns --
    # and it is wrong: over the tile the total shift is 44 columns, so where
    # the tile wraps the shift snaps back to zero and a 44 pixel step walks up
    # the panel once per loop, which looks exactly like a torn scanline. A
    # shift that is itself periodic in the tile height wraps for free, and a
    # wandering lean is closer to what a draught does to flame than a constant
    # one anyway.
    rows_i = np.arange(tile_h)[:, None]
    shift = np.round(7.0 * np.sin(2.0 * np.pi * rows_i / float(tile_h))
                     + 4.0 * np.sin(6.0 * np.pi * rows_i / float(tile_h)))
    idx = (np.arange(W)[None, :] + shift.astype(np.int64)) % W
    n = np.take_along_axis(n, idx, axis=1)

    n -= n.min()
    n /= max(n.max(), 1e-6)
    return (0.42 + 1.05 * n).astype(f32), tile_h


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cycle", type=float, default=46.0,
                    help="seconds for one burn, from a small flame back to one")
    ap.add_argument("--scroll", type=float, default=34.0,
                    help="how fast the flame texture rises, rows/sec")
    ap.add_argument("--stages", type=int, default=24,
                    help="baked lighting steps across the arc")
    ap.add_argument("--embers", type=int, default=54, help="rising sparks")
    ap.add_argument("--no-text", action="store_true",
                    help="no punchline; just the room")
    ap.add_argument("--text", default="THIS IS FINE.", help="the punchline")
    ap.add_argument("--text-scale", type=int, default=3,
                    help="pixel scale of the punchline's 3x5 font")
    ap.add_argument("--seed", type=int, default=7)


def bubble_layout(text, scale, W, H):
    """Where the speech bubble goes, and the three masks that make it.

    A pure function of the panel size, at module scope on purpose: `build()`
    draws from this and the test checks against it, so the geometry is
    described once. The previous banner had its placement formula copied
    into the test, which is exactly the arrangement that lets a demo and its
    test drift apart while both stay green.

    Returns fill / border / glyph masks in card coordinates, the card size,
    and the (ty, tx) it should be pasted at.
    """
    # The dog's geometry, rederived from the same two numbers build() uses.
    floor_y = int(round(H * 0.69))
    dog_w = len(HAT[0])
    dog_x = int(round(W * 0.30)) - dog_w // 2
    table_y = floor_y - 3
    dog_h = len(HAT) + len(HEAD) + len(BODY)
    dog_y = table_y + 2 - dog_h
    dog_cx = dog_x + dog_w * 0.5

    tmask = text_mask(text, max(1, scale))
    gh, gw = tmask.shape
    PADX, PADY, TAIL, MARGIN = 4, 3, 4, 1
    bh, bw = gh + 2 * PADY, gw + 2 * PADX
    th, tw = bh + TAIL + 2 * MARGIN, bw + 2 * MARGIN

    # Shift-based dilation rather than np.roll: roll wraps, and a border
    # computed with it would wrap the tail's ink around to the top edge.
    def _dilate(m):
        d = m.copy()
        d[1:, :] |= m[:-1, :]; d[:-1, :] |= m[1:, :]
        d[:, 1:] |= m[:, :-1]; d[:, :-1] |= m[:, 1:]
        d[1:, 1:] |= m[:-1, :-1]; d[1:, :-1] |= m[:-1, 1:]
        d[:-1, 1:] |= m[1:, :-1]; d[:-1, :-1] |= m[1:, 1:]
        return d

    fill = np.zeros((th, tw), bool)
    fill[MARGIN:MARGIN + bh, MARGIN:MARGIN + bw] = True
    # Knock the corners off. A two-step chamfer is as much "round" as eleven
    # rows can carry; an actual radius just loses the corner pixel.
    for r in range(2):
        for c in range(2 - r):
            fill[MARGIN + r, MARGIN + c] = False
            fill[MARGIN + r, MARGIN + bw - 1 - c] = False
            fill[MARGIN + bh - 1 - r, MARGIN + c] = False
            fill[MARGIN + bh - 1 - r, MARGIN + bw - 1 - c] = False
    # The tail hangs off the bubble's lower left and leans left, because the
    # dog is down and to the left of it. Its tip is what gets aimed at him.
    tail_x = max(2, int(bw * 0.20))
    for r in range(TAIL):
        x0, x1 = tail_x - r, tail_x + (TAIL - r)
        if x1 > x0:
            fill[MARGIN + bh + r,
                 MARGIN + max(0, x0):MARGIN + min(bw, x1)] = True

    border = _dilate(fill) & ~fill
    glyphs = np.zeros((th, tw), bool)
    glyphs[MARGIN + PADY:MARGIN + PADY + gh,
           MARGIN + PADX:MARGIN + PADX + gw] = tmask

    # Placement. The tail's tip is the thing being aimed, not the box: it
    # goes just above the dog's head, which puts the bubble over his
    # shoulder. The bubble must never touch him -- "the dog never moves" is
    # the invariant this whole demo rests on, and a bubble drawn across his
    # ears would break it in a way that still looks plausible in a still.
    ty = dog_y - 2 - th
    tx = int(round(dog_cx)) - (MARGIN + tail_x)
    if ty < 1:
        # Not enough headroom at this text size. Sit it beside him instead;
        # the tail leans left, so from there it still points back at him.
        ty = 1
        tx = dog_x + dog_w + 6
    tx = int(np.clip(tx, 1, max(1, W - tw - 1)))
    if ty + th > dog_y and tx < dog_x + dog_w and tx + tw > dog_x:
        raise ValueError(
            "fine: the bubble for %r at scale %d will not fit clear of the "
            "dog on a %dx%d panel" % (text, scale, W, H))
    return {"fill": fill, "border": border, "glyphs": glyphs,
            "th": th, "tw": tw, "ty": ty, "tx": tx}


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    K = max(2, args.stages)

    # --- geometry ---------------------------------------------------------
    # The room in rows: wall, then floor at 69% down. Everything else hangs
    # off those two numbers so a different panel size still makes a room.
    floor_y = int(round(H * 0.69))
    dog_w = len(HAT[0])
    dog_x = int(round(W * 0.30)) - dog_w // 2
    table_x0 = dog_x - 12
    table_x1 = dog_x + dog_w + 12
    table_y = floor_y - 3
    geo = {
        "floor_y": floor_y,
        "door": (int(W * 0.035), int(W * 0.155), 12),
        "picture": (int(W * 0.47), 22),
        "shelf": (int(W * 0.74), int(W * 0.90), 27),
        "table": (table_x0, table_x1, table_y),
        "dog_cx": dog_x + dog_w * 0.5,
    }
    # The dog's feet are the table top; the sprite hangs upward from there.
    dog_h = len(HAT) + len(HEAD) + len(BODY)
    dog_y = table_y + 2 - dog_h
    cup_rest = (table_y - len(CUP), dog_x + dog_w + 2)
    cup_lip = (dog_y + len(HAT) + 7, dog_x + dog_w - 9)
    # The sprites are a fixed size, so a panel much smaller than the wall's
    # has nowhere to put them. Say so here rather than throwing a shape
    # mismatch out of a blit forty lines later.
    if dog_y < 0 or dog_x < 0 or dog_x + dog_w > W or table_y + 17 > H:
        raise ValueError("fine needs at least %dx%d; got %dx%d"
                         % (dog_w * 4, dog_h + 24, W, H))

    # --- the room, baked at every lighting stage --------------------------
    clean = paint_room(W, H, geo, False)
    burnt = paint_room(W, H, geo, True)
    dog_rgb = {}
    dog_mask = {}
    for shut in (False, True):
        for singed in (False, True):
            dog_rgb[(shut, singed)], dog_mask[(shut, singed)] = \
                rasterize(dog_grid(shut, singed))
    cup_rgb, cup_mask = rasterize(CUP)

    rooms = []
    dogs = {}
    cups = []
    for k in range(K):
        s = k / float(K - 1)
        L = light_field(H, W, geo, s)
        base = clean * (1.0 - s) + burnt * s
        rooms.append(ds.dither(np.clip(base * L, 0, 255)))
        # Sprites are lit by the same field sampled where they actually sit,
        # so the dog picks up the room's colour instead of being pasted on.
        dl = L[dog_y:dog_y + dog_h, dog_x:dog_x + dog_w]
        for key, rgb in dog_rgb.items():
            dogs.setdefault(key, []).append(
                np.clip(rgb * dl, 0, 255).astype(np.uint8))
        cl = L[cup_rest[0]:cup_rest[0] + len(CUP),
               cup_rest[1]:cup_rest[1] + len(CUP[0])]
        cups.append(np.clip(cup_rgb * cl, 0, 255).astype(np.uint8))
    rooms = np.array(rooms)
    dogs = dict((key, np.array(v)) for key, v in dogs.items())
    cups = np.array(cups)

    # --- the flame field --------------------------------------------------
    noise, tile_h = turbulence(H, W, rng)
    # Two copies stacked, so a scrolled window is a slice with no modulo and
    # no copy at all.
    noise2 = np.vstack([noise, noise[:H]])
    fuels = np.array([fuel_profile(W, geo, k / float(K - 1)) for k in range(K)])

    # Height above the floor, normalised. Below the floor line it is zero, so
    # the floorboards themselves glow rather than being a cold shelf the fire
    # stands on.
    rows = np.arange(H, dtype=f32)
    b = np.clip((floor_y - rows) / float(floor_y), 0.0, None).astype(f32)[:, None]

    lut = ds.gradient(ds.FIRE, 256)
    # Two baked phase ramps, so the travelling waves cost one add and one sin
    # each rather than a multiply per frame.
    ph1 = (np.arange(W, dtype=f32) * 0.085)
    ph2 = (np.arange(W, dtype=f32) * 0.027)

    # --- embers -----------------------------------------------------------
    ne = max(0, args.embers)
    em_x = rng.uniform(0, W, ne).astype(f32)
    em_life = rng.uniform(1.6, 4.2, ne).astype(f32)
    em_phase = rng.uniform(0, 10.0, ne).astype(f32)
    em_rise = rng.uniform(7.0, 20.0, ne).astype(f32)
    em_amp = rng.uniform(1.5, 7.0, ne).astype(f32)
    em_w = rng.uniform(0.9, 2.6, ne).astype(f32)
    em_p = rng.uniform(0, 6.28, ne).astype(f32)
    em_rank = rng.random(ne).astype(f32)      # which embers exist how early
    em_y0 = np.full(ne, float(floor_y) + 2.0, f32)
    EMBER = np.array((255, 196, 96), np.uint8)

    # --- the punchline, as a speech bubble --------------------------------
    # The line is dialogue, not a title card. Centred white type across the
    # top read as a caption imposed on the scene -- the wall announcing the
    # joke rather than the dog saying it -- so it is drawn the way a comic
    # draws speech: dark ink inside a light rounded bubble with a tail
    # pointing at whoever is talking.
    #
    # The bubble also solves the legibility problem more honestly than the
    # banner did. By the time the line appears the room is entirely orange,
    # and white type on it needed a per-glyph halo to survive. A light
    # bubble puts the type on its own ground, so the contrast comes from the
    # object rather than from an outline traced around every letter.
    #
    # Geometry lives in bubble_layout() at module scope so the test can
    # check the drawn bubble against it rather than against a copy.
    _lay = bubble_layout(args.text, max(1, args.text_scale), W, H)
    th, tw = _lay["th"], _lay["tw"]
    ty, tx = _lay["ty"], _lay["tx"]
    INK = (26, 16, 12)
    text_card = np.zeros((th, tw, 3), f32)
    text_card[_lay["fill"]] = (250, 246, 235)
    text_card[_lay["border"]] = INK
    text_card[_lay["glyphs"]] = INK
    text_any = _lay["fill"] | _lay["border"]
    card_pix = text_card[text_any]

    # --- the schedule, drawn once ----------------------------------------
    # The dog's whole performance is three sips and a handful of blinks, and
    # the *sequence* is fixed here so render() only ever asks where it is in
    # it. Nothing about the dog is decided per frame.
    #
    # Both are drawn on jittered slots rather than as plain uniforms. Three
    # uniform draws over a cycle overlap about as often as not, and two sips
    # 0.9 s apart is the dog fumbling its coffee -- which is a reaction, which
    # is the one thing this demo is not allowed to have.
    cyc = max(4.0, args.cycle)
    SIP = 2.6
    # How many sips depends on how long the cycle is: three of them in a
    # twelve second loop is a dog gulping its coffee, which is a reaction.
    # One every sixteen seconds or so is a dog with all the time in the world.
    nsip = int(min(3, max(1, round(cyc / 16.0))))
    slots = {1: ((0.30, 0.50),),
             2: ((0.10, 0.26), (0.62, 0.78)),
             3: ((0.08, 0.24), (0.40, 0.56), (0.80, 0.90))}[nsip]
    sips = np.array([rng.uniform(a, b) * cyc for a, b in slots], f32)
    nb = 9
    blinks = np.array([(i + rng.uniform(0.15, 0.85)) / nb * cyc
                       for i in range(nb)], f32)
    # The line lands late: long enough after the room has gone that you have
    # had time to notice the dog is not going to do anything about it.
    text_on, text_off = 0.755 * cyc, 0.960 * cyc

    buf = np.empty((H, W, 3), np.uint8)
    heat = np.empty((H, W), f32)
    tsl = slice(table_y, min(H, table_y + 16))
    txl = slice(max(0, table_x0 - 1), min(W, table_x1 + 1))

    def smoothstep(u):
        u = float(np.clip(u, 0.0, 1.0))
        return u * u * (3.0 - 2.0 * u)

    def render(t, frame):
        tt = t % cyc
        # The arc: up over the first three quarters, held, then back down over
        # the last few seconds so the loop closes on a room worth setting fire
        # to again.
        if tt < 0.70 * cyc:
            s = smoothstep(tt / (0.70 * cyc))
        elif tt < 0.965 * cyc:
            s = 1.0
        else:
            # A quick collapse rather than a slow one: the room un-burning is
            # the one shot in the loop that cannot be sold, so it is over in
            # about a second and a half and reads as a cut back to the top.
            s = 1.0 - smoothstep((tt - 0.965 * cyc) / (0.035 * cyc))
        k = min(K - 1, int(s * (K - 1) + 0.5))

        # --- flame field: six whole-panel calls, all pure in t -------------
        wave = np.sin(ph1 + t * 2.05)
        wave += np.sin(ph2 - t * 1.28)
        wave *= 0.18
        wave += 0.74                              # 0.38 .. 1.10
        wave *= fuels[k]

        off = int(t * args.scroll) % tile_h
        # Augmented assignment would rebind `heat` as a local; every step
        # writes through out= into the scratch buffer instead, which is also
        # the whole point of having one.
        np.multiply(noise2[off:off + H], wave[None, :], out=heat)
        np.subtract(heat, b, out=heat)
        np.clip(heat, 0.0, 1.0, out=heat)
        np.multiply(heat, 255.0, out=heat)

        # One pass, not two: the room and the fire meet in the same call that
        # writes the frame, so the background is never copied and then
        # overwritten. The fire is composited with maximum rather than a
        # blend because a flame is emissive -- it should never darken what it
        # is standing in front of.
        np.maximum(rooms[k], lut[heat.astype(np.uint8)], out=buf)
        # The table is redrawn over the fire: the dog's own table stays a
        # clean silhouette, which is the only place in the frame where the
        # picture is allowed to be calm.
        buf[tsl, txl] = rooms[k][tsl, txl]

        # --- embers -------------------------------------------------------
        if ne and s > 0.08:
            age = (t + em_phase) % em_life
            ey = em_y0 - em_rise * age
            ex = em_x + em_amp * np.sin(em_w * t + em_p)
            live = (ey > 0.0) & (em_rank < s * 1.15)
            yi = ey[live].astype(np.int32)
            xi = ex[live].astype(np.int32) % W
            buf[yi, xi] = EMBER

        # --- the dog ------------------------------------------------------
        # Where are we in the sip? Everything about the pose comes from this
        # one number, and the number comes from a table drawn in build().
        i = int(np.searchsorted(sips, tt)) - 1
        u = (tt - sips[i]) / SIP if i >= 0 else 2.0
        if 0.0 <= u <= 1.0:
            if u < 0.34:
                f = smoothstep(u / 0.34)
            elif u < 0.62:
                f = 1.0
            else:
                f = 1.0 - smoothstep((u - 0.62) / 0.38)
        else:
            f = 0.0
        drinking = 0.42 <= u <= 0.58

        j = int(np.searchsorted(blinks, tt)) - 1
        shut = drinking or (j >= 0 and 0.0 <= tt - blinks[j] < 0.17)

        art = dogs[(shut, s > 0.52)][k]
        m = dog_mask[(shut, s > 0.52)]
        np.copyto(buf[dog_y:dog_y + dog_h, dog_x:dog_x + dog_w], art,
                  where=m[:, :, None])

        cy = int(round(cup_rest[0] + (cup_lip[0] - cup_rest[0]) * f))
        cx = int(round(cup_rest[1] + (cup_lip[1] - cup_rest[1]) * f))
        ch, cw = cup_mask.shape
        np.copyto(buf[cy:cy + ch, cx:cx + cw], cups[k],
                  where=cup_mask[:, :, None])

        # --- the line -----------------------------------------------------
        if not args.no_text and text_on <= tt < text_off:
            a = min(1.0, (tt - text_on) / 0.35)
            if tt > text_off - 0.6:
                a = min(a, (text_off - tt) / 0.6)
            sub = buf[ty:ty + th, tx:tx + tw]
            px = sub[text_any].astype(f32)
            px *= (1.0 - a)
            px += card_pix * a
            sub[text_any] = px.astype(np.uint8)

        return buf

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
