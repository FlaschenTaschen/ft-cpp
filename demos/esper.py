#!/usr/bin/env python3
"""The Esper machine from Blade Runner: a photograph, enhanced to death.

Deckard sits in front of a screen and talks to it -- ENHANCE 224 176, PAN
RIGHT, STOP, TRACK 45 LEFT -- and the machine walks a reticle over a
photograph, dives into the box, and resolves what is inside it, until a detail
that was never visible in the original is filling the frame. That sequence is
almost the only thing in cinema built for a 5:1 letterbox: the picture is a
wide still, the commands run along the bottom in a single thin line, and the
whole drama is a crop rectangle moving. 320x64 is exactly that shape.

**The blockiness is the aesthetic, not a compromise.** The panel is 320 px
across; anything zoomed past about 4x is visibly made of squares. The film
leans on that too -- the Esper's enhancements arrive as chunky mosaics that
sharpen in passes -- so this demo does it on purpose: every move ends with the
image frankly blocky and then resolving in three visible steps, 8x8 blocks to
4x4 to 2x2 to full detail. On a wall seen from across a room that read is
unmistakable even when the content is not.

**The photograph is generated, not baked.** `build()` draws a 1280x256 room in
numpy -- deep shadow, a sodium lamp, venetian blind bars across the left wall,
a doorway with a figure in it, a chair, and a mirror on the far wall reflecting
a workbench with a soldering iron sitting in its stand. It is deliberately
detailed at several scales, because a source that only has detail at one scale
gives one good zoom and then nothing: the wallpaper stripes are 8 px, the chair
slats 3 px, the reflected iron 48 px end to end with a tip two pixels across.
That iron is the payoff, and the tip is what carries it: at the opening framing
the whole bench is a smudge and the tip is a single warm pixel, indistinguishable
from a highlight on the glass; at the last enhance it is the brightest thing on
the panel and the thing it is attached to is unmistakable. Generating all of it
costs under a tenth of a second and 8 kB of source, against the megabyte a baked
image would add to the repository.

**A zoom is one gather.** The source and its three mosaic levels are stored
flat as (N, 3) uint8; a frame computes 320 column indices and 64 row indices
from the current crop rectangle, forms one (64, 320) index array, and does a
single np.take into the output buffer. There is no resampling, no PIL and no
float in the per-frame path -- which is what keeps this at a tenth of a
millisecond here and inside the Pi's budget, where a per-frame PIL resize of
even this small an image would not be close. Scanlines are free: the source is
stored twice, once dimmed, and odd output rows simply index into the second
copy, so the darkening is baked into the row offsets rather than costing a pass
over the frame.

**The command line is a schedule.** Every keystroke's arrival time is computed
in `build()` and `render()` binary searches it, the same as `defcon` -- ftsched
builds a demo ahead of time and starts it at t=0 and the preview baker steps it
at a fixed rate, so nothing may carry state between frames. The moves are the
same: a table of (t0, t1, crop_from, crop_to) with the width interpolated
logarithmically, because a linear interpolation of width in a 10x zoom spends
most of the move nearly landed and then lurches. The 3x5 font is baked in this
file for the reason every demo here bakes one: the Pi has none of this
desktop's TrueType faces installed, and a font lookup that falls through is a
demo that dies on the wall and nowhere else.

**Rhythm, not one long push-in.** Eight commands: an enhance onto the figure in
the doorway, a slow pan right, a stop, a track back left, a tight enhance onto
the objects on the table, a pull back that re-establishes the room, then the
two-stage move onto the mirror -- WAIT... holds while the frame settles on the
glass, then ENHANCE 15 to 23 goes all the way in and the iron lands. A beat,
GIVE ME A HARD COPY RIGHT THERE, the print flash, and the display resets to the
wide shot for the loop. About 60 s, which wants a `seconds: 70` slot: a cut
before the mirror is a cycle with no ending in it.

Run:  python3 esper.py --host 127.0.0.1
      python3 esper.py --colour amber          # monochrome Esper CRT
      python3 esper.py --cycle 40 --speed 1.2  # the short version
      python3 esper.py --no-commands           # just the photograph moving
"""

import math
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# --------------------------------------------------------------------------
# A 3x5 pixel font, five rows a glyph, each row an octal digit whose three bits
# are its three columns. Same encoding as defcon.py's, and here for the same
# reason: TrueType at five pixels is mush, and the Pi has no faces installed.
# --------------------------------------------------------------------------

_FONT = {
    "0": "75557", "1": "26227", "2": "71747", "3": "71717", "4": "55711",
    "5": "74717", "6": "74757", "7": "71222", "8": "75757", "9": "75717",
    "A": "25755", "B": "65656", "C": "34443", "D": "65556", "E": "74647",
    "F": "74644", "G": "34553", "H": "55755", "I": "72227", "J": "11152",
    "K": "55655", "L": "44447", "M": "57755", "N": "65555", "O": "25552",
    "P": "65644", "Q": "25573", "R": "65655", "S": "34216", "T": "72222",
    "U": "55557", "V": "55552", "W": "55775", "X": "55255", "Y": "55222",
    "Z": "71247", " ": "00000", "-": "00700", ".": "00002", ":": "02020",
    "/": "11244",
}

GLYPH_W, GLYPH_H, PITCH = 3, 5, 4


def _text_mask(s):
    """A (5, 4n-1) bool mask for a string, one pixel between glyphs."""
    s = s.upper()
    out = np.zeros((GLYPH_H, max(1, len(s) * PITCH - 1)), bool)
    for i, ch in enumerate(s):
        rows = _FONT.get(ch, _FONT[" "])
        for r, digit in enumerate(rows):
            bits = int(digit, 8)
            for c in range(GLYPH_W):
                if bits & (4 >> c):
                    out[r, i * PITCH + c] = True
    return out


# --------------------------------------------------------------------------
# The photograph.
#
# Source geometry is 1280x256 -- four times the panel and the same 5:1 shape,
# so a crop of the full frame is a clean 4:1 downsample and the deepest crop is
# a little over 2x magnification. Everything below is in these coordinates.
# --------------------------------------------------------------------------

SRC_W, SRC_H = 1280, 256
FLOOR_Y = 190                           # where the back wall meets the floor
DOOR_X0, DOOR_X1 = 330, 452             # the lit doorway
MIRROR = (890, 45, 1070, 168)           # the mirror's outer frame, x0 y0 x1 y1
IRON_X, IRON_Y = 975, 100               # centre of the reflected bench: the payoff

# Crop rectangles the script moves between, as (centre x, centre y, width).
# Height follows from the panel's aspect, so every one of these is a crop of
# the same shape as the display and nothing is ever stretched.
VIEWS = {
    "WIDE":  (640.0, 128.0, 1280.0),    # the whole photograph
    "DOOR":  (395.0, 100.0, 500.0),     # the figure in the doorway
    "PANR":  (700.0, 100.0, 500.0),     # panned right, mid-room
    "STOP":  (742.0, 100.0, 500.0),     # the overrun the STOP command halts
    "TABLE": (215.0, 112.0, 520.0),     # tracked back left to the lamp
    "PHOTO": (208.0, 146.0, 230.0),     # tight on the objects on the table
    "ROOM":  (700.0, 128.0, 1120.0),    # pulled back, the mirror now in frame
    # Wide enough to hold the whole mirror. A crop that cuts its frame off
    # reads as a dark rectangle rather than as a mirror, and then the last
    # enhance has nothing to have been a step out of.
    "GLASS": (975.0, 108.0, 600.0),     # settled on the mirror
    "IRON":  (975.0, 100.0, 150.0),     # the reflection, all the way in
}

# The spoken commands, their pauses, and the moves each one triggers.
#   (text, {char index: extra seconds after it}, [move, ...])
# and a move is (trigger char index or -1 for end of line, view, seconds to
# travel, seconds to hold afterwards, style). The style picks how blocky the
# picture goes while it travels: a zoom mosaics hard, a pan only softens.
SCRIPT = [
    ("ENHANCE 224 176",          {},       [(-1, "DOOR",  1.5, 1.8, "zoom")]),
    ("PAN RIGHT",                {},       [(-1, "PANR",  3.2, 0.3, "pan")]),
    ("STOP",                     {},       [(-1, "STOP",  0.5, 1.4, "stop")]),
    ("TRACK 45 LEFT",            {},       [(-1, "TABLE", 2.8, 1.0, "pan")]),
    ("ENHANCE 34 TO 36",         {},       [(-1, "PHOTO", 1.5, 2.2, "zoom")]),
    ("PULL BACK",                {},       [(-1, "ROOM",  1.8, 1.4, "pull")]),
    ("WAIT... ENHANCE 15 TO 23", {6: 2.4}, [(6,  "GLASS", 1.6, 0.5, "zoom"),
                                            (-1, "IRON",  1.9, 4.6, "zoom")]),
    ("GIVE ME A HARD COPY RIGHT THERE", {}, [(-1, None,   0.0, 1.3, "print")]),
]

# How hard each style mosaics the picture while it moves. A zoom goes all the
# way to 8x8 blocks because that is the film's look and because it is what
# makes the landing feel like an answer; a pan only goes to 2x2, which reads as
# the motion being slightly soft rather than as a different picture.
BLOCK = {"zoom": 3, "pan": 1, "stop": 1, "pull": 2, "print": 0, "reset": 2}
RESOLVE = 0.85                          # seconds to step the mosaic back out
LEAD_IN = 2.6                           # wide shot before the first command
FLASH = 0.45                            # the hard copy going to the printer
RESET = 1.6                             # snapping back out to the wide shot

CMD_CPS = 11.0                          # keystrokes a second
CMD_JITTER = 0.06
CARET_HZ = 2.4
RETICLE_WALK = 1.1                      # the box hunting before a zoom starts

# Where the readout sits: five rows of glyphs plus a knocked-back plate under
# them, hard against the bottom edge -- the picture is the demo and the text is
# an overlay on it, not a panel beside it. Measured up from the bottom so a
# panel that is not 64 rows still puts the line on the bottom edge rather than
# off the end of the buffer.
TEXT_UP, TEXT_X = 7, 3
PLATE_UP = 10

PALETTES = {
    # Multipliers and a lift per channel, applied to the finished photograph.
    # "sodium" is the film: teal shadows, warm practicals. The other two are a
    # single-phosphor CRT, which is what an Esper terminal would actually be.
    "sodium": None,
    "amber": [(0.00, (0, 0, 0)), (0.35, (70, 30, 0)), (0.70, (210, 120, 20)),
              (0.90, (255, 200, 90)), (1.00, (255, 245, 210))],
    "cold": [(0.00, (0, 0, 0)), (0.35, (0, 26, 48)), (0.70, (30, 130, 190)),
             (0.90, (150, 220, 255)), (1.00, (240, 252, 255))],
}

TEXT_RGB = {"sodium": (255, 196, 110), "amber": (255, 214, 130),
            "cold": (170, 230, 255)}


def add_arguments(ap):
    ap.add_argument("--colour", "--palette", dest="colour", default="sodium",
                    choices=sorted(PALETTES),
                    help="sodium is the film's own palette; amber and cold are "
                         "the single-phosphor terminal it would have been on")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="multiplies the clock; 2 runs the session twice as fast")
    ap.add_argument("--cycle", type=float, default=60.0,
                    help="seconds for one pass. The whole script is scaled to "
                         "fit, so a short cycle is the same session hurried "
                         "rather than one cut off before the iron")
    ap.add_argument("--commands", dest="commands", action="store_true",
                    default=True)
    ap.add_argument("--no-commands", dest="commands", action="store_false",
                    help="hide the typed command line, leaving the photograph")
    ap.add_argument("--reticle", dest="reticle", action="store_true",
                    default=True)
    ap.add_argument("--no-reticle", dest="reticle", action="store_false",
                    help="hide the crop box that walks before each enhance")
    ap.add_argument("--scanlines", dest="scanlines", action="store_true",
                    default=True)
    ap.add_argument("--no-scanlines", dest="scanlines", action="store_false",
                    help="do not darken every other row")
    ap.add_argument("--seed", type=int, default=7,
                    help="fixes the film grain and the keystroke wobble")


# --------------------------------------------------------------------------
# Drawing the room.
#
# All of this runs once. It is written as flat numpy rather than as PIL calls
# because most of it is gradients and masks over the whole 1280x256 field,
# which PIL cannot do at all, and the few rectangles are one slice each.
# --------------------------------------------------------------------------

def _disc(img, cx, cy, rx, ry, colour, feather=0.0):
    """Fill an axis-aligned ellipse, optionally with a soft edge."""
    x0, x1 = max(0, int(cx - rx - 2)), min(SRC_W, int(cx + rx + 3))
    y0, y1 = max(0, int(cy - ry - 2)), min(SRC_H, int(cy + ry + 3))
    if x1 <= x0 or y1 <= y0:
        return
    gx = (np.arange(x0, x1, dtype=f32) - f32(cx)) / f32(max(rx, 0.5))
    gy = (np.arange(y0, y1, dtype=f32) - f32(cy)) / f32(max(ry, 0.5))
    r = np.sqrt(gx[None, :] ** 2 + gy[:, None] ** 2)
    if feather > 0.0:
        a = np.clip((1.0 + feather - r) / feather, 0.0, 1.0)
    else:
        a = (r <= 1.0).astype(f32)
    sub = img[y0:y1, x0:x1]
    sub *= (1.0 - a)[:, :, None]
    sub += a[:, :, None] * np.array(colour, f32)


def _box(img, x0, y0, x1, y1, colour, alpha=1.0):
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(SRC_W, int(x1)), min(SRC_H, int(y1))
    if x1 <= x0 or y1 <= y0:
        return
    sub = img[y0:y1, x0:x1]
    if alpha >= 1.0:
        sub[:] = np.array(colour, f32)
    else:
        sub *= f32(1.0 - alpha)
        sub += f32(alpha) * np.array(colour, f32)


def _stroke(img, x0, y0, x1, y1, w0, w1, colour, alpha=1.0):
    """A line segment of width w0 at one end and w1 at the other, soft-edged.

    The iron is a shallow diagonal, and a diagonal assembled out of axis-aligned
    boxes at this scale is a staircase -- which, magnified two-fold by the last
    enhance, reads as a staircase and not as a barrel. A distance field costs a
    few hundred pixels once and tapers for free, which is what turns the last
    six pixels of the thing into a tip rather than a stub.
    """
    pad = max(w0, w1) * 0.5 + 2.0
    ax0, ax1 = max(0, int(min(x0, x1) - pad)), min(SRC_W, int(max(x0, x1) + pad + 1))
    ay0, ay1 = max(0, int(min(y0, y1) - pad)), min(SRC_H, int(max(y0, y1) + pad + 1))
    if ax1 <= ax0 or ay1 <= ay0:
        return
    gx = np.arange(ax0, ax1, dtype=f32)[None, :] - f32(x0)
    gy = np.arange(ay0, ay1, dtype=f32)[:, None] - f32(y0)
    dx, dy = f32(x1 - x0), f32(y1 - y0)
    s = np.clip((gx * dx + gy * dy) / max(float(dx * dx + dy * dy), 1e-6),
                0.0, 1.0)
    d = np.sqrt((gx - s * dx) ** 2 + (gy - s * dy) ** 2)
    half = (f32(w0) + (f32(w1) - f32(w0)) * s) * f32(0.5)
    a = np.clip(half + 0.5 - d, 0.0, 1.0) * f32(alpha)
    sub = img[ay0:ay1, ax0:ax1]
    sub *= (1.0 - a)[:, :, None]
    sub += a[:, :, None] * np.array(colour, f32)


def _ring(img, cx, cy, rx, ry, w, colour, alpha=1.0):
    """An ellipse outline: the rim of the solder spool's flange."""
    x0, x1 = max(0, int(cx - rx - 2)), min(SRC_W, int(cx + rx + 3))
    y0, y1 = max(0, int(cy - ry - 2)), min(SRC_H, int(cy + ry + 3))
    if x1 <= x0 or y1 <= y0:
        return
    gx = (np.arange(x0, x1, dtype=f32) - f32(cx)) / f32(max(rx, 0.5))
    gy = (np.arange(y0, y1, dtype=f32) - f32(cy)) / f32(max(ry, 0.5))
    r = np.sqrt(gx[None, :] ** 2 + gy[:, None] ** 2)
    d = np.abs(r - 1.0) * f32(min(rx, ry))
    a = np.clip(f32(w) * 0.5 + 0.5 - d, 0.0, 1.0) * f32(alpha)
    sub = img[y0:y1, x0:x1]
    sub *= (1.0 - a)[:, :, None]
    sub += a[:, :, None] * np.array(colour, f32)


def _glow(img, cx, cy, radius, colour, gain):
    """A soft practical light. 1/(1+r^2) rather than a gaussian: no exp over a
    third of a million pixels, and at this size the two are indistinguishable."""
    gx = (np.arange(SRC_W, dtype=f32) - f32(cx)) / f32(radius)
    gy = (np.arange(SRC_H, dtype=f32) - f32(cy)) / f32(radius)
    r2 = gx[None, :] ** 2 + gy[:, None] ** 2
    img += (f32(gain) / (1.0 + r2 * 2.2))[:, :, None] * np.array(colour, f32)


def _scene(rng):
    """The photograph: a room interior, 1280x256 float RGB in 0..255.

    Detail is put in at three scales on purpose. Something has to survive the
    4:1 downsample of the wide shot (the doorway, the lamp, the mirror's
    rectangle), something has to appear at the middle framings (the chair
    slats, the blind bars, the objects on the table), and something has to be
    invisible until the last enhance (the iron in the mirror). A scene that is
    interesting at only one of those gives one good move and seven dull ones.
    """
    img = np.zeros((SRC_H, SRC_W, 3), f32)
    xs = np.arange(SRC_W, dtype=f32)[None, :]
    ys = np.arange(SRC_H, dtype=f32)[:, None]

    # ---- back wall: darker at the top, where no practical reaches ----------
    wall_v = 0.45 + 0.55 * (ys[:FLOOR_Y] / f32(FLOOR_Y))
    img[:FLOOR_Y] = wall_v[:, :, None] * np.array((17.0, 20.0, 26.0), f32)
    # Fine paper stripes at 8 px and panel seams at 64. The stripes are the
    # thing that tells the eye a mid-zoom actually resolved something.
    img[:FLOOR_Y, 4::8] *= 1.16
    img[:FLOOR_Y, ::64] *= 0.66
    img[:FLOOR_Y, 1::64] *= 1.20
    # Dado rail and the panelling below it.
    img[150:FLOOR_Y] *= 1.30
    img[148:151] *= 1.9
    img[FLOOR_Y - 3:FLOOR_Y] *= 0.4

    # ---- floor: boards running away from the camera -------------------------
    fl = img[FLOOR_Y:]
    depth = np.arange(SRC_H - FLOOR_Y, dtype=f32)[:, None] / f32(SRC_H - FLOOR_Y)
    fl[:] = (0.55 + 0.75 * depth)[:, :, None] * np.array((26.0, 20.0, 15.0), f32)
    fl[:, ::27] *= 0.55                 # board seams
    fl[3::14] *= 1.12                   # the grain across them

    # ---- venetian blinds across the left wall ------------------------------
    # The single most recognisable texture in the film, and it happens to be
    # exactly what a mid-scale zoom needs: bars 30 px apart hold up wide and
    # still have an edge to resolve at 2x.
    bars = ((ys + xs * 0.42) % 30.0) < 12.0
    reach = np.clip((f32(430.0) - xs) / 260.0, 0.0, 1.0) * \
        np.clip((f32(FLOOR_Y + 40) - ys) / 90.0, 0.0, 1.0)
    img += (bars * reach)[:, :, None] * np.array((34.0, 22.0, 9.0), f32)

    # ---- the doorway, and the figure standing in it ------------------------
    _box(img, DOOR_X0 - 7, 22, DOOR_X1 + 7, FLOOR_Y, (34, 30, 28))
    _box(img, DOOR_X0, 30, DOOR_X1, FLOOR_Y, (96, 74, 44))
    # A gradient down the opening: light spills from somewhere above and
    # behind, so the top of the doorway is the brightest thing in the picture.
    door = img[30:FLOOR_Y, DOOR_X0:DOOR_X1]
    door *= np.clip(1.9 - 1.2 * np.arange(FLOOR_Y - 30, dtype=f32)
                    / f32(FLOOR_Y - 30), 0.4, 2.0)[:, None, None]
    _box(img, DOOR_X0, 30, DOOR_X0 + 3, FLOOR_Y, (150, 118, 70))
    # The light the doorway throws onto the floor: a widening trapezoid, which
    # is what stops the floor being a flat slab in the wide shot.
    spill_w = 62.0 + (ys[FLOOR_Y:] - f32(FLOOR_Y)) * 1.5
    spill = np.clip(1.0 - np.abs(xs - 391.0) / spill_w, 0.0, 1.0) ** 1.6
    img[FLOOR_Y:] += spill[:, :, None] * np.array((46.0, 33.0, 16.0), f32)

    # The figure: a silhouette, because a figure lit from behind in a doorway
    # is a silhouette, and because a silhouette survives a 4:1 downsample when
    # a modelled figure would turn to mud.
    _disc(img, 391, 66, 13, 15, (14, 13, 17))               # head
    _box(img, 366, 80, 416, 96, (16, 15, 19))               # shoulders
    _box(img, 371, 92, 412, FLOOR_Y + 2, (15, 14, 18))      # body
    _disc(img, 391, 84, 26, 10, (16, 15, 19), feather=0.35)
    # A rim of doorlight down one side, which is what makes it read as standing
    # in the opening rather than as a hole cut in it.
    _box(img, 412, 82, 415, 170, (120, 92, 54), alpha=0.55)

    # ---- the table and the lamp on it --------------------------------------
    _box(img, 96, 150, 300, 158, (44, 34, 25))              # table top
    _box(img, 104, 158, 292, 188, (24, 18, 13))             # its shadow side
    _box(img, 112, 158, 118, 188, (36, 27, 19))             # legs
    _box(img, 278, 158, 284, 188, (36, 27, 19))
    _box(img, 148, 96, 156, 150, (52, 40, 26))              # lamp stem
    # Shade: a trapezoid built as four stacked slices, wide at the bottom.
    for i in range(9):
        half = 22 + i * 3
        _box(img, 152 - half, 62 + i * 4, 152 + half, 66 + i * 4,
             (150 + i * 9, 108 + i * 7, 52 + i * 4))
    _glow(img, 152, 100, 210, (46, 32, 14), 1.5)
    _glow(img, 152, 96, 46, (90, 62, 26), 1.1)

    # Objects on the table -- the target of the tight enhance, and the one
    # place in the picture that has to survive a 1.4x magnification and still
    # be a *thing*. A photograph propped up with a figure of its own on it,
    # which is the joke the film is making: a photograph inside a photograph.
    _box(img, 168, 118, 250, 152, (126, 106, 82))           # print, its border
    _box(img, 172, 122, 246, 148, (46, 40, 36))             # its image area
    _box(img, 176, 124, 200, 146, (78, 66, 52), alpha=0.8)  # a lit wall in it
    _disc(img, 214, 130, 6, 7, (128, 104, 82))              # its figure: head
    _disc(img, 214, 128, 6, 4, (36, 28, 24))                # hair
    _box(img, 205, 139, 224, 148, (58, 50, 46))             # shoulders
    _box(img, 210, 130, 212, 131, (24, 20, 20))             # eyes, 2 px each
    _box(img, 216, 130, 218, 131, (24, 20, 20))
    _box(img, 168, 148, 250, 152, (74, 62, 48))             # the print's edge
    _box(img, 262, 126, 278, 152, (76, 72, 66), alpha=0.55)  # a glass
    _box(img, 262, 140, 278, 152, (146, 118, 68), alpha=0.7)
    _box(img, 262, 126, 265, 152, (150, 132, 104), alpha=0.5)

    # ---- a chair in the middle of the room ---------------------------------
    _box(img, 566, 96, 574, 158, (40, 33, 27))              # back posts
    _box(img, 664, 96, 672, 158, (40, 33, 27))
    for sy in range(102, 146, 11):                          # slats, 3 px
        _box(img, 570, sy, 668, sy + 3, (48, 39, 30))
    _box(img, 556, 150, 682, 158, (52, 42, 32))             # seat
    _box(img, 566, 158, 574, 194, (30, 24, 19))             # front legs
    _box(img, 664, 158, 672, 194, (30, 24, 19))
    _box(img, 556, 190, 690, 198, (12, 9, 7), alpha=0.6)    # its shadow

    # A framed print on the wall behind the chair. The pan between the doorway
    # and the mirror crosses a lot of bare wall, and a pan over nothing reads
    # as the demo having stalled rather than as the machine travelling.
    _box(img, 694, 62, 750, 114, (62, 50, 32))
    _box(img, 698, 66, 746, 110, (30, 34, 40))
    _box(img, 702, 78, 742, 106, (44, 40, 34))
    _box(img, 702, 70, 742, 78, (58, 50, 38))

    # ---- a pillar, to break the far wall up --------------------------------
    _box(img, 760, 0, 812, FLOOR_Y, (23, 25, 30))
    _box(img, 760, 0, 766, FLOOR_Y, (44, 42, 44))
    _box(img, 806, 0, 812, FLOOR_Y, (11, 12, 15))

    # ---- the mirror, and the workbench reflected in it ----------------------
    mx0, my0, mx1, my1 = MIRROR
    _box(img, mx0, my0, mx1, my1, (86, 68, 40))             # frame
    _box(img, mx0 + 3, my0 + 3, mx1 - 3, my1 - 3, (128, 100, 58))
    _box(img, mx0 + 7, my0 + 7, mx1 - 7, my1 - 7, (40, 32, 20))
    _box(img, mx0 + 10, my0 + 10, mx1 - 10, my1 - 10, (20, 25, 30))
    glass = img[my0 + 10:my1 - 10, mx0 + 10:mx1 - 10]
    # A diagonal sheen across the glass, so the mirror reads as a reflective
    # surface at the wide framing where none of its contents are legible.
    gh, gw = glass.shape[:2]
    sheen = ((np.arange(gw, dtype=f32)[None, :] * 0.5
              + np.arange(gh, dtype=f32)[:, None]) % 74.0) / 74.0
    glass += (0.35 + 0.65 * sheen)[:, :, None] * np.array((9.0, 12.0, 15.0), f32)
    # What is reflected: the doorway across the room, dim and reversed...
    _box(img, mx0 + 18, my0 + 16, mx0 + 46, my1 - 22, (58, 46, 26), alpha=0.85)
    # ...and a workbench, with a soldering iron in its cradle. The final
    # crop is 150x30 of source, so everything here is laid out inside x 900-1050
    # and y 86-114; anything outside that is furniture for the framings on the
    # way in and nothing more.
    #
    # At this size silhouette is the whole game: 48 px of iron is twelve pixels
    # in the wide shot, and no amount of modelling survives that. What has to
    # read is the outline -- fat handle, a step down at the ferrule, a thin
    # barrel, a tapered tip -- plus the one thing that is legible at every
    # framing, the hot point, which is why it is the only saturated thing here.
    tip_x, tip_y = IRON_X - 11.0, IRON_Y + 4.0       # 964, 104
    end_x, end_y = IRON_X + 37.0, IRON_Y - 12.0      # 1012, 88, butt of the grip
    _box(img, mx0 + 10, 111, mx1 - 10, 124, (40, 30, 20))     # the bench
    _box(img, mx0 + 10, 111, mx1 - 10, 113, (62, 47, 29))     # its lit front edge

    # A spool of solder and a small board, so the bench is a bench and not a
    # dark shelf with one object floating over it.
    _disc(img, 936, 103, 8, 8, (50, 44, 38))
    _ring(img, 936, 103, 7.5, 7.5, 1.6, (80, 71, 54))       # the flange rim
    for wind in range(-4, 5, 2):                            # wound solder
        _stroke(img, 930, 103 + wind, 942, 103 + wind, 1.0, 1.0,
                (104, 102, 98), alpha=0.55)
    _disc(img, 936, 103, 2.4, 2.4, (26, 22, 20))            # the hub
    _stroke(img, 943, 105, 954, 110, 1.2, 1.2, (108, 106, 102), alpha=0.7)
    _box(img, 1014, 105, 1044, 111, (24, 44, 38))           # the board
    _box(img, 1014, 105, 1044, 106, (46, 78, 62))
    for pad in (1019, 1025, 1031, 1038):                    # its pads
        _box(img, pad, 107, pad + 1, 108, (176, 152, 88))
    _box(img, 1022, 102, 1028, 105, (18, 17, 19))           # a component or two
    _box(img, 1034, 103, 1039, 105, (18, 17, 19))

    # The cradle: a plate on the bench and a V standing on it for the barrel to
    # lie in. A coiled-wire stand would be truer to the object, but a helix at
    # five pixels a turn is not a helix -- it is texture on the barrel. Two
    # straight arms meeting at a point is about the simplest shape that still
    # says holder, and a point is what survives the downsample.
    #
    # The arms are far steeper than the barrel they hold: an arm anywhere near
    # the barrel's own slope lies along it and vanishes into it. They meet on
    # the plate rather than on a post, because a post plus an arm is a straight
    # line with a kink in it, which at this size is just a leaning stick.
    _box(img, 962, 107, 998, 112, (42, 38, 36))
    _box(img, 962, 107, 998, 108, (66, 62, 58))
    # The far arm goes in before the barrel, so the metal buries the middle of
    # it and only the foot and the tip standing clear of the iron are seen.
    _stroke(img, 975.0, 106.4, 969.8, 99.0, 2.3, 1.4, (88, 82, 74))

    # Values are set against the wide shot, not against this close-up. The
    # barrel wants to be the brightest thing here and must not be: at 4:1 it
    # would be a pale streak lying in the mirror, an object plainly visible from
    # the first frame, and then the last enhance reveals something the audience
    # has been looking at for a minute. Dim metal, a lifted grip so the fat end
    # still has a silhouette at this zoom, and one saturated point.
    _stroke(img, end_x, end_y, 991, 95, 10.5, 8.4, (68, 57, 54))         # grip
    _stroke(img, end_x - 1, end_y - 3.2, 991, 92.4, 1.5, 1.3,
            (100, 84, 66), alpha=0.8)                       # light along its top
    _stroke(img, 991, 95, 983, 98, 7.0, 5.6, (88, 77, 61))               # ferrule
    _stroke(img, 983, 98, 968, 103, 4.4, 2.2, (78, 72, 63))              # barrel
    # The near arm goes in after the barrel and crosses it, which is the whole
    # reason the iron reads as sitting *in* the cradle rather than beside it.
    # It is the brighter of the two, but still well under the tip: the cradle is
    # supporting cast, and anything that out-weighs the hot point steals it.
    _stroke(img, 975.0, 106.4, 981.0, 94.6, 2.3, 1.4, (104, 97, 87))
    # The tip. Three lengths of the same taper, each hotter and shorter than the
    # last, so the heat looks like it is coming out of the iron rather than
    # being painted on the end of it. It is short on purpose: a long hot section
    # is a poker, and the last eight pixels are what say soldering iron.
    _stroke(img, 972, 101.3, tip_x, tip_y, 2.6, 1.3, (188, 78, 14))
    _stroke(img, 969.5, 102.1, tip_x, tip_y, 2.0, 1.1, (255, 156, 38))
    _stroke(img, 966.5, 103.2, tip_x, tip_y, 1.3, 0.9, (255, 234, 182))

    # The lead, trailing off the butt of the grip and out of frame. It is the
    # cheapest cue in the whole picture: a tapered glowing thing on a bench is a
    # wand, and a tapered glowing thing on a bench with a cord coming out of the
    # back of it is a tool that is plugged in.
    cord = [(1013.0, 89.0), (1024.0, 86.5), (1036.0, 89.5), (1047.0, 96.0),
            (1058.0, 106.0)]
    for i in range(1, len(cord)):
        _stroke(img, cord[i - 1][0], cord[i - 1][1], cord[i][0], cord[i][1],
                1.7, 1.7, (64, 56, 54))

    # The smoke. It is the one vertical in a composition of diagonals, it gives
    # the eye something to follow on the way down the last zoom, and it is the
    # reason the glowing point reads as work happening rather than as a lamp.
    px, py = tip_x + 0.5, tip_y - 1.5
    for i in range(1, 11):
        u = i / 10.0
        qx = tip_x + math.sin(u * 3.4) * 2.4 + u * u * 3.6
        qy = tip_y - 1.5 - u * 16.0
        _stroke(img, px, py, qx, qy, 0.9 + u * 1.7, 1.1 + u * 2.0,
                (100, 100, 106), alpha=0.52 * (1.0 - u) + 0.05)
        px, py = qx, qy

    _glow(img, IRON_X, IRON_Y, 120, (10, 8, 6), 0.7)
    _glow(img, tip_x, tip_y, 30, (40, 19, 5), 1.3)

    # ---- grade -------------------------------------------------------------
    # Grain first, so it is part of the photograph and gets mosaicked with it
    # rather than crawling over the top -- and so it costs nothing per frame.
    img += rng.standard_normal((SRC_H, SRC_W, 1)).astype(f32) * f32(3.2)
    # A vignette, in the photograph rather than on the display: this is a
    # picture being examined, and a print's corners fall off.
    vx = (np.arange(SRC_W, dtype=f32) / f32(SRC_W) - 0.5)
    vy = (np.arange(SRC_H, dtype=f32) / f32(SRC_H) - 0.5)
    img *= np.clip(1.06 - (vx[None, :] ** 2 * 1.15
                           + vy[:, None] ** 2 * 0.5), 0.25, 1.0)[:, :, None]
    return np.clip(img, 0.0, 255.0)


def _mosaic(flat_img, k):
    """Block-average by k and blow it back up, nearest. This is the enhance."""
    if k <= 1:
        return flat_img
    h, w = SRC_H // k, SRC_W // k
    small = flat_img.reshape(h, k, w, k, 3).mean(axis=(1, 3))
    return np.repeat(np.repeat(small, k, axis=0), k, axis=1)


# --------------------------------------------------------------------------
# Build.
# --------------------------------------------------------------------------

def _smooth(s):
    return s * s * (3.0 - 2.0 * s)


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    aspect = float(H) / float(W)

    # ---- the photograph and its three enhancement levels -------------------
    photo = _scene(rng)
    ramp = PALETTES[args.colour]
    if ramp is not None:
        # A single-phosphor terminal: collapse to luma and put it back through
        # the ramp. Done here rather than per frame for the obvious reason.
        lum = (photo[:, :, 0] * 0.30 + photo[:, :, 1] * 0.59
               + photo[:, :, 2] * 0.11)
        # Lifted before the ramp. The photograph's mean is down around 35 of
        # 255 -- it is a night interior -- and mapping that straight onto a
        # ramp whose first stop is black puts the whole picture in the ramp's
        # bottom eighth, which on the wall is a black panel with a lamp on it.
        lum = 255.0 * np.clip(lum / 255.0, 0.0, 1.0) ** f32(0.62)
        lut = ds.gradient(ramp, 256, dtype=np.uint8)
        photo = lut[lum.astype(np.uint8)].astype(f32)

    # Each level is stored twice, the second copy dimmed: an odd output row
    # indexes into the second half and the scanline costs nothing at run time.
    # Without --scanlines the row offsets are all zero and the second half is
    # simply never touched, which is not worth a branch to avoid.
    dim = f32(0.62) if args.scanlines else f32(1.0)
    banks = []
    for level in range(4):
        m = _mosaic(photo, 1 << level)
        pair = np.concatenate([m.reshape(-1, 3),
                               (m * dim).reshape(-1, 3)], axis=0)
        banks.append(np.clip(pair, 0, 255).astype(np.uint8))
    half = SRC_W * SRC_H
    bank_col = (((np.arange(H) & 1) * half) if args.scanlines
                else np.zeros(H)).astype(np.int32)[:, None]

    # ---- the session: keystrokes and moves, both baked ---------------------
    #
    # render() has to be a pure function of t, so nothing here is decided per
    # frame. `moves` is scanned with searchsorted on its start times and the
    # crop is interpolated closed-form; `keys` the same for the command line.
    beat = 1.0 / CMD_CPS
    lines = []          # (t_start, keystroke times, mask, image)
    moves = []          # (t0, t1, from view, to view, style)
    boxes = []          # (t_appear, t_lock, target view) -- the reticle walk
    clock = LEAD_IN
    here = VIEWS["WIDE"]
    flash_at = None
    text_off = 1e9

    for text, pauses, steps in SCRIPT:
        mask = _text_mask(text)
        stamps = np.empty(len(text) + 1, np.float64)
        when = clock
        for i in range(len(text)):
            stamps[i] = when
            when += beat * (1.0 + CMD_JITTER * (2.0 * rng.random() - 1.0))
            when += pauses.get(i, 0.0)
        stamps[len(text)] = when
        lines.append((clock, stamps, mask))

        for trigger, view, travel, hold, style in steps:
            fires = stamps[trigger if trigger >= 0 else len(text)]
            start = max(fires + 0.25, clock)
            if style == "print":
                # No move: the machine prints what is on the screen. The flash
                # is the exposure, and the display resets afterwards.
                flash_at = start
                clock = start + FLASH + hold
                moves.append((clock, clock + RESET, here, VIEWS["WIDE"],
                              "reset"))
                here = VIEWS["WIDE"]
                clock += RESET + hold
                # The readout blanks once the display is back at the wide
                # shot. Without this the last line is still sitting there when
                # the cycle wraps and vanishes on the seam, which is the one
                # visible cut in an otherwise continuous loop.
                text_off = clock
                continue
            if args.reticle and style == "zoom":
                # The box is kept on screen for the whole dive, not just the
                # hunt: it is drawn from the target's source rectangle, so as
                # the crop closes the box grows out to the frame edges and the
                # two arrive together. Dropping it at the start of the move
                # instead loses the one thing that ties the command to the
                # picture.
                boxes.append((start, start + RETICLE_WALK,
                              start + RETICLE_WALK + travel, VIEWS[view]))
                start += RETICLE_WALK
            moves.append((start, start + travel, here, VIEWS[view], style))
            here = VIEWS[view]
            clock = start + travel + hold
        # The line stays on screen until the next one starts typing, the way a
        # terminal leaves the last thing you said sitting there.
    total = clock + 1.4

    # Scale the whole session to the requested cycle. Everything is a time, so
    # this is one multiply and the rhythm is preserved exactly: --cycle 40 is
    # the same session at a hurry, not one with the ending sheared off.
    # Named `cycle` because the preview tooling reads this name out of the
    # closure to know how long a whole pass is.
    cycle = max(6.0, float(args.cycle))
    k = cycle / total
    lines = [(t0 * k, ts * k, m) for t0, ts, m in lines]
    moves = [(a * k, b * k, u, v, s) for a, b, u, v, s in moves]
    boxes = [(a * k, b * k, c * k, v) for a, b, c, v in boxes]
    if flash_at is not None:
        flash_at *= k
    text_off *= k
    flash_len = FLASH * k
    resolve = RESOLVE * k
    caret_hz = CARET_HZ / k

    move_t0 = np.array([m[0] for m in moves], np.float64)
    line_t0 = np.array([r[0] for r in lines], np.float64)

    # Command text, coloured once. A frame copies a prefix of it through its
    # own mask; re-rasterising the partial string every frame would cost a
    # layout per frame and buy nothing, since a fixed pitch never reflows.
    text_rgb = np.array(TEXT_RGB[args.colour], np.uint8)
    line_img = []
    for _t0, _ts, mask in lines:
        img = np.zeros(mask.shape + (3,), np.uint8)
        img[mask] = text_rgb
        line_img.append(img)
    caret = np.zeros((GLYPH_H, GLYPH_W, 3), np.uint8)
    caret[:] = text_rgb
    ret_rgb = np.array(TEXT_RGB[args.colour], np.uint8)
    ret_dim = (ret_rgb * 0.45).astype(np.uint8)

    # ---- per-frame buffers -------------------------------------------------
    out = np.zeros((H, W, 3), np.uint8)
    idx = np.zeros((H, W), np.int32)
    # Kept apart from idx: building the index map in place would have the
    # broadcast read a column of the array it is writing.
    row_i = np.zeros((H, 1), np.int32)
    col_i = np.zeros((1, W), np.int32)
    col_u = (np.arange(W, dtype=np.float64) + 0.5) / float(W)
    row_u = (np.arange(H, dtype=np.float64) + 0.5) / float(H)
    plate_lut = (np.arange(256) * 0.30).astype(np.uint8)
    text_y = max(0, H - TEXT_UP)
    plate_y = max(0, H - PLATE_UP)
    flash_lut = np.clip(64 + np.arange(256) * 1.4, 0, 255).astype(np.uint8)

    def crop_at(p):
        """The crop rectangle and the mosaic level at time p. Closed form."""
        i = int(np.searchsorted(move_t0, p, side="right")) - 1
        if i < 0:
            return VIEWS["WIDE"], 0
        t0, t1, src, dst, style = moves[i]
        depth = BLOCK[style]
        if p >= t1:
            # Landed. Step the mosaic back out in three visible passes, which
            # is the enhancement itself and the reason a zoom feels answered
            # rather than merely finished.
            u = (p - t1) / max(resolve, 1e-6)
            level = 0 if u >= 1.0 else int(math.ceil(depth * (1.0 - u)))
            return dst, level
        s = (p - t0) / max(t1 - t0, 1e-6)
        e = _smooth(s)
        # Width interpolates logarithmically: a 10x zoom lerped linearly spends
        # four fifths of the move already nearly landed and then lurches at the
        # end, which no camera and no machine has ever done.
        w = src[2] * math.exp(e * math.log(dst[2] / src[2]))
        view = (src[0] + (dst[0] - src[0]) * e,
                src[1] + (dst[1] - src[1]) * e, w)
        return view, (int(math.ceil(depth * min(1.0, s / 0.18)))
                      if s < 0.18 else depth)

    def to_view(view, sx, sy):
        """Source coordinates to panel coordinates under the current crop."""
        vw = view[2]
        return ((sx - (view[0] - vw * 0.5)) / vw * W,
                (sy - (view[1] - vw * aspect * 0.5)) / (vw * aspect) * H)

    def frame_rect(x0, y0, x1, y1, colour, bright):
        """A reticle: a thin box with brighter corner brackets, clipped."""
        x0, x1 = int(round(x0)), int(round(x1))
        y0, y1 = int(round(y0)), int(round(y1))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return
        for yy in (y0, y1):
            if 0 <= yy < H:
                a, b = max(0, x0), min(W, x1 + 1)
                if b > a:
                    out[yy, a:b] = colour
        for xx in (x0, x1):
            if 0 <= xx < W:
                a, b = max(0, y0), min(H, y1 + 1)
                if b > a:
                    out[a:b, xx] = colour
        arm_x = max(2, min(9, (x1 - x0) // 4))
        arm_y = max(2, min(7, (y1 - y0) // 4))
        for xx, dx in ((x0, arm_x), (x1 - arm_x + 1, arm_x)):
            for yy in (y0, y1):
                if 0 <= yy < H:
                    a, b = max(0, xx), min(W, xx + dx)
                    if b > a:
                        out[yy, a:b] = bright
        for yy, dy in ((y0, arm_y), (y1 - arm_y + 1, arm_y)):
            for xx in (x0, x1):
                if 0 <= xx < W:
                    a, b = max(0, yy), min(H, yy + dy)
                    if b > a:
                        out[a:b, xx] = bright

    def render(t, frame):
        p = (t * args.speed) % cycle

        view, level = crop_at(p)
        vw = view[2]
        vh = vw * aspect
        # One gather. The column and row index vectors are 320 and 64 numbers;
        # everything else the frame does is an overlay a few hundred pixels
        # wide, so the cost of a frame is essentially the cost of copying it.
        sx = np.clip(view[0] - vw * 0.5 + col_u * vw, 0, SRC_W - 1)
        sy = np.clip(view[1] - vh * 0.5 + row_u * vh, 0, SRC_H - 1)
        col_i[0] = sx
        row_i[:, 0] = sy
        # np.multiply/np.add with out=, not `*=`: an augmented assignment to a
        # closure name inside render() makes it a local and the frame dies.
        np.multiply(row_i, SRC_W, out=row_i)
        np.add(row_i, bank_col, out=row_i)
        np.add(row_i, col_i, out=idx)
        np.take(banks[level], idx, axis=0, out=out)

        # The reticle, while it hunts and then while the zoom swallows it. It
        # is drawn in view coordinates from the target's *source* rectangle, so
        # as the crop closes on the box the box grows out to the frame edges on
        # its own and the two arrive together.
        if args.reticle:
            for b0, b1, b2, target in boxes:
                if not (b0 <= p < b2):
                    continue
                s = min(1.0, (p - b0) / max(b1 - b0, 1e-6))
                e = _smooth(s)
                # It walks in from off to one side rather than materialising in
                # place: a box that simply appears reads as a UI element, one
                # that travels reads as a machine searching.
                ox = (1.0 - e) * vw * 0.34
                oy = (1.0 - e) * vh * 0.22
                tw, th = target[2] * 0.5, target[2] * aspect * 0.5
                # Blinking while it hunts, then knocked back to the dim colour
                # once the dive starts, so the picture wins as it resolves.
                dimmed = ((p * 9.0) % 1.0 < 0.35) if s < 1.0 else True
                x0, y0 = to_view(view, target[0] - tw + ox, target[1] - th + oy)
                x1, y1 = to_view(view, target[0] + tw + ox, target[1] + th + oy)
                frame_rect(x0, y0, x1, y1,
                           ret_dim if dimmed else ret_rgb,
                           ret_dim if dimmed else (255, 255, 255))

        if flash_at is not None and flash_at <= p < flash_at + flash_len:
            # The exposure: the whole panel lifts, then falls back. Cheap
            # because it happens for a dozen frames a minute.
            if (p - flash_at) < flash_len * 0.55:
                np.take(flash_lut, out, out=out)

        if args.commands:
            j = -1 if p >= text_off else \
                int(np.searchsorted(line_t0, p, side="right")) - 1
            plate = out[plate_y:]
            np.take(plate_lut, plate, out=plate)
            if j >= 0:
                _t0, stamps, mask = lines[j]
                n = int(np.searchsorted(stamps, p, side="right"))
                n = min(n, mask.shape[1] // PITCH + 1)
                if n > 0:
                    px = min(W - TEXT_X, n * PITCH - 1)
                    sub = out[text_y:text_y + GLYPH_H, TEXT_X:TEXT_X + px]
                    np.copyto(sub, line_img[j][:, :px],
                              where=mask[:, :px, None])
                # Solid while it writes, blinking once the line is finished --
                # a caret that blinks through the typing reads as a fault.
                done = p >= stamps[-1]
                if not done or (p * caret_hz) % 1.0 < 0.55:
                    cxp = min(TEXT_X + n * PITCH, W - GLYPH_W)
                    out[text_y:text_y + GLYPH_H, cxp:cxp + GLYPH_W] = caret

        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
