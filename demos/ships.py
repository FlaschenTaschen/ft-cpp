#!/usr/bin/env python3
"""Ships in and out of San Francisco, drawn against the water they ride.

A movements board on a time axis. Two days of it run left to right across the
panel with now marked: every scheduled arrival and departure at the Port's
cruise berths as a mark on the axis with the vessel, the time, the berth and
where it is coming from or going to. Underneath, on exactly the same axis, the
predicted tide at Fort Point and a ribbon of the predicted current at the
Golden Gate -- warm where the flood is running in, cold where the ebb is
running out, dark and still at the turn.

That pairing is the whole point. Big ships are handled around the water: the
Bay entrance runs to five knots and a loaded ship crossing it wants the flood
behind her and slack under her at the berth, so a movement mark sitting on one
of the dark bands in the ribbon is a ship working with the tide, and one that
is not is a schedule that has other constraints. It is a fact you can read off
this panel in about two seconds and cannot get from either half alone.

**Where the ships come from, and why not AIS.** Every live AIS feed within
reach wants a key -- aisstream.io, MarineTraffic and VesselFinder all register
you first, AISHub's price is a receiver of your own -- and the Marine Exchange
of the San Francisco Bay Region, which does publish precisely the arrivals and
departures report this wants, sells it to members. There is no keyless
live-position feed for this bay, so there are no moving ships on this panel and
nothing here pretends otherwise. What is public and authoritative is the Port
of San Francisco's own cruise terminal schedule, and cruise ships are the
biggest things that come through the Gate on a published timetable.

**These are berth times, not bridge times.** The schedule says when a ship is
alongside Pier 27 or Pier 35, and a ship alongside at 07:00 passed under the
Golden Gate the better part of an hour earlier. Nothing here converts between
the two: the offset depends on the pilot, the ship and the day, and an invented
one drawn to the minute would look exactly as authoritative as the published
number beside it. The mark is where the Port says the ship is due; the water
under it is the water at that moment.

**Data comes from the cache, never from the network.** `ftdata.py` fetches on a
timer in a process of its own -- the schedule PDF every six hours, the NOAA
predictions on the ordinary pass -- and this reads the files it leaves behind.
It does not import a HTTP library and `render()` does not touch the disk at
all, because the scheduler builds the next segment on a worker thread and a
`build()` blocked on a socket stops the render loop through the GIL. Run the
fetcher first:

    $ python3 ftdata.py --loop 900

Nothing here believes a file because it parsed. A schedule is a promise about
the future and the tide predictions are a calculation about it, so the test is
not only how old the fetch is but whether what it contains still reaches
forward past now. When the schedule is gone or past its TTL the panel says so
in words; when the predictions have run out from under the window the water is
not drawn. An empty board with an honest reason on it beats a confident one
built out of nothing.

**Frame budget.** A 20 fps segment is 50 ms a frame on a Pi 3 held at 600 MHz,
and a bare numpy call costs 55-80 us there whatever the array size, so what
matters is how many calls a frame makes rather than how big they are. Almost
everything on this panel is a function of the window and of nothing else -- the
curve, the ribbon's colours, the ticks, every caption -- so it is rasterised
once and copied. A frame is that copy plus three overlays: the past half of the
curve painted under the cursor off a precomputed mask, the breathing now
cursor, and the stipple. About fifteen numpy calls on 320-element arrays.

Measured on this desktop: **0.020 ms p95** a frame, 0.9 ms for `build()`, and
0.6 ms for the one frame in which the window steps, which happens twice an
hour. At the 76-114x ratio this project keeps measuring -- and every optimistic
extrapolation in it has been wrong low -- that is 1.5 to 2.5 ms a frame on the
wall against a 50 ms budget, with about one 70 ms hitch every half hour when
the axis steps. The first static picture is drawn in `build()` rather than on
the first frame, so the frame being crossfaded into is not the expensive one.
"""

import math
import sys
import time

import numpy as np

import demoscene as ds
import ftdata

# The tide demo already owns the pieces this needs and owns them well: a 3x5
# font, a clock that --at can move, an interpolated series, and the readers
# that turn a CO-OPS record into something with the malformed cases already
# caught. Copying two hundred lines of that here would give this panel a second
# place for the same bug to be fixed, so it imports them. Nothing in tide.py's
# module body is expensive -- the DEM is only read inside its build().
import tide

f32 = np.float32

text_mask = tide.text_mask
text_width = tide.text_width
blit_text = tide.blit_text

SHIPS_PRODUCT = "sfport-cruise"
TIDE_STATION = tide.TIDE_STATION
CURRENT_STATION = tide.CURRENT_STATION

# Shared with tide.py on purpose: the two panels are neighbours in the same
# rotation and flood being the same orange on both is worth more than either
# one having its own scheme.
C_FLOOD = tide.C_FLOOD
C_EBB = tide.C_EBB
C_SLACK = tide.C_SLACK
C_FUTURE = tide.C_FUTURE
C_PAST = tide.C_PAST
C_FILL = tide.C_FILL
C_TEXT = tide.C_TEXT
C_DIM = tide.C_DIM
C_MARK = tide.C_MARK
C_WARN = tide.C_WARN

# In and out. Green and pink rather than the obvious warm/cold pair, because
# warm and cold are already spoken for by flood and ebb three rows below and a
# green arrival mark over an orange flood band has to stay readable as two
# different facts.
C_ARR = (110, 235, 160)
C_DEP = (255, 130, 175)

C_RULE = (14, 16, 20)
C_AXIS = (52, 60, 72)
C_AXIS_LINE = (74, 84, 100)             # the baseline the marks stand on
C_GUIDE = (34, 40, 50)                  # the faint verticals at slack water
C_DRIFT = (208, 226, 242)               # the stipple riding the current ribbon

# Full scale for the ribbon's colour. The Gate predicts to about four and a
# half knots on a big spring, and clipping the top of the range costs less than
# making every ordinary afternoon look like nothing is happening.
RIBBON_FULL_KN = 3.2

# Below this the water is called slack. NOAA labels the instant; a panel needs
# a band, because a mark two pixels from an instant means nothing. A third of a
# knot is roughly the last twenty minutes either side of the turn at the Gate.
SLACK_KN = 0.35

# Columns between stipple dots on the ribbon. Nine is far enough apart that a
# dot reads as a dot rather than as a dashed line, and close enough that a
# flood two hours from its peak still has three or four of them in it.
DOT_GAP = 9.0

# How far ahead the tide record has to reach before the water is worth drawing
# at all. Less than half a day forward and the window is mostly empty axis.
MIN_AHEAD = 12 * 3600.0

# How coarsely the left edge of the axis is stepped. See window().
WINDOW_STEP = 1800.0


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
#
# Three records, three separate ways of being absent, and they are not equally
# fatal. No schedule means there is no board and the panel says so. No tide
# means the board still draws with no water under it. No current means the
# ribbon goes away and the marks lose their phase line -- a real loss, since
# the phase is the point, but not a reason to throw the arrivals away too.
# --------------------------------------------------------------------------

def berth_short(s):
    """'PIER 35S' -> 'P35S'. Three characters of 'PIER' for six of vessel."""
    s = (s or "").strip().upper()
    return ("P" + s[4:].strip()) if s.startswith("PIER") else s


def read_calls(cache_dir):
    """The scheduled calls, as a record, or (None, age, why)."""
    got = ftdata.load(SHIPS_PRODUCT, cache_dir)
    if got is None:
        return None, None, "no cached ship schedule"
    payload, age = got
    try:
        raw = payload["calls"]
    except Exception:                                        # noqa: BLE001
        return None, age, "ship schedule is malformed"
    calls = []
    for c in raw:
        try:
            eta = c.get("eta")
            etd = c.get("etd")
            calls.append({
                "vessel": str(c.get("vessel") or "").upper(),
                "berth": berth_short(c.get("berth")),
                "line": str(c.get("line") or "").upper(),
                "type": str(c.get("type") or "").upper(),
                "eta": float(eta) if eta is not None else None,
                "etd": float(etd) if etd is not None else None,
                "from": str(c.get("from") or "").upper(),
                "to": str(c.get("to") or "").upper()})
        except Exception:                                    # noqa: BLE001
            continue
    if not calls:
        return None, age, "ship schedule has no calls"
    return {"calls": calls, "age": age,
            "port": str(payload.get("port") or "SAN FRANCISCO"),
            "revised": payload.get("revised")}, age, None


def movements(calls):
    """One entry per thing that happens, sorted in time.

    A call is a ship arriving and later leaving, which is two movements and not
    one. Splitting them here rather than at drawing time is what lets a berth
    with an overnight call put its arrival on Monday's axis and its departure
    on Tuesday's without either of them being a special case.
    """
    out = []
    for c in calls:
        for kind, t, port, lead in (("ARR", c["eta"], c["from"], "FM"),
                                    ("DEP", c["etd"], c["to"], "TO")):
            if t is None:
                continue
            # 'N/A' is what the sheet prints when the next port is not settled
            # yet. Drawing the letters N/A on a wall says nothing to anybody.
            where = "" if port in ("", "N/A", "TBA") else "%s %s" % (lead, port)
            out.append({"t": float(t), "kind": kind, "vessel": c["vessel"],
                        "berth": c["berth"], "line": c["line"],
                        "type": c["type"], "where": where})
    out.sort(key=lambda m: m["t"])
    return out


def next_movement(moves, now):
    for m in moves:
        if m["t"] >= now:
            return m
    return None


def until_words(seconds):
    """'IN 3H 20M', 'IN 2D 4H'. Coarse on purpose; this is a countdown to a
    ship, not to a rocket, and the schedule is not accurate to the minute."""
    s = max(0.0, seconds)
    if s < 3600:
        return "IN %dM" % int(s / 60)
    if s < 86400:
        return "IN %dH %02dM" % (int(s / 3600), int(s % 3600) / 60)
    return "IN %dD %dH" % (int(s / 86400), int(s % 86400) / 3600)


def phase_text(v, metric):
    """What the water is doing, as words and a colour, or None if unknown."""
    if v is None:
        return None
    if abs(v) < SLACK_KN:
        return "SLACK", C_SLACK
    scale, unit = (tide.KNOT_MS, "M/S") if metric else (1.0, "KN")
    word, rgb = ("FLOOD", C_FLOOD) if v > 0 else ("EBB", C_EBB)
    return "%s %.1f%s" % (word, abs(v) * scale, unit), rgb


# --------------------------------------------------------------------------
# Layout.
# --------------------------------------------------------------------------

class Layout(object):
    """Four registers stacked on one shared time axis.

    The proportions are fixed rather than fitted because the thing that has to
    survive a change of panel size is the *order*: header, board, axis, ribbon,
    water. A board that has shrunk to eight rows still says which ship and
    when; a ribbon that has lost its third row still says which way.
    """

    def __init__(self, w, h, board_rows=0):
        self.w, self.h = w, h
        self.head_h = 6 if h >= 24 else 0
        self.tick_h = 5 if h >= 56 else (1 if h >= 24 else 0)
        self.ribbon_h = 3 if h >= 48 else (2 if h >= 24 else 1)
        rest = h - self.head_h - self.ribbon_h - self.tick_h
        if board_rows <= 0:
            board_rows = int(round(rest * 0.58))
        self.board_h = max(4, min(rest - 6, board_rows))
        self.board_y = self.head_h
        self.axis_y = self.board_y + self.board_h - 1
        self.ribbon_y = self.board_y + self.board_h
        self.curve_y = self.ribbon_y + self.ribbon_h
        self.curve_h = max(4, h - self.tick_h - self.curve_y)
        self.tick_y = self.curve_y + self.curve_h


def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--tide-station", default=TIDE_STATION,
                    help="NOAA CO-OPS water level station")
    ap.add_argument("--current-station", default=CURRENT_STATION,
                    help="NOAA CO-OPS current prediction station")
    ap.add_argument("--span", type=float, default=60.0,
                    help="most hours across the panel; the axis is clipped to "
                         "wherever the NOAA predictions actually stop, which "
                         "is between two and three days out depending on the "
                         "hour, so the real span is usually a little less")
    ap.add_argument("--past", type=float, default=5.0,
                    help="hours of it behind now, so a movement stays on the "
                         "panel for a while after it has happened")
    ap.add_argument("--board-rows", type=int, default=0,
                    help="rows given to the movement board (0 = about 58%%)")
    ap.add_argument("--max-labels", type=int, default=6,
                    help="most labelled movements at once; the rest keep their "
                         "mark on the axis and lose their caption")
    ap.add_argument("--drift", type=float, default=2.2,
                    help="stipple columns per second at one knot on the "
                         "current ribbon; the water is a time lapse")
    ap.add_argument("--metric", action="store_true",
                    help="metres and metres per second instead of feet and knots")
    ap.add_argument("--24h", dest="h24", action="store_true", help="24 hour clock")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second")


# --------------------------------------------------------------------------
# The static picture. Everything here is redrawn a few times an hour at most.
# --------------------------------------------------------------------------

def draw_water(dst, lay, curve, t0, t1):
    """The predicted tide across the window, drawn as if it were all ahead.

    Same treatment as tide.py's curve and for the same reason: on a panel this
    size a one-pixel plot of a function that moves three rows in a column is a
    dotted line and reads as noise, so the curve is a filled band between
    neighbouring columns.

    What is *not* done here is the "behind now" half of it, and that is a
    deliberate split. Filled behind the marker and hollow ahead of it is what
    makes the direction of time obvious across a room, but it is also the one
    thing in this whole static picture that moves every minute, and redrawing
    the panel every minute to keep it in step would cost a dropped frame every
    minute. So the masks come back instead and the frame loop paints the past
    itself, which keeps the fill exactly under the cursor for four numpy calls.
    """
    w = lay.w
    ts = t0 + (np.arange(w, dtype=np.float64) + 0.5) * (t1 - t0) / w
    hv = curve.at(ts)
    lo, hi = float(hv.min()), float(hv.max())
    if hi - lo < 0.5:
        lo, hi = lo - 0.25, hi + 0.25
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad

    top_r = lay.curve_y
    bot_r = lay.curve_y + lay.curve_h - 1
    rows = np.clip(bot_r - (hv - lo) / (hi - lo) * (bot_r - top_r), top_r, bot_r)
    ri = np.round(rows).astype(int)
    nxt = np.empty_like(ri)
    nxt[:-1] = ri[1:]
    nxt[-1] = ri[-1]

    yy = np.arange(lay.curve_y, lay.curve_y + lay.curve_h)[:, None]
    reg = dst[lay.curve_y:lay.curve_y + lay.curve_h]
    on = (yy >= np.minimum(ri, nxt)[None, :]) & (yy <= np.maximum(ri, nxt)[None, :])
    under = yy > np.maximum(ri, nxt)[None, :]
    reg[on] = C_FUTURE
    return (lo, hi), on, under


def day_marks(t0, t1):
    """[(epoch, hour)] for every local midnight and six-hour mark in a window.

    Stepped off local midnight rather than off a multiple of six hours in epoch
    seconds, which is the same thing only in Greenwich. Doing the arithmetic in
    epoch put the "midnight" ticks at 17:00, 23:00, 05:00 and 11:00 Pacific and
    the weekday labels never drew at all, because no tick ever landed on hour
    zero. The day is stepped by asking for the local date thirty-six hours on
    and taking midnight of *that*, so the one day a year that is twenty-five
    hours long is still one day here.
    """
    lt = time.localtime(t0)
    day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    out = []
    while day < t1 + 86400.0:
        for hour in (0, 6, 12, 18):
            lt = time.localtime(day)
            t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                             hour, 0, 0, 0, 0, -1))
            if t0 <= t <= t1:
                out.append((t, hour))
        lt = time.localtime(day + 36 * 3600.0)
        day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return out


def draw_ticks(dst, lay, t0, t1, h24):
    """Midnight and the six-hour marks, with the weekday spelled out.

    A two-day axis with nothing on it is two days of nowhere in particular.
    Three letters at each midnight is the cheapest thing that turns a position
    on the panel into a day of the week.
    """
    if lay.tick_h <= 0:
        return
    w = lay.w
    for t, hour in day_marks(t0, t1):
        c = col_of(t, t0, t1, w)
        if hour == 0:
            dst[lay.tick_y:lay.tick_y + lay.tick_h, c] = C_DIM
            if lay.tick_h >= 5:
                label = time.strftime("%a", time.localtime(t)).upper()
                lx = min(c + 2, w - text_width(label))
                blit_text(dst, lay.tick_y, max(0, lx), label, C_TEXT)
        else:
            dst[lay.tick_y, c] = C_AXIS
            if lay.tick_h >= 5 and hour == 12:
                label = tide.hhmm(t, not h24).replace(":00", "")
                if c + 2 + text_width(label) <= w:
                    blit_text(dst, lay.tick_y, c + 2, label, C_AXIS)


def ribbon_colours(vel, t0, t1, w):
    """(w, 3) uint8: the predicted current at each column, as a colour.

    Flood warm, ebb cold, and the turn genuinely dark rather than merely a
    different hue -- the dark bands are what a movement mark gets compared
    against, so they have to survive being looked at from across a room.
    """
    ts = t0 + (np.arange(w, dtype=np.float64) + 0.5) * (t1 - t0) / w
    v = np.asarray(vel.at(ts), f32)
    k = np.clip(np.abs(v) / RIBBON_FULL_KN, 0.0, 1.0)[:, None]
    warm = np.array(C_FLOOD, f32)
    cold = np.array(C_EBB, f32)
    base = np.array((10, 12, 16), f32)
    hue = np.where((v > 0)[:, None], warm, cold)
    return (base + (hue - base) * k).astype(np.uint8), v


def col_of(t, t0, t1, w):
    return int(np.clip((t - t0) / (t1 - t0) * w, 0, w - 1))


def present_run(vcol, cursor):
    """(first, last, signed knots) of the flood or ebb that now is inside.

    The only part of this panel that is allowed to move, and the reason it is
    worth being careful about which part. Everything on the axis is *time*, not
    distance, so drifting anything along it at the speed of the water would be
    a picture of nothing: two adjacent columns are twenty minutes apart, not
    twenty metres, and a stipple whose phase advanced by the local current
    would come apart into noise within seconds of being switched on -- which is
    exactly what the first version of it did.

    What is honest to animate is the water *now*, over the span of the present
    flood or ebb: from the turn behind us to the turn ahead. That run is one
    sign of velocity, so a stipple drifting across it at one uniform speed says
    a true thing -- which way it is running, how hard, and how much of it is
    left -- and cannot alias, because its phase gradient is constant.
    """
    n = len(vcol)
    cursor = int(np.clip(cursor, 0, n - 1))
    v = float(vcol[cursor])
    if abs(v) < SLACK_KN:
        return None
    sign = v > 0
    lo = hi = cursor
    while lo > 0 and (vcol[lo - 1] > 0) == sign and abs(vcol[lo - 1]) >= 0.02:
        lo -= 1
    while hi < n - 1 and (vcol[hi + 1] > 0) == sign and abs(vcol[hi + 1]) >= 0.02:
        hi += 1
    return (lo, hi, v) if hi - lo >= 3 else None


def label_forms(m, phase, h24):
    """The captions for one movement, widest first.

    A ladder rather than a truncation, because what falls off the end of a
    truncated caption is the part that says which ship. Every rung still names
    the movement and its time; the rungs above add the berth, then the water,
    then where it is coming from or going to.
    """
    when = tide.hhmm(m["t"], not h24)
    head = "%s %s" % (m["kind"], when)
    name = m["vessel"]
    forms = []
    if phase and m["where"]:
        forms.append([name, "%s %s" % (head, m["berth"]), phase[0], m["where"]])
    if phase:
        forms.append([name, "%s %s" % (head, m["berth"]), phase[0]])
    if m["where"]:
        forms.append([name, "%s %s" % (head, m["berth"]), m["where"]])
    forms.append([name, "%s %s" % (head, m["berth"])])
    forms.append([name, head])
    forms.append([name[:10], head])
    forms.append([head])
    return forms


def draw_board(dst, lay, moves, t0, t1, now, phases, h24, max_labels):
    """The movement marks and as many captions as the panel can honestly hold.

    Placing captions is the only fiddly part. Two movements of the same call
    are typically nine hours apart, which on a two-day axis is sixty pixels --
    almost exactly one caption wide -- so they collide about as often as not.
    The answer is the same one the tide labels use: try the widest form, then
    shorter ones, and if none of them fits without overlapping something
    already placed, drop the caption and leave the mark. A mark with no words
    is a movement you can still see; two captions on top of each other is
    neither.
    """
    w = lay.w
    dst[lay.axis_y, :] = C_AXIS_LINE
    boxes = []
    drawn = 0
    for m in moves:
        c = col_of(m["t"], t0, t1, w)
        rgb = C_ARR if m["kind"] == "ARR" else C_DEP
        dim = m["t"] < now
        mark = tuple(int(v * 0.45) for v in rgb) if dim else rgb
        # The mark: three columns at the axis so it has weight, and a stem up
        # into the board so the eye can follow it to the caption.
        dst[lay.axis_y - 1:lay.axis_y + 2, max(0, c - 1):c + 2] = mark
        m["col"] = c

        if drawn >= max_labels or lay.board_h < 12:
            continue
        placed = None
        for form in label_forms(m, phases.get(id(m)), h24):
            tw = max(text_width(s) for s in form)
            th = len(form) * 6 - 1
            top = lay.axis_y - 3 - th
            if top < lay.board_y:
                continue
            lx = c - tw // 2
            lx = max(0, min(lx, w - tw))
            box = (lx - 2, top - 1, lx + tw + 2, top + th + 1)
            if any(box[0] < b[2] and b[0] < box[2]
                   and box[1] < b[3] and b[1] < box[3] for b in boxes):
                continue
            placed = (form, lx, top, box)
            break
        if placed is None:
            continue
        form, lx, top, box = placed
        boxes.append(box)
        drawn += 1
        # The stem runs from the top of the caption down to the mark, so a
        # caption nudged sideways to fit is still visibly attached to its ship.
        dst[top + th + 1:lay.axis_y - 1, c] = mark
        y = top
        for i, s in enumerate(form):
            phase = phases.get(id(m))
            if phase and i == 2 and s == phase[0]:
                rgb_line = phase[1] if not dim else C_DIM
            elif i == 0:
                rgb_line = C_TEXT if not dim else C_DIM
            else:
                rgb_line = C_MARK if i == 1 and not dim else C_DIM
            blit_text(dst, y, lx, s, rgb_line)
            y += 6
    return drawn, boxes


def draw_later(dst, lay, moves, t1, boxes, h24, rows=3):
    """A short ledger of what comes after the right-hand edge of the axis.

    Two and a half days is a busy window in September and an empty one in
    February -- cruise calls here run from two a week in winter to two a day in
    the autumn -- so on a quiet day the board is thirty rows of nothing with the
    next ship four days off the end of it. The axis cannot be widened to reach
    her: the tide predictions stop where they stop, and an axis drawn past them
    would be a time scale with no water under half of it.

    So the ledger, in the space the marks are not using, clearly dated and
    clearly not on the axis. It is drawn last and skipped entirely wherever a
    caption already is, which is what keeps it out of the way on the busy days
    when it is not needed anyway.
    """
    later = [m for m in moves if m["t"] > t1][:rows]
    if not later or lay.board_h < 14:
        return 0
    # A weekday alone stops being a date after a week -- "THU" three weeks out
    # is the same three letters as "THU" tomorrow, and February's schedule is
    # sparse enough for that to happen. Past six days it gets the day of the
    # month too.
    lines = ["LATER"] + [
        "%s %s %s" % (time.strftime(
            "%a" if m["t"] - t1 < 6 * 86400 else "%a %d",
            time.localtime(m["t"])).upper(),
            tide.hhmm(m["t"], not h24), m["vessel"][:14])
        for m in later]
    tw = max(text_width(s) for s in lines)
    th = len(lines) * 6 - 1
    top = lay.board_y + 1
    for lx in (1, lay.w - tw - 1):
        box = (lx - 2, top - 1, lx + tw + 2, top + th + 1)
        if any(box[0] < b[2] and b[0] < box[2]
               and box[1] < b[3] and b[1] < box[3] for b in boxes):
            continue
        blit_text(dst, top, lx, lines[0], C_DIM)
        y = top + 6
        for m, s in zip(later, lines[1:]):
            blit_text(dst, y, lx, s, C_ARR if m["kind"] == "ARR" else C_DEP)
            y += 6
        boxes.append(box)
        return len(later)
    return 0


def draw_guides(dst, lay, events, t0, t1):
    """A faint vertical at every predicted slack, through the board only.

    Through the board and not through the water, because in the water register
    the ribbon already says it in colour and a line over the curve would read
    as part of the curve. These are the things a movement mark is being
    compared against; they want to be visible and they want to be quiet.
    """
    for t, kind, _v in events:
        if kind != "slack" or not (t0 <= t <= t1):
            continue
        c = col_of(t, t0, t1, lay.w)
        reg = dst[lay.board_y:lay.axis_y, c]
        np.maximum(reg, np.array(C_GUIDE, np.uint8), out=reg)


def header_text(state, h24, w=ds.WIDTH):
    """The status line: what this is, what is next, and how old the data is.

    Ladders on all three, and the age is the last thing to go, which is why it
    gets its own rung at the shortest form. A panel that has quietly stopped
    saying how old it is looks exactly like one that is up to date.
    """
    port = state.get("port") or "SAN FRANCISCO"
    lefts = ["%s MOVEMENTS" % port, "%s SHIPS" % port.split()[-1], "SHIPS"]

    nxt = state.get("next")
    if nxt is None:
        mids, midc = ["NO SCHEDULED CALLS", "NO CALLS", ""], C_DIM
    else:
        until = until_words(nxt["t"] - state["now"])
        mids = ["NEXT %s %s %s" % (nxt["kind"], nxt["vessel"], until),
                "NEXT %s %s" % (nxt["vessel"], until),
                "%s %s" % (nxt["vessel"], until),
                until]
        midc = C_ARR if nxt["kind"] == "ARR" else C_DEP

    age = state.get("age")
    right = "DATA " + ftdata.describe_age(age) if age is not None else ""
    rights = ([("STALE " + right).strip(), "STALE"] if state.get("stale")
              else [right, ftdata.describe_age(age) if age is not None else ""])

    gap = 5
    for left in lefts:
        for right in rights:
            for mid in mids:
                need = text_width(left) + (text_width(right) if right else 0) + 2
                if mid:
                    need += text_width(mid) + 2 * gap
                if need <= w:
                    return left, mid, midc, right
    return lefts[-1], "", midc, ""


def draw_header(dst, lay, state, h24):
    if lay.head_h <= 0:
        return
    left, mid, midc, right = header_text(state, h24, lay.w)
    dst[:lay.head_h] = 0
    blit_text(dst, 0, 1, left, C_TEXT)
    rw = text_width(right) if right else 0
    if right:
        blit_text(dst, 0, lay.w - rw - 1, right,
                  C_WARN if state.get("stale") else C_DIM)
    if mid:
        mw = text_width(mid)
        mx = min(lay.w - rw - 4 - mw,
                 max(text_width(left) + 5, (lay.w - mw) // 2))
        blit_text(dst, 0, max(0, mx), mid, midc)
    dst[lay.head_h - 1] = C_RULE


def draw_nodata(dst, lay, lines):
    """The honest panel. No axis, no marks, no implication of a schedule."""
    dst[:] = (6, 6, 8)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.h // 2 - (len(lines) * (6 * scale + 2)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        blit_text(dst, y, x, s, rgb, sc)
        y += 5 * sc + 3
    return dst


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h, args.board_rows)
    cache = args.cache_dir
    now_of = tide.clock(tide.parse_when(args.at), args.rate)

    # Read once. `render()` never returns here, which is the difference between
    # this and tide.py's --reload: the rule this side of the wall is that the
    # frame loop touches no files, and the scheduler rebuilds a segment every
    # time it comes round anyway. The cost is that a long standalone run holds
    # one snapshot -- so the age shown on the panel is *advanced* below rather
    # than frozen, and the panel goes stale on its own when it should.
    ships, ships_age, ships_err = read_calls(cache)
    tide_rec, _tage, tide_err = tide.read_tide(cache, args.tide_station)
    cur_rec, _cage, cur_err = tide.read_current(cache, args.current_station)
    built_at = now_of()

    moves = movements(ships["calls"]) if ships else []

    # The stipple's spatial phase, built once. Cheap either way, but building
    # it per frame would be an allocation and an arange in the render loop for
    # an array that never changes.
    xs = np.arange(w, dtype=f32) * (1.0 / DOT_GAP)

    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)
    header = np.zeros((max(1, lay.head_h), w, 3), np.uint8)

    cell = {"key": None, "head_key": None, "window": None, "next": None,
            "drawn": 0, "later": 0, "ribbon": None, "vcol": None, "run": None,
            "cursor": 0, "on": None, "under": None}

    def window(now):
        """(t0, t1), stepped in half hours and clipped to the predictions.

        Two decisions in four lines, and both of them are about how often the
        rest of the panel has to be redrawn.

        The left edge is **quantised**, because a window measured continuously
        from now slides a fraction of a pixel a minute and every label, tick
        and curve column with it -- which means the whole static picture is
        wrong the moment it is drawn. Stepping it in half hours costs three
        pixels of drift, lets the now cursor traverse between steps the way
        tide.py's does, and turns a redraw every few minutes into one every
        thirty.

        The right edge is **clipped** to where the predictions actually stop --
        to *whichever runs out first*, which is the part that had to be learned.
        The two CO-OPS series are fetched over the same GMT dates but sampled
        differently, six minutes for the water level and thirty for the
        current, so the current's last sample is up to half an hour short of
        the tide's. Clipping to the tide alone put twenty-nine minutes of axis
        past the end of the current series, `covers()` refused it, and the
        entire ribbon and every phase line silently went away -- a completely
        plausible-looking panel with the best thing on it missing. Both, then.
        When there is less than half a day left there is no useful panel here
        at all, and render() says so instead of drawing one.
        """
        t0 = math.floor((now - args.past * 3600.0) / WINDOW_STEP) * WINDOW_STEP
        t1 = t0 + args.span * 3600.0
        if tide_rec is not None:
            t1 = min(t1, tide_rec["curve"].t1)
        if cur_rec is not None:
            t1 = min(t1, cur_rec["vel"].t1)
        return t0, max(t1, now + 60.0)

    def stale_now(now):
        """Age is measured from the fetch, and the fetch does not get younger.

        A demo that read the cache once at build time and then reported that
        age forever would say '2M' on a panel that had been up since Tuesday.
        The elapsed display time is added back on, so the corner counts up and
        the TTL eventually trips exactly as it would if this reread the file.
        """
        if ships_age is None:
            return None, True
        age = ships_age + max(0.0, now - built_at)
        return age, not ftdata.is_fresh(SHIPS_PRODUCT, age)

    def rebuild(now, t0, t1):
        static[:] = 0
        cell["window"] = (t0, t1)
        cell["run_col"] = None
        phases = {}
        cell["vis"] = vis = [m for m in moves if t0 <= m["t"] <= t1]

        if tide_rec is not None:
            _range, cell["on"], cell["under"] = draw_water(
                static, lay, tide_rec["curve"], t0, t1)
        else:
            cell["on"] = cell["under"] = None
            msg = "NO TIDE PREDICTIONS"
            blit_text(static, lay.curve_y + max(0, lay.curve_h // 2 - 2),
                      max(0, (w - text_width(msg)) // 2), msg, C_WARN)
        draw_ticks(static, lay, t0, t1, args.h24)

        if cur_rec is not None and cur_rec["vel"].covers(t0, t1):
            rib, vcol = ribbon_colours(cur_rec["vel"], t0, t1, w)
            static[lay.ribbon_y:lay.ribbon_y + lay.ribbon_h] = rib[None, :, :]
            cell["ribbon"], cell["vcol"] = rib, vcol
            for m in vis:
                phases[id(m)] = phase_text(float(cur_rec["vel"].value(m["t"])),
                                           args.metric)
            draw_guides(static, lay, cur_rec["events"], t0, t1)
        else:
            cell["ribbon"], cell["vcol"], cell["run"] = None, None, None
            static[lay.ribbon_y:lay.ribbon_y + lay.ribbon_h] = (12, 12, 14)

        drawn, boxes = draw_board(static, lay, vis, t0, t1, now, phases,
                                  args.h24, args.max_labels)
        cell["drawn"] = drawn
        if not vis:
            # An empty board is a true statement about the next two days and
            # has to look like one, not like a demo that failed to draw.
            msg = "NO CALLS ON THIS AXIS"
            mx = max(0, (w - text_width(msg)) // 2)
            my = lay.board_y + max(0, lay.board_h // 2 - 3)
            blit_text(static, my, mx, msg, C_DIM)
            boxes.append((mx - 2, my - 1, mx + text_width(msg) + 2, my + 6))
        cell["later"] = draw_later(static, lay, moves, t1, boxes, args.h24)
        cell["boxes"] = boxes

    def render(t, i):
        now = now_of()
        age, stale = stale_now(now)

        if ships is None or (tide_rec is None and cur_rec is None):
            lines = [("NO SHIP DATA", C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --LOOP 900", C_TEXT)]
            for p in [ships_err, tide_err, cur_err]:
                if p:
                    lines.append((p.upper()[:52], C_DIM))
            return draw_nodata(frame, lay, lines[:4])

        t0, t1 = window(now)
        if t1 - now < MIN_AHEAD:
            return draw_nodata(frame, lay, [
                ("PREDICTIONS RAN OUT", C_WARN),
                ("RUN  PYTHON3 FTDATA.PY --LOOP 900", C_TEXT),
                ("TIDE RECORD ENDS %s" % tide.hhmm(t1, not args.h24), C_DIM)])

        # The static picture is a function of the window and of nothing else,
        # so it is redrawn when the window steps -- twice an hour -- and not
        # otherwise. Everything below this line happens every frame.
        if (t0, t1) != cell["window"]:
            rebuild(now, t0, t1)
        t0, t1 = cell["window"]
        cell["cursor"] = c = col_of(now, t0, t1, w)
        if cell["next"] is None or now > cell["next"]["t"]:
            cell["next"] = next_movement(moves, now)
        # Which flood or ebb we are in changes when the cursor moves, which is
        # once every eleven minutes on a sixty-hour axis. Walking the run out
        # from the cursor every frame instead would be a hundred and fifty
        # thousand pointless comparisons an hour.
        if c != cell["run_col"]:
            cell["run_col"] = c
            cell["run"] = (present_run(cell["vcol"], c)
                           if cell["vcol"] is not None else None)

        state = {"now": now, "age": age, "stale": stale,
                 "next": cell["next"], "port": ships["port"]}
        hk = (stale, ftdata.describe_age(age) if age is not None else "",
              id(cell["next"]),
              int((cell["next"]["t"] - now) / 60) if cell["next"] else 0)
        if hk != cell["head_key"]:
            cell["head_key"] = hk
            draw_header(header, lay, state, args.h24)

        frame[:] = static
        if lay.head_h:
            frame[:lay.head_h] = header[:lay.head_h]

        # The past half of the curve, painted here rather than baked into the
        # static so that the fill edge and the cursor are the same column even
        # thirty minutes after the last redraw. Filled behind, hollow ahead:
        # that, and not the colour, is what makes the direction of time obvious
        # from across the room.
        if cell["on"] is not None and c > 0:
            reg = frame[lay.curve_y:lay.curve_y + lay.curve_h, :c]
            reg[cell["under"][:, :c]] = C_FILL
            reg[cell["on"][:, :c]] = C_PAST

        # The stipple, across the flood or ebb now is inside and nowhere else:
        # one uniform speed, the direction of the water, and it stops entirely
        # at the turn because the water does. See present_run().
        run = cell["run"]
        if run is not None and lay.ribbon_h > 0:
            lo, hi, v = run
            ph = xs[:hi - lo + 1] - v * (args.drift * (now - built_at) / DOT_GAP)
            np.mod(ph, 1.0, out=ph)
            reg = frame[lay.ribbon_y:lay.ribbon_y + lay.ribbon_h, lo:hi + 1]
            reg[:, ph < 0.18] = C_DRIFT

        # Now: a full-height cursor that breathes, so a still photograph of
        # this panel still says which column is the present moment.
        k = 0.72 + 0.28 * math.sin(t * 1.7)
        col = np.array([int(v * k) for v in C_MARK], np.uint8)
        frame[lay.head_h:lay.tick_y, c] = col
        if lay.tick_h:
            frame[lay.tick_y:lay.tick_y + 1, c] = col

        # And anything about to happen, or just having happened, pulses. Half
        # an hour either side: close enough that somebody in the room could go
        # and look at it. Over the movements on the axis, not over the whole
        # calendar -- the calendar is two hundred entries and this runs twenty
        # times a second.
        for m in cell["vis"]:
            if abs(m["t"] - now) > 1800.0 or "col" not in m:
                continue
            mc = m["col"]
            base = C_ARR if m["kind"] == "ARR" else C_DEP
            g = 0.55 + 0.45 * math.sin(t * 4.0)
            frame[lay.axis_y - 2:lay.axis_y + 3,
                  max(0, mc - 1):mc + 2] = [int(v * g) for v in base]
        return frame

    # The first static picture is drawn here and not on the first frame, which
    # is the difference between a segment that starts clean and one whose
    # opening frame is eighty times dearer than the rest of it -- and the
    # opening frame is the one being crossfaded into.
    if ships is not None and (tide_rec is not None or cur_rec is not None):
        t0, t1 = window(built_at)
        if t1 - built_at >= MIN_AHEAD:
            cell["next"] = next_movement(moves, built_at)
            rebuild(built_at, t0, t1)

    render.layout = lay
    render.movements = moves
    render.ships = ships
    render.tide = tide_rec
    render.current = cur_rec
    render.problems = [p for p in (ships_err, tide_err, cur_err) if p]
    render.cell = cell
    render.window_of = window
    return render


def main():
    ds.standalone(sys.modules[__name__],
                  "scheduled ship movements at San Francisco, against the tide",
                  fps=20)


if __name__ == "__main__":
    main()
