#!/usr/bin/env python3
"""A laser cutter working through a job.

A searing point traces a vector path across dark material at constant arc
speed, leaving a kerf that glows white at the head and cools through yellow,
orange and deep red behind it. Interior holes are cut first, then the outline;
when the outline closes the piece drops out of the sheet and falls away, and
the next job starts somewhere else on the panel.

Everything visible is one scalar **heat** field. The head writes 1.0 into it
along the arc it covered this frame, the whole field is multiplied down once
per frame, and the result is mapped through a black -> red -> orange -> yellow
-> white ramp. That single idea gives the kerf, the cooling trail and the
bloom around the head for the price of one multiply and one lookup, which is
what makes it cheap enough for a Pi driving 320x64.

The cooling curve is the load-bearing tuning. Decay is a half-life in seconds
(`--cool`, default 1.15) applied as heat *= 0.5**(dt/cool), so the trail is the
same length in seconds at any frame rate rather than in frames. At the default
the kerf is yellow a second behind the head, orange at three, red at five and
a dying ember at eight — a whole contour stays legible as a temperature
gradient while the head is unmistakably the hottest thing on the panel. Halve
it and the demo is a moving dot with no history; double it and the entire path
sits lit and the motion is lost. Because the decay is exponential the
interesting range of heat is all near zero, so the ramp's colour stops are
placed nonlinearly rather than gamma correcting every pixel every frame.

Paths are generated, never hand-authored: finger-jointed box panels, gears
with real teeth, filigree rosettes, concentric rounded rectangles and slot
lettering. Contours are ordered nearest-first from the head, like CAM would,
and the head rapids dark between them.

Run:  python3 laser.py --host 127.0.0.1
      python3 laser.py --shapes gear,letters --speed 110 --cool 1.6 --no-smoke
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * np.pi

# Heat -> colour. Positions are heat values, not evenly spaced: exponential
# cooling spends most of its time near zero, so the whole red end lives below
# 0.1 and the white core is a thin sliver at the top.
KERF = [(0.000, (0, 0, 0)), (0.010, (26, 0, 0)), (0.030, (95, 5, 0)),
        (0.075, (185, 28, 0)), (0.170, (255, 92, 0)), (0.360, (255, 168, 30)),
        (0.640, (255, 226, 130)), (0.860, (255, 248, 215)),
        (1.000, (255, 255, 255))]
LUT_N = 1024

# The kerf stamp: a hot core with a little spill, ordered dimmest first. A
# single pixel line carries no weight on an LED wall, and the spill is also
# what makes a diagonal read as a line rather than a staircase.
_STAMP = ((1, 1, 0.10), (1, -1, 0.10), (-1, 1, 0.10), (-1, -1, 0.10),
          (1, 0, 0.30), (-1, 0, 0.30), (0, 1, 0.30), (0, -1, 0.30),
          (0, 0, 1.00))
STAMP_DY = np.array([s[0] for s in _STAMP], f32)
STAMP_DX = np.array([s[1] for s in _STAMP], f32)
STAMP_V = np.array([s[2] for s in _STAMP], f32)

SMOKE_RGB = np.array([44, 46, 54], f32)     # cool grey; the fire is the kerf
PILOT_RGB = np.array([18, 26, 40], np.uint8)  # dim blue dot on a rapid move


# --------------------------------------------------------------------------
# Geometry. Everything is a closed polyline; nothing here needs curves.
# --------------------------------------------------------------------------

def _circle(cx, cy, r, step=1.3):
    n = max(10, int(TAU * r / step))
    a = np.linspace(0.0, TAU, n, endpoint=False)
    return np.stack([cx + r * np.cos(a), cy + r * np.sin(a)], 1).astype(f32)


def _rrect(cx, cy, w, h, r):
    r = float(min(r, w * 0.5, h * 0.5))
    xs, ys = w * 0.5 - r, h * 0.5 - r
    n = max(3, int(TAU * r / 5.0))
    parts = []
    for sx, sy, a0 in ((1, 1, 0.0), (-1, 1, 0.25), (-1, -1, 0.5), (1, -1, 0.75)):
        a = np.linspace(a0 * TAU, (a0 + 0.25) * TAU, n + 1)
        parts.append(np.stack([cx + sx * xs + r * np.cos(a),
                               cy + sy * ys + r * np.sin(a)], 1))
    return np.concatenate(parts).astype(f32)


def _stroke(points, r):
    """Outline of a fattened polyline: the slot a cutter would leave.

    Vertex normals are the averaged segment normals, so a corner pinches a
    little rather than mitring out to infinity — at a 1.5 px slot radius that
    is invisible and it keeps the contour simple.
    """
    p = np.asarray(points, f32)
    d = np.diff(p, axis=0)
    keep = np.hypot(d[:, 0], d[:, 1]) > 1e-6
    if not keep.any():
        return _circle(p[0, 0], p[0, 1], r)
    p = np.concatenate([p[:1], p[1:][keep]])
    d = np.diff(p, axis=0)
    seg = d / np.hypot(d[:, 0], d[:, 1])[:, None]
    nrm = np.stack([-seg[:, 1], seg[:, 0]], 1)
    vn = np.empty_like(p)
    vn[0], vn[-1] = nrm[0], nrm[-1]
    if len(p) > 2:
        m = nrm[:-1] + nrm[1:]
        m /= np.maximum(np.hypot(m[:, 0], m[:, 1]), 1e-6)[:, None]
        vn[1:-1] = m

    def cap(at, forward):
        a0 = np.arctan2(forward[1], forward[0]) - TAU * 0.25
        a = np.linspace(a0, a0 + TAU * 0.5, 7)[1:-1]
        return np.stack([at[0] + r * np.cos(a), at[1] + r * np.sin(a)], 1)

    return np.concatenate([p + vn * r, cap(p[-1], seg[-1]),
                           (p - vn * r)[::-1], cap(p[0], -seg[0])]).astype(f32)


class Contour(object):
    """A closed path, parameterised by arc length so the head runs at speed."""

    def __init__(self, pts):
        pts = np.asarray(pts, f32)
        pts = np.concatenate([pts, pts[:1]])            # close it
        d = np.diff(pts, axis=0)
        seg = np.hypot(d[:, 0], d[:, 1])
        self.pts = pts
        self.cum = np.concatenate([[0.0], np.cumsum(seg)]).astype(f32)
        self.length = float(self.cum[-1])
        self.start = pts[0]

    def at(self, s):
        """Position(s) at arc length s. Vectorized; s is clipped to the path."""
        return (np.interp(s, self.cum, self.pts[:, 0]),
                np.interp(s, self.cum, self.pts[:, 1]))


# --------------------------------------------------------------------------
# Shapes. Each returns (holes, outline) in local coords about the origin,
# fitting inside +-r vertically and +-wmax/2 horizontally.
# --------------------------------------------------------------------------

def shape_gear(rng, r, wmax):
    teeth = int(rng.integers(9, 15))
    root = r * 0.80
    per = 26
    u = np.linspace(0.0, 1.0, per, endpoint=False)
    # Flanks are a smoothstep between root and tip: not a true involute, but
    # the curvature is what separates a gear from a cog stamped out of a star.
    prof = np.where(u < 0.10, 0.0,
            np.where(u < 0.30, (u - 0.10) / 0.20,
             np.where(u < 0.48, 1.0,
              np.where(u < 0.68, 1.0 - (u - 0.48) / 0.20, 0.0))))
    prof = prof * prof * (3.0 - 2.0 * prof)
    rad = root + (r - root) * prof
    ang = (np.arange(teeth)[:, None] + u[None, :]) * (TAU / teeth)
    rad = np.tile(rad, teeth)
    ang = ang.ravel()
    outline = np.stack([rad * np.cos(ang), rad * np.sin(ang)], 1)

    holes = [_circle(0.0, 0.0, r * 0.26)]
    spokes = int(rng.integers(4, 7))
    sr = r * 0.145
    if sr >= 2.0:
        for k in range(spokes):
            a = TAU * k / spokes + 0.3
            holes.append(_circle(r * 0.52 * np.cos(a), r * 0.52 * np.sin(a), sr))
    return holes, outline


def shape_box(rng, r, wmax):
    """A finger-jointed panel: the bread and butter of a laser job."""
    h = r * 2.0
    w = min(wmax, h * float(rng.uniform(1.4, 2.6)))
    d = max(2.0, r * 0.16)                              # finger depth
    corner = []
    for ax in range(4):
        # Walk one edge of the rectangle in its own frame, then rotate.
        length = w if ax % 2 == 0 else h
        n = max(3, int(round(length / max(5.0, r * 0.42))))
        n += (n + 1) % 2                                # odd: starts and ends in
        fw = length / n
        pts = [(-length * 0.5, 0.0)]
        for i in range(n):
            x0 = -length * 0.5 + i * fw
            y = 0.0 if i % 2 == 0 else -d
            pts += [(x0, y), (x0 + fw, y)]
        pts.append((length * 0.5, 0.0))
        pts = np.array(pts, f32)
        off = h * 0.5 if ax % 2 == 0 else w * 0.5
        pts[:, 1] += off
        if ax == 0:
            e = pts
        elif ax == 1:
            e = np.stack([pts[:, 1], -pts[:, 0]], 1)
        elif ax == 2:
            e = np.stack([-pts[:, 0], -pts[:, 1]], 1)
        else:
            e = np.stack([-pts[:, 1], pts[:, 0]], 1)
        corner.append(e)
    outline = np.concatenate(corner)

    # Holes spread across the panel, each sized to its own share of the width
    # so two of them can never collide.
    n = int(rng.integers(2, 5))
    inner = w - 4.0 * d
    cell = inner / n
    holes = []
    for k in range(n):
        cx = -inner * 0.5 + (k + 0.5) * cell
        if rng.random() < 0.45:
            holes.append(_circle(cx, 0.0, min(cell * 0.3, max(2.0, r * 0.2))))
        else:
            holes.append(_rrect(cx, 0.0, min(cell * 0.72, max(6.0, r * 0.8)),
                                max(4.0, r * 0.34), r * 0.17))
    return holes, outline


def shape_rrect(rng, r, wmax):
    h = r * 2.0
    w = min(wmax, h * float(rng.uniform(1.3, 2.4)))
    outline = _rrect(0.0, 0.0, w, h, r * 0.45)
    holes = []
    inset = max(4.0, r * 0.34)
    k = 1
    while h - 2 * inset * k > r * 0.55:
        holes.append(_rrect(0.0, 0.0, w - 2 * inset * k, h - 2 * inset * k,
                            max(1.5, r * 0.45 - inset * k * 0.5)))
        k += 1
    return holes[::-1], outline


def shape_filigree(rng, r, wmax):
    outline = _circle(0.0, 0.0, r)
    petals = int(rng.integers(5, 9))
    holes = [_circle(0.0, 0.0, r * 0.20)]
    # A leaf is two mirrored sine arcs: cheap, and it tapers to points, which
    # is what makes cut-out filigree read as filigree rather than as dots.
    u = np.linspace(0.0, 1.0, 16)
    lx = u * (r * 0.60)
    ly = np.sin(np.pi * u) ** 1.25 * (r * 0.17)
    leaf = np.concatenate([np.stack([lx, ly], 1), np.stack([lx, -ly], 1)[::-1]])
    for k in range(petals):
        a = TAU * k / petals
        c, s = np.cos(a), np.sin(a)
        p = leaf + np.array([r * 0.32, 0.0], f32)
        holes.append(np.stack([p[:, 0] * c - p[:, 1] * s,
                               p[:, 0] * s + p[:, 1] * c], 1).astype(f32))
    return holes, outline


FONT = {
    "L": [[(0, 0), (0, 4), (2, 4)]],
    "A": [[(0, 4), (0, 1), (1, 0), (2, 1), (2, 4)], [(0, 2.4), (2, 2.4)]],
    "S": [[(2, 0.4), (1.4, 0), (0.5, 0), (0, 0.7), (0.4, 1.7), (1.6, 2.2),
           (2, 3.1), (1.5, 4), (0.5, 4), (0, 3.5)]],
    "E": [[(2, 0), (0, 0), (0, 4), (2, 4)], [(0, 2), (1.5, 2)]],
    "R": [[(0, 4), (0, 0), (1.5, 0), (2, 0.8), (1.4, 1.8), (0, 2)],
          [(1.0, 2), (2, 4)]],
    "C": [[(2, 0.6), (1.3, 0), (0.6, 0), (0, 1), (0, 3), (0.6, 4), (1.3, 4),
           (2, 3.4)]],
    "U": [[(0, 0), (0, 3.2), (0.7, 4), (1.3, 4), (2, 3.2), (2, 0)]],
    "T": [[(0, 0), (2, 0)], [(1, 0), (1, 4)]],
    "M": [[(0, 4), (0, 0), (1, 1.8), (2, 0), (2, 4)]],
    "K": [[(0, 0), (0, 4)], [(2, 0), (0, 2.2), (2, 4)]],
    "F": [[(0, 4), (0, 0), (2, 0)], [(0, 2), (1.5, 2)]],
}
WORDS = ["LASER", "CUT", "MAKE", "KERF", "CRAFT", "FLASK", "SCALE"]


def shape_letters(rng, r, wmax):
    word = str(rng.choice([w for w in WORDS
                           if all(ch in FONT for ch in w)]))
    # Size the type to the smaller of the height and width budgets: five
    # characters of a 2-unit-wide font is a long plate on a 64 row panel.
    pad = r * 0.30
    s = min(r * 0.34, (wmax - 2 * pad) / (3.3 * (len(word) - 1) + 2.0))
    adv = 3.3 * s
    tw = adv * (len(word) - 1) + 2.0 * s
    slot = max(1.2, s * 0.20)
    holes = []
    for i, ch in enumerate(word):
        ox = -tw * 0.5 + i * adv
        for poly in FONT[ch]:
            p = np.array(poly, f32) * s
            p[:, 0] += ox
            p[:, 1] -= 2.0 * s
            holes.append(_stroke(p, slot))
    outline = _rrect(0.0, 0.0, tw + 2 * pad, 4.0 * s + 2 * pad, s * 0.7)
    return holes, outline


SHAPES = {"gear": shape_gear, "box": shape_box, "rrect": shape_rrect,
          "filigree": shape_filigree, "letters": shape_letters}


# --------------------------------------------------------------------------
# Rasterising, for the moment the piece drops out.
# --------------------------------------------------------------------------

def _fill(poly, H, W):
    """Nonzero-winding scanline fill of a closed polygon into a bool mask.

    Vectorized across every edge at once. A per-edge Python loop is only a few
    hundred iterations and runs once per part, but on a Pi that is tens of
    milliseconds in one frame — a visible stall, which is exactly the failure
    mode a frame-time average hides.
    """
    x0, y0 = poly[:, 0], poly[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    live = y0 != y1
    x0, y0, x1, y1 = x0[live], y0[live], x1[live], y1[live]
    if x0.size == 0:
        return np.zeros((H, W), bool)

    down = y1 > y0
    r0 = np.clip(np.ceil(np.where(down, y0, y1) - 0.5), 0, H).astype(np.int32)
    r1 = np.clip(np.ceil(np.where(down, y1, y0) - 0.5), 0, H).astype(np.int32)
    cnt = np.maximum(r1 - r0, 0)
    total = int(cnt.sum())
    if total == 0:
        return np.zeros((H, W), bool)

    # One entry per (edge, scanline it crosses), built without a loop: the
    # usual repeat-and-subtract-the-run-start trick.
    ends = np.cumsum(cnt)
    e = np.repeat(np.arange(cnt.size), cnt)
    rows = np.arange(total) - np.repeat(ends - cnt, cnt) + r0[e]
    xs = x0[e] + (x1[e] - x0[e]) * ((rows + 0.5 - y0[e]) / (y1[e] - y0[e]))
    cols = np.clip(np.ceil(xs - 0.5), 0, W + 1).astype(np.int32)
    # bincount rather than np.add.at: crossings genuinely accumulate, so the
    # indices collide, but bincount is the fast way to say that.
    acc = np.bincount(rows * (W + 2) + cols,
                      weights=np.where(down, 1.0, -1.0)[e],
                      minlength=H * (W + 2)).reshape(H, W + 2)
    return np.abs(np.cumsum(acc, axis=1)[:, :W]) > 0.5


def _dilate(m):
    o = m.copy()
    o[1:] |= m[:-1]
    o[:-1] |= m[1:]
    o[:, 1:] |= m[:, :-1]
    o[:, :-1] |= m[:, 1:]
    return o


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=82.0,
                    help="cutting speed, px/s on a 64 row panel")
    ap.add_argument("--cool", type=float, default=1.15,
                    help="kerf heat half-life in seconds; the trail stays "
                         "visible for about seven of these")
    ap.add_argument("--shapes", default=",".join(sorted(SHAPES)),
                    help="comma separated subset of %s" % ",".join(sorted(SHAPES)))
    ap.add_argument("--smoke", dest="smoke", action="store_true", default=True,
                    help="wisps rising off the head")
    ap.add_argument("--no-smoke", dest="smoke", action="store_false")
    ap.add_argument("--grain", type=float, default=1.0,
                    help="faint speckle in the uncut material, 0 = pure black")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    scale = f32(H / 64.0)
    speed = f32(args.speed) * scale
    rapid = speed * 3.2
    gravity = f32(115.0) * scale
    half_life = max(0.05, float(args.cool))

    lut = ds.gradient(KERF, LUT_N)
    heat = np.zeros((H, W), f32)
    hot = np.zeros((H, W), f32)
    # Which pixels the current job has cut. Heat alone cannot answer that: a
    # cut from fifteen seconds ago has decayed into the noise, and the flare
    # when the part drops needs to relight all of it.
    cut = np.zeros((H, W), bool)

    # A bloom stamp, precomputed: a whole frame blur is 40-200x slower on the
    # Pi than on a desktop, so the glow around the head is a 13x13 splat.
    br = max(3, int(round(6 * scale)))
    gy, gx = np.mgrid[-br:br + 1, -br:br + 1].astype(f32)
    d2 = (gx * gx + gy * gy) / f32(max(1.0, (br * 0.62) ** 2))
    bloom = (np.exp(-d2) * 0.85).astype(f32)
    bloom[br, br] = 1.0
    bloom[bloom < 0.004] = 0.0

    # Material grain as sparse speckle rather than a smooth field: a large
    # smooth dark gradient bands at 8 PWM bits, and this is cheaper anyway.
    grain_yx = grain_rgb = None
    if args.grain > 0.0:
        n = int(W * H * 0.035)
        flat = np.unique(rng.integers(0, H * W, n))
        v = rng.uniform(2.0, 9.0, flat.size) * float(args.grain)
        grain_yx = (flat // W, flat % W)
        grain_rgb = np.clip(v[:, None] * np.array([1.0, 0.85, 0.72]),
                            0, 40).astype(np.uint8)

    names = [s.strip() for s in args.shapes.split(",") if s.strip()]
    bad = [s for s in names if s not in SHAPES]
    if bad or not names:
        raise SystemExit("--shapes: unknown %s" % (bad or "(empty)",))

    # Smoke pool.
    NS = 220
    sx = np.zeros(NS, f32)
    sy = np.zeros(NS, f32)
    svx = np.zeros(NS, f32)
    svy = np.zeros(NS, f32)
    slife = np.zeros(NS, f32)
    slife0 = np.ones(NS, f32)
    smoke_budget = [0.0]

    pieces = []                                         # falling cut-outs
    prev_x = [W * 0.5]

    # ---- the job ---------------------------------------------------------
    state = {}

    def new_job():
        name = names[int(rng.integers(len(names)))]
        r = float(rng.uniform(0.30, 0.40)) * H
        holes, outline = SHAPES[name](rng, r, min(W - 24.0, H * 2.4))
        span = float(np.abs(outline[:, 0]).max())
        # Keep consecutive jobs apart, so the panel is used rather than one
        # spot being cut over and over.
        margin = span + 4.0
        cx = W * 0.5
        if W - 2 * margin > 2:
            for _ in range(8):
                cx = float(rng.uniform(margin, W - margin))
                if abs(cx - prev_x[0]) > W * 0.22:
                    break
        prev_x[0] = cx
        cy = H * 0.5 + float(rng.uniform(-1.0, 1.0)) * max(0.0, H * 0.5 - r - 2)
        off = np.array([cx, cy], f32)

        holes = [Contour(h + off) for h in holes]
        outline = Contour(outline + off)

        # Nearest-first ordering from where the head already is: that is what a
        # real toolpath does, and it makes the rapids short and legible.
        order, cur = [], np.array(state.get("pos", (cx, cy)), f32)
        left = list(holes)
        while left:
            i = int(np.argmin([np.hypot(*(c.start - cur)) for c in left]))
            order.append(left.pop(i))
            cur = order[-1].start
        ops = []
        for c in order:
            ops.append(("rapid", c.start))
            ops.append(("cut", c))
        ops.append(("rapid", outline.start))
        ops.append(("cut", outline))
        ops.append(("dwell", 0.30))
        ops.append(("drop", outline))
        ops.append(("dwell", 0.60))
        cut[:] = False
        state["ops"] = ops
        state["op"] = 0
        state["s"] = 0.0
        state["from"] = np.array(state.get("pos", (cx, cy)), f32)

    state["pos"] = np.array([W * 0.5, H * 0.5], f32)
    state["cutting"] = False
    new_job()

    def drop(outline):
        inside = _fill(outline.pts, H, W)
        mask = _dilate(inside)                          # + the kerf itself
        if not mask.any():
            return
        ys, xs = np.nonzero(mask)
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        sub = heat[y0:y1, x0:x1] * mask[y0:y1, x0:x1]
        # The moment it lets go, every cut on the part flares — not just the
        # outline that was under the head a moment ago, but the holes and the
        # lettering from a dozen seconds earlier, which have long since cooled
        # out of the ramp. That is the payoff: the whole part reveals itself as
        # one object for the second it takes to fall.
        flare = cut[y0:y1, x0:x1] & mask[y0:y1, x0:x1]
        pieces.append({"h": np.minimum(sub * 1.3 + 0.40 * flare, 1.0),
                       "x": float(x0), "y": float(y0),
                       "vy": 5.0 * float(scale),
                       "vx": float(rng.uniform(-7.0, 7.0)) * float(scale)})
        # The sheet keeps only the outer lip of the outline kerf, at its own
        # temperature: a glowing hole where the part used to be. Everything
        # inside leaves with the part, which is what makes the drop read
        # instead of leaving a ghost of it behind.
        rim = (mask & ~inside)[y0:y1, x0:x1]
        keep = heat[y0:y1, x0:x1]
        heat[y0:y1, x0:x1] = np.where(mask[y0:y1, x0:x1],
                                      np.where(rim, keep * 0.55, 0.0), keep)

    def deposit(p0, p1):
        """Write the kerf along the arc the head covered this frame.

        The whole stamp goes down in one indexed write rather than a loop over
        the nine offsets. Repeated indices are unavoidable — a pixel is the
        core of one sample and a neighbour of the next — and a plain indexed
        write lets the last one win, so the offsets are ordered dimmest first
        and the hot core is always the last writer. That is what np.maximum.at
        would guarantee, at a fraction of the cost.
        """
        n = max(1, int(np.hypot(p1[0] - p0[0], p1[1] - p0[1]) / 0.4) + 1)
        u = np.linspace(0.0, 1.0, n)
        xs = np.rint(p0[0] + (p1[0] - p0[0]) * u)
        ys = np.rint(p0[1] + (p1[1] - p0[1]) * u)
        yy = (ys[None, :] + STAMP_DY[:, None]).ravel().astype(np.int32)
        xx = (xs[None, :] + STAMP_DX[:, None]).ravel().astype(np.int32)
        ok = (xx >= 0) & (xx < W) & (yy >= 0) & (yy < H)
        if not ok.any():
            return
        yy, xx = yy[ok], xx[ok]
        v = np.repeat(STAMP_V, n)[ok]
        heat[yy, xx] = np.maximum(heat[yy, xx], v)
        cut[yy, xx] = True

    def advance(dt):
        """Run the toolpath for dt seconds, crossing op boundaries as needed."""
        guard = 0
        while dt > 1e-6 and guard < 24:
            guard += 1
            kind, val = state["ops"][state["op"]]
            pos = state["pos"]
            if kind == "dwell":
                state["cutting"] = False
                if dt < val - state["s"]:
                    state["s"] += dt
                    return
                dt -= val - state["s"]
            elif kind == "drop":
                drop(val)                                # instantaneous
            else:
                cutting = kind == "cut"
                state["cutting"] = cutting
                v = speed if cutting else rapid
                total = val.length if cutting else float(
                    np.hypot(*(val - state["from"])))
                step = min(dt * v, total - state["s"])
                s1 = state["s"] + step
                if cutting:
                    # Stepping in arc length, not in parameter, is what keeps
                    # the head at one speed through a corner.
                    p1 = np.array(val.at(s1), f32)
                    deposit(pos, p1)
                else:
                    p1 = state["from"] + (val - state["from"]) * (
                        s1 / max(total, 1e-6))
                state["pos"] = p1
                if s1 < total - 1e-6:
                    state["s"] = s1
                    return
                dt -= step / v
            # Next op.
            state["op"] += 1
            state["s"] = 0.0
            state["from"] = state["pos"].copy()
            if state["op"] >= len(state["ops"]):
                new_job()

    frame_rgb = np.zeros((H, W, 3), np.uint8)
    scaled = np.zeros((H, W), f32)
    index = np.zeros((H, W), np.uint16)
    last_t = [0.0]

    def render(t, frame_idx):
        dt = float(min(0.1, max(0.0, t - last_t[0])))
        last_t[0] = t

        k = f32(0.5 ** (dt / half_life))
        heat[:] *= k
        advance(dt)
        pos = state["pos"]
        cutting = state["cutting"]

        # Smoke: only while the beam is on the material.
        if args.smoke:
            slife[:] -= dt
            live = slife > 0.0
            if live.any():
                sy[live] += svy[live] * dt
                sx[live] += svx[live] * dt
                # Wander, so a wisp curls instead of rising as a ruled line.
                svx[live] += (rng.random(int(live.sum()), dtype=np.float32) - 0.5) * 26.0 * dt
                svx[live] *= 0.94
            if cutting:
                smoke_budget[0] += 34.0 * dt
                n = int(smoke_budget[0])
                if n > 0:
                    smoke_budget[0] -= n
                    free = np.flatnonzero(slife <= 0.0)[:n]
                    if free.size:
                        m = free.size
                        sx[free] = pos[0] + rng.normal(0.0, 0.7, m)
                        sy[free] = pos[1] + rng.normal(0.0, 0.7, m)
                        svx[free] = rng.normal(0.0, 4.0, m)
                        svy[free] = -rng.uniform(7.0, 17.0, m) * float(scale)
                        slife[free] = slife0[free] = rng.uniform(1.2, 2.6, m)

        # Falling pieces cool a little faster than the sheet: they are in air.
        if pieces:
            kp = f32(0.5 ** (dt / (half_life * 1.15)))
            for p in pieces:
                p["h"] *= kp
                p["vy"] += gravity * dt
                p["y"] += p["vy"] * dt
                p["x"] += p["vx"] * dt
            pieces[:] = [p for p in pieces if p["y"] < H and p["h"].max() > 0.004]

        # ---- compose -----------------------------------------------------
        if pieces:
            np.copyto(hot, heat)
            for p in pieces:
                ph, pw = p["h"].shape
                iy, ix = int(round(p["y"])), int(round(p["x"]))
                dy0, dy1 = max(0, iy), min(H, iy + ph)
                dx0, dx1 = max(0, ix), min(W, ix + pw)
                if dy1 <= dy0 or dx1 <= dx0:
                    continue
                src = p["h"][dy0 - iy:dy1 - iy, dx0 - ix:dx1 - ix]
                np.maximum(hot[dy0:dy1, dx0:dx1], src, out=hot[dy0:dy1, dx0:dx1])
            field = hot
        else:
            field = heat

        # The head's bloom is drawn, not stored: it belongs to the beam, not to
        # the material, so it must not smear along the trail.
        px, py = int(round(pos[0])), int(round(pos[1]))
        if cutting:
            if field is heat:
                np.copyto(hot, heat)
                field = hot
            y0, y1 = max(0, py - br), min(H, py + br + 1)
            x0, x1 = max(0, px - br), min(W, px + br + 1)
            if y1 > y0 and x1 > x0:
                b = bloom[y0 - py + br:y1 - py + br, x0 - px + br:x1 - px + br]
                np.maximum(field[y0:y1, x0:x1], b, out=field[y0:y1, x0:x1])

        # Heat -> pixels. np.take straight into the output buffer rather than
        # lut[idx]: fancy indexing allocates and is four times slower, and this
        # gather is the single biggest cost in the frame.
        np.multiply(field, f32(LUT_N - 1), out=scaled)
        np.copyto(index, scaled, casting="unsafe")
        np.take(lut, index, axis=0, out=frame_rgb)

        if grain_yx is not None:
            frame_rgb[grain_yx] = np.maximum(frame_rgb[grain_yx], grain_rgb)

        if args.smoke:
            live = np.flatnonzero(slife > 0.0)
            if live.size:
                iy = np.rint(sy[live]).astype(np.int32)
                ix = np.rint(sx[live]).astype(np.int32)
                ok = (iy >= 0) & (iy < H) & (ix >= 0) & (ix < W)
                if ok.any():
                    iy, ix = iy[ok], ix[ok]
                    frac = slife[live][ok] / slife0[live][ok]
                    # Fade in as it leaves the kerf and out as it thins.
                    a = np.clip(np.minimum(frac * 2.2, (1.0 - frac) * 6.0), 0, 1)
                    c = (a[:, None] * SMOKE_RGB).astype(np.uint8)
                    frame_rgb[iy, ix] = np.maximum(frame_rgb[iy, ix], c)

        if not cutting and 0 <= px < W and 0 <= py < H:
            # Rapid traverse: beam off, just the pilot dot moving dark.
            np.maximum(frame_rgb[py, px], PILOT_RGB, out=frame_rgb[py, px])

        return frame_rgb

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
