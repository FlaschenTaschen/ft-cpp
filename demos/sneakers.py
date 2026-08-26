#!/usr/bin/env python3
"""SETEC ASTRONOMY -- the anagram from Sneakers (1992), solved on the wall.

The film's cryptography box shows a phrase that will not parse until the
letters are pushed around: SETEC ASTRONOMY is TOO MANY SECRETS with nothing
added and nothing thrown away. That is the entire demo. Fourteen letters are
fourteen physical tiles; they lift off the line, fly along staggered arcs to
their places in the other phrase, overshoot slightly and settle. The joke only
works if you can read both ends of the trip, so both are held for six and a
half seconds and the type is as big as the panel will take.

A 320x64 strip is exactly the wrong shape for most things and exactly the right
shape for one line of large type. Fourteen glyphs at an 18 px advance is 260 px
of ink -- edge to edge on a 5:1 letterbox, 27 px tall, legible from the far
side of a room where a 12 px terminal font is a smudge.

**Everything is closed form in t.** No tile carries state between frames: a
letter's position is a function of which phase t falls in and how far through
it is, and the garbage characters during the scramble are a lookup into a baked
random table indexed by int(t * rate). ftsched builds a demo ahead of time and
starts it at t=0, the preview baker steps it at a fixed rate, and the wall's
own loop drifts; render() has to land in the same place whichever of those is
driving it.

**The glyphs are baked, and so is every brightness they are ever drawn at.**
build() rasterises the 6x9 bitmap font in this file at the display scale, then
bakes each character at sixteen brightness levels and both scanline phases --
32 finished uint8 tiles per character. A frame is then at most 28 blits of
np.maximum into a cleared buffer with no arithmetic at all, which is why the
motion trail behind each letter is affordable: it is the same tile drawn at the
position it had 60 ms ago, at level 5 of 16. The scanline phase is
selected by the tile's own y parity, so the CRT texture stays locked to the
panel while a letter moves through it -- much cheaper than dimming alternate
rows of a 320x64 frame every frame, which is a full-frame float pass the Pi
cannot spare.

Amber on black, because wopr.py and console.py already own green phosphor, and
because the prop in the film is a warm readout. The dim rules, corner brackets,
ticks and the status word (SOLVING / LOCKED / PERMUTE) are there to make it
read as an instrument rather than a screensaver.

--words takes further pairs, and build() refuses a pair that is not actually an
anagram rather than letting letters quietly appear and vanish mid-flight.

Run:  python3 sneakers.py --host 127.0.0.1
      python3 sneakers.py --colour green --speed 1.4
      python3 sneakers.py --words 'ELVIS|LIVES;THE EYES|THEY SEE' --hold 5
"""

import bisect
import sys
from collections import Counter

import numpy as np

import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The font, as data: 6 columns by 9 rows, '#' lit.
#
# Five columns of ink and a sixth column of side bearing, so the advance is the
# full cell and adjacent letters get a 3 px gutter at scale 3 without any
# per-glyph metrics. Nine rows rather than the usual seven because the panel
# has the height to spare and a taller, slightly condensed capital is what a
# readout looks like.
# --------------------------------------------------------------------------

GW, GH = 6, 9

FONT = {
    " ": ("......", "......", "......", "......", "......",
          "......", "......", "......", "......"),
    "A": (".###..", "#...#.", "#...#.", "#...#.", "#####.",
          "#...#.", "#...#.", "#...#.", "#...#."),
    "B": ("####..", "#...#.", "#...#.", "#...#.", "####..",
          "#...#.", "#...#.", "#...#.", "####.."),
    "C": (".###..", "#...#.", "#.....", "#.....", "#.....",
          "#.....", "#.....", "#...#.", ".###.."),
    "D": ("####..", "#...#.", "#...#.", "#...#.", "#...#.",
          "#...#.", "#...#.", "#...#.", "####.."),
    "E": ("#####.", "#.....", "#.....", "#.....", "####..",
          "#.....", "#.....", "#.....", "#####."),
    "F": ("#####.", "#.....", "#.....", "#.....", "####..",
          "#.....", "#.....", "#.....", "#....."),
    "G": (".###..", "#...#.", "#.....", "#.....", "#..##.",
          "#...#.", "#...#.", "#...#.", ".###.."),
    "H": ("#...#.", "#...#.", "#...#.", "#...#.", "#####.",
          "#...#.", "#...#.", "#...#.", "#...#."),
    "I": (".###..", "..#...", "..#...", "..#...", "..#...",
          "..#...", "..#...", "..#...", ".###.."),
    "J": ("..###.", "...#..", "...#..", "...#..", "...#..",
          "...#..", "#..#..", "#..#..", ".##..."),
    "K": ("#...#.", "#..#..", "#.#...", "##....", "##....",
          "#.#...", "#..#..", "#...#.", "#...#."),
    "L": ("#.....", "#.....", "#.....", "#.....", "#.....",
          "#.....", "#.....", "#.....", "#####."),
    "M": ("#...#.", "##.##.", "#.#.#.", "#.#.#.", "#...#.",
          "#...#.", "#...#.", "#...#.", "#...#."),
    "N": ("#...#.", "##..#.", "##..#.", "#.#.#.", "#.#.#.",
          "#.#.#.", "#..##.", "#..##.", "#...#."),
    "O": (".###..", "#...#.", "#...#.", "#...#.", "#...#.",
          "#...#.", "#...#.", "#...#.", ".###.."),
    "P": ("####..", "#...#.", "#...#.", "#...#.", "####..",
          "#.....", "#.....", "#.....", "#....."),
    "Q": (".###..", "#...#.", "#...#.", "#...#.", "#...#.",
          "#...#.", "#.#.#.", "#..#..", ".##.#."),
    "R": ("####..", "#...#.", "#...#.", "#...#.", "####..",
          "#.#...", "#..#..", "#..#..", "#...#."),
    "S": (".###..", "#...#.", "#.....", "#.....", ".###..",
          "....#.", "....#.", "#...#.", ".###.."),
    "T": ("#####.", "..#...", "..#...", "..#...", "..#...",
          "..#...", "..#...", "..#...", "..#..."),
    "U": ("#...#.", "#...#.", "#...#.", "#...#.", "#...#.",
          "#...#.", "#...#.", "#...#.", ".###.."),
    "V": ("#...#.", "#...#.", "#...#.", "#...#.", "#...#.",
          "#...#.", ".#.#..", ".#.#..", "..#..."),
    "W": ("#...#.", "#...#.", "#...#.", "#...#.", "#.#.#.",
          "#.#.#.", "#.#.#.", "##.##.", "#...#."),
    "X": ("#...#.", "#...#.", ".#.#..", ".#.#..", "..#...",
          ".#.#..", ".#.#..", "#...#.", "#...#."),
    "Y": ("#...#.", "#...#.", ".#.#..", ".#.#..", "..#...",
          "..#...", "..#...", "..#...", "..#..."),
    "Z": ("#####.", "....#.", "....#.", "...#..", "..#...",
          ".#....", "#.....", "#.....", "#####."),
    "0": (".###..", "#...#.", "#...#.", "#..##.", "#.#.#.",
          "##..#.", "#...#.", "#...#.", ".###.."),
    "1": ("..#...", ".##...", "..#...", "..#...", "..#...",
          "..#...", "..#...", "..#...", ".###.."),
    "2": (".###..", "#...#.", "....#.", "....#.", "...#..",
          "..#...", ".#....", "#.....", "#####."),
    "3": (".###..", "#...#.", "....#.", "....#.", "..##..",
          "....#.", "....#.", "#...#.", ".###.."),
    "4": ("...#..", "..##..", ".#.#..", "#..#..", "#..#..",
          "#####.", "...#..", "...#..", "...#.."),
    "5": ("#####.", "#.....", "#.....", "####..", "....#.",
          "....#.", "....#.", "#...#.", ".###.."),
    "6": ("..##..", ".#....", "#.....", "#.....", "####..",
          "#...#.", "#...#.", "#...#.", ".###.."),
    "7": ("#####.", "....#.", "....#.", "...#..", "...#..",
          "..#...", "..#...", "..#...", "..#..."),
    "8": (".###..", "#...#.", "#...#.", "#...#.", ".###..",
          "#...#.", "#...#.", "#...#.", ".###.."),
    "9": (".###..", "#...#.", "#...#.", "#...#.", ".####.",
          "....#.", "....#.", "...#..", ".##..."),
    "#": (".#.#..", ".#.#..", "#####.", ".#.#..", ".#.#..",
          "#####.", ".#.#..", ".#.#..", "......"),
    "*": ("......", "#.#.#.", ".###..", "#####.", ".###..",
          "#.#.#.", "......", "......", "......"),
    "/": ("....#.", "....#.", "...#..", "...#..", "..#...",
          ".#....", ".#....", "#.....", "#....."),
    "\\": ("#.....", "#.....", ".#....", ".#....", "..#...",
           "...#..", "...#..", "....#.", "....#."),
    "+": ("......", "......", "..#...", "..#...", "#####.",
          "..#...", "..#...", "......", "......"),
    "-": ("......", "......", "......", "......", "#####.",
          "......", "......", "......", "......"),
    "=": ("......", "......", "......", "#####.", "......",
          "#####.", "......", "......", "......"),
    "?": (".###..", "#...#.", "....#.", "...#..", "..#...",
          "..#...", "......", "..#...", "..#..."),
    "!": ("..#...", "..#...", "..#...", "..#...", "..#...",
          "..#...", "......", "..#...", "..#..."),
    ":": ("......", "..#...", "..#...", "......", "......",
          "......", "..#...", "..#...", "......"),
    ".": ("......", "......", "......", "......", "......",
          "......", "......", "..#...", "..#..."),
    "'": ("..#...", "..#...", "..#...", "......", "......",
          "......", "......", "......", "......"),
}

# What an unresolved cell riffles through. Letters and digits alone read as a
# word being typed badly; the symbols are what make it read as ciphertext.
GARBAGE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#*/\\+-=?!:"

# Warm phosphor first. The film's box is amber, and the two other terminal
# demos in this collection are already green.
COLOURS = {
    "amber": (255, 168, 36),
    "gold": (255, 206, 74),
    "ember": (255, 118, 30),
    "green": (120, 255, 140),
    "white": (226, 234, 255),
}

DEFAULT_WORDS = "SETEC ASTRONOMY|TOO MANY SECRETS"

# Sixteen brightness steps is enough that the pre-lock flicker looks analogue
# and few enough that the whole baked table stays a couple of megabytes.
LEVELS = 16
FULL = LEVELS - 1
TRANSIT = 12                            # a letter in flight, off its line
TRAIL = 5                               # the ghost it drags behind it

# The status word for each phase. Naming the machine's state is most of what
# separates an instrument from a screensaver.
LABELS = {"scramble": "SOLVING", "hold": "LOCKED",
          "fly": "PERMUTE", "dissolve": "SCRAMBLE"}


def add_arguments(ap):
    ap.add_argument("--words", default=DEFAULT_WORDS,
                    help="'|' between the two phrases of a pair, ';' between "
                         "pairs; each pair must be a true anagram")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="rate of the flights and the scramble; the holds are "
                         "--hold regardless")
    ap.add_argument("--hold", type=float, default=6.5,
                    help="seconds resting on each resolved phrase")
    ap.add_argument("--scramble", type=float, default=4.0,
                    help="seconds of ciphertext before the first phrase locks")
    ap.add_argument("--cycle", type=int, default=2,
                    help="round trips between the two phrases before the next "
                         "pair")
    ap.add_argument("--colour", "--color", dest="colour", default="amber",
                    choices=sorted(COLOURS), help="phosphor colour")
    ap.add_argument("--arc", type=float, default=1.0,
                    help="height the letters rise on their way across, "
                         "0 = flat")
    ap.add_argument("--seed", type=int, default=1992,
                    help="fixes the stagger, arcs and ciphertext")
    ap.add_argument("--no-scanlines", dest="no_scanlines", action="store_true",
                    help="drop the CRT raster texture")
    ap.add_argument("--no-trails", dest="no_trails", action="store_true",
                    help="drop the motion trail behind a letter in flight")


# --------------------------------------------------------------------------
# Text: validation, metrics, and which tile goes where.
# --------------------------------------------------------------------------

def normalise(phrase):
    """Upper case, single spaces, and every character in the font."""
    text = " ".join(phrase.upper().split())
    bad = sorted(set(c for c in text if c not in FONT))
    if bad:
        raise SystemExit("sneakers: no glyph for %s in %r (the font has %s)"
                         % (" ".join(repr(c) for c in bad), phrase,
                            "".join(sorted(c for c in FONT if c != " "))))
    return text


def check_anagram(a, b):
    """Refuse a pair that is not one.

    A near-miss pair is the failure mode worth catching: the demo would still
    run, and letters would silently appear from nowhere and vanish mid-flight,
    which looks like a bug in the animation rather than a bug in the words.
    """
    ca = Counter(a.replace(" ", ""))
    cb = Counter(b.replace(" ", ""))
    if ca == cb:
        return
    only_a = sorted((ca - cb).elements())
    only_b = sorted((cb - ca).elements())
    raise SystemExit(
        "sneakers: %r and %r are not an anagram -- %r has %d letters, %r has "
        "%d.%s%s" % (a, b, a, sum(ca.values()), b, sum(cb.values()),
                     "  Only in the first: %s." % "".join(only_a) if only_a
                     else "",
                     "  Only in the second: %s." % "".join(only_b) if only_b
                     else ""))


def measure(phrase, scale):
    """[(char, x), ...] from x=0, and the width of the ink.

    A space advances by less than a full cell: at a full cell the two words of
    SETEC ASTRONOMY drift apart far enough that the line stops reading as one
    phrase. The ink stops 5 columns into the last cell -- the sixth is the side
    bearing that gives every pair of letters its gutter.
    """
    adv = GW * scale
    gap = int(round(adv * 0.7))
    slots = []
    x = 0
    for ch in phrase:
        if ch == " ":
            x += gap
        else:
            slots.append((ch, x))
            x += adv
    return slots, (slots[-1][1] + 5 * scale) if slots else 0


def layout(phrase, scale, width):
    """measure(), centred on the panel: [(char, x), ...] and the right edge."""
    slots, ink = measure(phrase, scale)
    left = (width - ink) // 2
    return [(ch, x + left) for ch, x in slots], left + ink


def assign(slots_a, slots_b):
    """Match each tile of the first phrase to a slot of the second.

    Same character, nearest slot first. Matching duplicates in order of
    appearance instead is simpler and looks worse: the two Os of ASTRONOMY
    would swap ends of the panel for no reason, and a viewer trying to follow
    one letter across sees a pointless crossing.
    """
    taken = [False] * len(slots_b)
    out = []
    for ch, xa in slots_a:
        best, best_d = -1, None
        for j, (ch_b, xb) in enumerate(slots_b):
            if taken[j] or ch_b != ch:
                continue
            d = abs(xb - xa)
            if best_d is None or d < best_d:
                best, best_d = j, d
        taken[best] = True
        out.append((ch, xa, slots_b[best][1]))
    return out


# --------------------------------------------------------------------------
# Baking.
# --------------------------------------------------------------------------

def glyph_mask(ch, scale):
    """The character as a boolean (GH*scale, GW*scale) mask."""
    rows = FONT.get(ch, FONT["?"])
    m = np.array([[c != "." for c in r] for r in rows], bool)
    return np.repeat(np.repeat(m, scale, 0), scale, 1)


def bake_tiles(chars, scale, rgb, scanlines):
    """Every character at every brightness, for both scanline phases.

    tiles[c][level][phase] is a finished uint8 tile, so drawing one is a single
    np.maximum with no arithmetic. `phase` is chosen at blit time from the
    tile's own y, which keeps the raster locked to the panel rather than to the
    letter -- a texture that slides along with a moving letter reads as a
    corrupted sprite, not as a screen.
    """
    tiles = {}
    for ch in chars:
        m = glyph_mask(ch, scale).astype(f32)
        levels = []
        for lvl in range(LEVELS):
            k = lvl / float(FULL)
            phases = []
            for phase in (0, 1):
                a = m * k
                if scanlines:
                    rows = (np.arange(a.shape[0]) + phase) % 2 == 0
                    a = a * np.where(rows, 0.62, 1.0).astype(f32)[:, None]
                phases.append((a[:, :, None] * np.array(rgb, f32)
                               ).clip(0, 255).astype(np.uint8))
            levels.append(phases)
        tiles[ch] = levels
    return tiles


def bake_label(text, rgb, k):
    """A status word at scale 1, dim, as one strip."""
    strip = np.zeros((GH, GW * len(text), 3), np.uint8)
    for i, ch in enumerate(text):
        m = glyph_mask(ch, 1)
        strip[:, i * GW:(i + 1) * GW][m] = tuple(
            int(c * k) for c in rgb)
    return strip


def bake_chrome(width, height, rgb, y_top, y_bot):
    """The static instrument frame: two rules, brackets, ticks.

    Baked into the background the frame is cleared to, so the whole of it costs
    one memcpy per frame rather than any drawing.
    """
    bg = np.zeros((height, width, 3), np.uint8)
    faint = tuple(int(c * 0.16) for c in rgb)
    bright = tuple(int(c * 0.42) for c in rgb)
    for y in (y_top, y_bot):
        bg[y, 8:width - 8] = faint
        bg[y, 8:20] = bright
        bg[y, width - 20:width - 8] = bright
    # Corner stubs turning the two rules into one enclosure.
    for x in (8, width - 9):
        bg[y_top:y_top + 3, x] = bright
        bg[y_bot - 2:y_bot + 1, x] = bright
    # Ticks along the lower rule, which is what stops it reading as underline.
    for x in range(width // 3, width - 24, 20):
        bg[y_bot - 2:y_bot, x] = faint
    return bg


# --------------------------------------------------------------------------

def build(args):

    width, height = args.width, args.height
    rgb = COLOURS[args.colour]
    rng = np.random.default_rng(args.seed & 0x7fffffff)

    pairs = []
    for chunk in args.words.split(";"):
        if not chunk.strip():
            continue
        halves = chunk.split("|")
        if len(halves) != 2:
            raise SystemExit("sneakers: --words wants two phrases per pair "
                             "separated by '|', got %r" % chunk)
        first, second = normalise(halves[0]), normalise(halves[1])
        if not first.replace(" ", ""):
            raise SystemExit("sneakers: %r has no letters" % chunk)
        check_anagram(first, second)
        pairs.append((first, second))
    if not pairs:
        raise SystemExit("sneakers: --words is empty")

    # Largest scale whose *widest* phrase still leaves a margin. The film's
    # pair lands on 3, which is 15 px of ink per letter and 27 tall.
    scale = 1
    for cand in range(1, 7):
        if GH * cand > height - 20:
            break
        widest = max(measure(phrase, cand)[1]
                     for pair in pairs for phrase in pair)
        if widest > width - 16:
            break
        scale = cand

    glyph_h = GH * scale
    y_base = (height - glyph_h) // 2 + 1
    y_rule_top, y_rule_bot = 2, height - 5
    # The status word lives between the type and the lower rule. On a panel
    # where the type has grown to fill the height there is no such gap, and a
    # label overprinting the phrase is worse than no label.
    y_label = min(y_base + glyph_h + 4, y_rule_bot - GH - 1)
    show_label = y_label >= y_base + glyph_h + 1

    used = set(GARBAGE)
    for first, second in pairs:
        used |= set(first) | set(second)
    for word in LABELS.values():
        used |= set(word)
    tiles = bake_tiles(sorted(used - set(" ")), scale, rgb, not args.no_scanlines)

    # One flat list, so a blit is an index rather than a dict lookup.
    order = sorted(tiles)
    index = {ch: i for i, ch in enumerate(order)}
    table = [tiles[ch] for ch in order]
    garbage_ids = np.array([index[c] for c in GARBAGE], np.int32)

    labels = dict((kind, bake_label(word, rgb, 0.30))
                  for kind, word in LABELS.items())
    chrome = bake_chrome(width, height, rgb, y_rule_top, y_rule_bot)
    caret = np.empty((glyph_h, 3, 3), np.uint8)
    caret[:] = tuple(int(c * 0.55) for c in rgb)
    # A full word space before the caret, not a letter gutter. Sitting a few
    # pixels off the last glyph it stops reading as a cursor and starts reading
    # as another letter -- SECRETS and ASTRONOMY both end up looking as though
    # they have gained an I, which on a demo whose entire point is that you can
    # read both phrases is the one mistake worth avoiding.
    caret_gap = 3 * scale

    fly_dur = 3.2 / max(args.speed, 0.05)
    scr_dur = max(args.scramble, 0.1) / max(args.speed, 0.05)
    hold_dur = max(args.hold, 0.1)
    trips = max(args.cycle, 1)

    # Per pair: the tiles, their two homes, and the per-tile timing that keeps
    # them from moving in lockstep.
    acts = []
    for first, second in pairs:
        slots_a, right_a = layout(first, scale, width)
        slots_b, right_b = layout(second, scale, width)
        matched = assign(slots_a, slots_b)
        count = len(matched)
        act = {
            "ids": np.array([index[ch] for ch, _, _ in matched], np.int32),
            "xa": np.array([xa for _, xa, _ in matched], f32),
            "xb": np.array([xb for _, _, xb in matched], f32),
            "right_a": right_a,
            "right_b": right_b,
            # Departure order, forwards and back. Two different orders so the
            # return trip is not the first one run in reverse. The spread is
            # wide on purpose: with everything leaving together, fourteen
            # letters are all in the middle third of the panel at once and the
            # crossing turns to mush.
            "lag_out": rng.uniform(0.0, 0.55, count).astype(f32),
            "lag_back": rng.uniform(0.0, 0.55, count).astype(f32),
            # Every letter rises; only how far varies. Alternating up and down
            # would put half of them through the status line. The spread is the
            # other half of staying trackable -- two letters passing through
            # each other at the same height merge into one blob.
            "arc": (rng.uniform(3.0, 15.0, count)
                    * max(args.arc, 0.0)).astype(f32),
            # When each tile stops being ciphertext. Spread across most of the
            # scramble so the phrase assembles raggedly rather than at once.
            "lock": rng.uniform(0.18, 0.94, count).astype(f32),
            # Ciphertext and its flicker, as a table indexed by int(t * rate):
            # random, but a pure function of t.
            "noise": garbage_ids[rng.integers(0, len(GARBAGE), (256, count))],
            "flick": rng.integers(5, 12, (256, count)).astype(np.int32),
        }
        acts.append(act)

    # The schedule. One act per pair: solve into the first phrase, then bounce
    # between the two, then fall apart again for the next pair.
    plan = []
    clock = 0.0
    for pair_act in acts:
        plan.append((clock, scr_dur, "scramble", pair_act, 0))
        clock += scr_dur
        for _ in range(trips):
            for kind, dur, side in (("hold", hold_dur, 0),
                                    ("fly", fly_dur, 0),
                                    ("hold", hold_dur, 1),
                                    ("fly", fly_dur, 1)):
                plan.append((clock, dur, kind, pair_act, side))
                clock += dur
        plan.append((clock, scr_dur * 0.5, "dissolve", pair_act, 0))
        clock += scr_dur * 0.5
    total = clock
    starts = [p[0] for p in plan]

    fb = np.zeros((height, width, 3), np.uint8)
    lit = np.full(max(len(a["ids"]) for a in acts), FULL, np.int32)

    def sample(when):
        """(ids, x, y, level, entry) for every tile at time `when`.

        Pure in `when`: no frame-to-frame state, so the trail can simply ask
        for the positions of 60 ms ago and the whole thing can be entered at
        any t.
        """
        at = when % total
        entry = plan[max(bisect.bisect_right(starts, at) - 1, 0)]
        t0, dur, kind, act, side = entry
        u = (at - t0) / dur
        n = len(act["ids"])

        if kind == "hold":
            home = act["xb"] if side else act["xa"]
            return act["ids"], home, np.full(n, f32(y_base)), lit[:n], entry

        if kind in ("scramble", "dissolve"):
            # Dissolve is the scramble run backwards: the tiles that locked
            # last come apart first.
            done = (act["lock"] <= u) if kind == "scramble" else \
                   (act["lock"] > u)
            slot = int(at * 17.0) & 255
            ids = np.where(done, act["ids"], act["noise"][slot])
            lvl = np.where(done, FULL, act["flick"][slot])
            return ids, act["xa"], np.full(n, f32(y_base)), lvl, entry

        # In flight. Each tile waits out its own lag, then has the rest of the
        # phase to cross, so they leave in a wave and still all land together.
        lag = act["lag_back"] if side else act["lag_out"]
        src = act["xb"] if side else act["xa"]
        dst = act["xa"] if side else act["xb"]
        k = np.clip((u - lag) / (1.0 - lag), 0.0, 1.0)
        # Overshoot and settle. A letter that decelerates smoothly into its
        # slot looks like a tween; one that arrives slightly long and rocks
        # back looks like an object with mass.
        v = k - 1.0
        eased = 1.0 + 2.35 * v * v * v + 1.35 * v * v
        x = src + (dst - src) * eased
        y = y_base - act["arc"] * np.sin(np.pi * k)
        # A letter off its line is dimmer than one seated in the phrase, so the
        # two resolved states are the brightest thing the demo ever shows.
        lvl = np.where(k * (1.0 - k) > 0.0, TRANSIT, FULL)
        return act["ids"], x, y, lvl, entry

    def blit(tile, x0, y0):
        """np.maximum a tile into the frame, clipped to the panel."""
        h, w = tile.shape[:2]
        sx0 = -x0 if x0 < 0 else 0
        sy0 = -y0 if y0 < 0 else 0
        sx1 = w - (x0 + w - width) if x0 + w > width else w
        sy1 = h - (y0 + h - height) if y0 + h > height else h
        if sx1 <= sx0 or sy1 <= sy0:
            return
        view = fb[y0 + sy0:y0 + sy1, x0 + sx0:x0 + sx1]
        np.maximum(view, tile[sy0:sy1, sx0:sx1], out=view)

    def draw(when, dim):
        ids, xs, ys, lvls, entry = sample(when)
        for i in range(len(ids)):
            y0 = int(ys[i] + 0.5)
            lvl = int(lvls[i]) if dim < 0 else dim
            blit(table[ids[i]][lvl][y0 & 1], int(xs[i] + 0.5), y0)
        return entry

    def render(t, frame):
        fb[:] = chrome
        entry = plan[max(bisect.bisect_right(starts, t % total) - 1, 0)]
        kind, act, side = entry[2], entry[3], entry[4]

        if kind == "fly" and not args.no_trails:
            # A ghost of where the letters were 60 ms ago, which is what gives
            # each one a direction the eye can follow. One ghost, not two: a
            # longer trail starts filling the gaps between letters and the
            # crossing reads as a smear rather than as objects.
            draw(t - 0.06, TRAIL)
        draw(t, -1)

        if show_label:
            blit(labels[kind], 8, y_label)
        if kind in ("hold", "scramble") and (t % 1.6) < 1.0:
            right = act["right_b"] if side else act["right_a"]
            blit(caret, min(right + caret_gap, width - 4), y_base)
        return fb

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
