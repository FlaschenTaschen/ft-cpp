#!/usr/bin/env python3
"""A Ceefax page for the wall: 40x8 character cells, seven colours and black.

Teletext is this wall's ancestor. It was a character display with no
half-tones, no anti-aliasing and no blending -- eight colours, being the corners
of the RGB cube, and pictures built out of a 2x3 block "mosaic" alphabet
carried in the same character slots as the letters. It looked like nothing else
because it could not look like anything else, and it carried real information
for thirty-eight years. That is the whole brief for this panel: get the *form*
exactly right, and then put honest numbers in it.

**The grid falls out of the panel.** 320 wide is exactly 40 columns of 8 px,
which is the real teletext column count. 64 tall is exactly 8 rows of 8 px --
teletext had 25, so this is a page cropped to a strip rather than a page shrunk,
and it is laid out that way deliberately: a header row, a double-height
headline over two rows, four rows of body, and the four coloured Fastext links
along the bottom. Everything that would have been in the missing seventeen rows
is on another page instead, which is exactly what a page number is for.

**One representation choice makes everything else fall out**: the page is never
pixels. It is three (8, 40) integer arrays -- a glyph index, a foreground colour
index and a background colour index per cell -- and the only pixel-producing
code in the file expands those three through a bank of 8x8 bitmaps. Text and
mosaic graphics are the same operation with different glyph indices. The
one-colour-per-cell rule that gives teletext its characteristic "colour clash"
is not simulated, it is structural: there is one integer, so there is one
colour, and a tree drawn across a blue sky really does have to choose per cell.

**The mosaic alphabet** is built, not typed: 64 entries, one per 6-bit code,
each filling the sub-rectangles of an 8x8 cell whose bit is set. The sub-rows
are 3, 3 and 2 pixels tall, because 8 does not divide by 3 -- the real SAA5050
cell was 6x10 split 3/3/4 and had the same problem in the other direction. A
sub-block is 4x3 px, so drawing in mosaic space means an 80x24 canvas over the
whole panel, and that is what every picture here is composed in.

**The data is real and its age is on screen.** Pages 102/103/104 read products
already in the ftdata cache -- met.no and the NWS station for weather, NOAA's
tide predictions and NDBC buoy 46026 for the sea, sequoia.garden's battery
telemetry for power -- at build time, from disk, never the network. Nothing is
invented: a product that is missing gets teletext's own honest idiom
("PAGE NOT AVAILABLE"), a product that is stale is drawn with its age in
yellow or red beside it, and a tide prediction whose span has run out says so
rather than extrapolating. Real teletext pages were visibly stale all the time
and dated themselves for exactly this reason. Page 100 is the index and doubles
as a freshness board for all four products; page 101 is a pure block-mosaic
station ident with no data on it at all, which is the page the medium was best
at.

**The motion is period-correct.** Pages do not fade, they flip: a page is held,
then the header's page number rolls like a search and the new page arrives in a
burst of mosaic garbage over half a second, top rows first, as a real page
arrived a packet at a time. The index page's subtitle reveals a character at a
time. Nothing else moves except the clock in the header, which is the one
legitimate wall-clock element here and ticks the real seconds.

Run:  python3 teletext.py --host 127.0.0.1
      python3 teletext.py --page 101          # just the ident, held
      python3 teletext.py --hold 4 --load 0.3 # a faster rotation
"""

import sys
import time

import numpy as np

import demoscene as ds
import ftdata
import ftsite

# --------------------------------------------------------------------------
# The grid. 40 columns of 8 px is the real teletext column count and it is
# exact here; 8 rows of 8 px is a crop of the real 25, chosen over squeezing
# seven 9 px rows in because a character cell that is not a power of two makes
# both the mosaic split and the double-height stretch messy for no gain.
# --------------------------------------------------------------------------

COLS, ROWS = 40, 8
CW, CH = 8, 8                       # character cell, pixels
SUB_Y = ((0, 3), (3, 6), (6, 8))    # the three mosaic sub-rows, 3/3/2 px
SUB_X = ((0, 4), (4, 8))            # the two mosaic sub-columns, 4 px each
MW, MH = COLS * 2, ROWS * 3         # the mosaic canvas over a whole page

# The palette, and there is no other. Full-intensity corners of the RGB cube:
# anything between them is a colour teletext could not make, so nothing here
# ever blends, dithers or anti-aliases.
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)
PAL = np.array([
    (0, 0, 0), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
], np.uint8)

DEG = "\xb0"

# --------------------------------------------------------------------------
# The font: 5x7 in an 8x8 cell, drawn here rather than measured off a system
# face. At five pixels wide a real typeface is mush, the Pi does not have the
# same faces installed as the machine this was written on, and a demo module
# must not depend on Pillow -- so the glyphs are literals and their size is
# known rather than assumed. One px of left bearing, two px to the right and
# one below, which is what gives text its spacing without a gap character.
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
    "/": "00001 00010 00010 00100 01000 01000 10000",
    "%": "11001 11010 00010 00100 01000 01011 10011",
    "'": "00100 00100 01000 00000 00000 00000 00000",
    "!": "00100 00100 00100 00100 00100 00000 00100",
    "?": "01110 10001 00001 00010 00100 00000 00100",
    "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000",
    "+": "00000 00100 00100 11111 00100 00100 00000",
    "=": "00000 00000 11111 00000 11111 00000 00000",
    "*": "00000 10101 01110 11111 01110 10101 00000",
    "<": "00010 00100 01000 10000 01000 00100 00010",
    ">": "01000 00100 00010 00001 00010 00100 01000",
    DEG: "01100 10010 01100 00000 00000 00000 00000",
}


def _build_bank():
    """One (N, 8, 8) bool bank holding every glyph the page can contain.

    Three families share it, because to the renderer they are all just an
    index: the 64 mosaic codes first, then the normal-height characters, then
    the top and bottom halves of each character stretched to double height.
    Double height in teletext is genuinely the same glyph with every scan line
    doubled, split across two character rows, and that is exactly what this
    does -- so a headline is two rows of ordinary cells, not a special case in
    the renderer.
    """
    order = sorted(FONT)
    index = {}
    n = 64 + 3 * len(order)
    bank = np.zeros((n, CH, CW), bool)

    # 0..63: the mosaic alphabet. Bit k of the code lights sub-block k, in
    # reading order: top-left, top-right, middle-left, middle-right,
    # bottom-left, bottom-right.
    for code in range(64):
        for k in range(6):
            if code & (1 << k):
                y0, y1 = SUB_Y[k // 2]
                x0, x1 = SUB_X[k % 2]
                bank[code, y0:y1, x0:x1] = True

    base = 64
    for i, ch in enumerate(order):
        rows = FONT[ch].split()
        g = np.zeros((7, 5), bool)
        for r, bits in enumerate(rows):
            for c, b in enumerate(bits):
                g[r, c] = (b == "1")
        index[ch] = base + i
        bank[base + i, 0:7, 1:6] = g
        tall = np.repeat(g, 2, axis=0)              # 14 rows
        bank[base + len(order) + i, 0:8, 1:6] = tall[0:8]
        bank[base + 2 * len(order) + i, 0:6, 1:6] = tall[8:14]
    return bank, index, len(order)


BANK, CHAR, NCHAR = _build_bank()
BLANK = CHAR[" "]
TOP_OFF, BOT_OFF = NCHAR, 2 * NCHAR


# --------------------------------------------------------------------------
# The mosaic canvas. Everything drawn as a picture on this panel is drawn
# here, in 4x3 px blocks, and then squashed into character cells -- which is
# the difference between a teletext picture and pixel art. A block is wider
# than it is tall, so anything meant to look round takes a bigger y radius.
# --------------------------------------------------------------------------

class Mosaic(object):
    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.bits = np.zeros((rows * 3, cols * 2), bool)
        self.fg = np.zeros((rows, cols), np.int16)
        self.bg = np.zeros((rows, cols), np.int16)

    def px(self, x, y, colour):
        """Light one block. The colour lands on the whole character cell.

        Last write wins, and that is the colour clash: two things drawn in one
        cell can only be one colour. Real teletext artists composed around it
        and so does every picture below.
        """
        if 0 <= x < self.cols * 2 and 0 <= y < self.rows * 3:
            self.bits[y, x] = True
            self.fg[y // 3, x // 2] = colour

    def clear(self, x, y):
        if 0 <= x < self.cols * 2 and 0 <= y < self.rows * 3:
            self.bits[y, x] = False

    def hline(self, y, x0, x1, colour):
        for x in range(min(x0, x1), max(x0, x1) + 1):
            self.px(x, y, colour)

    def vline(self, x, y0, y1, colour):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            self.px(x, y, colour)

    def rect(self, x0, y0, x1, y1, colour):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            self.hline(y, x0, x1, colour)

    def disc(self, cx, cy, rx, ry, colour):
        for y in range(int(cy - ry), int(cy + ry) + 1):
            for x in range(int(cx - rx), int(cx + rx) + 1):
                dx = (x - cx) / float(rx)
                dy = (y - cy) / float(ry)
                if dx * dx + dy * dy <= 1.0:
                    self.px(x, y, colour)

    def paper(self, r0, c0, r1, c1, colour):
        """A background colour block, at cell resolution as teletext had it."""
        self.bg[max(0, r0):r1 + 1, max(0, c0):c1 + 1] = colour

    def codes(self):
        """(rows, cols) mosaic glyph indices for the bits set."""
        out = np.zeros((self.rows, self.cols), np.int16)
        for k in range(6):
            sy, sx = k // 2, k % 2
            sub = self.bits[sy::3, sx::2]
            out[sub] |= (1 << k)
        return out


# --------------------------------------------------------------------------
# A page: three integer arrays and the one function that turns them into
# pixels. Nothing else in this file writes a pixel.
# --------------------------------------------------------------------------

class Page(object):
    def __init__(self):
        self.glyph = np.full((ROWS, COLS), BLANK, np.int16)
        self.fg = np.full((ROWS, COLS), WHITE, np.int16)
        self.bg = np.zeros((ROWS, COLS), np.int16)

    def text(self, r, c, s, fg=WHITE, bg=BLACK):
        if not (0 <= r < ROWS):
            return
        for i, ch in enumerate(s.upper()):
            x = c + i
            if not (0 <= x < COLS):
                continue
            self.glyph[r, x] = CHAR.get(ch, BLANK)
            self.fg[r, x] = fg
            self.bg[r, x] = bg

    def dtext(self, r, c, s, fg=WHITE, bg=BLACK):
        """Double height: the same glyph, stretched, over rows r and r+1."""
        for i, ch in enumerate(s.upper()):
            x = c + i
            if not (0 <= x < COLS) or not (0 <= r < ROWS - 1):
                continue
            g = CHAR.get(ch, BLANK)
            self.glyph[r, x] = g + TOP_OFF
            self.glyph[r + 1, x] = g + BOT_OFF
            self.fg[r:r + 2, x] = fg
            self.bg[r:r + 2, x] = bg

    def paper(self, r0, c0, r1, c1, colour):
        self.bg[max(0, r0):r1 + 1, max(0, c0):c1 + 1] = colour

    def blit(self, r0, c0, m):
        """Drop a Mosaic in at a cell position."""
        codes = m.codes()
        r1, c1 = min(ROWS, r0 + m.rows), min(COLS, c0 + m.cols)
        h, w = r1 - r0, c1 - c0
        sub = codes[:h, :w]
        dst = self.glyph[r0:r1, c0:c1]
        lit = sub != 0
        dst[lit] = sub[lit]
        self.fg[r0:r1, c0:c1][lit] = m.fg[:h, :w][lit]
        paper = m.bg[:h, :w] != 0
        self.bg[r0:r1, c0:c1][paper] = m.bg[:h, :w][paper]

    def rgb(self):
        """(64, 320, 3) uint8. Five numpy calls, and only at build time."""
        mask = BANK[self.glyph]                       # (ROWS, COLS, 8, 8)
        fg = PAL[self.fg][:, :, None, None, :]
        bg = PAL[self.bg][:, :, None, None, :]
        img = np.where(mask[:, :, :, :, None], fg, bg)
        return img.transpose(0, 2, 1, 3, 4).reshape(ROWS * CH, COLS * CW, 3)


# --------------------------------------------------------------------------
# Freshness, stated the way teletext would have stated it: the age is on the
# page, in a colour that says how much to trust it.
# --------------------------------------------------------------------------

def freshness(name, got):
    """-> (text, colour). '4H OLD' in yellow, 'NO DATA' in red, and so on."""
    if got is None:
        return "NO DATA", RED
    age = got[1]
    label = ftdata.describe_age(age) + " OLD"
    ttl = ftdata.ttl_for(name) or 3600.0
    if age <= ttl:
        return label, GREEN
    if age <= 4 * ttl:
        return label, YELLOW
    return label, RED


def unavailable(page, why):
    """The whole body of a page, when there is nothing honest to put in it."""
    page.dtext(2, 6, "NO DATA", RED)
    page.text(5, 6, why[:28], YELLOW)
    page.text(6, 6, "SEE PAGE 100 FOR STATUS", CYAN)


def clock_str(now):
    lt = time.localtime(now)
    return time.strftime("%H:%M/%S", lt)


def date_str(now):
    return time.strftime("%a %d %b", time.localtime(now)).upper()


def hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


# --------------------------------------------------------------------------
# The five pages.
# --------------------------------------------------------------------------

# Fastext: the four coloured links along the bottom of every real page after
# 1987. They do nothing here -- there is no remote -- but they are half of why
# a teletext page looks like a teletext page, and they label the rotation.
FASTEXT = ((0, "WEATHER", RED), (10, "SEA", GREEN),
           (20, "POWER", YELLOW), (30, "INDEX", CYAN))


def fastext(page):
    for col, label, colour in FASTEXT:
        page.text(7, col, label, colour)


def header(page, number, station, now):
    """Row 0 of every page: page number, station, date, ticking clock."""
    page.text(0, 0, "P%d" % number, WHITE)
    page.text(0, 5, station, CYAN)
    page.text(0, 18, date_str(now), WHITE)
    page.text(0, 31, clock_str(now), YELLOW)


CLOCK_COL = 31          # where clock_str() starts, for the per-frame blit


def page_index(page, states, subtitle):
    """P100 -- the contents, and the freshness board for everything here."""
    page.dtext(1, 0, "SEQUOIAFAX", YELLOW)
    page.text(1, 22, "TELETEXT FOR A", WHITE)
    page.text(2, 22, "320 BY 64 WALL", WHITE)
    page.text(3, 0, "101", WHITE)
    page.text(3, 4, "STATION IDENT", GREEN)
    page.text(3, 20, "103", WHITE)
    page.text(3, 24, "TIDE AND SEA", CYAN)
    page.text(4, 0, "102", WHITE)
    page.text(4, 4, "WEATHER", GREEN)
    page.text(4, 20, "104", WHITE)
    page.text(4, 24, "SOLAR BATTERY", CYAN)
    # The freshness board. Four products, each with its real age in the colour
    # that says whether to believe it. This is the page that makes the honesty
    # rule visible instead of merely true.
    col = 0
    for label, (age_txt, colour) in states:
        page.text(5, col, label, WHITE)
        page.text(5, col + len(label) + 1, age_txt.replace(" OLD", ""), colour)
        col += 10
    page.text(6, 0, subtitle, MAGENTA)
    fastext(page)


def page_ident(page):
    """P101 -- pure mosaic, no data. Six rows of blocks and a caption.

    A grove at sunset: three sequoias drawn as tapering stacks of blocks over a
    blue paper sky with the sun sitting behind them. Everything is generated
    from a couple of rules here -- trunk width, taper per level -- rather than
    traced from anything.
    """
    m = Mosaic(6, COLS)                      # 18 x 80 blocks
    # Sky and ground are background colour blocks, not lit blocks: teletext had
    # a paper colour per cell and using it is both cheaper and more authentic
    # than filling every sub-block. The band edges land on cell boundaries so
    # that no cell has to hold two colours and lose one of them.
    m.paper(0, 0, 3, COLS - 1, BLUE)                    # sky, rows 0..11
    m.paper(4, 0, 5, COLS - 1, GREEN)                   # ground, rows 12..17
    m.disc(64, 5, 6, 7, YELLOW)                         # the sun, low behind

    canopy_foot = 11
    for base_x, height, lean in ((12, 10, 0), (31, 11, 1), (55, 8, -1)):
        # A sequoia is columnar, not conical: the half-width goes wide fast
        # and then stops, and the top is a rounded point rather than a spire.
        top = canopy_foot - height
        for y in range(top, canopy_foot + 1):
            f = (y - top) / float(max(1, canopy_foot - top))
            half = int(0.5 + 4.0 * (f ** 0.55))
            x0 = base_x + int(lean * (1.0 - f) * 2)
            m.hline(y, x0 - half, x0 + half, GREEN)
        m.vline(base_x, 12, 13, RED)                    # trunk, on the ground
    page.blit(1, 0, m)
    page.paper(7, 0, 7, COLS - 1, BLUE)
    page.text(7, 12, "SEQUOIA FABRICA", YELLOW, BLUE)


def sky_art(m, family, night):
    """The weather symbol, in blocks: 18 x 28 of them.

    Five families, because that is as many as reads at this size from three
    metres, and the met.no symbol code is collapsed onto them.
    """
    if family in ("clear", "fair", "partly"):
        if night:
            # A crescent is four blocks wide at best and reads as a blob, so
            # the moon is a plain disc and the night is said with stars.
            m.disc(8, 4, 3, 4, YELLOW)
            for x, y in ((20, 1), (25, 5), (17, 7), (24, 12)):
                m.px(x, y, WHITE)
        else:
            m.disc(9, 5, 4, 5, YELLOW)                  # sun
            for dx, dy in ((-7, 0), (7, 0), (0, -8), (0, 8),
                           (-6, -5), (6, -5), (-6, 5), (6, 5)):
                m.px(9 + dx, 5 + dy, YELLOW)
    if family in ("partly", "cloudy", "rain", "snow"):
        colour = WHITE if family in ("partly", "cloudy") else CYAN
        m.rect(4, 12, 23, 14, colour)                   # cloud body
        m.disc(10, 10, 4, 2, colour)                    # and two bumps
        m.disc(17, 10, 3, 2, colour)
    if family == "rain":
        for x in range(7, 24, 5):
            m.vline(x, 15, 17, BLUE)
    if family == "snow":
        for x in range(7, 24, 5):
            m.px(x, 16, WHITE)
            m.px(x + 2, 17, WHITE)
    if family == "fog":
        for y in range(4, 17, 3):
            m.hline(y, 3, 25, CYAN)


def met_family(symbol):
    """met.no symbol code -> (family, night)."""
    s = (symbol or "").lower()
    night = "night" in s
    for key, fam in (("fog", "fog"), ("snow", "snow"), ("sleet", "snow"),
                     ("rain", "rain"), ("cloudy", "cloudy"),
                     ("partlycloudy", "partly"), ("fair", "partly"),
                     ("clearsky", "clear")):
        if s.startswith(key) or key in s:
            if key == "cloudy" and s.startswith("partlycloudy"):
                return "partly", night
            return fam, night
    return "cloudy", night


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(deg):
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def page_weather(page, model, obs):
    """P102 -- one temperature you can read, one symbol you can read.

    Deliberately not a second copy of wx.py: a headline number, the wind, and
    the measured station reading beside the modelled one, with both ages shown.
    """
    if model is None:
        unavailable(page, "MET.NO FORECAST NOT CACHED")
        fastext(page)
        return
    p, _ = model
    fam, night = met_family(p.get("symbol_1h"))
    m = Mosaic(6, 14)
    sky_art(m, fam, night)
    page.blit(1, 0, m)

    temp = p.get("temp_c")
    if temp is None:
        page.dtext(1, 15, "--" + DEG, RED)
    else:
        page.dtext(1, 15, "%.0f%sC" % (temp, DEG), YELLOW)
    label, colour = freshness("wx-model-%.4f_%.4f" % (ftsite.LAT, ftsite.LON),
                              model)
    page.text(1, 22, (p.get("label") or "MET.NO")[:10], WHITE)
    page.text(2, 22, label, colour)

    wind = p.get("wind_ms")
    if wind is None:
        page.text(3, 15, "WIND NOT REPORTED", CYAN)
    else:
        page.text(3, 15, "WIND %s %.1f M/S"
                  % (compass(p.get("wind_dir") or 0), wind), CYAN)
    bits = []
    if p.get("cloud_pct") is not None:
        bits.append("CLOUD %d%%" % round(p["cloud_pct"]))
    if p.get("rh_pct") is not None:
        bits.append("RH %d%%" % round(p["rh_pct"]))
    page.text(4, 15, "  ".join(bits), GREEN)

    if obs is None:
        page.text(5, 15, "NO STATION REPORT", RED)
    else:
        op, _ = obs
        olabel, ocolour = freshness("wx-obs-%s" % op.get("station", ""), obs)
        if op.get("temp_c") is None:
            page.text(5, 15, "%s NO TEMP" % op.get("station", "OBS"), RED)
        else:
            page.text(5, 15, "%s %.1f%sC"
                      % (op.get("station", "OBS"), op["temp_c"], DEG), WHITE)
            page.text(5, 15 + len(op.get("station", "OBS")) + 8, olabel,
                      ocolour)
    if p.get("pressure_hpa") is not None:
        page.text(6, 15, "PRESSURE %d HPA" % round(p["pressure_hpa"]), WHITE)
    fastext(page)


def page_sea(page, tide, buoy, now):
    """P103 -- the tide curve in blocks, and the swell in one line.

    The curve is the picture and it is real: NOAA's own predicted heights,
    sampled into 80 columns of a twelve-hour window and filled from the bottom
    like a sea. If the cached prediction has run out -- they are fetched for a
    fixed span and the fetcher can miss for days -- the window slides back to
    the end of what was actually predicted and the page says so in red, which
    is exactly what a teletext page did when its data stopped arriving.
    """
    if tide is None:
        unavailable(page, "NOAA TIDE NOT CACHED")
        fastext(page)
        return
    p, _ = tide
    curve = p.get("curve") or {}
    v = curve.get("v") or []
    t0, step = curve.get("t0"), curve.get("step") or 360.0
    if not v or t0 is None:
        unavailable(page, "TIDE CURVE EMPTY")
        fastext(page)
        return

    end = t0 + (len(v) - 1) * step
    span = 12 * 3600.0
    expired = now > end
    # Anchor: normally now sits in the middle of the window; once the
    # prediction has run out, show its last twelve hours rather than nothing.
    centre = now if not expired else end - span / 2.0
    left = centre - span / 2.0

    m = Mosaic(3, COLS)                      # 9 x 80 blocks
    lo, hi = min(v), max(v)
    rng = max(0.1, hi - lo)
    now_x = None
    for x in range(MW):
        ts = left + (x + 0.5) * span / MW
        i = int(round((ts - t0) / step))
        if 0 <= i < len(v):
            h = int(round(8.0 * (v[i] - lo) / rng))
            for y in range(8 - h, 9):
                m.px(x, y, CYAN)
        if now_x is None and ts >= now:
            now_x = x
    if now_x is not None and now_x < MW:
        for y in range(0, 9):
            m.px(now_x, y, WHITE if expired else RED)
    page.blit(4, 0, m)

    label, colour = freshness("tide-9414290", tide)
    nxt = [e for e in (p.get("extremes") or []) if e.get("t", 0) > now]
    if nxt and not expired:
        e = nxt[0]
        word = "HIGH" if e.get("type") == "H" else "LOW"
        page.dtext(1, 0, "%s %s" % (word, hhmm(e["t"])), GREEN)
        page.text(1, 16, "%.1f %s %s" % (e.get("v", 0.0),
                                         p.get("units", "FT"),
                                         p.get("datum", "")), WHITE)
    else:
        page.dtext(1, 0, "TIDE ENDED", RED)
        page.text(1, 16, "PREDICTION RAN OUT", RED)
    page.text(2, 16, (p.get("name") or "TIDE")[:13], CYAN)
    page.text(2, 30, label, colour)

    if buoy is None:
        page.text(3, 0, "BUOY 46026 NOT CACHED", RED)
    else:
        bp, _ = buoy
        sw = bp.get("swell") or {}
        blabel, bcolour = freshness("ndbc-46026", buoy)
        parts = []
        if sw.get("h") is not None:
            parts.append("SWELL %.1fM" % sw["h"])
        if sw.get("p") is not None:
            parts.append("%.0fS" % sw["p"])
        if sw.get("pt"):
            parts.append(str(sw["pt"]))
        if bp.get("wtmp") is not None:
            parts.append("SEA %.0f%sC" % (bp["wtmp"], DEG))
        page.text(3, 0, " ".join(parts) if parts else "NO SWELL REPORTED",
                  GREEN)
        page.text(3, 30, blabel, bcolour)
    fastext(page)


def page_power(page, solar):
    """P104 -- the space's own battery: state of charge, and a day of volts.

    solar.py draws this day at length; this is the teletext summary of it, so
    the picture is the one series with structure in it (the terminal voltage)
    and the numbers are the ones somebody would actually repeat out loud.
    """
    if solar is None:
        unavailable(page, "SEQUOIA.GARDEN NOT CACHED")
        fastext(page)
        return
    p, _ = solar
    soc = p.get("soc_pct")
    page.dtext(1, 0, "%d%%" % round(soc) if soc is not None else "--%",
               GREEN if (soc or 0) >= 60 else YELLOW)
    page.text(1, 6, (p.get("status") or "")[:12].upper(), YELLOW)
    page.text(2, 6, (p.get("site") or "BATTERY")[:14].upper(), CYAN)
    label, colour = freshness("solar-garden", solar)
    page.text(1, 26, "BATTERY VOLTS", WHITE)
    page.text(2, 26, "LAST 24 HOURS", WHITE)

    volts = [x for x in (p.get("volt") or []) if x is not None]
    if volts:
        m = Mosaic(3, COLS)
        series = p.get("volt") or []
        lo, hi = min(volts), max(volts)
        rng = max(0.05, hi - lo)
        n = len(series)
        for x in range(MW):
            i = int(x * n / float(MW))
            val = series[i] if i < n else None
            if val is None:
                continue
            h = int(round(8.0 * (val - lo) / rng))
            for y in range(8 - h, 9):
                m.px(x, y, GREEN if x < MW - 2 else YELLOW)
        page.blit(4, 0, m)
    else:
        page.text(4, 0, "NO VOLTAGE HISTORY", RED)

    line = []
    if p.get("v") is not None:
        line.append("%.2fV" % p["v"])
    if p.get("i_ma") is not None:
        line.append("%.2fA IN" % (p["i_ma"] / 1000.0))
    if p.get("load_w") is not None:
        line.append("LOAD %.1fW" % p["load_w"])
    page.text(3, 0, "  ".join(line)[:32], YELLOW)
    page.text(3, 34, label, colour)
    fastext(page)


# --------------------------------------------------------------------------
# Options and build.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--hold", type=float, default=7.0,
                    help="seconds a page stays up")
    ap.add_argument("--load", type=float, default=0.5,
                    help="seconds of arriving-page noise between pages")
    ap.add_argument("--page", type=int, default=0,
                    help="show only this page (100..104), held")
    ap.add_argument("--cps", type=float, default=14.0,
                    help="characters per second on the revealed subtitle")
    ap.add_argument("--at", type=float, default=0.0,
                    help="pin the clock to this epoch (0 = the real one)")
    ap.add_argument("--seed", type=int, default=1974,
                    help="the arriving-page noise")


SUBTITLE = "40 COLUMNS 8 ROWS 7 COLOURS AND BLACK"


def build(args):
    W, H = args.width, args.height
    rng = np.random.RandomState(args.seed & 0x7FFFFFFF)
    now = args.at or time.time()

    # Everything the pages read, read once, here, from disk. No network, and
    # nothing below this line can fail on a missing product: load() returns
    # None and every page has a None branch.
    wx_name = "wx-model-%.4f_%.4f" % (ftsite.LAT, ftsite.LON)
    model = ftdata.load(wx_name)
    obs = None
    for station in ("SFOC1", "FTPC1", "OKXC1"):
        obs = ftdata.load("wx-obs-%s" % station)
        if obs is not None:
            break
    tide = ftdata.load("tide-9414290")
    buoy = ftdata.load("ndbc-46026")
    solar = ftdata.load("solar-garden")

    station_name = (ftsite.NAME.split()[0][:7] + "FAX").upper()

    states = [("WX", freshness(wx_name, model)),
              ("TIDE", freshness("tide-9414290", tide)),
              ("SEA", freshness("ndbc-46026", buoy)),
              ("PWR", freshness("solar-garden", solar))]

    def make(number):
        page = Page()
        header(page, number, station_name, now)
        if number == 100:
            page_index(page, states, SUBTITLE)
        elif number == 101:
            page_ident(page)
        elif number == 102:
            page_weather(page, model, obs)
        elif number == 103:
            page_sea(page, tide, buoy, now)
        else:
            page_power(page, solar)
        return page

    numbers = [100, 101, 102, 103, 104]
    if args.page:
        numbers = [args.page if args.page in numbers else 100]
    pages = [make(n) for n in numbers]

    # Baked once: the finished pages, and the frames of the flip. A page
    # arriving over the air came in a packet at a time from the top, with the
    # rows that had not arrived yet holding whatever was in the buffer -- so
    # each flip frame is the real page above a line and mosaic garbage below.
    NOISE = 3
    frames = []
    for page in pages:
        frames.append([page.rgb()])
    load_frames = []
    for i, page in enumerate(pages):
        seq = []
        for k in range(NOISE):
            g = Page()
            g.glyph[:] = page.glyph
            g.fg[:] = page.fg
            g.bg[:] = page.bg
            arrived = 1 + int((k + 1) * (ROWS - 1) / float(NOISE + 1))
            junk = (ROWS - arrived, COLS)
            g.glyph[arrived:] = rng.randint(1, 64, junk)
            g.fg[arrived:] = rng.randint(1, 8, junk)
            g.bg[arrived:] = 0
            # The page number rolls while the set searches, which is the other
            # half of what a flip looked like.
            roll = "P%d" % (100 + rng.randint(0, 900))
            g.text(0, 0, roll, WHITE)
            seq.append(g.rgb())
        load_frames.append(seq)

    # The clock. Six of the eight characters change, so they are pre-rendered
    # as cells and blitted; nothing is re-laid-out per frame.
    digits = []
    for d in "0123456789":
        cell = Page()
        cell.text(0, 0, d, YELLOW)
        digits.append(cell.rgb()[0:CH, 0:CW].copy())
    clock_x = [CLOCK_COL * CW + i * CW for i in (0, 1, 3, 4, 6, 7)]

    reveal_row = 6 if 100 in numbers else -1
    reveal_index = numbers.index(100) if 100 in numbers else -1

    hold, load = max(0.5, args.hold), max(0.0, args.load)
    period = hold + load
    total = period * len(pages)

    out = np.zeros((H, W, 3), np.uint8)
    ph, pw = min(H, ROWS * CH), min(W, COLS * CW)
    live_clock = not args.at

    def render(t, frame):
        tt = t % total
        i = int(tt / period)
        if i >= len(pages):
            i = len(pages) - 1
        phase = tt - i * period
        if load > 0 and phase < load:
            src = load_frames[i][min(len(load_frames[i]) - 1,
                                     int(phase / load * len(load_frames[i])))]
        else:
            src = frames[i][0]
        out[:ph, :pw] = src[:ph, :pw]

        # The subtitle reveals a character at a time, by cutting the row it
        # was baked into -- the text is never re-rendered.
        if i == reveal_index and phase >= load:
            n = int((phase - load) * args.cps)
            if n < len(SUBTITLE):
                x = min(pw, n * CW)
                out[reveal_row * CH:reveal_row * CH + CH, x:pw] = 0

        # The clock is the one wall-clock element on the panel, and it is only
        # six cells of it. During a flip the page is still arriving, so the
        # header is not there to update.
        if phase >= load or load <= 0:
            wall = time.time() if live_clock else (args.at + t)
            lt = time.localtime(wall)
            hh, mm, ss = lt.tm_hour, lt.tm_min, lt.tm_sec
            for x, d in zip(clock_x, (hh // 10, hh % 10, mm // 10, mm % 10,
                                      ss // 10, ss % 10)):
                if x + CW <= pw:
                    out[0:CH, x:x + CW] = digits[d]
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
