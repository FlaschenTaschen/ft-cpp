#!/usr/bin/env python3
"""A war dialer working its way through a San Francisco exchange.

Before the web there was a telephone number, and before you had a number worth
calling you had a program that dialed every number in an exchange and wrote
down which ones answered with a carrier. WarGames put one on screen in 1983 and
called it nothing in particular; by the time ToneLoc and BlueBeep were passed
around on floppies it had a name, a config file and a scan log. The display was
always the same three things: a log of what each number did, a map of the
exchange filling in as it went, and -- if you had the modem's speaker on -- the
call progress audio. This panel is those three things.

**The numbers are the reserved fictional block, and that is also the joke.**
Every number here is 555-0100 through 555-0199, which is the range the North
American numbering plan sets aside so that films and demos can put a number on
a screen without ringing a stranger's phone. It happens to be exactly one
hundred numbers, which is exactly a 10x10 grid, which is exactly what the
exchange map wants to be. And 555 is not an arbitrary choice of the film
industry either: in the days of two-letter exchange names it was KLondike 5,
which is why the panel calls it that. A demo on a wall in a public workshop
should not display a dialable stranger's number, and here the safe choice and
the authentic one are the same choice.

The area code is San Francisco's: 415, with 628 -- the overlay added in 2015
when 415 ran out -- available as `--area 628`. Everything else is invented:
the systems that answer are fictional boards named after this city.

**The whole scan is a schedule, computed once.** build() draws every attempt's
start time, outcome and duration from a seeded generator and sorts them into
one list; render() binary searches it. Nothing carries state between frames,
because ftsched builds a segment ahead of time and starts it at t=0, the
preview baker drives it at a fixed step, and the wall's own loop drifts -- so
render() has to be a pure function of t that can be entered anywhere at any
frame rate and land in the same picture.

**Length.** Each outcome takes its own time -- a busy signal comes back at
once, a number that merely rings has to be given long enough to not answer --
so the loop is a little different for every seed. Measured across 39 of them it
runs 41.2 to 44.9 s including the three-second hold on the finished scan, which
is what `--cps` is set to: a 45 s slot sees the whole exchange swept and totted
up rather than being cut off half way down the map.

**Cost.** The log is four baked RGB strips copied into place, the exchange map
is one np.take through a baked index image the way wopr paints its lamp banks,
and the call-progress trace is one 320-element envelope turned into a mask.
Nothing per-frame touches the whole frame more than twice, which is what keeps
it inside the budget on the 600 MHz Pi 3 that drives this wall.

**The trace is call progress, not audio.** It is an envelope -- signal level
against time, about a second and a half of it scrolling right to left -- and
not a waveform, because 320 columns cannot represent 1600 Hz and pretending
otherwise just draws noise. What it does represent honestly is *cadence*, which
is the part a human recognises anyway: the stutter of the dial, the two-on
four-off of a US ring, the half-second chop of a busy signal, and the flat
unbroken wall of a carrier.

Run:  python3 wardial.py --host 127.0.0.1
      python3 wardial.py --area 628 --cps 3.5
      python3 wardial.py --seed 7 --no-trace
"""

import os
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# Call outcomes, their share of a hundred numbers, and how they read. The
# weights are what an evening on a residential exchange actually looked like:
# most numbers are somebody's home phone and simply ring out, a fifth are
# picked up by a person who says hello to a modem, and the carriers -- the only
# reason to run the program at all -- are a handful.
#
# (label, weight, colour, seconds the attempt takes)
OUTCOMES = (
    ("NO ANSWER",  46, (150, 96, 26),  0.46),
    ("VOICE",      22, (235, 92, 52),  0.34),
    ("BUSY",       16, (205, 130, 34), 0.26),
    ("TIMEOUT",     6, (150, 104, 38), 0.62),
    ("FAX",         4, (120, 220, 255), 0.40),
    ("CARRIER",     6, (120, 255, 140), 0.58),
)

# What answers, when something answers. All invented, all local. 1:125 was
# genuinely FidoNet's San Francisco net, and a board that quoted its node
# number in the banner was a board that wanted you to know it carried mail.
SYSTEMS = (
    "SEQUOIA FABRICA  NODE 1",
    "FOG CITY BBS  1:125/7",
    "THE SUNSET UNDERGROUND",
    "POTRERO HILL ELITE",
    "MISSION ROLLERDROME",
    "ALCATRAZ ECHO  1:125/12",
    "BALBOA HIGH  LAB 2",
    "OCEAN BEACH SALVAGE",
    "TWIN PEAKS REPEATER",
    "EMBARCADERO FREENET",
)

# Connect rates, oldest first. A scan that reported 14400 on every hit would be
# a scan of one modem pool; a real evening's log is a museum of what everyone
# happened to own that year.
RATES = ("300", "1200", "2400", "2400", "9600", "14400")

AMBER = (255, 172, 40)
DIM = (128, 84, 20)
UNLIT = (26, 17, 5)


def add_arguments(ap):
    ap.add_argument("--area", default="415", choices=("415", "628"),
                    help="San Francisco area code; 628 is the 2015 overlay")
    ap.add_argument("--exchange", default="555",
                    help="the exchange to sweep. The default is the reserved "
                         "fictional block -- change it and the panel starts "
                         "displaying numbers that ring somebody")
    ap.add_argument("--cps", type=float, default=3.0,
                    help="calls per second. The default is set so the hundred "
                         "numbers, the pauses and the hold all land inside a "
                         "45 s slot -- the scan finishes and totals up rather "
                         "than being cut off part way through the exchange")
    ap.add_argument("--hold", type=float, default=3.0,
                    help="seconds to hold the finished scan before looping")
    ap.add_argument("--font-px", type=int, default=10, help="log glyph height")
    ap.add_argument("--trace", dest="trace", action="store_true", default=True)
    ap.add_argument("--no-trace", dest="trace", action="store_false",
                    help="drop the call-progress strip along the bottom")
    ap.add_argument("--order", default="random", choices=("random", "serial"),
                    help="random scatters the map as it fills, which is what "
                         "the scanners did to look less like a scan; serial "
                         "walks it in order")
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


def build(args):
    from PIL import Image, ImageDraw

    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    font = find_font(max(5, int(args.font_px)))

    # ------------------------------------------------------------------ layout
    head_h = 10
    wave_h = 11 if args.trace else 0
    body_y0 = head_h + 1
    body_y1 = H - wave_h - (1 if wave_h else 0)
    map_w = min(116, W // 3)
    map_x0 = W - map_w
    sep_x = map_x0 - 3
    log_w = max(16, sep_x - 2)

    line_h = max(6, int(args.font_px))
    rows_n = max(1, (body_y1 - body_y0) // line_h)

    out = np.zeros((H, W, 3), np.uint8)

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def advance(text):
        return float(probe.textlength(text, font=font))

    def strip(parts, width):
        """Bake one log row: [(text, colour), ...] laid out left to right."""
        img = Image.new("RGB", (max(1, width), line_h), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        x = 0.0
        for text, colour in parts:
            draw.text((x, 0), text, font=font, fill=tuple(int(c) for c in colour))
            x += advance(text)
        return np.asarray(img, np.uint8)

    # ------------------------------------------------------------- the scan
    # Every attempt, decided once. `order` is the sequence the hundred numbers
    # are tried in; `slot` is where each lands so the map can be indexed by
    # line number rather than by attempt.
    n = 100
    order = np.arange(n)
    if args.order == "random":
        rng.shuffle(order)

    labels = [o[0] for o in OUTCOMES]
    weights = np.array([o[1] for o in OUTCOMES], f32)
    weights /= weights.sum()
    kinds = rng.choice(len(OUTCOMES), size=n, p=weights)

    # A carrier is worth dwelling on: the log gets a second row for the banner
    # of whatever answered, so the panel occasionally has something to read
    # besides a column of NO ANSWER.
    sys_pick = rng.integers(0, len(SYSTEMS), n)
    rate_pick = rng.integers(0, len(RATES), n)

    step = 1.0 / max(args.cps, 1e-3)
    starts = np.empty(n, f32)
    ends = np.empty(n, f32)
    clock = 0.6                                   # a beat before the first dial
    events = []                                   # (time, strip index)
    strips = []

    num_w = advance("415-555-0148  ")
    for k in range(n):
        line = int(order[k])
        kind = int(kinds[k])
        label, _, colour, dwell = OUTCOMES[kind]
        number = "%s-%s-01%02d" % (args.area, args.exchange, line)
        starts[k] = clock
        # The dwell is the outcome's own: a busy signal comes back at once, a
        # number that simply rings has to be given time to not answer.
        clock += step * (dwell / 0.40)
        ends[k] = clock
        clock += step * 0.15                      # hang up, seize the line again

        strips.append(strip([(number + "  ", DIM), (label, colour)], log_w))
        events.append((float(ends[k]), len(strips) - 1))
        if label == "CARRIER":
            strips.append(strip(
                [("  >> ", (90, 160, 100)),
                 (SYSTEMS[int(sys_pick[k])], (120, 255, 140))], log_w))
            events.append((float(ends[k]) + 0.10, len(strips) - 1))
            strips.append(strip(
                [("     CONNECT ", (90, 160, 100)),
                 (RATES[int(rate_pick[k])], (170, 255, 190))], log_w))
            events.append((float(ends[k]) + 0.20, len(strips) - 1))

    # The dialing row, one per attempt, shown while the call is in progress.
    dialing = [strip([("%s-%s-01%02d" % (args.area, args.exchange, int(order[k]))
                       + "  ", AMBER), ("DIALING", (255, 214, 120))], log_w)
               for k in range(n)]

    total = float(clock) + max(args.hold, 0.0)
    ev_t = np.array([e[0] for e in events], f32)
    ev_i = [e[1] for e in events]

    # --------------------------------------------------------- the exchange map
    # Painted the way wopr paints its lamp banks: a baked index image saying
    # which of the hundred cells owns each pixel, so a frame is one gather and
    # a multiply rather than a hundred rectangle writes.
    cell_w, cell_h = map_w // 10, max(2, (body_y1 - body_y0) // 10)
    grid_w, grid_h = cell_w * 10 - 1, cell_h * 10 - 1
    gx0 = map_x0 + (map_w - grid_w) // 2
    gy0 = body_y0 + (body_y1 - body_y0 - grid_h) // 2
    map_idx = np.full((H, W), -1, np.int32)
    for r in range(10):
        for c in range(10):
            y = gy0 + r * cell_h
            x = gx0 + c * cell_w
            map_idx[y:y + cell_h - 1, x:x + cell_w - 1] = r * 10 + c
    map_sub = map_idx[body_y0:body_y1, map_x0:W]
    map_mask = map_sub >= 0
    map_take = np.where(map_mask, map_sub, 0)

    # Cell colours: index 0 is "not tried yet", then one per outcome.
    cell_cols = np.zeros((len(OUTCOMES) + 1, 3), f32)
    cell_cols[0] = UNLIT
    for i, (_, _, colour, _) in enumerate(OUTCOMES):
        cell_cols[i + 1] = colour
    # line number -> outcome, and line number -> when it was tried
    line_kind = np.zeros(n, np.int32)
    line_when = np.zeros(n, f32)
    line_kind[order] = kinds + 1
    line_when[order] = ends
    cell_state = np.zeros(n, np.int32)
    # line_kind holds outcome+1, so that 0 can mean "not tried yet".
    CARRIER_KIND = labels.index("CARRIER") + 1

    # ------------------------------------------------------------- the header
    title = strip([("TONELOC  ", AMBER),
                   ("%s-%s-01XX" % (args.area, args.exchange), (255, 214, 120)),
                   ("  KLONDIKE 5", DIM)], sep_x)

    # ------------------------------------------------------ the progress trace
    if wave_h:
        wave_y0 = H - wave_h
        cols = np.arange(W, dtype=f32)
        # 1.6 s of history across the panel, newest at the right edge.
        col_dt = f32(1.6 / max(W - 1, 1))
        wave_rows = np.arange(wave_h, dtype=f32)[:, None]
        wave_mid = f32((wave_h - 1) * 0.5)

    def envelope(s):
        """Signal level at absolute times `s` -- an array. See the docstring.

        Outside any call the line is idle and nearly flat. Inside one, the
        shape is the outcome's: everything starts with the dial, and then the
        cadence is what tells you what happened.
        """
        env = np.full(s.shape, 0.04, f32)
        k = np.searchsorted(starts, s, side="right") - 1
        np.clip(k, 0, n - 1, out=k)
        live = (s >= starts[k]) & (s < ends[k])
        if not live.any():
            return env
        rel = (s - starts[k]) / np.maximum(ends[k] - starts[k], 1e-6)
        kind = kinds[k]

        # The dial itself: a burst per digit, four digits, then a pause while
        # the switch does its work.
        dial = (rel < 0.30) & live
        env[dial] = np.where(((rel[dial] / 0.055) % 1.0) < 0.62, 0.72, 0.06)

        after = (rel >= 0.30) & live
        # Ring: two seconds on, four off, which at this compression is a long
        # burst and a longer gap. Busy: half a second on, half off. Carrier:
        # the answer tone, then a wall that does not stop.
        ring = after & np.isin(kind, [0, 3])                # NO ANSWER, TIMEOUT
        env[ring] = np.where(((rel[ring] - 0.30) / 0.34 % 1.0) < 0.42, 0.55, 0.05)
        busy = after & (kind == 2)
        env[busy] = np.where(((rel[busy] - 0.30) / 0.10 % 1.0) < 0.5, 0.62, 0.05)
        voice = after & (kind == 1)
        # A person: irregular, and it stops when they hang up on the noise.
        env[voice] = 0.30 + 0.34 * np.abs(np.sin((rel[voice] - 0.30) * 34.0))
        carrier = after & np.isin(kind, [4, 5])             # FAX, CARRIER
        cr = (rel[carrier] - 0.30) / 0.70
        env[carrier] = np.where(cr < 0.35, 0.66, 0.92)
        return env

    def state_at(tt):
        """How many calls have been placed, and which event log row is last."""
        k = int(np.searchsorted(starts, tt, side="right")) - 1
        e = int(np.searchsorted(ev_t, tt, side="right")) - 1
        return k, e

    def render(t, frame):
        tt = t % total
        out[:] = 0

        # ------------------------------------------------------------ header
        out[0:line_h, 0:title.shape[1]] = title[:, :title.shape[1]]
        cur, last_ev = state_at(tt)
        tried = int(np.searchsorted(ends, tt, side="right"))
        # Counted over calls that have *finished*. Indexing a cumulative sum by
        # the call in progress credits a carrier the moment its number is
        # dialed, which puts the total one ahead of the log for half a second
        # every time one lands.
        found = int(((line_when <= tt) & (line_kind == CARRIER_KIND)).sum())
        head = strip([("%3d/100  " % tried, DIM),
                      ("%d CR" % found, (120, 255, 140))], map_w)
        out[0:line_h, map_x0:map_x0 + head.shape[1]] = head

        # ------------------------------------------------------- the exchange
        cell_state[:] = 0
        done = line_when <= tt
        cell_state[done] = line_kind[done]
        cols_now = cell_cols[cell_state]
        # The cell being dialed right now blinks, so the map has a cursor.
        if 0 <= cur < n and tt < ends[cur]:
            live_line = int(order[cur])
            lit = 0.35 + 0.65 * ((tt * 7.0) % 1.0 < 0.5)
            cols_now = cols_now.copy()
            cols_now[live_line] = np.array(AMBER, f32) * lit
        for c in range(3):
            plane = np.take(cols_now[:, c], map_take)
            plane *= map_mask
            np.copyto(out[body_y0:body_y1, map_x0:W, c], plane,
                      casting="unsafe")

        # a hairline between the two panes
        out[body_y0:body_y1, sep_x] = (40, 26, 8)

        # ------------------------------------------------------------- the log
        # The bottom row is the call in progress; the rows above it are the
        # most recent finished events, oldest at the top.
        show = []
        if 0 <= cur < n and tt < ends[cur]:
            show.append(dialing[cur])
            hist = rows_n - 1
        else:
            hist = rows_n
        for j in range(hist):
            i = last_ev - j
            if i < 0:
                break
            show.append(strips[ev_i[i]])
        for slot, img in enumerate(show):
            y = body_y0 + (rows_n - 1 - slot) * line_h
            if y < body_y0 or y + img.shape[0] > body_y1 + line_h:
                continue
            h = min(img.shape[0], body_y1 - y)
            if h > 0:
                out[y:y + h, 0:img.shape[1]] = img[:h]

        # ---------------------------------------------------------- the trace
        if wave_h:
            s = tt - (W - 1 - cols) * col_dt
            env = envelope(np.maximum(s, 0.0))
            half = env * f32(wave_h * 0.48)
            band = np.abs(wave_rows - wave_mid) <= half[None, :]
            tile = out[wave_y0:wave_y0 + wave_h]
            tile[:] = 0
            tile[..., 0] = band * 190
            tile[..., 1] = band * 120
            tile[..., 2] = band * 30
            # the zero line, so an idle pair of wires still reads as a pair of
            # wires rather than as a dead panel
            mid = int(wave_mid)
            tile[mid, :, 0] = np.maximum(tile[mid, :, 0], 70)
            tile[mid, :, 1] = np.maximum(tile[mid, :, 1], 46)
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
