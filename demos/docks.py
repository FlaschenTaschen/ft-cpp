#!/usr/bin/env python3
"""Can I get a bike right now? The Bay Wheels docks within a walk of this room.

**The most immediately useful thing on the wall.** Somebody puts their tools
down, walks past this panel on the way to the door, and wants to know one of two
things: is there a bike close enough to be worth walking to, and is it electric,
because Potrero Hill is a hill -- or, coming the other way with a bike, is there
anywhere near here to leave it. Every other data panel in this tree tells you
about the world. This one can change what you do in the next sixty seconds, and
that is the whole justification for the space it takes.

**It is the companion to `bikes.py`, not a second version of it.** That panel is
the city: twelve kilometres of commute axis, half a day replayed, net flow
inferred from how dock counts changed. This one is 1.0 km, right now, counted
rather than inferred, and it is deliberately drawn nothing like it -- a map with
a list beside it and a number beside that, calm, still except for one breathing
mark. `bikes` is meant to be arresting; this is meant to be an instrument.

**Three panes, left to right, and the order is the order you read them in.**

  *the map*   58 x 57 pixels, the wall at the centre, about 39 m to the pixel.
              A local map is the one map that fits this panel: San Francisco's
              383 docks are a square blob 11.8 by 11.3 km and drawing all of
              them on a 5:1 letterbox spends three hundred columns saying the
              city is square -- which is exactly why `bikes.py` refuses to be a
              map. A kilometre of Dogpatch does not have that problem, because
              a local map does not have to fill the panel. Rings at five and ten
              minutes' walk; a 500 m scale bar; the building as a green cross.

  *the list*  the eight nearest docks by name, because "Jackson Playground" and
              "Rhode Island/17th" are how people actually refer to these and a
              dot on a map is not something you can say out loud. Walk minutes,
              a grade mark, the name, and three numbers: classic bikes, ebikes,
              free docks.

  *the number*  how many minutes to the nearest bike of any kind, in the largest
              type on the panel, and what and where it is. Then the nearest
              ebike, the nearest free dock, and the totals inside the radius.

**Every dock is a little vertical bar chart, and the mnemonic is up and down.**
The bright pixel is the dock. What grows *upward* out of it is bikes you can
take -- green for classic, amber for electric, one to three pixels for 1-2, 3-6,
7+ -- and what grows *downward* is free docks you can leave one in, in blue, on
the same scale. A dock with nothing above it has no bikes and gets a red pip; a
dock with nothing below it is jammed full and gets one too. Up is take, down is
leave, and the two failure modes are the same colour because they are the same
disappointment from opposite directions. That symmetry is the point: a station
that is full is exactly as useless to somebody arriving as an empty one is to
somebody leaving, and a panel that only drew bike counts would say the second
and not the first.

**Ebikes are drawn in their own colour and counted in their own column, because
GBFS makes them easy to lose.** The docked ebike count lives in
`num_ebikes_available`. The obvious place to look -- `num_bikes_available_types`
-- is not published at all by Lyft's San Francisco feed, so code that reads it
finds zero at every station and confidently reports a city with no docked
ebikes. There were 79 inside 1.5 km on the evening this was written, a third of
the docked fleet. `num_ebikes_available` is a subset of `num_bikes_available`,
so the classic column is the difference and the two add to the total.

**The loose ebikes are on the map too, as bare amber dots.** Free-floating bikes
have no dock and belong to nobody's station, and one of them is regularly closer
than any dock is -- 321 m on the evening this was written, against Jackson
Playground's 292. They are drawn without the white dock pixel, which is what
tells them apart, and the headline will name one if it is the nearest bike.

**Elevation is in the list because uphill and downhill are different walks.**
The heights come from `bikes-terrain.npz`, the committed USGS 3DEP bake that
`bikes.py` uses, and the mark before each name is a triangle up, a triangle down
or a dash, at plus or minus eight metres against the shop floor. Potrero Ave at
Mariposa is 14 m above this room and 682 m away; that is a materially worse
errand than Hubbell St, which is further and downhill. The shop's own height is
the nearest baked dock's, since the bake is dock locations and not a DEM, and
the payload says so.

**Walk minutes, not metres, and they are straight-line optimistic.** 75 m/min is
an ordinary adult pace, applied to the crow-flying distance, which in a grid
like Dogpatch is not far off and in general is a floor. The panel says WALK and
prints minutes because minutes are the unit somebody standing at the door thinks
in; the metres are in the record for anyone who wants them.

**Age is part of the data and this panel is the one where it bites hardest.**
The record's TTL is ten minutes. Past it the header says OLD and prints the age.
Past thirty minutes the counts are not drawn at all -- the map furniture, the
rings and the building stay, and the panel says so in words. A dock count is not
like a tide table: half an hour late on a Friday evening is not a stale reading
of a slow quantity, it is a confident and specific lie about which dock has two
bikes in it. No record at all gets the no-data card and the command that fixes
it.

**Nothing here touches the network.** `build()` calls `ftdata.load()`; `render()`
touches neither disk nor socket. Run the fetcher:

    $ python3 ftdata.py --loop 120 --due

**Frame cost.** The map, the list, the column and the header are rasterised once
in `build()` into a single frame. `render()` copies it, walks one dim ring
outward from the building, breathes the nearest bike's marker and toggles a
heartbeat pixel -- about nine numpy calls a frame on arrays no bigger than the
map pane. Measured over a full loop on this desktop: see the README.

Run:  python3 ftdata.py --once --only docks-nearby
      python3 docks.py --host 127.0.0.1
      python3 docks.py --radius 1500 --rows 8
      FT_DATA_CACHE=/tmp/empty python3 docks.py     # the no-data card
      python3 scripts/test-docks.py
"""

import math
import os
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

PRODUCT = "docks-nearby"

# Where the wall is, if the record does not say. The record normally does, and
# the fetcher's DOCKS_SITE is the authority; this is the fallback for a payload
# written by an older version. See ftdata's note on why this differs from the
# (37.7627, -122.3966) that adsb.py and quake.py carry: 273 m, immaterial at
# their scales and seven pixels at this one.
SITE_LAT, SITE_LON = 37.7624929274026, -122.39969356310202

# Metres a minute on foot, if the record does not say. See ftdata.
WALK_M_PER_MIN = 75.0

# Past this many TTLs the counts stop being drawn. Same call quake.py and
# propagation.py make and for a sharper reason: three TTLs here is half an hour,
# and half an hour is long enough for every number on this panel to be wrong in
# the direction that sends somebody on a walk for nothing.
STALE_MULTIPLE = 3.0

# Metres of climb before the grade mark stops being a dash. Eight is about two
# storeys, which is where a walk starts to feel like one.
GRADE_M = 8.0

# --------------------------------------------------------------------------
# Colour. Two ideas carry nearly all of it: green-and-amber is a bike you can
# take, blue is a space you can leave one in, and red is neither.
#
# The map is nearly black because the marks are the picture. There is no
# basemap under it -- no streets, no shoreline -- and that is deliberate: at
# 39 m to the pixel a street grid is a grid of lit pixels edge to edge and every
# one of them competes with a dock. The rings and the cross give the eye enough
# to locate itself, which is all a map this small can honestly offer.
# --------------------------------------------------------------------------

C_BG = (3, 5, 7)
C_RULE = (24, 32, 40)
C_RING = (20, 27, 34)
C_SITE = (60, 225, 160)                 # the building, and only the building
C_DOCK = (116, 130, 144)                # the dock itself: where it is
C_BIKE = (70, 205, 120)                 # a classic bike, there to be taken
C_EBIKE = (255, 176, 40)                # an electric one
C_LOOSE = (226, 140, 54)                # a free-floating ebike, no dock
C_FREE = (58, 140, 228)                 # an empty dock, there to be filled
C_NONE = (150, 48, 40)                  # nothing to take, or nowhere to leave
C_SHUT = (58, 58, 62)                   # station not renting, on the map
C_SHUT_TXT = (128, 124, 120)            # ...and the word for it, which has to read

C_TEXT = (200, 214, 226)
C_DIM = (98, 112, 126)
C_LABEL = (72, 90, 110)
C_WARN = (255, 168, 40)
C_ALERT = (255, 62, 46)

C_UP = (188, 126, 74)                   # the walk is uphill
C_DOWN = (86, 150, 178)                 # the walk is downhill
C_FLAT = (74, 84, 94)


def scale_colour(rgb, k):
    return (int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k))


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 table, the same one quake, propagation and tide use --
# five rows a glyph, each row an octal digit whose three bits are its columns.
# A real typeface is mush at five pixels and the Pi does not have the same faces
# installed as the machine this was written on, so a baked font is the only one
# that is certainly there. The table already carries "/", ".", "-" and ":",
# which is every non-letter this panel needs.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)

_GLYPHS = {}
for _ch, _rows in _FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((5 * scale, 1), bool)
    out = np.zeros((5, len(s) * 4 - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def text_width(s, scale=1):
    return max(1, (len(str(s)) * 4 - 1) * scale)


def blit_text(dst, y, x, s, rgb, scale=1):
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn.

    Clipped rather than asserted: this panel is laid out for 320x64 and has to
    survive being asked for something else, and a demo that raises on a narrow
    canvas takes the whole rotation down with it.
    """
    m = text_mask(s, scale)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    dst[y0:y1, x0:x1][sub] = rgb
    return gw


def blit_right(dst, y, x_right, s, rgb, scale=1):
    """Same, right-aligned so a column of numbers lines up on its units."""
    return blit_text(dst, y, x_right - text_width(s, scale) + 1, s, rgb, scale)


def fit(s, width, scale=1):
    """Trim a string until it fits. What falls off the end is the least of it."""
    s = str(s)
    while s and text_width(s, scale) > width:
        s = s[:-1]
    return s


# --------------------------------------------------------------------------
# Names. Bay Wheels station names are written for a phone screen and this panel
# has 23 characters of 3x5 type to put one in.
#
# The transformation is the one a person makes out loud: "Rhode Island St at
# 17th St" is "Rhode Island and Seventeenth", never its full legal name, so the
# street types go and the junction becomes a slash. What is left is both shorter
# and closer to how somebody in the room would say it. Names that are not
# junctions -- "Jackson Playground", "Esprit Park" -- are already what people
# call them and are left exactly alone.
# --------------------------------------------------------------------------

# Trailing tokens that are noise on a junction name. Not "PARK", "PLAZA",
# "SQUARE", "PLAYGROUND" or "STATION": those are the name.
STREET_TYPES = frozenset((
    "ST", "STREET", "AVE", "AVENUE", "BLVD", "BOULEVARD", "RD", "ROAD",
    "DR", "DRIVE", "WAY", "LN", "LANE", "CT", "COURT", "TER", "TERRACE",
    "PL", "HWY", "HIGHWAY", "ALY", "ALLEY",
))

# Everything the 3x5 table can draw. Anything else in a name becomes a space
# rather than a blank glyph-shaped hole; a name with an accent or an ampersand
# in it should read as a slightly odd name, not as a rendering fault.
DRAWABLE = frozenset(_FONT)


def short_name(name):
    """'Rhode Island St at 17th St' -> 'RHODE ISLAND/17TH'."""
    s = str(name or "").upper().replace("&", " AT ")
    s = "".join(ch if ch in DRAWABLE else " " for ch in s)
    parts = [p.strip() for p in s.split(" AT ")]
    out = []
    for p in parts:
        words = p.split()
        # Only ever the last word, and never the only word: "ST AT MARY" would
        # otherwise lose the half of it that is a name.
        if len(words) > 1 and words[-1] in STREET_TYPES:
            words = words[:-1]
        if words:
            out.append(" ".join(words))
    return "/".join(out) if out else "DOCK"


def walk_minutes(dist_m, per_min):
    return int(round(float(dist_m) / max(1.0, float(per_min))))


def mins(n):
    """Walk minutes as text. Zero minutes is 'HERE', not '0M'."""
    return "HERE" if n <= 0 else "%dM" % n


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises. Everything that can
# still be wrong afterwards is wrong about *content* and is caught here. The
# three states are genuinely different: absent means no file, stale means a file
# too old to believe, and a record whose arrays disagree in length is a fourth
# thing that has to be refused rather than indexed into.
# --------------------------------------------------------------------------

COLUMNS = ("name", "dist_m", "lat", "lon", "bikes", "ebikes", "free_docks",
           "capacity", "open")


def record_stamp(cache_dir=None, product=PRODUCT):
    """(mtime, size) of the cache record, or None. A cheap 'has it changed?'."""
    try:
        path = ftdata.record_path(product, cache_dir)
        if path is None:
            return None
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


class Station(object):
    """One dock, with everything the panel needs about it precomputed."""

    __slots__ = ("name", "short", "dist", "walk", "lat", "lon", "bikes",
                 "ebikes", "classic", "free", "cap", "open", "grade")

    def __init__(self, rec, i, per_min, site_elev):
        self.name = str(rec["name"][i])
        self.short = short_name(self.name)
        self.dist = float(rec["dist_m"][i])
        self.walk = walk_minutes(self.dist, per_min)
        self.lat = float(rec["lat"][i])
        self.lon = float(rec["lon"][i])
        self.bikes = int(rec["bikes"][i] or 0)
        self.ebikes = min(self.bikes, int(rec["ebikes"][i] or 0))
        self.classic = self.bikes - self.ebikes
        self.free = int(rec["free_docks"][i] or 0)
        self.cap = int(rec["capacity"][i] or 0)
        self.open = bool(rec["open"][i])
        elev = (rec.get("elev_m") or [None] * (i + 1))[i]
        self.grade = (None if elev is None or site_elev is None
                      else float(elev) - float(site_elev))

    @property
    def takeable(self):
        """Bikes somebody could actually rent. A shut station has none."""
        return self.bikes if self.open else 0

    @property
    def leavable(self):
        """Docks somebody could actually leave a bike in."""
        return self.free if self.open else 0


class Loose(object):
    """A free-floating bike: a position, a distance and nothing else."""

    __slots__ = ("dist", "walk", "lat", "lon", "elec")

    def __init__(self, d, la, lo, elec, per_min):
        self.dist = float(d)
        self.walk = walk_minutes(self.dist, per_min)
        self.lat, self.lon = float(la), float(lo)
        self.elec = bool(elec)


class Docks(object):
    """The docks near the wall, or a reason there are none."""

    def __init__(self, cache_dir=None, radius_m=1000.0, product=PRODUCT):
        self.problem = None
        self.age = None
        self.state = "absent"
        self.stations = []
        self.loose = []
        self.radius = float(radius_m)
        self.site = (SITE_LAT, SITE_LON)
        self.site_elev = None
        self.per_min = WALK_M_PER_MIN
        self.stored_radius = None
        self.as_of = None
        self.ttl = ftdata.ttl_for(product) or 600.0

        got = ftdata.load(product, cache_dir)
        if got is None:
            self.problem = "no cached dock record"
            return
        payload, self.age = got
        self.state = ("fresh" if self.age <= self.ttl else
                      "aging" if self.age <= self.ttl * STALE_MULTIPLE
                      else "stale")
        try:
            n = int(payload["n"])
            # Every column has to be as long as `n` or this is a record from a
            # version that stored something else, and indexing into it would
            # pair one station's name with another's counts -- which draws
            # perfectly and is completely wrong.
            for k in COLUMNS:
                if not isinstance(payload.get(k), list) or len(payload[k]) < n:
                    raise ValueError("column %s is short" % k)
            site = payload.get("site") or list(self.site)
            self.site = (float(site[0]), float(site[1]))
            self.site_elev = payload.get("site_elev_m")
            self.per_min = float(payload.get("walk_m_per_min")
                                 or WALK_M_PER_MIN)
            self.stored_radius = float(payload.get("radius_m") or 0.0)
            self.as_of = payload.get("as_of")
            for i in range(n):
                st = Station(payload, i, self.per_min, self.site_elev)
                if st.dist <= self.radius:
                    self.stations.append(st)
            lo = payload.get("loose") or {}
            for j in range(int(lo.get("n") or 0)):
                b = Loose(lo["dist_m"][j], lo["lat"][j], lo["lon"][j],
                          (lo.get("elec") or [1] * (j + 1))[j], self.per_min)
                if b.dist <= self.radius:
                    self.loose.append(b)
        except Exception:                                    # noqa: BLE001
            self.problem = "dock record is malformed"
            self.state = "absent"
            self.stations, self.loose = [], []
            return
        if not self.stations:
            # A radius with nothing in it is a legitimate answer, but it is not
            # one this panel can draw as a map, and it is far more likely to be
            # somebody's --radius typo than a real bikeless kilometre.
            self.problem = "no docks within %.0f m" % self.radius
            self.state = "absent"
            return
        if self.state == "stale":
            # Not "dim it": drop it. Half-hour-old dock counts drawn as if they
            # were now is the one lie this panel could tell that would send
            # somebody on a walk for nothing.
            self.problem = ("counts are %s old"
                            % ftdata.describe_age(self.age))

    @property
    def usable(self):
        return self.state in ("fresh", "aging")

    @property
    def drawable(self):
        """Are there counts worth putting numbers on? See the stale note."""
        return self.usable and bool(self.stations)

    def nearest_bike(self):
        """(walk minutes, label, kind), over docks and loose bikes together.

        Both fleets, because a loose ebike lying against a wall three hundred
        metres away is a better answer than a dock four hundred metres away and
        the panel would be wrong to prefer the dock for being a dock. Returns
        None if there is nothing at all to ride.
        """
        best = None
        for st in self.stations:
            if st.takeable > 0:
                cand = (st.dist, st.short, "ebike" if st.ebikes == st.bikes
                        else "dock", st)
                if best is None or cand[0] < best[0]:
                    best = cand
        for b in self.loose:
            if best is None or b.dist < best[0]:
                best = (b.dist, "ON THE STREET",
                        "loose" if b.elec else "loose-classic", b)
        return best

    def nearest_ebike(self):
        best = None
        for st in self.stations:
            if st.open and st.ebikes > 0 and (best is None or st.dist < best[0]):
                best = (st.dist, st.short, "dock", st)
        for b in self.loose:
            if b.elec and (best is None or b.dist < best[0]):
                best = (b.dist, "ON THE STREET", "loose", b)
        return best

    def nearest_free(self):
        for st in self.stations:
            if st.leavable > 0:
                return (st.dist, st.short, "dock", st)
        return None

    def totals(self):
        bikes = sum(s.takeable for s in self.stations)
        ebikes = sum(s.ebikes for s in self.stations if s.open)
        free = sum(s.leavable for s in self.stations)
        return len(self.stations), bikes, ebikes, free, len(self.loose)


# --------------------------------------------------------------------------
# Projection. One tile, metres on both axes, the wall in the middle.
#
# Equirectangular with the longitude scaled by cos(latitude), which over a
# kilometre and a half is exact to well under a pixel. The scale is metres per
# pixel and it is the *same number* on both axes -- unlike quake.py's two tiles,
# which are squashed by whatever their extent and their pixels work out to. A
# map this small has to be isotropic or the rings are not rings and the eye
# cannot read distance off it at all, and here it can be, because the pane is
# 58 x 57 and the region is a circle.
# --------------------------------------------------------------------------

class Projection(object):
    def __init__(self, w, h, site, radius_m, margin=1.10):
        self.w, self.h = w, h
        self.lat0, self.lon0 = site
        self.kx = math.cos(math.radians(self.lat0))
        # The radius plus a margin has to fit in the *shorter* axis, or a dock
        # on the edge of the radius falls off the top of the pane.
        self.m_per_px = 2.0 * radius_m * margin / max(1.0, min(w, h) - 1.0)
        self.cx = (w - 1) * 0.5
        self.cy = (h - 1) * 0.5

    def project(self, lat, lon):
        """(row, col) in pane-local pixels, as floats. May be off the pane."""
        dy = (float(lat) - self.lat0) * 111320.0
        dx = (float(lon) - self.lon0) * 111320.0 * self.kx
        return (self.cy - dy / self.m_per_px, self.cx + dx / self.m_per_px)

    def px(self, metres):
        return float(metres) / self.m_per_px


def draw_ring(dst, proj, metres, rgb):
    """A circle at a known distance from the building, in the pane's own scale."""
    r = proj.px(metres)
    if r < 2.0:
        return
    # One point per pixel of circumference and a bit, so the ring has no gaps
    # and is not drawn a hundred times over.
    n = max(24, int(r * 7.0))
    a = np.linspace(0.0, 2.0 * math.pi, n)
    rr = np.round(proj.cy - np.sin(a) * r).astype(int)
    cc = np.round(proj.cx + np.cos(a) * r).astype(int)
    ok = (rr >= 0) & (rr < dst.shape[0]) & (cc >= 0) & (cc < dst.shape[1])
    if ok.any():
        sub = dst[rr[ok], cc[ok]]
        dst[rr[ok], cc[ok]] = np.maximum(sub, np.array(rgb, np.uint8))


def bar_pixels(n):
    """How many pixels of needle a count of n deserves: 0, 1, 2 or 3.

    Three steps and not a linear scale, because the map is not where somebody
    reads a number off -- the list is, and it prints the number. What the map
    has to say is none / a couple / a handful / plenty, at a glance, from across
    a room, in three pixels.
    """
    if n <= 0:
        return 0
    if n <= 2:
        return 1
    if n <= 6:
        return 2
    return 3


def draw_station(dst, r, c, st):
    """One dock as a vertical bar chart. Up is take, down is leave.

    Max-blended so two docks that land on the same column brighten rather than
    the later one winning, which at 39 m to the pixel happens wherever the docks
    are dense and should read as density rather than as one dock.
    """
    h, w = dst.shape[:2]
    if not (0 <= r < h and 0 <= c < w):
        return

    def put(row, rgb):
        if 0 <= row < h:
            np.maximum(dst[row, c], np.array(rgb, np.uint8), out=dst[row, c])

    if not st.open:
        # Out of service. It is a dock and it is there, and it is no use to
        # anybody in either direction, so it gets the pixel and no needles.
        put(r, C_SHUT)
        return
    put(r, C_DOCK)

    up = bar_pixels(st.bikes)
    if up == 0:
        put(r - 1, C_NONE)
    else:
        # The ebike share, drawn at the top of the needle: a dock with only
        # electric bikes is all amber, a mixed one has an amber tip. Rounded up,
        # so a single ebike among ten is still visible -- it is the fact
        # somebody on this hill most wants and one pixel is what there is.
        amber = 0 if st.ebikes <= 0 else max(
            1, int(math.ceil(up * st.ebikes / float(max(1, st.bikes)))))
        for k in range(up):
            put(r - 1 - k, C_EBIKE if k >= up - amber else C_BIKE)

    down = bar_pixels(st.free)
    if down == 0:
        put(r + 1, C_NONE)                          # jammed: nowhere to leave
    else:
        for k in range(down):
            put(r + 1 + k, C_FREE)


def draw_map(dst, proj, doc, radius_m, furniture_only=False):
    """The map pane: rings, scale bar, docks, loose bikes, and the building."""
    dst[:] = C_BG
    for metres in (5.0 * doc.per_min, 10.0 * doc.per_min):
        if metres <= radius_m * 1.05:
            draw_ring(dst, proj, metres, C_RING)

    # A bar of known length, because two panels of this project have been
    # photographed and cropped and a map with no scale on it is a decoration.
    bar = int(round(proj.px(500.0)))
    if 4 <= bar <= proj.w - 6 and proj.h >= 14:
        y = proj.h - 2
        dst[y, 2:2 + bar] = C_LABEL
        dst[y - 1:y + 1, 2] = C_LABEL
        dst[y - 1:y + 1, 2 + bar - 1] = C_LABEL
        blit_text(dst, y - 7, 2, "500M", C_LABEL)

    if not furniture_only:
        for b in doc.loose:
            r, c = proj.project(b.lat, b.lon)
            r, c = int(round(r)), int(round(c))
            if 0 <= r < proj.h and 0 <= c < proj.w:
                # No dock pixel under it: that absence is what says this is a
                # bike lying in the street and not a station.
                np.maximum(dst[r, c],
                           np.array(C_LOOSE if b.elec else C_BIKE, np.uint8),
                           out=dst[r, c])
        for st in doc.stations:
            r, c = proj.project(st.lat, st.lon)
            draw_station(dst, int(round(r)), int(round(c)), st)

    # The building last and brightest, a cross rather than a dot: a single lit
    # pixel among forty docks is another dock.
    r, c = int(round(proj.cy)), int(round(proj.cx))
    if 0 <= r < proj.h and 0 <= c < proj.w:
        dst[max(0, r - 2):r + 3, c] = C_SITE
        dst[r, max(0, c - 2):c + 3] = C_SITE


# --------------------------------------------------------------------------
# The list. Eight docks by name, which is the pane that makes the map sayable.
# --------------------------------------------------------------------------

def grade_mark(dst, y, x, grade):
    """A 3x3 triangle: up for a climb, down for a drop, a dash for neither.

    Drawn rather than typed because the 3x5 table has no arrows and a "+" and a
    "-" in front of a name read as arithmetic. Colour carries it too -- warm for
    up, cool for down -- so it survives being seen from an angle where three
    pixels do not resolve into a shape.
    """
    if grade is None:
        return
    h, w = dst.shape[:2]
    if y + 3 > h or x + 3 > w or y < 0 or x < 0:
        return
    if grade >= GRADE_M:
        rgb, rows = C_UP, ((1, 1, 0), (1, 1, 1), (1, 1, 1))
    elif grade <= -GRADE_M:
        rgb, rows = C_DOWN, ((1, 1, 1), (1, 1, 1), (1, 1, 0))
    else:
        rgb, rows = C_FLAT, ((0, 0, 0), (1, 1, 1), (0, 0, 0))
    for dy, row in enumerate(rows):
        for dx, on in enumerate(row):
            if on:
                dst[y + dy, x + dx] = rgb
    if grade >= GRADE_M:                # a caret: fill only the apex column
        dst[y, x] = C_BG
        dst[y, x + 2] = C_BG
        dst[y, x + 1] = rgb
    elif grade <= -GRADE_M:
        dst[y + 2, x] = C_BG
        dst[y + 2, x + 2] = C_BG
        dst[y + 2, x + 1] = rgb


class ListPane(object):
    """Column geometry for the named list, worked out once from its width."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.row_h = 6
        self.head_y = 0
        self.first_y = 7
        self.rows = max(0, (h - self.first_y) // self.row_h)
        self.walk_r = min(11, w - 1)                    # right edge of '10M'
        self.grade_x = 13
        self.name_x = 18
        # Three pixels of inset on the right, so the FREE column does not sit
        # against the hairline that separates this pane from the next one.
        self.r_free = w - 4
        self.r_e = max(self.name_x, w - 21)
        self.r_bike = max(self.name_x, w - 31)
        # The name stops short of the widest heading, not of the widest number:
        # 'BIKE' is four characters over a column that is usually two.
        self.name_w = max(8, self.r_bike - text_width("BIKE") - self.name_x - 2)


def draw_list(dst, pane, doc, highlight=None):
    """The nearest docks, one per row. `highlight` is the nearest bike's."""
    dst[:] = C_BG
    blit_text(dst, pane.head_y, 0, "MIN", C_LABEL)
    blit_text(dst, pane.head_y, pane.name_x, "DOCK", C_LABEL)
    # The heading is the legend. Three words in the three colours they label is
    # cheaper than a separate key and impossible to get out of step with it.
    blit_right(dst, pane.head_y, pane.r_bike, "BIKE", scale_colour(C_BIKE, 0.7))
    blit_right(dst, pane.head_y, pane.r_e, "E", scale_colour(C_EBIKE, 0.8))
    blit_right(dst, pane.head_y, pane.r_free, "FREE", scale_colour(C_FREE, 0.8))

    for i, st in enumerate(doc.stations[:pane.rows]):
        y = pane.first_y + i * pane.row_h
        hot = st is highlight
        blit_right(dst, y, pane.walk_r, mins(st.walk),
                   C_TEXT if hot else C_DIM)
        grade_mark(dst, y + 1, pane.grade_x, st.grade)
        name = fit(st.short, pane.name_w)
        blit_text(dst, y, pane.name_x, name, C_TEXT if hot else C_DIM)
        if not st.open:
            blit_right(dst, y, pane.r_free, "SHUT", C_SHUT_TXT)
            continue
        # Classic and electric as two numbers that add to the total, rather
        # than a total with a subset hidden inside it. Zero is drawn as a dash
        # and not as '0': a column of noughts is a wall of noise, and what the
        # eye wants from this column is where the bikes *are*.
        blit_right(dst, y, pane.r_bike,
                   "%d" % st.classic if st.classic else "-",
                   C_BIKE if st.classic else C_LABEL)
        blit_right(dst, y, pane.r_e, "%d" % st.ebikes if st.ebikes else "-",
                   C_EBIKE if st.ebikes else C_LABEL)
        blit_right(dst, y, pane.r_free, "%d" % st.free if st.free else "FULL",
                   C_FREE if st.free else C_NONE)


# --------------------------------------------------------------------------
# The column: the number somebody reads in two seconds, and three lines under
# it for the person who has stopped to read the whole thing.
# --------------------------------------------------------------------------

def draw_column(dst, doc, radius_m):
    w, h = dst.shape[1], dst.shape[0]
    dst[:] = C_BG
    y = 0

    best = doc.nearest_bike()
    if best is None:
        blit_text(dst, y, 0, fit("NO BIKES WITHIN", w), C_LABEL)
        blit_text(dst, y + 7, 0, fit("%d MIN WALK"
                                     % walk_minutes(radius_m, doc.per_min), w),
                  C_WARN)
        y = 20
    else:
        dist, label, kind, _obj = best
        blit_text(dst, y, 0, "NEAREST BIKE", C_LABEL)
        walk = walk_minutes(dist, doc.per_min)
        # The big number is minutes, not metres. Metres are a measurement;
        # minutes are the decision.
        txt = "%d" % walk if walk > 0 else "0"
        scale = 3
        while scale > 1 and text_width(txt, scale) > w - 30:
            scale -= 1
        big_w = blit_text(dst, 7, 0, txt, C_TEXT, scale)
        blit_text(dst, 7 + 5 * scale - 10, big_w + 4, "MIN", C_DIM, 2)
        colour = C_EBIKE if kind in ("ebike", "loose") else C_BIKE
        y = 7 + 5 * scale + 1
        blit_text(dst, y, 0, fit(label, w), colour)
        y += 7

    # The other two answers somebody wants, in the order they want them: an
    # ebike is a different proposition from a bike on this hill, and a free
    # dock is the whole question for anybody arriving with one.
    rows = []
    e = doc.nearest_ebike()
    if e is not None:
        rows.append(("EBIKE %s  %s" % (mins(walk_minutes(e[0], doc.per_min)),
                                       e[1]), C_EBIKE))
    else:
        rows.append(("NO EBIKE WITHIN REACH", C_LABEL))
    f = doc.nearest_free()
    if f is not None:
        rows.append(("FREE DOCK %s  %s"
                     % (mins(walk_minutes(f[0], doc.per_min)), f[1]), C_FREE))
    else:
        rows.append(("EVERY DOCK NEARBY IS FULL", C_NONE))

    # The totals live in the header, which is 320 pixels wide and otherwise
    # nearly empty; repeating them here would cost the two lines below, which
    # are the two facts the header has no room for.
    n, bikes, ebikes, free, loose = doc.totals()
    rows.append(("%d LOOSE EBIKES NEARBY" % loose if loose
                 else "NO LOOSE EBIKES NEARBY",
                 C_LOOSE if loose else C_LABEL))
    empty = sum(1 for s in doc.stations if s.open and s.bikes == 0)
    jam = sum(1 for s in doc.stations if s.open and s.free == 0)
    rows.append(("%d DOCKS EMPTY  %d FULL" % (empty, jam),
                 C_NONE if jam else C_DIM))

    for s, rgb in rows:
        if y + 5 > h:
            break
        blit_text(dst, y, 0, fit(s, w), rgb)
        y += 7


# --------------------------------------------------------------------------
# The header, and the no-data card.
# --------------------------------------------------------------------------

def header_text(doc, radius_m):
    """(left, middle, middle colour, right age)."""
    if doc.state == "absent":
        return ("BAY WHEELS", (doc.problem or "NO DOCK DATA").upper(),
                C_ALERT, ftdata.describe_age(doc.age) if doc.age else "")
    if doc.state == "stale":
        return ("BAY WHEELS",
                "COUNTS %s OLD -- NOT DRAWN" % ftdata.describe_age(doc.age),
                C_ALERT, ftdata.describe_age(doc.age))
    n, bikes, ebikes, free, loose = doc.totals()
    # BIKE means pedal and EBIKE means electric, here and in the list, and the
    # two are disjoint. GBFS reports the electric ones as a *subset* of
    # num_bikes_available; folding that subset back out is done once, in
    # Station, so that no two places on this panel can disagree about it.
    mid = "%d DOCKS  %d BIKE  %d EBIKE  %d FREE  IN %s WALK" % (
        n, bikes - ebikes, ebikes, free,
        mins(walk_minutes(radius_m, doc.per_min)))
    return "BAY WHEELS", mid, C_TEXT, ftdata.describe_age(doc.age)


def draw_header(dst, w, head_h, doc, radius_m):
    dst[:] = C_BG
    left, mid, midc, right = header_text(doc, radius_m)
    rw = 0
    if right:
        label = ("DATA " + right) if w >= 240 else right
        blit_text(dst, 0, w - text_width(label) - 1, label,
                  C_TEXT if doc.state == "fresh" else C_WARN)
        rw = text_width(label)
    if doc.state not in ("fresh", "absent"):
        # The word, not just an amber tint. A colour shift on a wall seen from
        # across a workshop is not a message.
        flag = "STALE" if doc.state == "stale" else "OLD"
        fw = text_width(flag)
        # Six pixels of gap, not three: at three the word and the age run
        # together into "OLDDATA 15M", which is how the first version of this
        # read on the wall.
        blit_text(dst, 0, w - rw - 6 - fw, flag, C_ALERT)
        rw += fw + 5
    lw = text_width(left) if left else 0
    if left:
        blit_text(dst, 0, 1, left, C_DIM)
    if mid:
        mid = fit(mid, w - lw - rw - 8)
        mx = max(lw + 4, min(w - rw - 3 - text_width(mid),
                             (w - text_width(mid)) // 2))
        blit_text(dst, 0, mx, mid, midc)
    dst[head_h] = C_ALERT if doc.state in ("absent", "stale") else C_RULE


def draw_nodata(frame, w, h, doc, cache_dir, lit=True):
    """Not a blank rectangle, and above all not a map with no docks on it."""
    frame[:] = (6, 6, 8)
    edge = C_ALERT if lit else scale_colour(C_ALERT, 0.25)
    frame[0], frame[-1] = edge, edge
    frame[:, 0], frame[:, -1] = edge, edge

    title = "NO DOCK DATA"
    scale = 3 if w >= 200 else 2
    while scale > 1 and text_width(title, scale) > w - 12:
        scale -= 1
    y = max(3, h // 2 - 5 * scale)
    blit_text(frame, y, (w - text_width(title, scale)) // 2, title,
              C_ALERT if lit else scale_colour(C_ALERT, 0.3), scale)
    y += 5 * scale + 4
    for s in ((doc.problem or "cache is empty").upper(),
              "RUN: PYTHON3 FTDATA.PY --LOOP 120 --DUE",
              (cache_dir or ftdata.CACHE_DIR).upper()):
        s = fit(s, w - 8)
        blit_text(frame, y, (w - text_width(s)) // 2, s, C_LABEL)
        y += 7
        if y + 5 > h - 2:
            break
    return frame


# --------------------------------------------------------------------------
# Options and layout.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--radius", type=float, default=1000.0,
                    help="metres of walking radius drawn; the record holds "
                         "1500 m, so this can be turned up on the wall "
                         "without the fetcher changing")
    ap.add_argument("--rows", type=int, default=0,
                    help="docks named in the list (0 = as many as fit)")
    ap.add_argument("--reload", type=float, default=120.0,
                    help="seconds between re-reads of the cache (0 = never, "
                         "which also makes render() exactly pure in t)")
    ap.add_argument("--pulse-hz", type=float, default=0.5,
                    help="rate the nearest bike breathes at; 0 holds it lit, "
                         "for a still photograph")
    ap.add_argument("--sweep", type=float, default=7.0,
                    help="seconds for one dim ring to walk out to the radius "
                         "(0 = no sweep)")
    ap.add_argument("--no-loose", action="store_true",
                    help="ignore the free-floating bikes and draw docks only")


class Layout(object):
    """Proportional with floors, so a canvas other than 320x64 gives something
    sane rather than an exception or a heap of overlapping type."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.head_h = 6 if h >= 40 else 5
        self.body_y = self.head_h + 1
        self.body_h = max(6, h - self.body_y)
        # The map wants to be square-ish or the rings stop being rings and the
        # scale bar starts lying, so it takes its width from the body height.
        self.map_w = int(min(w // 3, max(0, min(self.body_h + 1,
                                                int(round(w * 0.20))))))
        if self.map_w < 20:
            self.map_w = 0                          # too narrow to be a map
        self.col_w = int(min(150, max(52, round(w * 0.34))))
        self.map_x = 0
        self.list_x = self.map_w + (3 if self.map_w else 0)
        self.col_x = max(self.list_x, w - self.col_w)
        self.col_w = w - self.col_x
        self.list_w = max(0, self.col_x - 2 - self.list_x)


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h)
    cache = args.cache_dir
    radius = max(150.0, float(args.radius))
    pulse_hz = max(0.0, float(args.pulse_hz))
    sweep = max(0.0, float(args.sweep))

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)
    nodata = (np.zeros((h, w, 3), np.uint8), np.zeros((h, w, 3), np.uint8))
    proj = (Projection(lay.map_w, lay.body_h, (SITE_LAT, SITE_LON), radius)
            if lay.map_w else None)

    cell = {"doc": None, "loaded": -1e18, "stamp": None, "nodata": False,
            "pulse": None, "dist": None, "proj": proj}

    def bake(now):
        """Everything that changes only when the cache does. Once per reload."""
        doc = Docks(cache, radius)
        if args.no_loose:
            doc.loose = []
        cell["doc"] = doc
        cell["loaded"] = now
        cell["stamp"] = record_stamp(cache)
        cell["nodata"] = doc.state == "absent"
        cell["pulse"] = None
        cell["dist"] = None

        if cell["nodata"]:
            for lit, buf in zip((True, False), nodata):
                draw_nodata(buf, w, h, doc, cache, lit)
            return

        static[:] = C_BG
        draw_header(static[:lay.head_h + 1], w, lay.head_h, doc, radius)

        pr = None
        if lay.map_w:
            # The projection is rebuilt here because the record carries the
            # site, and a payload written after somebody moved the wall should
            # move the cross rather than being drawn against a stale constant.
            pr = Projection(lay.map_w, lay.body_h, doc.site, radius)
            cell["proj"] = pr
            pane = static[lay.body_y:, :lay.map_w]
            draw_map(pane, pr, doc, radius, furniture_only=not doc.drawable)
            # A distance field in metres over the map pane, so the sweep is a
            # circle on the ground rather than on the screen and costs three
            # numpy calls a frame instead of a trig table.
            yy = (np.arange(lay.body_h, dtype=f32) - pr.cy) * pr.m_per_px
            xx = (np.arange(lay.map_w, dtype=f32) - pr.cx) * pr.m_per_px
            cell["dist"] = np.hypot(yy[:, None], xx[None, :])

        if lay.list_w > 8:
            pane = ListPane(lay.list_w, lay.body_h)
            if args.rows > 0:
                pane.rows = min(pane.rows, int(args.rows))
            buf = np.zeros((lay.body_h, lay.list_w, 3), np.uint8)
            best = doc.nearest_bike() if doc.drawable else None
            hot = best[3] if (best and isinstance(best[3], Station)) else None
            if doc.drawable:
                draw_list(buf, pane, doc, hot)
            else:
                buf[:] = C_BG
                blit_text(buf, 2, 0, fit((doc.problem or "").upper(),
                                         lay.list_w), C_ALERT)
                blit_text(buf, 10, 0, fit("THE MAP IS GEOGRAPHY, NOT COUNTS",
                                          lay.list_w), C_LABEL)
                blit_text(buf, 18, 0,
                          fit("A DOCK COUNT THIS OLD IS NOT LATE.",
                              lay.list_w), C_DIM)
                blit_text(buf, 25, 0,
                          fit("IT IS WRONG ABOUT WHICH DOCK IS DRY.",
                              lay.list_w), C_DIM)
            static[lay.body_y:lay.body_y + lay.body_h,
                   lay.list_x:lay.list_x + lay.list_w] = buf

        if lay.col_w > 8:
            buf = np.zeros((lay.body_h, lay.col_w, 3), np.uint8)
            if doc.drawable:
                draw_column(buf, doc, radius)
            else:
                buf[:] = C_BG
                blit_text(buf, 2, 0, fit("COUNTS NOT DRAWN", lay.col_w),
                          C_ALERT)
                blit_text(buf, 10, 0, fit("RUN THE FETCHER", lay.col_w),
                          C_LABEL)
                blit_text(buf, 17, 0, fit("FTDATA.PY --LOOP 120 --DUE",
                                          lay.col_w), C_DIM)
            static[lay.body_y:lay.body_y + lay.body_h,
                   lay.col_x:lay.col_x + lay.col_w] = buf

        # The two hairlines between the three panes. Three abutting blocks read
        # as one picture without them, and a map that runs into a column of type
        # looks like a rendering fault.
        if lay.map_w and lay.map_w + 1 < w:
            static[lay.body_y:, lay.map_w + 1] = C_RULE
        if lay.col_x - 2 > 0:
            static[lay.body_y:, lay.col_x - 2] = C_RULE

        # Where the nearest bike is on the map, resolved once so render() only
        # has to change a colour.
        best = doc.nearest_bike() if (doc.drawable and pr is not None) else None
        if best is not None:
            obj = best[3]
            r, c = pr.project(obj.lat, obj.lon)
            r, c = int(round(r)), int(round(c))
            if 0 <= r < lay.body_h and 0 <= c < lay.map_w:
                loose = not isinstance(obj, Station)
                col = (C_LOOSE if loose else
                       C_EBIKE if obj.ebikes else C_BIKE)
                cell["pulse"] = (r + lay.body_y, c, col)

    def render(t, i=0):
        now = time.time()
        if args.reload and now - cell["loaded"] >= args.reload:
            # Stat before parse. Re-reading and rebaking is 10 kB of JSON, a map
            # and three panes of type; doing it on a file that has not changed
            # buys a visible hitch and nothing else. The fetcher writes by
            # rename, so a changed mtime is a changed record.
            stamp = record_stamp(cache)
            if stamp == cell["stamp"]:
                cell["loaded"] = now
            else:
                bake(now)

        lit = True if pulse_hz <= 0 else ((t * pulse_hz) % 1.0) < 0.55
        if cell["nodata"]:
            np.copyto(frame, nodata[0 if lit else 1])
            return frame

        np.copyto(frame, static)

        # One ring walking outward from the building, very dim. An instrument is
        # allowed to be still; what it must not be is *accidentally* still, and
        # this is the one motion that says something -- it is the radius being
        # paced out, which is what the numbers on the right are counting.
        if sweep > 0 and cell["dist"] is not None:
            k = (t / sweep) % 1.0
            r_m = k * radius
            m = np.abs(cell["dist"] - r_m) < cell["proj"].m_per_px
            if m.any():
                sub = frame[lay.body_y:lay.body_y + lay.body_h, :lay.map_w]
                col = np.array(scale_colour(C_SITE, 0.16 * (1.0 - k)), np.uint8)
                sub[m] = np.maximum(sub[m], col)

        if cell["pulse"] is not None and pulse_hz > 0:
            r, c, rgb = cell["pulse"]
            k = 0.4 + 0.6 * (0.5 + 0.5 * math.sin(t * pulse_hz * 2 * math.pi))
            frame[r, c] = scale_colour(rgb, k)

        # The heartbeat, in the one corner nothing else uses.
        frame[0, -1] = C_SITE if lit else C_RULE
        return frame

    bake(time.time())
    render.state = cell               # the tests reach in here; nothing else
    render.layout = lay
    render.static = static
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "Bay Wheels docks within a walk of the wall", fps=20)


if __name__ == "__main__":
    main()
