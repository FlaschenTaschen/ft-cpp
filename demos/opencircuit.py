#!/usr/bin/env python3
"""Open Circuit SF's next workshop, on the green phosphor tube it deserves.

Open Circuit SF is the hands-on electronics group whose workshops run in the
room this wall is in. Their website is a CRT: a photograph of a tube with a
canvas inside it, green type, scanlines, a soft bloom. **That is not decoration
to be sampled, it is their identity**, so this panel does not put their name in
a tidy sans-serif rectangle and call it branded. It becomes the tube.

The whole panel is one monochrome intensity field driven through a phosphor
ramp, and every element -- the logotype, the typed lines, the circuit rail
along the bottom -- is drawn as beam current rather than as colour. There is
exactly one colour decision in the file, the ramp, which is why the picture
holds together: nothing on it can be off-brand because nothing on it chooses
its own colour. `--phosphor amber` and `--phosphor white` swap the ramp for the
other two tubes that were actually sold (P3 and P4); green is P1 and is theirs.

**The name is the animation.** Along the bottom is a circuit: a wire, a
resistor, a capacitor, an LED and a switch. The switch starts *open* -- the
circuit is open, the LED is dark, which is the group's name drawn literally --
then it closes, a charge pulse runs the length of the trace, and the LED
flashes as the pulse reaches it. It is the only thing on the panel that moves
continuously, it is on every page, and it is what makes a wall of static type
read as alive rather than as a frozen render.

## What it shows, and what it does when it has nothing

Three pages on a loop: the identity card (the logotype and who they are), the
next workshop, and the one after it. The workshop pages carry the date and
start time at double height, the title, the venue and street, the seat count
and how far away it is -- which is the question somebody walking past actually
has. Then the tube powers down, the raster collapsing to a line and a dot, and
the loop starts again with it warming up. The power cycle is the loop seam:
there is no cut anywhere in this demo, because a CRT that blinks to a new
picture is a CRT nobody has ever seen.

The data comes off `ftdata.py`'s disk cache and nothing else -- `build()` and
`render()` open one JSON file, never a socket, for the reason in that file's
docstring. **The fetcher has to have run.** When it has not, the identity page
still draws, because the group's name is not a fact that expires, and the
workshop page becomes an honest card naming the fetcher and the cache path.
When the cache is there but the programme is empty -- a real state of a small
volunteer group between terms -- it says so and points at the website, which is
different from being broken and is drawn differently. Past three times the
record's TTL the details are not drawn at all: a wall advertising a class that
was cancelled a fortnight ago is the one failure worth designing against.

## Why render() has no phosphor buffer

The obvious way to get persistence is to keep a decaying frame between calls.
This does not, because `render(t, frame)` here is a pure function of `t`:
ftsched builds a segment ahead of time and starts it at t=0, the preview baker
drives it at a fixed step, and the wall's own loop drifts, so the callback has
to be enterable at any moment at any frame rate and land in the same picture.

So persistence is drawn rather than accumulated. The only thing on the panel
that moves fast enough to need a trail is the charge pulse, and its trail is
just the pulse at a few earlier positions with decaying weight -- which is
`--trail` echoes of a 320-element gaussian, computed from `t` alone, costing
less than the buffer would have. Everything else is static once drawn and wants
*bloom*, which is spatial and pure: a blurred copy of the field added back to
it, plus a weaker copy shifted a couple of columns right, because a composite
signal rings to the right of a bright edge and that ring is a third of why a
photograph of a terminal looks like a terminal.

## Cost

One (64, 320) float32 field, a separable bloom computed on a quarter of it,
and one `np.take` through a 256-entry ramp straight into the output buffer.
Measured over a whole loop on the wall's Pi -- a 1.2 GHz Pi 3 running Python
3.9.2 and numpy 1.19.5 -- that is **9.2 ms p50, 12.0 ms p95, 15.7 ms worst**
at 320x64, inside the 33 ms a 30 fps frame has. For scale, on the same machine
in the same session `fire` is 10.5 ms, `tunnel` 7.4 and `wardial` 7.2, so this
sits with the effects rather than with the cheap type panels. `build()` is
29 ms, once, on the worker thread.

Two measured decisions got it there from 17.2 ms, and both are the same
mistake in different clothes -- doing work on three channels that only needed
doing on one, or at a resolution finer than the thing being computed:

  * **the dither goes on the ramp index, not on the colour it produces.**
    `ds.dither()` on the (64, 320, 3) output costs 9.1 ms; offsetting the
    one-plane index instead and taking from a uint8 ramp costs 2.7 ms for the
    same stipple. It also stops rebuilding the Bayer tile sixty times a second.
  * **the bloom is computed at half resolution in both axes**, 4.6 ms down to
    2.4 ms. A halo is low-frequency by definition, so a quarter of the pixels
    carries it exactly as well.

The type is a baked 5x7 bitmap font -- teletext.py's, extended here by ten
glyphs -- so there is no font file to be missing on the Pi and no Pillow
import.

Run:  python3 opencircuit.py --host 127.0.0.1
      python3 opencircuit.py --phosphor amber --scanline 0.4
      python3 opencircuit.py --at 1788485000        # pin the clock, for a photo
      FT_DATA_CACHE=/tmp/nothing python3 opencircuit.py     # the no-data card
"""

import os
import sys
import time

import numpy as np

import demoscene as ds
import ftdata
import teletext

f32 = ds.f32

PRODUCT = "opencircuit"

# Past this many TTLs the workshop details are not drawn at all. Same multiple
# as propagation.py uses, and for the same reason: a little past its TTL a
# record is still worth showing with a caveat, and days past it is a lie.
STALE_MULTIPLE = 3.0


# --------------------------------------------------------------------------
# Type. teletext.py's 5x7 font is the table -- see the note above it there on
# why the glyphs are literals rather than a system face -- extended with the
# ten characters a workshop title and a domain name turn out to need.
# --------------------------------------------------------------------------

FONT = dict(teletext.FONT)
FONT.update({
    # A centred dot, which is the separator this panel uses everywhere. The
    # full stop sits on the baseline and reads as the end of a sentence; this
    # one sits on the middle row and reads as "and also".
    "\xb7": "00000 00000 00000 00100 00000 00000 00000",
    "_": "00000 00000 00000 00000 00000 00000 11111",
    "#": "01010 01010 11111 01010 11111 01010 01010",
    "@": "01110 10001 10111 10101 10111 10000 01110",
    '"': "01010 01010 00000 00000 00000 00000 00000",
    ";": "00000 01100 01100 00000 01100 00100 01000",
    "$": "00100 01111 10100 01110 00101 11110 00100",
    "[": "01110 01000 01000 01000 01000 01000 01110",
    "]": "01110 00010 00010 00010 00010 00010 01110",
    "|": "00100 00100 00100 00100 00100 00100 00100",
})

# Anything the font does not have is drawn as a hollow box rather than as a
# space. A title that arrives with a character nobody anticipated should look
# wrong on the wall, because that is how it gets fixed; silently swallowing it
# produces a plausible sentence with a word missing, which does not.
MISSING = "11111 10001 10001 10001 10001 10001 11111"

GLYPH_W, GLYPH_H, CELL_W = 5, 7, 6      # 5x7 on a 6-column pitch


def _bank():
    """Every glyph as a (7, 5) bool array, plus the missing-character box."""
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

    Scaling is `np.repeat`, so a double-height glyph is the same glyph with
    every row and column doubled. That is what a character generator running at
    half the dot clock actually did, and it is why the big type here has the
    same shapes as the small type rather than a second font's.
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
    """Columns a string occupies, trailing letter-space included."""
    return len(text) * CELL_W * scale


def centre(text, scale, w):
    """x for a centred string, biased left so an odd remainder is not a wobble."""
    return max(0, (w - width(text, scale)) // 2)


# --------------------------------------------------------------------------
# Phosphors. Three real ones, quoted rather than invented: P1 is the green
# every terminal in this story had, P3 the amber that was sold as the kinder
# alternative, P4 the white of a television. Each ramp runs unlit -> body ->
# saturated -> core burn, and the top of all three goes towards white, because
# a phosphor driven hard stops being its own colour -- which is exactly what
# makes a bright glyph on a dark tube look hot rather than merely bright.
#
# The green's saturated stop is #68FF23, which is Open Circuit SF's own accent
# off their stylesheet. That is the one borrowed number in the file.
# --------------------------------------------------------------------------

PHOSPHORS = {
    "green": [(0.00, (0, 0, 0)), (0.10, (2, 10, 3)), (0.30, (14, 62, 10)),
              (0.55, (44, 150, 22)), (0.78, (104, 255, 35)),
              (0.92, (190, 255, 140)), (1.00, (236, 255, 218))],
    "amber": [(0.00, (0, 0, 0)), (0.10, (10, 5, 0)), (0.30, (70, 30, 0)),
              (0.55, (160, 82, 4)), (0.78, (255, 160, 24)),
              (0.92, (255, 214, 130)), (1.00, (255, 245, 214))],
    "white": [(0.00, (0, 0, 0)), (0.10, (5, 7, 8)), (0.30, (40, 50, 58)),
              (0.55, (110, 128, 140)), (0.78, (196, 214, 226)),
              (0.92, (236, 244, 250)), (1.00, (255, 255, 255))],
}


def add_arguments(ap):
    ap.add_argument("--phosphor", default="green", choices=sorted(PHOSPHORS),
                    help="P1 green, P3 amber or P4 white")
    ap.add_argument("--loop", type=float, default=44.0,
                    help="seconds for one power-on to power-off cycle")
    ap.add_argument("--cps", type=float, default=34.0,
                    help="characters a second the terminal types at")
    ap.add_argument("--scanline", type=float, default=0.30,
                    help="how much every third row is darkened (0 = off)")
    ap.add_argument("--bloom", type=float, default=0.55,
                    help="how much of the blurred field is added back")
    ap.add_argument("--vignette", type=float, default=0.42,
                    help="how dark the corners of the glass go (0 = flat)")
    ap.add_argument("--flicker", type=float, default=0.05,
                    help="amplitude of the mains-hum brightness wobble")
    ap.add_argument("--trail", type=int, default=7,
                    help="echoes drawn behind the charge pulse")
    ap.add_argument("--pulse", type=float, default=4.6,
                    help="seconds for one trip around the circuit")
    ap.add_argument("--no-rail", dest="rail", action="store_false",
                    help="drop the circuit along the bottom")
    ap.add_argument("--at", type=float, default=0.0,
                    help="pin the clock to this epoch, for a repeatable photo")
    ap.add_argument("--cache-dir", default=None,
                    help="where ftdata.py writes; defaults to $FT_DATA_CACHE "
                         "or ~/.cache/ftdata")


# --------------------------------------------------------------------------
# The record, and the three things it can be.
# --------------------------------------------------------------------------

FRESH, AGING, STALE, ABSENT = "fresh", "aging", "stale", "absent"


class Feed(object):
    """The opencircuit record off the cache, with what its age means."""

    def __init__(self, cache_dir, now):
        got = ftdata.load(PRODUCT, cache_dir)
        self.ttl = ftdata.ttl_for(PRODUCT) or 21600.0
        self.payload, self.age = (got if got else (None, None))
        if got is None:
            self.state = ABSENT
        elif self.age <= self.ttl:
            self.state = FRESH
        elif self.age <= self.ttl * STALE_MULTIPLE:
            self.state = AGING
        else:
            self.state = STALE
        # Anything that has already finished is not upcoming, whatever the
        # record still holds -- the fetcher keeps the running one on purpose
        # and that is the fetcher's business, not the panel's. Only the class
        # happening *right now* survives the filter, and it is flagged so the
        # page can say ON NOW rather than counting down to the past.
        self.events = []
        if self.usable:
            for e in (self.payload.get("ev") or []):
                try:
                    start, dur = float(e["t"]), float(e.get("d") or 0.0)
                except (KeyError, TypeError, ValueError):
                    continue
                if start + dur >= now:
                    self.events.append(e)

    @property
    def usable(self):
        return self.state in (FRESH, AGING) and isinstance(self.payload, dict)

    def age_text(self):
        return "--" if self.age is None else ftdata.describe_age(self.age)


DOW = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")      # tm_wday order
MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
       "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def when_line(epoch):
    """`THU SEP 3 . 6:30 PM`, in the wall's own local time.

    The feed stamps UTC and `ftdata` stores the epoch, so this is the only
    place the panel decides what o'clock it is -- through `time.localtime`,
    which on the wall is the makerspace's own zone.
    """
    lt = time.localtime(epoch)
    hour = lt.tm_hour % 12 or 12
    return "%s %s %d \xb7 %d:%02d %s" % (
        DOW[lt.tm_wday], MON[lt.tm_mon - 1], lt.tm_mday,
        hour, lt.tm_min, "AM" if lt.tm_hour < 12 else "PM")


def days_between(now, epoch):
    """Calendar days from one local date to another.

    Rounded rather than floored because a day is not always 86400 seconds: the
    two Sundays a year that are 23 and 25 hours long would otherwise make
    tomorrow read as today or as the day after. Both boundaries go through
    `mktime` on the local date, so the answer is what a calendar says.
    """
    a = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    b = time.mktime(time.localtime(epoch)[:3] + (0, 0, 0, 0, 0, -1))
    return int(round((b - a) / 86400.0))


def countdown(epoch, dur, now):
    """How far away it is, in the coarsest unit that is still true.

    Coarse on purpose. `IN 22 DAYS` is what somebody reading a wall can use;
    `IN 21D 7H 14M` is a number they have to do arithmetic on to use, and the
    arithmetic is wrong by the time they have done it.

    **Counted in calendar days, not in elapsed hours**, which is the whole
    reason `days_between` exists. A class at half past six tomorrow evening is
    twenty-two hours away at half past eight tonight, and `IN 22 HOURS` is both
    true and useless: what a person wants to know is whether it is *tonight or
    tomorrow*, and those are properties of the calendar rather than of the
    interval. The one place elapsed time wins is inside the last hour, where
    `TODAY` has stopped being the useful half of the answer.
    """
    if now >= epoch:
        return "ON NOW" if now <= epoch + dur else "STARTED"
    days = days_between(now, epoch)
    if days <= 0:
        away = epoch - now
        if away < 3600:
            return "IN %d MIN" % max(1, int(away / 60))
        return "TODAY"
    if days == 1:
        return "TOMORROW"
    if days < 14:
        return "IN %d DAYS" % days
    return "IN %d WEEKS" % int(round(days / 7.0))


# --------------------------------------------------------------------------
# Pages. A page is a list of lines; a line is a baked bitmap, where it goes,
# how bright it burns, and whether it is typed or simply there.
# --------------------------------------------------------------------------

class Line(object):
    # `text` is kept alongside the bitmap it produced. Nothing in render()
    # reads it -- it is there so that scripts/test-opencircuit.py can assert
    # about the words on the panel rather than about its pixels, which is the
    # only way to check the thing that actually matters here: that a stale
    # record produces a page with no date on it.
    __slots__ = ("img", "x", "y", "level", "typed", "chars", "scale", "text")

    def __init__(self, text, x, y, scale=1, level=1.0, typed=True):
        self.img = bake(text, scale)
        self.x, self.y, self.scale = x, y, scale
        self.level, self.typed = level, typed
        self.chars = len(text)
        self.text = text


class Page(object):
    """Lines plus the schedule that reveals them, one character at a time."""

    def __init__(self, lines, cps, header=None):
        self.lines = lines
        self.header = header            # drawn whole, never typed, never wiped
        self.marks = []                 # (time this line finishes typing)
        clock = 0.0
        for ln in lines:
            if ln.typed:
                clock += ln.chars / max(cps, 1.0)
                clock += 0.16           # the beat at the end of a line
            self.marks.append(clock)
        self.type_time = clock

    def caret(self, tt):
        """(line index, columns revealed) for the line being typed at tt."""
        for i, ln in enumerate(self.lines):
            if tt < self.marks[i]:
                if not ln.typed:
                    continue
                start = self.marks[i - 1] if i else 0.0
                span = max(self.marks[i] - start - 0.16, 1e-3)
                frac = min(1.0, max(0.0, (tt - start) / span))
                return i, int(round(ln.chars * frac)) * CELL_W * ln.scale
        return len(self.lines), 0


# --------------------------------------------------------------------------

W_RAIL = 8                      # the circuit occupies the bottom eight rows
RULE_Y = 9                      # the hairline under the header

# The four components, as (natural x on a 320-wide panel, pixel span). The
# positions scale with the panel and the spans do not, because a resistor
# eight pixels long is a smudge rather than a resistor. Anything that would
# not fit at its scaled position, or would land on the component before it,
# is left out -- so a narrow panel loses parts of the circuit rather than
# drawing them on top of each other, and a very narrow one is a bare trace.
RAIL_TUNED_W = 320
RESISTOR_X, RESISTOR_W = 46, 36
CAP_X, CAP_W = 117, 6
LED_X, LED_W = 186, 12
SWITCH_X, SWITCH_W = 250, 20


def rail_art(width):
    """The circuit as a dim mask, plus where the LED and the switch are.

    Drawn as literals in an 8-row strip: a wire the width of the panel with a
    resistor, a capacitor, an LED and a switch inline. Everything is a
    rectangle or a run of pixels, so it rasterises exactly and no component is
    one pixel off its own wire.

    Returns (art, wire row, LED centre, switch left contact). The last two are
    None when there was no room for that component, which the caller reads as
    "there is nothing here to light up" rather than as an error.
    """
    art = np.zeros((W_RAIL, width), f32)
    wire = 4                                    # the row the trace runs along
    if width < 8:
        return art, wire, None, None
    art[wire, 2:width - 2] = 1.0

    scale = width / float(RAIL_TUNED_W)
    edge = [2]                                  # right edge of the last fitted

    def place(x, span):
        """Where this component goes, or None if it cannot go anywhere."""
        x0 = int(round(x * scale))
        x0 = max(x0, edge[0] + 4)
        if x0 + span > width - 3:
            return None
        edge[0] = x0 + span
        return x0

    # -- resistor: the zigzag, four peaks, drawn as line segments.
    x0 = place(RESISTOR_X, RESISTOR_W)
    if x0 is not None:
        x1 = x0 + RESISTOR_W
        art[wire, x0:x1] = 0.0
        span, peak = (x1 - x0) / 8.0, 3
        for k in range(8):
            a = x0 + span * k
            for j in range(int(span) + 1):
                xx = int(a + j)
                frac = j / max(span, 1.0)
                up = (k % 2 == 0)
                dy = int(round(peak * (frac if up else 1.0 - frac)))
                if 0 <= xx < width:
                    art[wire - dy, xx] = 1.0
                    art[wire - dy - 1, xx] = max(art[wire - dy - 1, xx], 0.35)

    # -- capacitor: two plates and the gap between them.
    cx = place(CAP_X, CAP_W)
    if cx is not None:
        cx += 1
        art[wire, cx - 1:cx + 3] = 0.0
        art[wire - 3:wire + 4, cx - 1] = 1.0
        art[wire - 3:wire + 4, cx + 2] = 1.0

    # -- LED: a triangle into a bar, with two emission ticks above it.
    lx = place(LED_X, LED_W)
    if lx is not None:
        art[wire, lx:lx + 12] = 0.0
        for k in range(5):
            art[wire - k:wire + k + 1, lx + 1 + k] = 1.0   # the filled triangle
        art[wire - 4:wire + 5, lx + 7] = 1.0               # the cathode bar
        for dx, dy in ((2, -6), (5, -6)):
            art[wire + dy, lx + dx] = 0.8
            art[wire + dy + 1, lx + dx + 1] = 0.8
            art[wire + dy + 2, lx + dx + 2] = 0.8

    # -- switch: the two contacts. The arm is drawn per frame, since whether
    #    it is open is the one thing about this circuit that changes.
    sx = place(SWITCH_X, SWITCH_W)
    if sx is not None:
        art[wire, sx:sx + 20] = 0.0
        art[wire - 1:wire + 2, sx] = 1.0
        art[wire - 1:wire + 2, sx + 19] = 1.0
    return art, wire, (lx + 4) if lx is not None else None, sx


def switch_arm(art, wire, sx, closed):
    """The switch arm, from the left contact to the right one or up in the air.

    `closed` runs 0 (fully open) to 1 (shut). The arm is a straight run of
    pixels between the two contacts, lifted at the far end -- which is what a
    knife switch looks like and, more to the point, is unmistakably *open* at
    a glance from across a room.
    """
    if sx is None:
        return
    lift = int(round((1.0 - closed) * 5))
    span = 19
    for j in range(span + 1):
        frac = j / float(span)
        y = wire - int(round(lift * frac))
        x = sx + j
        if 0 <= y < art.shape[0] and 0 <= x < art.shape[1]:
            art[y, x] = 1.0


# --------------------------------------------------------------------------

def build(args):
    W, H = args.width, args.height
    now0 = args.at if args.at else time.time()
    cache = args.cache_dir or ftdata.CACHE_DIR
    feed = Feed(cache, now0)

    org = "OPEN CIRCUIT SF"
    host = "OPENCIRCUITSF.COM"
    if feed.usable:
        org = str(feed.payload.get("org") or org)
        host = str(feed.payload.get("host") or host)

    cps = max(args.cps, 1.0)

    # ---------------------------------------------------------- the pages
    # The identity card. It carries no fact that can expire, so it is drawn
    # identically whether the fetcher has ever run or not -- which is the
    # whole reason the panel does not go blank when the cache is empty.
    logo_scale = 3 if width(org, 3) <= W - 8 else 2
    tag = "HANDS-ON ELECTRONICS \xb7 SAN FRANCISCO"
    ident = Page([
        Line(org, centre(org, logo_scale, W), 11, logo_scale, 1.0, typed=False),
        Line(tag, centre(tag, 1, W), 38, 1, 0.72),
        Line(host, centre(host, 1, W), 48, 1, 0.86),
    ], cps)

    def header(label):
        """The top strip: whose panel this is, and how old what it says is."""
        return [Line(org, 2, 0, 1, 0.62, typed=False),
                Line(label, W - width(label, 1) - 2, 0, 1, 0.9, typed=False)]

    def workshop(ev, label):
        when = when_line(float(ev["t"]))
        title = str(ev.get("n") or "")
        place = " \xb7 ".join([p for p in (str(ev.get("v") or ""),
                                           str(ev.get("a") or "")) if p])
        far = countdown(float(ev["t"]), float(ev.get("d") or 0.0), now0)
        cap = int(ev.get("cap") or 0)
        foot = far if not cap else ("%s \xb7 %d SEATS" % (far, cap))
        # Double height for the date if it fits, single if the title is long
        # enough that both cannot have it. The date wins that contest: the
        # title is the same four words every month and the date is not.
        big = 2 if width(when, 2) <= W - 4 else 1
        rows = [Line(when, 2, 11, big, 1.0)]
        y = 11 + GLYPH_H * big + 4
        rows.append(Line(title[:52], 2, y, 1, 0.92))
        rows.append(Line(place[:52], 2, y + 10, 1, 0.68))
        rows.append(Line(foot[:52], 2, y + 20, 1, 0.86))
        return Page(rows, cps, header=header(label))

    def card(lines, label):
        """A page of plain statements. What the panel says when it has none."""
        rows = []
        for i, (text, level) in enumerate(lines):
            rows.append(Line(text[:52], 2, 12 + i * 10, 1, level))
        return Page(rows, cps, header=header(label))

    pages = [(ident, 6.6)]
    if feed.events:
        pages.append((workshop(feed.events[0], "NEXT"), 15.5))
        if len(feed.events) > 1:
            pages.append((workshop(feed.events[1], "THEN"), 11.0))
    elif feed.usable:
        # The cache is good and the programme is empty. That is a real state of
        # a small volunteer group between terms, and it is not a fault, so it
        # gets a page that says what to do rather than an error.
        pages.append((card([("NO WORKSHOPS SCHEDULED", 1.0),
                            ("THE PROGRAMME IS BETWEEN TERMS", 0.7),
                            ("DATES GO UP AT", 0.7),
                            (host, 0.95)], "IDLE"), 13.0))
    else:
        why = ("RECORD IS %s OLD" % feed.age_text()) if feed.state == STALE \
            else "NO RECORD IN THE CACHE"
        pages.append((card([("NO WORKSHOP DATA", 1.0),
                            (why, 0.75),
                            ("RUN: PYTHON3 FTDATA.PY --ONCE", 0.8),
                            (("CACHE: " + cache.upper())[:52], 0.6)],
                           "NO DATA"), 13.0))
    pages.append((ident, 5.4))

    warm, down = 2.6, 1.8
    span = warm + down + sum(d for _, d in pages)
    scale = max(args.loop, 8.0) / span
    warm, down = warm * scale, down * scale
    marks, clock = [], warm
    for page, dur in pages:
        marks.append((clock, clock + dur * scale, page))
        clock += dur * scale
    total = clock + down

    # ------------------------------------------------------- the fixed art
    # The ramp is uint8 and the dither is applied to the *index* rather than
    # to the RGB it produces. Same stipple, a third of the arithmetic: the
    # index is one plane and the colour is three. Measured on the wall's Pi:
    # 9.1 ms through ds.dither() on the RGB, 2.7 ms doing it here. It works
    # because the ramp is monotone and smooth, so
    # jittering which of two neighbouring entries a pixel takes is exactly the
    # dither that jittering their colours would have produced.
    ramp = ds.gradient(PHOSPHORS[args.phosphor], 256, dtype=np.uint8)

    # ds.dither() rebuilds this tile on every call, which at 30 fps is a
    # (64, 320) allocation and fill sixty times a second for a matrix that
    # never changes. Built once here instead, in the [0, 1) form that note
    # explains: uint8 truncates rather than rounds, so the offset has to be
    # one-sided or every pixel lands half a level dark.
    bayer = np.tile(ds._BAYER8, (-(-H // 8), -(-W // 8)))[:H, :W].astype(f32)

    # max(..., 0.5) rather than the true half-extent: a one-pixel panel has no
    # centre to be away from, and a NaN here becomes a garbage ramp index at
    # the very end of render() where it is much harder to recognise.
    yy = (np.arange(H, dtype=f32) - (H - 1) / 2.0) / max((H - 1) / 2.0, 0.5)
    xx = (np.arange(W, dtype=f32) - (W - 1) / 2.0) / max((W - 1) / 2.0, 0.5)
    # An ellipse, and then a hard-ish shoulder: the glass is clear across the
    # middle of the tube and falls off only near the bezel. A smooth radial
    # falloff instead dims the whole picture, which is not what glass does.
    r = np.sqrt(xx[None, :] ** 2 * 0.82 + yy[:, None] ** 2)
    glass = 1.0 - args.vignette * np.clip((r - 0.62) / 0.55, 0.0, 1.0) ** 1.7
    glass = glass.astype(f32)
    # The dark floor, folded into the glass so it is one add rather than a
    # multiply and an add: an unlit tube is not black, it is the faint ambient
    # green of the phosphor with the room in the glass over it.
    ambient = (glass * f32(0.045)).astype(f32)

    scan = np.ones(H, f32)
    if args.scanline > 0:
        scan[0::3] = 1.0 - min(args.scanline, 0.95)
    scan = scan[:, None]

    rail, rail_wire, led_x, sw_x = rail_art(W)
    # The circuit sits on the bottom edge, wherever that is. A panel too short
    # to hold both a page and a rail does not get one: the workshop is the
    # content and the circuit is the flourish, so the flourish is what goes.
    rail_y = H - W_RAIL
    has_rail = args.rail and H >= 24 and W >= 24
    cols = np.arange(W, dtype=f32)

    # Deterministic noise. A table indexed by a function of t, rather than a
    # generator called per frame: the same t has to give the same picture.
    rng = np.random.RandomState(0x0C5F)
    hiss = rng.random_sample(2048).astype(f32)
    rowjit = rng.randint(-6, 7, size=4096)

    beam = np.zeros((H, W), f32)
    field = np.empty((H, W), f32)
    # The bloom's working pair, at half resolution in both axes. H and W are
    # even on every panel this runs on; an odd one would drop its last row or
    # column out of the halo, which is why the slices are taken rather than
    # assumed to tile exactly.
    HH, WW = H // 2, W // 2
    small = np.empty((HH, WW), f32)
    sblur = np.empty((HH, WW), f32)
    band = np.empty((H, W), f32)
    rail_buf = np.empty((W_RAIL, W), f32)
    lit = np.empty((W_RAIL, W), f32)
    glow = np.empty(W, f32)
    out = np.zeros((H, W, 3), np.uint8)
    idx = np.empty((H, W), np.intp)

    def stamp(img, x, y, level, cols_max=None):
        """Burn a baked bitmap into the beam, brightest-wins."""
        h, w = img.shape
        if cols_max is not None:
            w = min(w, max(0, cols_max))
        # Clipped on all four sides, cropping the bitmap rather than moving
        # it. A right-aligned header on a panel narrower than its own label
        # starts at a negative x, and sliding it back on screen would put the
        # end of the word where the start of it belongs.
        sx, sy = max(0, -x), max(0, -y)
        x, y = max(0, x), max(0, y)
        h = min(h - sy, H - y)
        w = min(w - sx, W - x)
        if h <= 0 or w <= 0:
            return
        tile = beam[y:y + h, x:x + w]
        np.maximum(tile, img[sy:sy + h, sx:sx + w] * f32(level), out=tile)

    def draw_rail(tt):
        """The circuit, and the charge going round it."""
        cycle = max(args.pulse, 0.5)
        phase = (tt % cycle) / cycle
        # Shut at 12% of the cycle, open again at 88%. Between those the
        # circuit is closed, which is the only time the pulse exists.
        closed = np.clip((phase - 0.06) / 0.10, 0.0, 1.0) \
            if phase < 0.5 else np.clip((0.94 - phase) / 0.10, 0.0, 1.0)
        art = rail_buf
        np.copyto(art, rail)
        switch_arm(art, rail_wire, sw_x, float(closed))
        np.multiply(art, f32(0.30), out=lit)

        if closed > 0.99:
            run = np.clip((phase - 0.16) / 0.66, 0.0, 1.0)
            head = 4.0 + run * (W - 8.0)
            glow[:] = 0.0
            for k in range(max(args.trail, 1)):
                px = head - k * 5.0
                if px < 0:
                    break
                d = (cols - px) / 7.0
                np.maximum(glow, np.exp(-d * d) * (0.92 ** k), out=glow)
            np.add(lit, art * glow[None, :] * f32(0.95), out=lit)
            # The LED fires as the charge arrives and holds for a moment, the
            # way a filament does not but an eye does.
            since = 9.9 if led_x is None else (head - led_x) / (W - 8.0)
            if 0.0 <= since <= 0.22:
                f = float(np.exp(-((since / 0.09) ** 2)))
                ly, lx = rail_wire, led_x
                y0, y1 = max(0, ly - 6), min(W_RAIL, ly + 7)
                x0, x1 = max(0, lx - 10), min(W, lx + 11)
                dy = (np.arange(y0, y1, dtype=f32) - ly)[:, None]
                dx = (np.arange(x0, x1, dtype=f32) - lx)[None, :]
                blob = np.exp(-(dx * dx / 26.0 + dy * dy / 9.0)) * f32(f)
                np.maximum(lit[y0:y1, x0:x1], blob, out=lit[y0:y1, x0:x1])
        np.maximum(beam[rail_y:rail_y + W_RAIL], lit,
                   out=beam[rail_y:rail_y + W_RAIL])

    def draw_page(page, tt, held):
        if page.header:
            for ln in page.header:
                stamp(ln.img, ln.x, ln.y, ln.level)
            if H > RULE_Y and W > 6:
                beam[RULE_Y, 2:W - 2] = np.maximum(
                    beam[RULE_Y, 2:W - 2], f32(0.34))
        cur, revealed = page.caret(tt)
        for i, ln in enumerate(page.lines):
            if i < cur or not ln.typed:
                stamp(ln.img, ln.x, ln.y, ln.level)
            elif i == cur:
                stamp(ln.img, ln.x, ln.y, ln.level, cols_max=revealed)
        # The caret. It blinks only once the typing has stopped; one that
        # blinks through the typing reads as a fault rather than as a caret.
        if cur < len(page.lines):
            ln = page.lines[cur]
            cx, cy, ch = ln.x + revealed, ln.y, GLYPH_H * ln.scale
            on = True
        else:
            ln = page.lines[-1]
            cx = ln.x + ln.img.shape[1]
            cy, ch = ln.y, GLYPH_H * ln.scale
            on = (held * 1.9) % 1.0 < 0.55
        if on and 0 <= cx < W - 4:
            beam[cy:min(H, cy + ch), cx:min(W, cx + 4)] = 1.0

    def render(t, frame):
        tt = t % total
        beam[:] = 0.0

        # -------------------------------------------------- what is on screen
        # The picture is always the same picture; the power cycle only decides
        # how much of the tube is scanning it. That is what makes the loop
        # seamless -- there is no cut anywhere in this demo, because the
        # raster collapses to a line and a dot and comes back out of one.
        open_frac = 1.0
        if tt < warm:
            k = tt / warm
            if k < 0.20:
                # The line strikes, then grows out of the middle of the tube.
                half = (k / 0.20) ** 0.6 * (W / 2.0 - 2.0)
                mid = H // 2
                beam[mid - 1:mid + 1,
                     int(W / 2 - half):int(W / 2 + half) + 1] = \
                    min(1.0, k / 0.05)
                open_frac = None
            else:
                open_frac = min(1.0, (k - 0.20) / 0.60)
                draw_page(marks[0][2], 0.0, 0.0)
                if has_rail:
                    draw_rail(tt)
        elif tt >= total - down:
            k = (tt - (total - down)) / down
            if k < 0.55:
                open_frac = max(0.0, 1.0 - k / 0.55)
                last = marks[-1][2]
                draw_page(last, last.type_time + 9.0, 9.0)
                if has_rail:
                    draw_rail(tt)
            else:
                # The spot: the last of the charge on a tube with no scan
                # left, shrinking to nothing in the middle of the glass.
                s = max(0.0, 1.0 - (k - 0.55) / 0.45)
                mid = H // 2
                half = max(1, int(round(s * s * (W / 2.0 - 2.0))))
                beam[mid - 1:mid + 1,
                     W // 2 - half:W // 2 + half] = 0.35 + 0.65 * s
                open_frac = None
        else:
            for start, end, page in marks:
                if start <= tt < end:
                    draw_page(page, tt - start, tt - start - page.type_time)
                    break
            if has_rail:
                draw_rail(tt)

        # A page that has just arrived has not locked yet: the first fraction
        # of a second of it tears sideways, which is what a picture does when
        # the source changes and the horizontal oscillator has not caught up.
        for start, _end, _page in marks:
            since = tt - start
            if 0.0 <= since < 0.30:
                amp = (1.0 - since / 0.30) ** 2
                off = (rowjit[(int(tt * 240.0) + np.arange(H)) % rowjit.size]
                       * amp).astype(np.int32)
                rows = np.arange(H)[:, None]
                take = (np.arange(W)[None, :] + off[:, None]) % W
                beam[:] = beam[rows, take]
                break

        # ----------------------------------------------------- the tube
        if open_frac is not None and open_frac < 0.999:
            # The raster is not at full height yet: the same picture, scanned
            # into a band in the middle of the tube. Nearest-neighbour rather
            # than an average, because a half-open raster genuinely drops scan
            # lines -- it does not blend them -- and the aliasing is the look.
            mid = (H - 1) / 2.0
            keep = max(2, int(round(H * open_frac)))
            y0 = int(round(mid - keep / 2.0))
            rows = np.arange(keep, dtype=f32) + y0 - mid
            src = np.clip(np.round(rows / max(open_frac, 1e-3) + mid)
                          .astype(np.int32), 0, H - 1)
            band[:] = 0.0
            lo = max(0, y0)
            band[lo:lo + keep] = beam[src[:H - lo]]
            # Overscan: the top and bottom of a squeezed raster pile up, which
            # is why a television being switched off has a bright rim.
            edge = f32(0.5 + 0.5 * (1.0 - open_frac))
            band[lo] = np.maximum(band[lo], edge)
            band[min(H - 1, lo + keep - 1)] = np.maximum(
                band[min(H - 1, lo + keep - 1)], edge)
            beam[:] = band

        # Bloom, at half resolution in both axes. A halo is low-frequency by
        # definition -- that is what makes it a halo -- so computing it on a
        # quarter of the pixels and stretching it back is not an approximation
        # anybody can see, and on the wall's Pi it is 2.4 ms where the same
        # kernel at full resolution is 4.6 ms. The full-resolution `beam` is still
        # what is added on top of it, so the glyphs keep their hard edges and
        # only the glow around them is coarse.
        #
        # Deliberately wider across than down: the beam is swept horizontally
        # and a bright run smears along its own scan line further than it does
        # into its neighbours.
        even = beam[:HH * 2, :WW * 2]
        np.add(even[0::2, 0::2], even[1::2, 0::2], out=small)
        np.add(small, even[0::2, 1::2], out=small)
        np.add(small, even[1::2, 1::2], out=small)
        np.multiply(small, f32(0.25), out=small)

        np.copyto(sblur, small)
        sblur[:, 1:] += small[:, :-1] * f32(0.70)
        sblur[:, :-1] += small[:, 1:] * f32(0.70)
        sblur[1:] += small[:-1] * f32(0.45)
        sblur[:-1] += small[1:] * f32(0.45)
        # The ring: a weaker copy a few columns to the right of every bright
        # edge, which is what a composite signal does to a hard transition and
        # a third of why a photograph of a terminal reads as one. Asymmetric,
        # so it is a ring and not more halo.
        sblur[:, 2:] += small[:, :-2] * f32(0.26)
        np.multiply(sblur, f32(args.bloom / 3.56), out=sblur)

        np.copyto(field, beam)
        wide = field[:HH * 2, :WW * 2]
        wide[0::2, 0::2] += sblur
        wide[0::2, 1::2] += sblur
        wide[1::2, 0::2] += sblur
        wide[1::2, 1::2] += sblur

        # Mains hum plus a little valve noise, both functions of t alone.
        if args.flicker > 0:
            n = hiss[int(tt * 41.0) % hiss.size]
            np.multiply(field, f32(1.0 + args.flicker
                                   * (0.6 * np.sin(tt * 2.0 * np.pi * 7.3)
                                      + 0.4 * (n - 0.5) * 2.0)), out=field)
        np.multiply(field, glass, out=field)
        np.multiply(field, scan, out=field)
        # The unlit tube is not black: a CRT with the gun off still shows the
        # faint ambient green of the phosphor and the grey of the glass.
        np.add(field, ambient, out=field)

        # One intensity, dithered, through one 256-entry ramp, straight into
        # the output buffer. See the note on `ramp` for why the dither goes on
        # the index rather than on the colour that comes out of it.
        np.multiply(field, f32(255.0), out=field)
        np.add(field, bayer, out=field)
        np.clip(field, 0.0, 255.0, out=field)
        np.copyto(idx, field, casting="unsafe")
        np.take(ramp, idx, axis=0, out=out)
        return out

    # The plan, hung off the callback for the test script. See Line.text.
    render.pages = marks
    render.total = total
    render.feed = feed
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
