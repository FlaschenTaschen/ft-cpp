#!/usr/bin/env python3
"""A domino run toppling all the way across, and a hand standing it back up.

Tiles stand in a line across the whole 320 px. A finger comes in from the left
and tips the first one. The wave crosses the panel -- fast where the spacing is
tight, lazy where it is wide -- splits onto a second run at the back, races
itself, rejoins, hangs on one tile that very nearly does not go over, and
finishes. Then a hand sweeps in from the right and stands them all back up,
which is the half of the video everybody secretly likes best, and it goes
again.

The one representation
----------------------
**A tile is a rigid rectangle rotating about its bottom edge, and the entire
run is the same tile with a different start time.** `build()` lays out the
pivots, wires up which tile knocks which, and solves for the exact second each
one begins to fall; `render(t)` is then a table lookup per tile -- `t` minus its
own start, through one shared fall curve. Nothing is simulated per frame, so
the run can be as long as you like at no per-frame cost, and `render` is a pure
function of `t` by construction rather than by care.

The fall curve is the real one. A thin rod pivoting about its end obeys

    d2(theta)/dt2 = (3g / 2L) sin(theta)

which is integrated once at build time into a (time -> angle) table and then
reused by every tile after rescaling to seconds. That is where the *mass* comes
from: a domino barely moves for the first third of its fall and then goes over
all at once, and a marquee or a sine wave does not do that. It is also what
makes the spacing matter -- a tile hits its neighbour when its top corner has
swung out as far as the gap, `sin(theta_c) = gap / height`, so a tight gap is
caught early in the slow part of the arc and a wide one late in the fast part.
Vary the gap along the run and the ripple speeds up and slows down for free.

Coupling, and why nothing has to be simulated
---------------------------------------------
A falling tile does not drop to the floor, it lands on the back of the next one
and stops there. Rather than solve a constrained chain, each tile carries a
*ceiling* on its angle that opens as its neighbour gets out of the way:

    limit(k) = rest(k) + (contact(k) - rest(k)) * (1 - progress(k+1))

where `rest = acos(thickness / pitch)` is the angle a stack of parallel leaning
slabs settles at, and `progress` is how far the neighbour has fallen. The
neighbour's *unconstrained* angle is used, so this is one pass and not a
fixed point. Three things fall out of it that would otherwise each have needed
code: a tile decelerates the instant it makes contact, a finished run is a
stack of slabs all leaning at the same angle rather than a row of flat lines,
and the tile in front of the stalled one hangs there at 74 degrees, held up by
the tile that will not go, until it does -- and then follows it down.

The stall is a wide gap, not a special case. A gap of 0.96 of the tile height
is caught at 74 degrees, right at the end of the arc, so the run genuinely
nearly dies there; a short teeter is added on top so it reads as comedy rather
than as a dropped frame.

Cost
----
The frame is a `uint8` code buffer -- 0 empty, otherwise `1 + colour*3 + face`
-- composited tile by tile and turned into pixels by one masked palette lookup
over the lit pixels only. Every tile that is standing still (not yet tipped, or
already at rest) uses a patch baked in `build()`, so the only rotated
rectangles rasterised per frame are the three to eight that are actually in
flight. A frame is about eighty numpy calls on small arrays regardless of how
long the run is; the count scales with the *number of tiles* through the
composite loop, and `--pitch` is the knob for that.

No pips: a run is seen down its length, so what faces you is the tile's narrow
edge, and the pips are on the two faces you cannot see. What the tiles get
instead is a dark outline and a bright end cap, which is what makes a finished
run read as a stack of leaning slabs and not as a row of dashes.

Run:  python3 dominoes.py --host 127.0.0.1
      python3 dominoes.py --seed 12 --pitch 1.3     # slow, wide, lazy
      python3 dominoes.py --no-branch --no-stall    # just the wave
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * np.pi
HALF_PI = np.pi * 0.5

# Tile colours. Saturated and light, because a 4 px wide bar on a dark ground
# at three metres is the whole subject. The back run reuses these dimmed, which
# is the only depth cue the panel gets and is enough.
TILE_RGB = (
    (255, 74, 74),        # red
    (255, 168, 46),       # amber
    (110, 230, 120),      # green
    (70, 214, 255),       # cyan
    (140, 148, 255),      # periwinkle
    (255, 106, 214),      # magenta
)
NC = len(TILE_RGB) * 2                       # bright set, then the dim set
DIM = 0.52

SKIN = (236, 186, 152)
SKIN_DARK = (116, 74, 62)
NAIL = (255, 226, 208)

# Code layout in the frame buffer: 0 is empty, a tile pixel is
# 1 + colour*3 + face with face in (outline, body, cap), and the hand sits
# above all of that.
HAND0 = 1 + NC * 3


# --------------------------------------------------------------------------
# The fall curve.
#
# Integrated once, dimensionless: phi'' = sin(phi), started with a small tip
# and a small kick so a tile that has just been hit does not have to crawl
# out of the exponential creep the way a tile balanced perfectly would. The
# kick is what sets how many tiles are in the air at once, which is the single
# most important number in how a domino run looks; it is exposed as --kick.
# --------------------------------------------------------------------------

def fall_curve(phi0, kick, step=0.0015):
    """(time, angle) for a rod toppling from rest-ish to flat, dimensionless."""
    phi = float(phi0)
    w = float(kick)
    # One sample at (0, 0) and nothing duplicated: np.interp with a repeated
    # x is not the identity at that x, and a curve that answers "0.02 rad"
    # for tau=0 makes every tile in the run count as in flight from the first
    # frame, which is invisible on the panel and quietly triples the cost.
    ts = [0.0]
    ph = [0.0]
    s = 0.0
    # Semi-implicit Euler: it conserves the shape of a pendulum problem far
    # better than forward Euler at the same step, and the step here is tiny.
    while phi < HALF_PI and len(ph) < 20000:
        w += np.sin(phi) * step
        phi += w * step
        s += step
        ts.append(s)
        ph.append(min(phi, HALF_PI))
    ts = np.array(ts, np.float64)
    ph = np.array(ph, np.float64)
    # Strictly increasing angle, so it can be inverted with np.interp.
    ph[-1] = HALF_PI
    return ts / ts[-1], ph


# --------------------------------------------------------------------------
# Rasterising a tile.
#
# One rotated rectangle, evaluated in the tile's own frame over a fixed patch:
# a = distance along the tile's thickness, b = distance up its length. Doing it
# this way rather than as four half-planes halves the arithmetic, and doing it
# for every in-flight tile at once as an (n, h, w) stack means the whole
# rasteriser is a dozen numpy calls no matter how many tiles are moving.
# --------------------------------------------------------------------------

def patch_geom(th, tt):
    """The patch box a tile can occupy relative to its pivot, plus grids."""
    # Angles run from a little negative (the stand-up overshoot) to flat.
    hb = th + 3
    oy = th + 2
    ox = tt + 2
    wb = ox + th + 2
    LX = (np.arange(wb, dtype=f32) - ox)[None, None, :]
    LY = (np.arange(hb, dtype=f32) - oy)[None, :, None]
    return {"hb": hb, "wb": wb, "oy": oy, "ox": ox, "LX": LX, "LY": LY,
            "th": f32(th), "tt": f32(tt)}


def patches(theta, cidx, g):
    """(n, hb, wb) uint8 code patches for n tiles at the given angles."""
    c = np.cos(theta).astype(f32)[:, None, None]
    s = np.sin(theta).astype(f32)[:, None, None]
    LX, LY = g["LX"], g["LY"]
    a = LX * c + LY * s                     # across the thickness, 0 at pivot
    b = LX * s - LY * c                     # up the tile, 0 at the floor
    th, tt = g["th"], g["tt"]
    body = (a >= -tt) & (a <= 0.0) & (b >= 0.0) & (b <= th)
    inner = (a >= 1.0 - tt) & (a <= -1.0) & (b >= 1.0) & (b <= th - 1.0)
    cap = inner & (b >= th - 2.5)
    code = body.astype(np.uint8) + inner + cap
    code += body * (cidx.astype(np.uint8) * 3)[:, None, None]
    return code


def blit(dst, src, mask, y, x):
    """Composite a code patch at (y, x), clipped to the buffer."""
    h, w = dst.shape
    ph, pw = src.shape
    dy0, dx0 = max(0, y), max(0, x)
    dy1, dx1 = min(h, y + ph), min(w, x + pw)
    if dy1 <= dy0 or dx1 <= dx0:
        return
    np.copyto(dst[dy0:dy1, dx0:dx1],
              src[dy0 - y:dy1 - y, dx0 - x:dx1 - x],
              where=mask[dy0 - y:dy1 - y, dx0 - x:dx1 - x])


# --------------------------------------------------------------------------
# The hand. A finger, really -- at 64 rows a whole hand is a blob, and a
# finger entering from off-panel with a lit nail on the end reads instantly as
# somebody reaching in. Built as a tapered capsule so it needs no artwork.
# --------------------------------------------------------------------------

def finger(length, wide):
    """A code patch of a finger, plus where its tip is inside the patch.

    The finger is long enough to run off the top of the panel from wherever
    its tip is put, which is the whole trick: a hand that starts inside the
    frame is a floating stub, and a finger that comes in from outside reads as
    somebody reaching in even when only a third of it is on screen.
    """
    hb = length + 4
    wb = int(round(length * 0.55)) + wide * 2 + 4
    ty, tx = hb - wide - 2.0, wb - wide - 2.0        # the tip
    by, bx = ty - length, tx - length * 0.5          # off the top-left
    ys = np.arange(hb, dtype=f32)[:, None] - ty
    xs = np.arange(wb, dtype=f32)[None, :] - tx
    dy, dx = by - ty, bx - tx
    L2 = dy * dy + dx * dx
    u = np.clip((ys * dy + xs * dx) / L2, 0.0, 1.0)
    d = np.hypot(ys - u * dy, xs - u * dx)
    r = wide + u * (wide * 0.55)                     # tapers wider at the base
    skin = d <= r
    out = d <= r + 1.0
    nail = (d <= wide * 0.72) & (u <= 0.14)
    code = np.where(nail, np.uint8(HAND0 + 2),
                    np.where(skin, np.uint8(HAND0 + 1),
                             np.where(out, np.uint8(HAND0), np.uint8(0))))
    return code.astype(np.uint8), int(round(ty)), int(round(tx))


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--seed", type=int, default=5,
                    help="the run: spacings, colours, where it branches and "
                         "which tile nearly does not go over")
    ap.add_argument("--fall", type=float, default=0.42,
                    help="seconds a free tile takes to go from upright to "
                         "flat; real dominoes are about 0.25")
    ap.add_argument("--kick", type=float, default=0.80,
                    help="how hard a tile is hit, dimensionless. Higher puts "
                         "more tiles in the air at once and speeds the wave")
    ap.add_argument("--pitch", type=float, default=1.0,
                    help="multiplies every spacing; bigger means fewer, "
                         "lazier tiles and a cheaper frame")
    ap.add_argument("--hold", type=float, default=1.5,
                    help="seconds the finished run is left lying there")
    ap.add_argument("--sweep", type=float, default=3.3,
                    help="seconds the hand takes to cross the panel standing "
                         "them back up")
    ap.add_argument("--pause", type=float, default=0.7,
                    help="seconds of everything standing still before it goes "
                         "again")
    ap.add_argument("--no-branch", dest="branch", action="store_false",
                    default=True, help="one run, no split to the back")
    ap.add_argument("--no-stall", dest="stall", action="store_false",
                    default=True, help="no tile that nearly does not go over")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    sc = H / 64.0
    sw = W / 320.0

    # ---- the tile, and the three levels it stands on ---------------------
    TT = max(2, int(round(4 * sc)))                  # thickness, px
    TH = max(8, int(round(17 * sc)))                 # height, px
    y_front = H - 3
    y_mid = y_front - TH - 1
    y_back = y_mid - TH - 1
    g = patch_geom(TH, TT)

    # ---- lay out the runs ------------------------------------------------
    # Spacing is quoted as a fraction of the tile height, which is how domino
    # people talk about it: below about 0.5 the tiles are too close to build
    # any speed, above about 0.95 the top corner cannot reach the next one and
    # the run simply stops.
    SPACINGS = (0.58, 0.70, 0.82, 0.94)
    STALL_GAP = 1.18                                 # 0.96 of the height, bare
    LEAN_MAX = float(np.hypot(TH, TT))               # see rest[] below
    px, py, lane, col, nxt = [], [], [], [], []

    def add(x, y, lane_i, c):
        px.append(float(x))
        py.append(float(y))
        lane.append(lane_i)
        col.append(int(c))
        nxt.append(-1)
        return len(px) - 1

    pal = list(rng.permutation(len(TILE_RGB)))
    # The run starts a little way in, so that the finger that tips the first
    # tile has somewhere to be: a finger whose tip is at x=2 is one column of
    # skin at the edge of the panel and reads as a glitch.
    x = 27.0 * sw + TT
    zone = 0
    zone_left = int(rng.integers(3, 6))
    step = SPACINGS[int(rng.integers(0, len(SPACINGS)))] * TH * args.pitch
    front = []
    stall_at = -1
    # Reserve the stall for near the end, where the beat lands best: the run
    # has already done everything else it is going to do, so a pause there is
    # the last thing that happens rather than an interruption.
    stall_x = W * float(rng.uniform(0.80, 0.88))
    while x < W - 2:
        if args.stall and stall_at < 0 and x > stall_x and len(front) > 4:
            stall_at = len(front)
        front.append(add(x, y_front, 2, pal[zone % len(pal)]))
        zone_left -= 1
        if zone_left <= 0:
            zone += 1
            zone_left = int(rng.integers(3, 7))
            step = SPACINGS[int(rng.integers(0, len(SPACINGS)))] * TH * args.pitch
        x += (STALL_GAP * TH * args.pitch
              if (args.stall and stall_at == len(front)) else step)
    for i in range(len(front) - 1):
        nxt[front[i]] = front[i + 1]

    # The branch: one front tile knocks its own successor *and* a tile on the
    # step behind it, which starts a second run at a tighter spacing. The back
    # run therefore travels faster, gets to the far end first, and comes back
    # down onto the front run some way ahead of the front wave -- so the two
    # meet head on somewhere in the middle of nowhere, which is the single best
    # thing a domino run does.
    edges = []                                       # (parent, child, extra)
    for i in range(len(front) - 1):
        edges.append((front[i], front[i + 1], 0.0))
    back = []
    if args.branch and len(front) > 14:
        si = int(rng.integers(max(2, len(front) // 7), max(4, len(front) // 3)))
        ji = int(len(front) * float(rng.uniform(0.74, 0.84)))
        ji = min(len(front) - 3, max(si + 8, ji))
        bstep = 0.50 * TH * args.pitch
        bcol = pal[(zone + 3) % len(pal)] + len(TILE_RGB)   # the dim set
        up = add(px[front[si]] + 0.62 * TH * args.pitch, y_mid, 1,
                 bcol)
        edges.append((front[si], up, 0.0))
        bx = px[up] + 0.62 * TH * args.pitch
        while bx < px[front[ji]] - 1.3 * TH * args.pitch:
            back.append(add(bx, y_back, 0, bcol))
            bx += bstep
        for i in range(len(back) - 1):
            nxt[back[i]] = back[i + 1]
            edges.append((back[i], back[i + 1], 0.0))
        if back:
            edges.append((up, back[0], 0.0))
            down = add(px[back[-1]] + 0.62 * TH * args.pitch, y_mid, 1, bcol)
            edges.append((back[-1], down, 0.0))
            edges.append((down, front[ji], 0.0))

    n = len(px)
    px = np.array(px, f32)
    py = np.array(py, f32)
    col = np.array(col, np.int32)
    nxt = np.array(nxt, np.int32)

    # ---- the fall curve, and the geometry that reads from it -------------
    cs, cph = fall_curve(0.02, args.kick)
    curve_t = (cs * float(args.fall)).astype(np.float64)
    curve_a = cph

    def tau_of(angle):
        """When in its fall a tile passes this angle."""
        return float(np.interp(float(angle), curve_a, curve_t))

    # Contact angle with whatever a tile is aimed at, and the angle it comes
    # to rest at once that thing is out of the way.
    contact = np.full(n, HALF_PI, np.float64)
    rest = np.full(n, HALF_PI, np.float64)
    lean = np.arange(n, dtype=np.int32)
    for i in range(n):
        j = int(nxt[i])
        if j < 0:
            continue
        pitch = float(px[j] - px[i])
        gap = pitch - TT
        if gap >= TH:                                # cannot reach: falls flat
            continue
        lean[i] = j
        contact[i] = np.arcsin(max(0.0, gap) / float(TH))
        # A stack of parallel slabs is spaced thickness / cos(angle) apart, so
        # acos(TT / pitch) is where a leaning run settles -- but only if the
        # tile is long enough to still be touching the next one's face at that
        # angle, which needs pitch <= hypot(TH, TT). Past that it slides off
        # the end and lands flat, which is exactly what a too-wide run does.
        if pitch <= LEAN_MAX:
            rest[i] = min(HALF_PI, np.arccos(min(1.0, TT / max(1e-3, pitch))))

    # ---- when each tile starts to fall -----------------------------------
    # Dijkstra over the knock-on graph: a tile starts when the first thing
    # that can reach it gets there. With a branch that is a genuine race, and
    # the losing wave arrives to find the tiles already down.
    teeter = np.zeros(n, np.float64)
    delay = {}
    for (a, b, extra) in edges:
        gap = float(px[b] - TT - px[a])
        if gap >= TH * 0.995:
            d = tau_of(HALF_PI)                      # it fell flat; nudge only
        else:
            d = tau_of(np.arcsin(max(0.0, gap) / float(TH)))
        if args.stall and stall_at >= 0 and b == front[stall_at]:
            extra = 1.15                             # the teeter
            teeter[b] = extra
        delay.setdefault((a, b), d + extra)

    t_enter = 0.35
    t_tip = t_enter + 0.6
    INF = 1e9
    t0 = np.full(n, INF, np.float64)
    t0[front[0]] = t_tip
    done = np.zeros(n, bool)
    adj = {}
    for (a, b) in delay:
        adj.setdefault(a, []).append(b)
    for _ in range(n):
        k = -1
        best = INF
        for i in range(n):
            if not done[i] and t0[i] < best:
                best, k = t0[i], i
        if k < 0:
            break
        done[k] = True
        for b in adj.get(k, ()):
            cand = t0[k] + delay[(k, b)]
            if cand < t0[b]:
                t0[b] = cand
    t0[t0 >= INF] = t_tip                            # unreachable: never happens

    fall_full = float(curve_t[-1])
    t_down = float(t0.max()) + fall_full + 0.25
    reset_at = t_down + float(args.hold)
    RISE = 0.30
    sweep_x0, sweep_x1 = W + 26.0, -30.0
    sweep_v = (sweep_x0 - sweep_x1) / max(0.2, float(args.sweep))
    rise_t0 = reset_at + (sweep_x0 - px) / sweep_v
    cycle = float(reset_at + args.sweep + RISE + args.pause)

    # ---- angles, closed form ---------------------------------------------
    teeter_on = teeter > 0.0
    wob_t0 = t0 - teeter
    OVER = 0.18

    def fall_angles(t):
        """Every tile's angle during the toppling half of the cycle."""
        tau = t - t0
        np.maximum(tau, 0.0, out=tau)
        free = np.interp(tau, curve_t, curve_a)
        # The ceiling that opens as the neighbour gets out of the way. Using
        # the neighbour's unconstrained angle keeps this one pass.
        prog = np.clip(free[lean] / rest[lean], 0.0, 1.0)
        limit = rest + (contact - rest) * (1.0 - prog)
        th = np.minimum(free, limit)
        if teeter_on.any():
            tw = t - wob_t0
            wob = (0.07 * (1.0 - np.exp(-2.6 * np.maximum(tw, 0.0)))
                   + 0.13 * np.exp(-2.6 * np.maximum(tw, 0.0))
                   * np.sin(TAU * 2.2 * tw))
            th = np.where(teeter_on & (tw > 0.0) & (t < t0), wob, th)
        return th

    theta_rest = fall_angles(reset_at - 1e-4)

    def rise_angles(t):
        """And during the half where they are put back."""
        u = np.clip((t - rise_t0) / RISE, 0.0, 1.0)
        e = u * u * (3.0 - 2.0 * u)
        bounce = np.sin(np.pi * np.clip((u - 0.55) / 0.45, 0.0, 1.0))
        return theta_rest * (1.0 - e) - OVER * bounce

    # ---- baked patches ----------------------------------------------------
    up_patch = patches(np.zeros(n), col, g)
    up_mask = up_patch > 0
    rest_patch = patches(theta_rest, col, g)
    rest_mask = rest_patch > 0
    dst_y = (py - g["oy"]).astype(np.int32)
    dst_x = (px - g["ox"]).astype(np.int32)

    # Painter order: back to front, then left to right. A tile that has fallen
    # lies *under* the one it fell onto, which is exactly this order, and it is
    # what makes a finished run look like a stack of slabs rather than a row of
    # separate marks.
    order = sorted(range(n), key=lambda i: (py[i], px[i]))

    # ---- the hand ---------------------------------------------------------
    fing, f_ty, f_tx = finger(int(round(H * 1.05)),
                              max(2, int(round(2.6 * sc))))
    fing_mask = fing > 0
    fing_l = fing[:, ::-1].copy()                    # entering from the right
    fing_l_mask = fing_l > 0
    f_lx = fing.shape[1] - 1 - f_tx
    tip_y = int(round(py[front[0]] - TH + 2))
    tip_x0, tip_x1 = -int(round(20 * sw)), int(px[front[0]] - TT - 1)
    sweep_y = int(round(py[front[0]] - TH * 0.5))
    sweep_y_b = sweep_y - (int(py[front[0]]) - int(y_back)) if back else sweep_y

    # ---- palette and background ------------------------------------------
    pal_rgb = np.zeros((HAND0 + 3, 3), np.uint8)
    for c in range(NC):
        base = np.array(TILE_RGB[c % len(TILE_RGB)], np.float64)
        if c >= len(TILE_RGB):
            base = base * DIM
        i = 1 + c * 3
        pal_rgb[i] = np.clip(base * 0.26, 0, 255)
        pal_rgb[i + 1] = np.clip(base, 0, 255)
        pal_rgb[i + 2] = np.clip(base * 0.45 + 150, 0, 255)
    pal_rgb[HAND0] = SKIN_DARK
    pal_rgb[HAND0 + 1] = SKIN
    pal_rgb[HAND0 + 2] = NAIL

    bg = np.zeros((H, W, 3), np.uint8)
    steps = ((0, y_back, (13, 14, 19)),
             (y_back + 1, y_mid, (18, 19, 26)),
             (y_mid + 1, H - 1, (24, 25, 34)))
    for k, (a, b, cc) in enumerate(steps):
        bg[a:b + 1] = np.array(cc, np.uint8)
        # The lit front edge of each step. This is the only line on the panel
        # and it is what tells you the three runs are at three depths.
        edge = np.array((46 + 16 * k, 50 + 18 * k, 64 + 22 * k), np.uint8)
        bg[b] = edge
        if b - 1 > a:
            bg[b - 1] = np.clip(edge.astype(np.int32) // 2, 0, 255).astype(np.uint8)

    # ---- per-frame buffers ------------------------------------------------
    code = np.zeros((H, W), np.uint8)
    out = np.empty((H, W, 3), np.uint8)

    def render(t, frame_idx):
        tt = float(t) % cycle
        resetting = tt >= reset_at
        th = rise_angles(tt) if resetting else fall_angles(tt)

        # Anything not moving uses a patch that was baked in build(); only the
        # handful actually in flight is rasterised.
        moving = (th > 1e-4) & (th < theta_rest - 1e-4)
        if resetting:
            moving = (th > 1e-4) | (th < -1e-4)
            moving &= (th < theta_rest - 1e-4)
        idx = np.flatnonzero(moving)
        if idx.size:
            live = patches(th[idx], col[idx], g)
            live_mask = live > 0
            slot = {}
            for r, i in enumerate(idx):
                slot[int(i)] = r
        else:
            slot = {}

        code[:] = 0
        for i in order:
            r = slot.get(i)
            if r is None:
                if th[i] > 1e-4:
                    blit(code, rest_patch[i], rest_mask[i], dst_y[i], dst_x[i])
                else:
                    blit(code, up_patch[i], up_mask[i], dst_y[i], dst_x[i])
            else:
                blit(code, live[r], live_mask[r], dst_y[i], dst_x[i])

        # ---- the hand -----------------------------------------------------
        if resetting:
            hx = sweep_x0 - (tt - reset_at) * sweep_v
            if sweep_x1 - 4 < hx < W + 30:
                hxi = int(round(hx))
                blit(code, fing_l, fing_l_mask, sweep_y - f_ty, hxi - f_lx)
                # The second hand, on the back run, only while there is
                # anything back there to pick up. Two hands is also simply
                # what you do: you stand a branched run back up with both.
                if back and px[back[0]] - 10 < hx < px[back[-1]] + 16:
                    blit(code, fing_l, fing_l_mask, sweep_y_b - f_ty,
                         hxi - f_lx)
        elif tt < t_tip + 0.75:
            # In, push, and away again.
            if tt < t_tip:
                u = np.clip((tt - t_enter) / max(1e-3, t_tip - t_enter), 0.0, 1.0)
                u = u * u * (3.0 - 2.0 * u)
                hx = tip_x0 + (tip_x1 - tip_x0) * u
                hy = tip_y
            else:
                u = np.clip((tt - t_tip) / 0.75, 0.0, 1.0)
                hx = tip_x1 + 3.0 * min(1.0, u * 4.0) - 34.0 * u * u
                hy = tip_y - 26.0 * u * u
            blit(code, fing, fing_mask,
                 int(round(hy)) - f_ty, int(round(hx)) - f_tx)

        np.copyto(out, bg)
        lit = code > 0
        out[lit] = pal_rgb[code[lit]]
        return out

    render.bg = bg
    render.cycle = cycle
    render.n_tiles = n
    render.t0 = t0
    render.theta_rest = theta_rest
    render.reset_at = reset_at
    render.t_tip = t_tip
    render.fall_angles = fall_angles
    render.rise_angles = rise_angles
    render.pivots = (px, py)
    render.front = front
    render.back = back
    render.stall_at = (front[stall_at] if stall_at >= 0 else -1)
    render.wave_span = float(t0.max() - t0.min())
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
