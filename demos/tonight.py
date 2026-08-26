#!/usr/bin/env python3
"""Is anything on at the makerspace tonight, and how full are the evenings ahead.

The wall hangs in a workshop that runs classes, socials and open hours.
Somebody walking past has one question about all of that and it is not "what is
the programme". It is **is something on, and is it soon.**

**The panel is a field of evenings, and the events are the lights being on.**
x is days, one cell per calendar day, today at the left edge. y is *time of
day*, and the window is narrow: the hours the workshop is actually used. The
ground behind it is the real sky -- computed solar elevation for this latitude
and this date, so the top of the grid is late afternoon blue, the bottom is
night, and a warm band of dusk runs across it and creeps upward as the season
turns. An event is a lit rectangle at its true hour, its true length. A busy
week is a row of lit windows. A quiet one is a dark building. That is the whole
representation and everything else in this file falls out of it.

**Three weeks, not one, and the data forced that.** The endpoint returns
fourteen upcoming events -- and on the day this was written they ran from the
13th of August to the 3rd of November, eighty-three days. One or two evenings a
week is what a volunteer-run makerspace actually schedules. A "next seven days"
panel, which is what this started as, is empty five days in seven, and an empty
panel is indistinguishable from a broken one. Three weeks reliably holds three
to five events, and it holds them *as three repeats of the same seven-day
pattern*, so the weekly rhythm -- these socials are Monday, Tuesday and
Thursday evenings -- is visible as a shape rather than inferred from a list.
When even three weeks holds nothing, the span extends in whole weeks until it
reaches the next event, and the header says how many weeks it is showing.

**The vertical window is measured, not chosen.** Every start in the feed is
18:00 or 19:00 and every end is 20:00 to 21:00, because these are things people
come to after work. So the panel spends its rows on the hours that carry
events, computed from the record and clamped: typically 16:00 to 22:00 over 38
rows, which makes a two-hour social a twelve-row block instead of the four-row
smear a flat 24-hour axis would give it. A morning class in the record widens
the window automatically and the hour labels at the right-hand edge say so.

**Today's cell is the one that is half spent.** Above the current time of day
its sky is darkened, and the boundary is a bright line with a pip travelling
along it. So "the evening has not started yet" and "you have missed most of it"
are the same picture at two different times, and the gap between that line and
the next lit block *is* the wait.

**The one state worth shouting.** Something starting within the hour puts
`IN 40 MIN` across the panel at double height in hot white-gold, pulsing, with
its block pulsing in step. On now says `ON NOW`. Everything else is a calm
amber: `TONIGHT 7P`, `TOMORROW 6P`, `THU 7P`, `MON 8/31 7P`. The event's name
sits beside it at single height -- full, not abbreviated, because the fetcher
already did the abbreviating and the longest title left is twenty-eight
characters, which fits.

**Where the trimming happened.** `ftdata.py`'s `sequoia-calendar` block is the
place to read about the feed, and there are two things in it worth knowing from
here. Its timestamps are stamped `-08:00` all year, including in August when
California is on -07:00, and the wall-clock fields are taken as authoritative
rather than the offset -- proved by a recurring orientation that is 19:00 in
every one of its six listings across the DST boundary, which is only true if
the local time is the real one. And the titles are edited there, not here:
"Upmending (upcycling + mending) Social" loses its parenthetical gloss before
it is ever stored, because that gloss is for somebody reading a web page. What
this file does to a title is only what the *font* requires -- see `sanitise`.

**The three data states.** Fresh is the panel. Stale prints `STALE 9H` beside
the count in amber and otherwise draws normally, because a calendar fetched
this morning is still true tonight and pretending otherwise would be the
dishonest move. Absent draws a card saying nobody has asked yet and what to
run. And an *empty* calendar is none of those three -- it is a real state of a
small workshop in August, and it draws the three weeks of empty evenings with
NOTHING ON across them, which is a picture rather than an error.

**The clock.** This is a wall-clock panel, like `muni`: `build()` takes the
present moment once from `time.time()` and every frame after that is a pure
function of `t`. `--now` pins it, which is how the tests and the screenshot get
a fixed picture, and `--pretend` moves it to a moment relative to a real event
so the urgent states can be reviewed on an afternoon when nothing is happening.
Nothing is fabricated by `--pretend` except the clock; the events stay real,
and the panel stamps SIM so a screenshot of one cannot be mistaken for the
wall.

Run:  python3 ftdata.py --once --only sequoia-calendar
      python3 tonight.py --host 127.0.0.1
      python3 tonight.py --pretend soon        # forty minutes to go
      python3 tonight.py --pretend now         # it has started
      python3 tonight.py --pretend quiet       # a month with nothing near
      FT_DATA_CACHE=/tmp/empty python3 tonight.py
      python3 scripts/test-tonight.py
"""

import math
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata
import ftsite

f32 = np.float32

PRODUCT = "sequoia-calendar"

# The span, in days. Three weeks is the default and the usual answer: it is
# three repeats of a seven-day pattern, which is what makes the weekly rhythm
# legible, and at 320 columns it leaves fifteen per day -- enough for a
# two-letter weekday under it. If three weeks holds nothing the span grows a
# week at a time until it reaches the next event, up to the cap.
SPAN_DAYS = 21
SPAN_MAX_DAYS = 63

# The vertical window, derived from the record and held between these. The low
# end may not creep past 16:00 however late the events are, because the panel
# would then be four hours of black with a block in it; the high end may not
# stop before 22:00 for the same reason at the other end.
HOUR_LO_CLAMP = (5, 17)
HOUR_HI_CLAMP = (20, 24)
HOUR_MIN_SPAN = 4
HOUR_DEFAULT = (17, 22)

# Something starting inside this many seconds is the state the whole panel is
# built to shout about.
SOON_S = 3600.0

# Past this multiple of the TTL the record is old enough to say so out loud.
# One TTL is six hours, which a fetcher that missed a single pass will hit.
STALE_TTL_MULT = 1.0


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, tide, muni and solar draw
# with: five rows a glyph, each row an octal digit whose three bits are the
# three columns.
#
# The height is *measured* off the built glyphs rather than assumed to be five.
# A panel that assumes five and meets a six-row font clips the bottom off every
# capital E, which is a bug this tree has actually shipped, so every vertical
# number below is expressed in GLYPH_H.
#
# The charset is measured too, and it decides how titles are spelled. The font
# has no ampersand, no apostrophe and no exclamation mark, so "Crochet &
# Knitting Social" and "Let's make BioYarn!" would come out with holes in them.
# `&` is already turned into `/` in the fetcher -- the same substitution
# `_muni_short` makes, so the tree has one separator and not two -- and what is
# left is dropped here rather than drawn as a blank, because a missing
# apostrophe reads as a style and a blank column reads as a fault.
# --------------------------------------------------------------------------

_GLYPHS = {}
for _ch, _rows in defcon._FONT.items():
    _g = np.zeros((len(_rows), 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g

GLYPH_H = max(g.shape[0] for g in _GLYPHS.values())
GLYPH_W = max(g.shape[1] for g in _GLYPHS.values())
GLYPH_PITCH = GLYPH_W + 1               # one blank column between glyphs
CHARSET = set(_GLYPHS)


def sanitise(s):
    """Drop what the font cannot draw. Typography, not editing -- see ftdata."""
    return "".join(c for c in str(s).upper() if c in CHARSET)


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = sanitise(s)
    if not s:
        return np.zeros((GLYPH_H * scale, 1), bool)
    out = np.zeros((GLYPH_H, len(s) * GLYPH_PITCH - 1), bool)
    for i, ch in enumerate(s):
        g = _GLYPHS[ch]
        out[:g.shape[0], i * GLYPH_PITCH:i * GLYPH_PITCH + g.shape[1]] = g
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def text_width(s, scale=1):
    n = len(sanitise(s))
    return max(1, (n * GLYPH_PITCH - 1) * scale) if n else 0


def text_height(scale=1):
    return GLYPH_H * scale


def blit_text(dst, y, x, s, rgb, scale=1):
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn."""
    m = text_mask(s, scale)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    dst[y0:y1, x0:x1][sub] = rgb
    return gw


# --------------------------------------------------------------------------
# Colour. One warm hue for "the lights are on" and a hot one for "and it is
# about to happen", against a sky that is genuinely the sky.
#
# There is deliberately no colour *per kind of event*. A palette keyed to the
# title would need a legend, and there is no room for one; worse, it would say
# that which social it is matters more than whether it is tonight, which is the
# wrong ranking for somebody walking past. Colour here means one thing only:
# how soon. Grey is over, amber is on the calendar, white-gold is now.
# --------------------------------------------------------------------------

# The sky, as two endpoints mixed by solar elevation. Both are dark: the room
# this hangs in has its lights on, and the lit blocks have to win.
SKY_NIGHT = (4, 6, 14)
SKY_DAY = (18, 38, 68)
SKY_TWILIGHT = (58, 28, 32)             # added, widest at the horizon

# Weekend cells get a hair more sky. Not a colour change -- a colour change
# would read as another category of event -- just enough lift that the seven
# day pattern is visible on a week with nothing in it at all.
WEEKEND_LIFT = 1.22

C_LAMP = (255, 176, 62)                 # on the calendar
C_SOON = (255, 228, 140)                # starting within the hour
C_LIVE = (150, 255, 172)                # happening right now
C_PAST = (92, 96, 108)                  # finished

# C_SOON started as a near-white cream, and on the panel it read as *washed
# out* rather than as hot -- next to a saturated amber block, a desaturated
# pale one looks like the greyed-out one. Urgency here is carried by staying in
# the lamp's own hue family and going brighter and more solid, never by going
# whiter: whiteness on this palette already means "finished" at low saturation
# and the eye resolves that first.

C_DAYRULE = (14, 18, 28)
C_WEEKRULE = (58, 68, 92)               # the week boundary; see draw_rules
C_HOURRULE = (22, 28, 40)
C_AXIS = (30, 36, 50)

C_NOWLINE = (255, 246, 214)
C_NOWPIP = (255, 255, 255)
# The now-line when the present moment is outside the window the panel draws --
# before the evening has started or after it is over. Still a line, because the
# pip has to have somewhere to run and a dead panel is worse than a clamped
# one, but plainly not the same claim as a line sitting between two hours.
C_NOWEDGE = (104, 104, 96)

C_TEXT = (178, 192, 210)
C_DIM = (86, 98, 116)
C_FAINT = (66, 76, 96)
C_NAME = (138, 186, 132)                # the site's own name, as in solar.py
C_WARN = (255, 128, 92)
C_SIM = (196, 118, 226)
C_RULE = (16, 20, 28)

# How much of today's sky above the now-line is left. Not zero: the hours
# already gone are still part of today and the day rules through them are what
# make the now-line read as a position rather than as the panel's top edge.
ELAPSED_K = 0.42

PULSE_HZ = 1.35                         # the imminent headline and its block
PIP_S = 2.6                             # the pip's trip along the now-line
REVEAL_S = 2.2


# --------------------------------------------------------------------------
# Clock and calendar arithmetic. Everything asks for `now` rather than reading
# the system clock, which is what makes a test and a contact sheet possible;
# see caiso.py and solar.py, which are laid out the same way.
#
# All of the day arithmetic goes through localtime/mktime rather than through
# dividing epochs by 86400. Two days a year are not 86400 seconds long, and on
# one of them a panel that divides puts an evening event in the wrong column
# and an hour off the axis. Doing it properly costs a few struct_times at build
# time and nothing at all per frame.
# --------------------------------------------------------------------------

def parse_when(s):
    """'now', an epoch, or 'YYYY-MM-DD HH:MM' in local time."""
    if not s or s == "now":
        return None
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    raise ValueError("cannot read a time out of %r" % s)


def local_midnight(epoch):
    """Midnight at the start of the local day `epoch` falls in."""
    lt = time.localtime(epoch)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def day_starts(first_midnight, n):
    """`n` consecutive local midnights, DST included.

    Stepping by 86400 would drift an hour twice a year and would then put every
    subsequent event in the wrong place on the hour axis. Landing on each day's
    noon first and asking mktime for that date's midnight is exact on all 365.
    """
    out = []
    for i in range(n):
        lt = time.localtime(first_midnight + i * 86400 + 43200)
        out.append(time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    return out


def solar_elevation(epoch, lat, lon):
    """The sun's altitude in degrees. Low-precision USNO series, ~0.01 deg.

    Copied from goes.py by way of solar.py, which use it for the same reason:
    the day/night call has to come from the sky rather than from the clock, or
    the dusk band would sit at a fixed row all year instead of climbing two
    rows over the three weeks the panel shows. `lon` is east positive, so San
    Francisco is negative.
    """
    d = epoch / 86400.0 - 10957.5              # days from J2000.0
    g = math.radians((357.529 + 0.98560028 * d) % 360.0)
    q = (280.459 + 0.98564736 * d) % 360.0
    lam = math.radians((q + 1.915 * math.sin(g)
                        + 0.020 * math.sin(2.0 * g)) % 360.0)
    eps = math.radians(23.439 - 0.00000036 * d)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24.0
    ha = math.radians(gmst * 15.0 + lon) - ra
    phi = math.radians(lat)
    return math.degrees(math.asin(
        math.sin(phi) * math.sin(dec)
        + math.cos(phi) * math.cos(dec) * math.cos(ha)))


DOW2 = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")     # time.localtime tm_wday
DOW3 = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def clock_str(epoch, h24=False):
    """'7P', '6:30P', '19:00'. Minutes only when there are any."""
    lt = time.localtime(epoch)
    if h24:
        return "%d:%02d" % (lt.tm_hour, lt.tm_min)
    h = lt.tm_hour % 12 or 12
    ap = "A" if lt.tm_hour < 12 else "P"
    if lt.tm_min:
        return "%d:%02d%s" % (h, lt.tm_min, ap)
    return "%d%s" % (h, ap)


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises, so everything that
# can still be wrong is wrong about *content* and is caught here. An empty
# event list is explicitly not a problem: it is the quiet-month state and the
# panel has a picture for it.
# --------------------------------------------------------------------------

def read_calendar(cache_dir):
    """(record, age, problem). `record` is None only if nothing is drawable."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached sequoia calendar"
    payload, age = got
    if not isinstance(payload, dict) or not isinstance(payload.get("ev"), list):
        return None, age, "sequoia calendar record is malformed"

    events = []
    for raw in payload["ev"]:
        try:
            start = float(raw["t"])
            dur = max(0.0, float(raw.get("d") or 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        name = sanitise(raw.get("n") or "")
        if not name:
            continue
        events.append({"t": start, "end": start + dur, "dur": dur,
                       "all": bool(raw.get("a")), "name": name})
    events.sort(key=lambda e: e["t"])

    return {
        "site": sanitise(payload.get("site") or ftsite.NAME),
        "events": events,
        "n_feed": payload.get("n_feed"),
        "age": age,
    }, age, None


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

PRETEND = ("none", "soon", "now", "quiet")


def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--now", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--pretend", default="none", choices=PRETEND,
                    help="move the clock to a moment relative to a real event, "
                         "so the urgent states can be looked at on a quiet day")
    ap.add_argument("--span", type=int, default=0,
                    help="days across the panel (0 = three weeks, extended if "
                         "three weeks hold nothing)")
    ap.add_argument("--reveal", type=float, default=REVEAL_S,
                    help="seconds the field takes to draw itself in (0 = off)")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--reload", type=float, default=900.0,
                    help="seconds between re-reads of the cache (0 = never)")
    ap.add_argument("--seed", type=int, default=20260812,
                    help="unused by the picture; kept so every demo takes one")


# --------------------------------------------------------------------------
# Layout. The headline gets the top, the field gets everything left, and the
# weekday ruler gets one line of type at the bottom.
#
# Written against GLYPH_H and against the panel's own height rather than
# against the numbers that fall out at 320x64, so the preview baker's odd sizes
# and a 16-row strip both produce something rather than an exception. The order
# things are given up in as the panel shrinks is the order they matter least:
# the weekday ruler, then the headline's second line, then the header.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h):
        self.w, self.h = w, h
        gh = GLYPH_H

        self.head_h = gh if h >= 34 else 0                  # site name + count
        self.rule_y = self.head_h                           # hairline under it

        # The headline: one line of double-height type with the event name
        # beside it. Two glyph heights plus a row of air above and below.
        self.hl_h = 2 * gh + 2 if h >= 52 else 0
        self.hl_y = self.head_h + 1

        self.day_h = gh if h >= 44 else 0                   # the weekday ruler
        self.day_y = h - self.day_h
        self.axis_y = self.day_y - 1 if self.day_h else h   # the ruler's rule

        self.grid_y0 = self.hl_y + self.hl_h + (1 if self.hl_h else 0)
        self.grid_y1 = self.axis_y                          # exclusive

        # If that left no field worth drawing, take the headline back: the
        # picture is the point and a two-row field is not a picture.
        if self.grid_y1 - self.grid_y0 < 10 and self.hl_h:
            self.hl_h = 0
            self.grid_y0 = self.hl_y
            self.grid_y1 = self.axis_y
        if self.grid_y1 - self.grid_y0 < 6 and self.head_h:
            self.head_h = self.rule_y = 0
            self.hl_y = 1
            self.grid_y0 = 0
            self.grid_y1 = self.axis_y
        self.grid_h = max(1, self.grid_y1 - self.grid_y0)


# --------------------------------------------------------------------------
# The field: which days, which hours, and where an event lands.
# --------------------------------------------------------------------------

def choose_span(events, now, day0, want):
    """How many days across. Three weeks unless three weeks are empty.

    An empty three weeks with an event in week five is the case this exists
    for: showing the default span would be an honest but useless panel, and
    showing the event without saying the axis changed would be a lie about the
    scale. So the span grows in whole weeks -- keeping the seven-day pattern
    the whole design rests on -- and the header prints how many.
    """
    if want > 0:
        return max(2, min(SPAN_MAX_DAYS, want))
    span = SPAN_DAYS
    ahead = [e for e in events if e["end"] > now]
    if not ahead:
        return span
    while span < SPAN_MAX_DAYS:
        if ahead[0]["t"] < day0 + span * 86400:
            break
        span += 7
    return min(span, SPAN_MAX_DAYS)


def choose_hours(events, mids, span):
    """(lo, hi) hours of the day the field covers, measured off the events.

    Clamped at both ends: see HOUR_LO_CLAMP. The clamps are what keep a panel
    of nothing but 7 pm socials from being a six-hour window with one block in
    the middle of it, and what keep one all-day entry from flattening every
    evening in the span into three rows.
    """
    lo_hi = []
    last = mids[-1] + 86400 if mids else 0
    for e in events:
        if e["all"] or not mids:
            continue
        if e["end"] <= mids[0] or e["t"] >= last:
            continue
        # Hours are measured against the event's *own* day's midnight, so a
        # DST weekend does not shift one column's blocks by an hour.
        base = local_midnight(e["t"])
        lo_hi.append(((e["t"] - base) / 3600.0, (e["end"] - base) / 3600.0))
    if not lo_hi:
        return HOUR_DEFAULT
    lo = int(math.floor(min(a for a, _ in lo_hi))) - 1
    hi = int(math.ceil(max(b for _, b in lo_hi))) + 1
    lo = max(HOUR_LO_CLAMP[0], min(HOUR_LO_CLAMP[1], lo))
    hi = max(HOUR_HI_CLAMP[0], min(HOUR_HI_CLAMP[1], hi))
    if hi - lo < HOUR_MIN_SPAN:
        hi = min(24, lo + HOUR_MIN_SPAN)
        lo = max(0, hi - HOUR_MIN_SPAN)
    return lo, hi


class Field(object):
    """The mapping between (day, hour) and (column, row), and nothing else."""

    def __init__(self, lay, mids, hours):
        self.lay = lay
        self.mids = mids
        self.n_days = len(mids)
        self.lo_h, self.hi_h = hours
        self.rows_per_hour = lay.grid_h / float(self.hi_h - self.lo_h)
        # Column edges from a rounded proportion rather than an accumulated
        # width, so 320 columns over 21 days is 15 and 16 alternating with no
        # rounding error left over at the right-hand edge.
        self.edges = [int(round(i * lay.w / float(self.n_days)))
                      for i in range(self.n_days + 1)]
        # date key -> day index, so an event is placed by its calendar date and
        # never by an epoch division that a DST weekend would break.
        self.index = {}
        for i, m in enumerate(mids):
            lt = time.localtime(m + 43200)
            self.index[(lt.tm_year, lt.tm_mon, lt.tm_mday)] = i

    def day_of(self, epoch):
        lt = time.localtime(epoch)
        return self.index.get((lt.tm_year, lt.tm_mon, lt.tm_mday))

    def cell(self, i):
        return self.edges[i], self.edges[i + 1]

    def row_of(self, hour):
        """Row for a float hour of the day. Not clipped; callers clip."""
        return (self.lay.grid_y0
                + (hour - self.lo_h) * self.rows_per_hour)

    def hour_at(self, row):
        """The centre of `row` as a float hour of the day. row_of's inverse."""
        return (self.lo_h
                + (row + 0.5 - self.lay.grid_y0) / self.rows_per_hour)

    def hour_of(self, epoch, day_i):
        return (epoch - self.mids[day_i]) / 3600.0


def place(field, event, now):
    """(day, y0, y1, state) for one event, or None if it is off the field.

    `y1` is exclusive. An event running past the bottom of the window is
    clipped there rather than dropped or wrapped into tomorrow's column: the
    block reaching the axis reads as "and it goes on", which is true, and
    wrapping would put a Tuesday evening in Wednesday's cell.
    """
    day = field.day_of(event["t"])
    if day is None:
        return None
    if event["all"]:
        y0, y1 = field.lay.grid_y0, field.lay.grid_y1
    else:
        h0 = field.hour_of(event["t"], day)
        h1 = h0 + event["dur"] / 3600.0
        y0 = int(round(field.row_of(h0)))
        y1 = int(round(field.row_of(h1)))
        y0 = max(field.lay.grid_y0, min(field.lay.grid_y1 - 1, y0))
        y1 = max(y0 + 1, min(field.lay.grid_y1, y1))
    if event["end"] <= now:
        state = "past"
    elif event["t"] <= now:
        state = "live"
    elif event["t"] - now <= SOON_S:
        state = "soon"
    else:
        state = "next"
    return day, y0, y1, state


STATE_RGB = {"past": C_PAST, "live": C_LIVE, "soon": C_SOON, "next": C_LAMP}

# How solid a block's body is, per state. Not one number, and the first draft's
# single 0.42 was the worst bug in this file: 42 % of the near-white C_SOON is
# a warm grey, so the *most urgent* block on the panel was drawing duller than
# the ordinary amber ones and reading as an event that had already finished --
# exactly backwards. The two states that mean "now" are nearly solid; the two
# that do not are outlines with a wash in them.
STATE_BODY = {"past": 0.28, "live": 0.78, "soon": 0.80, "next": 0.42}


# --------------------------------------------------------------------------
# Baking the sky.
# --------------------------------------------------------------------------

def sky_field(lay, field):
    """The whole grid as one (grid_h, w, 3) float image.

    Solar elevation is computed once per (day, row) -- twenty-one by thirty-
    eight at the usual size, under a thousand scalar evaluations at build time
    and none at all per frame -- and then the colour is one vectorised pass
    over that array. Columns inside a day share their day's sky, which is not
    an approximation being got away with: a day *is* the unit of this axis, and
    the day rules are drawn on the boundaries anyway.
    """
    gh, w, nd = lay.grid_h, lay.w, field.n_days
    hours = np.array([field.hour_at(lay.grid_y0 + r) for r in range(gh)], f32)

    elev = np.empty((gh, nd), f32)
    for d in range(nd):
        base = field.mids[d]
        for r in range(gh):
            elev[r, d] = solar_elevation(base + float(hours[r]) * 3600.0,
                                         ftsite.LAT, ftsite.LON)

    # Daylight fraction. Zero at civil dusk (-6 deg) rather than at the
    # geometric horizon, because the sky a person sees is still blue at 0 deg
    # and the panel should agree with the window rather than with the almanac.
    day = np.clip((elev + 6.0) / 14.0, 0.0, 1.0)
    night_rgb = np.asarray(SKY_NIGHT, f32)
    day_rgb = np.asarray(SKY_DAY, f32)
    pic = (night_rgb[None, None, :] * (1.0 - day)[:, :, None]
           + day_rgb[None, None, :] * day[:, :, None])

    # Twilight, a Gaussian on elevation centred just under the horizon: widest
    # at dusk, gone by mid-afternoon and gone again by full dark. This is the
    # band that climbs the panel from left to right as the sun sets earlier.
    tw = np.exp(-((elev + 2.0) / 5.0) ** 2).astype(f32)
    pic += np.asarray(SKY_TWILIGHT, f32)[None, None, :] * tw[:, :, None]

    # Weekends, a touch brighter. Applied per day, before the expansion.
    for d in range(nd):
        if time.localtime(field.mids[d] + 43200).tm_wday >= 5:
            pic[:, d, :] *= f32(WEEKEND_LIFT)

    # One day per column, expanded with a lookup rather than a Python loop.
    col_day = np.empty(w, np.int32)
    for d in range(nd):
        x0, x1 = field.cell(d)
        col_day[x0:x1] = d
    return np.take(pic, col_day, axis=1)


def draw_rules(dst, lay, field, h24):
    """Day boundaries, week boundaries, hour rules and the hour labels.

    Hour labels sit at the *right* edge, inside the field. That is the far
    future -- the least valuable columns on the panel -- and it is where an
    event is least likely to be drawn over them, which is the priority order
    the panel wants: a block always wins against its own axis.
    """
    gy0, gy1 = lay.grid_y0, lay.grid_y1

    # Hour rules first, dotted, so a solid day rule crosses them cleanly.
    for hour in range(field.lo_h, field.hi_h + 1):
        y = int(round(field.row_of(hour)))
        if not gy0 <= y < gy1:
            continue
        xs = np.arange(0, lay.w, 3)
        dst[y, xs] = np.maximum(dst[y, xs], np.asarray(C_HOURRULE, np.uint8))

    for d in range(field.n_days):
        x0, _ = field.cell(d)
        if x0 <= 0:
            continue
        monday = time.localtime(field.mids[d] + 43200).tm_wday == 0
        dst[gy0:gy1, x0] = C_WEEKRULE if monday else C_DAYRULE

    # Two labels, three hours apart, anchored to the window's own end so they
    # land on round hours whatever the window turned out to be.
    for hour in (field.hi_h - 1, field.hi_h - 4):
        if hour <= field.lo_h:
            continue
        y = int(round(field.row_of(hour))) + 1
        if not gy0 <= y < gy1 - GLYPH_H:
            continue
        lab = ("%d:00" % hour) if h24 else (
            "%d%s" % (hour % 12 or 12, "A" if hour < 12 else "P"))
        blit_text(dst, y, lay.w - text_width(lab) - 2, lab, C_FAINT)


def draw_days(dst, lay, field, focus_day):
    """The weekday ruler, and the rule above it that doubles as the axis.

    The letter count is decided **once, from the narrowest cell on the panel**,
    and not per cell. Deciding it per cell is what the first draft did and it
    produced `F SA S MOTU WTHFR` across a six-week span -- two letters wherever
    a cell happened to round up to eight columns and one wherever it rounded
    down to seven, which reads as damage rather than as a ruler. A ruler whose
    marks are all the same size is legible at any span; one whose marks vary is
    not legible at any.
    """
    if not lay.day_h:
        return
    dst[lay.axis_y, :] = C_AXIS
    narrow = min(field.cell(d)[1] - field.cell(d)[0]
                 for d in range(field.n_days))
    if text_width("MO") <= narrow - 1:
        chars = 2
    elif text_width("M") <= narrow - 1:
        chars = 1
    else:
        return                          # no honest ruler fits; draw none
    for d in range(field.n_days):
        x0, x1 = field.cell(d)
        lt = time.localtime(field.mids[d] + 43200)
        lab = DOW2[lt.tm_wday][:chars]
        tw = text_width(lab)
        if d == 0:
            rgb = C_NOWLINE             # today
        elif d == focus_day:
            rgb = C_LAMP                # the evening the headline is about
        elif lt.tm_wday >= 5:
            rgb = C_DIM
        else:
            rgb = C_FAINT
        blit_text(dst, lay.day_y, x0 + max(0, (x1 - x0 - tw) // 2), lab, rgb)


def draw_event(dst, lay, field, day, y0, y1, state, k=1.0):
    """One lit window: a dim body under a bright edge at its start time.

    The bright row is the *start*, which is the only number on the block
    somebody needs; the body below it is how long it runs.

    Inset from the day rule it sits against, so two events on consecutive
    evenings stay two blocks -- without that they merge into one wide one and
    the panel says something false about the week. The inset shrinks with the
    cell, because at a six-week span a fixed two columns would eat most of a
    block; what it never does is shrink the block below three columns, which is
    the narrowest thing still visible at three metres.
    """
    x0, x1 = field.cell(day)
    wide = x1 - x0
    pad = 2 if wide >= 11 else (1 if wide >= 6 else 0)
    bx0, bx1 = x0 + pad, x1 - (1 if wide >= 6 else 0)
    if bx1 - bx0 < 3:
        bx0 = max(x0, min(bx0, x1 - 3))
        bx1 = min(dst.shape[1], max(bx1, bx0 + 3))
    if bx1 <= bx0:
        return
    rgb = STATE_RGB[state]
    body = tuple(int(max(0, min(255, c * STATE_BODY[state] * k))) for c in rgb)
    edge = tuple(int(max(0, min(255, c * k))) for c in rgb)
    dst[y0:y1, bx0:bx1] = body
    dst[y0, bx0:bx1] = edge


def draw_nodata(dst, lay, lines):
    """The honest panel: no field, no implied calendar."""
    dst[:] = (5, 6, 9)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.h // 2 - (len(lines) * (GLYPH_H * scale + 2)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        blit_text(dst, y, x, s, rgb, sc)
        y += GLYPH_H * sc + 3
    return dst


# --------------------------------------------------------------------------
# The headline: the one sentence somebody reads from three metres.
# --------------------------------------------------------------------------

def headline(rec, field, now, h24):
    """(when, name, rgb, pulsing, focus_event).

    Five phrasings and a ladder between them, ordered by how much the answer
    changes what somebody does. "IN 40 MIN" is the only one that can change a
    decision in the next minute, so it wins whenever it is true, even over an
    event that started an hour ago and is still running.
    """
    ahead = [e for e in rec["events"] if e["end"] > now]
    if not ahead:
        return "NOTHING ON", "THE CALENDAR IS CLEAR", C_DIM, False, None

    ev = ahead[0]
    # Something starting inside the hour outranks something already running:
    # the first is a thing you can still get to, the second is a thing you
    # either are at or are not.
    for cand in ahead:
        if 0 < cand["t"] - now <= SOON_S:
            ev = cand
            break

    dt = ev["t"] - now
    if dt <= 0:
        left = int((ev["end"] - now) / 60.0)
        when = "ON NOW"
        if left <= 90:
            when = "ON NOW  %d MIN LEFT" % max(1, left)
        return when, ev["name"], C_LIVE, True, ev

    if dt <= SOON_S:
        mins = max(1, int(math.ceil(dt / 60.0)))
        return "IN %d MIN" % mins, ev["name"], C_SOON, True, ev

    at = clock_str(ev["t"], h24)
    day = field.day_of(ev["t"])
    if day == 0:
        lt = time.localtime(ev["t"])
        word = "TONIGHT" if lt.tm_hour >= 17 else "TODAY"
        return "%s %s" % (word, at), ev["name"], C_LAMP, False, ev
    if day == 1:
        return "TOMORROW %s" % at, ev["name"], C_LAMP, False, ev
    lt = time.localtime(ev["t"])
    if day is not None and day <= 6:
        return "%s %s" % (DOW3[lt.tm_wday], at), ev["name"], C_LAMP, False, ev
    # Past this week a weekday alone is ambiguous, so the date joins it. The
    # font has a slash and no comma, which decides the format.
    return ("%s %d/%d %s" % (DOW3[lt.tm_wday], lt.tm_mon, lt.tm_mday, at),
            ev["name"], C_LAMP, False, ev)


def count_line(rec, field, now):
    """'3 IN 3 WEEKS' -- the how-packed number, and the panel's only figure.

    Weeks rather than days because the span is always whole weeks and "21 DAYS"
    invites somebody to count columns to check.
    """
    lo = field.mids[0]
    hi = field.mids[-1] + 86400
    n = sum(1 for e in rec["events"] if lo <= e["t"] < hi and e["end"] > now)
    weeks = max(1, int(round(field.n_days / 7.0)))
    return "%d IN %d WEEK%s" % (n, weeks, "" if weeks == 1 else "S")


def pretend_clock(mode, events, now):
    """A real moment relative to a real event, for reviewing the urgent states.

    Nothing about the data is fabricated -- only which minute the panel thinks
    it is -- which is why this is safe to leave in: every block, every hour and
    every sunset drawn under it is the true one. The panel stamps SIM anyway.
    """
    if mode == "none" or not events:
        return now, False
    ahead = [e for e in events if e["end"] > now] or events
    first = ahead[0]
    if mode == "soon":
        return first["t"] - 40 * 60.0, True
    if mode == "now":
        return first["t"] + min(25 * 60.0, max(60.0, first["dur"] * 0.4)), True
    if mode == "quiet":
        # Far enough back that nothing is near, on a day of the same weekday so
        # the ruler looks like an ordinary week.
        return first["t"] - 35 * 86400.0, True
    return now, False


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h)
    cache = args.cache_dir
    h24 = bool(args.h24)

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)      # everything that never moves
    card = np.zeros((h, w, 3), np.uint8)        # the absent-record panel

    cell = {"rec": None, "problem": None, "loaded": -1e18, "stale": False,
            "sim": False, "now": 0.0, "field": None, "when": "", "name": "",
            "hl_rgb": C_DIM, "pulse": False, "hl_at": (0, 0), "name_at": (0, 0),
            "focus": None, "nowline": None, "now_inside": False, "count": ""}

    def reload_data(wall):
        rec, age, problem = read_calendar(cache)
        cell["rec"], cell["problem"], cell["loaded"] = rec, problem, wall
        if rec is None:
            cell["stale"] = cell["sim"] = False
            cell["field"] = None
            draw_nodata(card, lay, [
                ("NO CALENDAR YET", C_WARN),
                ("NOBODY HAS ASKED SEQUOIA.GARDEN WHAT IS ON", C_TEXT),
                ("RUN  PYTHON3 FTDATA.PY --ONCE --ONLY SEQUOIA-CALENDAR",
                 C_DIM),
            ])
            return

        ttl = ftdata.ttl_for(PRODUCT) or 21600.0
        cell["stale"] = age is not None and age > ttl * STALE_TTL_MULT

        now, sim = pretend_clock(args.pretend, rec["events"], wall)
        cell["now"], cell["sim"] = now, sim

        day0 = local_midnight(now)
        span = choose_span(rec["events"], now, day0, args.span)
        mids = day_starts(day0, span)
        field = Field(lay, mids, choose_hours(rec["events"], mids, span))
        cell["field"] = field

        when, name, rgb, pulse, focus = headline(rec, field, now, h24)
        cell["when"], cell["name"], cell["hl_rgb"] = when, name, rgb
        cell["pulse"], cell["focus"] = pulse, focus
        cell["count"] = count_line(rec, field, now)

        # ---- the picture -------------------------------------------------
        static[:] = 0
        sky = sky_field(lay, field)
        static[lay.grid_y0:lay.grid_y1] = ds.dither(sky)

        # Today's spent hours, darkened before anything is drawn on top, so a
        # social that finished at noon still shows through as a grey block.
        # The now-line always exists, because the pip that crawls along it is
        # the panel's guarantee of motion and because a viewer needs to be able
        # to find "here" whatever time they walk past. What changes outside the
        # window is what it *claims*: clamped to an edge and drawn in a plain
        # grey, it says "the present is off this end of the axis" rather than
        # "the present is at five o'clock", which a bright line at the top row
        # would say and which would be false for most of the working day.
        hnow = field.hour_of(now, 0)
        x0, x1 = field.cell(0)
        inside = field.lo_h < hnow < field.hi_h
        if hnow >= field.hi_h:
            cut = lay.grid_y1
            now_row = lay.grid_y1 - 1
        elif hnow <= field.lo_h:
            cut = lay.grid_y0
            now_row = lay.grid_y0
        else:
            cut = int(round(field.row_of(hnow)))
            now_row = max(lay.grid_y0, min(lay.grid_y1 - 1, cut))
        if cut > lay.grid_y0:
            reg = static[lay.grid_y0:cut, x0:x1]
            np.copyto(reg, reg.astype(f32) * ELAPSED_K, casting="unsafe")

        draw_rules(static, lay, field, h24)

        placed = []
        for e in rec["events"]:
            got = place(field, e, now)
            if got is not None:
                placed.append((e, got))
        # Painted furthest-out first, so an overlap resolves towards the near
        # event, which is the one the headline is about.
        for e, (day, y0, y1, state) in reversed(placed):
            draw_event(static, lay, field, day, y0, y1, state)

        focus_day = field.day_of(focus["t"]) if focus else None
        draw_days(static, lay, field, focus_day)

        # The now-line, and the geometry render() needs to run a pip along it.
        static[now_row, x0:x1] = C_NOWLINE if inside else C_NOWEDGE
        cell["nowline"] = (now_row, x0, x1)
        cell["now_inside"] = inside

        # ---- type --------------------------------------------------------
        if lay.head_h:
            blit_text(static, 0, 1, rec["site"], C_NAME)
            cnt = cell["count"]
            cx = w - text_width(cnt) - 1
            blit_text(static, 0, cx, cnt, C_DIM)
            note, nrgb = ("", C_DIM)
            if cell["sim"]:
                note, nrgb = "SIM", C_SIM
            elif cell["stale"]:
                note, nrgb = "STALE " + ftdata.describe_age(age).upper(), C_WARN
            if note:
                blit_text(static, 0, max(0, cx - text_width(note) - 4),
                          note, nrgb)
        if lay.rule_y:
            static[lay.rule_y, :] = C_RULE

        if lay.hl_h:
            wy = lay.hl_y + 1
            wx = 2
            ww = text_width(cell["when"], 2)
            cell["hl_at"] = (wy, wx)
            # The name sits beside the when, vertically centred against it, and
            # is clipped rather than allowed to run off: the longest title the
            # fetcher can store is 44 characters and the longest seen is 28, so
            # this only ever fires on a title nobody has written yet.
            nx = wx + ww + 6
            ny = wy + (2 * GLYPH_H - GLYPH_H) // 2
            room = max(0, w - nx - 1)
            nm = cell["name"]
            while nm and text_width(nm) > room:
                nm = nm[:-1].rstrip()
            cell["name_at"] = (ny, nx)
            cell["name"] = nm
            blit_text(static, ny, nx, nm, C_TEXT)
            if not cell["pulse"]:
                # Baked, because nothing about it changes between frames. Only
                # the pulsing variant is re-drawn per frame; see render().
                blit_text(static, wy, wx, cell["when"], cell["hl_rgb"], 2)

    def render(t, i):
        rec = cell["rec"]
        if rec is None:
            # Baked like everything else, with the same crawling dot the other
            # data panels use: a first-boot card sits on the wall until the
            # timer fires, and re-rasterising three strings twenty times a
            # second for a quarter of an hour is the most expensive thing this
            # file could do on the Pi.
            frame[:] = card
            px = int(((t / 6.0) % 1.0) * w)
            frame[h - 2, max(0, px - 2):px + 1] = C_DIM
            return frame

        frame[:] = static

        # The reveal: the three weeks arriving left to right, which is time
        # arriving in the direction the axis runs. Two slice writes, and what
        # is behind the edge is black rather than sky, so the field draws
        # itself in rather than being uncovered.
        edge = w
        if args.reveal > 0 and t < args.reveal:
            edge = int(w * (t / args.reveal))
            frame[:, edge:] = 0
            if edge < w:
                frame[:, edge] = C_NOWLINE

        # The imminent headline, breathing. Not baked, because this is the one
        # thing on the panel that has to be impossible to miss, and a still
        # panel between two animated ones reads as a crashed wall.
        if lay.hl_h and cell["pulse"]:
            k = 0.55 + 0.45 * math.sin(t * 2.0 * math.pi * PULSE_HZ)
            wy, wx = cell["hl_at"]
            blit_text(frame, wy, wx, cell["when"],
                      tuple(int(c * k) for c in cell["hl_rgb"]), 2)

        # The focus block, breathing in step with the headline, so the words
        # and the rectangle they are about are visibly the same thing.
        focus = cell["focus"]
        if focus is not None and cell["pulse"] and cell["field"] is not None:
            got = place(cell["field"], focus, cell["now"])
            if got is not None:
                day, y0, y1, state = got
                if cell["field"].cell(day)[0] < edge:
                    k = 0.62 + 0.38 * math.sin(t * 2.0 * math.pi * PULSE_HZ)
                    draw_event(frame, lay, cell["field"], day, y0, y1, state, k)

        # A pip crawling along the now-line. The one thing guaranteed to move
        # in *every* frame: a breath is an integer brightness and rounds to the
        # same value for a dozen frames near the top of its sine, and the
        # reveal is over after two seconds. It is also the right mark -- it is
        # the present, going past.
        nl = cell["nowline"]
        if nl is not None:
            row, x0, x1 = nl
            if x0 < edge:
                span = max(1, min(x1, edge) - x0)
                px = x0 + int(((t / PIP_S) % 1.0) * span)
                # Always brighter than the line under it, including the clamped
                # grey one: if the pip matched its line there would be no
                # moving pixel on the panel at all for most of the working day,
                # which is the failure this pip exists to prevent.
                frame[row, max(x0, px - 1):min(x1, px + 2)] = (
                    C_NOWPIP if cell["now_inside"] else (200, 200, 188))
        return frame

    reload_data(parse_when(args.now) or time.time())
    render.state = cell               # the tests reach in here; nothing else
    render.layout = lay
    render.field = cell["field"]
    render.static = static
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "what is on at the makerspace, and how soon",
                  fps=20)


if __name__ == "__main__":
    main()
