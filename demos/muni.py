#!/usr/bin/env python3
"""The 19, 22 and 55 converging on the stops we actually stand at.

The wall is in a makerspace at 1736 18th Street and the wiki names three buses
that serve it. Somebody walking past the wall has exactly one question about
them, and it is not "where is the bus". It is **do I need to leave now?**

That question has two halves and every departure board in the world shows only
one. "22 in 4 minutes" is useless on its own, because the 22's stop is four
hundred metres away and it takes seven minutes to walk there. The bus is not
early; you are late. So this panel draws the other half, to the same scale, on
the same axis.

**One row of the panel is one stop, and its name sits in the middle of it.**
That is the representation everything else falls out of, and it is the second
one this panel has had. The first gave a lane to each *route*, put our door at
the left edge and ran time rightwards, which drew every bus arriving from the
same side of the world -- so a viewer had no way to see that the 19 they can
see out of the window is going the wrong way. Direction was in a three-letter
destination label and nowhere else.

Now the geometry carries it:

  * **The centre column of a row is the stop.** Its name is written above it,
    the brightest type on the panel, because that is the thing you have to
    recognise before anything else on the row means anything.
  * **The two directions approach it from opposite edges.** Inbound runs in
    from the left along the top of the road, outbound in from the right along
    the bottom. Distance from the centre is minutes-until-arrival, so a bus
    slides *inwards* and touches the name at the moment it pulls in. Two buses
    on the same street heading opposite ways, meeting the place you stand,
    which is what is physically out there.
  * **The gap around the name is your walk, drawn to scale, twice.** A post
    stands on the road at exactly walking-time from the centre on each side,
    and between the two posts the road is dotted rather than solid. The width
    of that dotted gate *is* the walk: De Haro & 18th is 140 m away so its gate
    is a narrow collar around its name, and 16th & Wisconsin is 413 m away so
    its gate is half the row. Nothing is drawn equidistant; the three real
    distances are the picture.
  * **A bus outside the gate you can still catch. A bus inside it is gone** --
    it will reach the stop before you can walk there -- and it turns grey to
    say so. Same rule as the first layout, mirrored, and still no legend.
  * **The two numbers flanking the name are minutes before you must leave**,
    one per direction, on the side of the name that direction comes from. NOW
    means put your shoes on.

**The rows are not the same height, on purpose.** Six (route, direction) lanes
of eight rows each was the old panel and it was six equal bands of a thing that
is not equal. Grouping by stop instead collapses the six into four real places
-- the 55 and the 22 each have both directions at one corner -- and the nearest
of those four gets a taller row, a taller bus and more air than the far ones.
The stop you can reach in two minutes is the one you can act on.

**The bar along the road is lateness, and it is drawn to the same scale as
everything else.** 511 gives both what the timetable promised and what is
actually going to happen, so the gap between them is a length on this axis, not
a number to read. A mark trailing behind a bus is one running late; a mark
ahead of it is one running early, which sounds harmless and is not -- an early
22 is a 22 you will miss.

**Live where it can be, honest where it cannot.** The header says LIVE and goes
green when it is drawing 511 predictions of tracked vehicles. A bus 511 is not
actually watching -- `Monitored: false`, meaning it is quoting its own
timetable back -- is drawn as an outline instead of a solid, so a scheduled bus
never wears the clothes of a tracked one. And with no key, no fetch or a record
past its TTL, the panel falls back to SFMTA's published timetable, turns the
header amber and says SCHEDULE. It never claims more than it has.

**Where the data comes from.** Two products, and the split is deliberate.
`muni-18th` is SFMTA's static GTFS off San Francisco's open data portal --
keyless, fetched daily -- and it is the source of the *geometry*: which stop is
nearest for each route in each direction (derived from `ftsite`, not
hardcoded), how far away it is, how long that is to walk, and the fallback
timetable. `muni-live` is 511.org SIRI StopMonitoring, which needs a free token
in `$FT_511_KEY`, and it is the source of the *predictions*. 511 rate-limits to
sixty requests an hour and one stop per request, so six stops is six requests
and the fetch interval is fifteen minutes -- 24 requests an hour. `ftdata.py`
explains that budget at length. What is cached is absolute arrival timestamps,
so the countdown on the wall stays correct between fetches even though the
revisions being counted down to do not get revised.

    $ python3 ftdata.py --once --only muni-18th     # geometry + timetable
    $ FT_511_KEY=... python3 ftdata.py --once --only muni-live

**The clock.** This is one of the wall-clock panels: it takes the present
moment from `time.time()` once, in `build()`, and every frame is a pure
function of `t` from there. So a segment animates, a preview bakes
reproducibly, and `--now` pins the moment, which is how the tests and the
screenshot get a fixed picture.

    $ python3 muni.py --now 1786566746        # a fixed weekday morning
    $ python3 muni.py --horizon 25            # a longer approach
    $ python3 muni.py --source schedule       # ignore live, draw the timetable
"""

import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata
import ftsite

f32 = np.float32

PRODUCT = "muni-18th"                   # geometry and the fallback timetable
LIVE_PRODUCT = "muni-live"              # 511 predictions

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, propagation, sort and tide
# draw with: five rows a glyph, each row an octal digit whose three bits are
# the three columns. The height is *measured* off the built glyphs rather than
# assumed to be five, because a panel that assumes five and meets six clips the
# bottom off every capital E, which is a bug this tree has actually shipped.
# Everything that lays out a row is expressed in GLYPH_H / GLYPH_PITCH, so a
# taller font would push the rows apart rather than overprint them.
#
# The charset is measured too, and it matters here in a way it did not before:
# stop names are now the focal type on the panel and the font has no `&`. That
# is not worked around, it is used -- the abbreviator joins cross streets with
# `/`, which is the same convention `ftdata._muni_short` already uses for
# headsigns ("16TH/MSN"), so the panel has one separator and not two.
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


def text_mask(s):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((GLYPH_H, 1), bool)
    out = np.zeros((GLYPH_H, len(s) * GLYPH_PITCH - 1), bool)
    for i, ch in enumerate(s):
        g = _GLYPHS.get(ch, _GLYPHS[" "])
        out[:g.shape[0], i * GLYPH_PITCH:i * GLYPH_PITCH + g.shape[1]] = g
    return out


def text_width(s):
    return max(1, len(str(s)) * GLYPH_PITCH - 1)


def sanitise(s):
    """Drop anything the font cannot draw, rather than printing blanks."""
    return "".join(c for c in str(s).upper() if c in CHARSET)


def _shade(rgb, k):
    """One colour, scaled. `ds.shade` wants a whole image and a factor field."""
    return tuple(int(max(0, min(255, round(c * k)))) for c in rgb)


def blit_text(dst, y, x, s, rgb):
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn."""
    m = text_mask(s)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    dst[y0:y1, x0:x1][m[y0 - y:y1 - y, x0 - x:x1 - x]] = rgb
    return gw


# --------------------------------------------------------------------------
# Geometry. The panel is 320x64 and it is now symmetric about its middle
# column, because the middle column is the stop.
#
# Vertically: five rows of header, one rule, and the rest handed out to rows of
# stops. A row needs 8 + 2*bus rows -- name, a lateness rule and a bus band
# each side of one road row -- so 14 rows at its smallest and 16 with the taller
# bus. `_allocate` hands the spare rows to the nearest stops first; see there.
# --------------------------------------------------------------------------

W, H = ds.WIDTH, ds.HEIGHT

HDR_H = 5                               # the header's single line of type
RULE_Y = HDR_H                          # the rule, which doubles as the axis
LANE_TOP = HDR_H + 1
LANE_BUDGET = H - LANE_TOP              # 58 rows of stops

LANE_MIN_BUS = 3                        # rows of bus in the smallest row
LANE_MAX_BUS = 4
LANE_MIN_H = 8 + 2 * LANE_MIN_BUS       # 14
LANE_MAX_LANES = LANE_BUDGET // LANE_MIN_H

CX = W // 2                             # the stop: the middle column
EDGE_PAD = 1
HALF_W = CX - EDGE_PAD                  # columns of street on each side

LEFT, RIGHT = -1, 1                     # which way a direction approaches from
# GTFS direction_id -> which edge it comes in from. Inbound on the left is a
# convention, not a compass bearing, and it is held constant across every row
# so that "the left half is one way, the right half is the other" is learnable
# in one row and then free in the rest. Which way each half actually goes is
# written at the outer edge as the destination, which is more use than an arrow.
SIDE_OF_DIR = {1: LEFT, 0: RIGHT}
DIR_OF = {1: "IB", 0: "OB"}             # 511 DirectionRef against GTFS

# --------------------------------------------------------------------------
# Colour. One hue per route, and it cannot come from the feed: SFMTA paints
# every one of its bus routes the same corporate blue (005B95), so GTFS
# route_color would give three identical rows. These three are picked to
# survive three metres and a lit room, and they are the wall's existing
# vocabulary -- the warm/cool pair tide and stringline share, plus a green.
# --------------------------------------------------------------------------

ROUTE_RGB = {
    "19": (255, 168, 58),               # amber
    "22": (86, 198, 255),               # cyan
    "55": (118, 224, 132),              # green
}
ROUTE_FALLBACK = (200, 200, 210)

C_BG = (6, 7, 10)
C_ROAD = (26, 29, 38)                   # the street, outside the gate
C_ROAD_DEAD = (13, 14, 19)              # a half with no service at this stop
C_WALK = (86, 76, 56)                   # the gate: the walk, dotted
C_POST = (150, 140, 110)                # the last moment you can leave
C_STOP = (255, 214, 140)                # the stop itself, at the centre
C_STOP_DIM = (92, 74, 44)
C_NAME = (236, 244, 255)                # the stop's name: the focal type
C_TEXT = (198, 212, 226)
C_DIM = (96, 106, 120)
C_FAINT = (46, 52, 62)
C_LIVE = (120, 235, 150)                # the header tag when 511 is answering
C_SCHED = (255, 176, 48)                # ...and when it is not
C_STALE = (255, 96, 72)
C_URGENT = (255, 246, 226)              # "leave now": the brightest thing here
C_MISSED = (76, 70, 78)                 # a bus inside the gate: gone
C_LATE = (214, 78, 66)                  # running behind the timetable
C_EARLY = (72, 140, 190)                # running ahead of it, which is worse

# How far out the street reaches, in minutes, per side. Fifteen puts the 22's
# posts -- seven minutes of walking -- roughly halfway out on each side, which
# is the honest picture, and still leaves seven catchable minutes beyond them.
# One pixel is about six seconds, which is the same resolution the one-sided
# twenty-minute layout had, because the axis lost half its width and most of
# its length at the same time.
HORIZON_MIN = 15.0

# Buses are drawn a little past the stop so "you have just missed one" is
# visible, rather than a bus vanishing at the instant it mattered.
PAST_MIN = -0.5

# Below this, a bus is on time and the lateness mark is clutter.
LATE_FLOOR_MIN = 0.4

# Street-type words that carry no information once every name has them.
SUFFIXES = set(("ST", "STREET", "AVE", "AVENUE", "BLVD", "BOULEVARD",
                "DR", "DRIVE", "RD", "ROAD", "WAY", "PL", "PLACE",
                "TER", "TERRACE", "LN", "LANE", "CT", "COURT",
                "HWY", "HIGHWAY", "PKWY", "PARKWAY"))


def add_arguments(ap):
    ap.add_argument("--horizon", type=float, default=HORIZON_MIN,
                    help="minutes of approach drawn on each side")
    ap.add_argument("--now", type=float, default=0.0,
                    help="pin the clock to this epoch time (0 = the real one)")
    ap.add_argument("--source", choices=("auto", "live", "schedule"),
                    default="auto",
                    help="auto uses 511 when a fresh record is cached")
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")


# --------------------------------------------------------------------------
# The data, turned into rows of stops with absolute arrival times.
# --------------------------------------------------------------------------

def _load(name, cache_dir):
    return ftdata.load(name, cache_dir) if cache_dir else ftdata.load(name)


def _midnight(date):
    """Epoch seconds of the local midnight starting a GTFS service date."""
    y, m, d = int(date[0:4]), int(date[4:6]), int(date[6:8])
    return time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))


def scheduled(payload, stop, now, span=(-900.0, 5400.0)):
    """One stop's timetabled departures near `now`, as absolute epoch times.

    Three service dates are unrolled, not one, and that is the whole reason
    this is a function rather than a lookup. GTFS spells "still Tuesday's
    service" as an hour past 24 -- the 22 runs to 30:09 -- so this morning's
    owl trips live under *yesterday's* date, and a panel that unrolled only
    today would show an empty street at 05:00 with a bus a block away.
    Tomorrow is unrolled for the mirror-image reason, so that at 23:55 the
    00:10 departure is already on the street.
    """
    lo, hi = now + span[0], now + span[1]
    services = payload.get("services") or {}
    times = stop.get("times") or {}
    out = []
    for delta in (-1, 0, 1):
        date = time.strftime("%Y%m%d", time.localtime(now + delta * 86400.0))
        active = services.get(date)
        if not active:
            continue
        base = _midnight(date)
        for service in active:
            for minute in times.get(service, ()):
                when = base + minute * 60.0
                if lo <= when <= hi:
                    out.append((when, None, False))
    out.sort()
    return out


def live_visits(live, stop):
    """One stop's 511 predictions, filtered to this flow's route and direction.

    The filter matters: 16th & Wisconsin is the 22's stop *and* a 55 stop, so
    its record carries both, and the 55 has its own stop 200 m closer. Drawing
    Wisconsin's 55 on the 55's row would put it at the wrong walk distance,
    which is the one thing this panel exists to get right.
    """
    visits = (live.get("stops") or {}).get(str(stop.get("code") or ""))
    if not visits:
        return []
    route = str(stop.get("route") or "")
    want = DIR_OF.get(int(stop.get("dir") or 0))
    out = []
    for v in visits:
        if str(v.get("line") or "") != route:
            continue
        if want and str(v.get("dir") or "") != want:
            continue
        try:
            when = float(v["exp"])
        except (KeyError, TypeError, ValueError):
            continue
        aim = v.get("aim")
        try:
            aim = float(aim) if aim is not None else None
        except (TypeError, ValueError):
            aim = None
        out.append((when, aim, bool(v.get("mon"))))
    out.sort()
    return out


# --------------------------------------------------------------------------
# Naming. The stop name is now the focal text and the real ones are long:
# "Connecticut St & 18th St" is 24 characters, 95 px at this font's pitch, and
# there are only 320 columns with a bus coming in from each end of them.
#
# Three reductions, in order, and each is reversible in the sense that nothing
# ambiguous survives them:
#
#   1. Street-type words go. Every name on this panel is an intersection of two
#      San Francisco streets, so ST/AVE/BLVD distinguish nothing.
#   2. `&` becomes `/`, because the font has no ampersand and `/` is already
#      what `ftdata._muni_short` uses for headsigns.
#   3. The street the *panel itself* is on is elided. It is derived, not
#      hardcoded -- whichever street appears in the most stop names is our own
#      by definition, and it needs at least two appearances before it is
#      dropped -- and it is then written once in the header, where it labels
#      the whole panel instead of three quarters of the rows. That turns
#      "CONNECTICUT/18TH", "DE HARO/18TH" and "RHODE ISLAND/18TH" into
#      "CONNECTICUT", "DE HARO" and "RHODE ISLAND", and leaves the one stop
#      that is genuinely somewhere else reading "16TH/WISCONSIN" -- which is
#      exactly the distinction a reader needs, since that is the far one.
# --------------------------------------------------------------------------

def street_parts(name):
    """A stop name as its cross streets, uppercased and de-suffixed."""
    s = sanitise(str(name or "").upper().replace("&", "/").replace(".", ""))
    out = []
    for part in s.split("/"):
        toks = [t for t in part.split() if t not in SUFFIXES]
        joined = " ".join(toks).strip()
        if joined:
            out.append(joined)
    return out


def home_street(all_parts):
    """The street this panel is on: the one most stop names share."""
    counts = {}
    for parts in all_parts:
        for p in set(parts):
            counts[p] = counts.get(p, 0) + 1
    best, best_n = None, 1
    for p in sorted(counts):
        if counts[p] > best_n:
            best, best_n = p, counts[p]
    return best


def abbreviate(parts, home):
    """The name as drawn: the cross streets, minus our own street."""
    kept = [p for p in parts if p != home]
    return "/".join(kept or parts)


# --------------------------------------------------------------------------
# One row of the panel: a place, and the one or two directions that call there.
# --------------------------------------------------------------------------

class Flow(object):
    """One (route, direction) calling at a stop: its walk, and its buses."""

    def __init__(self, stop, buses):
        self.route = str(stop.get("route") or "?")
        self.dir = int(stop.get("dir") or 0)
        self.label = sanitise(stop.get("label") or "")
        self.code = str(stop.get("code") or "")
        self.metres = int(stop.get("metres") or 0)
        self.walk_min = float(stop.get("walk_s") or 0.0) / 60.0
        self.rgb = ROUTE_RGB.get(self.route, ROUTE_FALLBACK)
        self.side = SIDE_OF_DIR.get(self.dir, RIGHT)
        self.buses = buses
        self.post_x = CX                # filled in by Lane, which knows ppm


class Lane(object):
    """One stop: its name, and the flows arriving at it from each side."""

    def __init__(self, key, flows, ppm, horizon):
        self.key = key
        self.flows = flows
        self.horizon = horizon
        self.walk_min = min(f.walk_min for f in flows)
        self.metres = min(f.metres for f in flows)
        self.routes = sorted(set(f.route for f in flows))
        self.name = ""                  # set by build(), which knows them all
        # At most one flow a side. If a feed ever hands us two of the same
        # direction at one place, the nearer wins rather than the later.
        self.by_side = {}
        for f in sorted(flows, key=lambda f: f.walk_min):
            self.by_side.setdefault(f.side, f)
        for f in flows:
            # The post: the last column from which you can still walk it. Its
            # distance from the centre is the walk time at the same
            # pixels-per-minute the buses move at, which is the whole claim.
            off = min(f.walk_min, horizon) * ppm
            f.post_x = int(round(CX + f.side * max(2.0, min(HALF_W - 2, off))))
        # Vertical layout, filled in by _allocate/_place.
        self.y0 = 0
        self.h = LANE_MIN_H
        self.bus_h = LANE_MIN_BUS
        # Filled in by _background(), which is where the name's width -- and
        # therefore where the two margin numbers can go -- becomes known.
        self.name_y = 0
        self.slot_left = (0, MARGIN_W)
        self.slot_right = (W - MARGIN_W, W)


def _allocate(n, budget=LANE_BUDGET):
    """Rows per stop, nearest first.

    Equal bands were the old panel's answer and they are the wrong answer: the
    stop you can reach in two minutes is the one you can act on, and the one
    seven minutes away is mostly there to explain why you cannot. So every row
    gets the minimum that fits a name, a road and a bus band each side (14),
    and the spare rows are handed out from the nearest outwards -- two at a
    time first, which is what buys a taller bus, then one at a time as air.
    """
    if n <= 0:
        return []
    h = [LANE_MIN_H] * n
    used = LANE_MIN_H * n
    for i in range(n):                          # taller buses, nearest first
        if used + 2 > budget:
            break
        if h[i] >= 8 + 2 * LANE_MAX_BUS:
            continue
        h[i] += 2
        used += 2
    i = 0
    while used < budget:                        # leftovers become air
        h[i % n] += 1
        used += 1
        i += 1
    return h


def _place(lanes):
    """Give every row its top and its internal heights."""
    heights = _allocate(len(lanes))
    y = LANE_TOP
    for lane, h in zip(lanes, heights):
        lane.y0 = y
        lane.h = h
        lane.bus_h = LANE_MAX_BUS if h >= 8 + 2 * LANE_MAX_BUS else LANE_MIN_BUS
        y += h
    return lanes


def lane_rows(lane):
    """(name_y, late_in_y, bus_in_y, road_y, bus_out_y, late_out_y).

    The block is centred in whatever height the row got, so a row with a spare
    pixel gets air above and below rather than a stripe of it at one end.
    """
    b = lane.bus_h
    block = 8 + 2 * b
    top = lane.y0 + max(0, (lane.h - block) // 2)
    name_y = top
    late_in = top + GLYPH_H
    bus_in = late_in + 1
    road = bus_in + b
    bus_out = road + 1
    late_out = bus_out + b
    return name_y, late_in, bus_in, road, bus_out, late_out


def build(args):
    horizon = max(4.0, float(getattr(args, "horizon", HORIZON_MIN)))
    ppm = HALF_W / horizon                          # pixels per minute, a side
    pinned = float(getattr(args, "now", 0.0) or 0.0)
    now0 = pinned if pinned > 0 else time.time()
    want = getattr(args, "source", "auto")
    cache_dir = getattr(args, "cache_dir", None)

    got = _load(PRODUCT, cache_dir)
    if got is None:
        return _card(now0, "NO MUNI DATA",
                     "RUN FTDATA --ONLY " + PRODUCT, C_DIM)
    geom, geom_age = got
    if not isinstance(geom, dict) or not geom.get("stops"):
        return _card(now0, "NO MUNI DATA", "CACHED RECORD IS UNREADABLE",
                     C_STALE)

    # Live if we have it and it is worth believing. A record past its TTL is
    # not silently used -- eighteen-minute-old predictions are the design, but
    # hour-old ones are a different claim -- so it falls back to the timetable,
    # which does not go stale in the same way.
    live = None
    live_age = 0.0
    if want != "schedule":
        gotl = _load(LIVE_PRODUCT, cache_dir)
        if gotl is not None and isinstance(gotl[0], dict):
            if ftdata.is_fresh(LIVE_PRODUCT, gotl[1]) or want == "live":
                live, live_age = gotl

    # A static timetable can be perfectly fresh and still not cover today: the
    # feed carries a service period and the day after it ends there is no
    # answer at all. Only fatal when there is no live record to draw instead.
    today = time.strftime("%Y%m%d", time.localtime(now0))
    expired = today not in (geom.get("services") or {})
    if expired and live is None:
        return _card(now0, "SCHEDULE EXPIRED",
                     "SFMTA FEED RAN TO %s" % _pretty(geom.get("feed_end")),
                     C_STALE)

    lanes, dropped, home = group_stops(geom, live, now0, ppm, horizon)
    if not lanes:
        return _card(now0, "NO MUNI DATA", "RECORD NAMES NO STOPS", C_STALE)

    bg = _background(lanes, home, horizon, ppm, dropped)
    sprites = _bus_sprites(sorted(set(l.bus_h for l in lanes)))

    if live is not None:
        tag, tag_rgb, age = "LIVE", C_LIVE, live_age
        stale = not ftdata.is_fresh(LIVE_PRODUCT, live_age)
    else:
        tag, tag_rgb, age = "SCHEDULE", C_SCHED, geom_age
        stale = not ftdata.is_fresh(PRODUCT, geom_age)
    age_txt = ftdata.describe_age(age)

    def render(t, frame):
        out = bg.copy()
        clock = now0 + t
        for lane in lanes:
            _draw_lane(out, lane, clock, ppm, sprites)
        _header(out, clock, tag, tag_rgb, age_txt, stale)
        return out

    # The laid-out rows, hung off the closure. Nothing in `render` reads them,
    # so purity is untouched; `scripts/test-muni.py` needs them because every
    # column it asserts is derived from a row's height, its posts and where its
    # name ended up, and recomputing that in the test would be asserting the
    # test against itself.
    render.lanes = lanes
    render.home = home
    return render


def group_stops(geom, live, now0, ppm, horizon):
    """The record's (route, direction) stops, collapsed into places.

    Six flows, four places: the 55 and the 22 each have both directions at one
    corner, and the 19 does not -- its two directions are a block apart, at De
    Haro and at Rhode Island. That asymmetry is real and the old per-route
    layout hid it. Grouping is on the *unabbreviated* cross streets, so that
    "16th Street & Wisconsin St" and "16th St & Wisconsin St" -- which is how
    SFMTA spells the two sides of the same corner -- land in one row, while two
    genuinely different corners that abbreviate alike do not.
    """
    stops = list(geom.get("stops") or ())
    parts = [street_parts(s.get("name")) for s in stops]
    home = home_street(parts)

    order, groups = [], {}
    for stop, p in zip(stops, parts):
        key = tuple(sorted(p)) or (str(stop.get("code")),)
        if key not in groups:
            groups[key] = []
            order.append(key)
        if live is not None:
            buses = live_visits(live, stop)
        else:
            buses = scheduled(geom, stop, now0)
        groups[key].append(Flow(stop, buses))

    lanes = [Lane(k, groups[k], ppm, horizon) for k in order]
    lanes.sort(key=lambda l: (l.walk_min, l.metres))

    # Which rows fit. Nearest first, but a route may not be dropped just for
    # being far: the 22 is the whole reason `scripts/test-muni.py` exists, and
    # a confident panel naming three routes and showing two is this demo's
    # oldest bug. So one row per route is claimed before distance gets a vote.
    chosen, seen = [], set()
    for lane in lanes:
        if not set(lane.routes) <= seen:
            chosen.append(lane)
            seen |= set(lane.routes)
    for lane in lanes:
        if len(chosen) >= LANE_MAX_LANES:
            break
        if lane not in chosen:
            chosen.append(lane)
    dropped = len(lanes) - min(len(chosen), LANE_MAX_LANES)
    chosen = chosen[:LANE_MAX_LANES]
    chosen.sort(key=lambda l: (l.walk_min, l.metres))

    # Names last, so a collision between two abbreviations can be resolved by
    # falling back to the long form for both rather than for all of them.
    short = [abbreviate(list(l.key), home) for l in chosen]
    for lane, name in zip(chosen, short):
        if short.count(name) > 1:
            name = "/".join(lane.key)
        lane.name = sanitise(name) or "STOP"
    return _place(chosen), dropped, home


def _pretty(d):
    d = str(d or "")
    return "%s-%s-%s" % (d[0:4], d[4:6], d[6:8]) if len(d) == 8 else "?"


# --------------------------------------------------------------------------
# The baked background: everything that does not move. Drawing it once is most
# of why this panel is cheap. Four names, eight posts, eight gates, eight edge
# labels and an axis are a few hundred numpy calls, and they happen in build()
# rather than twenty times a second.
# --------------------------------------------------------------------------

def _background(lanes, home, horizon, ppm, dropped):
    bg = np.zeros((H, W, 3), np.uint8)
    bg[:, :] = C_BG

    x = 1
    x += blit_text(bg, 0, x, sanitise(ftsite.NAME), C_TEXT) + 4
    if home:
        blit_text(bg, 0, x, home, C_DIM)
    if dropped > 0:
        blit_text(bg, 0, x + text_width(home or "") + 4, "+%d" % dropped,
                  C_FAINT)

    # The rule under the header doubles as the panel's one scale bar: a tick
    # every five minutes out from the centre, both ways. Every row shares the
    # centre and the pixels-per-minute, so one axis serves all of them, and it
    # costs no rows of street.
    bg[RULE_Y, :] = C_FAINT
    minute = 5.0
    while minute <= horizon + 1e-6:
        off = int(round(minute * ppm))
        for side in (LEFT, RIGHT):
            x = CX + side * off
            if 0 <= x < W:
                bg[RULE_Y - 1:RULE_Y + 1, x] = C_DIM
        minute += 5.0
    bg[RULE_Y - 1:RULE_Y + 1, CX] = C_STOP_DIM
    # One tick gets its unit, so the scale bar is a scale bar and not a row of
    # dots. The +5 tick is chosen because the header's own type crowds every
    # other one; if the horizon is short enough that it does not exist, or the
    # tag on the right has grown into it, the label is simply not drawn.
    unit_x = CX + int(round(5.0 * ppm)) + 2
    if 5.0 <= horizon and unit_x + text_width("5MIN") < W - 76:
        blit_text(bg, 0, unit_x, "5MIN", C_FAINT)

    for lane in lanes:
        name_y, late_in, bus_in, road, bus_out, late_out = lane_rows(lane)
        del late_in, late_out
        b = lane.bus_h

        for side in (LEFT, RIGHT):
            flow = lane.by_side.get(side)
            lo, hi = (0, CX) if side == LEFT else (CX + 1, W)
            # A half with no service is drawn, but darker: at De Haro only the
            # inbound 19 calls, because the outbound 19 stops a block away at
            # Rhode Island, and an empty half of road says that better than a
            # missing row would.
            bg[road, lo:hi] = C_ROAD if flow is not None else C_ROAD_DEAD
            if flow is None:
                continue

            # The gate: the walk, dotted, from the post in to the centre. Its
            # width is the walk time at the same scale the buses move at, so
            # the 140 m stop wears a narrow collar and the 413 m stop wears
            # half the row. Nothing here is equidistant.
            a, z = sorted((flow.post_x, CX))
            bg[road, a:z:2] = C_WALK

            # The post stands into its own half's bus band, so the two posts
            # of a row point away from each other and the gate reads as a gap.
            if side == LEFT:
                bg[road - min(3, b):road + 1, flow.post_x] = C_POST
            else:
                bg[road:road + min(3, b) + 1, flow.post_x] = C_POST

            # The outer edge names the direction: route number in its own
            # colour, destination in grey. BEACH is north, SHIPYARD is south,
            # and that is more use than an arrow.
            route_w = text_width(flow.route)
            if side == LEFT:
                blit_text(bg, name_y, 1, flow.route, flow.rgb)
                blit_text(bg, name_y, 2 + route_w, flow.label, C_DIM)
            else:
                x = W - 1 - route_w
                blit_text(bg, name_y, x, flow.route, flow.rgb)
                blit_text(bg, name_y, x - 2 - text_width(flow.label),
                          flow.label, C_DIM)

        # The stop itself: a bright column through both bus bands, with the
        # name directly above it. This is the thing the two directions are
        # converging on and it is the only warm vertical on the panel.
        bg[bus_in:bus_out + b, CX] = C_STOP_DIM
        bg[bus_in + 1:bus_out + b - 1, CX] = C_STOP

        name = lane.name
        nx = CX - text_width(name) // 2
        blit_text(bg, name_y, nx, name, C_NAME)
        # The two margin numbers are drawn every frame, flanking the name on
        # the side of the direction they belong to. Their slots are reserved
        # here so the layout does not jump when NOW becomes 12.
        lane.slot_left = (nx - MARGIN_GAP - MARGIN_W, nx - MARGIN_GAP)
        lane.slot_right = (nx + text_width(name) + MARGIN_GAP,
                           nx + text_width(name) + MARGIN_GAP + MARGIN_W)
        lane.name_y = name_y
    return bg


# Wide enough for "00:00", which is what a row prints when nothing on it is
# catchable inside the horizon. Reserving the widest case is what stops the
# name shifting sideways when the answer changes.
MARGIN_W = 5 * GLYPH_PITCH - 1

# Two clear glyph widths of air between the name and each number. One looked
# like a digit belonging to the name -- "8 DE HARO" reads as a street number,
# which is precisely the wrong thing on a panel about a building number.
MARGIN_GAP = 2 * GLYPH_PITCH


# --------------------------------------------------------------------------
# The bus. Nine columns by three or four rows is not enough for a vehicle, so
# it is not a vehicle: a body, a window band, and a bright leading edge on the
# side it is going. Every appearance is baked rather than multiplied per frame
# -- the one you should be running for, the ones behind it, the ones already
# gone, the hollow one for a bus 511 is not actually watching, and each of
# those mirrored for the half of the panel that travels the other way --
# because that is a few dozen tiny arrays built once against three multiplies
# and a flip per bus per frame.
# --------------------------------------------------------------------------

BUS_W = 9
BUS_SHAPE = {
    4: ((0.00, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.00),
        (1.35, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 0.75),
        (1.35, 0.42, 0.42, 1.00, 0.42, 0.42, 1.00, 0.42, 0.75),
        (0.55, 0.90, 0.55, 0.55, 0.55, 0.90, 0.55, 0.55, 0.00)),
    # Three rows for the far stops. The window band and the body have to share
    # a row, so the windows become dark notches in a lit body rather than their
    # own stripe; at this size that still reads as a bus and a solid block does
    # not.
    3: ((0.00, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.00),
        (1.35, 1.00, 0.42, 1.00, 0.42, 1.00, 0.42, 1.00, 0.75),
        (0.55, 0.90, 0.55, 0.55, 0.55, 0.90, 0.55, 0.55, 0.00)),
}

# The same silhouettes with the middle knocked out: a bus 511 is quoting from
# its own timetable rather than tracking. It reads as "the shape of a bus, not
# a bus", which is exactly the claim.
BUS_GHOST = {
    4: ((0.00, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.00),
        (0.90, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.62),
        (0.90, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.62),
        (0.55, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.00)),
    3: ((0.00, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.00),
        (0.90, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.62),
        (0.55, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.62, 0.00)),
}

NEXT, LATER, MISSED = 0, 1, 2
BUS_GAIN = {NEXT: 1.00, LATER: 0.58, MISSED: 1.00}


def _bus_sprites(heights=(3, 4)):
    """(h, route, state, mon, side) -> (rgb image, mask), baked once."""
    out = {}
    routes = list(ROUTE_RGB.items()) + [(None, ROUTE_FALLBACK)]
    for h in heights:
        solid = np.array(BUS_SHAPE[h], f32)
        hollow = np.array(BUS_GHOST[h], f32)
        for route, rgb in routes:
            for state in (NEXT, LATER, MISSED):
                for mon in (True, False):
                    shape = solid if mon else hollow
                    base = np.array(C_MISSED if state == MISSED else rgb, f32)
                    img = np.clip(shape[:, :, None] * BUS_GAIN[state] * base,
                                  0.0, 255.0).astype(np.uint8)
                    mask = shape > 0.0
                    # The baked shape faces left, which is the way the right
                    # half of the panel travels. The left half travels right,
                    # so it gets the mirror -- the bright leading edge has to
                    # be at the front or the bus reads as reversing.
                    out[(h, route, state, mon, RIGHT)] = (img, mask)
                    out[(h, route, state, mon, LEFT)] = (
                        np.ascontiguousarray(img[:, ::-1]),
                        np.ascontiguousarray(mask[:, ::-1]))
    return out


def _blit_sprite(dst, y, x, img, mask, xlo=0, xhi=None):
    """Draw a sprite at (y, x), clipped on all four sides.

    A bus that has just called is drawn a little past the stop so that "you
    have missed one" is visible rather than the bus vanishing at the instant it
    mattered -- but it must not wander into the other direction's half of the
    row, where it would read as a bus going the wrong way. `xlo`/`xhi` are that
    fence, and clipping at it rather than refusing to draw is what makes it
    read as a bus leaving the picture behind the stop.
    """
    gh, gw = mask.shape
    xhi = dst.shape[1] if xhi is None else min(dst.shape[1], xhi)
    y0, x0 = max(0, y), max(xlo, x, 0)
    y1, x1 = min(dst.shape[0], y + gh), min(xhi, x + gw)
    if y1 <= y0 or x1 <= x0:
        return
    sub = mask[y0 - y:y1 - y, x0 - x:x1 - x]
    dst[y0:y1, x0:x1][sub] = img[y0 - y:y1 - y, x0 - x:x1 - x][sub]


MAX_BUSES = 6                           # per direction, per row


def _draw_lane(out, lane, clock, ppm, sprites):
    """One row's buses, lateness marks and two numbers, for this instant."""
    name_y, late_in, bus_in, road, bus_out, late_out = lane_rows(lane)
    b = lane.bus_h

    for side in (LEFT, RIGHT):
        flow = lane.by_side.get(side)
        if flow is None:
            _margin(out, lane, side, None)
            continue
        if side == LEFT:
            bus_y, late_y = bus_in, late_in
            xlo, xhi = 0, CX + 1 + int(round(-PAST_MIN * ppm))
        else:
            bus_y, late_y = bus_out, late_out
            xlo, xhi = CX - int(round(-PAST_MIN * ppm)), W

        first_catchable = None
        drawn = 0
        for when, aim, mon in flow.buses:
            mins = (when - clock) / 60.0
            if mins < PAST_MIN:
                continue
            if mins > lane.horizon:
                break                    # sorted, so the rest are further out
            catchable = mins >= flow.walk_min
            if catchable and first_catchable is None:
                first_catchable, state = mins, NEXT
            elif catchable:
                state = LATER
            else:
                state = MISSED

            x = int(round(CX + side * mins * ppm))

            # Lateness, on the road beside the bus, as a length on the same
            # axis: from where the timetable put this bus to where it actually
            # is. A faint dotted rule with a bright cap on the timetable end,
            # rather than a solid bar -- five minutes early is fifty pixels,
            # and fifty solid pixels per bus shouted louder than the buses did.
            # The cap is what you read; the rule only has to connect it to its
            # bus. Missed buses get none of it: how late a bus you cannot catch
            # is running is not information.
            if aim is not None and state != MISSED:
                slip = (when - aim) / 60.0
                if abs(slip) >= LATE_FLOOR_MIN:
                    rgb = C_LATE if slip > 0 else C_EARLY
                    ax = int(round(CX + side * ((aim - clock) / 60.0) * ppm))
                    x0, x1 = (x, ax) if ax > x else (ax, x)
                    x0, x1 = max(0, x0), min(W - 1, x1)
                    if x1 > x0:
                        out[late_y, x0:x1:2] = _shade(rgb, 0.45)
                    if 0 <= ax < W:
                        # Two rows, reaching from the lateness row towards the
                        # bus band it belongs to, so a cap is unambiguously
                        # attached to the direction above or below it.
                        lo = late_y if side == LEFT else late_y - 1
                        out[lo:lo + 2, ax] = rgb

            img, mask = sprites.get((b, flow.route, state, mon, side),
                                    sprites[(b, None, state, mon, side)])
            _blit_sprite(out, bus_y, x - BUS_W // 2, img, mask,
                         xlo=xlo, xhi=xhi)
            drawn += 1
            if drawn >= MAX_BUSES:
                break

        _margin(out, lane, side, _margin_text(flow, clock, first_catchable))


def _margin(out, lane, side, pair):
    """The one number a direction carries, printed beside the stop's name.

    It flanks the name on the side that direction comes in from, which is the
    whole reason it reads without a legend: the number on the left is about the
    buses on the left. Type over the name row needs a dark plate under it --
    one slice assignment, and worth it, because these two numbers are the
    answer the panel exists to give.
    """
    x0, x1 = lane.slot_left if side == LEFT else lane.slot_right
    y = lane.name_y
    out[y - 1:y + GLYPH_H, max(0, x0 - 1):min(W, x1 + 1)] = C_BG
    if pair is None:
        return
    text, rgb = pair
    w = text_width(text)
    x = x1 - w if side == LEFT else x0     # right-aligned left, left-aligned right
    blit_text(out, y, x, text, rgb)


def _margin_text(flow, clock, first_catchable):
    if first_catchable is not None:
        leave = first_catchable - flow.walk_min
        # Under a minute is NOW rather than "0": a row reading 0 next to
        # another reading NOW invites the question which of the two is sooner.
        if leave < 1.0:
            return "NOW", C_URGENT
        # Route colour, not white: the name beside it is the white, and a
        # number in the route's own hue also says *which* bus it is counting
        # down to on a row where two routes could share a corner. Inside three
        # minutes it goes white-hot instead, which outranks everything.
        return "%d" % int(leave), (C_URGENT if leave < 3.0 else flow.rgb)
    # Nothing catchable on this side. Say when the next one actually is,
    # rather than leaving the flank blank and ambiguous.
    for when, _aim, _mon in flow.buses:
        if (when - clock) / 60.0 >= flow.walk_min:
            return time.strftime("%H:%M", time.localtime(when)), C_DIM
    return "--", C_FAINT


def _header(out, clock, tag, tag_rgb, age_txt, stale):
    """The clock, the source tag, and the age when the age has become news."""
    right = W - 1
    stamp = time.strftime("%H:%M", time.localtime(clock))
    right -= text_width(stamp)
    blit_text(out, 0, right, stamp, C_TEXT)

    right -= 4 + text_width(tag)
    blit_text(out, 0, right, tag, C_STALE if stale else tag_rgb)

    # Age is only worth the columns once it is worth worrying about: a record
    # fetched ten minutes ago is the normal case and does not need announcing.
    if stale:
        text = age_txt + " OLD"
        right -= 4 + text_width(text)
        blit_text(out, 0, right, text, C_STALE)


# --------------------------------------------------------------------------
# The degraded state. A card rather than a blank panel or a traceback: the wall
# is in a room with people in it, and "nothing" is indistinguishable from
# "broken". Still a pure function of t, and still carrying the clock, so that
# even a no-data card is visibly alive.
# --------------------------------------------------------------------------

def _card(now0, title, detail, rgb):
    card = np.zeros((H, W, 3), np.uint8)
    card[:, :] = C_BG
    ty = H // 2 - GLYPH_H - 3
    blit_text(card, ty, (W - text_width(title)) // 2, title, rgb)
    blit_text(card, ty + GLYPH_H + 3, (W - text_width(detail)) // 2,
              detail, C_DIM)
    card[LANE_TOP:H, CX] = C_STOP_DIM

    def render(t, frame):
        out = card.copy()
        stamp = time.strftime("%H:%M", time.localtime(now0 + t))
        blit_text(out, 0, W - 1 - text_width(stamp), stamp, C_DIM)
        return out
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
