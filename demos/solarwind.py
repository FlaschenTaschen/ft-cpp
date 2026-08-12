#!/usr/bin/env python3
"""The solar wind in flight, from the Sun's limb to the Earth's magnetosphere.

This is the one subject on the wall that is *natively* 5:1: the Sun-Earth line
is a line, and everything interesting about space weather happens along it. So
the panel is that line, drawn left to right, and it is a picture rather than a
readout. `propagation.py` already puts these numbers on the wall -- SFI, Kp,
Bz, wind speed, in tiles, for a ham deciding on a band -- and a second panel of
the same numbers would be a duplicate. What a table of numbers cannot show is
*where the plasma is*, which way the field in it is pointing, and what it is
about to do to the Earth. That is what this draws.

**The one representation choice: x is distance, which is also time.** SWPC
publishes `propagated-solar-wind-1-hour.json`, an hour of one-minute samples
measured at L1 with, for each, the time it will reach the bow shock. At normal
wind speeds that trip takes about fifty minutes -- so the file is almost
exactly the plasma currently *in flight*. Its oldest row is arriving at the
magnetosphere about now; its newest was measured a minute ago, a million and a
half kilometres upstream. Lay those rows out with the oldest on the right and
the newest on the left and you have not drawn a chart with a time axis: you
have drawn the corridor, with the plasma in it, in the right places. Everything
else on the panel falls out of that. The stream flows rightward because the
plasma does. A southward patch of field sits at the x where that plasma
actually is. The magnetosphere is squeezed by the sample arriving *now*, which
is the right-hand end of the record and not the headline number.

**Honest about scale, which it cannot be.** The corridor is one hour of
travel, roughly 0.01 AU. The remaining 99% of the way to the Sun is not drawn;
the Sun's limb at the left edge is an emblem of where the wind came from, not a
body at its true distance, which would be a hundred panels away. The Earth is
drawn about four times oversized so that an aurora can be more than one pixel,
and the magnetosphere is at 1.55 px per Earth radius around it. Everything
*within* the magnetosphere is to that one scale, so the compression is real
even though the corridor's scale is not.

**The magnetopause is the Shue model, not a doodle.** Shue et al. (1997) fit
the standoff distance and flaring of the magnetopause to two numbers:

    r0    = (10.22 + 1.29 tanh(0.184 (Bz + 8.14))) * Dp^(-1/6.6)
    alpha = (0.58 - 0.007 Bz) (1 + 0.024 ln Dp)
    r(t)  = r0 (2 / (1 + cos t))^alpha

with dynamic pressure Dp = 1.6726e-6 n v^2 in nPa. On a quiet day that puts the
nose at 10.2 Earth radii; at 20 nPa and Bz -20 it puts it at 5.6, and on the
panel the whole cavity visibly caves in. The bow shock is drawn at 1.3 r0,
which is the usual rule of thumb rather than a model, and the magnetosheath
between the two is the brightest plasma on the panel because shocked plasma
piles up there -- which is exactly what the real thing looks like from a
spacecraft. When the nose has moved in by more than a couple of radii a dotted
ghost of the quiet-day magnetopause stays behind, so the compression is
readable in a single frame instead of needing a memory of yesterday.

**The chain the panel exists to teach.** Southward Bz -- the field lines
tilting down rather than up -- is what lets the solar wind couple into the
magnetosphere. So when the field arriving at the nose is southward, the dashes
turn from cool blue to hot magenta, sparks appear at the subsolar magnetopause
and run back along the flanks into the tail, the tail's X-line flashes, and a
beat later the poles light up. North field: the dashes are blue, the flanks are
quiet, the poles are a dim smudge. Nobody has to read that; it is a picture of
a cause and an effect, and it is the thing `propagation.py` cannot draw.

**Text is three numbers.** Wind speed with its unit, Bz with its sign, Kp.
Everything else is drawn. The age of the data is in the bottom right corner
always, because a picture of the solar wind that is six hours old is a lie told
confidently, and past three TTLs the panel greys out and stops quoting numbers
altogether.

**How it is cheap enough for the Pi.** The whole panel is a uint8 *index*
image through one 256-entry palette, split into bands -- plasma 0..63, north
field 64..95, south field 96..127, and so on -- so every layer composites in
integers and colour happens exactly once, in a single `np.take`. The streaming
plasma is a seeded streak texture, baked once, made periodic in the panel width
and stored twice side by side: scrolling it is a *slice*, not a roll and not a
take, and it costs nothing. Everything static -- the Sun, the field dashes, the
Shue curves, the type -- is baked into one overlay and stamped in with a single
`np.copyto(where=)`. That leaves five whole-panel numpy calls a frame plus a
dozen writes of a handful of pixels each for the sparks and the aurora.

Run:  python3 ftdata.py --once --only swpc_solarwind    # the fetcher
      python3 solarwind.py --host 127.0.0.1
      python3 solarwind.py --storm                    # the interesting day
      python3 solarwind.py --bz -18 --speed 750 --kp 7
      FT_DATA_CACHE=/tmp/empty python3 solarwind.py   # the no-data card
"""

import math
import sys

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

WIND_PRODUCT = "swpc_solarwind"
KP_PRODUCT = "swpc_kp"

# --------------------------------------------------------------------------
# Type. The same baked 3x5 font propagation, caiso and tide draw with: five
# rows a glyph, each row an octal digit whose three bits are its columns. There
# is no font file to be missing on the Pi, and at this size TrueType is mush.
# Here it stamps palette *indices* rather than colours, because every layer on
# this panel is an index image until the very last call.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT.update({"+": "02720", "?": "71302", "!": "22202", ".": "00002"})

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


def stamp(idx, mask, x, y, s, value, scale=1):
    """Draw text as a palette index into (H, W) uint8 `idx`, clipped.

    `mask` is the overlay's own opacity mask and is set wherever a glyph pixel
    lands. Clipping rather than raising: this is laid out for 320x64 but must
    survive being handed something narrower, and a demo that raises on an odd
    canvas takes the whole rotation down with it.
    """
    m = text_mask(s)
    if scale > 1:
        m = np.repeat(np.repeat(m, scale, 0), scale, 1)
    h, w = m.shape
    H, W = idx.shape
    x, y = int(x), int(y)
    sx, sy = max(0, -x), max(0, -y)
    ex, ey = min(w, W - x), min(h, H - y)
    if ex > sx and ey > sy:
        sub = m[sy:ey, sx:ex] > 0
        idx[y + sy:y + ey, x + sx:x + ex][sub] = value
        mask[y + sy:y + ey, x + sx:x + ex][sub] = True
    return w


# --------------------------------------------------------------------------
# The palette, in bands.
#
# One 256-entry table serves the whole panel, cut into ranges: an index says
# both *what* a pixel is and *how bright*. That is what lets the plasma, the
# field, the shock, the aurora and the type all composite as integers and turn
# into colour exactly once a frame. It also makes the stale state a
# build-time operation -- greying the panel is greying this table, and render()
# never learns that anything changed.
# --------------------------------------------------------------------------

PLASMA = 0        # 0..63   the wind itself
FIELD_N = 64      # 64..95  IMF pointing north: cool, quiet
FIELD_S = 96      # 96..127 IMF pointing south: hot, the dangerous colour
SHOCK = 128       # 128..143 bow shock and magnetopause
AURORA = 144      # 144..175
SUN = 176         # 176..207
SPARK = 208       # 208..215 reconnection
EARTH = 216       # 216..223
CAVITY = 224      # 224..231 the magnetosphere's inside, and the neutral sheet
INK = 232         # a value you are meant to read
LABEL = 233       # the word next to it
DIM = 234
WARN = 235
ALERT = 236
GOOD = 237
KP0 = 240         # 240..249, the conventional Kp ramp

# The conventional space-weather Kp colours, the ones every forecast site uses;
# a ham or an aurora chaser reads them faster than the digit beside them.
KP_COLOURS = [
    (40, 190, 90), (40, 190, 90), (90, 200, 60), (170, 210, 40),
    (240, 200, 0),
    (255, 140, 0), (255, 95, 10), (235, 40, 25),
    (200, 10, 60), (185, 0, 140),
]


def build_palette(grey=0.0, dim=1.0):
    """The 256-entry table. `grey` desaturates it, `dim` darkens it."""
    pal = np.zeros((256, 3), f32)

    # Plasma: near-black through deep ember to a pale hot core. It has to read
    # as *matter* against black at three metres, so the dark end climbs fast.
    pal[PLASMA:PLASMA + 64] = ds.gradient([
        (0.00, (0, 0, 0)), (0.18, (28, 8, 4)), (0.42, (120, 40, 10)),
        (0.68, (215, 105, 25)), (0.86, (250, 180, 80)),
        (1.00, (255, 240, 200))], 64, dtype=f32)

    pal[FIELD_N:FIELD_N + 32] = ds.gradient([
        (0.00, (10, 24, 46)), (0.55, (40, 110, 190)),
        (1.00, (130, 210, 255))], 32, dtype=f32)
    pal[FIELD_S:FIELD_S + 32] = ds.gradient([
        (0.00, (46, 8, 26)), (0.55, (205, 30, 90)),
        (1.00, (255, 120, 170))], 32, dtype=f32)

    pal[SHOCK:SHOCK + 16] = ds.gradient([
        (0.00, (16, 34, 44)), (0.55, (70, 150, 165)),
        (1.00, (190, 245, 255))], 16, dtype=f32)

    pal[AURORA:AURORA + 32] = ds.gradient([
        (0.00, (0, 10, 6)), (0.35, (10, 110, 60)), (0.70, (60, 235, 130)),
        (1.00, (215, 255, 230))], 32, dtype=f32)

    pal[SUN:SUN + 32] = ds.gradient([
        (0.00, (60, 12, 0)), (0.35, (210, 70, 0)), (0.70, (255, 175, 40)),
        (1.00, (255, 250, 225))], 32, dtype=f32)

    pal[SPARK:SPARK + 8] = ds.gradient([
        (0.00, (60, 90, 140)), (1.00, (240, 250, 255))], 8, dtype=f32)

    pal[EARTH:EARTH + 8] = ds.gradient([
        (0.00, (6, 16, 34)), (0.45, (30, 80, 150)),
        (1.00, (170, 220, 255))], 8, dtype=f32)

    pal[CAVITY:CAVITY + 8] = ds.gradient([
        (0.00, (2, 4, 10)), (1.00, (28, 40, 74))], 8, dtype=f32)

    pal[INK] = (215, 228, 245)
    pal[LABEL] = (98, 122, 155)
    pal[DIM] = (52, 66, 88)
    pal[WARN] = (255, 165, 30)
    pal[ALERT] = (255, 60, 45)
    pal[GOOD] = (30, 205, 80)
    for i, c in enumerate(KP_COLOURS):
        pal[KP0 + i] = c

    if grey > 0:
        lum = pal @ np.array([0.30, 0.59, 0.11], f32)
        pal += (lum[:, None] - pal) * float(grey)
    if dim != 1.0:
        pal *= float(dim)
    # Index 0 is the panel's ground and must stay exactly black: the wall's
    # layer compositing treats black as transparent, and a "nearly black"
    # background is a full-panel opaque rectangle over whatever is beneath.
    pal[0] = 0
    return np.clip(pal, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------
# Freshness. Same three stages the other data panels use.
# --------------------------------------------------------------------------

FRESH, AGING, STALE, ABSENT = "fresh", "aging", "stale", "absent"
STALE_MULTIPLE = 3.0


def _state(age, ttl):
    if age is None:
        return ABSENT
    if age <= ttl:
        return FRESH
    if age <= ttl * STALE_MULTIPLE:
        return AGING
    return STALE


# --------------------------------------------------------------------------
# The physics. Two formulas, both standard, both worth having exactly right --
# a magnetopause that does not move when the pressure trebles is the failure
# this whole panel would be built around.
# --------------------------------------------------------------------------

def dyn_pressure(density, speed):
    """Solar wind dynamic pressure in nPa from n (cm^-3) and v (km/s)."""
    if density is None or speed is None:
        return None
    return 1.6726e-6 * float(density) * float(speed) ** 2


def shue_r0(bz, dp):
    """Magnetopause subsolar standoff distance in Earth radii, Shue 1997."""
    dp = max(0.05, float(dp))
    return (10.22 + 1.29 * math.tanh(0.184 * (float(bz) + 8.14))) * dp ** (-1.0 / 6.6)


def shue_alpha(bz, dp):
    """The flaring exponent; bigger means a fatter, more open tail."""
    dp = max(0.05, float(dp))
    return (0.58 - 0.007 * float(bz)) * (1.0 + 0.024 * math.log(dp))


def shue_r(theta, r0, alpha):
    """Radius at angle theta from the Sun-Earth line. Vectorised."""
    # 1 + cos goes to zero straight down the tail, where the model diverges;
    # clipping it caps the tail's width at something the panel can hold rather
    # than producing an inf and a silent all-black frame.
    denom = np.maximum(1.0 + np.cos(theta), 0.045)
    return r0 * (2.0 / denom) ** alpha


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="where ftdata.py writes; defaults to $FT_DATA_CACHE "
                         "or ~/.cache/ftdata")
    ap.add_argument("--seed", type=int, default=20260811,
                    help="seed for the plasma streak texture")
    ap.add_argument("--storm", action="store_true",
                    help="ignore the cache and draw a severe storm: a fast "
                         "dense stream with the field rotating from south to "
                         "north across the panel. The interesting state is "
                         "rare and this is how it gets looked at")
    ap.add_argument("--speed", type=float, default=None,
                    help="override the wind speed, km/s, across the record")
    ap.add_argument("--density", type=float, default=None,
                    help="override the proton density, cm^-3")
    ap.add_argument("--bz", type=float, default=None,
                    help="override Bz, nT GSM; negative is southward")
    ap.add_argument("--kp", type=float, default=None,
                    help="override the planetary Kp, 0..9")
    ap.add_argument("--flow", type=float, default=0.11,
                    help="panel pixels per second per km/s of wind: how fast "
                         "the stream scrolls. 0.11 makes 400 km/s cross the "
                         "panel in seven seconds and 800 in three and a half")
    ap.add_argument("--no-ghost", dest="ghost", action="store_false",
                    default=True,
                    help="drop the dotted quiet-day magnetopause that stays "
                         "behind when the real one is compressed")


# --------------------------------------------------------------------------
# The record, and the four synthetic ones.
# --------------------------------------------------------------------------

def _clean(values, fallback):
    """Fill the nulls a dropped instrument leaves, without inventing a trend.

    Nearest-neighbour, not interpolated: a three-minute hole in the density
    during a shock arrival is a hole, and drawing a smooth ramp across it would
    be the fetcher's job if it were anybody's, which it is not.
    """
    out = list(values)
    n = len(out)
    last = None
    for i in range(n):
        if out[i] is None:
            out[i] = last
        else:
            last = out[i]
    nxt = None
    for i in range(n - 1, -1, -1):
        if out[i] is None:
            out[i] = nxt
        else:
            nxt = out[i]
    return [fallback if v is None else float(v) for v in out]


def _storm_record(n=56):
    """A severe geomagnetic storm, drawn from the textbook rather than fitted.

    A CME sheath already arriving -- 700-plus km/s, twenty protons per cc, Bz
    deep south -- followed by the magnetic cloud behind it whose field rotates
    smoothly back to north over the hour still in flight. That rotation is the
    single most legible thing this panel can show: half the field dashes are
    magenta and half are blue, with the turn visibly in transit.
    """
    speed, density, bz, bt = [], [], [], []
    for i in range(n):
        u = i / float(n - 1)            # 0 = arriving now, 1 = just measured
        speed.append(round(690 + 70 * u, 0))
        density.append(round(19.0 - 10.0 * u, 1))
        # -18 nT at the Earth end rotating to +9 at the far end, which is what
        # a flux rope passing over does.
        bz.append(round(-18.0 + 27.0 * u ** 1.4, 1))
        bt.append(round(21.0 - 3.0 * u, 1))
    return {"speed": speed, "density": density, "bz": bz, "bt": bt,
            "samples": n, "minutes_per_sample": 1,
            "latest": {"speed": speed[-1], "density": density[-1],
                       "bz": bz[-1], "bt": bt[-1], "t": None, "arrival": None},
            "aurora": {"north_gw": 118, "south_gw": 104, "t": None}}


class Wind(object):
    """The record, its age, and the four arrays the panel actually draws.

    Arrays are ordered oldest first, which is the order the fetcher stores and
    also the order of distance from the Sun: element 0 is the sample arriving
    at the bow shock about now, element -1 was measured at L1 a minute ago.
    """

    def __init__(self, args):
        self.state = ABSENT
        self.age = None
        # True when any of what is drawn was made up rather than measured, so
        # the corner can say SIM instead of quoting an age. A synthetic storm
        # labelled "0s" is a panel claiming a severe geomagnetic event is
        # happening right now, which is the single worst thing this file could
        # put on a wall in a public workshop.
        self.synthetic = bool(args.storm or args.speed is not None
                              or args.density is not None
                              or args.bz is not None)
        self.ttl = ftdata.ttl_for(WIND_PRODUCT) or 3600.0
        payload = None

        if args.storm:
            payload, self.age, self.state = _storm_record(), 0.0, FRESH
        else:
            got = ftdata.load(WIND_PRODUCT, args.cache_dir)
            if got is not None:
                payload, self.age = got
                self.state = _state(self.age, self.ttl)

        # An override with no cache at all is still a panel: somebody asking
        # for --bz -20 wants to see the picture, not a no-data card.
        forced = [v for v in (args.speed, args.density, args.bz) if v is not None]
        if payload is None and forced:
            payload = _storm_record()
            self.state, self.age = FRESH, 0.0

        self.payload = payload
        if payload is None:
            self.speed = self.density = self.bz = self.bt = None
            self.n = 0
        else:
            n = max(2, int(payload.get("samples") or len(payload.get("speed") or ())))
            self.speed = _clean(payload.get("speed") or [], 400.0)[:n]
            self.density = _clean(payload.get("density") or [], 5.0)[:n]
            self.bz = _clean(payload.get("bz") or [], 0.0)[:n]
            self.bt = _clean(payload.get("bt") or [], 5.0)[:n]
            # A short or ragged record is padded rather than refused: the file
            # is one row a minute and SWPC does drop minutes.
            for arr, fill in ((self.speed, 400.0), (self.density, 5.0),
                              (self.bz, 0.0), (self.bt, 5.0)):
                while len(arr) < n:
                    arr.append(arr[-1] if arr else fill)
            self.n = n
            if args.speed is not None:
                self.speed = [float(args.speed)] * n
            if args.density is not None:
                self.density = [float(args.density)] * n
            if args.bz is not None:
                self.bz = [float(args.bz)] * n
                self.bt = [max(abs(float(args.bz)) + 1.0, b) for b in self.bt]

    @property
    def usable(self):
        return self.payload is not None and self.state in (FRESH, AGING, STALE)

    @property
    def quote(self):
        """May the numbers be printed? Past three TTLs, no."""
        return self.usable and self.state in (FRESH, AGING)

    def aurora_gw(self):
        au = (self.payload or {}).get("aurora") or {}
        vals = [v for v in (au.get("north_gw"), au.get("south_gw"))
                if isinstance(v, (int, float))]
        return max(vals) if vals else None


def load_kp(args):
    """The planetary Kp, from the product propagation.py already fetches.

    Reused rather than re-fetched: swpc_kp is in the cache anyway, and two
    products quoting the same index from different files is how two panels on
    the same wall end up disagreeing about whether there is a storm.
    """
    if args.kp is not None:
        return float(args.kp), FRESH
    if args.storm:
        return 7.0, FRESH
    got = ftdata.load(KP_PRODUCT, args.cache_dir)
    if got is None:
        return None, ABSENT
    payload, age = got
    state = _state(age, ftdata.ttl_for(KP_PRODUCT) or 5400.0)
    now = (payload.get("now") or {}).get("kp")
    if now is None:
        series = payload.get("series") or []
        now = series[-1].get("kp") if series else None
    try:
        return float(now), state
    except (TypeError, ValueError):
        return None, state


# --------------------------------------------------------------------------
# The streak texture: the only thing on the panel that moves by itself.
# --------------------------------------------------------------------------

def streak_texture(H, W, length, seed, fill=0.055):
    """A periodic field of comet-shaped streaks, 0..63, tails to the left.

    Sparse seeds smeared with an exponential decay, wrapped in x so the texture
    tiles: that is what lets render() scroll it with a *slice* of a doubled
    copy rather than a roll or a take. The tail trails to the left because the
    stream flows right, and a streak with its bright end at the back reads as
    moving backwards -- which took an embarrassingly long time to notice.
    """
    rng = np.random.RandomState(seed & 0x7fffffff)
    noise = rng.random_sample((H, W)).astype(f32)
    seeds = np.where(noise > 1.0 - fill, (noise - (1.0 - fill)) / fill, 0.0)
    seeds = seeds.astype(f32) ** 0.6

    acc = np.zeros((H, W), f32)
    decay = math.exp(-1.0 / max(2.0, length * 0.42))
    weight = 1.0
    for i in range(int(max(2, length))):
        # roll by -i puts the seed's contribution at x - i, i.e. behind it.
        acc += np.roll(seeds, -i, axis=1) * weight
        weight *= decay
    peak = float(acc.max()) or 1.0
    return np.clip(acc * (63.0 / peak), 0, 63).astype(np.uint8)


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    W, H = int(args.width), int(args.height)
    out_rgb = np.empty((H, W, 3), np.uint8)

    wind = Wind(args)
    kp, kp_state = load_kp(args)

    if not wind.usable:
        return _no_data(args, W, H, out_rgb)

    grey = 0.0 if wind.state == FRESH else (0.35 if wind.state == AGING else 0.8)
    dim = 1.0 if wind.state == FRESH else (0.72 if wind.state == AGING else 0.45)
    pal = build_palette(grey, dim)

    # ---------------------------------------------------------------- layout
    yc = H // 2
    # 1.55 px per Earth radius is what makes the flanks of a quiet-day
    # magnetopause -- 15 Re out at the terminator -- land four rows short of
    # the panel edge. Any larger and the cavity is an egg filling the right
    # third; any smaller and the storm-day compression is two pixels.
    scale = 1.55 * (H / 64.0)
    xe = W - int(round(50 * W / 320.0))      # the Earth, with room for a tail
    x_sun = int(round(26 * W / 320.0))       # where the Sun's limb reaches

    # The corridor's data axis. Its right-hand end is the bow shock's nose:
    # past that the plasma is shocked, not solar wind, and the record's
    # rightmost sample is by construction the one arriving there.
    n = wind.n
    speed = np.array(wind.speed, f32)
    density = np.array(wind.density, f32)
    bz = np.array(wind.bz, f32)
    bt = np.maximum(np.array(wind.bt, f32), np.abs(bz))

    # The magnetosphere is driven by the sample arriving now, which is element
    # 0 -- not by the headline number, which was measured at L1 and will not
    # arrive for another fifty minutes. Getting that backwards would draw a
    # magnetosphere reacting to plasma that has not reached it.
    bz_now, n_now, v_now = float(bz[0]), float(density[0]), float(speed[0])
    dp_now = dyn_pressure(n_now, v_now) or 2.0
    r0 = shue_r0(bz_now, dp_now)
    alpha = shue_alpha(bz_now, dp_now)
    r0_quiet = shue_r0(0.0, 2.0)
    alpha_quiet = shue_alpha(0.0, 2.0)

    x_shock = xe - int(round(1.3 * r0 * scale))
    x_left = x_sun + 4
    span = max(8, x_shock - x_left)

    # Column -> sample. x_left is the newest sample (just measured at L1),
    # x_shock the oldest (arriving now), because that is where the plasma is.
    cols = np.arange(W, dtype=f32)
    frac = np.clip((x_shock - cols) / float(span), 0.0, 1.0)
    samp = np.clip((frac * (n - 1)).astype(np.int32), 0, n - 1)

    v_col = speed[samp]
    n_col = density[samp]
    bz_col = bz[samp]
    bt_col = bt[samp]

    # ------------------------------------------------------- the shaping map
    # One baked (H, W) multiplier carries every static thing about the plasma:
    # how dense the wind is in each column, the fan out of the Sun, the pile-up
    # in the magnetosheath and the hole where the magnetosphere is. Because it
    # is baked, render() gets all of it for one multiply.
    yy = np.arange(H, dtype=f32)[:, None] - yc
    xx = cols[None, :]

    dx_re = (xx - xe) / scale                 # +x is downstream (rightward)
    dy_re = -yy / scale                       # +y is north (up the panel)
    rr = np.sqrt(dx_re * dx_re + dy_re * dy_re)
    rr = np.maximum(rr, 1e-3)
    theta = np.arccos(np.clip(-dx_re / rr, -1.0, 1.0))
    r_mp = shue_r(theta, r0, alpha)
    r_bs = shue_r(theta, 1.3 * r0, alpha + 0.06)
    inside_mp = rr < r_mp
    sheath = (~inside_mp) & (rr < r_bs)

    # Density in each column, normalised around a typical 5 cm^-3. Clipped
    # hard at both ends: a tenuous coronal hole stream still has to be visible
    # and a 40 cm^-3 shock still has to leave headroom for the sheath.
    amp = np.clip(n_col / 6.0, 0.45, 1.35)[None, :] * np.ones((H, 1), f32)
    # The fan: brightest along the ecliptic, thinning towards the panel edges.
    amp *= (0.72 + 0.28 * np.exp(-(yy / (H * 0.62)) ** 2))
    # The Sun's glare, so the stream is born out of light rather than starting
    # abruptly at a column boundary.
    glare = np.exp(-np.maximum(0.0, xx - x_sun) / (W * 0.055))
    amp *= 1.0 + 0.7 * glare
    amp[sheath] *= 1.7                       # shocked plasma piles up
    amp[inside_mp] = 0.0                     # and none of it gets inside
    # Clipped against a fixed reference rather than normalised against this
    # panel's own maximum. Normalising is the tempting one line and it is
    # wrong: it makes every day look the same, which is the one thing a panel
    # about how much plasma is arriving must not do.
    amp = np.clip(amp * 0.80, 0.0, 1.0)
    amp2d = (amp * 256.0).astype(np.uint16)

    # ------------------------------------------------------------ the streaks
    # Streak length grows with the wind speed, which is the second cue after
    # scroll rate: fast wind is long bright dashes, slow wind is stipple. The
    # median rather than the latest, so one noisy minute cannot restyle the
    # whole panel.
    v_med = float(np.median(speed))
    tex = streak_texture(H, W, int(np.clip(v_med / 42.0, 6, 26)), args.seed)
    tex2 = np.concatenate([tex, tex], axis=1)

    # ------------------------------------------------------------ the overlay
    ovl = np.zeros((H, W), np.uint8)
    omask = np.zeros((H, W), np.bool_)

    _draw_sun(ovl, omask, W, H, yc, x_sun, args.seed)
    _draw_field(ovl, omask, W, H, yc, x_left, x_shock, bz_col, bt_col,
                inside_mp | sheath)
    ghosted = _draw_magnetosphere(ovl, omask, W, H, yc, xe, scale, r0, alpha,
                                  r0_quiet, alpha_quiet, args.ghost)
    _draw_earth(ovl, omask, yc, xe, scale)

    # ------------------------------------------------------------- the labels
    _draw_text(ovl, omask, W, H, x_left, xe, scale, wind, kp, kp_state)

    # -------------------------------------------------- the animated fragments
    aur_flat, aur_base = _aurora_pixels(W, H, yc, xe, scale, kp)
    spark_path = _spark_path(W, H, yc, xe, scale, r0, alpha)
    tail_x = min(W - 2, xe + int(round(12 * scale)))

    gw = wind.aurora_gw()
    if gw is not None:
        activity = float(np.clip((gw - 6.0) / 70.0, 0.05, 1.0))
    elif kp is not None:
        activity = float(np.clip(kp / 8.0, 0.05, 1.0))
    else:
        activity = 0.25
    # Southward field at the nose is the switch. Everything downstream of it --
    # the sparks, the tail, the brightness of the poles -- is gated on it,
    # because that is the physics and because a panel that flickers merrily
    # through a quiet northward day is telling somebody the wrong thing.
    coupling = float(np.clip(-bz_now / 12.0, 0.0, 1.0))
    aur_level = float(np.clip(0.22 + 0.78 * activity * (0.35 + 0.65 * coupling),
                              0.0, 1.0))

    px_per_s = max(4.0, v_med * float(args.flow) * (W / 320.0))

    return _make_render(out_rgb, pal, tex2, amp2d, ovl, omask, W, H,
                        px_per_s, spark_path, coupling, aur_flat, aur_base,
                        aur_level, tail_x, yc, scale, ghosted)


# --------------------------------------------------------------------------
# render()
# --------------------------------------------------------------------------

def _make_render(out_rgb, pal, tex2, amp2d, ovl, omask, W, H, px_per_s,
                 spark_path, coupling, aur_flat, aur_base, aur_level, tail_x,
                 yc, scale, ghosted):
    """Five whole-panel numpy calls and a dozen very small ones.

    The scroll is a slice into a texture stored twice, so it costs nothing at
    all; the multiply and shift apply the baked shaping map in integers; the
    copyto stamps every static thing at once; the take is the only place colour
    happens. render() is a pure function of t -- nothing is carried between
    calls -- so the preview baker, the scheduler's cold start and the wall's
    drifting loop all land on the same frame at the same t.
    """
    buf16 = np.empty((H, W), np.uint16)
    buf8 = np.empty((H, W), np.uint8)
    n_path = len(spark_path[0]) if spark_path else 0
    # Two buffers, not one, and the float one is not an accident: multiplying
    # a uint8 array by a float straight into a uint8 `out=` needs
    # casting="unsafe", and the exact casting rules around that moved between
    # numpy 1.19 (which the wall runs) and 2.x (which this was written on).
    # Going through float32 and copying back is the version that means the
    # same thing under both.
    aur_vals = np.empty(aur_base.shape, np.uint8) if aur_base.size else None
    aur_tmp = np.empty(aur_base.shape, f32) if aur_base.size else None

    def render(t, frame=0):
        shift = int(t * px_per_s) % W
        np.multiply(tex2[:, W - shift:2 * W - shift], amp2d, out=buf16)
        np.right_shift(buf16, 8, out=buf16)
        np.copyto(buf8, buf16, casting="unsafe")
        np.copyto(buf8, ovl, where=omask)

        # Reconnection: bright knots that appear at the subsolar magnetopause
        # and slide back along both flanks into the tail, but only while the
        # arriving field is southward. Three per flank is enough to read as a
        # flow and cheap enough to be free.
        if coupling > 0.05 and n_path:
            rate = 0.20 + 0.55 * coupling
            for k in range(3):
                u = (t * rate + k / 3.0) % 1.0
                i = int(u * (n_path - 1))
                lvl = SPARK + int(min(7, 3 + 5 * (1.0 - u)))
                for ys, xs in ((spark_path[0][i], spark_path[1][i]),
                               (spark_path[2][i], spark_path[1][i])):
                    if 1 <= ys < H - 2 and 1 <= xs < W - 2:
                        buf8[ys - 1:ys + 2, xs - 1:xs + 2] = lvl
            # The tail's X-line, flashing a beat after the knots leave the
            # flanks: substorm onset, which is what actually lights the poles.
            beat = (t * rate * 0.5) % 1.0
            if beat < 0.22:
                lvl = SPARK + int(3 + 4 * (1.0 - beat / 0.22))
                half = max(2, int(3 * scale))
                buf8[yc - half:yc + half, tail_x:tail_x + 2] = lvl

        if aur_vals is not None:
            # The poles breathe -- an aurora is never steady -- and the beat is
            # tied to the same substorm clock as the tail.
            pulse = 0.72 + 0.28 * math.sin(t * 1.9) * (0.4 + 0.6 * coupling)
            np.multiply(aur_base, aur_level * pulse, out=aur_tmp)
            np.add(aur_tmp, AURORA, out=aur_tmp)
            np.copyto(aur_vals, aur_tmp, casting="unsafe")
            np.put(buf8, aur_flat, aur_vals)

        np.take(pal, buf8, axis=0, out=out_rgb)
        return out_rgb

    return render


# --------------------------------------------------------------------------
# The pieces, all baked.
# --------------------------------------------------------------------------

def _draw_sun(ovl, omask, W, H, yc, x_sun, seed):
    """A limb, not a disc: the Sun is far too big to fit and far too far away.

    Centred well off the left edge with a radius that puts its limb a few
    columns in, so what shows is a curved edge with granulation on it. Drawn as
    a disc the panel would have to be a hundred pixels of sun.
    """
    rs = (x_sun + 40.0) * 1.05
    cx = x_sun - rs + 1.0
    xs = np.arange(0, min(W, x_sun + 2), dtype=f32)[None, :]
    ys = np.arange(H, dtype=f32)[:, None] - yc
    r = np.sqrt((xs - cx) ** 2 + ys ** 2) / rs
    disc = r <= 1.0
    if not disc.any():
        return
    rng = np.random.RandomState((seed + 7) & 0x7fffffff)
    gran = rng.random_sample(disc.shape).astype(f32)
    # Limb darkening, the real thing: I(mu) ~ 0.3 + 0.7 mu, mu = cos of the
    # angle from disc centre. It is what makes a drawn sun look like a sun
    # instead of an orange circle.
    mu = np.sqrt(np.clip(1.0 - r ** 2, 0.0, 1.0))
    level = (0.30 + 0.70 * mu) * (0.86 + 0.14 * gran)
    idx = SUN + np.clip(level * 31.0, 0, 31).astype(np.uint8)
    sub_i = ovl[:, :xs.shape[1]]
    sub_m = omask[:, :xs.shape[1]]
    sub_i[disc] = idx[disc]
    sub_m[disc] = True


def _draw_field(ovl, omask, W, H, yc, x_left, x_right, bz_col, bt_col, blocked):
    """The interplanetary field, as tilted dashes: a comb, not a curve.

    A field line integrated honestly across the panel leaves the top or the
    bottom within eighty columns and takes the whole idea with it, because a
    sustained southward Bz *is* a steady slope. So the field is drawn the way a
    wind field is drawn on a chart: short segments on a grid, each tilted by
    the local direction. Uniform field, and they line up into what reads as
    continuous lines; a rotation passing through, and the comb visibly turns.

    Colour is the message and it is binary on purpose: north is a cool blue,
    south is magenta. Brightness within each band is |Bz|/|B|, so a field that
    is strongly one way or the other shouts and a flat one recedes.
    """
    pitch_x = max(6, int(round(11 * W / 320.0)))
    dash = max(5, int(round(7 * W / 320.0)))
    rows = [int(round(yc + k * H / 5.4)) for k in (-2, -1, 0, 1, 2)]
    half = dash // 2

    for r_i, row in enumerate(rows):
        # Alternate rows are offset half a pitch. Aligned, the comb reads as
        # diagonal hatching across the whole panel -- a moire of its own that
        # fights the stream underneath it; staggered, it reads as field.
        stagger = (pitch_x // 2) if (r_i % 2) else 0
        for x0 in range(x_left + 3 + stagger, x_right - 2, pitch_x):
            b_z = float(bz_col[min(W - 1, x0)])
            b_t = max(1.0, float(bt_col[min(W - 1, x0)]))
            # Slope from the field's clock angle, clamped so a dash stays a
            # dash rather than becoming a vertical stroke.
            slope = float(np.clip(-b_z / max(3.0, b_t) * 0.9, -0.62, 0.62))
            share = min(1.0, abs(b_z) / b_t)
            base = FIELD_S if b_z < 0 else FIELD_N
            lvl = base + int(np.clip(11 + 20 * share, 0, 31))
            for k in range(-half, half + 1):
                x = x0 + k
                y = int(round(row + slope * k))
                if 0 <= x < W and 0 <= y < H and not blocked[y, x]:
                    ovl[y, x] = lvl
                    omask[y, x] = True


def _draw_magnetosphere(ovl, omask, W, H, yc, xe, scale, r0, alpha,
                        r0_quiet, alpha_quiet, ghost):
    """The bow shock and the magnetopause, as Shue curves, plus the cavity.

    Returns True if the quiet-day ghost was worth drawing, which is the panel's
    way of saying "this is a compressed magnetosphere" in one frame rather than
    requiring somebody to remember what it looked like yesterday.
    """
    # The inside first, so the curves paint over it. A cavity that is merely
    # black reads as a hole in the panel; a very dark blue reads as a volume.
    ys = np.arange(H, dtype=f32)[:, None] - yc
    xs = np.arange(W, dtype=f32)[None, :]
    dx_re = (xs - xe) / scale
    dy_re = -ys / scale
    rr = np.maximum(np.sqrt(dx_re ** 2 + dy_re ** 2), 1e-3)
    theta = np.arccos(np.clip(-dx_re / rr, -1.0, 1.0))
    inside = rr < shue_r(theta, r0, alpha)
    ovl[inside] = CAVITY + 1
    omask[inside] = True

    # The neutral sheet down the middle of the tail, which is where the field
    # reverses and where a substorm's X-line forms. One dim row, but it is what
    # makes the tail read as two lobes rather than one dark smear.
    tail = inside & (xs > xe + 2)
    ovl[yc, :][tail[yc, :]] = CAVITY + 4

    curves = ((1.3 * r0, alpha + 0.06, SHOCK + 5),        # bow shock, dimmer
              (r0, alpha, SHOCK + 12))                    # magnetopause
    ghosted = ghost and (r0_quiet - r0) * scale >= 3.0
    if ghosted:
        curves = ((r0_quiet, alpha_quiet, -1),) + curves

    th = np.linspace(-2.62, 2.62, 900).astype(f32)        # +-150 degrees
    for rad, alp, lvl in curves:
        r = shue_r(th, rad, alp)
        px = np.round(xe - r * np.cos(th) * scale).astype(np.int32)
        py = np.round(yc - r * np.sin(th) * scale).astype(np.int32)
        ok = (px >= 0) & (px < W) & (py >= 0) & (py < H)
        px, py = px[ok], py[ok]
        if lvl < 0:
            # Dotted, and only every third pixel: it is a reference, and a
            # solid second curve would read as a second boundary.
            px, py = px[::3], py[::3]
            ovl[py, px] = SHOCK + 2
        else:
            ovl[py, px] = lvl
        omask[py, px] = True
    return ghosted


def _draw_earth(ovl, omask, yc, xe, scale):
    """Four pixels of planet, drawn about four times oversized.

    At the panel's honest 2 px per Earth radius the Earth is two pixels across
    and an aurora is half of one. The magnetosphere around it is to scale; the
    planet in the middle of it is not, and that is the trade this panel makes
    to have poles at all.
    """
    r = max(2, int(round(3.0 * ovl.shape[0] / 64.0)))
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            d = math.hypot(dx, dy) / r
            if d > 1.0:
                continue
            y, x = yc + dy, xe + dx
            if 0 <= y < ovl.shape[0] and 0 <= x < ovl.shape[1]:
                # Lit from the left, because the Sun is on the left.
                lit = 0.35 - 0.5 * dx / r
                ovl[y, x] = EARTH + int(np.clip(1 + 6 * lit, 0, 7))
                omask[y, x] = True


def _aurora_pixels(W, H, yc, xe, scale, kp):
    """Flat indices for the auroral ovals, and a per-pixel weight.

    Seen from the side, the oval is two caps, one on each limb of each pole --
    four little arcs. Their latitude is the standard rule of thumb: the oval
    sits near 67 degrees when it is quiet and marches equatorward about two and
    a half degrees per unit of Kp, which is why people in Oregon see it during
    a G4 and people in Alaska see it every week. On this panel that march is
    two or three pixels, which is exactly enough to notice.
    """
    lat0 = 67.0 - 2.5 * (kp if kp is not None else 2.0)
    lat0 = float(np.clip(lat0, 52.0, 72.0))
    r = max(3.0, 3.0 * H / 64.0)
    ys, xs, wt = [], [], []
    for hemi in (1, -1):
        for lat in np.arange(lat0, 89.0, 2.5):
            for side in (1, -1):
                a = math.radians(lat)
                # Two rings: the oval proper and a dimmer glow just above it.
                for k, gain in ((0.0, 1.0), (1.0, 0.45)):
                    rr = r + k
                    y = int(round(yc - hemi * rr * math.sin(a)))
                    x = int(round(xe + side * rr * math.cos(a)))
                    if 0 <= y < H and 0 <= x < W:
                        ys.append(y)
                        xs.append(x)
                        # Brightest at the poleward edge of the oval, which is
                        # where the discrete arcs are.
                        wt.append(gain * (0.45 + 0.55 * (lat - lat0) / 25.0))
    if not ys:
        return np.zeros(0, np.intp), np.zeros(0, np.uint8)
    flat = np.array(ys, np.intp) * W + np.array(xs, np.intp)
    base = np.clip(np.array(wt, f32) * 31.0, 1, 31).astype(np.uint8)
    return flat, base


def _spark_path(W, H, yc, xe, scale, r0, alpha):
    """The magnetopause from the nose to the flanks, as a pixel path.

    Reconnection knots travel along this, so it is sampled by *angle* rather
    than by arc length: they start slowly at the subsolar point and accelerate
    down the flank, which is what the real ones do.
    """
    th = np.linspace(0.05, 2.30, 90).astype(f32)
    r = shue_r(th, r0, alpha)
    px = np.round(xe - r * np.cos(th) * scale).astype(np.int32)
    py_n = np.round(yc - r * np.sin(th) * scale).astype(np.int32)
    py_s = np.round(yc + r * np.sin(th) * scale).astype(np.int32)
    ok = (px >= 0) & (px < W - 1)
    if not ok.any():
        return None
    return py_n[ok], px[ok], py_s[ok]


def _plate(ovl, omask, x, y, w, h):
    """A black backing rectangle, one pixel proud of the type it sits behind.

    Only under the two labels that live over the magnetosphere: the cavity is
    nearly black already, so the plate is invisible there, and without it the
    magnetopause runs straight through the middle of a glyph and turns STALE
    into something that is not a word. The type over the stream gets no plate,
    because a black box in the middle of the plasma reads as a hole in it.
    """
    x0, y0 = max(0, int(x) - 1), max(0, int(y) - 1)
    x1, y1 = min(ovl.shape[1], int(x + w) + 1), min(ovl.shape[0], int(y + h) + 1)
    if x1 > x0 and y1 > y0:
        ovl[y0:y1, x0:x1] = 0
        omask[y0:y1, x0:x1] = True


def _draw_text(ovl, omask, W, H, x_left, xe, scale, wind, kp, kp_state):
    """Three numbers and an age. Everything else on the panel is drawn.

    Speed and Bz are quoted from the newest sample, which is what SWPC and
    every other space weather page mean by "now" -- it is the left-hand end of
    this panel, and it is where the type sits, so the number is next to the
    plasma it describes rather than floating over the middle of the picture.
    """
    latest = (wind.payload or {}).get("latest") or {}
    speed = latest.get("speed")
    bzv = latest.get("bz")
    if speed is None and wind.speed:
        speed = wind.speed[-1]
    if bzv is None and wind.bz:
        bzv = wind.bz[-1]
    # An override has to be what the panel prints, or the picture and the
    # caption disagree and the caption wins.
    if wind.speed:
        speed = wind.speed[-1]
    if wind.bz:
        bzv = wind.bz[-1]

    x0 = x_left + 2
    if wind.quote and speed is not None:
        w = stamp(ovl, omask, x0, 3, "%d" % int(round(speed)), INK)
        stamp(ovl, omask, x0 + w + 3, 3, "KM/S", LABEL)
    else:
        stamp(ovl, omask, x0, 3, "-- KM/S", DIM)

    if wind.quote and bzv is not None:
        col = ALERT if bzv <= -5 else (WARN if bzv < 0 else GOOD)
        stamp(ovl, omask, x0, 11, "BZ", LABEL)
        stamp(ovl, omask, x0 + text_w("BZ") + 3, 11, "%+.0f" % bzv, col)
    else:
        stamp(ovl, omask, x0, 11, "BZ --", DIM)

    # Kp goes over the Earth, because it is the Earth's number: it is what the
    # magnetosphere is doing, not what the wind is doing.
    if kp is not None and kp_state in (FRESH, AGING):
        col = KP0 + int(min(9, max(0, math.floor(kp))))
        label = "KP%d" % int(round(kp))
        # Downstream of the Earth, in the northern tail lobe: dark, always
        # inside the cavity at any standoff distance, and nowhere near the
        # curves. Over the Earth itself it collided with the magnetopause on
        # exactly the compressed days when it most needed reading.
        x = int(min(W - text_w(label) - 2, xe + max(6, int(5 * scale))))
        y = max(0, H // 2 - 11)
        _plate(ovl, omask, x, y, text_w(label), 5)
        stamp(ovl, omask, x, y, label, col)

    # The age, always, in ftdata's short form: bottom right, dim when fresh and
    # loud when not, because it is the only thing here that says whether any of
    # the rest is true.
    if wind.synthetic:
        age, col = "SIM", WARN
    elif wind.age is not None:
        age = ftdata.describe_age(wind.age)
        col = (DIM if wind.state == FRESH
               else WARN if wind.state == AGING else ALERT)
        if wind.state == STALE:
            age = "STALE " + age
    else:
        age = None
    if age:
        _plate(ovl, omask, W - text_w(age) - 2, H - 6, text_w(age), 5)
        stamp(ovl, omask, W - text_w(age) - 2, H - 6, age, col)
    stamp(ovl, omask, x_left + 1, H - 6, "L1", DIM)


def _no_data(args, W, H, out_rgb):
    """No record at all: say so, name the fetcher, give the command.

    A blank panel looks like a broken wall and a panel of zeros looks like a
    dead calm sun, which is a lie. This looks like neither.
    """
    pal = build_palette()
    idx = np.zeros((H, W), np.uint8)
    mask = np.zeros((H, W), np.bool_)
    for x0, y0, x1, y1 in ((0, 0, W, 1), (0, H - 1, W, H),
                           (0, 0, 1, H), (W - 1, 0, W, H)):
        idx[y0:y1, x0:x1] = ALERT

    title = "NO DATA"
    sc = 4
    while sc > 1 and text_w(title, sc) > W - 12:
        sc -= 1
    y = max(3, H // 2 - 5 * sc - 6)
    stamp(idx, mask, (W - text_w(title, sc)) // 2, y, title, ALERT, sc)

    cache = args.cache_dir or ftdata.CACHE_DIR
    lines = ["SOLAR WIND CACHE IS EMPTY",
             "RUN: PYTHON3 FTDATA.PY --LOOP 900", cache.upper()]
    yy = y + 5 * sc + 5
    for line in lines:
        while line and text_w(line) > W - 6:
            line = line[:-1]
        stamp(idx, mask, (W - text_w(line)) // 2, yy, line, LABEL)
        yy += 7
        if yy + 5 > H - 2:
            break

    blink = np.array(idx, np.uint8)
    blink[idx == ALERT] = DIM

    def render(t, frame=0):
        # One square wave, so the wall looks like an alarm rather than a
        # caption. Two distinct frames, both baked.
        np.take(pal, idx if (t % 1.4) < 0.9 else blink, axis=0, out=out_rgb)
        return out_rgb
    return render


def main():
    # 20 fps: the stream is the whole demo and it is a scroll, so it wants a
    # real frame rate, but nothing here moves faster than a few pixels a frame
    # and 30 would spend a third more of the wall's UDP on nothing.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
