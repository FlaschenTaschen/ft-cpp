#!/usr/bin/env python3
"""Outside, right here: what was measured, what was modelled, kept apart.

This panel is about the weather at one street address rather than in one city,
which turns out to be a harder claim to make honestly than it sounds. The
default address is in the Mission in San Francisco -- somewhere real, so that a
checkout draws a real panel, in the same spirit as tide.py's default station --
and `--site`, plus `FT_WX_SITES` and `FT_WX_STATIONS` for the fetcher, move it.
At that address there is exactly one real instrument anywhere nearby --
SFOC1, "San Francisco Downtown", 2.8 km north-west -- and it reports
temperature, dewpoint and humidity and nothing else. No wind. No pressure. Not
"sometimes no wind": the fields are in the JSON and they are `null`, every
hour, with a `Z` quality flag. Everything else on this panel -- wind, pressure,
cloud, air quality -- is a **model's output for this address**, computed rather
than sensed. Every keyed alternative was tried and none of them are keyless any
more: Weather Underground PWS 401s, PurpleAir 403s, Synoptic 401s, AirNow 401s.

So the panel is a composite, and a composite that blended the two into one
authoritative-looking readout would be worse than no panel at all. Somebody who
reads the wall and then decides whether to roll the door up is entitled to know
which numbers came off a thermometer and which came out of a supercomputer in
Norway. **That distinction is the design**, and it is carried four ways at once,
because any one of them fails on somebody:

  1. *Position.* Measured things live left of the first hairline. Modelled
     things live right of it. Nothing crosses.
  2. *A word.* Each zone is headed OBSERVED or MODELLED, with the instrument
     and its distance, or the model and whose it is, beside it.
  3. *Colour.* Observed values are near-white; modelled values are blue. Two
     hues, not two brightnesses, so dimming a stale source cannot erase it.
  4. *A mark on every number.* A modelled value is printed `~5.4`, the way one
     writes an approximation by hand. Crop the panel, photograph it, read it
     colour-blind, and the tilde is still there.

The tilde is on the AQI too, which is the number most likely to be read off
this wall and acted on -- during fire season a woodworker deciding about the
roll-up door reads that and nothing else. It gets the biggest colour on the
panel, on the EPA's own scale, and it still says `~55`, because CAMS modelling
a 0.1-degree cell over the Mission is not the same thing as a sensor on the
roof. When one *is* on the roof, the tilde comes off by itself; see below.

**The temperatures are shown twice on purpose.** Observed 2.8 km away, modelled
here, and the difference between them printed underneath. In this city that
difference is not an error, it is the sea breeze: a panel that showed one
number would be hiding the most interesting thing it knows.

**Nothing here touches the network.** `build()` reads a disk cache written by
`ftdata.py` in another process, because the scheduler builds the next segment
on a worker thread sharing the GIL with the render loop -- a `build()` that
blocks on a socket stops the wall for everybody. See ftdata.py's docstring.
That file is also where met.no's terms are honoured: identifying User-Agent
with a contact address, Expires respected, conditional requests, no polling
faster than the model changes.

**Staleness, in propagation.py's three stages**, and with its vocabulary, since
this panel sits in the same rotation and a second vocabulary would be a second
thing to learn. Fresh: full brightness. Past TTL: half brightness, amber age,
AGING. Past three TTLs: the numbers are withdrawn -- `--`, never a plausible
zero -- and a red STALE flag blinks. The ages are the data's own, not the
fetch's: a met.no record revalidated with a 304 has a new `fetched_at` and
hour-old contents, and it is the contents that matter. An empty cache is a
NO DATA card naming the fetcher and its command.

**The distinction survives all of that**, which is the part worth testing: a
stale zone keeps its header, keeps its hue, and keeps every tilde. It has
nothing to say and says so in the right voice.

**It is all baked.** The layout, the type, the compass arrow and the AQI block
are rasterised once in `build()`; `render()` copies that frame and repaints two
small rectangles -- the stale flag and a heartbeat. A Pi 3 throttled to 600 MHz
re-lays no type at all here. Ten frames a second by default, because there are
exactly two distinct frames and the other twenty would be identical datagrams.

Run:  python3 ftdata.py --loop 900 &          # the fetcher, in its own process
      python3 wx.py --host 127.0.0.1
      python3 wx.py --station KSFO --lat 37.6188 --lon -122.3750 --site "SFO"
      FT_DATA_CACHE=/tmp/empty python3 wx.py  # the no-data card
"""

import math
import sys
import time

import numpy as np

import demoscene as ds
import ftdata
import propagation as pr

# --------------------------------------------------------------------------
# Type. propagation's 3x5 font -- itself defcon's -- plus the three glyphs a
# weather readout needs and a space weather panel does not. A baked font,
# because the Pi does not have the same faces installed as the machine this was
# written on and TrueType at five pixels is mush anyway.
#
# The tilde is not decoration here. It is the per-number provenance mark, so it
# has to be legible at one pixel of stroke and unmistakable for a minus sign,
# which is why it is two offset dashes rather than a curve.
# --------------------------------------------------------------------------

_FONT = dict(pr._FONT)
_FONT.update({
    "~": "06300", "%": "51245", "=": "07070",
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

    Clipped rather than asserted, for propagation's reason: this is laid out
    for 320x64 and still has to survive being asked for something else, and a
    demo that raises on a narrow canvas takes the whole rotation down with it.
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


fill = pr.fill
scale_colour = pr.scale_colour
num = pr.num


# --------------------------------------------------------------------------
# Provenance, which is the whole point.
#
# A product is observed or modelled, and the panel never guesses generously:
# anything it does not recognise is treated as modelled, because the failure
# that matters is claiming a measurement that nobody made. Adding a real sensor
# later means adding its prefix to OBSERVED_PREFIXES -- one line -- and the
# tilde, the hue and the header word all follow from it.
# --------------------------------------------------------------------------

OBSERVED, MODELLED = "observed", "modelled"

OBSERVED_PREFIXES = (
    "wx-obs-",          # an NWS station: a real instrument, some distance away
    "wx-local",         # the building's own sensor, over MQTT (see the README)
    "wx-pa-",           # a PurpleAir, if a key is ever obtained
)
MODELLED_PREFIXES = ("wx-model-", "wx-air-")


def provenance(name):
    for prefix in OBSERVED_PREFIXES:
        if name.startswith(prefix):
            return OBSERVED
    return MODELLED


# Observed is near-white and modelled is blue, and they are different *hues*
# rather than different brightnesses on purpose: half-brightness is already
# spoken for by the aging state, so a distinction made in brightness would be
# destroyed by a stale source -- which is precisely when it matters most.
OBS_INK = (233, 240, 250)
OBS_ACCENT = (110, 232, 168)
MODEL_INK = (128, 190, 240)
MODEL_ACCENT = (78, 158, 230)

LABEL = pr.LABEL
DIM = pr.DIM
WARN = pr.WARN
ALERT = pr.ALERT
GOOD = pr.GOOD

FRESH, AGING, STALE, ABSENT = pr.FRESH, pr.AGING, pr.STALE, pr.ABSENT

# The EPA's own AQI scale, in the EPA's own colours. Not invented and not
# adjusted for the panel: everybody in California has spent a fire season
# learning to read exactly these six, and a nicer green would only be a slower
# one to recognise.
AQI_SCALE = (
    (50, (0, 228, 0), "GOOD"),
    (100, (255, 255, 0), "MODERATE"),
    (150, (255, 126, 0), "UNHEALTHY SG"),
    (200, (255, 0, 0), "UNHEALTHY"),
    (300, (143, 63, 151), "VERY UNHEALTHY"),
    (10 ** 6, (126, 0, 35), "HAZARDOUS"),
)

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def aqi_bucket(aqi):
    for limit, colour, word in AQI_SCALE:
        if aqi <= limit:
            return colour, word
    return AQI_SCALE[-1][1], AQI_SCALE[-1][2]


def on_colour(colour):
    """Ink that reads on a filled chip: dark on the bright end, light on purple."""
    lum = 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]
    return (8, 10, 14) if lum > 120 else (248, 246, 252)


def compass_point(deg):
    return COMPASS[int((float(deg) / 22.5) + 0.5) % 16]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class Source(pr.Source):
    """A product off the cache, with its provenance and its *data's* age.

    propagation ages a record by when it was fetched, which is right for a feed
    that is only ever fetched. It is wrong here. met.no is deliberately not
    re-requested inside its Expires window and answers 304 outside it, so a
    record can be rewritten -- new `fetched_at`, same numbers -- without a
    single new number in it. Ageing by `payload["t"]`, the epoch the numbers
    actually describe, is the only version of this that cannot be gamed by the
    fetcher's own good manners. The worse of the two ages wins.
    """

    def __init__(self, name, cache_dir=None, now=None):
        pr.Source.__init__(self, name, cache_dir)
        self.kind = provenance(name)
        if self.payload is not None and isinstance(self.payload, dict):
            t = self.payload.get("t")
            if isinstance(t, (int, float)):
                data_age = max(0.0, (now or time.time()) - float(t))
                self.age = max(self.age or 0.0, data_age)
                self.state = (FRESH if self.age <= self.ttl else
                              AGING if self.age <= self.ttl * pr.STALE_MULTIPLE
                              else STALE)

    @property
    def observed(self):
        return self.kind == OBSERVED

    @property
    def mark(self):
        """The per-number provenance mark: nothing measured, a tilde modelled."""
        return "" if self.observed else "~"

    @property
    def accent(self):
        return OBS_ACCENT if self.observed else MODEL_ACCENT

    def ink(self, colour=None):
        base = colour or (OBS_INK if self.observed else MODEL_INK)
        return base if self.state == FRESH else scale_colour(base, 0.5)

    def value(self, path, fmt="%s", suffix=""):
        """A marked, formatted field: '~5.4' modelled, '5.4' measured, '--' absent.

        The mark goes on the number and not on the '--', because a dash is not
        a claim about anything and '~--' is just noise. When there is nothing to
        say, the header and the hue are what carry the distinction, and they are
        still there.
        """
        raw = self.get(*(path if isinstance(path, tuple) else (path,)))
        text = num(raw, fmt)
        if text == "--":
            return text
        return self.mark + text + suffix


def _line(buf, x0, y0, x1, y1, colour):
    """A one-pixel line, clipped. Enough for a compass arrow and no more."""
    n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    H, W = buf.shape[:2]
    for i in range(n):
        f = i / float(max(1, n - 1))
        x = int(round(x0 + (x1 - x0) * f))
        y = int(round(y0 + (y1 - y0) * f))
        if 0 <= x < W and 0 <= y < H:
            buf[y, x] = colour


def draw_wind_arrow(buf, cx, cy, r, from_deg, colour):
    """An arrow flying downwind, from a `from_deg` meteorological direction.

    Which way an arrow should point is a genuine ambiguity -- station plots use
    barbs pointing *into* the wind, weather apps use arrows pointing the way it
    blows -- so this one flies the way the air is going and the text beside it
    says FROM. Both halves are needed; either alone is read wrong by half the
    room.
    """
    to_rad = math.radians((float(from_deg) + 180.0) % 360.0)
    dx, dy = math.sin(to_rad), -math.cos(to_rad)
    hx, hy = cx + dx * r, cy + dy * r
    tx, ty = cx - dx * r, cy - dy * r
    _line(buf, tx, ty, hx, hy, colour)
    for side in (140.0, -140.0):
        a = to_rad + math.radians(side)
        _line(buf, hx, hy, hx + math.sin(a) * r * 0.55,
              hy - math.cos(a) * r * 0.55, colour)


# --------------------------------------------------------------------------
# Options. None of the site is hardcoded: the station, the coordinates and the
# label are all arguments, and so -- more importantly -- are the *product
# names*, which is the seam a roof sensor or a PurpleAir arrives through.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="where ftdata.py writes; defaults to $FT_DATA_CACHE "
                         "or ~/.cache/ftdata")
    ap.add_argument("--station", default=ftdata.WX_STATION,
                    help="NWS station id for the observed half")
    ap.add_argument("--lat", type=float, default=ftdata.WX_LAT,
                    help="site latitude, for the modelled half and the "
                         "distance to the station")
    ap.add_argument("--lon", type=float, default=ftdata.WX_LON)
    ap.add_argument("--site", default="MISSION DISTRICT",
                    help="what to call this place on the status line")
    ap.add_argument("--obs-product", default=None,
                    help="product for the observed half; defaults to "
                         "wx-obs-<station>. Point it at a roof sensor's "
                         "product and the panel measures its own building")
    ap.add_argument("--model-product", default=None,
                    help="product for the modelled half; defaults to "
                         "wx-model-<lat>_<lon>")
    ap.add_argument("--aqi-product", default=None,
                    help="product for the air quality tile; defaults to "
                         "wx-air-<lat>_<lon>. A wx-pa-<id> PurpleAir product "
                         "here is treated as observed, tilde and all")
    ap.add_argument("--blink-hz", type=float, default=1.0,
                    help="rate the stale flag and heartbeat blink at; 0 holds "
                         "them lit, for a still photograph")


# --------------------------------------------------------------------------
# The panel.
# --------------------------------------------------------------------------

def build(args):
    W, H = args.width, args.height
    out = np.zeros((H, W, 3), np.uint8)
    base = np.zeros((H, W, 3), np.uint8)

    site = ftdata._wx_site(args.lat, args.lon)
    obs = Source(args.obs_product or ("wx-obs-" + args.station), args.cache_dir)
    model = Source(args.model_product or ("wx-model-" + site), args.cache_dir)
    air = Source(args.aqi_product or ("wx-air-" + site), args.cache_dir)
    sources = (obs, model, air)

    blink_hz = max(0.0, float(args.blink_hz))
    patches = []                 # (y0, y1, x0, x1, lit_image, dark_image)

    if all(s.state == ABSENT for s in sources):
        _draw_no_data(base, W, H, args, patches, blink_hz)
        return _make_render(out, base, patches, blink_hz)

    status_h = 8 if H >= 44 else 6
    main_y1 = max(12, H - status_h - 1)

    # Three zones: measured, modelled, and the one number people cross the room
    # for. The air quality tile is sized first because it is the only thing here
    # with a minimum useful size -- below about 46 columns the big number stops
    # being a big number -- and the other two share what is left.
    gap = 4
    aqi_w = int(min(88, max(0, round(W * 0.235))))
    if aqi_w < 46 or W < 150:
        # Below this the big number stops being a big number. The tile is
        # dropped whole and its columns given back, rather than drawn small --
        # a shrunken AQI block is a colour nobody notices, which is worse than
        # no block at all. Decided here so the space is never merely reserved.
        aqi_w = 0
    aqi_x = W - 1 - aqi_w
    rest = aqi_x - (gap if aqi_w else 0) - 1
    obs_w = int(max(40, round(rest * 0.545)))
    model_w = rest - gap - obs_w
    if model_w < 40:                   # squeeze the observed side first
        obs_w = max(30, rest - gap - 40)
        model_w = rest - gap - obs_w
    obs_x, model_x = 1, 1 + obs_w + gap

    for zx in (model_x, aqi_x if aqi_w else None):
        if zx:
            fill(base, zx - gap // 2 - 1, 0, zx - gap // 2, main_y1, DIM)

    _draw_observed(base, obs_x, 0, obs_w, main_y1, obs, args)
    if model_w >= 30:
        _draw_modelled(base, model_x, 0, model_w, main_y1, model, obs)
    if aqi_w >= 46:
        _draw_air(base, aqi_x, 0, aqi_w, main_y1, air)

    _draw_status(base, 0, H - status_h, W, status_h, sources, args,
                 patches, blink_hz)
    return _make_render(out, base, patches, blink_hz)


def _make_render(out, base, patches, blink_hz):
    """One uint8 full-frame copy, then a few dozen pixels of blinking.

    Everything above ran once. render() is a pure function of t -- nothing
    carries between frames -- so the preview baker and the wall's drifting loop
    land in the same place at the same t.
    """
    def render(t, frame=0):
        np.copyto(out, base)
        lit = True if blink_hz <= 0 else ((t * blink_hz) % 1.0) < 0.55
        for y0, y1, x0, x1, on_img, off_img in patches:
            out[y0:y1, x0:x1] = on_img if lit else off_img
        return out
    return render


def source_label(src, default):
    """What to call a source in a zone header, with its provenance mark.

    The label comes out of the payload when the product supplies one, which is
    what lets a product this demo has never heard of -- a roof sensor over
    MQTT, a PurpleAir -- name itself on the wall instead of arriving as a
    filename. The mark is the panel's own doing and is not negotiable.
    """
    payload = src.payload if isinstance(src.payload, dict) else {}
    label = payload.get("label") or payload.get("station") or default
    return src.mark + str(label).upper()


def _zone_head(base, x, y, w, title, right, src):
    """A zone's title, its source, and the rule that fences it off.

    Everything here is derived from the source rather than from where the zone
    happens to sit: the title word, the hue and the mark all come from the
    product's provenance. Point --obs-product at a model by mistake and this
    zone will say MODELLED in blue, which is the failure mode you want.

    Drawn whatever the state of the data, because it is what says which half of
    the panel you are looking at. A zone with nothing to show still has to say
    what it would have shown, and in what voice.
    """
    stamp(base, x, y, title, src.accent)
    if right and text_w(title) + 4 + text_w(right) <= w:
        stamp_right(base, x + w, y, right, LABEL)
    rule = src.accent if src.state == FRESH else scale_colour(src.accent, 0.45)
    fill(base, x, y + 6, x + w, y + 7, scale_colour(rule, 0.5))
    return y + 9


def zone_title(src):
    return "OBSERVED" if src.observed else "MODELLED"


def _rows(top, bottom, n, pitch=8):
    """Row baselines that fit, from the top down. Short panels lose the last."""
    return [top + i * pitch for i in range(n) if top + i * pitch + 5 <= bottom]


def _draw_observed(base, x, y, w, h, obs, args):
    """The measured half: temperature, dewpoint, humidity, and what is missing.

    The absent wind gets a line of its own rather than being left out. A reader
    who does not find wind here would reasonably assume it is somewhere else on
    the panel and that it is the same kind of number as the temperature beside
    it; saying NO WIND AT THIS STATION is what stops the modelled wind two
    columns over from being quietly adopted as an observation.
    """
    dist = None
    lat, lon = obs.get("lat"), obs.get("lon")
    if lat is not None and lon is not None:
        dist = haversine_km(args.lat, args.lon, float(lat), float(lon))
    right = source_label(obs, obs.name.replace("wx-obs-", ""))
    if dist is not None:
        # How far away the instrument is, which is the single most important
        # caption on the panel: 2.8 km of San Francisco is several microclimates
        # and the reader is entitled to weigh the number accordingly.
        right += " %.1fKM" % dist
    content_y = _zone_head(base, x, y, w, zone_title(obs), right, obs)

    temp = obs.value("temp_c", "%.1f", "C")
    scale = 3
    while scale > 1 and text_w(temp, scale) > w:
        scale -= 1
    stamp(base, x, content_y, temp, obs.ink(), scale)

    side_x = x + text_w(temp, scale) + 5
    if side_x + text_w("DEW 00.0C") <= x + w:
        stamp(base, side_x, content_y + 1, "DEW", LABEL)
        stamp(base, side_x + text_w("DEW") + 3, content_y + 1,
              obs.value("dewpoint_c", "%.1f", "C"), obs.ink())
        stamp(base, side_x, content_y + 9, "RH", LABEL)
        stamp(base, side_x + text_w("RH") + 3, content_y + 9,
              obs.value("rh_pct", "%.0f", "%"), obs.ink())

    rows = _rows(content_y + 5 * scale + 3, y + h, 3)
    lines = []
    if obs.state == ABSENT:
        lines.append(("NO OBSERVATION CACHED", scale_colour(ALERT, 0.85)))
    elif obs.state == STALE:
        lines.append(("OBSERVATION TOO OLD", scale_colour(ALERT, 0.85)))
    else:
        # Per-field absence, not per-station: the same station reports wind on
        # some hours and not others, and another station reports it always.
        if obs.get("wind_ms") is None and obs.get("wind_dir") is None:
            lines.append(("NO WIND AT THIS STATION", DIM))
        else:
            speed = obs.value("wind_ms", "%.1f")
            direction = obs.get("wind_dir")
            word = compass_point(direction) if direction is not None else ""
            lines.append(("WIND %s M/S %s" % (speed, word), obs.ink()))
        stamp_t = obs.payload.get("t") if isinstance(obs.payload, dict) else None
        if stamp_t:
            lines.append(("READ " + time.strftime("%H:%M", time.localtime(stamp_t)),
                          LABEL))
        name = obs.payload.get("name") if isinstance(obs.payload, dict) else None
        if name:
            lines.append((str(name), scale_colour(LABEL, 0.8)))

    for (text, colour), ry in zip(lines, rows):
        while text and text_w(text) > w:
            text = text[:-1]
        stamp(base, x, ry, text, colour)


def _draw_modelled(base, x, y, w, h, model, obs):
    """The computed half: wind first, because it is the reason this zone exists.

    Wind is the one quantity nobody within 12 km of here measures and the one
    that decides what the afternoon feels like, so it gets the arrow and the big
    type. Underneath, the modelled temperature sits next to its difference from
    the observed one -- which in this city is the sea breeze made into a number,
    and is information rather than an inconsistency to be hidden.
    """
    content_y = _zone_head(base, x, y, w, zone_title(model),
                           source_label(model, "MET.NO"), model)

    speed = model.value("wind_ms", "%.1f")
    direction = model.get("wind_dir")
    scale = 3
    r = 6 if h - content_y >= 14 else 4
    arrow_w = 2 * r + 3
    while scale > 1 and arrow_w + text_w(speed, scale) + 16 > w:
        scale -= 1

    if direction is not None:
        draw_wind_arrow(base, x + r, content_y + r, r, direction,
                        model.ink(MODEL_ACCENT))
    else:
        fill(base, x + r - 1, content_y + r - 1, x + r + 1, content_y + r + 1, DIM)

    wx0 = x + arrow_w
    stamp(base, wx0, content_y, speed, model.ink(), scale)
    tx = wx0 + text_w(speed, scale) + 4
    if tx + text_w("M/S") <= x + w:
        stamp(base, tx, content_y + 1, "M/S", LABEL)
        if direction is not None:
            # FROM, in words, and the arrow flying the other way. The degrees
            # are cut: this column is 40 px wide, nobody reads a bearing off a
            # wall, and "FROM WSW" is the half of it that stops the arrow being
            # read backwards.
            point = compass_point(direction)
            # Never truncated, only dropped: cutting "FROM WSW" down to
            # "FROM W" does not shorten a label, it changes the direction by
            # two points and says so with a straight face.
            for word in ("FROM " + point, point, ""):
                if word and tx + text_w(word) <= x + w:
                    stamp(base, tx, content_y + 9, word, model.ink())
                    break

    rows = _rows(content_y + max(5 * scale, 2 * r) + 3, y + h, 3)
    cells = [("PRES", model.value("pressure_hpa", "%.0f", "HPA")),
             ("CLOUD", model.value("cloud_pct", "%.0f", "%")),
             ("TEMP", model.value("temp_c", "%.1f", "C"))]
    if model.state == ABSENT or model.state == STALE:
        msg = ("NO MODEL CACHED" if model.state == ABSENT
               else "MODEL RUN TOO OLD")
        if rows:
            stamp(base, x, rows[0], msg, scale_colour(ALERT, 0.85))
            rows = rows[1:]
            cells = cells[:len(rows)]

    for (label, text), ry in zip(cells, rows):
        stamp(base, x, ry, label, LABEL)
        stamp(base, x + text_w(label) + 3, ry, text, model.ink())
        # The gradient, printed where the two temperatures meet. Dim and
        # neutral: it belongs to neither source, being the difference of both.
        if label == "TEMP":
            mt, ot = model.get("temp_c"), obs.get("temp_c")
            if mt is not None and ot is not None:
                # +0.0 rather than -0.0: a signed zero is a rounding artefact
                # and reads as a real negative at three pixels of stroke.
                delta = "%+.1f VS OBS" % (round(float(ot) - float(mt), 1) + 0.0)
                dx = x + text_w(label) + 3 + text_w(text) + 4
                if dx + text_w(delta) <= x + w:
                    stamp(base, dx, ry, delta, scale_colour(LABEL, 0.9))


def _draw_air(base, x, y, w, h, air):
    """US AQI as a block of EPA colour, with the tilde still on the number.

    This is the most-read number on the panel and the colour does the work: a
    green block is a door you can open and a purple one is not, from across the
    shop, before any of the type is legible. Which is exactly why the provenance
    mark matters more here than anywhere else -- a bright confident block is the
    easiest thing on the wall to mistake for a measurement.
    """
    content_y = _zone_head(base, x, y, w, "AQI",
                           source_label(air, "OPEN-METEO"), air)

    aqi = air.get("us_aqi")
    if aqi is None:
        colour, word = DIM, ("AQI TOO OLD" if air.state == STALE
                             else "NO AQI DATA")
        text = "--"
    else:
        colour, word = aqi_bucket(float(aqi))
        text = air.mark + "%d" % int(aqi)
    if air.state == AGING:
        colour = scale_colour(colour, 0.5)

    block_h = max(9, min(26, (y + h - content_y) - 18))
    fill(base, x, content_y, x + w, content_y + block_h, colour)
    scale = 4
    while scale > 1 and (text_w(text, scale) > w - 2 or 5 * scale > block_h - 2):
        scale -= 1
    stamp(base, x + max(0, (w - text_w(text, scale)) // 2),
          content_y + max(0, (block_h - 5 * scale) // 2), text,
          on_colour(colour) if aqi is not None else scale_colour(ALERT, 0.9),
          scale)

    rows = _rows(content_y + block_h + 3, y + h, 2)
    if rows:
        label = word
        while label and text_w(label) > w:
            label = label[:-1]
        stamp(base, x, rows[0], label,
              colour if aqi is not None else scale_colour(ALERT, 0.85))
    if len(rows) > 1:
        stamp(base, x, rows[1], "PM2.5", LABEL)
        stamp(base, x + text_w("PM2.5") + 3, rows[1],
              air.value("pm2_5", "%.0f"), air.ink())


def _draw_status(base, x, y, w, h, sources, args, patches, blink_hz):
    """Where it is, what the tilde means, and how old every source is.

    The ages are not a footnote -- they are the only thing on the panel that
    says whether the rest of it is true -- so they are drawn first, right to
    left, and everything else is measured against where they stop. The legend
    is next to them rather than tucked in a corner because a mark nobody has
    been told the meaning of is just a smudge.
    """
    ty = y + (h - 5) // 2
    fill(base, x, y - 1, x + w, y, scale_colour(DIM, 0.6))

    rx = x + w - 2
    # Three different complaints, and they are not interchangeable: a product
    # nobody has ever fetched is MISSING, one whose numbers have been withdrawn
    # is STALE, one still worth reading with a caveat is AGING. Calling an
    # empty cache "stale" would suggest there is something behind it.
    absent_any = any(s.state == ABSENT for s in sources)
    stale_any = any(s.state == STALE for s in sources)
    aging_any = any(s.state == AGING for s in sources)
    flag = ("MISSING" if absent_any else "STALE" if stale_any
            else "AGING" if aging_any else None)
    # Right to left, and reversed, so that they end up left to right in the
    # same order as the zones above them: OBS, MOD, AIR.
    for src, short in reversed(list(zip(sources, ("OBS", "MOD", "AIR")))):
        if src.state == ABSENT:
            txt, colour = "-", scale_colour(ALERT, 0.8)
        else:
            txt = src.age_text()
            colour = (src.accent if src.state == FRESH
                      else WARN if src.state == AGING else ALERT)
        need = text_w(short) + 3 + text_w(txt) + 5
        if rx - need < x + 2:
            break
        rx -= text_w(txt)
        stamp(base, rx, ty, txt, colour)
        rx -= text_w(short) + 3
        stamp(base, rx, ty, short, scale_colour(src.accent, 0.65))
        rx -= 5

    limit = rx - (text_w(flag) + 5 if flag else 0)
    cx = x + 2
    site = str(args.site).upper()
    if cx + text_w(site) < limit:
        stamp(base, cx, ty, site, LABEL)
        cx += text_w(site) + 6
    legend = "~ = MODELLED"
    if cx + text_w(legend) < limit:
        stamp(base, cx, ty, legend, MODEL_ACCENT)

    if flag:
        colour = WARN if flag == "AGING" else ALERT
        fw = text_w(flag)
        fx = max(x + 2, rx - fw - 2)
        pr._blink_patch(base, fx - 1, ty - 1, fx + fw + 1, ty + 6, patches,
                        lambda buf: stamp(buf, 1, 1, flag, colour),
                        lambda buf: stamp(buf, 1, 1, flag,
                                          scale_colour(colour, 0.25)))

    # A heartbeat, bottom right, two rows clear of the type: a frozen render
    # loop and a calm evening look identical on a panel made of static type.
    hx, hy = x + w - 3, y + h - 2
    pr._blink_patch(base, hx, hy, hx + 2, hy + 2, patches,
                    lambda buf: buf.__setitem__(Ellipsis, GOOD),
                    lambda buf: buf.__setitem__(Ellipsis, (10, 30, 16)))


def _draw_no_data(base, W, H, args, patches, blink_hz):
    """Nothing cached at all: name the process that fills it and the command.

    A blank panel looks like a broken wall; a tidy 0.0 C with 0 % humidity looks
    like a cold, dry, still day, which is the exact lie this demo exists to not
    tell.
    """
    cache = args.cache_dir or ftdata.CACHE_DIR
    fill(base, 0, 0, W, 1, ALERT)
    fill(base, 0, H - 1, W, H, ALERT)
    fill(base, 0, 0, 1, H, ALERT)
    fill(base, W - 1, 0, W, H, ALERT)

    title = "NO DATA"
    scale = 4
    while scale > 1 and text_w(title, scale) > W - 12:
        scale -= 1
    ty = max(3, H // 2 - 5 * scale - 6)
    tx = (W - text_w(title, scale)) // 2
    pr._blink_patch(base, tx - 2, ty - 1, tx + text_w(title, scale) + 2,
                    ty + 5 * scale + 1, patches,
                    lambda buf: stamp(buf, 2, 1, title, ALERT, scale),
                    lambda buf: stamp(buf, 2, 1, title,
                                      scale_colour(ALERT, 0.25), scale))

    lines = ["WEATHER CACHE IS EMPTY",
             "RUN: PYTHON3 FTDATA.PY --LOOP 900",
             cache.upper()]
    yy = ty + 5 * scale + 5
    for line in lines:
        while line and text_w(line) > W - 6:
            line = line[:-1]
        stamp(base, (W - text_w(line)) // 2, yy, line, LABEL)
        yy += 7
        if yy + 5 > H - 2:
            break


def main():
    # 10 fps, for propagation's reason: there are exactly two distinct frames
    # here -- the blink is a square wave and nothing else moves -- so twenty of
    # every thirty frames would be 61 kB of UDP carrying no information.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=10)


if __name__ == "__main__":
    main()
