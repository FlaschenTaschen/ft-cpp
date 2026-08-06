#!/usr/bin/env python3
"""Code typing itself out, with a cursor.

The wall used to show three Arduino one-liners as static text: a layer was
set, it sat there for five seconds, it was cleared. The joke is fine but a
still frame on a 320x64 panel is a poster, and people walk past posters.

Typed out a character at a time it is a different thing entirely -- you read
it at the speed it appears, the cursor gives it a pulse, and the pauses at the
end of each line are what make it feel like someone is at the keyboard rather
than like a slideshow. It is also the only demo here that anyone in the space
can add to without touching any code, since the lines are just an argument.

The typing is deliberately uneven. A constant interval reads as a machine
printing; a little jitter, a longer pause after a semicolon and a longer one
still after a blank line, reads as a person. The text is coloured the way an
editor would: keywords, strings, numbers and comments each get their own
colour, which on a panel this size does more for legibility than it does for
authenticity, because it breaks a run of same-coloured pixels into words.

Run:  python3 console.py --host 127.0.0.1
      python3 console.py --lines 'void setup() {;  Serial.begin(9600);;}'
      python3 console.py --cps 18 --palette-text 8ef0c0
"""

import os
import random
import re
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# The three that were on the wall, plus enough around them to fill a screen
# and give the typing somewhere to go. ';' separates lines.
DEFAULT = (
    "// betelgeuse, 320x64;"
    "void setup() {;"
    '  WiFi.begin("SequoiaFabrica", "********");;'
    '  Serial.println("Hello Sequoia Fabrica!");;'
    "};"
    ";"
    "void loop() {;"
    "  while (1) { blink(); };"
    "}"
)

KEYWORDS = {"void", "while", "for", "if", "else", "return", "int", "float",
            "char", "bool", "true", "false", "const", "static", "break",
            "setup", "loop", "delay", "def", "class", "import", "from"}

# Editor-ish colours, picked bright because they are competing with an LED
# panel's own glow rather than with a white page.
COL_TEXT = (200, 214, 232)
COL_KEYWORD = (128, 176, 255)
COL_STRING = (140, 226, 150)
COL_NUMBER = (246, 190, 120)
COL_COMMENT = (110, 122, 140)
COL_PUNCT = (170, 180, 200)
COL_CURSOR = (255, 220, 120)

TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|//.*|\b\d+\b|\w+|\s+|.')


def add_arguments(ap):
    ap.add_argument("--lines", default=DEFAULT, help="';' between lines")
    ap.add_argument("--cps", type=float, default=22.0,
                    help="characters per second")
    ap.add_argument("--jitter", type=float, default=0.5,
                    help="how uneven the typing is, 0..1")
    ap.add_argument("--line-pause", type=float, default=0.35,
                    help="extra seconds at the end of a line")
    ap.add_argument("--hold", type=float, default=2.5,
                    help="seconds to admire the finished screen")
    ap.add_argument("--font-px", type=int, default=11, help="glyph height")
    ap.add_argument("--cursor-hz", type=float, default=1.6)
    ap.add_argument("--gutter", dest="gutter", action="store_true", default=True)
    ap.add_argument("--no-gutter", dest="gutter", action="store_false",
                    help="hide the line numbers down the left")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def find_font(px):
    """A monospaced face if there is one, since this is meant to be code."""
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                 "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
                 "/System/Library/Fonts/Menlo.ttc"):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                pass
    return ImageFont.load_default()


def colourise(line):
    """-> [(text, rgb)], one run per token."""
    if line.strip().startswith("//") or line.strip().startswith("#"):
        return [(line, COL_COMMENT)]
    runs = []
    for tok in TOKEN.findall(line):
        if tok.startswith(("//", "#")):
            colour = COL_COMMENT
        elif tok[0] in "\"'":
            colour = COL_STRING
        elif tok.isdigit():
            colour = COL_NUMBER
        elif tok in KEYWORDS:
            colour = COL_KEYWORD
        elif tok.isspace() or tok.isalnum() or "_" in tok:
            colour = COL_TEXT
        else:
            colour = COL_PUNCT
        runs.append((tok, colour))
    return runs


def build(args):
    from PIL import Image, ImageDraw

    W, H = args.width, args.height
    rng = random.Random(args.seed or None)
    font = find_font(args.font_px)

    lines = args.lines.split(";")
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def advance(text):
        return probe.textlength(text, font=font)

    line_h = max(args.font_px + 1, 8)
    visible = max(1, H // line_h)
    gutter_w = int(advance("00 ")) if args.gutter else 0

    # Every character's arrival time, worked out once. Doing it up front is
    # what lets render() be a pure function of t -- the demo can be started at
    # any moment, seeked, or run at any frame rate, and still look the same.
    schedule = []                            # (time, line index, char count)
    clock = 0.0
    base = 1.0 / max(args.cps, 1e-3)
    for i, line in enumerate(lines):
        for n in range(len(line) + 1):
            schedule.append((clock, i, n))
            step = base * (1.0 + args.jitter * (rng.random() - 0.5) * 2.0)
            if n < len(line) and line[n:n + 1] in ";{}":
                step *= 2.2                  # a beat after the punctuation
            clock += max(step, 0.005)
        clock += args.line_pause * (2.5 if not line.strip() else 1.0)
    total = clock + args.hold

    # Each line pre-rendered at full length, then revealed by clipping to the
    # width of the characters typed so far. Rendering the partial string every
    # frame would re-lay-out the text constantly, and worse, a proportional
    # font would make the earlier glyphs shift as later ones arrive.
    baked = []
    for line in lines:
        w = max(1, int(advance(line)) + 2)
        img = Image.new("RGB", (w, line_h), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        x = 0.0
        for text, colour in colourise(line):
            draw.text((x, 0), text, font=font, fill=colour)
            x += advance(text)
        baked.append((np.asarray(img, np.uint8), [advance(line[:n])
                                                  for n in range(len(line) + 1)]))

    numbers = []
    if args.gutter:
        for i in range(len(lines)):
            img = Image.new("RGB", (max(gutter_w, 1), line_h), (0, 0, 0))
            ImageDraw.Draw(img).text((0, 0), "%2d" % (i + 1), font=font,
                                     fill=(74, 84, 102))
            numbers.append(np.asarray(img, np.uint8))

    out = np.zeros((H, W, 3), np.uint8)
    cursor = np.array(COL_CURSOR, np.uint8)

    def state_at(t):
        """Binary search the schedule for where the typing has got to."""
        lo, hi = 0, len(schedule) - 1
        if t >= schedule[hi][0]:
            return schedule[hi][1], schedule[hi][2]
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if schedule[mid][0] <= t:
                lo = mid
            else:
                hi = mid - 1
        return schedule[lo][1], schedule[lo][2]

    def render(t, frame):
        t = t % total
        cur_line, typed = state_at(t)
        out[:] = 0

        # Scroll so the line being typed is always on screen, and never scroll
        # back: text that jumps up and down as it is written is unreadable.
        top = max(0, cur_line - visible + 1)
        for row, i in enumerate(range(top, min(top + visible, len(lines)))):
            y = row * line_h
            if y + line_h > H:
                break
            if args.gutter and i < len(numbers):
                g = numbers[i]
                out[y:y + g.shape[0], :g.shape[1]] = g
            img, widths = baked[i]
            n = typed if i == cur_line else (len(lines[i]) if i < cur_line else 0)
            if i > cur_line:
                continue
            w = int(widths[min(n, len(widths) - 1)])
            w = min(w, W - gutter_w, img.shape[1])
            if w > 0:
                out[y:y + img.shape[0], gutter_w:gutter_w + w] = img[:, :w]
            if i == cur_line:
                cx = gutter_w + w
                # Blink only while idle; a cursor that blinks through the
                # typing looks like a fault rather than like a caret.
                on = (t * args.cursor_hz) % 1.0 < 0.55 or n < len(lines[i])
                if on and cx < W - 1:
                    out[y + 1:y + line_h - 1, cx:cx + 1] = cursor
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
