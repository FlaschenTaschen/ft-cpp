#!/usr/bin/env python3
"""Split-flap departures board.

The mechanical airport board: every character cell is a stack of hinged cards,
and to change a letter the board has to riffle through every card in between.
A cell reaching Z from blank flips twenty-six times and takes visibly longer
than one reaching B, which is the whole charm -- the board arrives at its
message in a ragged wave rather than all at once.

The flip is a rotating card, not a crossfade. Each cell is split across its
middle. A flip draws the *incoming* character's top half sitting statically
behind, and over it the *outgoing* character's top half squashed vertically
and hinged on the seam, falling. Halfway down the card is edge on and vanishes;
past that you are looking at its back, which is the incoming character's bottom
half, unfolding downward over the outgoing one. So for the whole first half of
a flip the cell shows two different characters -- new above the seam, old below
it -- which is exactly what a crossfade cannot do, and exactly what makes this
read as a physical card turning rather than one glyph dissolving into another.
The vertical squash is the foreshortening; the darkening that goes with it is
the card turning away from the light.

The glyphs are a 5x7 bitmap font defined in this file, in the same rows-of-
characters form as nyancat.py's sprite: no font file to go missing on the
target machine, and it diffs. build() bakes each character into a finished
card, splits it at the seam, and precomputes every squashed step of the fall,
so a frame is a handful of small blits into a persistent framebuffer -- and
cells that are not flipping are not touched at all.

Layout on a 320x64 panel is two rows of 24 characters at 2x scale. 3x scale
fits vertically but only gives seventeen columns, which is too few for a
destination and a status; 2x gives 14 px tall glyphs, still crisp and readable
across a room, and room for a real line of text. Cream on near-black with a
dark gap between cards -- what a real board looks like, and what an LED wall
with true-black unlit pixels does best.

A message containing {TIME} keeps flapping: the board compares every cell
against what it should say each frame, so when the minute rolls over only the
digits that changed flip.

Run:  python3 splitflap.py --host 127.0.0.1
      python3 splitflap.py --messages "HELLO|WORLD;ONE MORE|MESSAGE" --hold 4
"""

import math
import sys
import time

import numpy as np

import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The font, as data. 5x7, '#' lit.
# --------------------------------------------------------------------------

GW, GH = 5, 7

FONT = {
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#.#.#", "#..##", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..##.", "....#", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    ":": (".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."),
    "/": ("....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."),
    "'": ("..#..", "..#..", ".....", ".....", ".....", ".....", "....."),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."),
    "?": (".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."),
    ",": (".....", ".....", ".....", ".....", ".##..", ".##..", ".#..."),
    "(": ("...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."),
    ")": (".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "*": (".....", "#.#.#", ".###.", "#####", ".###.", "#.#.#", "....."),
    "&": (".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"),
}

# The order the cards are stacked in, and therefore the order a cell riffles
# through them. Blank first, then the alphabet, then digits, then punctuation
# -- the order on a real board, and the reason a word full of late letters
# takes noticeably longer to arrive than one full of early ones.
ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.:/'!?,()+*&"
INDEX = {c: i for i, c in enumerate(ALPHABET)}
NCHARS = len(ALPHABET)

# Cream on a card that is nearly, but not quite, off. The gap between cards is
# true black, which on an LED wall is genuinely nothing, so the cards read as
# separate objects without needing a drawn border.
GLYPH = (255, 246, 214)
CARD = (11, 11, 13)

COLOURS = {
    "cream": (255, 246, 214),
    "white": (255, 255, 255),
    "amber": (255, 176, 40),
    "green": (120, 255, 140),
    "cyan": (120, 230, 255),
}

DEFAULT_MESSAGES = (
    "FLASCHEN TASCHEN|DEPARTURES   {TIME}"
    ";BER  BERLIN TEGEL|GATE A12    ON TIME"
    ";AMS  AMSTERDAM|GATE C04    BOARDING"
    ";SFO  SAN FRANCISCO|GATE B22     DELAYED"
    ";320 X 64 LED PANEL|SPLIT FLAP DEPARTURES"
)


def add_arguments(ap):
    ap.add_argument("--messages", default=DEFAULT_MESSAGES,
                    help="';' between messages, '|' between the lines of one; "
                         "{TIME} and {DATE} are substituted live")
    ap.add_argument("--hold", type=float, default=6.0,
                    help="seconds to rest on a message once the board settles")
    ap.add_argument("--flap", type=float, default=52.0,
                    help="milliseconds per flap, before per-cell jitter")
    ap.add_argument("--jitter", type=float, default=0.22,
                    help="spread of per-cell flap rates, 0 = every cell equal")
    ap.add_argument("--stagger", type=float, default=0.35,
                    help="seconds of random start delay across the board")
    ap.add_argument("--ripple", type=float, default=0.012,
                    help="extra start delay per column, so the board runs left "
                         "to right")
    ap.add_argument("--rows", type=int, default=2, help="rows of cards")
    ap.add_argument("--min-cols", dest="min_cols", type=int, default=22,
                    help="prefer the largest glyph scale still giving this many "
                         "columns")
    ap.add_argument("--colour", "--color", dest="colour", default="cream",
                    choices=sorted(COLOURS), help="glyph colour")
    ap.add_argument("--no-settle", dest="no_settle", action="store_true",
                    help="skip the landing bounce")
    ap.add_argument("--12h", dest="ampm", action="store_true",
                    help="12 hour clock for {TIME}")


# --------------------------------------------------------------------------
# Baking the cards.
# --------------------------------------------------------------------------

def glyph_mask(ch, scale):
    """The character as a boolean (GH*scale, GW*scale) mask."""
    rows = FONT.get(ch, FONT[" "])
    m = np.array([[c != "." for c in r] for r in rows], bool)
    return np.repeat(np.repeat(m, scale, 0), scale, 1)


def bake_card(ch, scale, card_w, card_h, glyph_rgb):
    """One finished card: glyph centred on the card face, with its seam."""
    card = np.empty((card_h, card_w, 3), np.uint8)
    card[:] = CARD
    m = glyph_mask(ch, scale)
    gh, gw = m.shape
    half = card_h // 2
    y0, x0 = (card_h - gh) // 2, (card_w - gw) // 2
    # Nudge the glyph until the two seam rows straddle a boundary between font
    # rows rather than landing inside one. Centred and unnudged at 2x they land
    # squarely on the middle scanline, and the seam then swallows the whole
    # crossbar of A, E and H -- a legibility bug you only see in a capture.
    while (half - y0) % scale and y0 + gh < card_h:
        y0 += 1
    card[y0:y0 + gh, x0:x0 + gw][m] = glyph_rgb

    # The seam is always there, on every card, flipping or not: the hairline
    # where the two halves meet, plus the shadow the upper half casts on the
    # lower. Darkening it right across the glyph is what makes a resting cell
    # still look like two cards rather than one printed tile.
    card[half - 1] = (card[half - 1] * 0.22).astype(np.uint8)
    card[half] = (card[half] * 0.50).astype(np.uint8)
    return card


def squash(half_img, k):
    """Foreshorten a card half into k rows, hinge-end first.

    Nearest-neighbour on purpose: the panel is 64 rows and a blurred flap at
    this size turns to mush, where a hard-edged one still reads as a card.
    """
    n = half_img.shape[0]
    idx = ((np.arange(k) + 0.5) * n / k).astype(int).clip(0, n - 1)
    return half_img[idx]


def flap_tables(cards, half):
    """Every squashed step of the fall, shaded, for both faces of the flap.

    tops[c][k]: the outgoing character's top half, k rows tall, sitting on the
    seam -- the front of the card on its way down.
    bots[c][k]: the incoming character's bottom half, k rows tall, hanging off
    the seam -- the back of the same card, once it is past vertical.
    """
    tops, bots = [], []
    for card in cards:
        top, bot = card[:half].copy(), card[half:].copy()
        # A card turned away from you catches less light. It bottoms out well
        # above zero: a flap that goes fully black just reads as a hole.
        shade = [None] + [0.34 + 0.66 * (k / float(half))
                          for k in range(1, half + 1)]
        tops.append([None] + [(squash(top, k) * shade[k]).astype(np.uint8)
                              for k in range(1, half + 1)])
        bots.append([None] + [(squash(bot, k) * shade[k]).astype(np.uint8)
                              for k in range(1, half + 1)])
    return tops, bots


def geometry(W, H, rows, min_cols):
    """Pick a glyph scale, then the card and cell sizes that follow from it.

    Largest scale that fits the rows vertically *and* still leaves min_cols
    columns; if nothing does -- a 128x32 panel, say -- the constraint on
    columns is dropped and the largest scale that merely fits is used.
    """
    best = fallback = None
    for s in range(1, 9):
        card_w = GW * s + 2
        cell_w = card_w + max(1, s // 2)
        card_h = GH * s + 2 * max(2, 2 * s)
        card_h -= card_h % 2                    # the seam has to be the middle
        cell_h = card_h + max(1, s)
        cols = W // cell_w
        if cols < 1 or rows * cell_h > H:
            break
        cand = (s, cols, card_w, card_h, cell_w, cell_h)
        if cols >= min_cols:
            best = cand
        elif best is None:
            fallback = cand
    if best is None:
        # Too small even for one row of the smallest cards: take the smallest
        # anyway and let it clip, rather than failing to start.
        best = fallback or (1, max(1, W // 8), GW + 2, GH + 3, GW + 3, GH + 4)
    return best


# --------------------------------------------------------------------------

def build(args):

    W, H = args.width, args.height
    rows = max(1, args.rows)
    scale, cols, card_w, card_h, cell_w, cell_h = geometry(
        W, H, rows, args.min_cols)
    half = card_h // 2

    glyph_rgb = COLOURS[args.colour]
    cards = [bake_card(c, scale, card_w, card_h, glyph_rgb) for c in ALPHABET]
    tops, bots = flap_tables(cards, half)

    x_org = (W - (cols * cell_w - (cell_w - card_w))) // 2
    y_org = (H - (rows * cell_h - (cell_h - card_h))) // 2

    messages = [m for m in args.messages.split(";") if m.strip()] or [" "]

    rng = np.random.default_rng(20240712)
    n = rows * cols
    # Per cell, fixed for the life of the demo: how fast this one flips and
    # how late it starts. Identical cells would flip in lockstep and the board
    # would look like a screen doing an animation, not like fifty independent
    # little motors.
    step = (args.flap / 1000.0) * (1.0 + args.jitter
                                   * rng.uniform(-1.0, 1.0, n))
    delay = rng.uniform(0.0, max(args.stagger, 0.0), n)
    for i in range(n):
        delay[i] += args.ripple * (i % cols)

    cur = np.zeros(n, np.int32)          # the card a settled cell rests on
    src = np.zeros(n, np.int32)          # where its current riffle started
    steps = np.zeros(n, np.int32)        # how many flips that riffle is
    t0 = np.zeros(n, f32)                # when it starts
    tgt = np.zeros(n, np.int32)
    land = np.zeros(n, f32)              # when it stopped, for the bounce

    fb = np.zeros((H, W, 3), np.uint8)
    dirty = np.ones(n, bool)

    state = {"msg": -1, "next": 0.0, "sec": -1}
    SETTLE = 0.055

    def wanted(now):
        """The characters the board should be showing, one per cell."""
        m = messages[state["msg"] % len(messages)]
        if "{" in m:
            lt = time.localtime(now)
            hour = lt.tm_hour
            if args.ampm:
                hour = hour % 12 or 12
            m = (m.replace("{TIME}", "%02d:%02d" % (hour, lt.tm_min))
                  .replace("{DATE}", "%02d %s" % (lt.tm_mday,
                                                  time.strftime("%b", lt).upper())))
        lines = m.split("|")
        out = []
        for r in range(rows):
            s = lines[r].upper() if r < len(lines) else ""
            s = "".join(c if c in INDEX else " " for c in s)[:cols]
            pad = cols - len(s)
            out.append(" " * (pad // 2) + s + " " * (pad - pad // 2))
        return out

    def retarget(t, want):
        """Start a riffle on every cell that is not already heading somewhere
        right. Cells already mid-flip restart from the card they are showing,
        so a message that changes under them does not jump."""
        for r in range(rows):
            line = want[r]
            for c in range(cols):
                i = r * cols + c
                new = INDEX[line[c]]
                if new == tgt[i]:
                    continue
                shown = displayed(t, i)
                tgt[i] = new
                src[i] = shown
                steps[i] = (new - shown) % NCHARS
                t0[i] = t + (delay[i] if steps[i] else 0.0)
                if steps[i] == 0:
                    cur[i] = new
                    land[i] = t
                dirty[i] = True

    def displayed(t, i):
        """Which card cell i currently has at the top of its fall."""
        if steps[i] == 0:
            return int(cur[i])
        k = (t - t0[i]) / step[i]
        if k <= 0.0:
            return int(src[i])
        if k >= steps[i]:
            return int(cur[i])
        return int((src[i] + int(k)) % NCHARS)

    def render(t, frame):
        now = time.time()

        if state["msg"] < 0 or t >= state["next"]:
            state["msg"] += 1
            state["next"] = 1e18            # set for real once the board lands
            retarget(t, wanted(now))
        elif ("{" in messages[state["msg"] % len(messages)]
              and int(now) != state["sec"]):
            state["sec"] = int(now)
            retarget(t, wanted(now))        # the clock ticked; flip the digits

        busy = False
        for i in range(n):
            if steps[i]:
                k = (t - t0[i]) / step[i]
                if k >= steps[i]:           # landed
                    cur[i] = tgt[i]
                    steps[i] = 0
                    land[i] = t
                    dirty[i] = True
                elif k >= 0.0:
                    busy = True
                    s = int(k)
                    p = k - s
                    a = int((src[i] + s) % NCHARS)
                    b = int((a + 1) % NCHARS)
                    y = y_org + (i // cols) * cell_h
                    x = x_org + (i % cols) * cell_w
                    view = fb[y:y + card_h, x:x + card_w]
                    # The incoming card's top half is already in place behind
                    # the falling one; the outgoing card's bottom half is still
                    # showing below the seam. Two characters at once.
                    view[:half] = cards[b][:half]
                    view[half:] = cards[a][half:]
                    cos = math.cos(math.pi * p)
                    if cos > 0.0:           # front of the flap, above the seam
                        kk = int(round(half * cos))
                        if kk:
                            view[half - kk:half] = tops[a][kk]
                    else:                   # its back, unfolding below
                        kk = int(round(half * -cos))
                        if kk:
                            view[half:half + kk] = bots[b][kk]
                    continue
                else:
                    busy = True             # waiting out its start delay
            if dirty[i] or (not args.no_settle
                            and t - land[i] < SETTLE):
                y = y_org + (i // cols) * cell_h
                x = x_org + (i % cols) * cell_w
                view = fb[y:y + card_h, x:x + card_w]
                if not args.no_settle and 0.0 <= t - land[i] < SETTLE:
                    # The clack: the stack drops a pixel as the flap seats,
                    # then comes back. A card that simply stops looks digital.
                    view[0] = 0
                    view[1:] = cards[cur[i]][:-1]
                    dirty[i] = True
                else:
                    view[:] = cards[cur[i]]
                    dirty[i] = False

        if not busy and state["next"] > 1e17:
            state["next"] = t + args.hold

        return fb

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=60)


if __name__ == "__main__":
    main()
