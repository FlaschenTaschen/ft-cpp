#!/usr/bin/env python3
"""WOPR from WarGames: the lamp banks thinking, and the conversation printing.

Two things made the 1983 prop memorable and neither of them is a graphic. The
first is the machine itself -- a black monolith whose front is nothing but rows
of small amber and red lamps behind grille bars, blinking against each other
with no pattern you can name, and every so often a sweep running along one bank
that makes it look like a thought crossing the room. The second is the
terminal: chunky phosphor capitals arriving one at a time, a block cursor, and
the exchange that starts with GREETINGS PROFESSOR FALKEN and ends with somebody
asking a computer for a nice game of chess. This demo is both, side by side.

**The typing is a schedule, not a state machine.** Every character's arrival
time is worked out in build() into one sorted list, and render() binary
searches it. That is console.py's trick and it is here for the same reason: the
scheduler builds effects ahead of time and starts them at t=0, the preview
baker drives them at a fixed step, and the wall's own loop drifts -- so
render() has to be a pure function of t that can be entered at any moment and
at any frame rate and land in the same place. Nothing in this file carries
state between frames.

**The two speakers differ by colour and by rhythm.** WOPR prints steadily and
fast in the phosphor colour with only a few percent of jitter, because it is a
computer. The human's replies are paler, come in behind a `>` prompt that
appears before the first keystroke, run at a bit over half the speed, and carry
real hesitation: a wide random spread per keystroke plus an occasional quarter
second of nothing at a word boundary, which is what typing actually looks like
and what a constant interval never reads as. On a 320x64 panel you can tell
which of them is talking from across the room without reading a word.

**Text is baked once and revealed by slicing.** Each display row is drawn at
full length into its own little RGB strip in build(); a frame copies the first
`width of the characters typed so far` columns of it. Re-rendering the partial
string every frame would cost a Pillow layout per row per frame, and with any
proportional metric it also makes the earlier glyphs shift as the later ones
arrive. Only five rows of 12 px fit in 64, so the script is word-wrapped to the
terminal width in build() and scrolled the way console.py scrolls -- the row
being typed is always the bottom one, and it never scrolls back, because text
that jogs up and down is text nobody finishes reading.

**The lamps are 112 numbers, not 4600 pixels.** Each lamp gets a baked rate,
phase and duty cycle, so its brightness is `((t*rate + phase) % 1) < duty` --
an array op over a couple of hundred elements, deterministic, and with no RNG
called after build(). Painting them is a single gather: build() bakes an index
map (which lamp owns this pixel) and a weight map (1.0 in the lamp's core,
falling off around it), so the glow that stops a lamp reading as a rectangle is
free at run time. The chase sweeps are a gaussian in lamp-column space, one per
bank, with alternating directions and incommensurate speeds so the banks never
line up. The Pi 3 driving this wall is thirty to a hundred times slower than a
laptop on numpy, so the rule here was that nothing per-frame may touch the
whole frame more than a handful of times: measured, this is 0.05 ms/frame here
(0.1 in the `lights` layout, which paints all 320 columns), which scales to
something like 2-5 ms there against the rotation's calibration demos. It is the
cheapest effect in the show, because almost all of it is a memcpy.

**Length.** The whole exchange, eleven lines and 395 characters, lands in 35 to
38 s at the default speeds -- the spread is the human's hesitation, which is
drawn from --seed -- and then holds for three. So it fits inside a 45 s slot
and finishes on FINE, rather than being cut off mid-sentence and looping from
the top the way a longer extract would. That is what --cps defaults to 26 for:
20 is nicer to read, but the slot then ends before the game is chosen, which is
the whole joke. The idle raster glow and the lamp banks are lit at t=0, so
there is no black lead-in for the crossfade to land on.

The script is an argument, so anyone can retype it from the wall's control
panel. `;` separates lines; a line starting with `>` is the human at the
keyboard, anything else is WOPR. So `>HELLO.;HOW ARE YOU?` is one typed reply
followed by one machine line.

Run:  python3 wopr.py --host 127.0.0.1
      python3 wopr.py --layout lights --colour green
      python3 wopr.py --script 'SHALL WE PLAY A GAME?;>LOVE TO.' --cps 14
      python3 wopr.py --layout terminal --colour blue --no-scanlines
"""

import os
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# The Falken exchange, trimmed of stage business. ';' between lines, a leading
# '>' for the human. 392 characters, comfortably inside the editor's 512.
DEFAULT_SCRIPT = (
    "GREETINGS PROFESSOR FALKEN.;"
    ">HELLO.;"
    "HOW ARE YOU FEELING TODAY?;"
    ">I'M FINE. HOW ARE YOU?;"
    "EXCELLENT. IT'S BEEN A LONG TIME. CAN YOU EXPLAIN THE REMOVAL OF YOUR "
    "USER ACCOUNT NUMBER ON 6/23/73?;"
    ">PEOPLE SOMETIMES MAKE MISTAKES.;"
    "YES THEY DO. SHALL WE PLAY A GAME?;"
    ">LOVE TO. HOW ABOUT GLOBAL THERMONUCLEAR WAR?;"
    "WOULDN'T YOU PREFER A GOOD GAME OF CHESS?;"
    ">LATER. LET'S PLAY GLOBAL THERMONUCLEAR WAR.;"
    "FINE."
)

# Phosphor colours. All four are deliberately hot -- a CRT phosphor rendered at
# its real luminance on an LED panel looks like a screensaver of a screen,
# whereas the panel's own emission wants the glyph pushed to the top of the
# range so it survives the room's ambient light.
PHOSPHOR = {
    "amber": (255, 172, 40),
    "green": (120, 255, 140),
    "blue": (120, 195, 255),
    "white": (225, 235, 255),
}

# The lamps on the prop are amber and red behind smoked plastic, and they stay
# that way whatever the terminal is doing -- but a green-phosphor WOPR with
# orange lamps looks like two demos sharing a panel, so a third of the terminal
# colour is mixed into the lamp palette to tie them together.
LAMP_BASE = ((255, 148, 30), (255, 78, 22), (255, 200, 70))


def add_arguments(ap):
    ap.add_argument("--script", default=DEFAULT_SCRIPT,
                    help="the dialogue: ';' between lines, a leading '>' marks "
                         "a line the human types (slower, uneven, paler); "
                         "anything else is WOPR printing")
    ap.add_argument("--layout", default="split",
                    choices=("split", "terminal", "lights"),
                    help="split = lamp banks down the left and the terminal "
                         "beside them; terminal = full-width text; lights = "
                         "the machine's front panel alone")
    ap.add_argument("--colour", default="amber",
                    choices=tuple(sorted(PHOSPHOR)),
                    help="phosphor colour of the terminal, and the tint mixed "
                         "into the lamp banks")
    ap.add_argument("--cps", type=float, default=26.0,
                    help="characters per second WOPR prints at; the human "
                         "types at about half this")
    ap.add_argument("--jitter", type=float, default=0.7,
                    help="how uneven the human's typing is, 0..1 (WOPR is "
                         "always near-steady)")
    ap.add_argument("--line-pause", type=float, default=0.55,
                    help="extra seconds between one speaker's line and the "
                         "next; the beat that makes it a conversation")
    ap.add_argument("--hold", type=float, default=3.0,
                    help="seconds to hold the finished screen before looping")
    ap.add_argument("--font-px", type=int, default=11,
                    help="glyph height; 11 is five rows of text in 64")
    ap.add_argument("--cursor-hz", type=float, default=2.2,
                    help="block cursor blink rate while waiting")
    ap.add_argument("--lamp-rows", type=int, default=8,
                    help="rows of lamps in the banks; fewer means bigger lamps")
    ap.add_argument("--chase", type=float, default=1.0,
                    help="speed of the sweeps running along the banks; 0 stops "
                         "them and leaves only the blinking")
    ap.add_argument("--glow", type=float, default=1.0,
                    help="bleed around a lit lamp, 0..2; 0 gives hard blocks")
    ap.add_argument("--scanlines", dest="scanlines", action="store_true",
                    default=True)
    ap.add_argument("--no-scanlines", dest="scanlines", action="store_false",
                    help="drop the CRT raster on the screen glow and the "
                         "grille bars across the lamp banks")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def find_font(px):
    """A monospaced face if there is one, else whatever Pillow ships.

    Hard-coding one path is a demo that works on the laptop it was written on
    and is a black screen on the Pi, which has a different font package set.
    """
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


def wrap(text, advance, limit):
    """Greedy word wrap by measured pixel width. -> [row, ...]

    Measured rather than counted in characters: the fallback bitmap font is not
    the same metric as DejaVu Sans Mono, and a column count tuned here would
    overflow the panel on a machine that only has the fallback.
    """
    rows, cur = [], ""
    for word in text.split(" "):
        trial = word if not cur else cur + " " + word
        if cur and advance(trial) > limit:
            rows.append(cur)
            cur = word
        else:
            cur = trial
        # A single word wider than the panel: hand it over anyway rather than
        # loop forever trying to fit it.
        while advance(cur) > limit and len(cur) > 1:
            cut = len(cur) - 1
            while cut > 1 and advance(cur[:cut]) > limit:
                cut -= 1
            rows.append(cur[:cut])
            cur = cur[cut:]
    rows.append(cur)
    return rows or [""]


def build(args):
    from PIL import Image, ImageDraw

    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    font = find_font(max(5, int(args.font_px)))
    phos = np.array(PHOSPHOR[args.colour], f32)

    # ---------------------------------------------------------------- layout
    # The lamp strip is a fifth of the panel in `split`, which is enough for
    # fourteen lamps across -- fewer than that and a chase sweep has nothing to
    # travel along, more and the terminal drops below thirty characters a line.
    if args.layout == "lights":
        lamp_x0, lamp_w, term_x0, term_w = 0, W, 0, 0
    elif args.layout == "terminal":
        lamp_x0, lamp_w, term_x0, term_w = 0, 0, 0, W
    else:
        lamp_w = max(24, int(round(W * 0.215)))
        lamp_x0, term_x0, term_w = 0, lamp_w + 5, W - lamp_w - 5

    out = np.zeros((H, W, 3), np.uint8)

    # ------------------------------------------------------------ the lamps
    lamp_pack = None
    if lamp_w > 0:
        rows_n = max(2, int(args.lamp_rows))
        cell_h = max(3, H // rows_n)
        # A lamp is wider than it is tall, which is what the real bulbs behind
        # rectangular grille slots look like, and it also survives the panel's
        # coarse pitch better than a square dot.
        # A wide strip gets bigger lamps rather than more of them. Sixty-odd
        # 3 px dots across the whole panel came out as a texture with no scale
        # to it; at 5 px they are objects you can count, which is what the prop
        # looks like from across a room.
        core_w, cell_w = (5, 8) if lamp_w >= 160 else (3, 5)
        core_h = max(1, min(cell_h - 2, 2 + cell_h // 4))
        # Lamps come in panels of four with a wider gap between panels. An
        # evenly spaced field of dots reads as texture; the same dots broken
        # into groups read as hardware, which is the entire difference between
        # this and a starfield.
        pergroup, gap_x = 4, cell_w // 2
        cols_n = max(3, int(lamp_w // (cell_w + gap_x / float(pergroup))))
        span_x = [c * cell_w + (c // pergroup) * gap_x for c in range(cols_n)]
        pad_x = max(0, (lamp_w - (span_x[-1] + cell_w)) // 2)
        pad_y = (H - rows_n * cell_h) // 2

        n = rows_n * cols_n
        idx = np.zeros((H, lamp_w), np.int32)
        wgt = np.zeros((H, lamp_w), f32)
        glow = float(np.clip(args.glow, 0.0, 2.0))
        # 512 lamps at worst, once, at build time: a Python loop is fine here
        # and the alternative -- a vectorised nearest-lamp field -- is harder
        # to read for no run-time gain at all.
        for r in range(rows_n):
            for c in range(cols_n):
                i = r * cols_n + c
                y = pad_y + r * cell_h + (cell_h - core_h) // 2
                x = pad_x + span_x[c] + (cell_w - core_w) // 2
                # The halo goes down first so the core overwrites it, and it
                # claims only pixels no brighter lamp has claimed.
                if glow > 0:
                    y0, y1 = max(0, y - 1), min(H, y + core_h + 1)
                    x0, x1 = max(0, x - 1), min(lamp_w, x + core_w + 1)
                    halo = wgt[y0:y1, x0:x1]
                    take = halo < 0.22 * glow
                    halo[take] = 0.22 * glow
                    idx[y0:y1, x0:x1][take] = i
                y0, y1 = max(0, y), min(H, y + core_h)
                x0, x1 = max(0, x), min(lamp_w, x + core_w)
                wgt[y0:y1, x0:x1] = 1.0
                idx[y0:y1, x0:x1] = i

        if args.scanlines and core_h >= 3:
            # The grille. The prop's lamps sit behind a slotted fascia, so the
            # bottom row of every lamp is dimmed -- a shadow line across the
            # bulb. Two things that did not work: dimming the gaps between lamp
            # rows, which are already black and so changed nothing at all, and
            # dimming every other row of the whole strip, which halved each
            # lamp and turned the banks back into confetti.
            for r in range(rows_n):
                y = pad_y + r * cell_h + (cell_h - core_h) // 2 + core_h - 1
                if 0 <= y < H:
                    wgt[y, :] *= 0.45

        # Blink parameters, baked. Rates are spread over a decade so no two
        # lamps beat together for long; duty is biased low because a panel
        # where most lamps are lit most of the time has no texture in it.
        rate = (0.35 * (10.0 ** rng.uniform(0.0, 1.15, n))).astype(f32)
        phase = rng.random(n).astype(f32)
        duty = rng.uniform(0.18, 0.62, n).astype(f32)
        floor_ = rng.uniform(0.02, 0.10, n).astype(f32)
        # A few lamps are simply always on: status lights, and they give the
        # eye something fixed to measure the blinking against.
        steady = rng.random(n) < 0.06
        duty[steady] = 1.0

        lrow = np.repeat(np.arange(rows_n, dtype=np.int32), cols_n)
        lcol = (np.tile(np.arange(cols_n, dtype=f32), rows_n)
                / max(cols_n - 1, 1))
        # Mix the terminal's phosphor into the amber/red so the two halves of
        # the panel belong to the same object. See LAMP_BASE.
        base = np.array(LAMP_BASE, f32)[rng.integers(0, len(LAMP_BASE), n)]
        base = base * f32(0.68) + phos[None, :] * f32(0.32)
        base *= rng.uniform(0.72, 1.0, (n, 1)).astype(f32)

        # One sweep per bank, alternating direction, with periods that share no
        # common multiple worth waiting for.
        sw_rate = (0.13 + 0.052 * np.arange(rows_n, dtype=f32)) * float(args.chase)
        sw_dir = np.where(np.arange(rows_n) % 2 == 0, 1.0, -1.0).astype(f32)
        sw_phase = rng.random(rows_n).astype(f32)

        nlamp = n
        bright = np.empty(n, f32)
        field = np.empty((H, lamp_w), f32)
        tmp = np.empty((H, lamp_w), f32)
        rgb = np.empty((n, 3), f32)
        lamp_pack = (idx, wgt, rate, phase, duty, floor_, lrow, lcol, base,
                     sw_rate, sw_dir, sw_phase, bright, field, tmp, rgb, nlamp)

    # --------------------------------------------------------- the terminal
    line_h = max(7, int(args.font_px) + 1)
    visible = max(1, H // line_h)
    schedule = []
    strips = []                                  # (image, [reveal width, ...])
    total = 1.0

    if term_w > 0:
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

        def advance(text):
            return float(probe.textlength(text, font=font))

        prompt_w = int(advance("> "))
        human = phos * f32(0.45) + f32(0.55) * f32(255.0)
        human = np.clip(human * f32(0.92), 0, 255)
        col_wopr = tuple(int(v) for v in np.clip(phos, 0, 255))
        col_human = tuple(int(v) for v in human)
        col_prompt = tuple(int(v * 0.55) for v in col_human)

        # rows: (text, is_human, indent, show_prompt, is_line_end)
        rows = []
        for raw in args.script.split(";"):
            is_human = raw.startswith(">")
            text = raw[1:] if is_human else raw
            text = text.strip()
            indent = prompt_w if is_human else 0
            pieces = wrap(text, advance, term_w - indent - 3)
            for k, piece in enumerate(pieces):
                rows.append((piece, is_human, indent, is_human and k == 0,
                             k == len(pieces) - 1))

        # Every keystroke's arrival time, once. See the module docstring: this
        # is the only reason render() can be a pure function of t.
        clock = 0.0
        mach = 1.0 / max(args.cps, 1e-3)
        jit = float(np.clip(args.jitter, 0.0, 1.0))
        for r, (text, is_human, _, _, line_end) in enumerate(rows):
            step0 = mach * (2.05 if is_human else 1.0)
            for k in range(len(text) + 1):
                schedule.append((clock, r, k))
                if is_human:
                    # A person's inter-key interval is wide and lognormal-ish,
                    # and every so often they stop dead in the middle of a
                    # sentence. Both of those are what a flat rate lacks.
                    step = step0 * float(np.exp(rng.normal(0.0, 0.55 * jit)))
                    ch = text[k:k + 1]
                    if ch == " " and rng.random() < 0.18:
                        step += 0.20 + 0.35 * rng.random()
                    if k and text[k - 1:k] in ".,?!":
                        step += 0.28
                else:
                    step = step0 * float(1.0 + 0.09 * (rng.random() - 0.5))
                clock += max(step, 0.006)
            if line_end:
                clock += args.line_pause * (1.6 if is_human else 1.0)
        total = clock + max(args.hold, 0.0)

        for text, is_human, indent, show_prompt, _ in rows:
            w = max(1, int(advance(text)) + indent + 3)
            img = Image.new("RGB", (w, line_h), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            if show_prompt:
                draw.text((0, 0), ">", font=font, fill=col_prompt)
            draw.text((indent, 0), text, font=font,
                      fill=col_human if is_human else col_wopr)
            widths = [indent + int(advance(text[:k])) if k else indent
                      for k in range(len(text) + 1)]
            if not show_prompt and indent:
                widths[0] = 0                    # a wrapped row has no prompt
            strips.append((np.asarray(img, np.uint8), widths))

        # The raster: the screen is *on* from t=0, which is what stops the
        # crossfade into this effect landing on black. Baked, so it costs one
        # copy per frame rather than any arithmetic.
        wash = np.zeros((H, term_w, 3), f32)
        yy = np.arange(H, dtype=f32)[:, None]
        vig = f32(0.55) + f32(0.45) * np.sin(np.pi * (yy + 0.5) / H)
        wash += phos[None, None, :] * (f32(0.10) * vig)[:, :, None]
        if args.scanlines:
            wash[1::2] *= 0.35
        wash_u8 = np.clip(wash, 0, 255).astype(np.uint8)
        cursor_col = np.array(np.clip(phos * 1.0, 0, 255), np.uint8)
        cur_w = max(3, int(advance("M")))
    else:
        rows = []
        wash_u8 = None
        cursor_col = None
        cur_w = 4
        total = 12.0                             # lights-only: an arbitrary loop

    def state_at(t):
        """Binary search the schedule for where the typing has got to."""
        hi = len(schedule) - 1
        if t >= schedule[hi][0]:
            return schedule[hi][1], schedule[hi][2]
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if schedule[mid][0] <= t:
                lo = mid
            else:
                hi = mid - 1
        return schedule[lo][1], schedule[lo][2]

    def render(t, frame):
        tt = t % total
        out[:] = 0

        if lamp_pack is not None:
            (idx, wgt, rate, phase, duty, floor_, lrow, lcol, base,
             sw_rate, sw_dir, sw_phase, bright, field, tmp, rgb, n) = lamp_pack
            # Blink: a couple of hundred elements, no RNG, pure in t.
            np.multiply(rate, f32(tt), out=bright)
            bright += phase
            bright -= np.floor(bright)
            on = bright < duty
            np.copyto(bright, floor_)
            bright[on] = 1.0
            if args.chase:
                pos = (sw_rate * f32(tt) * sw_dir + sw_phase) % 1.0
                d = lcol - pos[lrow].astype(f32)
                np.multiply(d, d, out=d)
                d *= f32(-42.0)
                np.exp(d, out=d)
                bright += d * f32(0.85)
            # Clipped to 1, and it has to be: the store below is an unsafe
            # copy into uint8, so a lamp at 1.2 does not saturate, it wraps
            # round to dark -- which showed up as green confetti in the middle
            # of an amber panel and took a contact sheet to spot.
            np.clip(bright, 0.0, 1.0, out=bright)
            np.multiply(base, bright[:, None], out=rgb)

            # One gather plus three multiplies over the strip -- the glow and
            # the grille are already in `wgt`.
            for c in range(3):
                np.take(rgb[:, c], idx, out=field)
                np.multiply(field, wgt, out=tmp)
                np.copyto(out[:, lamp_x0:lamp_x0 + lamp_w, c], tmp,
                          casting="unsafe")

        if term_w > 0:
            out[:, term_x0:term_x0 + term_w] = wash_u8
            cur_row, typed = state_at(tt)
            # Scroll so the row being typed is the last one on screen, and
            # never scroll back.
            top = max(0, cur_row - visible + 1)
            for slot, i in enumerate(range(top, min(top + visible, len(rows)))):
                y = slot * line_h
                if y + line_h > H:
                    break
                if i > cur_row:
                    break
                img, widths = strips[i]
                k = typed if i == cur_row else len(widths) - 1
                w = int(widths[min(k, len(widths) - 1)])
                w = min(w, term_w - 1, img.shape[1])
                if w > 0:
                    out[y:y + img.shape[0], term_x0:term_x0 + w] = img[:, :w]
                if i == cur_row:
                    # A block cursor, solid while typing and blinking only when
                    # the line is finished -- a caret that blinks through the
                    # typing reads as a fault.
                    done = k >= len(widths) - 1
                    lit = (not done) or (tt * args.cursor_hz) % 1.0 < 0.55
                    cx = term_x0 + w + 1
                    if lit and cx + cur_w < term_x0 + term_w:
                        out[y + 1:y + line_h - 1, cx:cx + cur_w] = cursor_col
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
