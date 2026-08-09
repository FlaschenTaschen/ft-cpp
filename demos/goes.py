#!/usr/bin/env python3
"""The last six hours of weather over California, from orbit, on a loop.

GOES-18 sits over the equator at 137 degrees west and rescans the Pacific
Southwest every five minutes. This plays the most recent few hours of that as
a time lapse: the marine layer sliding in and out of the Gate, thunderheads
going up over the Sierra in the afternoon, a front coming down the coast. It
is the one thing on the wall that is a photograph of right now rather than a
drawing of it, and the loop is short enough -- twelve seconds -- that somebody
walking past sees the weather move.

**The band, and what it costs.** The panel is five times wider than it is
tall, and nothing NESDIS publishes is that shape. GOES-18's `CONUS` sector is
not the continental United States at all -- from 137 W it is the eastern
Pacific with the West Coast down the right-hand edge -- and GOES-19's real
CONUS, squashed from 5:3 to 5:1, turns Florida into a stub and the country
into a smear. What does fit is a **crop**: 500 by 100 pixels out of the
600x600 `psw` sector image is exactly 5:1, so it goes to 320x64 with nothing
stretched at all. That band runs from about 350 km offshore to central Utah,
1127 km by 301 km, centred so San Francisco Bay sits a third of the way in
from the left. The cost is the two ends of California: Eureka is off the top,
San Diego off the bottom. For a wall in a San Francisco workshop that is the
right trade -- the Bay is the part people look for, the ocean to the west of
it is where the weather comes from, and at 3.5 km a pixel the fog bank has
shape instead of being three pixels of grey.

The picture is the satellite's own view and not a map. A geostationary grid is
not north-up and it foreshortens north-south at this latitude, so the band is
slightly skewed -- its centre line runs from 37.7 N at the left edge to 38.1 N
at the right -- and one panel pixel is 3.5 km across but 4.7 km down. Nothing
here does that; it is what looking at 37 N from over the equator does, and it
is the same picture as the one on the NOAA website.

**GeoColor is two different products and the label says which.** In daylight
it is true colour, so cloud is white and the Central Valley is brown. After
dark there is no visible light to work with, so it becomes infrared cloud
tinted blue-grey over a static night map, with city lights in orange -- the
Bay Area, Sacramento, the 99 corridor, Reno over the hill. A window that
straddles sunset therefore changes character completely partway through, which
is the best thing about it and would look like a fault if it were not
announced, so each frame is labelled `DAY`, `DUSK`, `DAWN` or `NIGHT IR` from
the sun's elevation over the middle of the crop.

**The imagery comes out of a cache, never off the network.** `ftdata.py`
fetches on a timer in a process of its own; the JPEGs are decoded, cropped and
resized to 320x64 *there*, and only the 61 kB result is stored, so this file
imports numpy and nothing else -- no Pillow, no HTTP library, no JPEG decoder
in `build()`. See ftdata.py's docstring for why the network cannot live on
this side. Run the fetcher first, or the panel says so:

    $ python3 ftdata.py --loop 900

**Playback is an index and one blend.** Every frame on the wall is already the
right size and the right crop, so `render()` picks a frame, blends it with the
next one by a fraction, and writes a label. There is nothing else to do, which
is the point: this should be the cheapest thing in the rotation, and if it is
not then something is being done per frame that belonged in `build()`. The
blend is integer -- six passes over an int16 scratch rather than the float
crossfade in demoscene -- because on a Pi 3 at 600 MHz integer is about three
times cheaper and this runs thirty times a second forever.

**Missing, partial and stale are three different things and it says which.**
No cache, no sidecar, or a sidecar that will not open gets the no-data card
with the fetcher's command on it. A window with fewer frames than asked for
plays anyway and shows what it has -- a cold start is a partial window by
definition, and half an hour of weather moving beats a card that says wait.
And a window whose newest frame has gone stale keeps playing with `STALE` and
the age in red, because six hours of real weather from this morning is still
worth watching as long as nobody can mistake it for now. Two-hour-old imagery
presented as current is the failure this is arranged to avoid.
"""

import math
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

# Where the day/night call is made. Not one point: the band is 1100 km wide,
# which is fifty minutes of solar time, so for the best part of an hour twice a
# day one end of the picture is in daylight and the other is not. Asking at
# both ends is what lets the label say SUNSET rather than picking whichever
# half the centre pixel happens to agree with.
SUN_LAT = 37.9
SUN_WEST, SUN_EAST = -126.2, -113.6

# Colours. Everything is type over a photograph, so the bar under it is dark
# and the type is warm white; the only saturated colour on the panel is the
# stale flag, which is the one thing that must win against a bright cloud.
C_TEXT = (208, 220, 232)
C_DIM = (104, 118, 132)
C_WARN = (255, 88, 64)
C_RULE = (26, 30, 38)
C_TRACK = (70, 82, 96)
C_HEAD = (255, 236, 190)

# How much of the imagery survives under the caption. Not zero: the coastline
# and the city lights run right through the bottom of the band, and a solid
# black bar there cuts the picture in a way a smoked one does not.
BAR_DIM = 0.30

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same table propagation.py and tide.py use;
# there is no font file to be missing on the Pi and nothing built from a real
# typeface survives five pixels anyway.
# --------------------------------------------------------------------------

_GLYPHS = {}
for _ch, _rows in defcon._FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _digit in enumerate(_rows):
        _v = int(_digit, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g


def text_mask(s):
    """A (5, 4n-1) bool mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((5, 1), bool)
    out = np.zeros((5, len(s) * 4 - 1), bool)
    blank = _GLYPHS[" "]
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, blank)
    return out


def text_w(s, scale=1):
    return max(0, (4 * len(str(s)) - 1) * scale)


def stamp(buf, x, y, s, colour, scale=1):
    """Draw text into a uint8 (H, W, 3) buffer, clipped. Returns its width.

    Clipped rather than asserted: the panel is laid out for 320x64 but has to
    survive being asked for something else, and a demo that raises on a narrow
    canvas takes the whole rotation down with it.
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
        buf[y + sy:y + ey, x + sx:x + ex][m[sy:ey, sx:ex]] = colour
    return w


# --------------------------------------------------------------------------
# Time, and where the sun is.
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


def hhmm(epoch, ampm=True):
    """A compact label in the *display's* local time: '6:41P' or '18:41'.

    Local to the wall, not to the satellite and not to UTC. The frame is a
    photograph of the sky over the same city the panel is in, and the time
    somebody standing in front of it can act on is the one on their watch.
    """
    lt = time.localtime(epoch)
    if not ampm:
        return time.strftime("%H:%M", lt)
    return "%d:%02d%s" % (lt.tm_hour % 12 or 12, lt.tm_min,
                          "A" if lt.tm_hour < 12 else "P")


def solar_elevation(epoch, lat=SUN_LAT, lon=SUN_WEST):
    """The sun's altitude in degrees. Low-precision USNO series, ~0.01 deg.

    Which is roughly a thousand times more accuracy than a four-word label
    needs, but it is fifteen lines and it means the day/night call comes from
    the sky rather than from thresholding the picture's own brightness --
    which would swing with a cloud deck and call a bright fog bank daylight.
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


def light_words(epoch):
    """What GeoColor is showing in this frame, in one or two words.

    Both ends of the band are asked, because the honest answer for most of an
    hour around dawn and dusk is "both": the eastern edge over Utah goes dark
    fifty minutes before the ocean west of the Farallones does, and the
    terminator is visibly *in* the picture, marching across it. Calling that
    DAY because the middle pixel is still lit would leave somebody looking at
    half a dark panel wondering what had broken.
    """
    west = solar_elevation(epoch, SUN_LAT, SUN_WEST)
    east = solar_elevation(epoch, SUN_LAT, SUN_EAST)
    # Rising or setting: the same elevation morning and evening means opposite
    # things, and only the sun's own motion knows which.
    rising = solar_elevation(epoch + 900.0, SUN_LAT, SUN_WEST) > west
    lo, hi = min(west, east), max(west, east)
    # The thresholds are GeoColor's, not the almanac's. The product starts
    # cross-fading to its night rendering at a solar zenith of 80 degrees --
    # ten degrees of elevation, the better part of an hour before sunset --
    # and has finished by the time the sun is down, which is why the east end
    # of the picture goes murky while the almanac still says afternoon.
    if lo > 10.0:
        return "DAY"
    if hi < 0.0:
        return "NIGHT IR"
    if lo < 0.0 < hi:
        # The terminator itself is on the panel.
        return "SUNRISE" if rising else "SUNSET"
    return "DAWN" if rising else "DUSK"


# --------------------------------------------------------------------------
# Reading what ftdata left behind. Two files -- a JSON record and the array
# sidecar it names -- and everything that can be wrong with either of them
# ends up as one string the panel can print.
# --------------------------------------------------------------------------

def read_window(cache_dir, product):
    """Return (window, age, problem). Any of the first two may be None."""
    got = ftdata.load(product, cache_dir)
    if got is None:
        return None, None, "no cached goes imagery"
    payload, age = got
    if not isinstance(payload, dict):
        return None, age, "goes record is malformed"
    blob = ftdata.load_blob(payload.get("blob"), cache_dir)
    if blob is None:
        return None, age, "frame sidecar is missing or unreadable"
    try:
        frames = blob["frames"]
        stamps = np.asarray(blob["stamps"], np.float64)
    except Exception:                                        # noqa: BLE001
        return None, age, "frame sidecar has no frames in it"
    if (frames.ndim != 4 or frames.shape[3] != 3 or frames.dtype != np.uint8
            or len(frames) == 0 or len(frames) != len(stamps)):
        return None, age, "frame sidecar is malformed"
    order = np.argsort(stamps)
    return {"frames": np.ascontiguousarray(frames[order]),
            "stamps": stamps[order], "age": age,
            "want": int(payload.get("want", len(frames))),
            "cadence": float(payload.get("cadence", 300.0)),
            "sat": str(payload.get("sat", "GOES")),
            "sector": str(payload.get("sector", "")),
            "extent": payload.get("extent") or {}}, age, None


def resample(frames, w, h):
    """Nearest-neighbour the stored frames onto a different panel. Once.

    The sidecar is written at whatever geometry the fetcher was configured
    for, and --width/--height are allowed to disagree with it. Rescaling every
    frame here is one gather over the whole stack in `build()`; doing it per
    frame would be a gather per frame, which on this machine is the most
    expensive shape of work there is.
    """
    n, fh, fw = frames.shape[:3]
    if (fw, fh) == (w, h):
        return frames
    ry = np.clip(((np.arange(h) + 0.5) * fh / h).astype(np.intp), 0, fh - 1)
    rx = np.clip(((np.arange(w) + 0.5) * fw / w).astype(np.intp), 0, fw - 1)
    # One gather, not two. Indexing the rows and then the columns builds a
    # whole intermediate stack at the half-resampled size, and on the Pi that
    # doubles a cost already measured in seconds; flattening the picture and
    # gathering (row * fw + col) once does the same work in one pass.
    flat = (ry[:, None] * fw + rx[None, :]).reshape(-1)
    out = frames.reshape(n, fh * fw, 3)[:, flat]
    return np.ascontiguousarray(out.reshape(n, h, w, 3))


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--product", default=ftdata.GOES_PRODUCT,
                    help="ftdata product name to play")
    ap.add_argument("--frame-rate", type=float, default=6.0,
                    help="satellite frames per wall second; five real minutes "
                         "a sixth of a second reads as weather moving")
    ap.add_argument("--hold", type=float, default=1.4,
                    help="seconds paused on the newest frame at the end of "
                         "each pass, so the loop lands on now")
    ap.add_argument("--no-blend", dest="blend", action="store_false",
                    help="cut between frames instead of dissolving")
    ap.add_argument("--bar", type=int, default=-1,
                    help="rows of caption at the bottom (-1 = automatic, "
                         "0 = none)")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--reload", type=float, default=600.0,
                    help="seconds between re-reads of the cache (0 = never)")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this, for testing the "
                         "stale path (epoch or 'YYYY-MM-DD HH:MM' local)")


# --------------------------------------------------------------------------
# The no-data card. Same shape as tide.py's and propagation.py's: say what is
# missing, say the command that fixes it, say where it was looked for.
# --------------------------------------------------------------------------

def draw_nodata(frame, args, problems, cache_dir):
    frame[:] = (6, 6, 8)
    h, w = frame.shape[:2]
    frame[0] = C_WARN
    frame[h - 1] = C_WARN
    frame[:, 0] = C_WARN
    frame[:, w - 1] = C_WARN

    title = "NO IMAGERY"
    scale = 3
    while scale > 1 and text_w(title, scale) > w - 12:
        scale -= 1
    lines = ["RUN: PYTHON3 FTDATA.PY --LOOP 900"]
    for p in problems[:1]:
        lines.append(p.upper())
    lines.append((cache_dir or ftdata.CACHE_DIR).upper())

    y = max(2, h // 2 - (5 * scale + 2 + 7 * len(lines)) // 2)
    stamp(frame, (w - text_w(title, scale)) // 2, y, title, C_WARN, scale)
    y += 5 * scale + 4
    for line in lines:
        while line and text_w(line) > w - 6:
            line = line[:-1]
        stamp(frame, (w - text_w(line)) // 2, y, line, C_TEXT)
        y += 7
        if y + 5 > h - 2:
            break
    return frame


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    cache = args.cache_dir
    frame = np.zeros((h, w, 3), np.uint8)

    at = parse_when(args.at)
    offset = 0.0 if at is None else at - time.time()

    def now():
        return time.time() + offset

    bar_h = args.bar
    if bar_h < 0:
        # Six rows of caption -- a rule, five of type -- plus one for the
        # position track. Under about twenty rows there is no panel left after
        # that, so the picture gets the lot and the label goes.
        bar_h = 7 if h >= 22 else 0
    bar_h = max(0, min(bar_h, max(0, h - 6)))

    cell = {"win": None, "problem": None, "loaded": -1e18, "n": 0,
            "labels": None, "age_key": None, "age_mask": None,
            "age_colour": C_DIM, "stale": False, "card": None}

    def load(t):
        cell["loaded"] = t
        win, _, problem = read_window(cache, args.product)
        if problem != cell["problem"]:
            cell["card"] = None       # the card names the problem; it changed
        cell["problem"] = problem
        if win is None:
            cell["win"] = None
            cell["n"] = 0
            return
        frames = resample(win["frames"], w, h)
        if bar_h:
            # Smoke the caption strip into the imagery once, here, rather than
            # compositing a translucent bar every frame. It rides through the
            # blend for free and costs one pass over a twelfth of the stack.
            strip = frames[:, h - bar_h:]
            np.multiply(strip, BAR_DIM, out=strip, casting="unsafe")
            frames[:, h - bar_h] = C_RULE
        win["frames"] = frames
        cell["win"] = win
        cell["n"] = len(frames)
        cell["labels"] = bake_labels(win, w, bar_h)
        cell["age_key"] = None

    def bake_labels(win, w, bar_h):
        """One text mask per frame: what it is, when it is, what it shows.

        The timestamp belongs to the frame and not to the clock, so it is
        baked with the frame and never formatted in the loop. There are
        seventy-odd of them and each is a few hundred bools.

        The source, the time and the daylight word are three separate claims
        and they are not equally important, so the label is a ladder: whatever
        the panel is wide enough for, dropping the satellite's name first and
        the daylight word last. Clipping instead would lose the right-hand end
        of the line, which is exactly the part that says the picture has gone
        from true colour to infrared.
        """
        if not bar_h:
            return None
        src = " ".join(("%s %s" % (win["sat"], win["sector"])).upper().split())
        stamps = win["stamps"]
        multi_day = (len(stamps) > 1
                     and time.localtime(stamps[0]).tm_yday
                     != time.localtime(stamps[-1]).tm_yday)
        # Room for the age on the right, which is never given up.
        budget = w - 4 - text_w("STALE 12H") - 6
        out = []
        for t in stamps:
            clock = hhmm(t, not args.h24)
            if multi_day:
                clock = time.strftime("%a ", time.localtime(t)).upper() + clock
            words = light_words(t)
            for s in ("%s  %s  %s" % (src, clock, words),
                      "%s  %s" % (clock, words), clock):
                if text_w(s) <= budget:
                    break
            out.append(text_mask(s))
        return out

    load(now())

    # Scratch for the blend. int16 because the difference of two uint8 frames
    # is signed and can be a full -255..255, and because integer is about three
    # times cheaper than float32 a pass on the wall's Pi.
    diff = np.empty((h, w, 3), np.int16)

    def blend_into(dst, a, b, k):
        """dst = a + ((b - a) * k >> 7), in integers, six whole-frame passes.

        Sevenths of a step rather than eighths so the arithmetic stays in
        int16: the difference of two uint8 frames spans -255..255 and 255*127
        is 32385, which is inside int16 by three hundred. It cannot leave
        0..255 either -- the shift floors, and flooring something strictly
        greater than `b - a` cannot go below `b - a` when that is an integer,
        so the result stays between the two frames.
        """
        # np.multiply(..., out=) and not `diff *= k`: an augmented assignment
        # to a name from the enclosing scope makes it local to this function,
        # and the failure lands three lines earlier as an UnboundLocalError on
        # the read. tide.py has the same note; it is worth having twice.
        np.subtract(b, a, out=diff, dtype=np.int16, casting="unsafe")
        np.multiply(diff, k, out=diff)             # k is 0..127
        np.right_shift(diff, 7, out=diff)
        # `a` goes straight into the add rather than through a widening copy:
        # int16 + uint8 is int16, so numpy promotes it in place and that is one
        # fewer whole-frame pass, which on this machine is a fifth of the cost
        # of the blend.
        np.add(diff, a, out=diff)
        np.copyto(dst, diff, casting="unsafe")

    def render(t, i):
        tnow = now()
        if args.reload and tnow - cell["loaded"] >= args.reload:
            load(tnow)
        win = cell["win"]
        if win is None or cell["n"] == 0:
            # Baked once and copied, not laid out thirty times a second. The
            # card is the same every frame, and typesetting four lines costs
            # more than everything the working demo does.
            if cell["card"] is None:
                cell["card"] = draw_nodata(np.zeros_like(frame), args,
                                           [cell["problem"] or "cache is empty"],
                                           cache)
            np.copyto(frame, cell["card"])
            return frame

        n = cell["n"]
        frames = win["frames"]
        # Where in the loop. One pass through the window at --frame-rate, then
        # a hold on the newest frame: the loop lands on *now* rather than
        # snapping back from the middle of yesterday afternoon, which is the
        # difference between a time lapse and a flicker.
        span = n / max(args.frame_rate, 1e-3)
        cycle = span + max(0.0, args.hold)
        u = t % cycle if cycle > 0 else 0.0
        if u >= span or n == 1:
            k, frac = n - 1, 0.0
        else:
            x = u * args.frame_rate
            k = min(n - 1, int(x))
            frac = x - k
        kk = min(127, int(frac * 128.0))
        if not args.blend or k >= n - 1 or kk == 0:
            np.copyto(frame, frames[k])
        else:
            blend_into(frame, frames[k], frames[k + 1], np.int16(kk))

        if not bar_h:
            return frame

        y = h - bar_h + 1
        # The frame's own timestamp, already a mask, so this is one scatter
        # over a strip rather than a text layout every frame.
        m = cell["labels"][k]
        gh, gw = m.shape
        gw = min(gw, w - 3)
        if gw > 0 and y + gh <= h:
            frame[y:y + gh, 2:2 + gw][m[:, :gw]] = C_TEXT

        # The age of the *newest* frame, against the wall clock -- the one
        # number that says whether any of this is still true. It changes once
        # a minute at most, so it is laid out on the minute and blitted from a
        # mask in between.
        age = max(0.0, tnow - win["stamps"][-1])
        key = int(age // 30)
        if key != cell["age_key"]:
            cell["age_key"] = key
            stale = not ftdata.is_fresh(args.product, age)
            cell["stale"] = stale
            text = ftdata.describe_age(age)
            if stale:
                text = "STALE " + text
            elif n * 10 < win["want"] * 9:
                # A materially short window is worth admitting -- it is what a
                # cold start looks like, and it is why the loop is over in four
                # seconds. One or two slots missing out of seventy is the CDN
                # having an ordinary day and is not worth a number on the wall.
                text = "%d/%d  %s" % (n, win["want"], text)
            cell["age_mask"] = text_mask(text)
            cell["age_colour"] = C_WARN if stale else C_DIM
        m = cell["age_mask"]
        gh, gw = m.shape
        x0 = w - 2 - gw
        if x0 > 1 and y + gh <= h:
            frame[y:y + gh, x0:x0 + gw][m] = cell["age_colour"]

        # Where in the window we are. One row, two writes, and it is the only
        # thing that says this is a loop of the last few hours rather than a
        # live feed.
        p = int(round((k + frac) / max(1, n - 1) * (w - 1))) if n > 1 else w - 1
        frame[h - 1, :p + 1] = C_TRACK
        frame[h - 1, p + 1:] = C_RULE
        frame[h - 1, max(0, p - 1):p + 1] = C_HEAD
        return frame

    render.state = cell               # tests reach in here; nothing else does
    render.bar_h = bar_h
    render.clock = now
    return render


def main():
    # 30 fps: the blend wants a smooth ramp between satellite frames, and at
    # six frames a second there are five wall frames to spend on each dissolve.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
