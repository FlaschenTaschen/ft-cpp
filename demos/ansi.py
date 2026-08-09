#!/usr/bin/env python3
"""A BBS answering, drawing its welcome screen one character at a time.

Before a web page arrived all at once, a screen arrived at the speed of the
line, and you watched it happen. An ANSI welcome screen came down the wire in
reading order -- top left to bottom right, a character at a time, the box
border creeping across the top of the screen and the sysop's name appearing
letter by letter -- and the slowness was not a defect anybody apologised for.
It was the medium. That reveal is what this panel is; everything else here
exists to give it something worth revealing.

**The panel is a text screen, exactly.** 320x64 divides into 40 columns and 8
rows of 8x8 cells with nothing left over, which is a real terminal geometry and
the reason this demo fits the wall so precisely. Every glyph is 8x8 and 1-bit,
the way a CP437 ROM font was: the letters are rendered from whatever monospaced
face the machine has and then thresholded, because an antialiased edge on an
LED wall reads as a smudge and was not something a character generator could
have produced anyway.

**The box-drawing and shading characters are computed, not rendered.** A font
may or may not have a double-line box corner, and the fallback bitmap face
certainly does not, so a demo that asked Pillow for one would be a working
panel on this laptop and a screen of empty rectangles on the Pi. But these
glyphs are *geometry* -- two rails at rows 2 and 5, two at columns 2 and 5, and
which of the four directions the junction opens into -- so they are built from
that description and are identical everywhere. The shading blocks are their own
dither patterns for the same reason.

**Foreground colour only.** Real ANSI art leans hard on background colours,
which is how a lit bar gets its colour. On an LED wall a lit background is
expensive light and it washes out the type sitting on it, so the art here does
its shading with the block characters in colour on black instead, which is also
how a good deal of it was drawn anyway. The palette is the sixteen CGA colours
at their real values -- including a blue that is genuinely too dark to read,
which is why nobody put text in it.

**The reveal is two slice copies.** Cells arrive in reading order, so the part
of the screen that has been sent is always "every row above this one, plus a
prefix of this row" -- two rectangles. There is no per-pixel mask and no
compositing: a frame is `out[:r*8] = art[:r*8]` and one partial row. That is
about as cheap as a full-panel effect gets, which matters on the 600 MHz Pi 3
driving this wall.

**300 baud is a deliberate lie, and the honest speed would be worse.** These
boards ran at 1200 and 2400, where a 40-column screen paints in under two
seconds and the reveal is gone before you have looked up. At `--cps 30` -- 300
baud, which is what the previous generation of modems ran and what this panel
claims in its CONNECT line -- the welcome screen takes eleven seconds and you
can watch the border draw. Everything is closed form in t, so this is a
schedule and not a state machine: the arrival time of every cell is worked out
in build() and render() binary searches it.

Run:  python3 ansi.py --host 127.0.0.1
      python3 ansi.py --cps 80          # 1200 baud, if you want the truth
      python3 ansi.py --no-cursor
"""

import os
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

CELL = 8
COLS = 40
ROWS = 8

# The sixteen CGA colours at the values the hardware actually produced. The
# dark half tops out at 170 rather than 128, which is why "light grey" reads as
# white-ish and why the bright half had to go all the way to 255 to be
# distinguishable from it.
CGA = {
    "k": (0, 0, 0),         "b": (0, 0, 170),       "g": (0, 170, 0),
    "c": (0, 170, 170),     "r": (170, 0, 0),       "m": (170, 0, 170),
    "n": (170, 85, 0),      "w": (170, 170, 170),
    "K": (85, 85, 85),      "B": (85, 85, 255),     "G": (85, 255, 85),
    "C": (85, 255, 255),    "R": (255, 85, 85),     "M": (255, 85, 255),
    "Y": (255, 255, 85),    "W": (255, 255, 255),
}

# Which directions each double-line junction opens into: up, down, left, right.
BOX = {
    "═": (0, 0, 1, 1), "║": (1, 1, 0, 0),
    "╔": (0, 1, 0, 1), "╗": (0, 1, 1, 0),
    "╚": (1, 0, 0, 1), "╝": (1, 0, 1, 0),
    "╠": (1, 1, 0, 1), "╣": (1, 1, 1, 0),
    "╦": (0, 1, 1, 1), "╩": (1, 0, 1, 1),
    "╬": (1, 1, 1, 1),
}

RAIL_LO, RAIL_HI = 2, 5                 # where the two rails of a double line sit


def _double_line(up, down, left, right):
    """An 8x8 double-line box glyph, from the directions it connects.

    The four-rail model: horizontals at rows 2 and 5, verticals at columns 2
    and 5. A straight run draws both of its rails edge to edge. A corner draws
    the rail *away* from the opening out to the far vertical and the rail
    nearest the opening only as far as the near vertical, which is what makes
    the corner look mitred rather than crossed. A T leaves the rail on the far
    side unbroken and splits the near one; a cross splits all four.
    """
    g = np.zeros((CELL, CELL), bool)
    h, v = left or right, up or down

    def hrail(row, x0, x1):
        g[row, min(x0, x1):max(x0, x1) + 1] = True

    def vrail(col, y0, y1):
        g[min(y0, y1):max(y0, y1) + 1, col] = True

    if h and not v:                                  # ═
        hrail(RAIL_LO, 0, CELL - 1)
        hrail(RAIL_HI, 0, CELL - 1)
    elif v and not h:                                # ║
        vrail(RAIL_LO, 0, CELL - 1)
        vrail(RAIL_HI, 0, CELL - 1)
    elif (up + down) == 1 and (left + right) == 1:   # a corner
        outer_row, inner_row = (RAIL_HI, RAIL_LO) if up else (RAIL_LO, RAIL_HI)
        outer_col, inner_col = (RAIL_HI, RAIL_LO) if left else (RAIL_LO, RAIL_HI)
        far_x = CELL - 1 if right else 0
        far_y = CELL - 1 if down else 0
        hrail(outer_row, outer_col, far_x)
        hrail(inner_row, inner_col, far_x)
        vrail(outer_col, outer_row, far_y)
        vrail(inner_col, inner_row, far_y)
    else:                                            # a T or the cross
        # Rails that the junction does not open towards run straight through;
        # the ones it does open towards are cut where the crossing rails pass.
        for col, opens in ((RAIL_LO, left), (RAIL_HI, right)):
            if v:
                if opens:
                    vrail(col, 0, RAIL_LO)
                    vrail(col, RAIL_HI, CELL - 1)
                else:
                    vrail(col, 0, CELL - 1)
        for row, opens in ((RAIL_LO, up), (RAIL_HI, down)):
            if h:
                if opens:
                    hrail(row, 0, RAIL_LO)
                    hrail(row, RAIL_HI, CELL - 1)
                else:
                    hrail(row, 0, CELL - 1)
        # and the stubs reaching the edge in the directions it does open
        if left:
            hrail(RAIL_LO, 0, RAIL_LO)
            hrail(RAIL_HI, 0, RAIL_LO)
        if right:
            hrail(RAIL_LO, RAIL_HI, CELL - 1)
            hrail(RAIL_HI, RAIL_HI, CELL - 1)
        if up:
            vrail(RAIL_LO, 0, RAIL_LO)
            vrail(RAIL_HI, 0, RAIL_LO)
        if down:
            vrail(RAIL_LO, RAIL_HI, CELL - 1)
            vrail(RAIL_HI, RAIL_HI, CELL - 1)
    return g


def _blocks():
    """The shading and half-block glyphs, as their own dither patterns."""
    yy, xx = np.mgrid[0:CELL, 0:CELL]
    out = {
        "█": np.ones((CELL, CELL), bool),
        "▀": yy < CELL // 2,
        "▄": yy >= CELL // 2,
        "▌": xx < CELL // 2,
        "▐": xx >= CELL // 2,
        # 25 / 50 / 75 percent, which is what ░▒▓ are: a quarter-density grid,
        # a checkerboard, and the checkerboard's complement filled in.
        "░": (xx % 2 == 0) & (yy % 2 == 0),
        "▒": (xx + yy) % 2 == 0,
        "▓": ~((xx % 2 == 1) & (yy % 2 == 1)),
        "■": (xx >= 2) & (xx <= 5) & (yy >= 2) & (yy <= 5),
    }
    return out


def find_font(px):
    """A monospaced face if there is one, else whatever Pillow ships."""
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                 "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
                 "/System/Library/Fonts/Menlo.ttc"):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                pass
    return ImageFont.load_default()


# --------------------------------------------------------------------------
# The session, as pages. Each page is a full screen; the terminal clears
# between them, which is what a BBS did when it sent you somewhere new.
#
# Every row is (text, attributes). The attribute string names a CGA colour per
# character and has to be exactly as long as the text -- build() asserts it,
# because a miscount silently recolours the rest of the line and is otherwise
# very hard to see. Short rows are padded to 40 columns with black spaces,
# which are never transmitted: a terminal sends the line and a newline, not
# the blanks out to the right margin.
# --------------------------------------------------------------------------

CONNECT = [
    (" ATDT 415-555-0142",
     "kwwwwwwwwwwwwwwwww"),
    ("", ""),
    (" CONNECT 300",
     "kGGGGGGGGGGG"),
]

WELCOME = [
    ("╔══════════════════════════════════════╗",
     "cccccccccccccccccccccccccccccccccccccccc"),
    ("║  ░▒▓█ THE SUNSET UNDERGROUND █▓▒░    ║",
     "ckkbBCWkYYYYYYYYYYYYYYYYYYYYYYkWCBbkkkkc"),
    ("║  SAN FRANCISCO  ░  415-555-0142      ║",
     "ckkGGGGGGGGGGGGGkkbkkWWWWWWWWWWWWkkkkkkc"),
    ("╠══════════════════════════════════════╣",
     "cccccccccccccccccccccccccccccccccccccccc"),
    ("║  SYSOP: CAPTAIN FOG      NODE 1/2    ║",
     "ckkwwwwwwwwwwwwwwwwwwkkkkkkCCCCCCCCkkkkc"),
    ("║  300/1200/2400 BAUD  8N1  24 HOURS   ║",
     "ckkwwwwwwwwwwwwwwwwwwkkwwwkkwwwwwwwwkkkc"),
    ("║  FIDONET 1:125/7     14,203 CALLS    ║",
     "ckkMMMMMMMMMMMMMMMkkkkkYYYYYYYYYYYYkkkkc"),
    ("╚══════════════════════════════════════╝",
     "cccccccccccccccccccccccccccccccccccccccc"),
]

MENU = [
    ("▄▄▄ MAIN MENU ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄",
     "bbbkYYYYYYYYYkbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    (" [M] MESSAGE BASES    [F] FILE AREAS",
     "kCCCkwwwwwwwwwwwwwkkkkCCCkwwwwwwwwww"),
    (" [D] DOORS & GAMES    [O] ONELINERS",
     "kCCCkwwwwwwwwwwwwwkkkkCCCkwwwwwwwww"),
    (" [C] CHAT W/ SYSOP    [G] GOODBYE",
     "kCCCkwwwwwwwwwwwwwkkkkCCCkwwwwwww"),
    ("▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
     "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    (" ONELINER: ANSI IS ART, ASCII IS LIFE",
     "kKKKKKKKKKkGGGGGGGGGGGGGGGGGGGGGGGGGG"),
    ("   --THE MIDNIGHT SURFER    03/14/94",
     "kkkKKKKKKKKKKKKKKKKKKKKKkkkkKKKKKKKK"),
    (" COMMAND: G",
     "kwwwwwwwwwY"),
]

LOGOFF = [
    (" COMMAND: G",
     "kwwwwwwwwwY"),
    ("", ""),
    (" LOGGING OFF.  44 MIN LEFT TODAY.",
     "kwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww"),
    (" CALL BACK SOON  --CAPTAIN FOG",
     "kwwwwwwwwwwwwwwwwwwwwwwwwwwwww"),
    ("", ""),
    ("", ""),
    (" NO CARRIER",
     "kRRRRRRRRRR"),
]


def add_arguments(ap):
    ap.add_argument("--cps", type=float, default=30.0,
                    help="characters per second. 30 is 300 baud, which is slow "
                         "enough to watch; the boards these pages imitate ran "
                         "at 120 or 240 and painted this screen in under two "
                         "seconds")
    ap.add_argument("--ring", type=float, default=2.4,
                    help="seconds between the dial and the carrier -- the part "
                         "of a call where nothing at all is on screen")
    ap.add_argument("--cursor", dest="cursor", action="store_true", default=True)
    ap.add_argument("--no-cursor", dest="cursor", action="store_false",
                    help="drop the block cursor that leads the text in")
    ap.add_argument("--cursor-hz", type=float, default=2.4)


def build(args):
    from PIL import Image, ImageDraw

    W, H = args.width, args.height
    cols = max(1, W // CELL)
    rows = max(1, H // CELL)
    cps = max(args.cps, 1e-3)

    # ------------------------------------------------------------- the glyphs
    glyphs = _blocks()
    for ch, dirs in BOX.items():
        glyphs[ch] = _double_line(*dirs)
    glyphs[" "] = np.zeros((CELL, CELL), bool)

    # Everything else comes from a real face, rendered once and thresholded to
    # 1-bit -- an antialiased edge is not something a character ROM could have
    # produced, and on an LED wall it reads as a smudge.
    #
    # The size has to be *measured*, not assumed. A nominal 10 px face is not
    # 10 px of capital, and how much taller it is differs between DejaVu,
    # Liberation and Pillow's built-in fallback -- so picking a number here
    # produced letters with their bottom row shaved off on one machine and
    # loose spacing on another. Instead: walk the sizes down until the capitals
    # actually fit the cell.
    need = set()
    for page in (CONNECT, WELCOME, MENU, LOGOFF):
        for text, _ in page:
            need.update(text)
    caps = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    font = find_font(CELL)
    cap_box = (0, 0, CELL, CELL)
    for px in range(CELL + 5, 5, -1):
        trial = find_font(px)
        box = trial.getbbox(caps)
        widest = max(trial.getbbox(ch)[2] - trial.getbbox(ch)[0]
                     for ch in caps)
        if box[3] - box[1] <= CELL and widest <= CELL:
            font, cap_box = trial, box
            break

    # One baseline for every glyph, taken from the capitals. Centring each
    # glyph in its own cell instead would float a full stop in the middle of
    # the line, which is the sort of thing that looks merely odd until you
    # notice the comma is doing it too.
    cap_h = cap_box[3] - cap_box[1]
    y_at = (CELL - cap_h) // 2 - cap_box[1]

    pad = 6
    for ch in sorted(need - set(glyphs)):
        img = Image.new("L", (CELL + 2 * pad, CELL + 2 * pad), 0)
        d = ImageDraw.Draw(img)
        cb = font.getbbox(ch)
        x_at = (CELL - (cb[2] - cb[0])) // 2 - cb[0]
        d.text((pad + x_at, pad + y_at), ch, font=font, fill=255)
        a = np.asarray(img, np.uint8)[pad:pad + CELL, pad:pad + CELL]
        glyphs[ch] = a >= 128

    # ---------------------------------------------------------- the pages
    def bake(page):
        """-> (art image, [(row, col), ...] in the order the wire sends them)"""
        art = np.zeros((H, W, 3), np.uint8)
        seq = []
        for r, (text, attr) in enumerate(page[:rows]):
            if len(text) != len(attr):
                raise ValueError("row %d: %d characters but %d attributes"
                                 % (r, len(text), len(attr)))
            line = text[:cols]
            marks = attr[:cols]
            # Trailing blanks are never sent: a terminal writes the line and a
            # newline. Trimming them here is why a short row costs a short time
            # rather than a full 40 cells of silence.
            last = len(line.rstrip())
            for c, (ch, mk) in enumerate(zip(line, marks)):
                if c < last:
                    seq.append((r, c))
                g = glyphs.get(ch)
                if g is None or mk == "k":
                    continue
                colour = np.array(CGA[mk], np.uint8)
                tile = art[r * CELL:(r + 1) * CELL, c * CELL:(c + 1) * CELL]
                tile[g] = colour
        return art, seq

    pages = []
    for page, hold, pauses in (
            # (page, seconds held once complete, {cell index: extra seconds})
            # The pause after the dial line is the call going through: the
            # screen is empty and stays empty, which is exactly what waiting
            # for a carrier looked like.
            (CONNECT, 1.6, {"after_row": {0: args.ring}}),
            (WELCOME, 4.5, {}),
            (MENU, 5.0, {}),
            (LOGOFF, 3.5, {})):
        art, seq = bake(page)
        gaps = np.zeros(len(seq) + 1, f32)
        for r, secs in pauses.get("after_row", {}).items():
            # charge the pause to the moment the row's last cell has arrived
            idx = [i for i, (rr, _) in enumerate(seq) if rr == r]
            if idx:
                gaps[idx[-1] + 1] += secs
        when = np.cumsum(np.full(len(seq) + 1, 1.0 / cps, f32) + gaps)
        when -= when[0]
        pages.append({"art": art, "seq": seq, "when": when[1:],
                      "span": float(when[-1]) + hold})

    starts = np.cumsum([0.0] + [p["span"] for p in pages])
    total = float(starts[-1])

    out = np.zeros((H, W, 3), np.uint8)
    cursor_col = np.array(CGA["w"], np.uint8)

    def render(t, frame):
        tt = t % total
        p = int(np.searchsorted(starts, tt, side="right")) - 1
        p = min(max(p, 0), len(pages) - 1)
        page = pages[p]
        local = tt - float(starts[p])

        out[:] = 0
        k = int(np.searchsorted(page["when"], local, side="right"))
        if k > 0:
            r, c = page["seq"][k - 1]
            # Two rectangles: the rows already finished, and the prefix of the
            # row in progress. See the module docstring -- there is no mask.
            if r > 0:
                out[:r * CELL] = page["art"][:r * CELL]
            y0 = r * CELL
            x1 = (c + 1) * CELL
            out[y0:y0 + CELL, :x1] = page["art"][y0:y0 + CELL, :x1]
        else:
            r, c = 0, -1

        if args.cursor:
            # Where the next character will land. During a hold it sits after
            # the last one and blinks; during the reveal it runs ahead of the
            # text, which is what a cursor on a slow line actually did.
            cr, cc = (r, c + 1) if c + 1 < cols else (r + 1, 0)
            done = k >= len(page["seq"])
            lit = (not done) or ((tt * args.cursor_hz) % 1.0 < 0.55)
            if lit and cr < rows:
                out[cr * CELL:(cr + 1) * CELL,
                    cc * CELL:(cc + 1) * CELL] = cursor_col
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
