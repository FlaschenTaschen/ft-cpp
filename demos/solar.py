#!/usr/bin/env python3
"""Will our own website survive the night? The makerspace's solar server, as a day.

sequoia.garden is Sequoia Fabrica's website -- this makerspace's website, on a
machine in this building, on a 12 V battery behind a solar panel. Its front page
says so itself: *"This is a solar powered website. It may go offline!"* Nothing
else on this wall is ours in that way. `sfmix` and `bgp` are infrastructure we
happen to sit near; `caiso` is the whole state's grid seen from orbit. This is
one battery, one panel, one small computer, and it can actually lose.

**One column per five minutes, and that is the entire design.** The site
publishes `sparklines`: seven parallel arrays of 288 buckets, `bucket_ms`
300000, `window_ms` 86400000 -- exactly one day at five-minute resolution. The
panel is 320 columns wide. 288 of them are the day at native resolution, with no
resampling, no interpolation and no decimation, and the remaining 32 are the
readout. Every other choice in this file falls out of that one.

**x is time of day, not "the last 24 hours".** Those are the same 288 buckets
either way, but binning them by *local time of day* rather than by age nails
dawn and dusk to fixed columns -- midnight at the left edge, noon at column 144,
midnight again at the right -- so the lit band sits in the middle of the panel
every single day and the overnight trough is always the two dark ends. The cost
is that the columns to the right of the now-cursor are *yesterday's* tail
rather than empty space, and they are drawn at 60 % brightness to say so. That
is a better trade than an axis whose landmarks slide a column every five
minutes: somebody who walks past this wall twice a week should be looking at
the same picture, differently lit.

**The battery is not the interesting variable, and finding that out changed the
panel.** The bank sits at 99.7-100 % state of charge essentially all summer. A
"battery fills and drains" chart of that is a flat line. What is *not* flat is
the terminal voltage: it sags all night to about 13.24, lifts at first light,
spikes to the charge controller's absorb voltage -- 14.32 was observed at noon
in August -- and decays through the afternoon. So the **terrain is voltage**.
The hill in the middle of the panel is the charge controller doing its work,
and the long downhill slope on the right is the night. It is a landscape
because it genuinely is one.

**The sky is two different things at once, and the gap between them is the
story.** Its blue comes from the *computed* solar elevation for this latitude
and this date -- astronomy, what light was theoretically available. The warm
glow hugging the ridge comes from the *measured* charge current, which runs
5-15 mA in the dark and past 300 mA at solar noon. On a clear day the two agree
and there is a white-hot band over the hill at midday. On a foggy San Francisco
week the sky is bright blue and the ridge stays dark, and that difference --
"the sun was up, and we got nothing" -- is the case this whole panel exists to
be ready for.

**The three states, because the demo is about fragility.**

  * **Fresh and full** is serene, and it is most of the time. That is allowed
    to be the boring case; the panel earns its place by being pretty.
  * **Draining** tenses up: the ridge line goes amber then red, the ground
    turns from garden green to something dry, and the state of charge blinks
    once it is under the reserve mark drawn across the battery. `--soc 24`
    forces it, and stamps SIM on the panel so a screenshot of a simulation can
    never be mistaken for a screenshot of a bad week.
  * **Silent** is the funny one. If the record has aged past three TTLs -- an
    hour and a half of nobody answering -- the panel stops pretending, draws the
    last day it *did* see as a dim ghost behind the words NO ANSWER, and prints
    LAST SPOKE 3H AGO and IT DID SAY IT MIGHT. It does not claim the site is
    down, because from here a dead server and a dead fetcher look identical, and
    it is the site's own warning being quoted back at it either way. The site's
    separate `data_stale` flag -- the web server answering while the battery
    monitor behind it has gone quiet -- is a different failure and reads QUIET
    on a panel that still draws.

**Nothing here touches the network.** `build()` calls `ftdata.load()`, which
reads one JSON file. The fetcher is a separate process on a timer, at fifteen
minutes -- deliberately slower than the five-minute publication cadence, because
every request costs *that* battery a little radio and a little CPU, and three
extra columns at the right-hand edge are not worth it from three metres away.

**Frame budget.** Everything is baked in `build()`: the sky field, the terrain,
the stars, the battery glyph and every string. `render()` does one full-frame
copy, two ops for a sheen that only touches the ground, and writes a handful of
short columns. Seven or eight numpy calls, none of them scaling with anything a
knob controls.

Run:  python3 ftdata.py --once --only solar-garden
      python3 solar.py --host 127.0.0.1
      python3 solar.py --soc 24               # the foggy week, simulated
      python3 solar.py --off                  # the website is not answering
      python3 solar.py --quiet-sensor         # it answers, its monitor does not
      FT_DATA_CACHE=/tmp/empty python3 solar.py
      python3 scripts/test-solar.py
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

PRODUCT = "solar-garden"

# The day, in five-minute slots. 288 of them, which is what the endpoint
# publishes and what a 320 column panel has room for at 1:1.
SLOTS = 288
SLOT_S = 86400.0 / SLOTS                                # 300 seconds

# Past this multiple of the TTL the panel stops drawing a chart and starts
# quoting the site's own warning back at it. Three TTLs is ninety minutes: long
# enough that a single missed fetch pass, or a fetcher restart, does not put the
# joke on the wall, short enough that ninety minutes of silence is news.
SILENT_TTL_MULT = 3.0


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, tide, air and propagation
# draw with: five rows a glyph, each row an octal digit whose three bits are
# the three columns. Anything from a real typeface is mush at five pixels, and
# the Pi does not have the same faces installed as the machine this was written
# on. One glyph is added, for the only unit this panel prints.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT.update({"%": "51245"})

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
# Colour. Two palettes for the ground -- a watered one and a dry one -- because
# the one editorial move this panel makes is that a battery in trouble should
# look like a worse place, not like the same place with a red number on it.
# --------------------------------------------------------------------------

# Sky, as two vertical gradients that get mixed by how high the sun is. Both
# are darkest at the top: a real sky is, and more usefully it means the header
# text sits on the deepest colour on the panel at every hour of the day.
NIGHT_SKY = [(0.00, (4, 5, 12)), (0.65, (8, 11, 26)), (1.00, (14, 19, 42))]
DAY_SKY = [(0.00, (10, 28, 66)), (0.60, (34, 74, 122)), (1.00, (74, 122, 162))]

# The warm things that live near the horizon, both added rather than mixed, and
# both falling off exponentially with height above that column's own ridge.
C_TWILIGHT = (110, 48, 46)          # civil twilight, from the sun's elevation
C_CHARGE = (255, 186, 78)           # charge current, from the shunt

# Ground: green when the bank is healthy, dry when it is not. Index 0 is the
# ridge line's neighbour and the last index is the bottom of the panel.
GROUND_WET = [(0.00, (44, 92, 62)), (0.18, (20, 52, 40)),
              (0.55, (9, 26, 22)), (1.00, (4, 12, 12))]
GROUND_DRY = [(0.00, (96, 74, 34)), (0.18, (52, 36, 16)),
              (0.55, (24, 16, 8)), (1.00, (10, 7, 4))]

C_RIDGE_OK = (156, 232, 150)
C_RIDGE_WARN = (255, 190, 84)
C_RIDGE_LOW = (255, 96, 74)

C_STAR = (58, 64, 88)
C_GAP = (70, 24, 24)                # a five-minute slot the sensor never sent

C_TEXT = (176, 190, 208)
C_DIM = (84, 96, 114)
C_FAINT = (40, 46, 58)
C_NAME = (138, 186, 132)
C_WARN = (255, 118, 88)
C_SEP = (16, 20, 28)
C_NOW = (255, 246, 214)
C_NOW_HALO = (58, 56, 46)
C_BATT_CASE = (96, 108, 126)
C_RESERVE = (150, 60, 52)
C_SIM = (196, 118, 226)

# Where the health colour steps. Round numbers on purpose: this is a traffic
# light, and a traffic light whose boundaries move with the data is not one.
SOC_WARN, SOC_LOW = 55.0, 30.0

# The mark drawn across the battery. Not a measured cutoff -- the site does not
# publish one -- but the level at which a 12 V lead/LiFePO4 bank is understood
# to be in its reserve, and having *a* line there is what makes the empty space
# above the fill mean something.
SOC_RESERVE = 20.0

# How far above a column's ridge the warm light reaches, in rows. Eight is
# about an eighth of the panel: enough to read as a glow rather than as a
# coloured line, short enough that it never reaches the header.
GLOW_ROWS = 8.0

PULSE_HZ = 1.3


def health(soc):
    """(ridge colour, ground stops) for a state of charge in percent."""
    if soc is None:
        return C_RIDGE_WARN, GROUND_WET
    if soc >= SOC_WARN:
        return C_RIDGE_OK, GROUND_WET
    if soc >= SOC_LOW:
        return C_RIDGE_WARN, GROUND_WET
    return C_RIDGE_LOW, GROUND_DRY


# --------------------------------------------------------------------------
# Clock and sun. Same shape as caiso.py's: everything asks for `now` rather
# than reading the system clock, which is what makes a contact sheet across a
# whole day possible.
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


def clock(at=None, rate=1.0):
    """A callable returning the demo's present moment in epoch seconds."""
    base = time.time() if at is None else float(at)
    start = time.monotonic()
    if at is None and rate == 1.0:
        return time.time
    return lambda: base + (time.monotonic() - start) * rate


def slot_of(epoch):
    """Which five-minute slot of the *local* day an epoch falls in.

    Via localtime() rather than by dividing an offset, so the two days a year
    that are not 86400 seconds long put their samples under the wall clock time
    they actually happened at, which is what the axis claims to be.
    """
    lt = time.localtime(epoch)
    return int((lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) // SLOT_S)


def local_midnight(epoch):
    lt = time.localtime(epoch)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                        0, 0, 0, 0, 0, -1))


def solar_elevation(epoch, lat, lon):
    """The sun's altitude in degrees. Low-precision USNO series, ~0.01 deg.

    Lifted from goes.py, which uses it for the same reason: the day/night call
    has to come from the sky rather than from thresholding the data, or a foggy
    noon with no charge current would be drawn as midnight. `lon` is east
    positive, so San Francisco is negative.
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


def hhmm(epoch, ampm=True):
    lt = time.localtime(epoch)
    if not ampm:
        return time.strftime("%H:%M", lt)
    h = lt.tm_hour % 12 or 12
    return "%d:%02d%s" % (h, lt.tm_min, "A" if lt.tm_hour < 12 else "P")


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises, so everything that
# can still be wrong is wrong about *content* and is caught here.
# --------------------------------------------------------------------------

def _fseries(values, n):
    """A list that may contain nulls, as a float array with NaN for the gaps."""
    out = np.full(n, np.nan, f32)
    for i, v in enumerate(values[:n]):
        if v is None:
            continue
        try:
            out[i] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def read_garden(cache_dir):
    """(record, age, problem). `record` is None if there is nothing drawable."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached sequoia.garden record"
    payload, age = got
    try:
        n = int(payload["n"])
        t0 = float(payload["t0"])
        step = float(payload["step"])
        volt = _fseries(payload["volt"], n)
        cur = _fseries(payload["cur_ma"], n)
        soc = _fseries(payload["soc"], n)
    except Exception:                                        # noqa: BLE001
        return None, age, "sequoia.garden record is malformed"
    if n < 12 or step <= 0:
        return None, age, "sequoia.garden record has no usable series"
    if not np.isfinite(volt).any():
        return None, age, "sequoia.garden record has no voltages in it"

    def num(key):
        try:
            v = payload.get(key)
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    return {
        "n": n, "t0": t0, "step": step,
        "volt": volt, "cur": cur, "soc": soc,
        "soc_pct": num("soc_pct"), "status": str(payload.get("status") or ""),
        "v": num("v"), "i_ma": num("i_ma"),
        "cpu_c": num("cpu_c"), "cpu_load": num("cpu_load"),
        "sensor_stale": bool(payload.get("sensor_stale")),
        "sensor_age_s": num("sensor_age_s"),
        "uptime": str(payload.get("uptime") or ""),
        "up_days": payload.get("up_days"),
        "avg_volt": num("avg_volt"), "avg_cur_ma": num("avg_cur_ma"),
        "site": str(payload.get("site") or "sequoia.garden"),
        "age": age, "latest": t0 + (n - 1) * step,
    }, age, None


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--soc", type=float, default=-1.0,
                    help="pretend the state of charge is this percent, keeping "
                         "the day's own shape (-1 = use the real one)")
    ap.add_argument("--off", action="store_true",
                    help="pretend nobody has answered for hours")
    ap.add_argument("--quiet-sensor", action="store_true",
                    help="pretend the site answered but its monitor has not")
    ap.add_argument("--reveal", type=float, default=2.4,
                    help="seconds the day takes to draw itself in (0 = off)")
    ap.add_argument("--sweep", type=float, default=7.0,
                    help="seconds between light sweeps across the ground "
                         "(0 = off)")
    ap.add_argument("--sweep-width", type=int, default=40,
                    help="columns the sweep is wide")
    ap.add_argument("--sweep-gain", type=float, default=0.30,
                    help="how far towards white the sweep lifts the ground")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second")
    ap.add_argument("--reload", type=float, default=600.0,
                    help="seconds between re-reads of the cache (0 = never)")
    ap.add_argument("--seed", type=int, default=20260812,
                    help="star field seed; the panel is otherwise deterministic")


# --------------------------------------------------------------------------
# Layout. The day gets every column it can, and what is left is the readout.
#
# On a 320 wide panel that is 288 and 31, which is the whole point: 288 columns
# is exactly one per five-minute bucket. Narrower panels fall back to a
# many-buckets-per-column mapping that still puts noon in the middle, and very
# narrow ones give the readout up entirely rather than shrink the day.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.gut_w = 31 if w >= 200 else 0
        self.day_w = w - self.gut_w - (1 if self.gut_w else 0)
        self.gut_x = w - self.gut_w
        self.head_h = 5 if h >= 40 else 0
        self.foot_y = h - 5 if h >= 46 else h
        # Where the ridge may run. The hill wants to be tall enough to read as
        # terrain and the sky wants enough rows above it to be a sky; these two
        # numbers are the whole vertical composition.
        self.ridge_hi = max(self.head_h + 6, int(h * 0.36))
        self.ridge_lo = min(h - 6, int(h * 0.80))
        if self.ridge_lo <= self.ridge_hi:
            self.ridge_lo = min(h - 2, self.ridge_hi + 2)


# --------------------------------------------------------------------------
# Baking the picture.
# --------------------------------------------------------------------------

def bin_by_slot(rec, now):
    """The record's samples, rebinned onto the 288 slots of the local day.

    The buckets are already five minutes wide and aligned to whole multiples of
    300 s epoch, and every US Pacific offset is a whole number of hours, so this
    is a relabelling and not a resampling: each sample lands in exactly one slot
    and no slot gets two -- except across a DST change, where the repeated hour
    legitimately overwrites itself and the newer sample wins, which is the one
    an axis labelled with wall clock time should show.

    Returns (volt, cur, soc, age_rank) with NaN in any slot nothing arrived
    for. `age_rank` is the sample's index in the record, which is what the
    today/yesterday split is decided on.
    """
    volt = np.full(SLOTS, np.nan, f32)
    cur = np.full(SLOTS, np.nan, f32)
    soc = np.full(SLOTS, np.nan, f32)
    rank = np.full(SLOTS, -1, np.int32)
    for i in range(rec["n"]):
        if not np.isfinite(rec["volt"][i]):
            continue
        s = slot_of(rec["t0"] + i * rec["step"])
        if 0 <= s < SLOTS:
            volt[s], cur[s], soc[s], rank[s] = \
                rec["volt"][i], rec["cur"][i], rec["soc"][i], i
    return volt, cur, soc, rank


def sky_field(lay, elev, glow, dy):
    """The whole sky as one (h, day_w, 3) float image.

    Four terms, all vectorised over the panel at once. The mix of the two
    vertical gradients is astronomy -- what light was available. The two things
    added on top of it hug each column's own ridge: twilight from the sun's
    elevation, and the charge glow from the shunt. Keeping those two separate is
    the whole idea: on a foggy day the first is there and the second is not.
    """
    h, w = lay.h, lay.day_w
    night = ds.gradient(NIGHT_SKY, h, dtype=f32)[:, None, :]
    day = ds.gradient(DAY_SKY, h, dtype=f32)[:, None, :]

    # Daylight ramps in over the first twelve degrees of elevation: by then the
    # sky has stopped changing colour quickly and the eye has stopped noticing.
    d = np.clip(elev / 12.0, 0.0, 1.0).astype(f32)[None, :, None]
    sky = night * (1.0 - d) + day * d

    # Falls off exponentially above each column's ridge, so it is a glow rather
    # than a band and it moves up and down with the terrain under it.
    fall = np.exp(-np.maximum(dy, 0.0) / GLOW_ROWS).astype(f32)

    # Twilight: a Gaussian on elevation centred on the horizon, so it is widest
    # at dawn and dusk and gone by mid-morning.
    tw = np.exp(-(elev / 7.0) ** 2).astype(f32)[None, :]
    sky += np.asarray(C_TWILIGHT, f32) * (tw * fall)[:, :, None]
    sky += np.asarray(C_CHARGE, f32) * (glow[None, :] * fall)[:, :, None]
    return sky


def bake_day(lay, rec, now, args, rng):
    """The landscape: sky, terrain, ridge, stars, gaps and the yesterday dim.

    Returns (picture, ridge_row, now_col, gap_mask, ground_mask).
    """
    h, dw = lay.h, lay.day_w
    volt, cur, soc, rank = bin_by_slot(rec, now)

    if args.soc >= 0.0:
        # Shift the whole day so its newest sample reads as the requested
        # value, keeping every wiggle. A flat line moved to 24 % is still a flat
        # line, which is exactly what a bad week's data would look like at this
        # resolution; inventing a plausible decay would be inventing data.
        have = np.isfinite(soc)
        if have.any():
            soc = soc + (args.soc - float(soc[np.flatnonzero(have)[-1]]))

    # Slot -> column. Identity at 320 wide; a squeeze on anything narrower.
    col_slot = (np.arange(dw) * SLOTS) // dw
    v = volt[col_slot]
    c = cur[col_slot]
    s = soc[col_slot]
    r = rank[col_slot]

    # The leading edge of the *data*, not of the wall clock. The column with
    # the highest rank is the one holding the newest sample -- which is not the
    # rightmost occupied column, because a full 24 hour window fills every
    # column and the ones past the cursor hold yesterday. If the fetcher
    # stopped two hours ago the cursor stops two hours short of where the clock
    # says it should be, and the gap is visible rather than papered over.
    now_col = int(np.argmax(r)) if (r >= 0).any() else dw - 1

    # Voltage -> ridge row. Scaled to the day's own range, because the whole
    # span of interest is under a volt and a fixed 12-15 V axis would draw every
    # day as the same flat line. A floor on the span stops a becalmed day from
    # being amplified into a mountain range out of sensor noise.
    fin = np.isfinite(v)
    lo = float(np.nanmin(v)) if fin.any() else 13.0
    hi = float(np.nanmax(v)) if fin.any() else 13.5
    if hi - lo < 0.40:
        mid = 0.5 * (lo + hi)
        lo, hi = mid - 0.20, mid + 0.20
    lo -= 0.06 * (hi - lo)
    hi += 0.06 * (hi - lo)
    unit = np.clip((v - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    ridge = np.round(lay.ridge_lo - unit * (lay.ridge_lo - lay.ridge_hi))
    gap = ~fin
    # A slot the sensor never reported has no ground at all: the panel shows a
    # chasm with a scar at the bottom of it rather than a straight line drawn
    # through a hole.
    ridge = np.where(gap, h + 4, ridge).astype(np.int32)

    # Charge current -> glow. Square-rooted, because the interesting difference
    # is between nothing and something rather than between a lot and slightly
    # more, and a linear map spends nearly all its range on the noon spike.
    cmax = float(np.nanmax(c)) if np.isfinite(c).any() else 1.0
    cn = np.nan_to_num(c, nan=0.0) / max(cmax, 20.0)
    glow = np.sqrt(np.clip(cn, 0.0, 1.0)).astype(f32)

    # Elevation per column, from the sun rather than from the data.
    mid = local_midnight(now)
    elev = np.array([solar_elevation(mid + (sl + 0.5) * SLOT_S,
                                     ftsite.LAT, ftsite.LON)
                     for sl in col_slot], f32)

    yy = np.arange(h, dtype=f32)[:, None]
    dy = ridge[None, :].astype(f32) - yy            # rows above this ridge
    pic = sky_field(lay, elev, glow, dy)

    # Stars, where the sky is genuinely dark. Seeded in build() and static: a
    # twinkle would be four more numpy calls a frame to animate something
    # nobody is looking at.
    dark = (elev < -6.0)
    if dark.any():
        n_stars = max(6, dw // 9)
        sx = rng.randint(0, dw, n_stars)
        sy = rng.randint(1, max(2, lay.ridge_hi), n_stars)
        keep = dark[sx] & (sy < ridge[sx])
        pic[sy[keep], sx[keep]] = np.asarray(C_STAR, f32)

    # The ground. One lookup by depth below the ridge, so the fill is a gradient
    # that starts under the ridge line and darkens all the way down -- which is
    # what makes a 30 row block of green read as ground rather than as a bar.
    latest_soc = None
    fin_s = np.isfinite(s)
    if fin_s.any():
        latest_soc = float(s[np.flatnonzero(fin_s)[-1]])
    if latest_soc is None and rec["soc_pct"] is not None:
        latest_soc = rec["soc_pct"]
    ridge_rgb, ground_stops = health(latest_soc)
    ground_lut = ds.gradient(ground_stops, h, dtype=f32)

    depth = np.clip(-dy, 0, h - 1).astype(np.int32)
    land = (yy >= ridge[None, :].astype(f32))
    np.copyto(pic, np.take(ground_lut, depth, axis=0), where=land[:, :, None])

    # The ridge line itself, one row of a bright colour that carries the health.
    on = (ridge >= 0) & (ridge < h)
    pic[ridge[on], np.arange(dw)[on]] = np.asarray(ridge_rgb, f32)

    # Gaps get a scar on the bottom row so a missing hour cannot read as a
    # peaceful flat night.
    if gap.any():
        pic[h - 1, np.flatnonzero(gap)] = np.asarray(C_GAP, f32)

    # Yesterday. Everything to the right of the data's leading edge came from
    # before local midnight; it is real, it is just not today.
    #
    # Six tenths and not a third, which was the first number here and was
    # wrong. At nine in the morning -- when a makerspace fills up -- the cursor
    # is a third of the way across and *the entire solar event is on the
    # yesterday side*. Dimming it to a third threw away the best part of the
    # picture for most of the hours anybody is standing in front of it. Six
    # tenths is still an unmistakable step at the cursor and leaves yesterday
    # worth looking at, which it is: it is the same day, one turn earlier.
    if now_col + 1 < dw:
        pic[:, now_col + 1:] *= f32(0.60)

    return (pic, ridge, now_col, gap, land, latest_soc, ridge_rgb,
            (lo, hi), glow)


def draw_hours(dst, lay, ridge):
    """Dotted rules and three labels at six, twelve and eighteen hundred.

    In the sky rather than under the ground, because the sky is the darkest part
    of this panel at every hour and five pixel type needs that. No midnight
    label: it is the panel's own edges, twice, and saying so costs the header.
    """
    dw = lay.day_w
    for hour, label in ((6, "6A"), (12, "12P"), (18, "6P")):
        col = int(hour / 24.0 * dw)
        if col >= dw:
            continue
        top = lay.head_h + 7
        rows = np.arange(top, lay.h, 3)
        rows = rows[rows < ridge[col]]
        if len(rows):
            dst[rows, col] = np.maximum(dst[rows, col],
                                        np.asarray(C_FAINT, np.uint8))
        tw = text_width(label)
        x = min(max(0, col - tw // 2), dw - tw)
        if lay.head_h:
            blit_text(dst, lay.head_h + 1, x, label, C_FAINT)


def battery_rect(lay):
    """(x0, x1, y0, y1) of the battery body, inclusive, in gutter coordinates."""
    cx = lay.gut_x + lay.gut_w // 2
    x0, x1 = cx - 9, cx + 9
    y0 = lay.head_h + 3
    y1 = lay.foot_y - 12
    return x0, x1, y0, y1


def draw_battery(dst, lay, soc, ridge_rgb):
    """A battery, drawn as one, with the reserve mark across it.

    A bar chart of a number that is 100 for months on end is not worth 44 rows.
    A *battery* is: the empty space above the fill is the thing being looked at,
    and the dashed reserve line is what makes that empty space mean something
    rather than just be dark.
    """
    if not lay.gut_w:
        return
    x0, x1, y0, y1 = battery_rect(lay)
    # Terminal, then the case outline, then the well it fills.
    dst[y0 - 3:y0, (x0 + x1) // 2 - 2:(x0 + x1) // 2 + 3] = C_BATT_CASE
    dst[y0, x0:x1 + 1] = C_BATT_CASE
    dst[y1, x0:x1 + 1] = C_BATT_CASE
    dst[y0:y1 + 1, x0] = C_BATT_CASE
    dst[y0:y1 + 1, x1] = C_BATT_CASE

    iy0, iy1 = y0 + 2, y1 - 2
    rows = iy1 - iy0 + 1

    # The reserve mark, dashed, drawn before the fill and drawn even when there
    # is no fill to put behind it. It is the whole reason the case is worth 44
    # rows: without it the empty space above the level is just dark.
    ry = int(round(iy1 - (SOC_RESERVE / 100.0) * rows))
    if y0 < ry < y1:
        xs = np.arange(x0 + 1, x1, 2)
        dst[ry, xs] = C_RESERVE

    if soc is None:
        return
    frac = max(0.0, min(1.0, soc / 100.0))
    fill = int(round(frac * rows))
    if fill > 0:
        # A gradient rather than a flat block: the top of the fill is the
        # brightest row, which gives the level an edge the eye can find from
        # across the room.
        lut = ds.gradient([(0.00, ridge_rgb),
                           (0.35, tuple(int(v * 0.55) for v in ridge_rgb)),
                           (1.00, tuple(int(v * 0.25) for v in ridge_rgb))],
                          max(fill, 2))
        dst[iy1 - fill + 1:iy1 + 1, x0 + 2:x1 - 1] = lut[:fill][:, None, :]
        if y0 < ry < y1 and iy1 - fill + 1 <= ry:
            dst[ry, np.arange(x0 + 1, x1, 2)] = C_RESERVE


def status_line(rec, silent, stale, sensor_quiet, sim):
    """The one string at the top right that says how much to believe this."""
    if sim:
        return "SIM", C_SIM
    age = ftdata.describe_age(rec["age"])
    if sensor_quiet:
        sa = rec["sensor_age_s"]
        return ("QUIET " + ftdata.describe_age(sa if sa else rec["age"]),
                C_WARN)
    if stale:
        return "STALE " + age, C_WARN
    return age + " AGO", C_DIM


def foot_line(rec):
    """Status word, volts, uptime. The 190 days is a quiet brag; it gets a line.

    A ladder of shorter forms rather than a clip, because what falls off the end
    of a clipped line here is the uptime, which is the only part of it anybody
    repeats out loud.
    """
    v = rec["v"]
    volts = "%.2fV" % v if v is not None else ""
    up = ""
    if rec["up_days"]:
        up = "UP %dD" % int(rec["up_days"])
    elif rec["uptime"]:
        up = "UP " + rec["uptime"].split()[0].upper()
    word = (rec["status"] or "").upper()[:9]
    for parts in ((word, volts, up), (volts, up), (up,), (volts,)):
        s = "  ".join(p for p in parts if p)
        if s:
            return s
    return ""


def draw_nodata(dst, lay, lines):
    """The honest panel: no landscape, no implied battery."""
    dst[:] = (5, 6, 9)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.h // 2 - (len(lines) * (6 * scale + 2)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        blit_text(dst, y, x, s, rgb, sc)
        y += 5 * sc + 3
    return dst


SILENT_BIG = "NO ANSWER"
SILENT_JOKE = "IT DID SAY IT MIGHT"

# Seconds for the dot to cross the silent card once. See render().
SILENT_PING_S = 6.0


def bake_silent(dst, lay, ghost, rec):
    """It stopped answering, so the panel quotes its own warning back at it.

    The last day it *did* send stays on the panel at two fifths brightness --
    including an empty battery case, because the interesting thing about a
    website that has gone quiet is what it was doing beforehand. It does not
    claim the site is down: from here a dead server and a dead fetcher are the
    same silence, and the only honest claim is that nobody has answered. The
    joke underneath is the site's own front page, quoted back at it.

    Everything except the two big words is baked here; see the note in render()
    on why those are not.
    """
    # In float and back, once, rather than a uint8 ufunc with a Python float:
    # numpy 1.19 resolves that against the *value* rather than the type and
    # picks a float16 loop, which rounds differently from what this machine
    # does. A cast that says what it means costs nothing at bake time.
    np.copyto(dst, (ghost.astype(f32) * 0.42), casting="unsafe")
    # A scrim under the words, so five pixel type survives a hillside.
    y0 = max(0, lay.h // 2 - 14)
    np.copyto(dst[y0:y0 + 29], dst[y0:y0 + 29].astype(f32) * 0.45,
              casting="unsafe")

    age = ftdata.describe_age(rec["age"]) if rec else "A WHILE"
    sub = "%s LAST SPOKE %s AGO" % (
        (rec["site"] if rec else "SEQUOIA.GARDEN").upper(), age.upper())
    blit_text(dst, y0 + 15, max(0, (lay.w - text_width(sub)) // 2), sub, C_TEXT)
    blit_text(dst, y0 + 22, max(0, (lay.w - text_width(SILENT_JOKE)) // 2),
              SILENT_JOKE, C_DIM)
    return y0 + 2, max(0, (lay.w - text_width(SILENT_BIG, 2)) // 2)


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h)
    cache = args.cache_dir
    now_of = clock(parse_when(args.at), args.rate)
    rng = np.random.RandomState(args.seed & 0x7fffffff)

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)
    ghost = np.zeros((h, w, 3), np.uint8)      # the landscape with no type on it
    silent = np.zeros((h, w, 3), np.uint8)     # the card, less its two big words

    # The sweep. Lifts what it crosses towards white, and `delta` is baked with
    # zeros everywhere except the ground, so the per-frame cost is one multiply
    # and one add over a narrow window with no mask lookup -- and the sky, which
    # is lit everywhere and would otherwise turn into a travelling white bar,
    # is untouched. Raised cosine rather than a box: a hard edge reads as a
    # rendering fault and the same brightness with a soft one reads as light.
    sw = max(2, int(args.sweep_width))
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0, 2 * math.pi, sw, dtype=f32)))
    ramp = (ramp * float(args.sweep_gain)).astype(f32)[None, :, None]
    delta = np.zeros((h, w, 3), f32)
    sheen = np.empty((h, sw, 3), f32)

    cell = {"rec": None, "problem": None, "loaded": -1e18, "stale": False,
            "silent": False, "quiet": False, "sim": False, "soc": None,
            "now_col": 0, "charging": False, "volt_range": (0.0, 0.0),
            "silent_at": (0, 0)}

    def reload_data(now):
        rec, age, problem = read_garden(cache)
        cell["rec"], cell["problem"], cell["loaded"] = rec, problem, now
        cell["sim"] = bool(args.soc >= 0.0 or args.off or args.quiet_sensor)
        if rec is None:
            cell["stale"] = cell["silent"] = cell["quiet"] = False
            draw_nodata(silent, lay, [
                ("NO WORD YET", C_WARN),
                ("NOBODY HAS ASKED SEQUOIA.GARDEN HOW IT IS", C_TEXT),
                ("RUN  PYTHON3 FTDATA.PY --ONCE --ONLY SOLAR-GARDEN", C_DIM),
            ])
            return

        ttl = ftdata.ttl_for(PRODUCT) or 1800.0
        cell["stale"] = not ftdata.is_fresh(PRODUCT, age)
        cell["silent"] = bool(args.off) or age > ttl * SILENT_TTL_MULT
        cell["quiet"] = bool(args.quiet_sensor) or rec["sensor_stale"]

        (pic, ridge, now_col, gap, land, soc, ridge_rgb, vrange,
         glow) = bake_day(lay, rec, now, args, rng)
        cell["soc"] = soc
        cell["now_col"] = now_col
        cell["volt_range"] = vrange
        # "Charging" is the shunt, not the status word: the word says Full for
        # months and the current says which hours of which days actually did
        # anything.
        cell["charging"] = bool(rec["i_ma"] is not None and rec["i_ma"] > 50.0)

        static[:] = 0
        static[:, :lay.day_w] = ds.dither(pic)
        if lay.gut_w:
            static[:, lay.gut_x - 1] = C_SEP
        # The ghost the silent card draws behind its words: the landscape and
        # an empty battery case, and no type at all -- five pixel letters at a
        # third brightness under other five pixel letters is mud.
        ghost[:] = static
        draw_battery(ghost, lay, None, ridge_rgb)

        draw_hours(static, lay, ridge)
        draw_battery(static, lay, soc if not cell["silent"] else None,
                     ridge_rgb)

        # Type. All of it baked: every string here comes out of the record, so
        # it can only change when the record does, and formatting four of them
        # thirty times a second to discover nothing had changed is the most
        # expensive thing this file could do on the Pi.
        if lay.head_h:
            blit_text(static, 0, 1, rec["site"], C_NAME)
            msg, rgb = status_line(rec, cell["silent"], cell["stale"],
                                   cell["quiet"], cell["sim"])
            tw = text_width(msg)
            blit_text(static, 0, max(0, lay.day_w - tw - 1), msg, rgb)
        if lay.foot_y < h:
            blit_text(static, lay.foot_y, 1, foot_line(rec), C_DIM)
        if lay.gut_w and soc is not None:
            num = "%d" % int(round(soc))
            scale = 2 if lay.gut_w >= 26 and len(num) <= 3 else 1
            tw = text_width(num, scale)
            x = lay.gut_x + max(0, (lay.gut_w - tw) // 2)
            y = min(h - 5 * scale, lay.foot_y - 5 * (scale - 1))
            blit_text(static, y, x, num,
                      C_RIDGE_LOW if soc < SOC_LOW else C_TEXT, scale)

        if cell["silent"]:
            cell["silent_at"] = bake_silent(silent, lay, ghost, rec)

        # The sweep only ever touches the ground, and only inside the day.
        delta[:] = 0.0
        reg = delta[:, :lay.day_w]
        np.subtract(255.0, static[:, :lay.day_w], out=reg, dtype=f32)
        np.multiply(reg, land[:, :, None], out=reg)

    def state_of():
        return {"rec": cell["rec"], "stale": cell["stale"],
                "silent": cell["silent"], "quiet": cell["quiet"],
                "soc": cell["soc"], "problem": cell["problem"],
                "sim": cell["sim"], "now_col": cell["now_col"]}

    def render(t, i):
        now = now_of()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        rec = cell["rec"]
        if rec is None:
            # Baked, like everything else, and given the same crawling dot: a
            # first-boot panel is on the wall for as long as it takes the timer
            # to fire, and re-rasterising three strings twenty times a second
            # for a quarter of an hour was five times the cost of the panel
            # that has data on it.
            frame[:] = silent
            px = int(((t / SILENT_PING_S) % 1.0) * w)
            frame[h - 2, max(0, px - 2):px + 1] = C_DIM
            return frame
        if cell["silent"]:
            # One copy, one small blit and one three-pixel write. The two big
            # words are re-drawn every frame at a breathing brightness rather
            # than baked with the rest, because a completely still panel
            # between two animated demos reads as a crashed wall -- which is
            # precisely the wrong thing for the panel whose subject is a
            # machine that may have crashed.
            #
            # The breath alone is not enough and the tests caught it: it is an
            # integer brightness, and near the top of the sine it rounds to the
            # same value for a dozen frames running. So a single dot crawls
            # along the bottom of the card as well, once every six seconds,
            # which at twenty frames a second moves three columns every frame
            # and can never round to nothing. It is also the right dot: it is
            # something small still trying.
            frame[:] = silent
            y, x = cell["silent_at"]
            k = 0.62 + 0.38 * math.sin(t * 1.5)
            blit_text(frame, y, x, SILENT_BIG,
                      tuple(int(c * k) for c in C_WARN), 2)
            px = int(((t / SILENT_PING_S) % 1.0) * w)
            frame[h - 2, max(0, px - 2):px + 1] = C_DIM
            return frame

        frame[:] = static

        # The reveal: the day drawing itself in, left to right, which is the day
        # being replayed at a couple of seconds a day. Two slice copies rather
        # than a mask, and what is restored underneath is black rather than the
        # sky, so the landscape arrives rather than being uncovered.
        edge = lay.day_w
        if args.reveal > 0 and t < args.reveal:
            edge = int(lay.day_w * (t / args.reveal))
            frame[:, edge:lay.day_w] = 0
            if edge < lay.day_w:
                frame[:, edge] = C_NOW
        elif args.sweep > 0:
            phase = ((t - args.reveal) % args.sweep) / args.sweep
            x0 = int(phase * (lay.day_w + 2 * sw)) - sw
            a, b = max(0, x0), min(lay.day_w, x0 + sw)
            if b > a:
                buf = sheen[:, :b - a]
                np.multiply(delta[:, a:b], ramp[:, a - x0:b - x0], out=buf)
                np.add(buf, static[:, a:b], out=buf)
                frame[:, a:b] = buf

        # The cursor, over everything. It breathes, and a pulse runs down it --
        # which is the one thing on this panel guaranteed to move in *every*
        # frame: the sweep spends half a second off the edge between passes and
        # the breath is an integer brightness that rounds the same way for
        # several frames at a time. Both driven by the segment's own `t`, so a
        # test harness rendering a hundred frames in a millisecond sees the same
        # animation the wall does.
        col = cell["now_col"]
        if col < edge:
            blink = 0.55 + 0.45 * math.sin(t * 2.0)
            top = lay.head_h + 1
            frame[top:h, col] = tuple(int(c * blink) for c in C_NOW)
            if col + 1 < lay.day_w:
                frame[top:h, col + 1] = C_NOW_HALO
            py = top + int(((t * PULSE_HZ) % 1.0) * (h - top))
            frame[max(top, py - 1):py + 2, col] = C_NOW

        # A charge shimmer rising through the fill, only while the shunt says
        # something is actually going in. Two short writes, and off entirely
        # for the twenty hours a day nothing is.
        if cell["charging"] and lay.gut_w and cell["soc"] is not None:
            x0, x1, y0, y1 = battery_rect(lay)
            iy0, iy1 = y0 + 2, y1 - 2
            rows = iy1 - iy0 + 1
            fill = int(round(max(0.0, min(1.0, cell["soc"] / 100.0)) * rows))
            if fill > 3:
                span = iy1 - fill + 1
                sy = iy1 - int(((t * 0.45) % 1.0) * (iy1 - span))
                frame[max(span, sy - 1):sy + 1, x0 + 2:x1 - 1] = C_CHARGE
        return frame

    reload_data(now_of())
    render.state = cell               # the tests reach in here; nothing else
    render.layout = lay
    render.clock = now_of
    render.static = static
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "the makerspace's solar-powered website, as one day",
                  fps=20)


if __name__ == "__main__":
    main()
