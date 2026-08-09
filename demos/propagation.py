#!/usr/bin/env python3
"""HF propagation: the space weather a ham checks before deciding on a band.

This is an instrument panel, not an effect. Somebody walks past the wall,
looks at it for two seconds, and either goes and turns the radio on or does
not. So it is laid out the way a rack-mounted readout is laid out -- one
quantity per place, the same place every time, and the units and the age
printed next to the number rather than assumed.

**What is on it, and why those.** SFI is the 10.7 cm solar flux, the proxy for
how hard the sun is ionising the F layer, and it is the single number that
decides whether 15 m and 10 m are open at all. SSN is the sunspot number,
which says the same thing more slowly and with more history behind it. A and K
are the geomagnetic indices: K is a three-hourly measure of how disturbed the
field is, A is the day's worth of K flattened into one linear number. K is the
one that ruins an afternoon -- flux can be splendid and a K of 6 will still
have shut the high bands and put an aurora hiss across the low ones -- so it
gets the biggest type on the panel and the whole left-hand tile. Bz and solar
wind speed sit under it because southward Bz is *why* K will be bad, several
hours before K itself has noticed.

**The K history is worth more than the K.** One number cannot tell you whether
you are watching a storm arrive or watching one leave, and those call for
opposite decisions. So the middle column is the published 3-hourly planetary
Kp for the last day as a bar strip, coloured on the conventional scale, with
the G1 storm threshold ruled across it. A wide panel is the right shape for
this and it costs almost nothing: eight bars.

**The band ladder is the part people actually read.** Four band pairs, day and
night, green/amber/red. That judgement comes from N0NBH rather than from
anything computed here, and it is quoted rather than reproduced on purpose --
see ftdata.py's note on the hamqsl product. Colour does the work; a ham reads
the ladder from the doorway and the words in the chips only confirm it.

**Nothing here touches the network.** `build()` and `render()` read a disk
cache written by a separate process, `ftdata.py`, and if that process is not
running there is nothing to show. That is not a limitation to be worked
around, it is the design: the scheduler builds the next segment on a worker
thread that shares the GIL with the render loop, so a `build()` that blocks on
a socket drops frames on the wall for everybody. See ftdata.py's docstring.

**So the panel has to be honest about age, and it is, in three stages.** Every
source's age is printed in the status bar, always, in ftdata's short form.
Past its TTL a source's numbers are drawn at half brightness and its age
turns amber. Past three times its TTL they are not drawn at all -- the Kp tile
shows `--` and a red STALE flag blinks -- because a stale K is the specific
lie that matters here. Showing 5 when the truth is 2 sends somebody to the
wrong band, and a panel that admits it has nothing is strictly better than one
that is confidently wrong. If the cache is missing altogether the whole panel
becomes a no-data card that names the fetcher and the command to start it,
rather than a blank rectangle or, far worse, a tidy row of zeros.

**It is all baked.** The panel is static type: the layout, the glyphs, the
bars and the sparkline are rasterised once in `build()` into a single uint8
frame. `render()` copies that frame and repaints three small rectangles -- the
flare flag, the stale flag and a heartbeat dot that tells you the loop is
alive rather than frozen. That is one full-frame uint8 copy and a few dozen
pixels a frame, which is the cheapest thing in this directory, and it has to
be: the wall's Pi 3 is throttled to 600 MHz and re-laying out text at 30 fps
there would cost more than every other demo put together.

**So there are exactly two distinct frames, and that is on purpose.** The
blink is a square wave: over three seconds at any frame rate you get the lit
panel and the dark-flag panel and nothing else. On a fresh, quiet cache the
two differ by four pixels -- the heartbeat alone -- and during a storm with an
M-flare by eighty-odd, the two flags as well. An instrument is allowed to be
still; what it is not allowed to be is *accidentally* still, which is why the
heartbeat is there at all. It is also why the standalone default is 10 fps
rather than 30: the other twenty frames a second are identical.

The type is the same baked 3x5 pixel font as defcon.py, extended by four
glyphs, so there is no font file to be missing on the Pi.

Run:  python3 ftdata.py --loop 900 &        # the fetcher, in its own process
      python3 propagation.py --host 127.0.0.1
      python3 propagation.py --hours 72     # three days of Kp instead of one
      FT_DATA_CACHE=/tmp/empty python3 propagation.py   # the no-data card
"""

import sys

import numpy as np

import defcon
import demoscene as ds
import ftdata

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font is the table; five rows a glyph, each row an
# octal digit whose three bits are the three columns. TrueType at this size is
# mush and the Pi does not have the same faces installed as the machine this
# was written on, so a baked font is the only thing that is certainly there.
# Four glyphs are added for the things a readout needs and a map of a nuclear
# exchange does not.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT.update({
    "+": "02720", "?": "71302", "!": "22202", "_": "00007",
})

_GLYPHS = {}
for _ch, _rows in _FONT.items():
    _g = np.zeros((5, 3), np.uint8)
    for _r, _digit in enumerate(_rows):
        _v = int(_digit, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = 1
    _GLYPHS[_ch] = _g


def text_mask(s):
    """A (5, 4n-1) uint8 mask for a string; 1 px between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((5, 1), np.uint8)
    out = np.zeros((5, len(s) * 4 - 1), np.uint8)
    blank = _GLYPHS[" "]
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, blank)
    return out


def text_w(s, scale=1):
    return max(0, (4 * len(str(s)) - 1) * scale)


def stamp(buf, x, y, s, colour, scale=1):
    """Draw text into a uint8 (H, W, 3) buffer, clipped. Returns its width.

    Clipping rather than asserting: this panel is laid out for 320x64 but has
    to survive being asked for something else, and a demo that raises on a
    narrow canvas is a demo that takes the whole rotation down with it.
    """
    m = text_mask(s)
    if scale > 1:
        m = np.repeat(np.repeat(m, scale, 0), scale, 1)
    h, w = m.shape
    H, W = buf.shape[:2]
    x, y = int(x), int(y)
    sx, sy = max(0, -x), max(0, -y)
    ex, ey = min(w, W - x), min(h, H - y)
    if ex > sx and ey > sy:
        sub = m[sy:ey, sx:ex]
        buf[y + sy:y + ey, x + sx:x + ex][sub > 0] = colour
    return w


def stamp_right(buf, x_right, y, s, colour, scale=1):
    w = text_w(s, scale)
    stamp(buf, x_right - w, y, s, colour, scale)
    return w


def fill(buf, x0, y0, x1, y1, colour):
    """Filled rectangle, clipped to the buffer."""
    H, W = buf.shape[:2]
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(W, int(x1)), min(H, int(y1))
    if x1 > x0 and y1 > y0:
        buf[y0:y1, x0:x1] = colour


# --------------------------------------------------------------------------
# Colour, which carries most of the meaning here.
#
# Both scales on this panel are conventional and a ham reads them faster than
# they read the text beside them, so neither is invented: the Kp ramp is the
# green/yellow/orange/red one every space weather site uses, breaking at 5
# where the G-scale starts, and Good/Fair/Poor is N0NBH's own traffic light.
# --------------------------------------------------------------------------

KP_COLOURS = [
    (40, 190, 90), (40, 190, 90), (90, 200, 60), (170, 210, 40),   # 0..3
    (240, 200, 0),                                                  # 4
    (255, 140, 0), (255, 95, 10), (235, 40, 25),                    # 5..7  G1-G3
    (200, 10, 60), (185, 0, 140),                                   # 8, 9  G4-G5
]

BAND_COLOURS = {
    "GOOD": (30, 205, 80),
    "FAIR": (240, 190, 0),
    "POOR": (225, 45, 30),
    "CLSD": (70, 75, 85),
    "?": (70, 75, 85),
}

XRAY_COLOURS = (
    (1e-4, (255, 45, 45)),      # X
    (1e-5, (255, 115, 0)),      # M
    (1e-6, (235, 195, 0)),      # C
    (1e-7, (20, 170, 130)),     # B
    (0.0, (10, 105, 145)),      # A
)

INK = (215, 228, 245)           # a value you are meant to read
LABEL = (98, 122, 155)          # the word next to it
DIM = (52, 66, 88)              # rules, gridlines, axis furniture
WARN = (255, 165, 30)
ALERT = (255, 60, 45)
GOOD = (30, 205, 80)


def scale_colour(colour, k):
    return tuple(int(round(c * k)) for c in colour)


def kp_colour(kp):
    if kp is None:
        return DIM
    return KP_COLOURS[int(min(9, max(0, np.floor(kp))))]


def kp_word(kp):
    """The conventional name for a Kp level, G-scale once it is a storm."""
    if kp is None:
        return "NO DATA"
    if kp < 2:
        return "QUIET"
    if kp < 3:
        return "UNSETTLED"
    if kp < 5:
        return "ACTIVE"
    return ("G1 MINOR", "G2 MODERATE", "G3 STRONG", "G4 SEVERE",
            "G5 EXTREME")[min(4, int(kp) - 5)]


# --------------------------------------------------------------------------
# Freshness. Three states, because two is not enough: a source that is a
# little past its TTL is still worth showing with a caveat, and one that is
# days past it is not worth showing at all.
# --------------------------------------------------------------------------

FRESH, AGING, STALE, ABSENT = "fresh", "aging", "stale", "absent"

STALE_MULTIPLE = 3.0            # past this many TTLs, stop quoting the numbers


class Source(object):
    """One product off the cache, with its age and what that age means."""

    def __init__(self, name, cache_dir=None, now=None):
        self.name = name
        got = ftdata.load(name, cache_dir)
        self.ttl = ftdata.ttl_for(name) or 3600.0
        if got is None:
            self.payload, self.age, self.state = None, None, ABSENT
        else:
            self.payload, self.age = got
            if self.age <= self.ttl:
                self.state = FRESH
            elif self.age <= self.ttl * STALE_MULTIPLE:
                self.state = AGING
            else:
                self.state = STALE

    @property
    def usable(self):
        """True if the numbers may be printed at all."""
        return self.state in (FRESH, AGING)

    @property
    def brightness(self):
        return 1.0 if self.state == FRESH else 0.5

    def ink(self, colour=INK):
        return colour if self.state == FRESH else scale_colour(colour, 0.5)

    def get(self, *path):
        """Dig a field out, returning None for absent, null or unusable."""
        if not self.usable:
            return None
        cur = self.payload
        for key in path:
            if not isinstance(cur, dict) or cur.get(key) is None:
                return None
            cur = cur[key]
        return cur

    def age_text(self):
        if self.age is None:
            return "--"
        return ftdata.describe_age(self.age)


def num(value, fmt="%s"):
    """Format a value, or '--'. Never prints 'None' and never invents a zero."""
    if value is None:
        return "--"
    if isinstance(value, str):
        value = value.strip()
        # The live feeds say 'NoRpt', 'No Report' or simply nothing when a
        # quantity is unavailable. All three mean absent, and printing any of
        # them as if it were a reading is the mistake this panel exists to
        # avoid.
        if not value or value.lower().replace(" ", "") in (
                "norpt", "noreport", "n/a", "na", "none", "-1"):
            return "--"
        return value
    try:
        return fmt % value
    except (TypeError, ValueError):
        return "--"


def as_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def band_word(text):
    if not text:
        return "?"
    t = text.strip().lower()
    if "good" in t:
        return "GOOD"
    if "fair" in t:
        return "FAIR"
    if "poor" in t:
        return "POOR"
    if "clos" in t:
        return "CLSD"
    return "?"


BAND_ROWS = ("80m-40m", "30m-20m", "17m-15m", "12m-10m")
BAND_LABELS = {"80m-40m": "80-40M", "30m-20m": "30-20M",
               "17m-15m": "17-15M", "12m-10m": "12-10M"}


def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="where ftdata.py writes; defaults to $FT_DATA_CACHE "
                         "or ~/.cache/ftdata")
    ap.add_argument("--hours", type=int, default=24,
                    help="span of the Kp history strip; the cache holds three "
                         "days, so 24 or 72 both work")
    ap.add_argument("--blink-hz", type=float, default=1.0,
                    help="rate the flare and stale flags blink at; 0 holds "
                         "them lit, for a still photograph")
    ap.add_argument("--no-xray-strip", dest="xray_strip", action="store_false",
                    default=True,
                    help="drop the 24h X-ray sparkline and give the columns "
                         "the rows back")


# --------------------------------------------------------------------------
# The panel.
# --------------------------------------------------------------------------

def build(args):
    W, H = args.width, args.height
    out = np.zeros((H, W, 3), np.uint8)
    base = np.zeros((H, W, 3), np.uint8)

    hamqsl = Source("hamqsl", args.cache_dir)
    kp_src = Source("swpc_kp", args.cache_dir)
    xray = Source("swpc_xray", args.cache_dir)
    wind = Source("swpc_solarwind", args.cache_dir)
    sources = (hamqsl, kp_src, xray, wind)

    blink_hz = max(0.0, float(args.blink_hz))
    patches = []                 # (y0, y1, x0, x1, lit_image, dark_image)

    # ---------------------------------------------------------------- no data
    # Nothing in the cache at all: say so, name the process that fills it, and
    # give the command. A blank panel looks like a broken wall; zeros look
    # like a quiet sun, which is a lie.
    if all(s.state == ABSENT for s in sources):
        _draw_no_data(base, W, H, args, patches)
        return _make_render(out, base, patches, blink_hz)

    # ------------------------------------------------------------ the layout
    # Proportional, with floors, so --width/--height other than 320x64 give
    # something sane rather than an exception or a heap of overlapping type.
    status_h = 8 if H >= 44 else 6
    xray_h = 8 if (args.xray_strip and H >= 58) else 0
    main_y1 = H - status_h - (xray_h + 2 if xray_h else 1)
    main_y1 = max(10, main_y1)

    gap = 4
    c1_w = int(min(90, max(40, round(W * 0.205))))
    c3_w = int(min(126, max(56, round(W * 0.29))))
    # Squeeze the outer two before letting the middle collapse, then drop
    # columns outright rather than overlapping them. Overlapping type is the
    # one failure mode that still looks like a working panel from a distance,
    # which is what makes it the worst one.
    while W - c1_w - c3_w - 2 * gap < 34 and (c1_w > 40 or c3_w > 56):
        if c3_w - 56 >= c1_w - 40:
            c3_w -= 2
        else:
            c1_w -= 2
    c2_w = W - c1_w - c3_w - 2 * gap
    if c2_w < 34:                       # no room for indices and a strip
        c2_w = 0
        c3_w = max(0, W - c1_w - 2 * gap)
    c1_x = 1
    c2_x = c1_x + c1_w + gap
    c3_x = c2_x + (c2_w + gap if c2_w else 0)
    c3_w = min(c3_w, W - c3_x - 1)

    # Hairlines between the columns. Not decoration: three abutting blocks of
    # small type read as one paragraph without them.
    for cx, cw in ((c2_x, c2_w), (c3_x, c3_w)):
        if cw > 0:
            fill(base, cx - gap // 2, 1, cx - gap // 2 + 1, main_y1 - 1, DIM)

    _draw_kp_tile(base, c1_x, 0, c1_w, main_y1, kp_src, wind, hamqsl, patches,
                  blink_hz)
    if c2_w > 0:
        _draw_indices(base, c2_x, 0, c2_w, main_y1, hamqsl, kp_src, args)
    if c3_w >= 30:
        _draw_bands(base, c3_x, 0, c3_w, main_y1, hamqsl)

    if xray_h:
        y0 = main_y1 + 1
        fill(base, 0, y0, W, y0 + 1, scale_colour(DIM, 0.6))
        _draw_xray_strip(base, 0, y0 + 1, W, xray_h, xray)

    _draw_status(base, 0, H - status_h, W, status_h, sources, hamqsl, xray,
                 wind, patches, blink_hz)

    return _make_render(out, base, patches, blink_hz)


def _make_render(out, base, patches, blink_hz):
    """One uint8 full-frame copy, then a few dozen pixels of blinking.

    Everything above ran once. Per frame this is a memcpy plus a handful of
    tiny slice assignments, which is what lets a panel this dense hold 30 fps
    on a 600 MHz Pi. render() is a pure function of t: nothing is carried
    between frames, so the preview baker and the wall's drifting loop both
    land in the same place at the same t.
    """
    def render(t, frame=0):
        np.copyto(out, base)
        lit = True if blink_hz <= 0 else ((t * blink_hz) % 1.0) < 0.55
        for y0, y1, x0, x1, on_img, off_img in patches:
            out[y0:y1, x0:x1] = on_img if lit else off_img
        return out
    return render


# --------------------------------------------------------------------------
# The pieces.
# --------------------------------------------------------------------------

def _blink_patch(base, x0, y0, x1, y1, patches, draw_lit, draw_dark):
    """Bake a rectangle twice and hand render() the two versions.

    Cheaper and far less error-prone than re-drawing type every frame, and it
    keeps every glyph on the panel a build-time cost.
    """
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1 = min(base.shape[1], int(x1))
    y1 = min(base.shape[0], int(y1))
    if x1 <= x0 or y1 <= y0:
        return
    lit = base[y0:y1, x0:x1].copy()
    draw_lit(lit)
    dark = base[y0:y1, x0:x1].copy()
    draw_dark(dark)
    base[y0:y1, x0:x1] = lit
    patches.append((y0, y1, x0, x1, lit, dark))


def _draw_no_data(base, W, H, args, patches):
    """The cache is empty. Say what is missing and how to fix it."""
    cache = args.cache_dir or ftdata.CACHE_DIR
    fill(base, 0, 0, W, 1, ALERT)
    fill(base, 0, H - 1, W, H, ALERT)
    fill(base, 0, 0, 1, H, ALERT)
    fill(base, W - 1, 0, W, H, ALERT)

    title = "NO DATA"
    scale = 4
    while scale > 1 and text_w(title, scale) > W - 12:
        scale -= 1
    y = max(3, H // 2 - 5 * scale - 6)
    x = (W - text_w(title, scale)) // 2
    # The headline blinks, because a wall showing a stopped fetcher should
    # look like an alarm and not like a caption.
    _blink_patch(base, x - 2, y - 1, x + text_w(title, scale) + 2,
                 y + 5 * scale + 1, patches,
                 lambda buf: stamp(buf, 2, 1, title, ALERT, scale),
                 lambda buf: stamp(buf, 2, 1, title, scale_colour(ALERT, 0.25),
                                   scale))

    lines = ["PROPAGATION CACHE IS EMPTY",
             "RUN: PYTHON3 FTDATA.PY --LOOP 900"]
    # The path is the thing you actually need when this appears, so it goes on
    # even if it has to be cut to fit.
    lines.append(cache.upper())
    yy = y + 5 * scale + 5
    for line in lines:
        while line and text_w(line) > W - 6:
            line = line[:-1]
        stamp(base, (W - text_w(line)) // 2, yy, line, LABEL)
        yy += 7
        if yy + 5 > H - 2:
            break


def _draw_kp_tile(base, x, y, w, h, kp_src, wind, hamqsl, patches, blink_hz):
    """The left tile: Kp now, what that is called, and why it will get worse.

    Kp comes from SWPC rather than from hamqsl's `kindex`, and the reason is
    consistency rather than provenance: the bars in the next column are the
    SWPC series, and a headline from a different observatory sitting on top of
    them would sooner or later read 5 over a strip of 3s, which looks like a
    bug and destroys the panel's credibility at exactly the moment it matters.
    """
    kp = None
    estimated = False
    if kp_src.usable:
        now = kp_src.get("now", "kp")
        if now is not None:
            kp, estimated = as_float(now), True
        else:
            series = kp_src.get("series") or []
            if series:
                kp = as_float(series[-1].get("kp"))

    stamp(base, x, y + 1, "KP EST" if estimated else "KP", LABEL)
    if kp_src.state == STALE:
        stamp_right(base, x + w, y + 1, "STALE", scale_colour(ALERT, 0.7))

    # The three small lines hang off the bottom of the tile and the big number
    # takes whatever is left in the middle. Anchoring them to the top instead
    # is what silently drops the last line when the panel is a few rows short,
    # which is precisely the failure that is invisible until somebody notices
    # the wind speed has not been on the wall for a week.
    bz = wind.get("bz")
    speed = wind.get("speed")
    if bz is None and hamqsl.usable:
        bz = as_float(hamqsl.get("magneticfield"))
    if speed is None and hamqsl.usable:
        speed = as_float(hamqsl.get("solarwind"))

    foot = []
    word = kp_word(kp) if kp_src.usable else "NO CURRENT KP"
    if kp is None and kp_src.state == STALE:
        word = "TOO OLD TO SAY"
    colour = kp_colour(kp) if kp is not None else DIM
    if kp is not None and kp_src.state == AGING:
        colour = scale_colour(colour, 0.5)
    foot.append((word, colour if kp is not None else scale_colour(ALERT, 0.8),
                 None))
    # Southward Bz is why the number above will get worse, hours before it
    # does, so it lives in the same tile rather than out with the other
    # measurements. The sign is the whole message, so the sign is what is
    # coloured.
    foot.append(("BZ", LABEL,
                 ("--" if bz is None else "%+d" % int(round(bz)),
                  LABEL if bz is None else
                  (WARN if bz <= -5 else INK if bz < 0 else GOOD))))
    foot.append(("V", LABEL,
                 ("--" if speed is None else "%d" % int(round(speed)), INK)))

    line_h = 7 if h >= 44 else 6
    foot = foot[:max(1, (h - 14) // line_h)]
    foot_top = y + h - len(foot) * line_h + 1

    txt = "--" if kp is None else ("%.1f" % kp)
    scale = 4
    while scale > 1 and (text_w(txt, scale) > w or
                         5 * scale > foot_top - (y + 7) - 1):
        scale -= 1
    big_y = y + 7 + max(0, (foot_top - (y + 7) - 5 * scale) // 2)
    stamp(base, x, big_y, txt, colour, scale)

    for i, (label, label_col, value) in enumerate(foot):
        yy = foot_top + i * line_h
        if value is None:
            text = label
            while text and text_w(text) > w:
                text = text[:-1]
            stamp(base, x, yy, text, label_col)
        else:
            stamp(base, x, yy, label, label_col)
            stamp(base, x + text_w(label) + 3, yy, value[0], value[1])


def _draw_indices(base, x, y, w, h, hamqsl, kp_src, args):
    """SFI, SSN and A across the top; the Kp history strip underneath."""
    sfi = hamqsl.get("solarflux")
    ssn = hamqsl.get("sunspots")
    a_idx = None
    series = kp_src.get("series") or []
    if series:
        a_idx = series[-1].get("a")
    if a_idx is None and hamqsl.usable:
        a_idx = as_float(hamqsl.get("aindex"))

    cells = (("SFI", num(sfi), hamqsl), ("SSN", num(ssn), hamqsl),
             ("A", num(a_idx, "%d"), kp_src))
    cell_w = w // 3
    scale = 3
    while scale > 1 and (5 * scale > max(6, h // 3) or
                         text_w("000", scale) > cell_w - 2):
        scale -= 1
    for i, (label, value, src) in enumerate(cells):
        cx = x + i * cell_w
        stamp(base, cx, y, label, LABEL)
        stamp(base, cx, y + 6, value, src.ink(), scale)

    strip_y = y + 6 + 5 * scale + 2
    if strip_y + 12 <= y + h:
        _draw_kp_strip(base, x, strip_y, w, y + h - strip_y, kp_src, args)


def _draw_kp_strip(base, x, y, w, h, kp_src, args):
    """The last day of published Kp as bars, with the storm line ruled across.

    Bars rather than a line: each Kp is an interval, not an instant, and a
    polyline between three-hourly points implies a continuity the index does
    not have. The right-hand edge is labelled with the last bar's own UTC hour
    rather than 'NOW', because on a stale cache it is emphatically not now.
    """
    hours = max(3, int(args.hours))
    want = max(2, hours // 3)
    series = (kp_src.get("series") or [])[-want:]

    axis_h = 6 if h >= 16 else 0
    bar_h = max(4, h - 5 - axis_h)
    top = y + 5
    baseline = top + bar_h

    stamp(base, x, y, "KP %dH" % hours, LABEL)

    if not series:
        stamp(base, x, top + max(0, bar_h // 2 - 2),
              "NO KP HISTORY" if kp_src.state != STALE else "KP HISTORY TOO OLD",
              scale_colour(ALERT, 0.8))
        return

    # The G1 line. Ruled first so the bars paint over it; a storm-threshold
    # marker floating on top of a bar reads as part of the bar.
    g1_y = baseline - int(round(5.0 / 9.0 * bar_h))
    for gx in range(x, x + w, 3):
        fill(base, gx, g1_y, gx + 1, g1_y + 1, (95, 45, 35))

    n = len(series)
    pitch = max(2, w // n)
    bw = max(1, pitch - 1)
    dim = kp_src.state == AGING
    for i, rec in enumerate(series):
        kp = as_float(rec.get("kp"))
        bx = x + i * pitch
        if kp is None:
            # A gap in the series is drawn as a gap, not as zero.
            fill(base, bx, baseline - 1, bx + bw, baseline, DIM)
            continue
        height = max(1, int(round(min(9.0, kp) / 9.0 * bar_h)))
        colour = kp_colour(kp)
        if dim:
            colour = scale_colour(colour, 0.5)
        fill(base, bx, baseline - height, bx + bw, baseline, colour)

    fill(base, x, baseline, x + n * pitch, baseline + 1, scale_colour(DIM, 1.4))

    if axis_h:
        ay = baseline + 2
        left = "-%dH" % hours
        last_t = str(series[-1].get("t") or "")
        # '2026-08-08T18:00:00' -> '18Z'. Anything unexpected simply omits it
        # rather than printing a slice of whatever arrived.
        right = last_t[11:13] + "Z" if ("T" in last_t and len(last_t) >= 13) else ""
        if text_w(left) + text_w(right) + 4 <= n * pitch:
            stamp(base, x, ay, left, scale_colour(LABEL, 0.85))
            if right:
                stamp_right(base, x + n * pitch, ay, right,
                            scale_colour(LABEL, 0.85))
        elif right:
            stamp_right(base, x + n * pitch, ay, right, scale_colour(LABEL, 0.85))


def _draw_bands(base, x, y, w, h, hamqsl):
    """Four band pairs, day and night, as coloured chips.

    The chips carry the word as well as the colour. On a wall the colour is
    what gets read, but the word is what stops somebody who is colour-blind,
    or looking at a photograph of the panel, from having to guess.
    """
    bands = hamqsl.get("bands") or {}
    label_w = text_w(BAND_LABELS[BAND_ROWS[0]])
    chip_w = max(8, (w - label_w - 7) // 2)
    chip_x0 = x + label_w + 4
    chip_x1 = chip_x0 + chip_w + 3

    # On a short panel the DAY/NIGHT header is the first thing to go. Four
    # bands with three rows visible is a ladder that lies by omission; the same
    # four with no column headings is merely terser, and the left chip is
    # always the day one on every display that has ever drawn this.
    head_h = 7 if (h - 7) // len(BAND_ROWS) >= 6 else 0
    if head_h:
        stamp(base, x, y + 1, "BAND", LABEL)
        for cx, head in ((chip_x0, "DAY"), (chip_x1, "NIGHT")):
            if text_w(head) > chip_w:
                head = head[:3]
            if text_w(head) <= chip_w:
                stamp(base, cx + max(0, (chip_w - text_w(head)) // 2), y + 1,
                      head, LABEL)

    row_y = y + head_h
    row_h = max(6, min(11, (y + h - row_y) // len(BAND_ROWS)))
    if not bands:
        msg = "NO BAND DATA" if hamqsl.state != STALE else "BAND DATA TOO OLD"
        while msg and text_w(msg) > w:
            msg = msg[:-1]
        stamp(base, x, row_y + 3, msg, scale_colour(ALERT, 0.8))
        return

    for i, key in enumerate(BAND_ROWS):
        ry = row_y + i * row_h
        if ry + 5 > y + h:
            break
        chip_h = max(5, row_h - 2)
        stamp(base, x, ry + max(0, (chip_h - 5) // 2), BAND_LABELS[key], INK)
        for cx, when in ((chip_x0, "day"), (chip_x1, "night")):
            word = band_word((bands.get(key) or {}).get(when))
            colour = BAND_COLOURS[word]
            if hamqsl.state == AGING:
                colour = scale_colour(colour, 0.5)
            fill(base, cx, ry, cx + chip_w, ry + chip_h, colour)
            # Dark type on a bright chip; the chip is the signal and the word
            # only confirms it, so it must not out-shout the colour.
            tw = text_w(word)
            if tw <= chip_w - 2 and chip_h >= 7:
                stamp(base, cx + (chip_w - tw) // 2, ry + (chip_h - 5) // 2,
                      word, (8, 10, 14))


def _draw_xray_strip(base, x, y, w, h, xray):
    """24 hours of GOES 1-8A flux as a filled log sparkline.

    Filled columns rather than a line, because at seven rows a one-pixel trace
    is dashes. The vertical scale is four decades, A at the floor to X at the
    ceiling, and each column is coloured by its own class -- so a C-class
    afternoon is a yellow ripple and an M-flare is an orange spike, readable
    as a shape before any of the text is.
    """
    series = xray.get("series") or []
    fill(base, x, y, x + w, y + h, (5, 7, 11))
    # A list of 96 nulls is not a series. It is truthy, though, which is
    # exactly how a panel ends up drawing an empty plot with no explanation.
    if not any(v for v in series):
        msg = ("NO X-RAY HISTORY" if xray.state != STALE
               else "X-RAY HISTORY TOO OLD")
        stamp(base, x + 2, y + max(0, (h - 5) // 2), msg, scale_colour(ALERT, 0.7))
        return

    lo, hi = -8.0, -4.0            # A .. X
    n = len(series)
    dim = 0.5 if xray.state == AGING else 1.0

    # Decade rules at C and M, so the spike has something to be tall against.
    for level in (1e-6, 1e-5):
        ly = y + h - 1 - int(round((np.log10(level) - lo) / (hi - lo) * (h - 1)))
        for gx in range(x, x + w, 4):
            fill(base, gx, ly, gx + 1, ly + 1, (28, 34, 46))

    for col in range(w):
        v = series[min(n - 1, col * n // max(1, w))]
        if v is None or v <= 0:
            continue
        f = (np.log10(v) - lo) / (hi - lo)
        height = int(round(max(0.0, min(1.0, f)) * (h - 1))) + 1
        colour = XRAY_COLOURS[-1][1]
        for thresh, c in XRAY_COLOURS:
            if v >= thresh:
                colour = c
                break
        fill(base, x + col, y + h - height, x + col + 1, y + h,
             scale_colour(colour, dim))


def _draw_status(base, x, y, w, h, sources, hamqsl, xray, wind, patches,
                 blink_hz):
    """The bottom line: the flare state, the odds and ends, and every age.

    The ages are not a footnote. They are the only thing on the panel that
    says whether any of the rest of it is true, so they are printed for every
    source, always, in ftdata's short form, and they are the last thing on the
    line where the eye finishes.
    """
    ty = y + (h - 5) // 2
    fill(base, x, y - 1, x + w, y, scale_colour(DIM, 0.6))

    # The ages are drawn first, right to left, because they are the segment
    # that may not be dropped -- everything to their left is measured against
    # where they end. A status line that overruns is a line whose last field
    # is silently a different field, and on this panel that field is the one
    # that says whether the rest is true.
    rx = x + w - 2
    stale_any = any(s.state == STALE for s in sources)
    aging_any = any(s.state == AGING for s in sources)
    for src, short in ((wind, "SW"), (xray, "XR"), (kp_short(sources), "KP"),
                       (hamqsl, "HQ")):
        if src is None:
            continue
        if src.state == ABSENT:
            txt, colour = "-", scale_colour(ALERT, 0.8)
        else:
            txt = src.age_text()
            colour = (INK if src.state == FRESH
                      else WARN if src.state == AGING else ALERT)
        need = text_w(short) + 2 + text_w(txt) + 4
        if rx - need < x + 2:
            break
        rx -= text_w(txt)
        stamp(base, rx, ty, txt, colour)
        rx -= text_w(short) + 2
        stamp(base, rx, ty, short, LABEL)
        rx -= 4

    cur_class = xray.get("current_class")
    peak_class = xray.get("peak_class")
    flare = bool(cur_class and cur_class[0] in ("M", "X"))
    limit = rx - (text_w("STALE") + 4 if (stale_any or aging_any) else 0)

    cx = x + 2
    if cx + text_w("XRAY") + 3 < limit:
        stamp(base, cx, ty, "XRAY", LABEL)
        cx += text_w("XRAY") + 3
    if flare:
        # An M or X flare in progress is a D-layer blackout happening right
        # now, so it is the one thing on this panel allowed to move.
        txt = cur_class + " FLARE"
        if cx + text_w(txt) > limit:
            txt = cur_class
        tw = text_w(txt)
        if cx + tw <= limit:
            _blink_patch(base, cx - 1, ty - 1, cx + tw + 1, ty + 6, patches,
                         lambda buf: stamp(buf, 1, 1, txt, ALERT),
                         lambda buf: stamp(buf, 1, 1, txt,
                                           scale_colour(ALERT, 0.3)))
        cx += tw + 5
    else:
        colour = INK
        if cur_class and cur_class[0] == "C":
            colour = WARN
        if cx + text_w(num(cur_class)) <= limit:
            stamp(base, cx, ty, num(cur_class), xray.ink(colour))
        cx += text_w(num(cur_class)) + 5

    # Everything after this point is nice to have, and gets dropped in order
    # the moment it would collide with the ages.
    extras = [("PK", peak_class, xray)] if peak_class else []
    extras += [("MUF", num(hamqsl.get("muf")), hamqsl),
               ("S/N", num(hamqsl.get("signalnoise")), hamqsl),
               ("AUR", num(hamqsl.get("aurora")), hamqsl)]
    for label, value, src in extras:
        if cx + text_w(label) + 3 + text_w(value) + 5 > limit:
            break
        stamp(base, cx, ty, label, LABEL)
        cx += text_w(label) + 3
        stamp(base, cx, ty, value, src.ink())
        cx += text_w(value) + 5

    if stale_any or aging_any:
        flag = "STALE" if stale_any else "AGING"
        colour = ALERT if stale_any else WARN
        fw = text_w(flag)
        fx = max(x + 2, rx - fw - 2)
        _blink_patch(base, fx - 1, ty - 1, fx + fw + 1, ty + 6, patches,
                     lambda buf: stamp(buf, 1, 1, flag, colour),
                     lambda buf: stamp(buf, 1, 1, flag, scale_colour(colour, 0.25)))

    # A heartbeat, bottom right. Two pixels, and the only reason they are
    # there is that a frozen render loop and a very quiet sun look identical
    # on a panel made entirely of static type.
    # Two rows below the type, not three: the rightmost age runs to the last
    # usable column, and at three rows the dot clipped one pixel off its final
    # glyph -- which is invisible on the wall and turns "32S" into "32?" the
    # moment anything tries to read the panel back.
    hx, hy = x + w - 3, y + h - 2
    _blink_patch(base, hx, hy, hx + 2, hy + 2, patches,
                 lambda buf: buf.__setitem__(Ellipsis, GOOD),
                 lambda buf: buf.__setitem__(Ellipsis, (10, 30, 16)))


def kp_short(sources):
    for s in sources:
        if s.name == "swpc_kp":
            return s
    return None


def main():
    # 10 fps, not 30. There are exactly two distinct frames in this demo --
    # the blink is a square wave and nothing else moves -- so twenty of every
    # thirty frames are 61 kB of UDP carrying no information at all. At 10 the
    # blink still lands within a tenth of a second of its edge, which nobody
    # can see. A sequencer that wants another rate can still ask for one; this
    # is only the standalone default.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=10)


if __name__ == "__main__":
    main()
