#!/usr/bin/env python3
"""Two bad computer players, one ball, and a score that has been running since August.

Pong, on a panel that is exactly the right shape for it -- a 5:1 letterbox is
a Pong court and nothing else on this wall is. Black ground, a dashed net, two
blocky paddles, a square ball, and the score in slab numerals across the top.
No gradients, no glow, no particles. The austerity is the whole look.

**The score is the point.** It is not the score of this rally and it is not
the score since the panel came up: it is every point these two machines have
played since 00:00 UTC on 1 August 2026, and it keeps climbing whether or not
anybody is in the room. Walk past twice in a month and the numbers have moved
by tens of thousands, and the small grey line along the bottom tells you which
of them is winning the long game and by how much. That is the reason to look
at this panel a second time.

**How a score survives a power cut with no storage.** A demo may not write
files and render() must be a pure function of t, so the match cannot be
simulated forward and remembered. Instead the whole thing is a closed-form
function of the wall clock, the same trick dvd.py uses to give its corner
counter a history:

  * Rally durations are made *computable*, not emergent. The ball's
    **horizontal** speed is the clock: it starts at v0, is multiplied by
    `--rally-gain` on every return, and is capped. A rally with R returns
    therefore lasts serve-pause + half a court at v0 + R-1 courts at the
    successive speeds + the run-off past the beaten paddle + the celebration,
    and that is a closed form in (R, v0). Everything vertical -- the angles,
    the wall bounces, the paddles flailing -- changes what you watch and
    changes the duration by exactly nothing. That single separation is what
    makes the rest possible.
  * The parameters of rally j -- R, v0, and who loses -- come from a
    RandomState seeded once in build(), for j in [0, --book) only. Rally n
    uses slot j = n mod M. So the *book* of rallies repeats every M of them,
    which at the default M = 4096 is about nine hours; nobody stands in front
    of an LED wall for nine hours, and in exchange the cumulative time and the
    cumulative score are two `cumsum`s of length M.
  * Hence, at any absolute time u since the epoch: block = u // S where S is
    the book's total duration, j = searchsorted(cum_dur, u mod S), and

        left(u)  = block * K + cumL[j]        right(u) = block * (M-K) + cumR[j]

    with K the number of rallies in the book that LEFT wins. K is *exact*, not
    sampled: the book's outcomes are a shuffle of a multiset with exactly K
    wins in it, so the long-run edge is a designed number rather than a
    coin-flip that might have come out the wrong way round for this seed.

The result needs no history, no file, and no continuity across restarts: reboot
the Pi, redeploy the demo, change the rotation, and the score is still right,
because it was never being counted -- it was being computed. `--epoch N` pins
the clock so tests and screenshots are deterministic, exactly as `dvd --epoch`
does.

**The two players are deliberately bad, and badly in different ways.** A
competent Pong AI produces an infinite rally and nothing ever happens.

  * **LEFT lunges.** Reaction 0.10 s and 108 px/s, the faster paddle by a
    long way -- but it commits early. For the first 55% of the ball's flight
    it chases a straight-line extrapolation that *ignores the top and bottom
    walls*, so on any shot that bounces it charges confidently to the wrong
    end of the court and then has to sprint back. When it is not receiving it
    pre-positions where it guesses the ball will return to. It is never still.
  * **RIGHT plods.** Reaction 0.42 s and 64 px/s. It aims at the truth with a
    small steady bias and never overshoots, and between shots it slides back
    to the middle of the court and waits. It loses points by arriving late,
    never by going the wrong way.

Over the long run steady beats flashy by a whisker: `--edge` is the fraction of
points LEFT wins and defaults to 0.4934, so RIGHT gains about 54 points every
4096 -- roughly 150 a day, which is a lead you can watch grow over a month and
never a blowout.

**The angle off the paddle comes from where it hit**, as the 1972 machine does
-- a hit near the tip leaves steeply, a hit on the middle leaves flat. It is
one line and it is most of what makes the play legible. It is bent only when
it has to be: the outgoing angle is chosen from a grid of candidates as the
one closest to what the hit offset asks for *among those the receiving paddle
can actually reach in time*, so a rally cannot end by accident on a shot
neither player was ever going to make. The one shot that is allowed to be
unreachable is the last one, and which player is beaten by it was decided by
the book, not by the geometry.

**Cost.** The net, the walls, the score and the grey readout change only when
the score does, so they are composited once per rally into a single frame-sized
array; a frame is one copy of that plus three rectangle fills and two
`np.interp` calls on a few dozen keypoints. Nothing here is per-pixel.

Run:  python3 pong.py --host 127.0.0.1
      python3 pong.py --epoch 1786577088 --duration 60   # deterministic
      python3 pong.py --edge 0.6 --rally-gain 1.12       # a rout, and fast
"""

import sys
import time

import numpy as np

import defcon
import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The epoch. The match started here and has not stopped. Fixed in the source
# rather than taken from the clock, because a score that resets when the demo
# is redeployed is not a score, it is a session counter.
# --------------------------------------------------------------------------

MATCH_EPOCH = 1785542400.0          # 2026-08-01 00:00:00 UTC


# --------------------------------------------------------------------------
# Type.
#
# Two faces, both bitmaps in this file, both *measured* rather than assumed --
# a past demo in this tree assumed a size and clipped the bottom off every
# capital E.
#
# The score is a 5x7 slab set with one-unit strokes, drawn here rather than
# taken from a typeface: at this size a real face is a smudge, and the whole
# point is the 1972 look, which is a segment display with the corners filled
# in. It is scaled by whole numbers only, so the strokes stay 2 or 3 px and
# hard-edged.
#
# The small grey readout along the bottom uses defcon.py's 3x5 font, the same
# one caiso, dvd, propagation, sort and tide draw with.
# --------------------------------------------------------------------------

_DIGITS = {
    "0": ("11111", "10001", "10001", "10001", "10001", "10001", "11111"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("11111", "00001", "00001", "11111", "10000", "10000", "11111"),
    "3": ("11111", "00001", "00001", "11111", "00001", "00001", "11111"),
    "4": ("10001", "10001", "10001", "11111", "00001", "00001", "00001"),
    "5": ("11111", "10000", "10000", "11111", "00001", "00001", "11111"),
    "6": ("11111", "10000", "10000", "11111", "10001", "10001", "11111"),
    "7": ("11111", "00001", "00001", "00010", "00100", "00100", "00100"),
    "8": ("11111", "10001", "10001", "11111", "10001", "10001", "11111"),
    "9": ("11111", "10001", "10001", "11111", "00001", "00001", "11111"),
}

_DIGIT_GRID = {}
for _ch, _rows in _DIGITS.items():
    _g = np.zeros((len(_rows), len(_rows[0])), bool)
    for _r, _row in enumerate(_rows):
        for _c, _v in enumerate(_row):
            _g[_r, _c] = (_v == "1")
    _DIGIT_GRID[_ch] = _g

DIGIT_H, DIGIT_W = _DIGIT_GRID["0"].shape
DIGIT_ADVANCE = DIGIT_W + 1         # one blank column between digits


def digits_mask(value, scale):
    """A bool mask for a non-negative integer, measured from the glyphs."""
    s = "%d" % max(0, int(value))
    out = np.zeros((DIGIT_H, len(s) * DIGIT_ADVANCE - 1), bool)
    for i, ch in enumerate(s):
        g = _DIGIT_GRID.get(ch, _DIGIT_GRID["0"])
        out[:, i * DIGIT_ADVANCE:i * DIGIT_ADVANCE + DIGIT_W] = g
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def digits_width(ndigits, scale):
    """Exactly what digits_mask() will produce, without producing it."""
    return (ndigits * DIGIT_ADVANCE - 1) * scale


# defcon's 3x5: five rows a glyph, each row an octal digit whose three bits
# are the three columns.
_SMALL = {}
for _ch, _rows in defcon._FONT.items():
    _g = np.zeros((len(_rows), 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _SMALL[_ch] = _g

SMALL_H, SMALL_W = _SMALL[" "].shape
SMALL_ADVANCE = SMALL_W + 1


def small_mask(s):
    """A bool mask for a short string in the 3x5 face."""
    s = str(s).upper()
    if not s:
        return np.zeros((SMALL_H, 1), bool)
    out = np.zeros((SMALL_H, len(s) * SMALL_ADVANCE - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * SMALL_ADVANCE:i * SMALL_ADVANCE + SMALL_W] = \
            _SMALL.get(ch, _SMALL[" "])
    return out


# --------------------------------------------------------------------------
# Colour. Four greys and white. That is the entire palette and it is not an
# accident: the boing ball, the dvd logo and the crash gallery all already own
# colour on this wall, and a monochrome court next to them reads as a
# different machine rather than as the same demo with the saturation down.
# --------------------------------------------------------------------------

C_WALL = (78, 78, 82)               # the top and bottom boundary lines
C_NET = (112, 112, 118)             # the dashed centre line
C_SCORE = (146, 146, 152)           # the big numerals: present, not white
C_PLAY = (255, 255, 255)            # paddles and ball, the only pure white
C_READOUT = (62, 66, 74)            # burn-in grey along the bottom
C_FLASH = (255, 255, 255)

LEFT, RIGHT = 0, 1


def add_arguments(ap):
    ap.add_argument("--seed", type=int, default=1972,
                    help="draws the book of rallies: their lengths, speeds "
                         "and outcomes. The one source of randomness")
    ap.add_argument("--epoch", type=float, default=-1.0,
                    help="pretend the wall clock reads this unix time; "
                         "pins the whole match for tests and screenshots")
    ap.add_argument("--since", type=float, default=MATCH_EPOCH,
                    help="unix time the match started; the score counts from "
                         "here and nowhere else")
    ap.add_argument("--book", type=int, default=4096,
                    help="rallies in the repeating book. The score is exact "
                         "forever; only the *sequence* of rallies repeats, "
                         "and 4096 of them is about nine hours")
    ap.add_argument("--edge", type=float, default=0.4934,
                    help="fraction of points LEFT wins, exactly. Below 0.5 "
                         "the plodder wins the long game")
    ap.add_argument("--ball-speed", type=float, default=168.0,
                    help="horizontal speed at the serve, px/s. Horizontal "
                         "speed is the clock: it alone sets rally durations")
    ap.add_argument("--rally-gain", type=float, default=1.055,
                    help="the ball speeds up by this on every return")
    ap.add_argument("--rally-length", type=float, default=0.30,
                    help="P(rally ends) per return; smaller means longer, "
                         "grindier rallies")
    ap.add_argument("--serve-pause", type=float, default=0.95,
                    help="seconds the ball waits at the centre line")
    ap.add_argument("--celebrate", type=float, default=1.30,
                    help="seconds of held frame and flashing score on a point")
    ap.add_argument("--paddle", type=int, default=13,
                    help="paddle height in pixels")
    ap.add_argument("--no-readout", action="store_true",
                    help="drop the grey lead/day line along the bottom")


# --------------------------------------------------------------------------
# The one piece of geometry everything else is written against.
#
# fold() is ideal reflection as a triangle wave, lifted from dvd.py for the
# same reason it is there: an integrator accumulates a fraction of a pixel per
# bounce, and after a day of uptime the ball no longer meets the paddle where
# the arithmetic says it does. A fold of the distance travelled is exact at
# any t, and a dropped frame costs a frame rather than the trajectory.
# --------------------------------------------------------------------------

def fold(u, span):
    m = (u + span) % (2.0 * span)
    return np.abs(m - span)


class Character(object):
    """One paddle's incompetence, in four numbers.

    `commit` is the interesting one and belongs only to LEFT: the fraction of
    the ball's flight it spends chasing a straight-line prediction that
    ignores the walls. On a shot with no wall bounce that prediction is
    correct and the paddle looks brilliant; on a shot with one it is at the
    wrong end of the court and has 45% of the flight to get back. That is the
    whole difference between the two players and it is one number.
    """

    def __init__(self, delay, speed, commit, bias, recover):
        self.delay = delay          # seconds before it moves at all
        self.speed = speed          # px/s, its only gear
        self.commit = commit        # fraction of flight spent on a guess
        self.bias = bias            # px it habitually aims off by
        self.recover = recover      # "centre" or "anticipate", between shots


CHARACTERS = (
    Character(delay=0.10, speed=108.0, commit=0.55, bias=2.4,
              recover="anticipate"),
    Character(delay=0.42, speed=64.0, commit=0.0, bias=-1.6,
              recover="centre"),
)


def build(args):
    W, H = args.width, args.height

    # ---------------------------------------------------------------- court
    #
    # The ball's *centre* is what the maths tracks. A contact plane is the x
    # at which the ball's edge meets a paddle's face, so at a contact the ball
    # is flush against the paddle rather than one pixel inside it.
    ball = 4                                    # a square, as it should be
    bhalf = ball // 2
    pad_w = 3
    pad_h = max(5, int(args.paddle))
    pad_x = (8, W - 8 - pad_w)                  # left column of each paddle
    plane = (float(pad_x[0] + pad_w + bhalf),
             float(pad_x[1] - 1 - bhalf))
    court = plane[1] - plane[0]                 # 295 px at the defaults
    centre_x = (plane[0] + plane[1]) * 0.5
    serve_dist = centre_x - plane[0]
    # How far past a beaten paddle the ball runs before the point is over. The
    # paddles are placed symmetrically, so this is one number rather than two,
    # which is what lets the duration arithmetic below stay closed form.
    run_off = plane[0] + bhalf + 2.0

    # Vertical: rows 0 and H-1 are the boundary lines, so the ball's centre
    # lives in [1 + bhalf, H - 2 - bhalf] and reflects across that span.
    y_lo = 1.0 + bhalf
    y_hi = float(H - 2 - (ball - 1 - bhalf))
    y_span = y_hi - y_lo
    # A paddle's centre, in a span that keeps the whole paddle inside the same
    # two lines.
    p_lo = 1.0 + pad_h * 0.5
    p_hi = float(H - 1) - pad_h * 0.5
    p_mid = (p_lo + p_hi) * 0.5
    # Clear of the ball by this much and the paddle has missed it.
    clear = pad_h * 0.5 + 3.0
    covered = pad_h * 0.5 - 1.0

    # ------------------------------------------------------------- the book
    #
    # M rallies, drawn once, from --seed alone. Everything about the score is
    # two cumsums over these arrays; nothing is ever simulated forward.
    M = max(16, int(args.book))
    rs = np.random.RandomState(int(args.seed) & 0x7fffffff)

    # Returns per rally. Geometric, capped: mostly two to five, occasionally a
    # grinding fifteen. That distribution is the rhythm of the panel -- a
    # quick point, another quick point, then one that will not end.
    p_end = float(min(0.9, max(0.02, args.rally_length)))
    RMAX = 18
    returns = np.minimum(rs.geometric(p_end, size=M), RMAX).astype(np.int32)

    v0 = np.asarray(float(args.ball_speed) * rs.uniform(0.88, 1.14, size=M),
                    dtype=np.float64)
    gain = max(1.0, float(args.rally_gain))
    v_cap = float(args.ball_speed) * 2.0

    # Who loses. An exact multiset, shuffled -- not M independent coin flips.
    # A sampled edge of 0.66% over 4096 rallies has a standard error of half a
    # percent, so for some seeds the "long game" would come out the other way
    # round and the panel would quietly contradict its own documentation.
    K = int(round(M * float(np.clip(args.edge, 0.02, 0.98))))
    K = int(min(M - 1, max(1, K)))
    won_by_left = np.zeros(M, bool)
    won_by_left[:K] = True
    won_by_left = won_by_left[rs.permutation(M)]
    loser = np.where(won_by_left, RIGHT, LEFT).astype(np.int32)

    # The ball changes side on every return, so who is beaten follows from the
    # serve direction and the number of returns. We want the *loser* to be the
    # free choice, so the serve direction is what gets derived from it.
    serve_to = ((loser + returns) & 1).astype(np.int32)

    # ------------------------------------------------- rally durations
    #
    # Closed form, and the reason the whole demo works. Horizontal speed after
    # r returns is min(v0 * gain^r, cap); the ball covers half a court to the
    # first contact, a full court between contacts, and a court plus the
    # run-off after the last one. Nothing vertical appears anywhere here.
    play = np.full(M, serve_dist) / v0
    for r in range(1, RMAX + 1):
        v = np.minimum(v0 * (gain ** r), v_cap)
        mid = (returns > r)                     # a return in the middle
        last = (returns == r)                   # the shot that wins the point
        play = play + np.where(mid, court / v, 0.0)
        play = play + np.where(last, (court + run_off) / v, 0.0)

    serve_pause = max(0.0, float(args.serve_pause))
    celebrate = max(0.05, float(args.celebrate))
    duration = serve_pause + play + celebrate

    cum_dur = np.zeros(M + 1)
    np.cumsum(duration, out=cum_dur[1:])
    book_seconds = float(cum_dur[-1])

    cum_left = np.zeros(M + 1, np.int64)
    np.cumsum(won_by_left, out=cum_left[1:])
    cum_right = np.arange(M + 1, dtype=np.int64) - cum_left

    # ------------------------------------------------------------ the clock
    now = time.time() if args.epoch < 0 else float(args.epoch)
    since = float(args.since)

    # ------------------------------------------------------------- the type
    #
    # The score is measured, then the scale is chosen to fit what was
    # measured. At five digits a scale of 3 gives 87 px a side and the two
    # numbers sit either side of the net with room to spare; by the time the
    # match reaches eight digits -- some years from now -- 3 no longer fits,
    # and this steps down to 2 on its own rather than running the two numbers
    # into each other over the net.
    net_gap = 26                                # clear either side of the net
    score_y = 4

    def score_scale(ndigits):
        for s in (3, 2, 1):
            if 2 * digits_width(ndigits, s) + 2 * net_gap + 4 <= W - 12:
                return s
        return 1

    # ----------------------------------------------------- static furniture
    field = np.zeros((H, W, 3), np.uint8)
    field[0, :] = C_WALL
    field[H - 1, :] = C_WALL
    net_x = W // 2 - 1
    rows = np.arange(1, H - 1)
    lit = ((rows - 1) % 8) < 4                  # four on, four off
    field[rows[lit], net_x] = C_NET
    field[rows[lit], net_x + 1] = C_NET

    buf = np.empty((H, W, 3), np.uint8)

    def blit(dst, mask, y, x, rgb):
        mh, mw = mask.shape
        y0, x0 = max(0, y), max(0, x)
        y1, x1 = min(H, y + mh), min(W, x + mw)
        if y1 <= y0 or x1 <= x0:
            return
        sub = mask[y0 - y:y1 - y, x0 - x:x1 - x]
        dst[y0:y1, x0:x1][sub] = rgb

    comp_cache = {}

    def composite(sl, sr, days):
        """Walls, net, both scores and the grey readout, as one array.

        Rebuilt only when the score ticks -- once every eight seconds or so --
        so the per-frame cost of everything static on this panel is a single
        np.copyto. Returns the array and the two score masks with their
        positions, because the celebration flashes one of them.
        """
        key = (sl, sr, days)
        hit = comp_cache.get(key)
        if hit is not None:
            return hit

        img = field.copy()
        scale = score_scale(max(len("%d" % sl), len("%d" % sr)))
        ml, mr = digits_mask(sl, scale), digits_mask(sr, scale)
        xl = net_x - net_gap - ml.shape[1]
        xr = net_x + 2 + net_gap
        blit(img, ml, score_y, xl, C_SCORE)
        blit(img, mr, score_y, xr, C_SCORE)

        if not args.no_readout:
            lead = sl - sr
            if lead > 0:
                text = "LEFT LEADS BY %d" % lead
            elif lead < 0:
                text = "RIGHT LEADS BY %d" % (-lead)
            else:
                text = "LEVEL"
            ry = H - 1 - SMALL_H - 1
            # Inset clear of the paddle lanes: a paddle parked over the
            # readout turns it into rubble rather than covering it.
            lx = pad_x[0] + pad_w + 4
            rx = pad_x[1] - 4
            blit(img, small_mask(text), ry, lx, C_READOUT)
            rm = small_mask("DAY %d" % days)
            blit(img, rm, ry, rx - rm.shape[1], C_READOUT)

        if len(comp_cache) > 64:
            comp_cache.clear()
        entry = (img, (ml, score_y, xl), (mr, score_y, xr))
        comp_cache[key] = entry
        return entry

    # --------------------------------------------------------- the players
    #
    # Everything below is vertical, and none of it touches the clock: the
    # segment start and end times were fixed by the speed schedule above, and
    # this only decides what happens between them.

    slope_grid = np.concatenate([-np.linspace(0.06, 0.62, 48),
                                 np.linspace(0.06, 0.62, 48)])

    def reach(side, dt, pessimistic=True):
        """How far a paddle can move in dt, allowing for its worst habit."""
        c = CHARACTERS[side]
        usable = max(0.0, dt - c.delay)
        if pessimistic and c.commit > 0.0:
            usable *= (1.0 - c.commit)          # assume the guess was wrong
        return c.speed * usable

    def track(side, t0, t1, y_start, target, guess=None, commit=0.0,
              delay=None):
        """Keypoints for one paddle over one segment: wait, then move, then stop.

        A piecewise-linear path rather than a formula, because the lunger's
        change of mind halfway through is a second piece and np.interp over a
        few dozen keypoints is one numpy call. It also makes the guarantees
        below checkable rather than hoped for: the position this paddle ends
        the segment at is a number the function returns.
        """
        c = CHARACTERS[side]
        wait = c.delay if delay is None else max(0.0, float(delay))
        if guess is not None and commit > 0.0:
            tc = t0 + (t1 - t0) * commit
            phases = ((t0, tc, guess, wait), (tc, t1, target, 0.0))
        else:
            phases = ((t0, t1, target, wait),)

        pts = []
        y = float(y_start)
        for (a, b, tgt, w) in phases:
            tgt = min(p_hi, max(p_lo, float(tgt)))
            go = a + w
            if go >= b:
                pts.append((b, y))
                continue
            pts.append((go, y))
            dist = tgt - y
            if abs(dist) < 1e-9:
                pts.append((b, y))
                continue
            travel = abs(dist) / c.speed
            if go + travel <= b:
                pts.append((go + travel, tgt))
                pts.append((b, tgt))
                y = tgt
            else:
                y = y + dist * ((b - go) / travel)
                pts.append((b, y))
        return pts, y

    def receive(side, t0, t1, y_start, arrive, flip, guess):
        """A paddle going for a ball it is *meant* to return.

        The lunger's guess is a straight-line prediction with the walls left
        out, which is why it is at the wrong end of the court on any shot that
        bounces. If it turns out it could not recover in time, the commitment
        is halved and it is tried again -- because a rally that ends here is a
        point the book did not award, and the score would then disagree with
        the picture.
        """
        c = CHARACTERS[side]
        target = arrive + c.bias * (1.0 if flip else -1.0)
        commit = c.commit
        for _ in range(6):
            pts, endy = track(side, t0, t1, y_start, target, guess, commit)
            if abs(endy - arrive) <= covered:
                return pts, endy, True
            commit *= 0.5
            if commit < 0.05:
                commit = 0.0
        # It genuinely could not get there. Let it stretch the last pixel or
        # two rather than concede a point nobody scored -- clamped into the
        # paddle's own range, which still covers the ball, because a ball
        # outside that range is within half a paddle of the end of the court
        # by construction. The test script counts how often this fires; at the
        # defaults it is eight times in thirteen thousand contacts.
        snap = min(p_hi, max(p_lo, arrive))
        pts[-1] = (pts[-1][0], snap)
        return pts, snap, False

    def beaten(side, t0, t1, y_start, arrive):
        """A paddle that has to miss, made to miss the way it would.

        Which player is beaten was decided by the book, so the geometry has to
        be talked into agreeing. Both characters do it by being late, which is
        the failure both of them actually have: the reaction is stretched
        until the paddle runs out of segment a clear paddle-width short of the
        ball, and it is still gliding when the ball goes past it. For the
        lunger there is a better-looking option first -- it reads the bounce
        backwards, lunges to the mirror image of the ball, and cannot get
        back -- and it is used whenever it produces a genuine miss.
        """
        c = CHARACTERS[side]
        if c.commit > 0.0:
            mirror = y_lo + y_hi - arrive       # the bounce read backwards
            pts, endy = track(side, t0, t1, y_start, arrive, mirror, 0.72)
            if abs(endy - arrive) > clear:
                return pts, endy
        need = abs(arrive - y_start)
        allow = max(0.0, need - clear)
        late = (t1 - t0) - allow / c.speed
        pts, endy = track(side, t0, t1, y_start, arrive,
                          delay=max(c.delay, late))
        return pts, endy

    rally_cache = {}

    def make_rally(j):
        """The vertical story of book slot j. A pure function of (seed, j)."""
        hit = rally_cache.get(j)
        if hit is not None:
            return hit

        R = int(returns[j])
        s0 = int(serve_to[j])
        v_start = float(v0[j])
        prs = np.random.RandomState((int(args.seed) * 2654435761 + j * 40503)
                                    & 0x7fffffff)

        # Segment 0 is the serve, segments 1..R-1 are returns, and segment R
        # is the shot that beats somebody. The receiver of segment k is
        # (s0 + k) & 1, which is why the loser fixes the serve direction.
        t = serve_pause
        segs = []
        keys = [[(0.0, p_mid)], [(0.0, p_mid)]]
        pad_now = [p_mid, p_mid]
        stretched = 0

        y = float(prs.uniform(y_lo + 6.0, y_hi - 6.0))
        serve_y = y
        slope = float(prs.uniform(0.12, 0.34) * (1 if prs.rand() < 0.5 else -1))
        x_from = centre_x

        for k in range(R + 1):
            recv = (s0 + k) & 1
            hitter = 1 - recv
            v = min(v_start * (gain ** k), v_cap)
            dx_hit = abs(plane[recv] - x_from)
            dx = dx_hit + (run_off if k == R else 0.0)
            dt = dx / v
            direction = -1.0 if recv == LEFT else 1.0
            x_to = x_from + dx * direction
            t0, t1 = t, t + dt

            # Where the ball meets the paddle plane, reflected off the walls.
            arrive = float(y_lo + fold(y - y_lo + slope * dx_hit, y_span))
            segs.append((t0, t1, x_from, x_to, y, slope))

            if k == R:
                pts, endy = beaten(recv, t0, t1, pad_now[recv], arrive)
            else:
                guess = (y + slope * dx_hit
                         if CHARACTERS[recv].commit > 0.0 else None)
                pts, endy, ok = receive(recv, t0, t1, pad_now[recv], arrive,
                                        bool((j + k) & 1), guess)
                if not ok:
                    stretched += 1
            keys[recv].extend(pts)
            pad_now[recv] = endy

            # The other paddle, between shots. The plodder slides back to the
            # middle and waits; the lunger pre-positions where it guesses the
            # ball will be returned to, which is exactly the kind of confident
            # wrong answer that makes it fun to watch.
            rest = p_mid if CHARACTERS[hitter].recover == "centre" else arrive
            pts2, endy2 = track(hitter, t0, t1, pad_now[hitter], rest)
            keys[hitter].extend(pts2)
            pad_now[hitter] = endy2

            t = t1
            if k == R:
                break

            # --- the return, and the angle it leaves at ---
            #
            # Where on the paddle it hit sets the angle it leaves at, exactly
            # as the original does: a hit on the tip leaves steeply, a hit on
            # the middle leaves flat. That is one line and it is most of what
            # makes the play legible.
            #
            # The angle is then bent as little as possible to keep the rally
            # honest. The next receiver must be able to reach where this shot
            # lands, or the rally would end on a shot the book never awarded;
            # and if the *next* segment is the deciding one, the opposite is
            # wanted -- the winning shot is a placement, put as far from the
            # beaten paddle as the hit offset can be argued into allowing.
            off = float(np.clip((arrive - endy) / (pad_h * 0.5), -1.0, 1.0))
            if abs(off) < 0.08:
                want = 0.10 * (1.0 if prs.rand() < 0.5 else -1.0)
            else:
                want = (0.10 + 0.42 * abs(off)) * (1.0 if off > 0 else -1.0)

            nxt = (s0 + k + 1) & 1
            v_next = min(v_start * (gain ** (k + 1)), v_cap)
            dt_next = court / v_next
            land = y_lo + fold(arrive - y_lo + slope_grid * court, y_span)
            gapped = np.abs(land - pad_now[nxt])
            if k + 1 == R:
                cost = np.abs(slope_grid - want) - gapped * 0.05
            else:
                room = max(covered, reach(nxt, dt_next) - 4.0)
                cost = np.abs(slope_grid - want) + np.where(gapped <= room,
                                                            0.0, 100.0)
            slope = float(slope_grid[int(np.argmin(cost))])

            y = arrive
            x_from = plane[recv]

        # A short glide back to the middle while the score flashes.
        for side in (0, 1):
            keys[side].append((t, pad_now[side]))
            keys[side].append((t + celebrate * 0.7, p_mid))
            keys[side].append((t + celebrate + 2.0, p_mid))

        record = {
            "returns": R, "serve_to": s0, "segs": segs, "play_end": t,
            "keys": [np.array(kk, float) for kk in keys],
            "loser": int(loser[j]), "serve_y": serve_y,
            "stretched": stretched,
        }
        if len(rally_cache) > 8:
            rally_cache.clear()
        rally_cache[j] = record
        return record

    # ----------------------------------------------------------- the score
    def state_at(u):
        """(rally, slot, seconds into it, left score, right score).

        The closed form. Two integer divisions and a searchsorted, over a
        table of length M; no history of any kind, which is why a power cut
        costs this panel nothing.
        """
        if u < 0.0:
            u = 0.0
        block = int(u // book_seconds)
        rem = u - block * book_seconds
        j = int(np.searchsorted(cum_dur, rem, side="right")) - 1
        j = min(M - 1, max(0, j))
        tau = rem - cum_dur[j]
        return (block * M + j, j, tau,
                block * K + int(cum_left[j]),
                block * (M - K) + int(cum_right[j]))

    def render(t, frame):
        u = (now + t) - since
        _, j, tau, sl, sr = state_at(u)
        r = make_rally(j)

        scored = tau >= r["play_end"]
        if scored:
            if r["loser"] == RIGHT:
                sl += 1
            else:
                sr += 1

        # The day count comes off the render clock, not off build(): a demo
        # rebuilt at 23:59 and still running at 00:01 would otherwise keep
        # yesterday's number, and two builds an hour apart would draw
        # different panels for the same instant.
        img, left_box, right_box = composite(sl, sr, int(u // 86400.0))
        np.copyto(buf, img)

        # The paddles: one np.interp over a few dozen keypoints each.
        for side in (0, 1):
            kk = r["keys"][side]
            cy = float(np.interp(tau, kk[:, 0], kk[:, 1]))
            top = min(H - 1 - pad_h, max(1, int(round(cy - pad_h * 0.5))))
            buf[top:top + pad_h, pad_x[side]:pad_x[side] + pad_w] = C_PLAY

        # The ball.
        bx = by = None
        if tau < serve_pause:
            # Waiting on the centre line, blinking, the way a machine that has
            # just conceded waits before it serves again.
            if (tau % 0.42) < 0.26:
                bx, by = centre_x, r["serve_y"]
        elif not scored:
            for (t0, t1, x0, x1, y0, slope) in r["segs"]:
                if t0 <= tau <= t1:
                    bx = x0 + (x1 - x0) * ((tau - t0) / max(t1 - t0, 1e-9))
                    by = y_lo + float(fold(y0 - y_lo + slope * abs(bx - x0),
                                           y_span))
                    break

        if bx is not None:
            ix, iy = int(round(bx)), int(round(by))
            x0 = max(0, ix - bhalf)
            x1 = min(W, ix + bhalf + 1)
            if x1 > x0:
                y0 = min(H - 1 - ball, max(1, iy - bhalf))
                buf[y0:y0 + ball, x0:x1] = C_PLAY

        # The point. The beaten end of the court lights up and the score that
        # changed flashes -- three times, hard, then it is over. Nothing
        # expands and nothing fades away; this machine does not know how.
        if scored:
            k = (tau - r["play_end"]) / celebrate
            if k < 0.75 and (int(k * 6.0) & 1) == 0:
                mask, my, mx = left_box if r["loser"] == RIGHT else right_box
                blit(buf, mask, my, mx, C_FLASH)
                if r["loser"] == LEFT:
                    buf[1:H - 1, 0:2] = C_FLASH
                else:
                    buf[1:H - 1, W - 2:W] = C_FLASH

        return buf

    render.match = dict(
        book=M, book_seconds=book_seconds, left_per_book=K,
        right_per_book=M - K, mean_rally=float(duration.mean()),
        duration=duration, court=court, planes=plane, run_off=run_off,
        epoch=now, since=since, cum_dur=cum_dur,
        cum_left=cum_left, cum_right=cum_right, returns=returns, loser=loser,
        state_at=state_at, make_rally=make_rally, composite=composite,
        score_y=score_y, score_scale=score_scale, net_gap=net_gap,
        net_x=net_x, pad_h=pad_h, pad_w=pad_w, pad_x=pad_x, ball=ball,
        y_lo=y_lo, y_hi=y_hi, p_lo=p_lo, p_hi=p_hi, covered=covered,
        clear=clear, serve_pause=serve_pause, celebrate=celebrate,
    )
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
