#!/usr/bin/env python3
"""A marble run, seen from the side, with the mechanisms that make one worth
watching: a steep ramp, a loop-the-loop, a long shallow one it coasts down, a ski
jump, a Newton's cradle, a see-saw, a funnel it spirals down, and an
Archimedes screw that lifts it back to the top.

The one representation
----------------------
The whole run is **solved once in build()** into a single table of samples
`(t, x, y, z, spin)` covering exactly one lap, and `render(t)` is a bisect into
that table plus a lerp. Nothing is integrated per frame, nothing accumulates,
and the panel is a pure function of `t` by construction -- which the wall needs,
because the scheduler builds a segment on a worker thread and starts it at t=0
while the preview baker steps it at its own rate.

The table is built by walking the track geometry part by part, carrying the
marble's speed forward with real kinematics:

    v^2 <- v^2 + 2 g dh - k v^2 ds

The first term is `v^2 = 2gh` written incrementally, so the marble genuinely
accelerates down the steep ramp and genuinely decelerates climbing to the
see-saw's pivot. The second is rolling resistance, which is what makes the
shallow ramp read as *shallow*: its slope is below `k v^2 / 2g`, so the marble
loses more to the rail than it gains from the drop and coasts down rather than
building. Turning that up far enough to make the coast dramatic is not free --
at k = 0.0034 the marble no longer makes it round the loop -- so 0.0022 is
where it sits, and the speed contrast the panel actually shows is between the
16 px/s crawl off the screw, the 87 px/s at the ski jump and the 24 px/s lift.
Drag is also why the run needs the screw at all: the marble does not come back
on its own. Time is then `dt = ds / v`, accumulated, and a
part that is not gravity-driven (only the screw) sets its speed directly.

Because the speed comes out of the geometry rather than out of a tween, the
loop-the-loop is a real constraint rather than a drawing: build() checks
`v_top^2 >= g r` at the top of the loop and prints the margin, and the test
script asserts it. If the first ramp were made shallower the marble would fall
off the loop, which is exactly the failure a real marble run has.

Everything moves because the marble moved it
--------------------------------------------
The see-saw's tilt is not a timer. Its height profile is `y(u) = ycp - u s(u)`
where `u` is the marble's signed distance from the pivot and `s` flips sign as
`u` crosses zero -- so the tip is *inside the potential*, the marble climbs the
entry half, tips the lever with its own weight, and the far end dropping is what
gives it the speed to leave. The Newton's cradle is a queue of four steel balls
resting against a stop: the marble rolls in, stops dead, and the ball at the far
end leaves at the speed the marble arrived with, after which the queue nudges
along one place. The count is conserved -- the arriving marble *is* the new
fourth ball -- which is the only version of that beat that survives being
watched twice.

The screw is the exception and is meant to be: it is the motor, and it is the
reset. Its rotation is geared to the marble's rise (one turn, one pitch), and
build() picks the ride time so that the screw's own period divides the lap
exactly, and divides it by the number of marbles as well -- otherwise the second
marble would ride up between the threads.

Cost
----
Every moving part is **pre-rendered in build() as a small stack of patches
indexed by phase**: sixteen screw phases, thirteen see-saw tilts, twelve cradle
settle states. A frame is then one background copy, three patch copies, and one
`np.maximum` per marble sprite -- about twenty numpy calls regardless of what is
happening. The marble sprites are baked per (colour, brightness, rotation,
subpixel) so that positioning is smooth and the blit is a single call, and the
path lookup is done in plain Python floats off `bisect`, which costs less than a
numpy call would.

Run:  python3 marble.py --host 127.0.0.1
      python3 marble.py --marbles 1          # follow one all the way round
      python3 marble.py --gravity 130        # slow, calm; 260 is frantic
"""

import bisect
import math
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * math.pi

# --------------------------------------------------------------------------
# The track, in panel pixels. y is down, as everywhere else here.
#
# This is a fixed, hand-designed layout and the seed only chooses which marble
# colours turn up and in what order. A randomly generated track loses on a
# 320x64 panel every time: the whole point is that the mechanisms have to be
# spaced so each one is legible on its own, and the vertical budget is 64 rows
# with a 12 row loop and a 10 row funnel in it. There is no slack for a
# generator to find. The layout below was drawn on paper first.
# --------------------------------------------------------------------------

W, H = 320, 64

R_MARBLE = 2.6                  # marble radius, px -- the rail is offset by it

SCREW_X = 14.0                  # screw axis
SCREW_R = 6.0                   # helix radius
SCREW_TOP = 6.5                 # release height
SCREW_BOT = 58.8                # catch height
SCREW_TURNS = 8                 # threads traversed on the way up

START = (SCREW_X + SCREW_R, SCREW_TOP)          # (20.0, 6.5)

RAMP1_END = (78.0, 24.0)        # steep: slope 0.30, this is what feeds the loop
LOOP_C = (88.0, 18.0)           # loop centre; bottom at y = 24
LOOP_R = 6.0

SHALLOW_A = (96.0, 24.3)        # the dawdle: 82 px at slope 0.016, which
SHALLOW_B = (178.0, 25.6)       # is below what the rolling resistance takes
CHUTE_A = (184.0, 26.4)         # 26 px at slope 0.24 -- earns the jump
CHUTE_B = (210.0, 32.6)
KICKER = (218.0, 32.9)          # flat lip; the marble leaves here

LAND_AIM = (264.0, 40.0)        # left rim of the funnel
LAND_SLOPE = 0.10               # the landing rail runs back-left from LAND_AIM

FUN_CX = 288.0                  # funnel axis
FUN_RIM_R = 24.0
FUN_RIM_Y = 40.0
FUN_HOLE_R = 4.0
FUN_HOLE_Y = 49.0
FUN_TURNS = 3.0
ELL = 0.25                      # how much of the depth axis shows as screen y

DECK2_Y = 50.6                  # rail height under the funnel's hole
DECK2_B = (250.0, 53.6)

SEE_CX, SEE_CY = 232.0, 51.2    # see-saw pivot
SEE_L = 18.0                    # half length
SEE_RISE = 3.4                  # end rise/fall, px
SEE_EPS = 3.0                   # how far past the pivot the lever takes to tip

DECK3_B = (124.0, 56.2)         # rail from the see-saw exit to the cradle
BALL_R = 3.0                    # cradle ball radius
BALL_D = 6.0                    # cradle ball spacing
CRADLE_N = 4
CRADLE_STOP = 86.0              # x of the leftmost (ejected) ball
CRADLE_HIT = CRADLE_STOP + (CRADLE_N - 1) * BALL_D + BALL_R + R_MARBLE
CRADLE_SLOPE = 0.0278           # the groove falls this much toward the stop
CRADLE_Y = 56.6                 # rail height at CRADLE_HIT
DECK4_B = (SCREW_X + SCREW_R, SCREW_BOT)        # into the screw's catch

# --------------------------------------------------------------------------
# Colour. Thin bright detail on a dark ground survives being seen at an angle
# from three metres; subtle mid-tone contrast does not, so the rail is a light
# steel line and everything structural behind it is nearly black.
# --------------------------------------------------------------------------

INK_BG = (5, 6, 10)
INK_FRAME = (26, 29, 38)
INK_POST = (46, 51, 64)
INK_RAIL = (124, 136, 158)
INK_RAIL_HI = (186, 202, 228)
INK_STEEL = (150, 158, 174)
INK_BRASS = (168, 130, 58)
INK_SHAFT = (74, 62, 34)
INK_FUNNEL = (86, 98, 122)
INK_FUNNEL_BACK = (44, 52, 68)

MARBLE_COLOURS = [
    (70, 225, 255),     # cyan
    (255, 170, 50),     # amber
    (255, 95, 180),     # magenta
    (120, 245, 155),    # jade
    (175, 152, 255),    # violet
]

# Sprite bank shape. Ten rotations is enough for the roll to read at 2.6 px;
# three subpixel steps kill the jitter a fast marble would otherwise have; the
# brightness levels do double duty as funnel depth cueing and as motion-trail
# ghosts, which is why there are five of them.
N_ROT, N_SUB, SPR_R = 10, 3, 4
BRIGHT = (1.00, 0.72, 0.50, 0.30, 0.16)
DEPTH_LEVEL = (0, 1, 2)         # front, middle, back
GHOST_LEVEL = (3, 4)            # the two trail samples
GHOST_DT = (0.022, 0.045)


# --------------------------------------------------------------------------
# Drawing helpers. All of these run in build() only, so they are written for
# clarity rather than for speed -- but they still work over a bounding tile
# rather than the whole panel, because the track is stamped a couple of
# thousand times and a whole-panel pass per stamp would make build() slow
# enough for the scheduler to notice.
#
# Everything composites with maximum() into a float canvas. Ink is additive in
# spirit but saturating in practice: two rails crossing should read as one
# bright rail, not as a hot spot.
# --------------------------------------------------------------------------

def _tile(shape, cx, cy, rad):
    """Integer bounding box around a stamp, clipped to the canvas."""
    x0 = max(0, int(math.floor(cx - rad)))
    x1 = min(shape[1], int(math.ceil(cx + rad)) + 1)
    y0 = max(0, int(math.floor(cy - rad)))
    y1 = min(shape[0], int(math.ceil(cy + rad)) + 1)
    return x0, x1, y0, y1


def _dot(img, cx, cy, r, colour, gain=1.0):
    """An antialiased disc. r is the radius at which coverage reaches zero."""
    x0, x1, y0, y1 = _tile(img.shape, cx, cy, r + 1.0)
    if x1 <= x0 or y1 <= y0:
        return
    xs = np.arange(x0, x1, dtype=f32) - cx
    ys = np.arange(y0, y1, dtype=f32) - cy
    d = np.sqrt(xs[None, :] ** 2 + ys[:, None] ** 2)
    a = np.clip(r + 0.5 - d, 0.0, 1.0) * gain
    np.maximum(img[y0:y1, x0:x1], a[:, :, None] * np.array(colour, f32),
               out=img[y0:y1, x0:x1])


def _seg(img, p0, p1, colour, r=0.6, gain=1.0):
    """An antialiased line segment, round ends. Distance field over the box."""
    (ax, ay), (bx, by) = p0, p1
    ex, ey = bx - ax, by - ay
    ll = ex * ex + ey * ey
    cx, cy = 0.5 * (ax + bx), 0.5 * (ay + by)
    rad = 0.5 * math.sqrt(ll) + r + 1.0
    x0, x1, y0, y1 = _tile(img.shape, cx, cy, rad)
    if x1 <= x0 or y1 <= y0:
        return
    px = np.arange(x0, x1, dtype=f32)[None, :] - ax
    py = np.arange(y0, y1, dtype=f32)[:, None] - ay
    if ll < 1e-9:
        d = np.sqrt(px ** 2 + py ** 2)
    else:
        u = np.clip((px * ex + py * ey) / ll, 0.0, 1.0)
        d = np.sqrt((px - u * ex) ** 2 + (py - u * ey) ** 2)
    a = np.clip(r + 0.5 - d, 0.0, 1.0) * gain
    np.maximum(img[y0:y1, x0:x1], a[:, :, None] * np.array(colour, f32),
               out=img[y0:y1, x0:x1])


def _poly(img, pts, colour, r=0.6, gain=1.0):
    for i in range(len(pts) - 1):
        _seg(img, pts[i], pts[i + 1], colour, r, gain)


def _ball(img, cx, cy, r, colour, hi=(255, 255, 255)):
    """A little shaded sphere: lambert-ish body plus a specular pip.

    Used for the cradle's steel balls. The marbles themselves get the same
    treatment in the sprite bank, at more trouble, because they also roll.
    """
    x0, x1, y0, y1 = _tile(img.shape, cx, cy, r + 1.5)
    if x1 <= x0 or y1 <= y0:
        return
    dx = (np.arange(x0, x1, dtype=f32) - cx)[None, :]
    dy = (np.arange(y0, y1, dtype=f32) - cy)[:, None]
    d = np.sqrt(dx ** 2 + dy ** 2)
    a = np.clip(r + 0.5 - d, 0.0, 1.0)
    nz = np.sqrt(np.clip(1.0 - (d / r) ** 2, 0.0, 1.0))
    lam = np.clip((-dx / r) * 0.50 + (-dy / r) * 0.60 + nz * 0.62, 0.0, 1.0)
    body = a * (0.22 + 0.90 * lam * lam)
    spec = a * np.exp(-(((dx + 0.85 * r) ** 2 + (dy + 0.85 * r) ** 2)) / 0.7)
    rgb = (body[:, :, None] * np.array(colour, f32)
           + spec[:, :, None] * np.array(hi, f32))
    np.maximum(img[y0:y1, x0:x1], rgb, out=img[y0:y1, x0:x1])


def _hermite(p0, d0, p1, d1, n):
    """Cubic Hermite joint between two track parts, in (x, y).

    Rails join tangentially or the marble kinks; the loop's entry in particular
    has to leave a 0.30 slope and arrive horizontal, and a corner there would
    read as a mistake even at this size.
    """
    s = np.linspace(0.0, 1.0, n, dtype=np.float64)
    h00 = 2 * s ** 3 - 3 * s ** 2 + 1
    h10 = s ** 3 - 2 * s ** 2 + s
    h01 = -2 * s ** 3 + 3 * s ** 2
    h11 = s ** 3 - s ** 2
    x = h00 * p0[0] + h10 * d0[0] + h01 * p1[0] + h11 * d1[0]
    y = h00 * p0[1] + h10 * d0[1] + h01 * p1[1] + h11 * d1[1]
    return x, y


# --------------------------------------------------------------------------
# The solver.
#
# A _Run is a growing table of samples with the marble's speed carried along.
# Each track part hands it a finely sampled polyline in *world* coordinates
# (x across, z into the panel, h down) and says whether the marble is rolling
# on a rail there. The run turns that into times.
#
# Keeping h (physical height) separate from the screen y matters exactly twice,
# in the funnel and on the screw: both are helices around a vertical axis, so a
# point's height depends only on its radius and the depth axis shows up on
# screen as ELL * z. Physics uses h; drawing uses h + ELL*z. Everywhere else
# z is zero and the two are the same thing.
# --------------------------------------------------------------------------

MARK_RAMP1, MARK_LOOP, MARK_SHALLOW, MARK_JUMP = 0, 1, 2, 3
MARK_FUNNEL, MARK_SEESAW, MARK_CRADLE, MARK_SCREW = 4, 5, 6, 7


class _Run(object):

    def __init__(self, g, drag, v0):
        self.g = float(g)
        self.drag = float(drag)
        self.v2 = float(v0) ** 2
        self.vmin2 = 8.0 ** 2            # keeps dt = ds/v finite on a flat
        self.X, self.Z, self.Hh, self.T = [], [], [], []
        self.SP, self.RX, self.RY = [], [], []
        self.t = 0.0
        self.spin = 0.0
        self.marks = {}                  # part id -> (t_enter, t_leave)
        self.vlog = {}                   # part id -> speed on entry

    # -- geometry in, times out -------------------------------------------

    def extend(self, xs, hs, zs=None, rolling=True, rail=True,
               nx=None, ny=None, speed=None, mark=None):
        """Append a polyline. xs[0] must be where the run currently is."""
        n = len(xs)
        zs = [0.0] * n if zs is None else zs
        t0 = self.t
        if not self.X:                              # very first sample
            self._push(xs[0], hs[0], zs[0], rail, nx, ny, 0, 0.0)
        for i in range(1, n):
            dx = xs[i] - xs[i - 1]
            dz = zs[i] - zs[i - 1]
            dh = hs[i] - hs[i - 1]
            dsl = math.sqrt(dx * dx + dz * dz + dh * dh)
            if dsl < 1e-9:
                continue
            if speed is None:
                # v^2 = 2gh, incrementally, less rolling resistance.
                self.v2 += 2.0 * self.g * dh
                if rolling:
                    self.v2 -= self.drag * self.v2 * dsl
                if self.v2 < self.vmin2:
                    self.v2 = self.vmin2
                v = math.sqrt(self.v2)
            else:
                v = speed
                self.v2 = v * v
            self.t += dsl / v
            self.spin += dsl / R_MARBLE * (1.0 if dx >= 0.0 else -1.0)
            self._push(xs[i], hs[i], zs[i], rail, nx, ny, i, dsl)
        if mark is not None:
            self.marks[mark] = (t0, self.t)
        return math.sqrt(self.v2)

    def _push(self, x, h, z, rail, nx, ny, i, dsl):
        self.X.append(x)
        self.Hh.append(h)
        self.Z.append(z)
        self.T.append(self.t)
        self.SP.append(self.spin)
        if rail:
            # The rail is offset a marble radius along the track normal, so the
            # ball rides on top of it rather than through it. The loop passes
            # its own outward normal in; everywhere else "down" is right.
            if nx is None:
                if len(self.X) >= 2:
                    ddx = self.X[-1] - self.X[-2]
                    ddy = (self.Hh[-1] + ELL * self.Z[-1]) - (
                        self.Hh[-2] + ELL * self.Z[-2])
                else:
                    ddx, ddy = 1.0, 0.0
                ln = math.hypot(ddx, ddy) or 1.0
                ox, oy = -ddy / ln, ddx / ln
                if oy < 0:
                    ox, oy = -ox, -oy
            else:
                ox, oy = nx[i], ny[i]
            self.RX.append(x + ox * R_MARBLE)
            self.RY.append(h + ELL * z + oy * R_MARBLE)
        else:
            self.RX.append(None)
            self.RY.append(None)

    # -- the two parts that are not a polyline ----------------------------

    def ballistic(self, until_h, dt=0.004, mark=None):
        """Free flight: no rail, no rolling resistance, gravity only.

        The launch velocity is whatever the marble actually had at the lip, so
        where it lands falls out of the run rather than being placed. That is
        the point of the ski jump: it is the one part of the track whose
        geometry is a *consequence* of the speed earned upstream.
        """
        x, h = self.X[-1], self.Hh[-1]
        # Direction from the last two samples; magnitude from the energy.
        dx = self.X[-1] - self.X[-2]
        dh = self.Hh[-1] - self.Hh[-2]
        ln = math.hypot(dx, dh) or 1.0
        v = math.sqrt(self.v2)
        vx, vh = v * dx / ln, v * dh / ln
        xs, hs = [x], [h]
        for _ in range(4000):
            vh += self.g * dt
            x += vx * dt
            h += vh * dt
            xs.append(x)
            hs.append(h)
            if h >= until_h(x):
                break
        return self.extend(xs, hs, rolling=False, rail=False, mark=mark)

    def hop(self, x, h):
        """A discontinuity in position at constant speed: the cradle's
        transfer, where the arriving marble stops dead and a different ball
        leaves the far end carrying the momentum."""
        self.t += 1e-4
        self._push(x, h, 0.0, False, None, None, 0, 0.0)


def _lin(p0, p1, step=0.5):
    """A straight part, sampled at roughly `step` pixels."""
    n = max(2, int(math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / step) + 1)
    s = np.linspace(0.0, 1.0, n)
    return (p0[0] + (p1[0] - p0[0]) * s), (p0[1] + (p1[1] - p0[1]) * s)


def _dirn(p0, p1, scale):
    """A Hermite tangent of the given length, pointing p0 -> p1."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    ln = math.hypot(dx, dy) or 1.0
    return (dx / ln * scale, dy / ln * scale)


def _see_slope(u):
    """The lever's slope with the marble at signed distance u from the pivot.

    Positive u is past the pivot. The lever rests entry-end-down against a
    stop; once the marble is a few pixels beyond the pivot its weight has
    carried the far end over, and the sign flips. Because this is a function
    of u and not of t, the drop is part of the marble's potential: the lever
    tipping is what pays for the speed it leaves with.
    """
    s0 = SEE_RISE / SEE_L
    k = min(1.0, max(0.0, u / SEE_EPS))
    k = k * k * (3.0 - 2.0 * k)                     # smoothstep, so no jerk
    return s0 * (1.0 - 2.0 * k)


def _see_h(u):
    return SEE_CY - u * _see_slope(u)


def _solve(g, drag):
    """Walk the whole gravity-driven track once and return the run."""
    run = _Run(g, drag, 14.0)

    # 1. the steep ramp -- everything downstream is paid for here
    xs, ys = _lin(START, RAMP1_END, 0.4)
    run.extend(list(xs), list(ys), mark=MARK_RAMP1)

    # ...curving into the bottom of the loop, tangentially
    loop_bot = (LOOP_C[0], LOOP_C[1] + LOOP_R)
    d0 = _dirn(START, RAMP1_END, 14.0)
    hx, hy = _hermite(RAMP1_END, d0, loop_bot, (14.0, 0.0), 34)
    run.extend(list(hx[1:]), list(hy[1:]))

    # 2. the loop. Position and outward normal are the same unit vector, which
    #    is what puts the rail on the outside of the loop rather than under the
    #    marble -- at the top the track is above the ball, holding it in.
    a = np.linspace(0.0, TAU, 150)
    lx = LOOP_C[0] + LOOP_R * np.sin(a)
    ly = LOOP_C[1] + LOOP_R * np.cos(a)
    run.extend(list(lx), list(ly), nx=list(np.sin(a)), ny=list(np.cos(a)),
               mark=MARK_LOOP)

    # 3. out of the loop and onto the long shallow ramp: the dawdle
    hx, hy = _hermite(loop_bot, (12.0, 0.0), SHALLOW_A,
                      _dirn(SHALLOW_A, SHALLOW_B, 12.0), 26)
    run.extend(list(hx[1:]), list(hy[1:]))
    xs, ys = _lin(SHALLOW_A, SHALLOW_B, 0.5)
    run.extend(list(xs[1:]), list(ys[1:]), mark=MARK_SHALLOW)

    # 4. a short steep chute, then a flat kicker lip
    hx, hy = _hermite(SHALLOW_B, _dirn(SHALLOW_A, SHALLOW_B, 10.0),
                      CHUTE_A, _dirn(CHUTE_A, CHUTE_B, 10.0), 20)
    run.extend(list(hx[1:]), list(hy[1:]))
    xs, ys = _lin(CHUTE_A, CHUTE_B, 0.5)
    run.extend(list(xs[1:]), list(ys[1:]))
    hx, hy = _hermite(CHUTE_B, _dirn(CHUTE_A, CHUTE_B, 9.0),
                      KICKER, (9.0, 0.3), 18)
    run.extend(list(hx[1:]), list(hy[1:]))

    # 5. the jump. Where it lands is decided by the speed, not by me.
    land_line = lambda x: LAND_AIM[1] - LAND_SLOPE * (LAND_AIM[0] - x)
    run.ballistic(land_line, mark=MARK_JUMP)
    land = (run.X[-1], run.Hh[-1])          # used to place the landing rail
    # Landing kills the speed normal to the rail, as a real one does.
    tx, ty = 1.0, LAND_SLOPE
    tl = math.hypot(tx, ty)
    dx = run.X[-1] - run.X[-2]
    dh = run.Hh[-1] - run.Hh[-2]
    dl = math.hypot(dx, dh) or 1.0
    tang = abs((dx * tx + dh * ty) / (dl * tl))
    run.v2 *= tang * tang
    xs, ys = _lin(land, LAND_AIM, 0.5)
    run.extend(list(xs[1:]), list(ys[1:]))

    # 6. the funnel. A cone: height depends only on radius, and the angle
    #    around it shows up on screen as ELL*z. Entering at the left rim
    #    heading right sends the marble around the back first, which is the
    #    dim half, so the first thing it does is disappear behind the rim.
    n = int(FUN_TURNS * 220)
    ph = np.linspace(math.pi, math.pi + TAU * FUN_TURNS, n)
    u = np.linspace(0.0, 1.0, n)
    rr = FUN_RIM_R + (FUN_HOLE_R - FUN_RIM_R) * u
    fx = FUN_CX + rr * np.cos(ph)
    fz = rr * np.sin(ph)
    fh = FUN_RIM_Y + (FUN_HOLE_Y - FUN_RIM_Y) * u
    run.extend(list(fx), list(fh), list(fz), rail=False, mark=MARK_FUNNEL)

    # 7. out of the hole, down onto the lower deck, heading back left
    run.v2 *= 0.55                       # the hole is a squeeze, not a chute
    run.ballistic(lambda x: DECK2_Y)
    run.v2 *= 0.5
    xs, ys = _lin((run.X[-1], run.Hh[-1]), DECK2_B, 0.5)
    run.extend(list(xs[1:]), list(ys[1:]))

    # 8. the see-saw
    us = np.linspace(-SEE_L, SEE_L, 90)
    sx = SEE_CX - us
    sh = np.array([_see_h(float(v)) for v in us])
    run.extend(list(sx[1:]), list(sh[1:]), mark=MARK_SEESAW)

    # 9. down to the cradle, stop dead, and a different ball leaves the far end
    xs, ys = _lin((SEE_CX - SEE_L, float(sh[-1])), DECK3_B, 0.5)
    run.extend(list(xs[1:]), list(ys[1:]))
    xs, ys = _lin(DECK3_B, (CRADLE_HIT, CRADLE_Y), 0.4)
    t_hit = run.t
    run.extend(list(xs[1:]), list(ys[1:]), mark=MARK_CRADLE)
    run.hop(CRADLE_STOP, CRADLE_Y + CRADLE_SLOPE * (CRADLE_HIT - CRADLE_STOP))
    run.marks[MARK_CRADLE] = (t_hit, run.t)

    # 10. the last rail, into the screw's catch
    xs, ys = _lin((run.X[-1], run.Hh[-1]), DECK4_B, 0.5)
    run.extend(list(xs[1:]), list(ys[1:]))
    return run


def _add_screw(run, ride):
    """The lift, and the only thing on the panel that is not gravity.

    The marble rides one thread of the helix, so its rotation *is* the screw's
    rotation and there is nothing to keep in sync at render time.
    """
    n = 120
    sx = [SCREW_X + SCREW_R] * n
    sh = list(np.linspace(SCREW_BOT, SCREW_TOP, n))
    d = abs(SCREW_BOT - SCREW_TOP)
    run.extend(sx, sh, rail=False, speed=d / ride, mark=MARK_SCREW)
    return run


# --------------------------------------------------------------------------
# Baking. Everything that does not move goes into one background image; every
# part that does move goes into a small stack of pre-rendered patches indexed
# by its phase. That is the whole performance story: a frame is a background
# copy, three patch copies and one blit per marble, and it costs the same
# whatever the marble is doing.
# --------------------------------------------------------------------------

SCREW_BOX = (5, 24, 2, 62)              # x0, x1, y0, y1
SEE_BOX = (211, 253, 43, 62)
CRADLE_BOX = (79, 117, 50, 62)
N_SCREW_PHASE, N_TILT, N_SETTLE = 16, 13, 12
SETTLE = 0.30                           # seconds for the queue to nudge along
RESET = 0.55                            # seconds for the lever to fall back


def _rail_path(run, skip):
    """The rail polyline, as (x, y) pairs, with the given index range cut out."""
    out, cur = [], []
    for i in range(len(run.RX)):
        if run.RX[i] is None or skip[0] <= i < skip[1]:
            if len(cur) > 1:
                out.append(cur)
            cur = []
        else:
            cur.append((run.RX[i], run.RY[i]))
    if len(cur) > 1:
        out.append(cur)
    return out


def _draw_rail(img, chain, sleeper_every=13.0):
    """A rail as a thin bright line with short brackets hanging off it.

    A solid mass of track hides where the marble is going. A 1 px rail with
    visible supports lets the eye run ahead of the ball, which is most of why
    a marble run is watchable at all.
    """
    _poly(img, chain, INK_RAIL, r=0.55)
    acc = 0.0
    for i in range(1, len(chain)):
        acc += math.hypot(chain[i][0] - chain[i - 1][0],
                          chain[i][1] - chain[i - 1][1])
        if acc >= sleeper_every:
            acc = 0.0
            x, y = chain[i]
            _seg(img, (x, y + 0.5), (x, y + 3.4), INK_POST, r=0.5)


def _draw_funnel(img):
    """The funnel as a cone seen from just above: two ellipses and meridians.

    The back half of each ellipse is drawn darker than the front half, which
    is the only depth cue on the panel and is what makes the spiral read as
    going round something rather than as a zigzag.
    """
    for r, y, n in ((FUN_RIM_R, FUN_RIM_Y, 96), (FUN_HOLE_R, FUN_HOLE_Y, 40)):
        a = np.linspace(0.0, TAU, n)
        px = FUN_CX + r * np.cos(a)
        py = y + ELL * r * np.sin(a)
        for i in range(n - 1):
            back = py[i] < y
            _seg(img, (px[i], py[i]), (px[i + 1], py[i + 1]),
                 INK_FUNNEL_BACK if back else INK_FUNNEL, r=0.5)
    for k in range(12):
        a = k * TAU / 12.0
        back = math.sin(a) < 0
        p0 = (FUN_CX + FUN_RIM_R * math.cos(a),
              FUN_RIM_Y + ELL * FUN_RIM_R * math.sin(a))
        p1 = (FUN_CX + FUN_HOLE_R * math.cos(a),
              FUN_HOLE_Y + ELL * FUN_HOLE_R * math.sin(a))
        _seg(img, p0, p1, INK_FUNNEL_BACK if back else INK_FUNNEL,
             r=0.45, gain=0.55)


def _bake_background(run, see_range):
    img = np.zeros((H, W, 3), f32)
    img[:] = np.array(INK_BG, f32)
    # Frame uprights, very dim: the thing the whole run is bolted to.
    for x in (44.0, 156.0, 208.0):
        _seg(img, (x, 1.0), (x, 62.0), INK_FRAME, r=0.5)
    _draw_funnel(img)
    for chain in _rail_path(run, see_range):
        _draw_rail(img, chain)
    # The groove under the cradle's queue, which no path sample covers because
    # the marble hops across it.
    y0 = CRADLE_Y + R_MARBLE
    _draw_rail(img, [(CRADLE_HIT, y0),
                     (CRADLE_STOP - BALL_R - 1.5,
                      y0 + CRADLE_SLOPE * (CRADLE_HIT - CRADLE_STOP + BALL_R))])
    # The stop the queue rests against.
    xs = CRADLE_STOP - BALL_R - 1.2
    ys = CRADLE_Y + CRADLE_SLOPE * (CRADLE_HIT - xs)
    _seg(img, (xs, ys + 3.0), (xs, ys - 3.2), INK_STEEL, r=0.6)
    # The cradle's frame: a bar over the queue with a string to each ball.
    bx0, bx1 = CRADLE_STOP - 4.0, CRADLE_STOP + 3 * BALL_D + 4.0
    bar = CRADLE_Y - 7.5
    _seg(img, (bx0, bar), (bx1, bar), INK_POST, r=0.55)
    return img


def _bake_screw(bg):
    """The lift, as a helix around a shaft, in N_SCREW_PHASE rotations.

    Only the drawn phase changes; the threads sweep upward past a marble that
    is riding one of them at a fixed azimuth, which is what a screw lift
    actually looks like and is much easier to read at 6 px radius than a
    corkscrewing ball would be.
    """
    x0, x1, y0, y1 = SCREW_BOX
    out = []
    for k in range(N_SCREW_PHASE):
        img = bg.copy()
        ph = k * TAU / N_SCREW_PHASE
        _seg(img, (SCREW_X, SCREW_TOP - 3.0), (SCREW_X, SCREW_BOT + 2.0),
             INK_SHAFT, r=1.1)
        n = 420
        th = np.linspace(0.0, TAU * SCREW_TURNS, n) + ph
        hx = SCREW_X + SCREW_R * np.cos(th)
        hz = SCREW_R * np.sin(th)
        hy = np.linspace(SCREW_BOT, SCREW_TOP, n) + ELL * hz
        for i in range(n - 1):
            front = hz[i] > 0
            c = INK_BRASS if front else INK_SHAFT
            _seg(img, (hx[i], hy[i]), (hx[i + 1], hy[i + 1]), c,
                 r=0.55 if front else 0.45)
        # Bearings top and bottom, so it reads as driven rather than floating.
        _seg(img, (SCREW_X - 3.0, SCREW_TOP - 3.0),
             (SCREW_X + 3.0, SCREW_TOP - 3.0), INK_POST, r=0.6)
        _seg(img, (SCREW_X - 3.5, SCREW_BOT + 2.2),
             (SCREW_X + 3.5, SCREW_BOT + 2.2), INK_POST, r=0.7)
        out.append(img[y0:y1, x0:x1].copy())
    return out


def _bake_seesaw(bg):
    """The lever, at N_TILT slopes from entry-down to entry-up."""
    x0, x1, y0, y1 = SEE_BOX
    s0 = SEE_RISE / SEE_L
    out = []
    for k in range(N_TILT):
        s = s0 - 2.0 * s0 * k / (N_TILT - 1.0)
        img = bg.copy()
        a = (SEE_CX + SEE_L, SEE_CY + SEE_L * s)
        b = (SEE_CX - SEE_L, SEE_CY - SEE_L * s)
        # The lever's top surface is where the marble rides, so the bar is
        # drawn a marble radius below the height profile the solver used.
        _seg(img, (a[0], a[1] + R_MARBLE), (b[0], b[1] + R_MARBLE),
             INK_RAIL, r=0.7)
        _seg(img, (a[0], a[1] + R_MARBLE + 1.4), (a[0], a[1] + R_MARBLE - 1.4),
             INK_STEEL, r=0.5)
        _seg(img, (b[0], b[1] + R_MARBLE + 1.4), (b[0], b[1] + R_MARBLE - 1.4),
             INK_STEEL, r=0.5)
        # Pivot: an A-frame up to the fulcrum with a bright pin on top. The
        # triangle is what makes a short bar read as a see-saw rather than as
        # another piece of rail.
        base = SEE_CY + 8.0
        _seg(img, (SEE_CX - 3.4, base), (SEE_CX, SEE_CY + R_MARBLE + 0.8),
             INK_POST, r=0.6)
        _seg(img, (SEE_CX + 3.4, base), (SEE_CX, SEE_CY + R_MARBLE + 0.8),
             INK_POST, r=0.6)
        _seg(img, (SEE_CX - 4.2, base), (SEE_CX + 4.2, base), INK_POST, r=0.6)
        _dot(img, SEE_CX, SEE_CY + R_MARBLE, 1.1, INK_RAIL_HI)
        out.append(img[y0:y1, x0:x1].copy())
    return out


def _bake_cradle(bg):
    """The queue of steel balls, at N_SETTLE positions along its nudge.

    q = 1 is the instant after the strike, with the arriving marble as the new
    last ball and a gap where the ejected one was; q = 0 is the queue back
    against its stop, which is where it spends nearly all of its time.
    """
    x0, x1, y0, y1 = CRADLE_BOX
    span = CRADLE_HIT - CRADLE_STOP - (CRADLE_N - 1) * BALL_D
    out = []
    for k in range(N_SETTLE):
        q = k / (N_SETTLE - 1.0)
        img = bg.copy()
        for i in range(CRADLE_N):
            bx = CRADLE_STOP + i * BALL_D + q * span
            by = CRADLE_Y + CRADLE_SLOPE * (CRADLE_HIT - bx) - 0.2
            _seg(img, (bx, by - BALL_R - 0.5), (bx, CRADLE_Y - 7.5),
                 INK_POST, r=0.4)                       # its string
            _ball(img, bx, by, BALL_R, INK_STEEL)
        if q > 0.8:                                     # the click
            g = (q - 0.8) / 0.2
            _dot(img, CRADLE_STOP + 3 * BALL_D + span * q - BALL_R,
                 CRADLE_Y - 0.2, 3.4, (255, 250, 230), gain=0.85 * g)
            _dot(img, CRADLE_STOP, CRADLE_Y + CRADLE_SLOPE * span * q,
                 3.0, (255, 250, 230), gain=0.7 * g)
        out.append(img[y0:y1, x0:x1].copy())
    return out


def _bake_sprites(colours):
    """Marble sprites, per (colour, brightness, rotation, subpixel).

    A 2.6 px ball has to read as a sphere and it has to visibly roll, and both
    of those are shading problems rather than shape problems: a lambert body
    with a fixed specular pip says sphere, and one dark spot orbiting the
    centre says rolling. The spot sits at depth 0.6 r so it never disappears
    round the back, which at five pixels across would just look like flicker.

    The alpha, shading and spot are computed once per (rotation, subpixel) and
    only the colour multiply is repeated, because this is 1350 sprites and the
    Pi builds them on a worker thread with a segment waiting on it.
    """
    grid = np.arange(2 * SPR_R, dtype=f32)
    r = R_MARBLE
    base = []
    for ir in range(N_ROT):
        th = ir * TAU / N_ROT
        row = []
        for jy in range(N_SUB):
            for jx in range(N_SUB):
                cx = (SPR_R - 1) + jx / float(N_SUB)
                cy = (SPR_R - 1) + jy / float(N_SUB)
                dx = grid[None, :] - cx
                dy = grid[:, None] - cy
                d = np.sqrt(dx ** 2 + dy ** 2)
                a = np.clip(r + 0.5 - d, 0.0, 1.0)
                nz = np.sqrt(np.clip(1.0 - (d / r) ** 2, 0.0, 1.0))
                lam = np.clip((-dx / r) * 0.50 + (-dy / r) * 0.55 + nz * 0.66,
                              0.0, 1.0)
                body = a * (0.30 + 0.82 * lam * lam)
                sx, sy = 0.78 * r * math.cos(th), 0.78 * r * math.sin(th)
                spot = np.clip(1.25 - np.sqrt((dx - sx) ** 2 + (dy - sy) ** 2),
                               0.0, 1.0) * a
                body = body * (1.0 - 0.62 * spot)
                spec = a * np.exp(-((dx + 0.78 * r) ** 2
                                    + (dy + 0.80 * r) ** 2) / 0.60)
                row.append((body, spec))
        base.append(row)

    white = np.array((255.0, 255.0, 255.0), f32)
    banks = []
    for colour in colours:
        col = np.array(colour, f32)
        per_bright = []
        for b in BRIGHT:
            per_rot = []
            for ir in range(N_ROT):
                subs = []
                for body, spec in base[ir]:
                    rgb = body[:, :, None] * (col * b)
                    rgb += spec[:, :, None] * (white * b)
                    subs.append(np.clip(rgb, 0, 255).astype(np.uint8))
                per_rot.append(subs)
            per_bright.append(per_rot)
        banks.append(per_bright)
    return banks


def _blit(dst, spr, x0, y0):
    """One np.maximum, clipped at the panel edge."""
    sh, sw = spr.shape[:2]
    sx0 = 0 if x0 >= 0 else -x0
    sy0 = 0 if y0 >= 0 else -y0
    ex = min(sw, W - x0)
    ey = min(sh, H - y0)
    if ex <= sx0 or ey <= sy0:
        return
    d = dst[y0 + sy0:y0 + ey, x0 + sx0:x0 + ex]
    np.maximum(d, spr[sy0:ey, sx0:ex], out=d)


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--seed", type=int, default=7,
                    help="which marble colours turn up, and in what order")
    ap.add_argument("--marbles", type=int, default=3,
                    help="how many are on the run at once (1-5)")
    ap.add_argument("--gravity", type=float, default=190.0,
                    help="px/s^2; the whole run is timed by this")
    ap.add_argument("--drag", type=float, default=0.0022,
                    help="rolling resistance, per px of v^2")
    ap.add_argument("--ride", type=float, default=2.2,
                    help="target seconds for the screw lift")


def build(args):
    rng = np.random.RandomState(args.seed & 0x7fffffff)
    nm = int(min(5, max(1, args.marbles)))

    run = _solve(args.gravity, args.drag)
    t_grav = run.t

    # The loop is a real constraint: below v^2 = g r at the top the marble
    # would leave the rail, and the panel would show a ball gliding round the
    # inside of a loop it could not stay on. Nothing downstream can fix this,
    # so it is checked here and said out loud.
    i0 = bisect.bisect_left(run.T, run.marks[MARK_LOOP][0])
    i1 = bisect.bisect_right(run.T, run.marks[MARK_LOOP][1])
    top = i0 + int(np.argmin(np.array(run.Hh[i0:i1])))
    dt = run.T[top + 1] - run.T[top - 1]
    v_top2 = (math.hypot(run.X[top + 1] - run.X[top - 1],
                         run.Hh[top + 1] - run.Hh[top - 1]) / dt) ** 2
    loop_margin = v_top2 / (args.gravity * LOOP_R) - 1.0
    if loop_margin < 0.05:
        sys.stderr.write("marble: the loop is marginal (%.0f%% of g*r) -- "
                         "less drag or a steeper first ramp\n"
                         % (100.0 * (1.0 + loop_margin)))

    # The screw's period has to divide the lap, and divide it nm ways as well,
    # or the second marble arrives at the bottom between two threads. Pick the
    # integer number of screw revolutions per lap nearest the requested ride
    # time and solve back for the ride: ride = NT * t_grav / (m - NT).
    m = int(round(SCREW_TURNS * (t_grav / max(0.3, args.ride) + 1.0)))
    m = max(SCREW_TURNS + nm, int(round(m / float(nm))) * nm)
    ride = SCREW_TURNS * t_grav / float(m - SCREW_TURNS)
    _add_screw(run, ride)
    period = run.t

    TS = list(run.T)
    PX = list(run.X)
    PY = [run.Hh[i] + ELL * run.Z[i] for i in range(len(run.X))]
    PZ = list(run.Z)
    PS = list(run.SP)
    nsamp = len(TS)

    see_lo = bisect.bisect_left(TS, run.marks[MARK_SEESAW][0])
    see_hi = bisect.bisect_right(TS, run.marks[MARK_SEESAW][1])
    t_see0, t_see1 = run.marks[MARK_SEESAW]
    t_hit = run.marks[MARK_CRADLE][1]
    s0 = SEE_RISE / SEE_L

    bgf = _bake_background(run, (see_lo, see_hi))
    BG = np.clip(bgf, 0, 255).astype(np.uint8)
    SCREW = [np.clip(p, 0, 255).astype(np.uint8) for p in _bake_screw(bgf)]
    SEESAW = [np.clip(p, 0, 255).astype(np.uint8) for p in _bake_seesaw(bgf)]
    CRADLE = [np.clip(p, 0, 255).astype(np.uint8) for p in _bake_cradle(bgf)]

    order = list(rng.permutation(len(MARBLE_COLOURS)))[:nm]
    SPR = _bake_sprites([MARBLE_COLOURS[i] for i in order])

    omega = TAU * SCREW_TURNS / ride          # screw angular rate, rad/s
    offsets = [k * period / nm for k in range(nm)]
    out = np.empty((H, W, 3), np.uint8)
    sx0, sx1, sy0, sy1 = SCREW_BOX
    ex0, ex1, ey0, ey1 = SEE_BOX
    cx0, cx1, cy0, cy1 = CRADLE_BOX

    def at(tau):
        """Position, depth and spin at a lap time. Plain Python floats: the
        table has two thousand rows and a bisect plus a lerp is cheaper than
        the numpy call that would replace it."""
        i = bisect.bisect_left(TS, tau)
        if i <= 0:
            return PX[0], PY[0], PZ[0], PS[0]
        if i >= nsamp:
            i = nsamp - 1
        a, b = TS[i - 1], TS[i]
        f = 0.0 if b <= a else (tau - a) / (b - a)
        g = 1.0 - f
        return (PX[i - 1] * g + PX[i] * f, PY[i - 1] * g + PY[i] * f,
                PZ[i - 1] * g + PZ[i] * f, PS[i - 1] * g + PS[i] * f)

    def paint(bank, x, y, z, spin, level):
        ix, iy = int(math.floor(x)), int(math.floor(y))
        jx = int((x - ix) * N_SUB)
        jy = int((y - iy) * N_SUB)
        ir = int(spin / TAU * N_ROT) % N_ROT
        _blit(out, bank[level][ir][jy * N_SUB + jx], ix - SPR_R + 1,
              iy - SPR_R + 1)

    def render(t, frame):
        np.copyto(out, BG)

        # The screw turns whether or not anything is riding it: it is the
        # motor. Threads sweep upward, so the drawn phase runs backwards.
        k = int((-omega * t / TAU) % 1.0 * N_SCREW_PHASE) % N_SCREW_PHASE
        out[sy0:sy1, sx0:sx1] = SCREW[k]

        # Mechanism states, from the marbles' own progress. Only one marble
        # can be in any given mechanism at a time -- the phase spacing is far
        # wider than any mechanism's dwell -- so max() picks it out.
        tilt, settle = 0, 0
        for off in offsets:
            tau = (t - off) % period
            if t_see0 <= tau <= t_see1:
                s = _see_slope(SEE_CX - at(tau)[0])
            elif t_see1 < tau < t_see1 + RESET:
                p = (tau - t_see1) / RESET
                e = 1.0 - (1.0 - p) ** 2 * math.cos(4.2 * p)
                s = -s0 + 2.0 * s0 * min(1.0, max(0.0, e))
            else:
                s = s0
            i = int(round((s0 - s) / (2.0 * s0) * (N_TILT - 1)))
            tilt = max(tilt, min(N_TILT - 1, max(0, i)))
            if t_hit <= tau < t_hit + SETTLE:
                p = (tau - t_hit) / SETTLE
                q = (1.0 - p) ** 2
                settle = max(settle, int(round(q * (N_SETTLE - 1))))
        out[ey0:ey1, ex0:ex1] = SEESAW[tilt]
        out[cy0:cy1, cx0:cx1] = CRADLE[settle]

        # Marbles last, so they sit on top of every mechanism.
        for mi, off in enumerate(offsets):
            bank = SPR[mi]
            tau = (t - off) % period
            for gi in range(2):
                gx, gy, gz, gs = at((tau - GHOST_DT[1 - gi]) % period)
                paint(bank, gx, gy, gz, gs, GHOST_LEVEL[1 - gi])
            x, y, z, spin = at(tau)
            lvl = DEPTH_LEVEL[0 if z > 5.0 else (1 if z > -5.0 else 2)]
            paint(bank, x, y, z, spin, lvl)
        return out

    render.period = period
    render.at = at
    render.ride = ride
    render.t_grav = t_grav
    render.run = run
    render.loop_margin = loop_margin
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
