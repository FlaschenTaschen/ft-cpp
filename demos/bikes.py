#!/usr/bin/env python3
"""Today's bike traffic against the shape of a typical one, hour by hour.

One day, left to right: midnight to midnight across 320 columns. The dark blue
silhouette is **what this weekday normally looks like** -- the two commute
peaks, the midday trough between them, the long tail into the evening -- built
from three months of Bay Wheels' own published trip records. The bright gold
line drawn over it is **today, so far**. A lit vertical rule says where *now*
is, and everything to the right of it is what the rest of the day usually does.

That is the whole idea, and it is the third design this panel has had. The
first was a hillside of 383 docks coloured by occupancy, which showed *state*
and not activity. The second replayed twelve hours of inferred flow over a
distance-and-altitude cross-section, which was a good instrument and did not
communicate anything in the two seconds somebody spends walking past. What was
actually asked for was "the patterns of the day, and where we are in the
cycle", and this is that question drawn literally: one familiar shape, and a
line moving along it.

--------------------------------------------------------------------------
THE HONESTY PROBLEM, WHICH IS THE WHOLE OF THE DESIGN
--------------------------------------------------------------------------

**The silhouette is measured. Today's line is estimated.** They are not the
same kind of number and this panel is arranged so that nobody can mistake one
for the other.

Bay Wheels publishes a monthly CSV of every trip taken -- start time, end time,
which dock at each end -- and that is a *census*. The silhouette is built from
it offline and committed as `bikes-typical.npz`; see `bake_typical()`.

The live feed publishes no trips at all. GBFS is a snapshot: how many bikes are
in each dock right now. Everything this panel knows about today comes from
differencing those counts every ten minutes, which sees a **floor** on movement
and not a trip count -- two riders swapping a dock inside one window cancel, a
rebalancing van looks like fifteen riders, and the several hundred free-floating
ebikes that never touch a dock are invisible to it entirely.

So how do you put those two on one axis without lying? Three options were
available and the third was chosen:

  1. *Plot both in absolute trips and print a percentage difference.* Rejected.
     It claims a commensurability that had not been established, and the
     percentage would have been a made-up figure with two significant digits.
  2. *Normalise both to their own peak and compare shape only.* Rejected, but
     not easily: it is honest, and shape is most of what was asked for. It
     fails on the mechanics -- today's own peak is not known until the day has
     ended, so at nine in the morning the normalisation has nothing to divide
     by, and any substitute for it reintroduces exactly the assumption it was
     supposed to avoid.
  3. **Calibrate the estimator against the archive, and disclose the
     calibration on the panel.** Chosen.

**The calibration, and how it was measured.** The archive has both halves of
the comparison in it. Every trip in it can be replayed as the two dock-count
changes it would have caused -- minus one at the dock it left when it left,
plus one at the dock it reached when it reached it -- so the estimator can be
*run on the census*, ten-minute bucket by ten-minute bucket, and its output
compared against the true number of trips in the same bucket. Over 92 days in
May, June and July 2026 the ratio is:

    true trips / dock-count moves = 1.83   (median day; 1.72 to 1.94, p10-p90)

and it varies with the hour of the day, from about 1.3 at four in the morning
to about 2.2 at five in the afternoon -- busier hours cancel more inside a
bucket, and the free-floating ebike share moves too. Today's line is the live
estimator multiplied by that hour-of-day factor, and **the panel prints the
factor**: `EST FROM DOCK COUNTS X1.9`. A viewer who reads that line knows
exactly how much of the number is measurement and how much is arithmetic, which
is a better disclosure than an error bar they cannot check.

**What the calibration does not cover, and why there is no percentage on the
panel.** It was measured by simulating the estimator on the archive, not by
comparing live GBFS against the archive -- nobody has months of paired
snapshots, so that comparison cannot be made today. The live estimator sees
things the simulation cannot: vans, bikes going out of service, a one-minute
feed sampled every ten. So 5.9% is the *measured* spread of the calibration and
a floor on its real error, not the error.

Against that, the day-to-day spread of the real thing is remarkably small: the
middle half of thirteen Mondays' daily totals is within about 3% of the median.
The estimator's uncertainty is therefore roughly twice the signal that a
percentage would be reporting. Printing "+12% vs a typical Monday" would be
reporting noise to two digits. So the panel prints no percentage. It prints a
**verdict word** -- `BUSIER THAN USUAL`, `QUIETER THAN USUAL`, `USUAL FOR A
MONDAY` -- and it only leaves the middle verdict when today is outside the
typical range *widened by the calibration's own uncertainty*. On an ordinary
day it says the ordinary thing, which is the correct output.

--------------------------------------------------------------------------
WHY IT LOOKS LIKE THIS AND NOT LIKE CAISO
--------------------------------------------------------------------------

`caiso.py` is also one day across 320 columns with now marked, so the two had
to be told apart from across a room, and they are, three ways. caiso is
**full-bleed**: a stacked area of five saturated fuels that fills the panel edge
to edge and top to bottom, and its subject is the *composition* of a total.
This one is **mostly black**: a single dark blue silhouette that touches the top
of the chart for about twenty minutes a day, one gold line, and nothing else --
its subject is *one quantity against its own history*. caiso's palette is five
hues at once; this is two, and one of them is nearly the background. And caiso
is a picture of a single day, where this one draws two days at the same time,
which is what the silhouette-plus-line form exists to do.

The `p10`-to-`p90` crust along the top of the silhouette is the one piece of
statistical furniture. It is drawn a shade brighter than the fill and it is
where the day-to-day variation actually lives: two rows wide at the morning
peak, six in the middle of the afternoon, which says something true about when
this city is predictable and when it is not.

--------------------------------------------------------------------------
FOUR STATES, ALL DELIBERATE
--------------------------------------------------------------------------

**Cold start is the interesting one, and it is designed rather than tolerated.**
The silhouette is baked, so it is on the panel in the first frame after a fresh
install -- there is no version of this demo that shows an empty rectangle.
Today's line is the opposite: it begins at local midnight with nothing and
fills in as the day goes, and on the day the fetcher is first started it begins
only from when it started. The record carries one slot per ten minutes with a
null in every slot that was never fetched, so the panel knows the difference
between "nothing happened" and "nobody was looking", draws the line only where
it was looking, and changes the headline from `TRIPS TODAY` to `TRIPS SINCE
3:10P`. The comparison follows it: today's estimate is only ever compared
against the typical day *over the same slots*, so a panel that has been up for
two hours makes a two-hour comparison and says so.

**Stale.** Past the half-hour TTL the header says `STALE` with the age, and the
gold line simply stops where the data stopped while the now-rule keeps moving,
so the gap between the two is visible on the panel. That is deliberately not a
refusal: this chart's whole shape is the day so far, and a day so far that ends
an hour early is still true.

**Yesterday.** A record whose day does not match the local date gets no line at
all -- a day-shaped picture of the wrong day is the one lie this panel could
tell -- and the headline says which day it is from.

**Absent.** No live record, but the asset is there: the silhouette and the
now-rule are drawn, the headline says `NO LIVE DATA` and the command that fixes
it, and there is no gold line to mistake for one. Only a missing *asset* gets
the plain no-data card.

--------------------------------------------------------------------------
FRAME BUDGET
--------------------------------------------------------------------------

Everything is rasterised once per cache read: the silhouette, the crust, the
gold line, the gridlines, the axis labels, the header and all five strings of
the headline strip. `render()` copies one frame, writes a comet of two dozen
pixels along the gold line, and draws the now-rule and the pulse running up it
-- about seven numpy calls on top of the copy. On the wall's Pi a numpy call
costs tens of microseconds whatever the array size, so the call count is the
budget and not the pixel count. Measured here over a full loop: see the README.

Run:  python3 ftdata.py --once --only baywheels   # twice, ten minutes apart
      python3 bikes.py --host 127.0.0.1
      python3 bikes.py --at '2026-08-10 08:40'    # pretend it is the peak
      FT_DATA_CACHE=/tmp/empty python3 bikes.py   # the typical day alone
      python3 scripts/test-bikes.py
      python3 bikes.py --bake-typical 202605 202606 202607   # offline, once
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

PRODUCT = "baywheels"

# The committed archive bake. Three months of Bay Wheels' published trip
# records reduced to two matrices per weekday; see bake_typical() at the bottom
# of this file for what is in it and how it was made.
ASSET = "bikes-typical.npz"

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, tide, propagation and sort
# draw with: five rows a glyph, each row an octal digit whose three bits are
# the three columns. Anything from a real typeface is mush at five pixels, and
# the Pi does not have the same faces installed as the machine this was
# written on. Two glyphs are added, for the two signs a meter needs and a map
# of a nuclear exchange does not.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT.update({"%": "51245", "+": "02720"})

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


def text_height(scale=1):
    """Measured, not assumed. A past bug in this tree clipped the bottom off
    every capital E because five rows was guessed rather than asked for."""
    return _GLYPHS[" "].shape[0] * scale


def blit_text(dst, y, x, s, rgb, scale=1, halo=0.0):
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn.

    `halo` darkens the one-pixel border around every stroke by that factor
    before the stroke is drawn. It is what lets the axis labels and the scale
    tick sit over the silhouette instead of having to dodge it: the darkening
    follows the letterforms rather than leaving a rectangle behind them, so on
    the black sky where most of the type lands it is invisible.
    """
    m = text_mask(s, scale)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    if halo > 0.0:
        pad = np.zeros((gh + 2, gw + 2), bool)
        pad[1:-1, 1:-1] = m
        grow = (pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2]
                | pad[1:-1, 2:] | m)
        sub = grow[y0 - y:y1 - y, x0 - x:x1 - x]
        reg = dst[y0:y1, x0:x1]
        reg[sub] = (reg[sub].astype(f32) * (1.0 - halo)).astype(np.uint8)
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    dst[y0:y1, x0:x1][sub] = rgb
    return gw


def fit(dst, y, x_right, forms, rgb, scale=1, halo=0.0, floor=0):
    """Draw the first of `forms` that fits, right-aligned at `x_right`.

    tide.py's ladder-of-shorter-forms, which every panel in this tree that has
    to put a sentence on a 320 pixel row ends up needing. Returns the left edge
    it drew at, or None if even the shortest form would collide with `floor`.
    """
    for s in forms:
        if not s:
            continue
        x = x_right - text_width(s, scale)
        if x >= floor:
            blit_text(dst, y, x, s, rgb, scale, halo)
            return x
    return None


# --------------------------------------------------------------------------
# Colour.
#
# Two colours carry meaning and everything else is furniture. **Blue is the
# past**: the silhouette of a typical weekday, dark enough that it reads as
# ground rather than as data, with the day-to-day spread a shade brighter along
# its top edge. **Gold is today**, and it is the only warm thing on the panel,
# which is what makes it findable from across a room without a legend.
#
# The silhouette is deliberately dim. It is context, it covers a third of the
# panel, and the failure mode of a chart like this is that the background
# competes with the line drawn over it. C_BAND at (26,34,52) is about a tenth
# of full brightness -- visible as a shape, never as a subject.
# --------------------------------------------------------------------------

C_BAND = (26, 34, 52)            # a typical day for this weekday, filled
C_CRUST = (54, 72, 106)          # ...and the 10th-to-90th spread on top of it
C_TODAY = (255, 186, 66)         # today, so far
C_TODAY_HALO = (86, 60, 18)      # one row either side, so the line has body
C_TODAY_HEAD = (255, 242, 208)   # the leading edge of the line

C_TEXT = (198, 210, 222)
C_DIM = (86, 98, 112)
C_FAINT = (44, 52, 64)
C_GRID = (22, 28, 36)
C_GRID_HI = (38, 46, 58)
C_SEP = (14, 18, 24)
C_WARN = (255, 96, 72)
C_NOW = (255, 246, 214)
C_NOW_DIM = (120, 112, 88)
C_BUSY = (255, 206, 96)          # busier than usual: the gold, brightened
C_QUIET = (128, 176, 232)        # quieter than usual: cool, never a rebuke

# How far the one-pixel border around each stroke is darkened where text lands
# on the silhouette.
HALO = 0.72

# The comet that runs along today's line, in samples and in seconds a pass.
# It exists because a panel between two animated demos that holds one frame
# reads as a crash, and because a light travelling along the line is the one
# animation that says what the line *is* -- the day, moving. Twenty-six samples
# at four and a half minutes a column is about two hours of city.
COMET_LEN = 26
COMET_S = 3.6

# How fast the pulse runs up the now-rule, in hertz. The same trick and the
# same reason as caiso.py's: it is the only thing on the panel guaranteed to
# move in every single frame.
PULSE_HZ = 1.3

# Ten-minute slots in a day. The record's own resolution and the archive's.
SLOTS = 144

# Hours to rule and label. Six, not three: on a panel that is mostly black,
# eight vertical lines is furniture competing with the two things that matter.
HOUR_RULES = (0, 6, 12, 18)

# The least coverage a headline will call "today" rather than naming the hour
# it starts at, and the least it will name an hour for rather than admitting to
# holes. A missed pass or two out of a hundred and forty-four is an ordinary
# day; a third of the day missing is a different claim.
COVER_FULL = 0.85
COVER_PART = 0.70

# The verdict never fires on less than this many hours of covered day. Two
# hours of a Monday morning is a hundred and fifty trips and the difference
# between a busy day and a quiet one is not visible in it yet.
VERDICT_MIN_H = 2.0

# ...and never on a difference smaller than this, whatever the arithmetic says.
# The calibration's own measured spread is 5.9% and the day-to-day spread of
# the real thing is about 3%, so anything under ten per cent is the estimator
# talking about itself. See the docstring.
VERDICT_FLOOR = 0.10


# --------------------------------------------------------------------------
# Clock. Everything asks for `now` rather than reading the system clock, which
# is what makes a contact sheet across a whole day possible: --at moves the
# demo's idea of the present and --rate runs it fast. The same shape as
# caiso.py's and tide.py's.
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


def hhmm(epoch, ampm=True):
    """A compact local-time label: '5:47P' or '17:47'."""
    lt = time.localtime(epoch)
    if not ampm:
        return time.strftime("%H:%M", lt)
    h = lt.tm_hour % 12 or 12
    return "%d:%02d%s" % (h, lt.tm_min, "A" if lt.tm_hour < 12 else "P")


def hour_label(hour, ampm=True):
    """An axis tick: '12A', '6A', '12P', '6P'."""
    if not ampm:
        return "%02d" % (hour % 24)
    h = hour % 24
    return "%d%s" % (h % 12 or 12, "A" if h < 12 else "P")


def day0_of(now):
    """The epoch of the local midnight that starts the day containing `now`.

    Identical to the fetcher's `_bikes_day0`, and it has to be: the record's
    slots are indexed from it and the panel's axis is drawn from it, and the
    two disagreeing by an hour twice a year would put the whole day one
    thirteenth of a panel out of place.
    """
    lt = time.localtime(now)
    return float(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                              0, 0, 0, 0, 0, -1)))


WEEKDAY = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
           "SATURDAY", "SUNDAY")
WEEKDAY_SHORT = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


# --------------------------------------------------------------------------
# The archive bake.
#
# `bikes-typical.npz` holds, per weekday, two matrices of one row per date and
# 144 ten-minute columns: `t<wd>` is the trips that actually happened, and
# `e<wd>` is what the dock-count estimator *would have reported* for the same
# bucket had it been running. Everything the panel needs is derived from those
# two here rather than baked, because the derivation is a dozen operations on
# arrays of thirteen by a hundred and forty-four and because keeping the raw
# counts in the asset is what makes the numbers on the panel checkable.
# --------------------------------------------------------------------------

def asset_path(name=None):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        name or ASSET)


def read_typical(path=None):
    """The baked archive, or (None, problem). numpy only; never touches a net."""
    path = asset_path(path)
    try:
        with np.load(path, allow_pickle=False) as z:
            out = {"months": [str(m) for m in z["months"]],
                   "span": [str(s) for s in z["span"]],
                   "bucket": float(z["bucket"][0])}
            for wd in range(7):
                t = z["t%d" % wd].astype(f32)
                e = z["e%d" % wd].astype(f32)
                if t.shape != e.shape or t.shape[1] != SLOTS or not len(t):
                    return None, "bikes-typical.npz has no usable weekday %d" % wd
                out["t%d" % wd], out["e%d" % wd] = t, e
    except Exception as exc:                                 # noqa: BLE001
        return None, "cannot read %s (%r)" % (os.path.basename(path), exc)
    return out, None


def smooth(rows, width=3):
    """A centred moving mean along the slot axis, edges held.

    Half an hour, applied to each *date* before the percentiles are taken. A
    single ten-minute bucket of a single Monday is a couple of hundred trips at
    the peak and a dozen at four in the morning, so its sampling noise is
    comparable to the day-to-day variation the band is supposed to be showing;
    smoothing first is what stops the band being mostly Poisson. It costs the
    morning peak about two per cent of its height, which is a smaller lie than
    a band twice as wide as the truth.
    """
    if width <= 1:
        return rows
    k = np.ones(width, f32) / float(width)
    pad = width // 2
    wide = np.concatenate([np.repeat(rows[:, :1], pad, axis=1), rows,
                           np.repeat(rows[:, -1:], pad, axis=1)], axis=1)
    out = np.empty_like(rows)
    for i in range(len(rows)):
        out[i] = np.convolve(wide[i], k, mode="valid")
    return out


def profile(z, wd, span=(10.0, 50.0, 90.0)):
    """(lo, med, hi) trips per hour for weekday `wd`, over 144 slots.

    Percentiles across the dates of that weekday, after each date has been
    smoothed. Ten and ninety rather than the quartiles, and that is a drawing
    decision as much as a statistical one: the middle half of thirteen Mondays
    is within about three per cent of the median at the morning peak, which on
    a forty row chart is a band one pixel high and reads as a rendering
    artefact. The tenth to ninetieth is two rows there and six in the middle of
    the afternoon, which is both visible and a true statement about when this
    city is predictable.
    """
    t = smooth(z["t%d" % wd])
    lo, med, hi = np.percentile(t, span, axis=0)
    scale = 3600.0 / (z["bucket"] or 600.0)
    return (lo.astype(f32) * scale, med.astype(f32) * scale,
            hi.astype(f32) * scale)


def calibration(z, wd):
    """(k_by_hour, spread) -- trips per unit of dock-count movement.

    The estimator run on the census, hour by hour: how many trips really
    happened for every one unit of `sum |change| / 2` the dock counts would
    have shown. Pooled over weekdays and weekends separately rather than over
    the seven days individually, because thirteen dates is thin for a
    twenty-four point curve and a Tuesday and a Wednesday have no reason to
    differ; a Saturday and a Tuesday plainly do.

    `spread` is the half-width of the tenth-to-ninetieth of the *daily* ratio
    across every date in the bake, relative to its median -- the measured
    day-to-day instability of the calibration itself, and the number the
    verdict threshold is built out of.
    """
    group = range(5) if wd < 5 else (5, 6)
    t = np.concatenate([z["t%d" % d] for d in group])
    e = np.concatenate([z["e%d" % d] for d in group])
    hours = t.shape[1] // 24
    th = t.reshape(len(t), 24, hours).sum(axis=(0, 2))
    eh = e.reshape(len(e), 24, hours).sum(axis=(0, 2))
    k = np.where(eh > 0, th / np.maximum(eh, 1e-6), 1.0).astype(f32)

    every_t = np.concatenate([z["t%d" % d].sum(1) for d in range(7)])
    every_e = np.concatenate([z["e%d" % d].sum(1) for d in range(7)])
    daily = every_t / np.maximum(every_e, 1e-6)
    p10, p50, p90 = np.percentile(daily, (10.0, 50.0, 90.0))
    return k, float((p90 - p10) * 0.5 / max(p50, 1e-6))


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# `load()` hands back a payload and an age and never raises, so everything that
# can still be wrong is wrong about *content* and is caught here. What this
# panel wants out of the record is small: the day the slots belong to, and two
# columns of 144 numbers. Everything else in the baywheels record -- the
# stations, the flow field, the rolling twelve hours -- belongs to a question
# this panel is not asking.
# --------------------------------------------------------------------------

def read_today(cache_dir):
    """(record, age, problem). `record` is None if there is nothing drawable."""
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached bay wheels record"
    payload, age = got
    day = payload.get("today")
    if not isinstance(day, dict):
        return None, age, "record has no day slots - fetcher is out of date"
    try:
        day0 = float(day["day0"])
        bucket = float(day.get("bucket") or 600.0)
        mov = day["mov"]
        dt = day["dt"]
    except (KeyError, TypeError, ValueError):
        return None, age, "day slots are malformed"
    if not (isinstance(mov, list) and isinstance(dt, list)
            and len(mov) == len(dt) == SLOTS):
        return None, age, "day slots are the wrong length"

    # None means "no pass landed in this slot" all the way through, and NaN is
    # how that survives arithmetic without a branch at every step.
    m = np.array([np.nan if v is None else float(v) for v in mov], f32)
    d = np.array([np.nan if v is None else float(v) for v in dt], f32)
    # A slot with a movement and no interval cannot be turned into a rate, and
    # a slot with an interval outside what the fetcher will difference is one
    # the fetcher itself refused; both are absences and not zeroes.
    bad = ~np.isfinite(d) | (d <= 0.0)
    m[bad] = np.nan
    d[bad] = np.nan
    return {"day0": day0, "bucket": bucket, "mov": m, "dt": d,
            "age": age, "as_of": float(payload.get("as_of") or 0.0),
            "totals": dict(payload.get("totals") or {})}, age, None


def estimate(rec, k_hour):
    """Today's slots turned into an estimated trip rate. The one inference.

    Returns a dict with, over the 144 slots:

      rate     estimated trips per hour, NaN where nothing was measured
      cover    seconds of each slot a measurement actually spans
      seen     boolean: cover > 0, for drawing
      trips    estimated trips *in* the slot, for the cumulative total
      secs     seconds of the day actually covered

    **Coverage is counted in seconds and not in slots**, which is not a detail.
    A difference is as long as the gap between the two passes that made it, and
    that is only ten minutes if the fetcher is on its ten minute timer -- run it
    every five and each slot is half measured. Marking the whole slot as seen
    and then comparing today's five minutes against the archive's ten is a
    silent halving of the headline, and it is exactly what the first version of
    this function did; it showed up as a live estimate landing at 0.83 of the
    archive when the arithmetic said it should land near 1.6.

    Two things happen here and both matter.

    **A measurement is spread over the interval it covers, not over the slot it
    was written into.** The fetcher writes each difference into the slot the
    pass landed in, along with the seconds that difference actually spans. A
    missed pass makes the next one forty minutes long instead of ten; charging
    that to a single slot would draw a spike with three holes beside it, when
    what was measured is a flat rate over forty minutes. So the rate goes into
    every slot the interval touches, and those slots count as covered -- which
    they are.

    **The rate is multiplied by the hour's calibration factor.** `mov` is the
    sum of |change| over the stations, so `mov / 2` is docked bikes that
    changed place; times 3600/dt is an hourly rate; times k is the estimated
    *trips*, including the free-floating ebike traffic that never touches a
    dock. See the module docstring for where k comes from and what it does not
    cover.
    """
    rate = np.full(SLOTS, np.nan, f32)
    trips = np.zeros(SLOTS, f32)
    cover = np.zeros(SLOTS, f32)
    bucket = rec["bucket"] or 600.0
    edge = np.arange(SLOTS + 1, dtype=f32) * bucket
    idx = np.flatnonzero(np.isfinite(rec["mov"]))
    for i in idx:
        dt = float(rec["dt"][i])
        moved = float(rec["mov"][i]) * 0.5
        # The interval ends at the end of the slot it was written into and
        # reaches back `dt` seconds. Clipped at midnight: a difference that
        # straddles it describes yesterday as much as today, and yesterday is
        # not on this axis.
        t1 = (i + 1) * bucket
        t0 = max(0.0, t1 - dt)
        a = int(t0 // bucket)
        b = min(SLOTS, int(math.ceil(t1 / bucket)))
        if b <= a:
            continue
        hour = min(23, int((t0 + t1) * 0.5 // 3600.0))
        est = moved * float(k_hour[hour])
        span = max(t1 - t0, 1.0)
        rate[a:b] = est * 3600.0 / span
        # How much of each slot this interval really spans. Whole slots in the
        # middle, part slots at either end.
        cover[a:b] += np.maximum(np.minimum(edge[a + 1:b + 1], t1)
                                 - np.maximum(edge[a:b], t0), 0.0)
        # The cumulative is the estimate itself and not the rate integrated
        # over the slots it was painted into, so that a forty minute interval
        # contributes what it measured exactly once. It is booked to the first
        # slot of its own span; the intervals tile rather than overlap, because
        # each difference starts where the previous one ended.
        trips[a] += est
    np.minimum(cover, bucket, out=cover)
    return {"rate": rate, "seen": cover > 0.0, "cover": cover,
            "trips": trips, "secs": float(cover.sum()), "bucket": bucket}


def coverage(est, now_slot):
    """(first, last, fraction) of the elapsed day the estimate actually covers.

    The fraction is of *seconds*, not of slots: a fetcher on a five minute timer
    covers every slot and half of each one, and calling that a fully observed
    morning would make the headline half what it should be.
    """
    seen = np.flatnonzero(est["seen"][:max(1, now_slot + 1)])
    if not len(seen):
        return None, None, 0.0
    first, last = int(seen[0]), int(seen[-1])
    elapsed = max(1, now_slot + 1 - first) * est["bucket"]
    return first, last, min(1.0, float(est["cover"][first:now_slot + 1].sum())
                            / elapsed)


def verdict(est, prof_rows, k_spread, now_slot):
    """(word, colour) for how today compares with the typical day so far.

    Compared over exactly the slots that were measured, never over the elapsed
    day: two hours of coverage makes a two-hour comparison. `prof_rows` is the
    per-date matrix for this weekday so the typical total over an arbitrary set
    of slots is a real sum over real days rather than a sum of medians.

    The threshold is the typical spread and the calibration's spread added
    together, floored at ten per cent -- see VERDICT_FLOOR. It is not a
    confidence interval and is not claimed as one; it is the smallest
    difference this instrument can see, and below it the panel says the day is
    ordinary because that is what it knows.
    """
    frac = est["cover"] / max(est["bucket"], 1.0)
    frac[now_slot + 1:] = 0.0
    sel = frac > 0.0
    if float(est["cover"][sel].sum()) < VERDICT_MIN_H * 3600.0:
        return "TOO EARLY TO SAY", C_DIM
    mine = float(est["trips"][sel].sum())
    # Weighted by how much of each slot was actually watched, so that half an
    # hour of coverage is compared against half an hour of history and not
    # against the hour it sits inside.
    theirs = (prof_rows[:, sel] * frac[sel]).sum(axis=1)   # one per past date
    p10, p50, p90 = np.percentile(theirs, (10.0, 50.0, 90.0))
    if p50 <= 0:
        return "TOO EARLY TO SAY", C_DIM
    tol = max(VERDICT_FLOOR, (p90 - p10) * 0.5 / p50 + k_spread)
    if mine > p50 * (1.0 + tol):
        return "BUSIER THAN USUAL", C_BUSY
    if mine < p50 * (1.0 - tol):
        return "QUIETER THAN USUAL", C_QUIET
    return None, C_DIM                              # "usual", said by the caller


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--asset", default=None,
                    help="the baked archive to draw the typical day from")
    ap.add_argument("--peak", type=float, default=0.0,
                    help="full scale in trips per hour (0 = fit the day)")
    ap.add_argument("--weekday", type=int, default=-1,
                    help="draw this weekday's typical day (0=Mon, -1=today)")
    ap.add_argument("--reveal", type=float, default=1.6,
                    help="seconds today's line takes to draw itself in "
                         "(0 = off)")
    ap.add_argument("--comet", type=float, default=COMET_S,
                    help="seconds for the light to run along today's line "
                         "(0 = off)")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second")
    ap.add_argument("--reload", type=float, default=300.0,
                    help="seconds between re-reads of the cache (0 = never)")


# --------------------------------------------------------------------------
# Layout. Four regions down a 64 row panel, and the order they give way in on a
# shorter one is: the axis labels, then the second line of the headline strip,
# then the header. The chart is the demo and it is the last thing to go.
# --------------------------------------------------------------------------

class Layout(object):

    def __init__(self, w, h):
        self.w, self.h = w, h
        th = text_height()
        self.head_h = th + 1 if h >= 30 else 0          # header row + separator
        self.strip_h = text_height(2) + 1 if h >= 44 else 0
        self.axis_h = th + 2 if h >= 24 else 0          # rule + labels
        self.strip_y = self.head_h
        self.chart_y = self.strip_y + self.strip_h
        self.axis_y = h - self.axis_h if self.axis_h else h
        self.chart_bot = self.axis_y - 1
        self.chart_h = self.chart_bot - self.chart_y + 1
        while self.chart_h < 10 and (self.strip_h or self.head_h):
            if self.strip_h:
                self.strip_h = 0
            else:
                self.head_h = 0
            self.strip_y = self.head_h
            self.chart_y = self.strip_y + self.strip_h
            self.chart_bot = self.axis_y - 1
            self.chart_h = self.chart_bot - self.chart_y + 1
        self.chart_h = max(2, self.chart_h)

    def col_of(self, seconds_into_day):
        return int(np.clip(seconds_into_day / 86400.0 * self.w, 0, self.w - 1))


# --------------------------------------------------------------------------
# Baking the picture. All of this happens once per cache read.
# --------------------------------------------------------------------------

def slot_to_col(values, w):
    """A 144 slot series onto `w` panel columns, at column centres."""
    xs = (np.arange(w, dtype=f32) + 0.5) * (SLOTS / float(w))
    return np.interp(xs, np.arange(SLOTS, dtype=f32) + 0.5,
                     values).astype(f32)


def rows_of(values, lay, scale):
    """Trips per hour to panel rows, clipped into the chart."""
    f = np.clip(np.asarray(values, f32) / max(scale, 1.0), 0.0, 1.0)
    return np.round(lay.chart_bot - f * (lay.chart_h - 1)).astype(np.int32)


def fill_between(dst, lay, top, bot, mask, rgb):
    """One vectorised pass over the chart region between two row arrays.

    A Python loop over three hundred and twenty columns is a tenth of a second
    on the wall's Pi even once, and this runs again on every cache read.
    """
    reg = dst[lay.chart_y:lay.chart_bot + 1]
    rows = np.arange(lay.chart_h)[:, None] + lay.chart_y
    sel = (rows >= top[None, :]) & (rows <= bot[None, :]) & mask[None, :]
    reg[sel] = rgb


def bake_furniture(dst, lay, h24, scale, tick):
    """Gridlines, the scale tick and the hour labels: everything that is
    there whether or not any data arrived."""
    w = lay.w
    for hour in HOUR_RULES:
        c = int(hour / 24.0 * w)
        if 0 <= c < w:
            dst[lay.chart_y:lay.chart_bot + 1, c] = (
                C_GRID_HI if hour == 12 else C_GRID)

    # One horizontal rule, at a round number of trips an hour, labelled. A
    # chart with no unit on it anywhere is a shape and not a measurement; a
    # chart with four labelled rules is furniture. Drawn before the silhouette
    # so that where the two meet the data is on top.
    if tick > 0:
        r = int(rows_of(np.array([tick]), lay, scale)[0])
        if lay.chart_y < r < lay.chart_bot:
            dst[r] = np.maximum(dst[r], np.array(C_GRID_HI, np.uint8))
            blit_text(dst, r - text_height() - 1, 2, "%d/H" % tick, C_DIM)

    if not lay.axis_h:
        return
    y = lay.axis_y + 2
    dst[lay.axis_y] = C_SEP
    for hour in HOUR_RULES + (24,):
        s = hour_label(hour, not h24)
        c = int(hour / 24.0 * w)
        x = c + 2 if hour < 24 else w - text_width(s) - 1
        if hour == 0:
            x = 1
        if 0 <= x <= w - text_width(s):
            # C_DIM and not C_FAINT. The axis is the calibration of the whole
            # picture and C_FAINT peaks at 64 of 255, which is under what a
            # 3x5 glyph needs to survive being looked at from ten feet away.
            blit_text(dst, y, x, s, C_DIM)


def bake_typical_day(dst, lay, lo, med, hi, scale):
    """The silhouette and its crust: what this weekday normally does.

    Filled to the median rather than drawn as a line, because a filled shape
    reads as ground at a glance and a line reads as a second data series -- and
    there is only one data series on this panel that is about today. The crust
    between the tenth and ninetieth percentile then sits on the skyline where
    it is legible, instead of being a shaded region a bright line has to be
    picked out of.
    """
    w = lay.w
    mask = np.ones(w, bool)
    r_med = rows_of(slot_to_col(med, w), lay, scale)
    r_lo = rows_of(slot_to_col(lo, w), lay, scale)
    r_hi = rows_of(slot_to_col(hi, w), lay, scale)
    fill_between(dst, lay, r_med, np.full(w, lay.chart_bot), mask, C_BAND)
    fill_between(dst, lay, r_hi, r_lo, mask, C_CRUST)
    return r_med


def bake_today(dst, lay, rate, seen, scale):
    """Today's line, and the flat pixel indices the comet runs along.

    Two rows of halo either side of a one row core: a single lit row of gold
    over a dark blue field is legible up close and disappears at ten feet,
    which is the distance this panel is actually read from. The halo is dark
    enough that the line still reads as one pixel wide.

    Gaps in coverage are gaps in the line. The alternative -- interpolating
    across a missed pass -- draws a measurement that was not made, and this
    panel's entire argument is about not doing that.
    """
    w = lay.w
    col_rate = slot_to_col(np.nan_to_num(rate, nan=0.0), w)
    col_seen = slot_to_col(seen.astype(f32), w) > 0.5
    if not col_seen.any():
        return np.zeros(0, np.int64), None
    r = rows_of(col_rate, lay, scale)
    prev = np.concatenate([r[:1], r[:-1]])
    joined = col_seen & np.concatenate([col_seen[:1], col_seen[:-1]])
    top = np.where(joined, np.minimum(r, prev), r)
    bot = np.where(joined, np.maximum(r, prev), r)
    fill_between(dst, lay, np.maximum(top - 1, lay.chart_y),
                 np.minimum(bot + 1, lay.chart_bot), col_seen, C_TODAY_HALO)
    fill_between(dst, lay, top, bot, col_seen, C_TODAY)
    cols = np.flatnonzero(col_seen)
    flat = (r[cols].astype(np.int64) * w + cols).astype(np.int64)
    return flat, int(cols[-1])


def bake_now_label(dst, lay, col):
    """`NOW` on the axis row, under the rule. The word and not the time.

    A clock here would be baked with everything else and would therefore be up
    to one reload interval behind the wall -- five minutes of saying 5:50 at
    5:55, which is a small lie told by the one mark on the panel whose whole
    job is to be trustworthy. `NOW` is three characters and is always true.
    """
    if not lay.axis_h:
        return
    y = lay.axis_y + 2
    s = "NOW"
    x = int(np.clip(col - text_width(s) // 2, 1, lay.w - text_width(s) - 1))
    # Cleared wide enough to take a whole hour label out either side. The first
    # version cleared two pixels of margin, and at ten to six the panel read
    # `NOW P` -- the tail of the 6P tick surviving next to the word that had
    # replaced it.
    dst[y - 1:y + text_height() + 1,
        max(0, x - 14):x + text_width(s) + 14] = 0
    blit_text(dst, y, x, s, C_NOW)


def bake_header(dst, lay, rec, stale, wd, w):
    if not lay.head_h:
        return
    # The weekday is deliberately *not* here. It is already on the panel twice
    # -- in the verdict and in the line that says what the shaded shape is --
    # and a header that says MONDAY over a strip that says USUAL FOR A MONDAY
    # over a note that says 13 RECENT MONDAYS is three sentences spending rows
    # to agree with each other.
    for form in ("BAY WHEELS SF", "BAY WHEELS", "SF BIKES"):
        if text_width(form) + 40 <= w:
            blit_text(dst, 0, 1, form, C_DIM)
            break
    if rec is not None:
        age = ftdata.describe_age(rec["age"])
        right = ("STALE " + age) if stale else (age + " AGO")
        blit_text(dst, 0, w - text_width(right) - 1, right,
                  C_WARN if stale else C_DIM)
    dst[lay.head_h - 1] = C_SEP


def bake_strip(dst, lay, lines):
    """The headline strip: a big number, two captions, two right-hand notes.

    `lines` is (big, big_colour, left_top, left_bot, right_top, right_colour,
    right_bot). Everything on it is a ladder of shorter forms, because what
    falls off the end of a clipped line here is the part that says the number
    is an estimate.
    """
    if not lay.strip_h:
        return
    big, big_rgb, lt, lb, rt, rt_rgb, rb = lines
    y0 = lay.strip_y
    y1 = y0 + text_height(2) - text_height()
    x = 1
    if big:
        x += blit_text(dst, y0, 1, big, big_rgb, 2) + 5
    left_end = x
    if lt:
        left_end = max(left_end, x + text_width(lt))
        blit_text(dst, y0, x, lt, C_TEXT)
    if lb:
        for form in ([lb] if isinstance(lb, str) else lb):
            if x + text_width(form) <= lay.w - 2:
                blit_text(dst, y1, x, form, C_DIM)
                left_end = max(left_end, x + text_width(form))
                break
    if rt:
        fit(dst, y0, lay.w - 1, ([rt] if isinstance(rt, str) else rt),
            rt_rgb, floor=left_end + 6)
    if rb:
        # C_DIM and not C_FAINT: this is the line that says what the shaded
        # shape is, and the first draft drew it in the faintest colour on the
        # panel on the grounds that it is small print. C_FAINT peaks at 64 of
        # 255, which is under what is legible across a workshop. Small print
        # that has to be read is still print.
        fit(dst, y1, lay.w - 1, ([rb] if isinstance(rb, str) else rb),
            C_DIM, floor=left_end + 6)


def draw_nodata(dst, lay, lines):
    """The honest panel, for when there is not even an archive to draw."""
    dst[:] = (6, 6, 8)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.h // 2 - (len(lines) * (text_height(scale) + 2)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        blit_text(dst, y, x, s, rgb, sc)
        y += text_height(sc) + 3
    return dst


def full_scale(hi, rate, want=0.0):
    """Trips per hour at the top of the chart, rounded to a round number.

    Rounded up to a whole 250 an hour so the one labelled rule lands somewhere
    sayable, and so the axis does not creep by half a row every time the peak
    nudges -- an axis that rescales itself every ten minutes makes the morning
    look different at teatime for no reason.
    """
    if want > 0:
        return float(want)
    top = float(np.nanmax(hi)) if len(hi) else 0.0
    if rate is not None and len(rate) and np.isfinite(rate).any():
        top = max(top, float(np.nanmax(rate)))
    return max(500.0, math.ceil(top / 250.0) * 250.0)


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h)
    cache = args.cache_dir
    now_of = clock(parse_when(args.at), args.rate)

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)
    base = np.zeros((h, w, 3), np.uint8)       # the same, with no gold on it

    z, asset_problem = read_typical(args.asset)

    # The comet's colours, baked: a ramp from the line's own gold up to white
    # at the head, so it reads as a light travelling *along* the line rather
    # than as a separate object crossing it.
    ramp = np.linspace(0.0, 1.0, COMET_LEN, dtype=f32)[:, None] ** 2
    comet_pal = (np.array(C_TODAY, f32)[None, :] * (1.0 - ramp)
                 + np.array(C_TODAY_HEAD, f32)[None, :] * ramp
                 ).clip(0, 255).astype(np.uint8)

    cell = {"rec": None, "problem": asset_problem, "loaded": -1e18,
            "stale": False, "flat": np.zeros(0, np.int64), "head_col": None,
            "now_col": 0, "wd": 0, "scale": 500.0}

    def reload_data(now):
        rec, age, problem = read_today(cache)
        day0 = day0_of(now)
        wd = args.weekday if 0 <= args.weekday <= 6 \
            else time.localtime(now).tm_wday
        cell["wd"] = wd
        cell["loaded"] = now

        if z is None:
            cell["rec"], cell["problem"] = None, asset_problem
            return

        # A record whose slots belong to another day is not stale, it is
        # *wrong*: this axis is midnight to midnight and yesterday's line drawn
        # under today's clock puts the evening peak where the morning goes.
        wrong_day = None
        if rec is not None and abs(rec["day0"] - day0) > 1.0:
            wrong_day = time.strftime("%-m/%-d", time.localtime(rec["day0"]))
            problem = "LAST DATA %s" % wrong_day
            rec = None

        cell["rec"] = rec
        cell["problem"] = problem
        cell["stale"] = rec is not None and not ftdata.is_fresh(PRODUCT, age)

        lo, med, hi = profile(z, wd)
        k_hour, k_spread = calibration(z, wd)
        est = estimate(rec, k_hour) if rec is not None else None
        now_slot = int(np.clip((now - day0) // 600.0, 0, SLOTS - 1))
        cell["now_col"] = lay.col_of(now - day0)

        scale = full_scale(hi, est["rate"] if est else None, args.peak)
        cell["scale"] = scale
        tick = int(round(scale * 0.6 / 250.0) * 250)

        base[:] = 0
        bake_furniture(base, lay, args.h24, scale, tick)
        bake_typical_day(base, lay, lo, med, hi, scale)
        bake_now_label(base, lay, cell["now_col"])
        bake_header(base, lay, rec, cell["stale"], wd, w)
        bake_strip(base, lay, strip_lines(rec, est, z, wd, k_hour, k_spread,
                                          now_slot, wrong_day))
        static[:] = base
        if est is not None:
            flat, head = bake_today(static, lay, est["rate"], est["seen"],
                                    scale)
            cell["flat"], cell["head_col"] = flat, head
        else:
            cell["flat"], cell["head_col"] = np.zeros(0, np.int64), None
        cell["est"] = est

    def strip_lines(rec, est, z, wd, k_hour, k_spread, now_slot, wrong_day):
        """Every string on the headline strip, chosen once per cache read.

        Formatting is not free -- caiso measured a third of a millisecond a
        frame doing exactly this, which is thirty on the Pi -- and none of it
        can change between cache reads, so all of it happens here.
        """
        # No equals sign anywhere: defcon.py's 3x5 font has no glyph for one
        # and silently draws a space, so the first render of this line read
        # `SHADED   13 RECENT MONDAYS` and looked like a typesetting fault. The
        # punctuation on this panel is hyphens by necessity, as it is on every
        # other panel in this tree that uses this font.
        short = WEEKDAY_SHORT[wd]
        nmon = len(z["t%d" % wd])
        shade = ["SHADED - %d RECENT %sS" % (nmon, WEEKDAY[wd]),
                 "%d RECENT %sS" % (nmon, WEEKDAY[wd]),
                 "%d %sS" % (nmon, short)]
        if rec is None or est is None:
            said = wrong_day and ("LAST DATA %s" % wrong_day) or \
                "RUN  PYTHON3 FTDATA.PY --LOOP 600"
            return ("NO LIVE DATA", C_WARN, None, None,
                    [said, "NO LIVE DATA"], C_DIM, shade)

        first, last, frac = coverage(est, now_slot)
        total = int(round(float(est["trips"].sum())))
        if first is None:
            return ("WAITING", C_DIM, None,
                    ["NEEDS TWO FETCHES TEN MINUTES APART", "NO MOVEMENT YET"],
                    ["THE DAY STARTS HERE"], C_DIM, shade)

        if first <= 3 and frac >= COVER_FULL:
            what = "TRIPS TODAY"
        elif frac >= COVER_PART:
            what = "TRIPS SINCE %s" % hhmm(
                rec["day0"] + first * 600.0, not args.h24)
        else:
            what = "TRIPS IN %dH SEEN" % max(1, int(round(est["secs"] / 3600.0)))

        # The calibration, printed. The effective factor over the slots that
        # were actually measured, which is what was applied to the number to
        # its left -- not the day's average, which would be a different number
        # on a panel that has only seen the morning.
        sel = est["seen"].copy()
        sel[now_slot + 1:] = False
        hours = np.minimum(np.arange(SLOTS) // 6, 23)
        eff = float(np.mean(k_hour[hours[sel]])) if sel.any() else 0.0
        note = ["EST FROM DOCK COUNTS X%.1f" % eff,
                "FROM DOCK COUNTS X%.1f" % eff,
                "DOCK COUNTS X%.1f" % eff, "EST"]

        word, rgb = verdict(est, smooth(z["t%d" % wd]), k_spread, now_slot)
        if word is None:
            words = ["USUAL FOR A %s" % WEEKDAY[wd],
                     "USUAL FOR A %s" % short, "AS USUAL"]
        else:
            words = [word]
        return ("%d" % total, C_TODAY, what, note, words, rgb, shade)

    def render(t, i):
        now = now_of()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        if z is None:
            return draw_nodata(frame, lay, [
                ("NO BIKE ARCHIVE", C_WARN),
                ("RUN  PYTHON3 BIKES.PY --BAKE-TYPICAL", C_TEXT),
                (str(cell["problem"] or "").upper()[:52], C_DIM)])

        frame[:] = static
        flat = cell["flat"]
        n = len(flat)

        # The reveal, and then the comet. Two slice copies rather than a mask:
        # the part of today that has not been drawn in yet is restored from
        # `base`, which already carries the silhouette, so the gold line wipes
        # in over a chart rather than over a black hole.
        drawn = n
        if args.reveal > 0 and t < args.reveal and n:
            drawn = int(n * (t / args.reveal))
            col = int(flat[min(drawn, n - 1)] % w)
            frame[lay.chart_y:lay.chart_bot + 1, col:] = \
                base[lay.chart_y:lay.chart_bot + 1, col:]
        elif args.comet > 0 and n:
            phase = ((t - args.reveal) / args.comet) % 1.0
            head = int(phase * (n + COMET_LEN)) - COMET_LEN
            a, b = max(0, head), min(n, head + COMET_LEN)
            if b > a:
                frame.reshape(-1, 3)[flat[a:b]] = \
                    comet_pal[a - head:b - head]

        # The now-rule last, over everything. It breathes, and a short bright
        # pulse runs up it, which is the only thing on the panel guaranteed to
        # move in every frame. Both are driven by the segment's own `t` and not
        # by the wall clock, so the animation is the same under a test harness
        # rendering a hundred frames in a millisecond as it is on the wall.
        col = cell["now_col"]
        if col < w:
            top, bot = lay.chart_y, lay.chart_bot + 1
            # A shallower breath than caiso's, which swings between 10 and 100
            # per cent. This rule is already the dimmest deliberate mark on the
            # panel, and at caiso's depth it spends a third of every cycle
            # under what is legible across a workshop -- the mark whose whole
            # job is to say where now is should never be the one that
            # disappears.
            blink = 0.78 + 0.22 * math.sin(t * 2.0)
            frame[top:bot, col] = tuple(int(c * blink) for c in C_NOW_DIM)
            py = top + int(((t * PULSE_HZ) % 1.0) * (bot - top))
            frame[max(top, py - 1):py + 2, col] = C_NOW
            # The head of the line, where today stops. On a fresh record that
            # is the now-rule; on a stale one it is behind it, and the gap is
            # the panel saying so.
            if cell["head_col"] is not None and drawn >= n:
                hc = cell["head_col"]
                hr = int(flat[-1] // w)
                frame[max(top, hr - 1):min(bot, hr + 2), hc] = C_TODAY_HEAD
        return frame

    reload_data(now_of())
    render.state = cell               # tests reach in here; nothing else does
    render.layout = lay
    render.clock = now_of
    render.static = static
    render.typical = z
    return render


# --------------------------------------------------------------------------
# Baking `bikes-typical.npz` out of Bay Wheels' published trip archive.
#
# An offline tool that never runs on the wall: it wants sixty megabytes of zip,
# csv, zipfile and about ten seconds. It lives in this file rather than in a
# script of its own so that the asset and the code that reads it cannot drift
# apart, which is the rule stringline.py's --bake-lines set.
#
#     $ python3 bikes.py --bake-typical                    # last 3 whole months
#     $ python3 bikes.py --bake-typical 202605 202606 202607
#
# The source is `https://s3.amazonaws.com/baywheels-data/YYYYMM-baywheels-
# tripdata.csv.zip`: one row per trip, keyless, published monthly, with the
# start and end time, the dock at each end where there was one, and coordinates
# rounded to two decimals for the free-floating ebikes. Some months are
# published as `-tripdata.zip` instead, so both are tried.
#
# **It is cropped to San Francisco**, by the start coordinate, against the same
# box `ftdata.py` crops the live feed with. Bay Wheels is one system covering
# four separated cities and San Jose's commute is not this wall's.
#
# Two matrices come out per weekday, one row per date and 144 ten-minute
# columns:
#
#   t<wd>   trips that started in the box in that bucket. The census.
#   e<wd>   what the dock-count estimator would have reported for the same
#           bucket: every trip replayed as minus one at the dock it left when
#           it left and plus one at the dock it reached when it reached it,
#           then the sum of |change| over the docks, halved. Only endpoints
#           inside the box count, which is the same blindness the live
#           estimator has. Trips on free-floating ebikes have no dock at either
#           end and so contribute nothing to it -- which is exactly why the
#           ratio of the two matrices is bigger than one.
#
# Both are kept raw rather than reduced to a median and a spread, because the
# derivation is cheap, because the panel needs sums over arbitrary subsets of
# the day for a cold-started comparison, and because a committed asset that
# carries the counts can be argued with and one that carries percentiles
# cannot.
#
# **What is left in on purpose.** Public holidays, Bay to Breakers, rain and
# Muni strikes are all in there and are not removed. Thirteen dates a weekday
# is too few to identify outliers without also removing real variety, and the
# tenth-to-ninetieth band is exactly the right instrument for absorbing one odd
# Monday in thirteen.
# --------------------------------------------------------------------------

ARCHIVE = "https://s3.amazonaws.com/baywheels-data/%s-baywheels-tripdata%s"

# The same crop as ftdata's BIKES_BBOX, repeated here rather than imported
# because the bake is an offline tool and the whole point of the split is that
# this file never reaches into the fetcher's internals at run time. If one
# moves, both move; the asset records the box it was made with.
BAKE_BBOX = (37.700, 37.840, -122.530, -122.350)


def _recent_months(n=3, now=None):
    """The last `n` complete calendar months, newest last."""
    lt = time.localtime(now if now is not None else time.time())
    y, m = lt.tm_year, lt.tm_mon
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append("%04d%02d" % (y, m))
    return list(reversed(out))


def _fetch_month(month, cache_dir):
    """The month's zip on disk, downloading it if it is not there already."""
    import urllib.request
    path = os.path.join(cache_dir, "%s.zip" % month)
    if os.path.exists(path) and os.path.getsize(path) > 1 << 20:
        return path
    last = None
    for suffix in (".csv.zip", ".zip"):
        url = ARCHIVE % (month, suffix)
        try:
            with urllib.request.urlopen(url, timeout=300) as resp:
                blob = resp.read()
        except Exception as exc:                             # noqa: BLE001
            last = exc
            continue
        with open(path, "wb") as fh:
            fh.write(blob)
        print("  %s  %.1f MB  %s" % (month, len(blob) / 1e6, url))
        return path
    raise IOError("cannot fetch %s (%r)" % (month, last))


def _read_month(path):
    """(trips, net) counters keyed by (date, slot). Streaming, one pass."""
    import collections
    import csv
    import io
    import zipfile

    zf = zipfile.ZipFile(path)
    name = [n for n in zf.namelist()
            if n.lower().endswith(".csv") and not n.startswith("__")][0]
    lat0, lat1, lon0, lon1 = BAKE_BBOX
    trips = collections.Counter()
    net = collections.defaultdict(collections.Counter)

    def slot_of(stamp):
        # 'YYYY-MM-DD HH:MM:SS.mmm', local time, sliced rather than parsed:
        # strptime on a million rows is forty seconds and this is one.
        return (int(stamp[11:13]) * 6 + int(stamp[14:16]) // 10)

    with zf.open(name) as fh:
        r = csv.reader(io.TextIOWrapper(fh, "utf-8-sig"))
        ix = {k: i for i, k in enumerate(next(r))}
        i_sa, i_ea = ix["started_at"], ix["ended_at"]
        i_ss, i_es = ix["start_station_id"], ix["end_station_id"]
        i_sla, i_slo = ix["start_lat"], ix["start_lng"]
        i_ela, i_elo = ix["end_lat"], ix["end_lng"]
        for row in r:
            sa = row[i_sa]
            try:
                s = slot_of(sa)
                la, lo = float(row[i_sla]), float(row[i_slo])
            except (ValueError, IndexError):
                continue
            if lat0 <= la <= lat1 and lon0 <= lo <= lon1:
                trips[(sa[:10], s)] += 1
                if row[i_ss]:
                    net[(sa[:10], s)][row[i_ss]] -= 1
            if not row[i_es]:
                continue
            try:
                ela, elo = float(row[i_ela]), float(row[i_elo])
                e = slot_of(row[i_ea])
            except (ValueError, IndexError):
                continue
            if lat0 <= ela <= lat1 and lon0 <= elo <= lon1:
                net[(row[i_ea][:10], e)][row[i_es]] += 1
    return trips, net


def bake_typical(months=None, out=None, cache_dir=None):
    """Read the monthly trip archive and write the committed asset."""
    import collections
    import datetime

    months = list(months) or _recent_months(3)
    out = out or asset_path()
    cache_dir = cache_dir or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "baywheels-archive")
    os.makedirs(cache_dir, exist_ok=True)

    trips = collections.Counter()
    net = collections.defaultdict(collections.Counter)
    for month in months:
        t0 = time.time()
        path = _fetch_month(month, cache_dir)
        tr, nt = _read_month(path)
        trips.update(tr)
        for key, counter in nt.items():
            net[key].update(counter)
        print("  %s  %d SF trips  %.1fs"
              % (month, sum(tr.values()), time.time() - t0))

    # Only the dates wholly inside the months asked for. A file's first day
    # collects arrivals from trips that started in the previous month and its
    # last day loses the ones that end in the next, and a partial day drawn as
    # a whole one would drag every percentile down.
    keep = set(months)
    dates = sorted(d for d, _s in trips if d.replace("-", "")[:6] in keep)
    dates = sorted(set(dates))
    if len(dates) < 14:
        raise ValueError("only %d dates parsed; that is not a typical day"
                         % len(dates))

    by_wd = collections.defaultdict(list)
    for d in dates:
        y, m, dd = (int(x) for x in d.split("-"))
        by_wd[datetime.date(y, m, dd).weekday()].append(d)

    arrays = {}
    for wd in range(7):
        days = by_wd[wd]
        t = np.zeros((len(days), SLOTS), np.float32)
        e = np.zeros((len(days), SLOTS), np.float32)
        for i, d in enumerate(days):
            for s in range(SLOTS):
                t[i, s] = trips.get((d, s), 0)
                counter = net.get((d, s))
                if counter:
                    e[i, s] = sum(abs(v) for v in counter.values()) * 0.5
        arrays["t%d" % wd] = t
        arrays["e%d" % wd] = e
        print("  %s  %2d dates  median %6d trips/day  k=%.2f"
              % (WEEKDAY_SHORT[wd], len(days), int(np.median(t.sum(1))),
                 t.sum() / max(e.sum(), 1.0)))

    arrays["months"] = np.array(months)
    arrays["span"] = np.array([dates[0], dates[-1]])
    arrays["bbox"] = np.array(BAKE_BBOX, np.float64)
    arrays["bucket"] = np.array([86400 // SLOTS], np.int32)
    np.savez_compressed(out, **arrays)
    print("wrote %s  (%d dates, %s to %s, %.1f kB)"
          % (out, len(dates), dates[0], dates[-1],
             os.path.getsize(out) / 1024.0))


def main():
    # The bake is not a demo option: it has to happen instead of a frame loop,
    # not inside one, and megademo must never see it in add_arguments().
    if "--bake-typical" in sys.argv:
        i = sys.argv.index("--bake-typical")
        bake_typical(sys.argv[i + 1:])
        return
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
