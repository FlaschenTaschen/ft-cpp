#!/usr/bin/env python3
"""The big board from WarGames: a vector world map playing out an exchange.

NORAD's display in the 1983 film is a wall of thin glowing coastline with
missile tracks arcing across it, warheads blooming as expanding rings over
their targets, and a DEFCON readout stepping down while it all goes wrong.
That is almost exactly the shape of this panel — 320x64 is a letterbox, and a
letterbox is what a wall map wants to be — so this demo is the map and the
simulation. (`wopr` next door is the machine and the dialogue; this one has no
conversation in it.)

**The map is real geography, baked into the file.** The coastline is Natural
Earth 1:110m "Coastline" (public domain — Natural Earth data is released free
of copyright, see naturalearthdata.com/about/terms-of-use), simplified offline
with Douglas-Peucker and encoded as the COAST string below: 81 polylines, 897
points, about 2 kB. The demo therefore reads no data file and needs no network
at runtime, which matters because the Pi driving this wall boots into the
rotation with no guarantee that anything else is mounted or reachable.

The projection is forced by the panel. 320 square pixels across 360 degrees of
longitude is 1.125 degrees a pixel, so 64 rows buy exactly 72 degrees of
latitude and nothing else. The band is 8N to 80N — which is not a compromise
so much as a gift, because that band *is* where the film's war happens: North
America, the Atlantic, Europe, Russia, China, Japan. Everything south of the
Sahel is off the bottom of the board and nobody misses it.

The map never moves, so it is rasterised once in `build()` — supersampled 2x
and box-filtered down, which is the whole reason the coast reads as a line
rather than as a rash of single lit pixels; a 1x Bresenham line on a 320-wide
panel is either aliased into dashes or, when thickened, into a blob. Per frame
this demo composites, it does not draw the map.

Everything after that is a scalar field. One float32 (H, W) accumulator gets
the map, then the tracks, then the blooms, then the readouts, and is mapped
through a 256-entry palette at the end with np.take(..., out=) — so the frame
costs about five whole-array passes and no allocation. The tracks themselves
are *not* drawn per frame: each trajectory's pixel path is baked in `build()`
as a flat index array, and a frame is a slice of that array scattered into the
accumulator with a brightness ramp along it. A live track is six numpy calls
over fifty-odd elements. Spent tracks — the lines that stay on the board
once a warhead has landed, which is most of what makes the finale look like
the film — are drawn in eight pre-concatenated groups rather than one call per
track, because by the end of a cycle there are over a hundred of them and a
hundred Python-level scatters is the difference between fitting the Pi's frame budget
and not.

Blooms are the one thing that could not be baked, since their radius is a
function of time. They are a squared linear falloff on a precomputed radius
window rather than a Gaussian: `exp` over ten 33x33 windows a frame is real
money on an ARMv7, and at this size nobody can tell.

**The cycle is one continuous acceleration and then the film's ending.** The
launch rate is not stepped: it ramps geometrically from about one launch every
five seconds to better than six a second, so the board goes from a single
trajectory you can follow with your eye to a mesh you cannot, without ever
crossing a line where something visibly changed gear. Flight times shorten
along the same curve, which compounds it — more in the air, arriving sooner —
and the DEFCON readout is derived from the schedule rather than from a clock
of its own: the level drops when the cumulative number of launches crosses 4,
12, 30 and 58 per cent of the cycle's total, so the countdown *is* the rate
rather than something running alongside it. It opens with tracks already in
the air (ftsched crossfades into an effect, so a demo that starts empty spends
its first seconds looking broken).

Then four phases, as fractions of `--cycle` rather than fixed tails, so that
`--cycle 30` is a shorter version of the same film and not a war with the
ending cut off:

  * 0.00-0.70  the exchange, accelerating the whole way; the last launch goes
               up about one flight time before the end, so impacts keep
               arriving to the last moment
  * 0.70-0.74  the board whites out on the final wave and goes black. The
               darkness is the point: a beat of nothing is what makes the
               next thing land
  * 0.74-0.96  THE ONLY WINNING MOVE IS NOT TO PLAY, typed out a character at
               a time and then held long enough to read
  * 0.96-1.00  the map fades back up at DEFCON 5, drawing the tracks that are
               already in the air at t=0 so the loop does not seam

**The closing line types itself.** In the film the machine prints its side of
the conversation to a terminal, so the line arrives as WOPR writing it rather
than as a caption being switched on: characters at about seven and a half a
second, a block caret at the write position, a beat at the line break where a
terminal would return the carriage, and a longer one before the last word. The
interval is a machine's — one rate, wobbled by five per cent, because
dead-constant timing at this size reads as a progress bar filling and anything
more uneven reads as a person at the keyboard, which is the wrong character.
The caret is solid while it writes and blinks only once the line is finished; a
caret that blinks *through* the typing looks like a fault. Every keystroke's
arrival time is drawn in `build()` and the frame binary-searches it, for the
same reason the war's schedule is baked: `render()` must be a pure function of
`t`.

Typing costs time that used to be reading time, so the message phase took four
points of the cycle off the war — the cheapest place there is to take them
from, since the exchange is one continuous ramp parameterised by its own
progress and a slightly shorter one is the same escalation very slightly
steeper. The whole performance is then capped at 55 per cent of the phase, so
better than half of it is always the finished line sitting still. A long
`--message`, or `--cycle 30`, types faster rather than running past the end of
the phase: being cut off mid-word is the one failure the ending cannot have.

`--message ""` turns the closing line off, and then the darkness shrinks to a
punctuation mark and the exchange takes the time back. Only the second half of
the film's line is here — "A STRANGE GAME." is the setup, and the setup is the
part the wall cannot spare pixels for.

A full pass is 80 s by default, so the rotation slot wants `seconds: 90`: a
slot that ends in the middle of the crescendo is worse than one that never
started.

Costs 0.1 ms/frame over the quiet stretches here and 0.35 ms at p95 through
the crescendo, with a build of 0.03 s. Against the calibration demos that
scales to something like 25-30 ms on the wall's Pi 3 — the whole-frame passes
scale with memory bandwidth, the per-track loops with interpreter speed, and the two do
not scale alike, which is why the number of live tracks and blooms a frame
will draw is capped rather than left to grow with the schedule, and why the
spent-track groups are deduplicated at build time: the board has only 20480
pixels, so a hundred and twenty overlapping trajectories are far fewer indices
than the sum of their lengths. The whole-board flash that the crescendo is built out of is
baked as one float per 1/60 s and costs a scalar add. 30 fps: the heads move
several pixels a frame and 24 makes them stutter.

Run:  python3 defcon.py --host 127.0.0.1
      python3 defcon.py --colour amber --arcs 10
      python3 defcon.py --cycle 30 --speed 1.5      # hurry the war along
      python3 defcon.py --message ""                # no closing line
      python3 defcon.py --no-defcon --no-labels     # just the map and tracks
"""

import math
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# --------------------------------------------------------------------------
# The coastline.
#
# Natural Earth 1:110m coastlines, clipped to 8N..80N, simplified to about a
# point a degree and quantised to a 640x128 grid (the 2x supersample the map is
# rasterised at). Encoding: polylines separated by a space; four base-64 digits
# for the first point (x high, x low, y high, y low), then two digits a point
# for the delta from the previous one, biased by 32. Segments longer than 31
# were split when baking, so every delta fits in one digit. This is 2 kB of
# source instead of 900 float pairs, and it decodes in one Python loop at build
# time.
# --------------------------------------------------------------------------

_ALPHA = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
          "abcdefghijklmnopqrstuvwxyz+/")
_IDX = dict((c, i) for i, c in enumerate(_ALPHA))

COAST_W, COAST_H = 640, 128             # grid the deltas are quantised on
LAT_HI, LAT_LO = 80.0, 8.0

COAST = (
    "4r0kVZQXYUVUaUZXVX 2Q09QWZVZX 2P04SWaW 2K02YXTXUUZW 2Z0AGSgWaYnWHY 1w0"
    "3YXQWaV 1x02TXZV 3A1jXXUWXV 2s1jZXSWXV 2k1bdXdaQXXVOSQXbU 3T0pUZcXXaUW"
    "WUUYPVZQZV 2h0QdZJWYSaX 2q0EoZZYTXiZSaPTccVXQUaZHRRXUVYVdWYTVUMSCVWUZT"
    "aWVXYXcUYZaW 2O0AdXNZVUZU 2D0DUVeVVXYXWYTXPTZW 2007aWVYLXUVZWMVaUhYUUa"
    "X 1c07hTVYMX 1e0AhYIaRUaTUVcW 0O0ZYXUV 0m0dYXRXZU 1K0kYWVYYXSUXV 1X0rZ"
    "ZOSbX 0F0TbXRV 2401hZMVYVTV 5x1FYWUYUVYV 5l1FRXbV 7E1/WUZY 8N1uVUYXWYV"
    "V 8Q1yZUUaVU 8P1/cVWVYY 831kUUaVUZ 8O1ZVaVTYTWY 8y19TdOZVUPXYXUZUWVTcS"
    "bWYTXXaTXRYWXa 9110XVXYSZTUVYUWZPbZ 5F16YWVZVT 5G12XWVZWT 5M0gVZVVYU 4"
    "v0cYWUYaWTZfbWZJYaUTVXTZVSVUSaT 4c0OYYNZPWXVSVZVTWaUYXhV 8+0lZeSYYZVVV"
    "YVIXVXX 8J1+TXaRVa 8P1kVdaXXYPUXUUVYQYX 8P1wYWUYWU 8V1vXYUVWYVSYX 2s1/"
    "gPYXUXXaWTZUZYhWVYbY 6V0BkSjVEaRaZYMVbSUW 5k01JV 5i04PWaVZX 5W01cXMaNR"
    " 2l1/PUSQKQQXJQVQIGUWWZfiVXSQRTYUQORUPMXMUSaXWTNTWTLM8ROZYSIgLYhSZSNXW"
    "UTWSTYTdVVVXVPXQTdVbXNSoQ/aaXlTaYeVkZTXjWVVXViYcUZXWYZSSVZSaXYYVXZYYVZ"
    "XXZZReXUYXYPXTZQYPbWZaaoZWaaZYUUScTSSZUUReWdZXZZYbSccVXeZZbOZLWOcgSYXU"
    "XXZcXXUYYMaUUZVSWPaYYPXZWTXUZVVXYUYVTWYVVYbMdYhUVSPRVRXWXOVQbVeadYXbVZ"
    "SbVTggXVdYZ 2j1zfY 311hcYQYSVVVZWVUZW 5Q1AYWVZRUaV 8h1JZUYXSYVV 4Z1iVR"
    "aPfPWRcRdXdTkVZXUbmcaSjZaVbXaQWSHWURbTsVWUNSbSOYZYRXUVXUSVQeYYLYZaUWVY"
    "QQXTKPVZhdTVVbUSLOSZQWVYSXVaSaQXQVVLlUWSQSbWWUbVcReUVRaVUaYYmVZVWSbXWT"
    "UVhVLWTUWSdSTVKeZYRdRYROTZRVVQvImUcXTWYYnYXYUXKWZXWZaXWTaYYVVVaUZXXVUS"
    "bXXXUXXXjSYWUXgVYXYVUVXVkZXVSVWTbSbYVXXbYXSZYXaTUSXVUUZUZZVUgXUShWVVYV"
    "/RbYiXNZvYWVdWdbmVUUZWmXcYhWZZoXVUmY 9/0RSWaaMYQZTVNXTZYXUbTXQcTOXTkPX"
    "TPaUTSXRZYX9WJecYZVYYUeNfRWOdZbVZSXVSYWSTXUOXYTVVPaaZbWQabcVcRdRZNYVYT"
    "UTXUZcdWcPcWUNPUdYY 7k1/YRTMTYTWWPSROYKgVgTZ 781+RKUMSXPONXPUUURXOPUXb"
    "fYUWaaVaSXabaTcRZBeVOICWUUZUSivdcVXYZjTUc 4f1/PQVRYUWQ 000KgaWVaWaYRXW"
    "YLTUWWY 000FaXSW 0i1hXXUXXU 2r1XXZVT 3E0xaXUXUU 6J0/aTdVWZRXaZXZYUXYTX"
    "YZWaNUWUZUPO 3D0sbXRV 2h0VZVTX 2w0MTWYVXX 2K0IYXVXQVbV 230CZacYTXXXDWO"
    "UfVMWaUQWdTgXZYVTaW 2o0DUVXVdYQW 8v0CcWQW 9808aXSV 8t07hXLYTUZV 2H06XX"
    "VYPVWVdV 7t01aYMXcT 7o00WYKV 2b01QYMU 3100MXZYOYaXRXGVZVVVcWRVaVUU 4U0"
    "1VZYXQXaXUYZXOYYYSWbZQUUYdX1eRbVaTWQVQRSQcRPWWUcXOUYUPREVRUeWLUkT 220B"
    "aXVXTU"
)


def _coastlines(scale_x, scale_y):
    """Decode COAST into [[(x, y), ...], ...] on the supersampled canvas."""
    out = []
    for chunk in COAST.split(" "):
        x = _IDX[chunk[0]] * 64 + _IDX[chunk[1]]
        y = _IDX[chunk[2]] * 64 + _IDX[chunk[3]]
        pts = [(x * scale_x, y * scale_y)]
        for i in range(4, len(chunk) - 1, 2):
            x += _IDX[chunk[i]] - 32
            y += _IDX[chunk[i + 1]] - 32
            pts.append((x * scale_x, y * scale_y))
        out.append(pts)
    return out


# --------------------------------------------------------------------------
# A 3x5 pixel font.
#
# TrueType at five pixels is mush, and the Pi does not have the same faces
# installed as the machine this was written on, so the readouts use a font
# baked here instead: five rows a glyph, each row an octal digit whose three
# bits are the three columns. It is the same trick every LED sign uses and it
# is the only thing that stays legible at this size.
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


def _glyph(ch):
    rows = _FONT.get(ch, _FONT[" "])
    g = np.zeros((5, 3), f32)
    for r, digit in enumerate(rows):
        v = int(digit, 8)
        for c in range(3):
            if v & (4 >> c):
                g[r, c] = 1.0
    return g


def _text(s):
    """A (5, 4n-1) float mask for a string; 1 px between glyphs."""
    s = s.upper()
    if not s:
        return np.zeros((5, 1), f32)
    out = np.zeros((5, len(s) * 4 - 1), f32)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _glyph(ch)
    return out


def _wrap(words, cols):
    """Greedy word wrap to `cols` characters, or None if a word is too long."""
    lines, cur = [], ""
    for word in words:
        if len(word) > cols:
            return None
        joined = word if not cur else cur + " " + word
        if len(joined) <= cols:
            cur = joined
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def _message_mask(text, W, H):
    """The closing line: a whole-panel mask plus its layout, or None.

    Returns `(mask, scale, rows)`, where rows is `[(text, y0, x0), ...]` — one
    entry a wrapped line, giving the top row and left column the line was drawn
    at. The typing needs the geometry as well as the pixels: it reveals the
    mask a glyph cell at a time and puts a caret at the write position, and
    both of those are `x0 + j * 4 * scale` arithmetic that only the layout
    knows.

    Same 3x5 font as the readouts, scaled by an integer factor with
    np.repeat — nearest-neighbour, so it stays a pixel font instead of turning
    to mush, and so it needs no font file. That last part is not fussiness:
    the Pi driving the wall does not have the faces installed that this was
    written on, and a TrueType lookup that falls through is a demo which dies
    on the wall and nowhere else.

    The layout is chosen by trying the largest scale first and wrapping to
    whatever fits, rather than by picking a line count up front. At 320 px,
    "THE ONLY WINNING MOVE IS NOT TO PLAY" as one line is nine pixels a
    character, which is not a letter so much as a rumour of one; broken over
    two centred lines the same string goes to 12x20 glyphs, which reads across
    a room. The search finds that on its own and will find the equivalent for
    whatever anyone retypes from the control panel.
    """
    words = str(text).upper().split()
    if not words:
        return None
    chosen = None
    for scale in range(6, 0, -1):
        cols = (W - 8 + 1) // (4 * scale)     # glyph pitch is 4, less the gap
        if cols < 1:
            continue
        lines = _wrap(words, cols)
        if lines is None:
            continue
        gap = max(2, scale - 1)
        height = len(lines) * 5 * scale + (len(lines) - 1) * gap
        if height <= H - 6:
            chosen = (lines, scale, gap, height)
            break
    if chosen is None:
        return None

    lines, scale, gap, height = chosen
    mask = np.zeros((H, W), f32)
    rows = []
    y = (H - height) // 2
    for line in lines:
        m = _text(line)
        if scale > 1:
            m = np.repeat(np.repeat(m, scale, axis=0), scale, axis=1)
        x = (W - m.shape[1]) // 2
        mask[y:y + m.shape[0], x:x + m.shape[1]] = m
        rows.append((line, y, x))
        y += 5 * scale + gap
    return mask, scale, rows


# --------------------------------------------------------------------------
# The board's geography and the two sides.
# --------------------------------------------------------------------------

# (label, longitude, latitude). Short enough to read at 3 px a glyph, and
# picked from the places the film's board actually points at.
BLUE = [
    ("SEATTLE", -122.3, 47.6), ("LOS ANGELES", -118.2, 34.1),
    ("DENVER", -104.8, 39.7), ("OMAHA", -95.9, 41.3),
    ("CHICAGO", -87.6, 41.9), ("NEW YORK", -74.0, 40.7),
    ("WASHINGTON", -77.0, 38.9), ("ANCHORAGE", -149.9, 61.2),
    ("LONDON", -0.1, 51.5), ("BERLIN", 13.4, 52.5),
]

RED = [
    ("MOSCOW", 37.6, 55.8), ("LENINGRAD", 30.3, 59.9),
    ("KIEV", 30.5, 50.5), ("MURMANSK", 33.1, 68.9),
    ("SVERDLOVSK", 60.6, 56.8), ("NOVOSIBIRSK", 82.9, 55.0),
    ("VLADIVOSTOK", 131.9, 43.1), ("PEKING", 116.4, 39.9),
    ("PETROPAVLOVSK", 158.6, 53.0), ("BAIKONUR", 63.3, 45.9),
]

PALETTES = {
    # Each ramp is dark -> the phosphor colour -> a pale core -> white, so a
    # bloom that overshoots saturates to white instead of clipping to a flat
    # slab of the base hue.
    "green": [(0.00, (0, 0, 0)), (0.22, (0, 40, 16)), (0.45, (0, 130, 50)),
              (0.70, (40, 235, 110)), (0.88, (170, 255, 190)),
              (1.00, (255, 255, 255))],
    "cyan": [(0.00, (0, 0, 0)), (0.22, (0, 28, 46)), (0.45, (0, 110, 165)),
             (0.70, (60, 210, 255)), (0.88, (185, 240, 255)),
             (1.00, (255, 255, 255))],
    "amber": [(0.00, (0, 0, 0)), (0.22, (34, 14, 0)), (0.45, (150, 62, 0)),
              (0.70, (255, 150, 20)), (0.88, (255, 220, 130)),
              (1.00, (255, 255, 255))],
}

# How many live tracks and blooms a single frame will draw. Both are Python
# loops around small numpy calls, and on the Pi a numpy call costs more than
# the arithmetic in it, so the crescendo is capped rather than allowed to grow
# with the schedule. Both caps are set where the acceleration lands: at the
# end of the cycle there are about twenty trajectories in the air and nine or
# ten blooms open at once, so the caps bite only on the worst frames and the
# rest of the cycle never notices them. Visually 20 tracks at 320x64 is
# already a solid mat of lines and a twenty-first would not be visible.
MAX_LIVE = 20
MAX_BLOOM = 10
TAIL = 52                                # path points lit behind the head
BLOOM_R = 16                             # bloom window half-size, px

# How many times as often the last launch of a cycle comes as the first. The
# whole escalation is this one number: rate(u) = rate0 * RAMP**u.
RAMP = 48.0

MESSAGE = "THE ONLY WINNING MOVE IS NOT TO PLAY"

# The closing line types itself out. Characters a second at the default cycle;
# slow enough to read along with, and the machine at the other end is printing
# to a terminal rather than racing. TYPE_SHARE caps the whole performance --
# lead-in, keystrokes and pauses -- at a fraction of the message phase, so what
# is left is always time to sit and read the finished line. A longer --message
# or a shorter --cycle scales the rate to fit rather than running past the end
# of the phase and getting cut off mid-word.
TYPE_CPS = 7.5
TYPE_SHARE = 0.55
TYPE_JITTER = 0.05                       # +/- this fraction of the interval
CURSOR_HZ = 2.0                          # blink rate once the line is done


def add_arguments(ap):
    ap.add_argument("--colour", default="green",
                    choices=sorted(PALETTES),
                    help="phosphor colour of the whole board")
    ap.add_argument("--arcs", type=int, default=6,
                    help="scale of the whole exchange: roughly how many "
                         "trajectories are in the air a third of the way in. "
                         "The opening is much quieter and the crescendo much "
                         "busier, whatever this is set to")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="multiplies the clock; 2 runs the war twice as fast")
    ap.add_argument("--cycle", type=float, default=80.0,
                    help="seconds for one pass: the accelerating exchange, "
                         "the crescendo, the blackout, the closing line and "
                         "the way back to DEFCON 5")
    ap.add_argument("--message", default=MESSAGE,
                    help="the line held on a black board after the exchange. "
                         "Empty turns it off, and the darkness and the time "
                         "it would have taken go back to the war")
    ap.add_argument("--defcon", dest="defcon", action="store_true",
                    default=True)
    ap.add_argument("--no-defcon", dest="defcon", action="store_false",
                    help="hide the DEFCON readout, its bar meter and the "
                         "launched/landed tally, leaving only the map")
    ap.add_argument("--labels", dest="labels", action="store_true",
                    default=True)
    ap.add_argument("--no-labels", dest="labels", action="store_false",
                    help="hide the target names that blink over impacts")
    ap.add_argument("--grid", dest="grid", action="store_true", default=True)
    ap.add_argument("--no-grid", dest="grid", action="store_false",
                    help="hide the dotted graticule behind the coastline")
    ap.add_argument("--seed", type=int, default=0,
                    help="0 picks one at random; any other value fixes which "
                         "cities shoot at which")


# --------------------------------------------------------------------------
# Build.
# --------------------------------------------------------------------------

def _base_map(W, H, grid):
    """The static board: graticule under an antialiased coastline, 0..1."""
    from PIL import Image, ImageDraw

    ss = 2
    img = Image.new("L", (W * ss, H * ss), 0)
    draw = ImageDraw.Draw(img)

    if grid:
        # Dotted, and drawn before the coast so the coast wins where they
        # overlap. Meridians every 30 degrees, parallels every 15: enough to
        # say "chart" without turning the ocean into graph paper.
        for lon in range(-180, 181, 30):
            x = int((lon + 180.0) / 360.0 * W) * ss
            for y in range(0, H * ss, 4):
                draw.point((x, y), fill=40)
        for lat in range(15, 80, 15):
            y = int((LAT_HI - lat) / (LAT_HI - LAT_LO) * H) * ss
            for x in range(0, W * ss, 4):
                draw.point((x, y), fill=40)

    for pts in _coastlines(W * ss / float(COAST_W), H * ss / float(COAST_H)):
        draw.line([(int(round(x)), int(round(y))) for x, y in pts],
                  fill=255, width=1)

    a = np.asarray(img, np.uint8).astype(f32) / 255.0
    # Box-filter the supersample down. This is what makes a one-pixel coast
    # read as a continuous line rather than as dashes: partial coverage lands
    # in the palette's dim end instead of being thrown away.
    a = a.reshape(H, ss, W, ss).mean(axis=(1, 3))
    # ...but a straight mean leaves the whole map at a quarter brightness, so
    # lift it. sqrt rather than a scale factor keeps the faint half-covered
    # pixels visible instead of crushing them.
    return np.sqrt(np.clip(a * 1.6, 0.0, 1.0)).astype(f32)


def _project(lon, lat, W, H):
    return ((lon + 180.0) / 360.0 * W,
            (LAT_HI - lat) / (LAT_HI - LAT_LO) * H)


def _path(x0, y0, x1, y1, W, H):
    """Flat pixel indices along one trajectory, plus its length in px.

    Not a true great circle: at these latitudes the great circle from Los
    Angeles to Moscow goes over the pole and straight off the top of a 72
    degree band. So the track is bowed poleward in map space instead, which is
    what the film's board draws anyway, and is clamped to stay on the panel.

    The x step takes whichever way round the world is shorter, and the indices
    are computed modulo the width -- so a Pacific shot leaves the right edge
    and reappears on the left, which on a cylindrical projection is exactly
    right and costs nothing.
    """
    dx = x1 - x0
    if dx > W * 0.5:
        dx -= W
    elif dx < -W * 0.5:
        dx += W
    dy = y1 - y0
    dist = math.hypot(dx, dy)
    bow = min(26.0, max(4.0, dist * 0.24))
    n = max(8, int(dist / 0.7))
    s = np.linspace(0.0, 1.0, n, dtype=f32)
    xs = x0 + dx * s
    sn = np.sin(s * f32(math.pi))
    # The bow has to be pulled back until the whole track fits on the panel.
    # Clipping instead -- which is what the first version did -- lays the top
    # of a long track flat along row 0, and a horizontal white bar across the
    # Arctic reads as a rendering fault rather than as a trajectory.
    for _ in range(4):
        ys = y0 + dy * s - bow * sn
        low = float(ys.min())
        if low >= 1.0 or bow <= 1.0:
            break
        bow = max(1.0, bow - (1.0 - low))
    xi = np.mod(np.round(xs).astype(np.int32), W)
    yi = np.clip(np.round(ys).astype(np.int32), 0, H - 1)
    flat = yi * W + xi
    # Consecutive duplicates are wasted work in every frame that draws this
    # track, so drop them once, here.
    keep = np.empty(flat.shape, bool)
    keep[0] = True
    np.not_equal(flat[1:], flat[:-1], out=keep[1:])
    return flat[keep], dist


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)

    base = _base_map(W, H, args.grid)
    lut = ds.gradient(PALETTES[args.colour], 256, dtype=np.uint8)

    cycle = max(8.0, float(args.cycle))

    # ---- the shape of the cycle -----------------------------------------
    # Phases are fractions of the cycle rather than fixed-length tails, so a
    # 30 s slot gets a shorter war *and* a shorter blackout and a shorter
    # message, instead of a war with the ending sheared off it.
    laid_out = _message_mask(args.message, W, H)
    msg_mask = None if laid_out is None else laid_out[0]
    if msg_mask is not None:
        # The message phase is a little wider than it was when the line simply
        # switched on, because typing it costs time that used to be reading
        # time. The four points come out of the war, which is the cheapest
        # place to take them from: the exchange is one continuous ramp
        # parameterised by its own progress, so a shorter one is the same
        # escalation slightly steeper and nothing in it visibly moves.
        f_war, f_dark = 0.70, 0.04
        msg_len = cycle * 0.22
    else:
        # Nothing to hold on a black board, so the darkness is only long
        # enough to punctuate the reset and the exchange takes the rest.
        f_war, f_dark = 0.94, 0.02
        msg_len = 0.0
    dark_at = cycle * f_war                  # the last wave whites the board
    # Long enough to be a flash, never so long that it eats the darkness it
    # is supposed to introduce -- which it would at --cycle 8 without the
    # upper bound.
    white_len = min(max(0.22, cycle * f_dark * 0.25), cycle * f_dark * 0.6)
    msg_at = dark_at + cycle * f_dark
    msg_off = msg_at + msg_len
    # The last launch goes up about one late-war flight time before the
    # blackout, so impacts are still arriving when the lights go out. Getting
    # this wrong leaves a dead board waiting for the phase to end, which is
    # the one thing the crescendo cannot survive.
    launch_end = max(cycle * 0.25, dark_at - max(1.2, cycle * 0.030))

    sites = []
    for name, lon, lat in BLUE + RED:
        x, y = _project(lon, lat, W, H)
        sites.append((name, x, y))
    n_blue = len(BLUE)

    # ---- the schedule, baked --------------------------------------------
    #
    # render() has to be a pure function of t: ftsched builds effects ahead of
    # time and starts them at t=0, and the preview baker steps them at a fixed
    # rate, so anything decided per frame would differ between the wall and
    # the thumbnail. Every launch, its flight time and its target are drawn
    # here instead and indexed by the clock.
    #
    # There is no separate finale here. The old version stepped the rate up
    # and then bolted a salvo of thirty-eight launches on the end, and the
    # join showed: the board was legible, then it was a wall, with nothing in
    # between. Instead the interval between launches shrinks geometrically
    # across the whole cycle and the flight times shrink with it, which
    # compounds — more going up, each of them arriving sooner — so the last
    # ten seconds saturate on their own without a single discontinuity in the
    # schedule for the eye to catch.
    tracks = []                              # (t0, t1, idx, tx, ty, site)
    rate0 = max(0.02, args.arcs * 0.030)     # launches a second at the start
    when = -7.0                              # start already mid-flight
    while when < launch_end:
        u = min(1.0, max(0.0, when) / launch_end)
        blue_to_red = rng.random() < 0.5
        if blue_to_red:
            src = int(rng.integers(0, n_blue))
            dst = n_blue + int(rng.integers(0, len(sites) - n_blue))
        else:
            src = n_blue + int(rng.integers(0, len(sites) - n_blue))
            dst = int(rng.integers(0, n_blue))
        idx, dist = _path(sites[src][1], sites[src][2],
                          sites[dst][1], sites[dst][2], W, H)
        flight = (3.4 + dist / 55.0) / (1.0 + 1.5 * u ** 1.4)
        tracks.append((when, when + flight, idx,
                       sites[dst][1], sites[dst][2], dst))
        when += 1.0 / (rate0 * RAMP ** u)

    tracks.sort(key=lambda r: r[0])

    # Live tracks, bucketed by half second. A frame looks up one list rather
    # than scanning all ninety.
    step = 0.5
    nb = int(cycle / step) + 2
    live = [[] for _ in range(nb)]
    for i, (t0, t1, _idx, _tx, _ty, _dst) in enumerate(tracks):
        a = max(0, int(math.floor(t0 / step)))
        b = min(nb - 1, int(math.ceil(t1 / step)))
        for k in range(a, b + 1):
            live[k].append(i)

    # The tracks that are already in the air when the cycle restarts. The tail
    # of the cycle draws these at negative time so the loop back to a quiet
    # board is a fade rather than a cut.
    preroll = [i for i, r in enumerate(tracks) if r[0] < 0.0]

    # Spent tracks, in eight groups. Concatenating each group's indices turns
    # what would be ninety scatters into eight; the price is that a whole
    # group shares one brightness, which at this size is invisible.
    #
    # np.unique on top of that is what keeps the crescendo affordable. Ninety
    # trajectories are perhaps thirty thousand path points but the panel has
    # only 20480 pixels and they cross each other constantly, so the duplicates
    # are most of the work and drawing them twice with maximum() changes
    # nothing. It also leaves the indices sorted, which a gather likes.
    ngroup = 8
    groups = []
    for g in range(ngroup):
        lo = -1e9 if g == 0 else dark_at * g / float(ngroup)
        hi = dark_at * (g + 1) / float(ngroup)
        member = [r[2] for r in tracks if lo <= r[1] < hi]
        if member:
            groups.append((hi, np.unique(np.concatenate(member))))

    # Impacts, bucketed the same way. A bloom's life shortens as the exchange
    # does: 2.4 s early, about a second by the end. Not for looks — at six
    # impacts a second a 2.4 s ring would leave fifteen open at once against a
    # cap of ten, and the cap would then be dropping rings that were still
    # visibly growing. Shortening them keeps the count under the cap, so the
    # crescendo is dense because there are many blooms rather than because
    # some of them vanish.
    impacts = []
    for r in tracks:
        u = min(1.0, max(0.0, r[1]) / max(1e-6, launch_end))
        impacts.append((r[1], r[3], r[4], r[5], 2.4 - 1.25 * u))
    bloom_at = [[] for _ in range(nb)]
    for i, (ti, _x, _y, _d, life) in enumerate(impacts):
        a = max(0, int(math.floor(ti / step)))
        b = min(nb - 1, int(math.ceil((ti + life) / step)))
        for k in range(a, b + 1):
            bloom_at[k].append(i)

    # The whole-board flash, baked at 60 Hz for the whole cycle. Every impact
    # adds a short exponential pulse, so while they are arriving one at a time
    # this is a flicker under the map and by the crescendo the pulses overlap
    # into a rising glare that never quite goes out. One float a frame and a
    # scalar add — which is the only reason the board can flare on every
    # detonation rather than only on the last one.
    FLASH_HZ = 60.0
    nf = int(cycle * FLASH_HZ) + 2
    flash = np.zeros(nf, f32)
    tf = np.arange(nf, dtype=f32) / f32(FLASH_HZ)
    for ti, _x, _y, _d, _life in impacts:
        a = max(0, int(ti * FLASH_HZ))
        b = min(nf, a + int(0.55 * FLASH_HZ))
        if b > a:
            np.add(flash[a:b],
                   np.exp(-(tf[a:b] - f32(ti)) * f32(6.0)) * f32(0.18),
                   out=flash[a:b])
    np.clip(flash, 0.0, 0.85, out=flash)

    # Counters, so the readout can say how many have flown without counting.
    launched = np.zeros(nb, np.int32)
    landed = np.zeros(nb, np.int32)
    for t0, t1, _i, _x, _y, _d in tracks:
        # The opening tracks were launched before the cycle started, so they
        # count from bucket zero rather than from a negative index.
        launched[max(0, min(nb - 1, int(t0 / step))):] += 1
        landed[max(0, min(nb - 1, int(t1 / step))):] += 1

    # ---- baked masks and ramps ------------------------------------------
    # A long tail with a steep taper. The first version was thirty points and
    # a gentle ramp, and at 320x64 it read as a scratch on the panel rather
    # than as something moving -- a track needs both length and a hard bright
    # head before the eye will follow it.
    head_ramp = (np.linspace(0.04, 1.0, TAIL, dtype=f32) ** f32(2.6)) * f32(1.7)
    rad = np.hypot(*np.meshgrid(
        np.arange(-BLOOM_R, BLOOM_R + 1, dtype=f32),
        np.arange(-BLOOM_R, BLOOM_R + 1, dtype=f32))).astype(f32)

    labels = [_text(name) for name, _x, _y in sites]
    defcon_word = _text("DEFCON")
    digits = [_text(str(d)) for d in range(10)]

    # DEFCON steps, read off the schedule rather than off a clock of their
    # own. The level drops when the cumulative number of launches crosses a
    # share of the cycle's total, so the countdown is a *consequence* of the
    # rate: because the rate ramps geometrically the early levels are long and
    # the late ones are short, which is both what the arithmetic gives and
    # what the film feels like.
    t0s = sorted(r[0] for r in tracks if r[0] > 0.0) or [0.0]
    steps = [(0.0, 5)]
    for share, level in ((0.04, 4), (0.12, 3), (0.30, 2), (0.58, 1)):
        at = t0s[min(len(t0s) - 1, int(share * len(t0s)))]
        # Nudged apart if the schedule is short enough to put two crossings in
        # the same second: a level nobody ever sees is a level the readout may
        # as well not have.
        steps.append((max(at, steps[-1][0] + cycle * 0.02), level))

    # ---- the closing line, typed ----------------------------------------
    #
    # Baked for the same reason the war is: render() has to be a pure function
    # of t, so every character's arrival time is drawn here and the frame does
    # a binary search rather than advancing a state machine. `type_t` holds the
    # time each state begins, `type_row`/`type_n` say which line is being
    # written and how much of it is on the board.
    #
    # The rhythm is a machine's, not a person's: one interval, wobbled by a few
    # per cent. That wobble is the whole difference between a caret writing and
    # a progress bar filling -- dead-constant timing at this size reads as a
    # wipe -- and any more of it would be a human at the keyboard, which is the
    # wrong character. The two pauses are punctuation: a beat at the line break
    # where a terminal would return the carriage, and a longer one before the
    # last word, because "NOT TO PLAY" is the sentence turning over.
    type_t = None
    if msg_mask is not None:
        mscale, rows = laid_out[1], laid_out[2]
        pitch, cell = 4 * mscale, 3 * mscale
        row_geom = [(y0, y0 + 5 * mscale, x0, len(txt))
                    for txt, y0, x0 in rows]

        # Where the last word starts, so it can be given its beat -- but only
        # when it follows a space on its own line, since a word that begins a
        # line has had the line break's pause already.
        last_row = len(rows) - 1
        last_txt = rows[last_row][0]
        gap_at = last_txt.rfind(" ")

        base = 1.0 / TYPE_CPS
        times, at_row, at_n = [], [], []
        clock = base * 3.0               # a beat of bare caret before the first
        for r, (txt, _y0, _x0) in enumerate(rows):
            for k in range(len(txt) + 1):
                times.append(clock)
                at_row.append(r)
                at_n.append(k)
                if k == len(txt):
                    break
                step = base * (1.0 + TYPE_JITTER * (2.0 * rng.random() - 1.0))
                if r == last_row and gap_at >= 0 and k == gap_at + 1:
                    step += base * 4.0
                clock += step
            if r < last_row:
                clock += base * 5.0      # the carriage return
        # Scale the whole performance to fit its share of the phase. A long
        # --message or a short --cycle types faster rather than being cut off
        # in the middle of a word, which is the one failure this cannot have.
        span = max(1e-6, times[-1] + base)
        budget = msg_len * TYPE_SHARE
        if span > budget:
            k = budget / span
            times = [v * k for v in times]
        type_t = np.array(times, np.float64)
        type_row = np.array(at_row, np.int32)
        type_n = np.array(at_n, np.int32)
        type_end = float(type_t[-1])

    # ---- per-frame buffers ----------------------------------------------
    acc = np.zeros((H, W), f32)
    accf = acc.reshape(-1)
    scratch = np.empty((H, W), f32)
    codes = np.empty((H, W), np.uint8)
    out = np.zeros((H, W, 3), np.uint8)

    def blit(mask, x, y, gain):
        """Draw a small mask into acc with max(), clipped to the panel."""
        mh, mw = mask.shape
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + mw), min(H, y + mh)
        if x1 <= x0 or y1 <= y0:
            return
        sub = acc[y0:y1, x0:x1]
        np.maximum(sub, mask[y0 - y:y1 - y, x0 - x:x1 - x] * gain, out=sub)

    def plate(x, y, w, h, keep):
        """Knock the map back under a readout so the text stays legible."""
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + w), min(H, y + h)
        if x1 > x0 and y1 > y0:
            acc[y0:y1, x0:x1] *= keep

    # ---- the drawing, split so the ending can skip nearly all of it ------
    #
    # The blackout and the closing line do not touch the map, the tracks or
    # the blooms at all, so the cheapest frames of the cycle are its last
    # ones. That is the right way round: the frames that could arrive late are
    # the ones in the crescendo, and the ending is where the wall gets its
    # headroom back.

    def live_tracks(pp, ids):
        """Draw up to MAX_LIVE trajectories at time `pp`, newest first.

        Newest first so that the cap drops the oldest rather than an arbitrary
        set -- an old track is nearly landed and its head is about to be
        replaced by a bloom anyway, whereas a young one disappearing mid-flight
        is the sort of thing an eye following it will catch.
        """
        drawn = 0
        for i in reversed(ids):
            t0, t1, idx, _tx, _ty, _dst = tracks[i]
            if not (t0 <= pp < t1):
                continue
            n = idx.shape[0]
            head = int((pp - t0) / (t1 - t0) * n)
            if head <= 0:
                continue
            tail = min(head, TAIL)
            sl = idx[head - tail:head]
            accf[sl] = np.maximum(accf[sl], head_ramp[TAIL - tail:])
            # A 2x2 warhead. One pixel is invisible at this size however
            # bright it is; two are a moving object.
            hy, hx = divmod(int(idx[head - 1]), W)
            blob = acc[max(0, hy - 1):hy + 1, max(0, hx - 1):hx + 1]
            np.maximum(blob, f32(1.75), out=blob)
            drawn += 1
            if drawn >= MAX_LIVE:
                break

    def readout(bucket, level, since):
        """DEFCON: the word, the number, a five-segment meter and the tally."""
        plate(0, 0, 36, 14, f32(0.10))
        blit(defcon_word, 2, 2, 0.85)
        # The number blinks for a second and a half after it changes.
        if since > 1.5 or (since * 4.0) % 1.0 < 0.55:
            blit(digits[level], 30, 2, 1.5)
        for s in range(5):
            bx = 2 + s * 6
            lit = 1.25 if s < (6 - level) else 0.18
            acc[9:12, bx:bx + 5] = np.maximum(acc[9:12, bx:bx + 5], lit)

        # Tally, bottom right, out over the empty Pacific.
        txt = _text("%03d/%03d" % (landed[bucket], launched[bucket]))
        plate(W - txt.shape[1] - 3, H - 8, txt.shape[1] + 3, 8, f32(0.10))
        blit(txt, W - txt.shape[1] - 2, H - 7, 0.8)

    def palette():
        """Map the accumulator through the ramp. np.take with out= keeps this
        allocation-free; the scalar 170 puts an accumulator value of 1.0 at the
        palette's bright end and leaves headroom above it for blooms, the
        glare and the whiteout to go all the way to white."""
        np.multiply(acc, f32(170.0), out=scratch)
        np.clip(scratch, 0.0, 255.0, out=scratch)
        np.copyto(codes, scratch, casting="unsafe")
        np.take(lut, codes, axis=0, out=out)
        return out

    def render(t, frame):
        p = (t * args.speed) % cycle

        # ---- the ending: whiteout, black, the line, and the way back -----
        if p >= dark_at:
            if p < dark_at + white_len:
                # The last wave takes the board with it. The map and the spent
                # tracks are still drawn under the white, because a panel
                # filled with one flat value reads as a fault rather than as a
                # detonation -- the eye needs to see the thing it recognises
                # being overwhelmed. The white itself goes as the fourth power
                # so it is a flash and not a hold; the board behind it fades
                # out linearly over the rest of the window.
                w = 1.0 - (p - dark_at) / white_len
                np.multiply(base, f32(0.62 * w), out=acc)
                for _hi, idx in groups:
                    accf[idx] = np.maximum(accf[idx], f32(0.55 * w))
                np.add(acc, f32(1.9 * w ** 4), out=acc)
            elif p < msg_at:
                # Nothing at all, and this is the phase doing the work: the
                # line lands because of the seconds of black in front of it.
                acc.fill(0.0)
            elif p < msg_off:
                tt = p - msg_at
                k = tt / (msg_off - msg_at)
                # No fade in: the caret arrives out of the black already lit,
                # which is what a terminal does. Only the tail fades, and only
                # so the map has something to come up out of.
                g = min(1.0, (1.0 - k) / 0.12)
                acc.fill(0.0)
                # Where the typing has got to. One searchsorted over a few
                # dozen floats, then at most three slice copies out of the
                # baked mask -- the ending stays the cheapest part of the
                # cycle, which is the point of it being at the end.
                s = int(np.searchsorted(type_t, tt, side="right")) - 1
                if s < 0:
                    s = 0
                cur, n = int(type_row[s]), int(type_n[s])
                for r in range(cur + 1):
                    y0, y1, x0, ncols = row_geom[r]
                    shown = ncols if r < cur else n
                    if shown <= 0:
                        continue
                    x1 = x0 + (shown - 1) * pitch + cell
                    np.multiply(msg_mask[y0:y1, x0:x1], f32(1.35 * g),
                                out=acc[y0:y1, x0:x1])
                # The caret: solid while it is writing, blinking once the line
                # is finished. A caret that blinks *through* the typing reads
                # as a fault rather than as a caret.
                done = tt >= type_end
                if not done or (tt * CURSOR_HZ) % 1.0 < 0.55:
                    y0, y1, x0, _ncols = row_geom[cur]
                    # A line that fills the panel leaves less than a cell of
                    # margin after its last glyph, so the caret is pushed back
                    # on rather than clipped: half a block at the edge reads as
                    # the text having been cut off, which is the one thing this
                    # phase must never look like.
                    cx = min(x0 + n * pitch, W - cell)
                    acc[y0:y1, cx:cx + cell] = f32(1.35 * g)
            else:
                # Back to a quiet board. The tracks that will be mid-flight at
                # t=0 are drawn here at negative time, so the loop is a fade
                # into a war already under way rather than a cut to one.
                np.multiply(base, f32(0.72), out=acc)
                live_tracks(p - cycle, preroll)
                if args.defcon:
                    readout(0, 5, 9.0)
                np.multiply(acc, f32((p - msg_off) /
                                     max(1e-6, cycle - msg_off)), out=acc)
            return palette()

        # ---- the exchange -------------------------------------------------
        bucket = min(nb - 1, int(p / step))

        # The map dims a touch as the exchange gets going -- not physically
        # motivated, but it makes the tracks read against it -- while the baked
        # flash lifts the whole panel on every detonation. So the board gets
        # brighter towards the crescendo because there are more explosions on
        # it, not because anything is ramping a gain.
        map_gain = 0.72 - 0.14 * min(1.0, p / dark_at)
        # A slow mains-hum wobble. One scalar; it costs nothing and it stops
        # the static half of the picture from looking like a screenshot.
        map_gain *= 1.0 + 0.03 * math.sin(t * 5.1)
        np.multiply(base, f32(map_gain), out=acc)

        # Spent tracks: the lines that stay on the board, and most of what
        # makes the late cycle look like the film.
        for hi, idx in groups:
            age = p - hi
            if age < -0.5:
                continue
            g = 0.42 * math.exp(-max(0.0, age) / 16.0) + 0.13
            accf[idx] = np.maximum(accf[idx], f32(g))

        live_tracks(p, live[bucket])

        # Blooms: an expanding ring plus a core flash, on a precomputed radius
        # window. Squared linear falloff, not a Gaussian -- see the docstring.
        # Newest first, for the same reason the tracks are.
        blooms = 0
        for i in reversed(bloom_at[bucket]):
            ti, bx, by, dst, life = impacts[i]
            age = p - ti
            if age < 0.0 or age > life:
                continue
            r = 2.0 + age * 8.0
            amp = max(0.0, 1.0 - age / life) ** 1.4
            cx, cy = int(round(bx)), int(round(by))
            x0, y0 = max(0, cx - BLOOM_R), max(0, cy - BLOOM_R)
            x1, y1 = min(W, cx + BLOOM_R + 1), min(H, cy + BLOOM_R + 1)
            if x1 > x0 and y1 > y0:
                sub = rad[y0 - cy + BLOOM_R:y1 - cy + BLOOM_R,
                          x0 - cx + BLOOM_R:x1 - cx + BLOOM_R]
                v = 1.0 - np.abs(sub - f32(r)) * f32(0.42)
                np.maximum(v, 0.0, out=v)
                v *= v
                v *= f32(amp * 1.5)
                tgt = acc[y0:y1, x0:x1]
                np.maximum(tgt, v, out=tgt)
            if age < 0.35:
                acc[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2] = 1.6
            if args.labels and blooms < 4 and 0.15 < age < life * 0.85:
                # Blinking, because a steady caption over a map reads as
                # furniture and a blinking one reads as an alarm.
                if (age * 5.0) % 1.0 < 0.62:
                    lab = labels[dst]
                    lx = cx + 4 if cx < W - 50 else cx - 4 - lab.shape[1]
                    # Nudge a caption that would hang off an edge back on;
                    # clipping it instead loses half a glyph and turns
                    # ANCHORAGE into 1CHORAGE.
                    lx = max(1, min(lx, W - lab.shape[1] - 1))
                    ly = cy - 8 if cy > 10 else cy + 5
                    plate(lx - 1, ly - 1, lab.shape[1] + 2, 7, f32(0.12))
                    blit(lab, lx, ly, 1.15)
            blooms += 1
            if blooms >= MAX_BLOOM:
                break

        if args.defcon:
            level, since = 5, p
            for tstep, lvl in steps:
                if p >= tstep:
                    level, since = lvl, p - tstep
            readout(bucket, level, since)

        glare = float(flash[min(nf - 1, int(p * FLASH_HZ))])
        if glare > 0.0:
            np.add(acc, f32(glare), out=acc)

        return palette()

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
