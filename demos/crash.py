#!/usr/bin/env python3
"""A gallery of famous computer deaths, each held long enough to be recognised.

Five screens a generation learned to dread, drawn as faithfully as 320x64
allows and captioned like exhibits: the C64's `?SYNTAX ERROR`, the Sad Mac,
the Amiga's Guru Meditation, the Windows blue screen, and a Linux kernel
panic. They cycle on a timer, one every eight and a half seconds, and a
museum label sits along the bottom of the panel naming the specimen and its
year for the whole time it is up.

**The label is not decoration, it is the safety catch.** A convincing blue
screen on a wall in a makerspace makes somebody think the wall has crashed and
go looking for whoever runs it. That is half the joke and it is also a support
call, so the demo carries a tell that reads in well under two seconds: a
persistent caption, in a typeface and a bone-white colour that belongs to none
of the specimens, in the same place every time, sitting on a dark plinth with a
hairline rule above it. It doubles as the thing that makes the panel
interesting rather than merely nostalgic -- most people can name two of these
five and learn three. The other two tells come free: the screens visibly change
every few seconds, which no real crash does, and the plinth reads as a matte
around an exhibit. Of the three, the caption is the one doing the work.

**Each specimen is rendered at its native column count, and the column count
picks the font.** That single decision settles every layout question in the
file. A C64 is a 40-column machine, so it gets an 8x8 cell -- 320 divides by 8
exactly -- and the chunky glyphs are the whole reason anybody recognises it.
Everything else here is an 80-column screen, so it gets a 4 px cell and 78
columns inside a small margin, which is the *same proportion* a 640-wide Amiga
or VGA screen gives 80 columns of 8x8 text. That is why the guru box comes out
occupying roughly the same fraction of the width it does on real hardware
rather than being eyeballed, and why the `*** STOP:` line -- the one line
everybody can picture -- fits on one line, as it must.

**Both fonts are bitmaps written out in this file.** Not a TrueType lookup:
the Pi driving this wall does not have the same faces installed as the machine
this was written on, a fallback face is a different metric, and at six pixels
of cap height an antialiased edge is a smudge rather than a letter. More to the
point, DejaVu Sans Mono at 8 px is not a C64 and no amount of thresholding
makes it one. The 8x8 set is the Commodore ROM shape (the flat-topped `A`, the
open `G`, digits with a slab `1`); the 4 px set is a 3x5 body with real
ascenders and a descender row, because a blue screen set in small capitals is
instantly wrong and mixed case is most of what makes it read as Windows.

**Colours are looked up, not guessed.** VIC-II blue is 0x352879 and light blue
0x6C5EB5 (the Pepto measurements, which is what every emulator ships); the blue
screen ground is VGA 0x0000AA exactly; the Linux console is VGA light grey
0xAAAAAA, which is *not* white and is the difference between a panic and a
blue screen in monochrome; the guru is pure red on black. Getting one of these
wrong is the most visible possible failure for this demo.

**Only two things move, and both are period-correct.** The guru's border
flashes, and the C64's cursor blinks -- at a third of a second on, a third
off, which is what a 60 Hz jiffy counter toggling every twenty frames gives
you. Everything else is dead still, because these screens are static by nature
and the stillness is what makes them read as death rather than as a
screensaver. Between specimens the picture collapses to a bright horizontal
line and the next one opens back out of it, a CRT losing and finding its
vertical, over about half a second.

**It is almost free to draw.** Every specimen is baked in build() as a
complete 64x320 frame, blink variants included, so a held frame is one memcpy
and nothing else -- two numpy calls including the buffer copy. The collapse
costs a row gather and a multiply on top of that, for a third of a second in
nine. Seven baked frames is 430 KB of RAM, which is the trade being made and
it is a good one on a machine where an operation costs more than a page of
pixels.

Nothing here reads the clock: the specimen order, and the hex codes in the
guru, the Sad Mac and the STOP line, are all drawn from --seed in build().

Run:  python3 crash.py --host 127.0.0.1
      python3 crash.py --only guru --hold 30
      python3 crash.py --shuffle --seed 7 --hold 6
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The 8x8 font: Commodore ROM shapes, for the 40-column specimens.
#
# Eight bytes a glyph, high bit leftmost, written as hex. The rightmost column
# and the bottom row are blank in almost every glyph, which is where the
# character cell's spacing comes from -- exactly as it did in the ROM, and the
# reason C64 text has that particular airy look rather than being tightly set.
# --------------------------------------------------------------------------

_CHUNKY_SRC = {
    "A": "183C667E66666600", "B": "7C66667C66667C00", "C": "3C66606060663C00",
    "D": "786C6666666C7800", "E": "7E60607860607E00", "F": "7E60607860606000",
    "G": "3C66606E66663C00", "H": "6666667E66666600", "I": "3C18181818183C00",
    "J": "1E0C0C0C0C6C3800", "K": "666C7870786C6600", "L": "6060606060607E00",
    "M": "63777F6B63636300", "N": "66767E7E6E666600", "O": "3C66666666663C00",
    "P": "7C66667C60606000", "Q": "3C666666663C0E00", "R": "7C66667C786C6600",
    "S": "3C66603C06663C00", "T": "7E18181818181800", "U": "6666666666663C00",
    "V": "66666666663C1800", "W": "6363636B7F776300", "X": "66663C183C666600",
    "Y": "6666663C18181800", "Z": "7E060C1830607E00",
    "0": "3C666E7666663C00", "1": "1818381818187E00", "2": "3C66060C30607E00",
    "3": "3C66061C06663C00", "4": "060E1E667F060600", "5": "7E607C0606663C00",
    "6": "3C66607C66663C00", "7": "7E660C1818181800", "8": "3C66663C66663C00",
    "9": "3C66663E06663C00",
    " ": "0000000000000000", ".": "0000000000181800", ",": "0000000018183000",
    ":": "0000180000180000", ";": "0000180000181830", "!": "1818181800181800",
    "?": "3C66060C18001800", '"': "6666660000000000", "'": "1818180000000000",
    "*": "00663CFF3C660000", "(": "0C18303030180C00", ")": "30180C0C0C183000",
    "-": "0000007E00000000", "+": "0018187E18180000", "/": "0003060C18306000",
    "=": "00007E007E000000", "#": "6666FF66FF666600", "$": "183E603C067C1800",
    "%": "62660C1830664600", "&": "3C663C3867663F00", "_": "00000000000000FF",
    "[": "3C30303030303C00", "]": "3C0C0C0C0C0C3C00", "<": "0E18306030180E00",
    ">": "70180C060C187000", "@": "3C666E6E603C0000", "█": "FFFFFFFFFFFFFFFF",
}


def _chunky_glyph(ch):
    """(8, 8) uint8 mask for one character of the 8x8 font."""
    hexs = _CHUNKY_SRC.get(ch.upper(), _CHUNKY_SRC[" "]).replace(" ", "")
    g = np.zeros((8, 8), np.uint8)
    for r in range(8):
        v = int(hexs[r * 2:r * 2 + 2], 16)
        for c in range(8):
            if v & (0x80 >> c):
                g[r, c] = 1
    return g


def chunky_mask(s):
    """(8, 8n) uint8 mask for a string in the 8x8 font. Cell pitch is 8."""
    out = np.zeros((8, max(1, len(s)) * 8), np.uint8)
    for i, ch in enumerate(s):
        out[:, i * 8:i * 8 + 8] = _chunky_glyph(ch)
    return out


# --------------------------------------------------------------------------
# The 4 px font: a 3x5 body with a descender row, for the 80-column specimens
# and for the museum label.
#
# Six octal digits a glyph, one per row, three bits wide -- the encoding
# defcon.py uses for its readouts, extended here with a sixth row so that g, j,
# p, q and y can hang below the baseline, and with a lowercase set. Lowercase
# matters: capitals occupy rows 0-4, x-height letters rows 2-4, ascenders rows
# 0-4. A blue screen set in small capitals is instantly, obviously wrong, and
# the ragged ascender rhythm of mixed case is most of what makes a wall of
# 3-pixel-wide text read as English rather than as noise.
# --------------------------------------------------------------------------

_TINY = {
    "A": "257550", "B": "656560", "C": "344430", "D": "655560", "E": "746470",
    "F": "746440", "G": "345530", "H": "557550", "I": "722270", "J": "111520",
    "K": "556550", "L": "444470", "M": "577550", "N": "655550", "O": "255520",
    "P": "656440", "Q": "255730", "R": "656550", "S": "342160", "T": "722220",
    "U": "555570", "V": "555520", "W": "557750", "X": "552550", "Y": "552220",
    "Z": "714470",
    "a": "006570", "b": "446560", "c": "003430", "d": "113530", "e": "002730",
    "f": "127220", "g": "003536", "h": "446550", "i": "202220", "j": "101116",
    "k": "456650", "l": "622230", "m": "007750", "n": "006550", "o": "002520",
    "p": "006564", "q": "003531", "r": "003440", "s": "003260", "t": "272230",
    "u": "005530", "v": "005520", "w": "005770", "x": "005250", "y": "005536",
    "z": "007270",
    "0": "755570", "1": "262270", "2": "717470", "3": "717170", "4": "557110",
    "5": "747170", "6": "747570", "7": "712220", "8": "757570", "9": "757170",
    " ": "000000", ".": "000020", ",": "000024", ":": "002020", ";": "002024",
    "!": "222020", "?": "612020", "'": "220000", '"': "550000", "-": "007000",
    "_": "000007", "+": "027200", "=": "007070", "*": "052500", "/": "011244",
    "\\": "044211", "(": "122210", ")": "422240", "[": "322230", "]": "622260",
    "<": "124210", ">": "421240", "#": "575750", "%": "512450", "&": "252530",
    "@": "257430", "$": "236320", "^": "250000", "~": "003600",
}


def _tiny_glyph(ch):
    """(6, 3) uint8 mask for one character of the 4 px font."""
    rows = _TINY.get(ch, _TINY.get(ch.upper(), _TINY[" "]))
    g = np.zeros((6, 3), np.uint8)
    for r in range(6):
        v = int(rows[r], 8)
        for c in range(3):
            if v & (4 >> c):
                g[r, c] = 1
    return g


def tiny_mask(s):
    """(6, 4n) uint8 mask for a string in the 4 px font. Cell pitch is 4."""
    out = np.zeros((6, max(1, len(s)) * 4), np.uint8)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _tiny_glyph(ch)
    return out


def tiny_width(s):
    """Ink width of a string in the 4 px font, without the trailing gap."""
    return max(0, len(s) * 4 - 1)


# --------------------------------------------------------------------------
# Drawing primitives. All of these clip, because the panel is 320x64 but the
# demo must not explode if somebody runs it at 128x32 from the control panel.
# --------------------------------------------------------------------------

def blit(img, mask, x, y, colour):
    """Paint `colour` where `mask` is set, with the mask's top-left at (x, y)."""
    h, w = img.shape[:2]
    mh, mw = mask.shape
    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    dw, dh = min(mw - sx0, w - dx0), min(mh - sy0, h - dy0)
    if dw <= 0 or dh <= 0:
        return
    sub = mask[sy0:sy0 + dh, sx0:sx0 + dw].astype(bool)
    img[dy0:dy0 + dh, dx0:dx0 + dw][sub] = colour


def rect(img, x0, y0, x1, y1, colour, fill=False, thick=1):
    """An inclusive rectangle, outline or filled."""
    h, w = img.shape[:2]
    x0, x1 = max(0, x0), min(w - 1, x1)
    y0, y1 = max(0, y0), min(h - 1, y1)
    if x1 < x0 or y1 < y0:
        return
    if fill:
        img[y0:y1 + 1, x0:x1 + 1] = colour
        return
    img[y0:min(y1 + 1, y0 + thick), x0:x1 + 1] = colour
    img[max(y0, y1 + 1 - thick):y1 + 1, x0:x1 + 1] = colour
    img[y0:y1 + 1, x0:min(x1 + 1, x0 + thick)] = colour
    img[y0:y1 + 1, max(x0, x1 + 1 - thick):x1 + 1] = colour


# --------------------------------------------------------------------------
# The palettes. Every one of these is a documented value, not a taste
# judgement, and the comments say where each came from.
# --------------------------------------------------------------------------

# VIC-II colours 6 (blue) and 14 (light blue) as measured by Philip Timmermann
# ("Pepto") off a real 6569 in 2001 -- the numbers every emulator ships. The
# default C64 screen is colour 6 inside a colour 14 border with colour 14 text,
# which is why the whole machine is remembered as "two blues".
C64_BG = (53, 40, 121)
C64_FG = (108, 94, 181)

# The Amiga alert is pure red on black. Nothing subtler: the Denise chip is
# putting up a 4-bit screen from a ROM routine that has already given up on
# the display database.
GURU_RED = (255, 0, 0)

# VGA text attribute 1 (blue) is 0x0000AA and 15 (bright white) is 0xFFFFFF.
# The bugcheck screen is white on blue, not the light grey 7 that the rest of
# the text console uses, and the difference is visible side by side.
BSOD_BG = (0, 0, 170)
BSOD_FG = (255, 255, 255)

# The Linux framebuffer console is VGA light grey, attribute 7 -- 0xAAAAAA.
# Drawing a panic in white is the single most common mistake in a recreation
# and it makes the screen look like a blue screen that lost its background.
PANIC_FG = (170, 170, 170)

# The Sad Mac is white on black: the ROM's failure path clears the screen and
# draws the icon lit, the inverse of the happy Mac you get when it works.
MAC_FG = (238, 238, 238)

# The museum's own colours, which belong to no specimen on purpose. Bone on
# near-black with a hairline rule -- a caption card under an object, and the
# one part of the panel that is the same in every frame of the loop.
LABEL_FG = (198, 184, 152)
LABEL_DIM = (118, 108, 88)
LABEL_BG = (8, 8, 10)
LABEL_RULE = (74, 68, 56)


# --------------------------------------------------------------------------
# The specimens.
#
# Real bugcheck codes, real Amiga alert numbers, real Sad Mac codes. The
# seeded generator picks which one of each is showing, so a rerun with the same
# seed is the same exhibition and a different seed is a different one, but
# nothing in here is invented -- an invented STOP code is the sort of thing
# somebody in a makerspace will notice.
# --------------------------------------------------------------------------

# (STOP code, bugcheck name). All eight are genuine NT bugchecks.
BUGCHECKS = [
    (0x0000000A, "IRQL_NOT_LESS_OR_EQUAL"),
    (0x0000001E, "KMODE_EXCEPTION_NOT_HANDLED"),
    (0x00000024, "NTFS_FILE_SYSTEM"),
    (0x00000050, "PAGE_FAULT_IN_NONPAGED_AREA"),
    (0x0000007B, "INACCESSIBLE_BOOT_DEVICE"),
    (0x0000009F, "DRIVER_POWER_STATE_FAILURE"),
    (0x000000D1, "DRIVER_IRQL_NOT_LESS_OR_EQUAL"),
    (0x000000ED, "UNMOUNTABLE_BOOT_VOLUME"),
]

# Amiga alert codes. The high word is the subsystem and severity: 0x00000003
# is exec's "software error" (a dead task), 0x80000004 a 68000 divide by zero,
# 0x8000000B an address error, 0x81000009 graphics.library out of memory.
# The word after the dot is the address of the task that died.
GURU_CODES = [0x00000003, 0x00000004, 0x80000003, 0x8000000B,
              0x81000009, 0x35000000, 0x0100000F]

# Sad Mac codes as the 128K/512K/Plus ROMs report them: the top word is the
# test class and the bottom the failure. 0x0000000F is the "hardware
# exception" catch-all everybody remembers; 01, 02, 03, 04 and 05 are the ROM
# checksum and the three RAM tests.
SADMAC_CODES = ["0000000F", "00000001", "00000002", "00000003",
                "00000004", "0000000B", "0F000064"]


def add_arguments(ap):
    ap.add_argument("--hold", type=float, default=8.0,
                    help="seconds each specimen is held; five of them plus the "
                         "collapses is the loop")
    ap.add_argument("--gap", type=float, default=0.55,
                    help="seconds of CRT collapse between specimens; 0 cuts")
    ap.add_argument("--only", default="",
                    choices=("", "c64", "sadmac", "guru", "bsod", "panic"),
                    help="show one specimen and nothing else")
    ap.add_argument("--shuffle", action="store_true",
                    help="order the specimens by --seed instead of by year")
    ap.add_argument("--no-label", dest="label", action="store_false",
                    default=True,
                    help="drop the museum caption. Do not do this on a wall "
                         "anybody walks past: the caption is what stops a "
                         "blue screen being mistaken for a real one")
    ap.add_argument("--blink", type=float, default=1.0,
                    help="scale on the guru border flash and the C64 cursor; "
                         "0 freezes both")
    ap.add_argument("--seed", type=int, default=1,
                    help="picks the hex codes, and the order under --shuffle; "
                         "0 draws a random one")


# --------------------------------------------------------------------------
# One function per specimen. Each returns a list of (H - plinth, W, 3) uint8
# frames -- one if it is still, two if something on it blinks -- plus the
# caption that goes on the plinth underneath it.
# --------------------------------------------------------------------------

def _c64(W, H, rng):
    """The 1982 boot screen, one typo in, in 39 columns of 8x8 cells.

    The border is the reason this needs a margin at all. A C64 without its
    lighter border is a blue rectangle with text on it and could be anything;
    with it, it is unmistakable from across a room, and it costs four pixels
    each side -- 39 columns rather than 40, which nobody has ever counted.
    """
    img = np.empty((H, W, 3), np.uint8)
    img[:] = C64_FG                                   # the border, full bleed
    pad = 4
    rect(img, pad, pad, W - 1 - pad, H - 1 - pad, C64_BG, fill=True)

    cols = max(1, (W - 2 * pad) // 8)
    rows_n = max(1, (H - 2 * pad) // 8)
    # The banner's leading spaces are the real ones; the RAM line's single
    # leading space is real too, and both are what make the screen look
    # slightly off-centre in the way it actually is.
    lines = [
        "   **** COMMODORE 64 BASIC V2 ****",
        " 64K RAM SYSTEM  38911 BASIC BYTES FREE",
        "PRNT \"HELLO WORLD\"",
        "?SYNTAX ERROR",
        "READY.",
        "",
    ]
    lines = lines[-rows_n:] if len(lines) > rows_n else lines
    y0 = pad + (H - 2 * pad - rows_n * 8) // 2
    for r, line in enumerate(lines[:rows_n]):
        blit(img, chunky_mask(line[:cols]), pad, y0 + r * 8, C64_FG)

    # The cursor sits at the start of the line after READY., and it is a solid
    # inverted character cell, not a caret. The C64 toggles it every twenty
    # frames of a 60 Hz field: a third of a second on, a third off.
    off = img.copy()
    cy = y0 + (len(lines) - 1) * 8
    rect(img, pad, cy, pad + 7, cy + 7, C64_FG, fill=True)
    return [img, off], "COMMODORE 64 - ?SYNTAX ERROR, 1982"


def _sadmac(W, H, rng):
    """The 1984 power-on self test failing, drawn rather than typed.

    The only specimen here that is a picture. It is generated from a
    description -- body, screen, two X eyes, a frown, the badge and the floppy
    slot -- at 44x40, which is about the fraction of the panel the 32-pixel
    icon occupied on a 512x342 Mac screen. The emptiness around it is not a
    layout failure, it is what the screen looked like.
    """
    img = np.zeros((H, W, 3), np.uint8)
    code = SADMAC_CODES[int(rng.integers(0, len(SADMAC_CODES)))]

    icon = np.zeros((40, 44), np.uint8)

    def r(x0, y0, x1, y1, fill=False):
        blk = np.ones((y1 - y0 + 1, x1 - x0 + 1), np.uint8)
        if not fill and blk.shape[0] > 2 and blk.shape[1] > 2:
            blk[1:-1, 1:-1] = 0
        icon[y0:y1 + 1, x0:x1 + 1] |= blk

    r(0, 0, 43, 39)                                   # the body
    icon[0, 0:2] = icon[0, 42:44] = 0                 # rounded corners
    icon[1, 0] = icon[1, 43] = 0
    icon[39, 0:2] = icon[39, 42:44] = 0
    icon[38, 0] = icon[38, 43] = 0
    r(6, 4, 37, 25)                                   # the screen bezel

    for ex in (12, 25):                               # two X eyes
        for k in range(6):
            icon[10 + k, ex + k] = 1
            icon[10 + k, ex + 5 - k] = 1
    # The mouth turns *down* at the ends. Drawn the other way up it is a
    # perfectly good happy Mac, which is the opposite of this demo.
    icon[20, 17:27] = 1
    icon[21, 15:17] = 1
    icon[21, 27:29] = 1

    r(6, 30, 15, 34)                                  # the badge
    r(24, 31, 37, 33, fill=True)                      # the floppy slot

    ix = (W - 44) // 2
    blit(img, icon, ix, max(0, (H - 52) // 2), MAC_FG)

    m = chunky_mask(code)
    blit(img, m, (W - m.shape[1]) // 2, max(0, (H - 52) // 2) + 44, MAC_FG)
    return [img], "MACINTOSH 128K - SAD MAC $%s, 1984" % code[-4:]


def _guru(W, H, rng):
    """The 1985 alert: a red box, two lines of Topaz, and a flashing border.

    The alert is a box the width of the screen and a quarter of its height,
    drawn by a ROM routine that has stopped trusting anything else, and its
    outline flashes. Both lines are the real strings; only the two hex words
    change, and they change from --seed.
    """
    code = int(GURU_CODES[int(rng.integers(0, len(GURU_CODES)))])
    addr = int(rng.integers(0, 1 << 30)) | 0x00C00000
    l1 = "Software Failure.  Press left mouse button to continue."
    l2 = "Guru Meditation #%08X.%08X" % (code, addr)

    off = np.zeros((H, W, 3), np.uint8)
    x0, x1 = 5, W - 6
    bh = min(H - 6, 42)
    y0 = (H - bh) // 2
    y1 = y0 + bh - 1
    m1, m2 = tiny_mask(l1), tiny_mask(l2)
    # The two lines sit in the upper half of the box on a real alert, not
    # centred in it -- there is a third line's worth of empty red-bordered
    # black underneath, and that dead space is part of the shape.
    ty = y0 + max(3, (bh - 20) // 2)
    blit(off, m1, (W - tiny_width(l1)) // 2, ty, GURU_RED)
    blit(off, m2, (W - tiny_width(l2)) // 2, ty + 12, GURU_RED)
    on = off.copy()
    rect(on, x0, y0, x1, y1, GURU_RED, thick=2)
    return [on, off], "AMIGA OS 1.3 - GURU MEDITATION, 1985"


def _bsod(W, H, rng):
    """The 2001 bugcheck, in 78 columns of 4 px text on VGA blue.

    Eight lines at a seven pixel pitch is exactly 56 rows, which is the panel
    minus the plinth, so the screen is full-bleed top to bottom the way it is
    on real hardware. The wrap points are the ones an 80-column screen gives
    and the `*** STOP:` line stays on one line, which is the point of using a
    4 px cell at all.
    """
    code, name = BUGCHECKS[int(rng.integers(0, len(BUGCHECKS)))]
    params = [int(rng.integers(0, 1 << 24)) for _ in range(4)]
    params[1] &= 0xFF
    params[3] |= 0xF0000000
    img = np.empty((H, W, 3), np.uint8)
    img[:] = BSOD_BG
    lines = [
        "A problem has been detected and Windows has been shut down to prevent",
        "damage to your computer.",
        "",
        name,
        "",
        "If this is the first time you've seen this Stop error screen, restart",
        "your computer. If this screen appears again, follow these steps:",
        "*** STOP: 0x%08X (0x%08X,0x%08X,0x%08X,0x%08X)"
        % (code, params[0], params[1], params[2], params[3]),
    ]
    pitch = 7
    y = max(0, (H - len(lines) * pitch) // 2)
    for i, line in enumerate(lines):
        blit(img, tiny_mask(line), 4, y + i * pitch, BSOD_FG)
    return [img], "WINDOWS XP - STOP 0x%08X, 2001" % code


def _panic(W, H, rng):
    """The kernel giving up, in VGA light grey on black.

    A panic is a scroll of text with no framing at all -- no box, no colour,
    no acknowledgement that a human is reading it -- and the recognisable
    parts are the first line, the call trace with its `+0x6b/0x83` offsets, and
    the `---[ end Kernel panic ... ]---` bracket that closes it. The offsets
    are seeded so the trace is not the same twice.
    """
    img = np.zeros((H, W, 3), np.uint8)

    def off():
        return "+0x%x/0x%x" % (int(rng.integers(3, 0x160)),
                               int(rng.integers(0x40, 0x300)))

    lines = [
        "Kernel panic - not syncing: Attempted to kill init! exitcode=0x00000009",
        "CPU: 0 PID: 1 Comm: init Not tainted 5.10.0-21-amd64 #1",
        "Hardware name: Gigabyte GA-MA78GM-S2H, BIOS F12 08/02/2010",
        "Call Trace:",
        " dump_stack" + off(),
        " panic" + off(),
        " do_exit.cold" + off(),
        "---[ end Kernel panic - not syncing: Attempted to kill init! ]---",
    ]
    pitch = 7
    y = max(0, (H - len(lines) * pitch) // 2)
    for i, line in enumerate(lines):
        blit(img, tiny_mask(line), 4, y + i * pitch, PANIC_FG)
    return [img], "LINUX 5.10 - KERNEL PANIC, 2020"


# Chronological, which is the default order: the machines died in this order.
SPECIMENS = [("c64", _c64), ("sadmac", _sadmac), ("guru", _guru),
             ("bsod", _bsod), ("panic", _panic)]


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed if args.seed else None)

    # The plinth. Six rows of 4 px caption, a hairline rule above it, and one
    # dark row under it so the caption is not sitting on the bezel.
    plinth_h = 8 if H >= 24 and args.label else 0
    screen_h = H - plinth_h

    chosen = SPECIMENS
    if args.only:
        chosen = [s for s in SPECIMENS if s[0] == args.only]
    elif args.shuffle:
        order = rng.permutation(len(SPECIMENS))
        chosen = [SPECIMENS[i] for i in order]

    n = len(chosen)
    frames = []                       # per specimen: list of (H, W, 3) uint8
    captions = []
    for k, (_name, fn) in enumerate(chosen):
        variants, caption = fn(W, screen_h, rng)
        captions.append(caption)
        plinth = np.empty((plinth_h, W, 3), np.uint8) if plinth_h else None
        if plinth_h:
            plinth[:] = LABEL_BG
            plinth[0] = LABEL_RULE
            blit(plinth, tiny_mask(caption), 3, 1, LABEL_FG)
            # The counter on the right is the second half of the tell: it says
            # out loud that this is item three of five in a sequence, and a
            # crashed machine is never item three of five.
            tag = "%d/%d" % (k + 1, n)
            blit(plinth, tiny_mask(tag), W - tiny_width(tag) - 3, 1, LABEL_DIM)
        full = []
        for v in variants:
            frame = np.zeros((H, W, 3), np.uint8)
            frame[:screen_h] = v
            if plinth_h:
                frame[screen_h:] = plinth
            full.append(frame)
        frames.append(full)

    # Blink half-periods, in seconds. The C64 toggles its cursor every twenty
    # fields of a 60 Hz display, which is a third of a second; the Amiga's
    # alert border flashes at about half that rate.
    half = []
    for k, (name, _fn) in enumerate(chosen):
        if len(frames[k]) < 2 or args.blink <= 0:
            half.append(0.0)
        elif name == "c64":
            half.append((1.0 / 3.0) / args.blink)
        else:
            half.append(0.5 / args.blink)

    hold = max(0.5, float(args.hold))
    gap = max(0.0, float(args.gap))
    step = hold + gap
    period = step * n

    out = np.empty((H, W, 3), np.uint8)
    rows = np.arange(screen_h, dtype=f32)
    idx = np.empty(screen_h, f32)
    centre = f32((screen_h - 1) * 0.5)
    scratch = np.empty((screen_h, W, 3), f32)

    def variant(k, t):
        """Which baked frame of specimen k is up at time t."""
        if half[k] <= 0.0:
            return frames[k][0]
        return frames[k][int((t / half[k]) % 2.0)]

    def render(t, frame_i):
        tt = t % period
        k = int(tt / step)
        if k >= n:                                   # only from float slop
            k = n - 1
        u = tt - k * step

        if gap <= 0.0 or u < hold:
            np.copyto(out, variant(k, tt))
            return out

        # The collapse. First half squeezes this specimen towards a line
        # across the middle of the screen, second half opens the next one back
        # out of it -- a CRT losing and finding its vertical hold. The scale
        # never reaches zero: at 1/screen_h the whole picture is one row, and
        # going further only spends frames on nothing.
        p = (u - hold) / gap
        if p < 0.5:
            src, s = variant(k, tt), 1.0 - p * 2.0
        else:
            src, s = variant((k + 1) % n, 0.0), p * 2.0 - 1.0
        s = max(s, 1.0 / max(screen_h, 1))
        # Brightness. A collapsing CRT does not merely squash the picture, it
        # runs the whole beam's energy into one line and that line goes white
        # whatever colour the picture was -- so there is an additive term as
        # well as a gain, cubed so the flash stays tight around the middle of
        # the transition. Without it the Sad Mac, whose centre row is black,
        # collapsed to nothing at all and the cut read as a dropped frame.
        w = 1.0 - abs(p * 2.0 - 1.0)
        gain, add = 1.0 + 1.4 * w, f32(210.0 * w * w * w)

        # Written through out= rather than with `idx /= s`: an augmented
        # assignment inside a closure would rebind the name and make it local.
        np.subtract(rows, centre, out=idx)
        np.divide(idx, f32(s), out=idx)
        np.add(idx, centre, out=idx)
        # Which rows the squeezed picture actually covers, decided *before*
        # the clip. Deciding it after blanks the top and bottom row at the
        # instant the scale is still 1, which is a one-pixel flicker on the
        # first frame of every transition.
        keep = (idx >= 0.0) & (idx <= screen_h - 1.0)
        np.clip(idx, 0.0, screen_h - 1.0, out=idx)

        take = np.take(src[:screen_h], idx.astype(np.int32), axis=0)
        np.multiply(take, f32(gain), out=scratch)
        np.add(scratch, add, out=scratch)
        np.clip(scratch, 0.0, 255.0, out=scratch)
        np.copyto(out[:screen_h], scratch, casting="unsafe")
        out[:screen_h][~keep] = 0                    # black above and below
        if plinth_h:
            # The plinth does not collapse: it is the frame around the exhibit,
            # not the exhibit. Its caption swaps at the moment the picture is a
            # single line, which is the only moment the swap is invisible.
            out[screen_h:] = frames[k if p < 0.5 else (k + 1) % n][0][screen_h:]
        return out

    # Handles for the test script and for anything sequencing this: the loop
    # length, and which specimen is up when.
    render.period = period
    render.hold = hold
    render.step = step
    render.names = [name for name, _fn in chosen]
    render.captions = captions
    render.screen_h = screen_h
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
