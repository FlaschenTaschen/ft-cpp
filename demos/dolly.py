#!/usr/bin/env python3
"""Nine twenty-five: a rhinestone minute for Dolly Parton.

Sixty seconds, cued at 9:25 in the morning and 9:25 at night, and the joke is
the time itself. The wall puts up a clock reading **9:25** with the colon
blinking the way every cheap clock radio in America blinked, and underneath it
says WE'RE WORKIN' 9:25. CLOSE ENOUGH. Then it stops being a joke and starts
being the reason: a butterfly a month crossing the panel, each one landing as a
book, twelve of them for a year, sixty by the time a kid starts school. That is
Dolly Parton's Imagination Library, drawn literally, and the last card asks the
only thing worth asking a room full of people who make things -- put a book in
a kid's hands.

Dolly Parton died on 25 August 2026, aged eighty. This is a tribute and it is
deliberately not a solemn one: an hour after the news her own website was still
selling a butterfly brooch, and a panel that went grey and quiet for her would
be the one thing she was never once in her life. It keeps the gag, it keeps the
pink, and it carries the dates in six-point type at the bottom of the last
card, which is the size she would have wanted them.

## Pink is the only colour decision

There is exactly one palette in this file and everything on the panel is drawn
through it. The whole picture is a single (64, 320) intensity field -- sky,
ridgeline, butterflies, type, rhinestones -- and the last thing render() does
is take that field through a 256-entry ramp that runs unlit -> plum -> magenta
-> hot pink -> blush -> gold -> rhinestone white. Nothing on the panel chooses
its own colour, so nothing on it can clash: a butterfly is bright, so it comes
out gold, and the ridge it rises off is dim, so it comes out the plum that the
same ramp gives darkness. That is `opencircuit.py`'s trick with a different
tube, and it is why a wall of hot pink reads as designed rather than as loud.

The hot pink stop is #FF2D95 and the gold is #FFC46E. Those two and the ramp
between them are the file's entire visual identity.

## The rhinestones are the point, and they are free

A field of forty specular glints, each a little cross, each twinkling at its
own frequency off `t`. They cost a fancy-index and a sine of a 40-element
array, they land on the top of the ramp so they come out white-hot, and they
are what makes 320x64 of gradient look like a stage costume instead of a
background. Turn them off with `--sparkle 0` and the panel is instantly a
weather graphic; that is how much of the design is in them.

## Everything is a pure function of t

`render(t, i)` never looks at the frame before it. ftsched builds a segment
ahead of time and starts it at t=0, the preview baker drives it at a fixed
step, and the wall's own loop drifts, so the callback has to be enterable at
any instant at any frame rate and land in the same picture. The butterflies
therefore fly on closed-form paths and the book stack's height is
`int(landed(t))` rather than a counter that gets incremented -- which also
means you can seek this demo, and `--at` will show you any second of it.

## The facts on it are real, and dated

  * A free book every month from birth to age five, so twelve a year and sixty
    by the time a child starts school. Those two numbers are arithmetic on the
    programme's own terms, which is why the twelve butterflies are twelve.
  * **304,539,509 books gifted since inception**, and about 3.4 million a
    month across five countries -- the USA, Canada, the UK, Australia and the
    Republic of Ireland. That is the Imagination Library's own 2025 year-end
    figure, from imaginationlibrary.com/news-resources/year-in-review, and the
    panel prints the year with it. News coverage in August 2026 puts the
    running total past 332 million; the audited year-end number with a date on
    it is the one that goes on a wall, because it is the one that will still
    be defensible in a year.

Update the two constants below when the next year-end report lands, and change
`FIGURES_YEAR` with them or the caption starts lying quietly.

## Cost

One (64, 320) float32 field, a half-resolution bloom, and one `np.take`
through a uint8 ramp into the output buffer. The type is a baked 5x7 bitmap
font carried in this file, so there is no font to be missing and no Pillow
import. Measured at 320x64: **see the table in scripts/test-dolly.py**, which
is what actually times it; the design budget it was written to is 12 ms a
frame at 24 fps on a 1.2 GHz Pi 3.

Run:  python3 dolly.py --host 127.0.0.1
      python3 dolly.py --at 47          # jump to the last card, for a photo
      python3 dolly.py --sparkle 0      # see what the rhinestones were doing
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# The Imagination Library's own 2025 year-end figures. See the docstring
# before touching these: the year goes on the panel next to the number.
BOOKS_TOTAL = "304,539,509"
BOOKS_MONTH = "3.4 MILLION A MONTH"
FIGURES_YEAR = "2025"

DATES = "DOLLY PARTON 1946-2026"


# --------------------------------------------------------------------------
# Type. A 5x7 bitmap font on a 6-column pitch, carried here rather than
# imported so the demo is one file: it draws sixty seconds of headline and a
# domain name, and a missing font file at 9:25 is a blank wall.
# --------------------------------------------------------------------------

FONT = {
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110",
    "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 11001 10101 10011 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 11011 01010",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
    " ": "00000 00000 00000 00000 00000 00000 00000",
    ".": "00000 00000 00000 00000 00000 01100 01100",
    ",": "00000 00000 00000 00000 01100 00100 01000",
    ":": "00000 01100 01100 00000 01100 01100 00000",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    "'": "00100 00100 01000 00000 00000 00000 00000",
    "!": "00100 00100 00100 00100 00100 00000 00100",
    "?": "01110 10001 00001 00010 00100 00000 00100",
    "/": "00001 00010 00010 00100 01000 01000 10000",
}

# Anything the font does not have is drawn as a hollow box rather than as a
# space: a caption that arrives with a character nobody anticipated should
# look wrong on the wall, because that is how it gets fixed.
MISSING = "11111 10001 10001 10001 10001 10001 11111"

GLYPH_W, GLYPH_H, CELL_W = 5, 7, 6


def _bank():
    out = {}
    for ch, rows in list(FONT.items()) + [(None, MISSING)]:
        g = np.zeros((GLYPH_H, GLYPH_W), bool)
        for r, bits in enumerate(rows.split()):
            for c, b in enumerate(bits):
                g[r, c] = (b == "1")
        out[ch] = g
    return out


BANK = _bank()


def bake(text, scale=1):
    """-> (7*scale, len(text)*6*scale) bool. Pixel-doubled, never resampled.

    Scaling is np.repeat, so the big type is the same glyphs as the small type
    with every row and column doubled -- a character generator running at half
    the dot clock, which is what the clock radio this panel is imitating did.
    """
    h, w = GLYPH_H * scale, max(1, len(text) * CELL_W * scale)
    out = np.zeros((h, w), bool)
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        g = BANK.get(ch, BANK[None])
        if scale > 1:
            g = np.repeat(np.repeat(g, scale, 0), scale, 1)
        x = i * CELL_W * scale
        out[:, x:x + GLYPH_W * scale] = g
    return out


def width(text, scale=1):
    return len(text) * CELL_W * scale


def fit_scale(text, scale, w):
    """The largest size up to `scale` at which `text` fits across `w`.

    The panel this was designed on is 320 wide, where a headline at scale 2 is
    26 characters and the lines below are written to that. Rather than let a
    narrower panel run a sentence off its right-hand edge -- which does not
    look broken, it looks like a sentence with the last two words missing --
    the line steps down a size. At scale 1 there is nothing left to give, and
    the caller gets 1 back: a demo that quietly dropped the line would be
    hiding the fact that this panel is too small for what it was asked to say.
    """
    while scale > 1 and width(text, scale) > w:
        scale -= 1
    return scale


def centre(text, scale, w):
    return max(0, (w - width(text, scale)) // 2)


# --------------------------------------------------------------------------
# The ramp. Unlit, plum, magenta, hot pink, blush, gold, rhinestone. Every
# pixel on the panel is one number taken through this, so the darkness has a
# colour (plum, not black) and the brightest thing in the frame is white --
# which is what a rhinestone is: not a colour, a highlight.
# --------------------------------------------------------------------------

RAMP = [
    (0.00, (0, 0, 0)),
    (0.10, (20, 3, 16)),
    (0.26, (74, 8, 54)),          # the ridge, and the dark end of the sky
    (0.46, (170, 18, 104)),
    (0.62, (255, 45, 149)),       # #FF2D95, the hot pink everything sits on
    (0.78, (255, 138, 190)),      # blush
    (0.90, (255, 196, 110)),      # #FFC46E, the gold the butterflies come out
    (1.00, (255, 248, 236)),      # rhinestone
]


# --------------------------------------------------------------------------
# Sprites. A butterfly in three wing positions and a book, hand-plotted for
# the same reason the font is: at this size a drawn shape reads and a scaled
# one does not.
# --------------------------------------------------------------------------

WINGS = [
    # Open. Nine across, which at a 320-wide panel is a butterfly you can see
    # from the far side of the room and still not a bird.
    ["110010011",
     "111011111",
     "111111111",
     "011111110",
     "001111100",
     "011010110",
     "010000010"],
    # Half.
    ["001010100",
     "011111110",
     "011111110",
     "001111100",
     "000111000",
     "001010100",
     "000010000"],
    # Up.
    ["000111000",
     "001111100",
     "001111100",
     "000111000",
     "000111000",
     "000111000",
     "000010000"],
]

# A book seen from the side: a spine, nine across and two deep. Twelve of
# them stack into a pile a foot high, which is the picture -- a face-on book
# at this size is a rectangle with a line in it, and a rectangle with a line
# in it is the font's missing-character box.
BOOK = ["111111111",
        "101111101"]


def _sprite(rows):
    g = np.zeros((len(rows), len(rows[0])), bool)
    for r, bits in enumerate(rows):
        for c, b in enumerate(bits):
            g[r, c] = (b == "1")
    return g


def add_arguments(ap):
    ap.add_argument("--loop", type=float, default=60.0,
                    help="seconds for one whole cue, gag to closing card")
    ap.add_argument("--clock", default="9:25",
                    help="what the clock reads; the joke assumes 9:25")
    ap.add_argument("--blink", type=float, default=1.0,
                    help="colon blinks per second (0 = a colon that stays put)")
    ap.add_argument("--butterflies", type=int, default=12,
                    help="one a month; twelve is a year of the programme")
    ap.add_argument("--sparkle", type=float, default=0.60,
                    help="rhinestones (0 = a weather graphic)")
    ap.add_argument("--bloom", type=float, default=0.55,
                    help="how much of the blurred field is added back")
    ap.add_argument("--at", type=float, default=-1.0,
                    help="pin the loop to one second of it, for a photograph")


# --------------------------------------------------------------------------
# The show. Four acts as fractions of --loop, so shortening the cue squeezes
# it evenly rather than truncating the ask off the end.
# --------------------------------------------------------------------------

ACTS = [
    ("clock", 0.00, 0.22),        # the gag
    ("books", 0.22, 0.56),        # butterflies, and the stack they become
    ("quote", 0.56, 0.80),
    ("ask", 0.80, 1.00),
]

QUOTE = ("FIND OUT WHO YOU ARE", "AND DO IT ON PURPOSE.")

SOURCE_LINE = "IMAGINATION LIBRARY, " + BOOKS_MONTH + ", " + FIGURES_YEAR

# Every line the panel can put up, with the size it goes up at. Here rather
# than at the call sites because a string that appears twice -- once to bake
# and once to centre -- is a string that will eventually differ in one of the
# two places, and because a line is only safe if something can measure it:
# 26 characters at scale 2 is the whole of a 320-pixel panel, and
# scripts/test-dolly.py fails the file if any of these overruns.
TEXT = {
    "gag": ("WE'RE WORKIN' 9:25", 2),
    "close": ("CLOSE ENOUGH.", 2),
    "month": ("A FREE BOOK EVERY MONTH", 2),
    "twelve": ("12 A YEAR, 60 BY AGE FIVE.", 2),
    "total": (BOOKS_TOTAL + " SO FAR", 2),
    "source": (SOURCE_LINE, 1),
    "q1": (QUOTE[0], 2),
    "q2": (QUOTE[1], 2),
    "who": ("DOLLY PARTON", 1),
    "ask": ("GIVE A KID A BOOK", 2),
    "where": ("IMAGINATIONLIBRARY.COM", 1),
    "dates": (DATES, 1),
}


def every_line():
    """(text, scale) for everything the panel draws, the clock included."""
    return list(TEXT.values())

# Seconds an act takes to dissolve in and out of the sky it shares with the
# others. Long enough to read as a dissolve, short enough that no act loses a
# word to it.
SEAM = 0.6


def build(args):
    W, H = args.width, args.height
    loop = max(8.0, args.loop)
    rng = np.random.RandomState(2925)      # fixed: the panel must be repeatable

    field = np.zeros((H, W), f32)
    out = np.empty((H, W, 3), np.uint8)
    idx = np.empty((H, W), np.uint8)
    ramp = ds.gradient(RAMP, 256, np.uint8)

    # -- the sky, baked once ------------------------------------------------
    # Dawn over a ridge: darkest at the top, warmest just above the skyline,
    # because that is where the sun is about to be. It is the same gradient at
    # 9:25 in the evening; a wall does not owe anybody an accurate sun.
    y = np.linspace(0.0, 1.0, H, dtype=f32)
    sky = (0.30 + 0.30 * y ** 1.6)[:, None] * np.ones((1, W), f32)

    # -- the ridgeline ------------------------------------------------------
    # Three summed sines, which is a mountain if you do not stare at it, and
    # the Smokies are the range in question. Baked into the sky so the frame
    # loop never draws it.
    x = np.linspace(0.0, 1.0, W, dtype=f32)
    ridge = (0.55 + 0.06 * np.sin(x * 7.1 + 0.6)
             + 0.035 * np.sin(x * 17.3 + 2.1)
             + 0.02 * np.sin(x * 31.7 + 4.0))
    ridge_y = (ridge * H).astype(np.int32)
    rows = np.arange(H, dtype=np.int32)[:, None]
    below = rows >= ridge_y[None, :]
    sky[below] = 0.16                       # the plum end of the ramp
    # A rim of light along the skyline itself. One row, and it is the whole
    # reason the silhouette reads as a mountain rather than as a crop.
    rim = (rows == ridge_y[None, :])
    sky[rim] = 0.52
    SKY = sky.copy()

    # -- rhinestones --------------------------------------------------------
    # Forty glints on fixed positions, each twinkling at its own rate off t.
    # Kept off the bottom sixteen rows, which is where every act puts type.
    n = 40
    sx = rng.randint(2, W - 2, n)
    sy = rng.randint(1, H - 18, n)
    sf = rng.uniform(0.25, 1.1, n).astype(f32)      # twinkles a second
    sp = rng.uniform(0.0, 2.0 * np.pi, n).astype(f32)

    # -- type, baked once ---------------------------------------------------
    clock_txt = args.clock
    colon_at = clock_txt.find(":")
    # The colon is baked separately from the digits so it can blink without
    # rebuilding anything: the digits are one bitmap for the whole minute.
    digits = bake(clock_txt.replace(":", " "), 4)
    colon = bake(":", 4)
    clock_x = centre(clock_txt, 4, W)
    colon_x = clock_x + (colon_at if colon_at >= 0 else 0) * CELL_W * 4

    fitted = {key: fit_scale(text, scale, W) for key, (text, scale) in TEXT.items()}
    lines = {key: bake(TEXT[key][0], fitted[key]) for key in TEXT}

    wings = [_sprite(w) for w in WINGS]
    book = _sprite(BOOK)
    BW, BH = book.shape[1], book.shape[0]

    # -- the flight ---------------------------------------------------------
    # Every butterfly is a closed-form path: it leaves the ridge at its own
    # moment, crosses to the stack on the right, and lands. Fixed offsets, so
    # the twelfth butterfly is in the same place on every playing of this cue.
    nb = max(1, int(args.butterflies))
    b_start = (rng.uniform(0.0, 0.7, nb).astype(f32)
               + np.arange(nb, dtype=f32) * 0.78)
    b_from = rng.uniform(0.04, 0.30, nb).astype(f32)      # where on the ridge
    b_bob = rng.uniform(0.6, 1.4, nb).astype(f32)
    b_ph = rng.uniform(0.0, 6.28, nb).astype(f32)
    FLIGHT = 3.6                                          # seconds to cross

    stack_x = W - BW - 22
    stack_y = H - 3                       # the pile grows up from here
    PITCH = 3                             # spine plus a hairline of shadow

    # Half-resolution bloom scratch, exactly as opencircuit does it: a halo is
    # low-frequency by definition, so a quarter of the pixels carries it.
    HH, WW = H // 2, W // 2
    small = np.empty((HH, WW), f32)
    sblur = np.empty((HH, WW), f32)
    bayer = (np.indices((H, W)).sum(0) % 2).astype(f32) * 0.5

    def act_at(tt):
        """(name, seconds in, 0..1 through, seconds long) for a loop time."""
        for name, a, b in ACTS:
            if tt < b * loop or b >= 1.0:
                span = max(1e-6, (b - a) * loop)
                local = tt - a * loop
                return name, local, min(1.0, max(0.0, local / span)), span
        return ACTS[-1][0], 0.0, 1.0, max(1e-6, (1.0 - ACTS[-1][1]) * loop)

    ENV = [1.0]                           # the act-seam envelope; see render()

    def blit(dst, mask, x0, y0, value):
        """Draw a bool sprite at (x0, y0), brightest-wins, clipped.

        `value` is scaled by the act envelope here rather than at every call
        site: an act should be written as though it were the only thing on the
        wall, and the dissolve into the next one is not its business.
        """
        value = value * ENV[0]
        h, w = mask.shape
        x0, y0 = int(x0), int(y0)
        sx0, sy0 = max(0, -x0), max(0, -y0)
        x0, y0 = max(0, x0), max(0, y0)
        w = min(w - sx0, dst.shape[1] - x0)
        h = min(h - sy0, dst.shape[0] - y0)
        if w <= 0 or h <= 0:
            return
        sub = dst[y0:y0 + h, x0:x0 + w]
        m = mask[sy0:sy0 + h, sx0:sx0 + w]
        np.maximum(sub, m * f32(value), out=sub)

    def line(dst, key, y, value, k=1.0):
        """Draw a line from TEXT, centred, optionally typed on.

        Centring is computed from the same string that was baked, which is the
        point: the call site names the line and never its text.
        """
        text = TEXT[key][0]
        reveal(dst, lines[key], centre(text, fitted[key], dst.shape[1]),
               y, value, k)

    def reveal(dst, mask, x0, y0, value, k):
        """Type a line on, column by column. k is 0..1 through the line."""
        w = int(round(mask.shape[1] * min(1.0, max(0.0, k))))
        if w > 0:
            blit(dst, mask[:, :w], x0, y0, value)

    def fade(k, up=0.12, down=0.12):
        """A 0..1 envelope over a line's own progress: on, hold, off."""
        return min(1.0, k / up, max(0.0, (1.0 - k) / down))

    def landed(tt):
        """How many butterflies have reached the stack by now."""
        arrive = b_start + FLIGHT
        return int(np.count_nonzero(arrive <= tt))

    def draw_clock(dst, tt, k):
        """The gag: a clock radio, and the two lines under it."""
        top = 6
        blit(dst, digits, clock_x, top, 0.98)
        # Blink. Half on, half off, which is what the clock in the kitchen
        # did, and it is the only thing moving for the first four seconds.
        if args.blink <= 0 or (tt * args.blink) % 1.0 < 0.5:
            blit(dst, colon, colon_x, top, 0.98)
        if k > 0.24:
            line(dst, "gag", top + 30, 0.86, (k - 0.24) / 0.22)
        if k > 0.62:
            line(dst, "close", top + 44, 0.80 * min(1.0, (k - 0.62) / 0.10))

    def draw_books(dst, tt, k):
        """Twelve butterflies, and the pile of books they turn into.

        `tt` is seconds into *this act*, not into the loop: a butterfly is a
        month and the twelve of them are the year this act is, so their clock
        starts when the act does.
        """
        H_, W_ = dst.shape
        n_down = landed(tt)
        for i in range(nb):
            age = tt - b_start[i]
            if age < 0 or age >= FLIGHT:
                continue                      # not yet, or a book by now
            p = age / FLIGHT
            x0 = b_from[i] * W_
            # Ease out into the pile, so the landing settles rather than
            # arriving at full speed and stopping dead.
            px = x0 + (stack_x - x0) * (1.0 - (1.0 - p) ** 2)
            base = ridge[min(W_ - 1, max(0, int(px)))] * H_
            top_y = stack_y - i * PITCH
            # Up off the ridge, over, and down onto the top of the pile.
            py = base + (top_y - base) * p - 15.0 * np.sin(np.pi * p)
            py += 2.2 * np.sin(age * 4.0 * b_bob[i] + b_ph[i])
            pose = wings[int(age * 9.0 * b_bob[i]) % 3]
            blit(dst, pose, px - 4, py - 3, 0.99)
        # Alternating brightness down the pile, or twelve spines read as one
        # tall block rather than as twelve books.
        for j in range(n_down):
            blit(dst, book, stack_x, stack_y - j * PITCH,
                 0.90 if j % 2 else 0.76)

        # Three captions in sequence, each typed on and held.
        if k < 0.34:
            line(dst, "month", 4, 0.90, k / 0.20)
        elif k < 0.64:
            line(dst, "twelve", 4, 0.90, (k - 0.34) / 0.16)
        else:
            line(dst, "total", 3, 0.94, (k - 0.64) / 0.16)
            if k > 0.80:
                line(dst, "source", 18, 0.66)

    def draw_quote(dst, tt, k):
        line(dst, "q1", 14, 0.94, k / 0.22)
        if k > 0.26:
            line(dst, "q2", 30, 0.94, (k - 0.26) / 0.22)
        if k > 0.58:
            line(dst, "who", 46, 0.70)

    def draw_ask(dst, tt, k):
        H_, W_ = dst.shape
        line(dst, "ask", 12, 0.96, k / 0.18)
        if k > 0.24:
            line(dst, "where", 30, 0.94 * min(1.0, (k - 0.24) / 0.12))
        if k > 0.46:
            # The dates, small, at the bottom, and they stay up until the cue
            # ends: this is the frame the wall is holding when the minute is
            # over, so it is the one that has to be right.
            line(dst, "dates", H_ - 9, 0.72 * min(1.0, (k - 0.46) / 0.14))
        # One butterfly, still going, because the programme is.
        px = 12 + (W_ - 40) * min(1.0, k / 0.9)
        py = H_ - 26 + 6.0 * np.sin(k * 9.0)
        blit(dst, wings[int(k * 26.0) % 3], px, py, 0.99)

    DRAW = {"clock": draw_clock, "books": draw_books,
            "quote": draw_quote, "ask": draw_ask}

    def render(t, i):
        tt = (args.at if args.at >= 0 else t) % loop
        np.copyto(field, SKY)

        name, local, k, span = act_at(tt)
        # In from the sky at the top of every act but the first, out into it
        # at the end of every act but the last. The last one does not dissolve
        # because its final frame is what the wall is holding when the cue
        # ends and the rotation cuts back in.
        rise = 1.0 if name == ACTS[0][0] else min(1.0, local / SEAM)
        fall = 1.0 if name == ACTS[-1][0] else min(1.0, (1.0 - k) * span / SEAM)
        ENV[0] = max(0.0, min(rise, fall))
        DRAW[name](field, local, k)

        # Rhinestones. Forty crosses, each on its own clock, added rather than
        # maxed so a glint on a bright glyph pushes it up into the white.
        if args.sparkle > 0:
            tw = 0.5 + 0.5 * np.sin(tt * 6.2831 * sf + sp)
            amp = (args.sparkle * tw ** 3).astype(f32)
            field[sy, sx] += amp
            field[sy - 1, sx] += amp * 0.45
            field[sy + 1, sx] += amp * 0.45
            field[sy, sx - 1] += amp * 0.45
            field[sy, sx + 1] += amp * 0.45

        # Bloom at half resolution: box-downsample, separable blur, add back.
        if args.bloom > 0:
            even = field[:HH * 2, :WW * 2]
            np.add(even[0::2, 0::2], even[1::2, 0::2], out=small)
            np.add(small, even[0::2, 1::2], out=small)
            np.add(small, even[1::2, 1::2], out=small)
            np.multiply(small, f32(0.25), out=small)
            np.copyto(sblur, small)
            sblur[:, 1:] += small[:, :-1] * f32(0.6)
            sblur[:, :-1] += small[:, 1:] * f32(0.6)
            sblur[1:] += small[:-1] * f32(0.5)
            sblur[:-1] += small[1:] * f32(0.5)
            np.multiply(sblur, f32(args.bloom / 2.7), out=sblur)
            wide = field[:HH * 2, :WW * 2]
            wide[0::2, 0::2] += sblur
            wide[0::2, 1::2] += sblur
            wide[1::2, 0::2] += sblur
            wide[1::2, 1::2] += sblur

        # One intensity, dithered on the ramp index rather than on the colour
        # it produces -- three channels of dither costs three times as much
        # and looks the same.
        np.multiply(field, f32(255.0), out=field)
        np.add(field, bayer, out=field)
        np.clip(field, 0.0, 255.0, out=field)
        np.copyto(idx, field, casting="unsafe")
        np.take(ramp, idx, axis=0, out=out)
        return out

    # Hung off the callback for the test script, which walks the acts.
    render.acts = ACTS
    render.loop = loop
    # Published for scripts/test-dolly.py: where the digits are, and where the
    # colon that blinks between them is. A test that guesses these coordinates
    # passes for the wrong reason the first time the clock moves.
    render.digits_box = (clock_x, 6, digits.shape[1], digits.shape[0])
    render.colon_box = (colon_x, 6, colon.shape[1], colon.shape[0])
    render.landed = landed          # books on the pile, act-local seconds in
    return render


def landed_at(render, t):
    """Books on the pile `t` seconds into the act they land in.

    A shim for scripts/test-dolly.py: the count is closed over in build(), and
    the test should not have to know that.
    """
    return render.landed(t)


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=24)


if __name__ == "__main__":
    main()
