#!/usr/bin/env python3
"""A pen plotter drawing, stroke by stroke.

A carriage rides a rail across the top of the panel, an arm hangs off it down
onto the sheet, and a pen at the end of that arm goes down, draws a path, comes
up, flies to the start of the next one and goes down again. Line art appears
the way it actually appears on an AxiDraw: not revealed, *drawn*, at a real
feed rate, with the machine visibly in the way of its own work.

The one representation
----------------------
Everything on the sheet is **a list of paths, each a polyline in sheet
coordinates**, and the whole plot is one *tour* over them: travel to the start
of a path with the pen up, drop the pen, walk the polyline, lift the pen, travel
to the next. That tour is flattened in build() into one array of moves --
`(x0, y0, x1, y1, kind, t0, t1)`, kind being ink, travel or a servo dwell -- with
cumulative *times*, not lengths, so that the pen-up and pen-down dwells are
first class rather than a fudge. A frame is then a lookup: `searchsorted` the
current time into `t1`, and everything before that index is finished, the move
at that index is in progress, and the pen is somewhere along it.

That makes render() a pure function of `t` by construction, which matters here
because the wall's scheduler builds a segment on a worker thread, starts it at
t=0, and the preview baker steps it at its own rate.

Travel is the point
-------------------
Ink stays on the paper; travel does not. Recent travel moves are drawn as faint
dashed ghosts that decay over about a second and a half, so at any moment you
can see where the pen just came from and where the last few hops went. That
contrast -- permanent ink, evaporating travel -- is the whole idea, and it is
also how plotter people think about a file: minimising travel is the entire
optimisation. Every piece with more than a handful of paths gets a greedy
nearest-endpoint reordering at build time, reversing paths where that helps,
which is exactly what a plotter's own toolpath optimiser does and is why the
ghosts are short hops rather than a cat's cradle.

Ink, purity and the one interesting problem
-------------------------------------------
Anti-aliasing is not optional at this size: a 1 px diagonal on a 64 row panel is
a staircase. Every stroke is laid into a float coverage buffer by evaluating the
exact distance to the segment over its bounding tile, so a diagonal is a smooth
1.1 px line and crossings do not blow out (coverage composites with `maximum`,
not addition).

But an ink buffer is *accumulated* state, and render() may not accumulate. The
resolution is that the buffer is defined as a **pure function of one integer**:
`ink(i)` is "every ink move with index < i, rasterised". It is then *memoised*
rather than accumulated. The cache holds `(i, buffer)`; when a frame asks for
`i' > i` -- the usual case, one or two moves a frame -- the moves in between are
added and the cache advances. When a frame asks for `i' < i`, which is what a
cold start, a loop wrap or a preview baker's rewind looks like, the buffer is
restored from the nearest snapshot below `i'` and walked forward. Snapshots are
taken every 128 moves as the cache passes them, so they cost memory and no work.
Because `maximum` is exact and the moves are always applied in increasing index
order, "restore and walk forward" is *bit identical* to "walk forward from
zero", which is what makes the purity assertion pass rather than nearly pass.
The partially drawn current segment is never put in the buffer at all; it is
stroked into a scratch copy each frame, so there is no quantisation in the tip
of the line either.

The cost model this buys: a frame stroke-rasterises the two or three segments
that were completed since the last frame, plus one partial, plus at most three
ghosts -- not the couple of thousand segments already on the paper.

What it plots
-------------
Five generated pieces, from the plotter-art tradition, cycled in order:

  hilbert  a chain of order-4 Hilbert blocks across the sheet -- one
           continuous stroke, no travel at all, gradually filling the paper
  spiro    hypotrochoids, the spirograph curve, one closed path each
  lissa    a row of Lissajous figures at rising frequency ratios
  flow     a flow field: short curves following a smooth vector field, the
           piece with the most travel and therefore the most ghosting
  truchet  Smith tiles, whose quarter arcs are *chained* through shared edge
           midpoints into a few long continuous strokes, which is the same
           trick a real plotter file uses to avoid a pen lift per arc

Each finishes, holds for a beat so the completed piece can be seen, and is fed
out for a fresh sheet with a new pen colour.

Run:  python3 plotter.py --host 127.0.0.1
      python3 plotter.py --piece hilbert --pen amber
      python3 plotter.py --paper light --speed 220
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * np.pi

PIECES = ("hilbert", "spiro", "lissa", "flow", "truchet")

# Pen colours as they read on the panel rather than as ink names: these have to
# survive being a 1.1 px line at three metres, so they are all near-saturated
# and none of them are dark.
PENS = {
    "cyan":    (60, 226, 255),
    "amber":   (255, 176, 40),
    "magenta": (255, 92, 190),
    "jade":    (90, 240, 150),
    "violet":  (168, 150, 255),
    "red":     (255, 96, 76),
}

# Paper. The wall rule is that thin bright detail on a dark ground survives and
# subtle mid-tone contrast does not, so the default sheet is a dark warm slate
# and the ink glows on it. "light" is the honest white-paper version and is
# there because it is what a plotter actually does; it is legible up close and
# much less so from across a room.
PAPERS = {
    "dark":  ((17, 16, 20), (26, 25, 30)),
    "blue":  ((10, 14, 26), (18, 24, 42)),
    "light": ((196, 190, 176), (228, 223, 210)),
}

STEEL = (128, 132, 142)
STEEL_LIT = (206, 212, 226)
CAST = (44, 44, 50)

# Move kinds.
TRAVEL, INK, DROP, LIFT = 0, 1, 2, 3

SNAP_EVERY = 128            # ink-buffer snapshots, in moves; see the docstring


# --------------------------------------------------------------------------
# A 3x5 pixel font, copied from defcon.py rather than imported, because a demo
# may not reach into another demo. Five rows a glyph, each row an octal digit
# whose three bits are the three columns.
# --------------------------------------------------------------------------

_FONT = {
    "0": "75557", "1": "26227", "2": "71747", "3": "71717", "4": "55711",
    "5": "74717", "6": "74757", "7": "71222", "8": "75757", "9": "75717",
    "A": "25755", "B": "65656", "C": "34443", "D": "65556", "E": "74647",
    "F": "74644", "G": "34553", "H": "55755", "I": "72227", "J": "11152",
    "K": "55655", "L": "44447", "M": "57755", "N": "65555", "O": "25552",
    "P": "65644", "Q": "25573", "R": "65655", "S": "34216", "T": "72222",
    "U": "55557", "V": "55552", "W": "55775", "X": "55255", "Y": "55222",
    "Z": "71247", " ": "00000", "-": "00700", ".": "00002", ":": "02020",
    "/": "11244",
}


def _glyph(ch):
    rows = _FONT.get(ch, _FONT[" "])
    g = np.zeros((5, 3), f32)
    for r, digit in enumerate(rows):
        v = int(digit, 8)
        for c in range(3):
            if v & (4 >> c):
                g[r, c] = 1.0
    return g


def text_mask(s):
    """A (5, 4n-1) float mask for a string; 1 px between glyphs."""
    s = s.upper()
    if not s:
        return np.zeros((5, 1), f32)
    out = np.zeros((5, len(s) * 4 - 1), f32)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _glyph(ch)
    return out


# --------------------------------------------------------------------------
# Strokes.
#
# One segment is rasterised by evaluating the distance from every pixel of its
# bounding tile to the segment and turning that into coverage. It is more
# arithmetic per pixel than a Bresenham walk, but it is a handful of numpy calls
# on a tile of a few dozen pixels instead of a Python loop, it anti-aliases
# exactly, and it round-joins for free -- consecutive segments of a polyline
# leave no notch at the vertex.
# --------------------------------------------------------------------------

def stroke(cov, x0, y0, x1, y1, half, amp=1.0):
    """max-composite an anti-aliased segment into a float coverage buffer."""
    h, w = cov.shape
    pad = half + 1.0
    ax0 = int(np.floor(min(x0, x1) - pad))
    ax1 = int(np.ceil(max(x0, x1) + pad)) + 1
    ay0 = int(np.floor(min(y0, y1) - pad))
    ay1 = int(np.ceil(max(y0, y1) + pad)) + 1
    ax0, ax1 = max(0, ax0), min(w, ax1)
    ay0, ay1 = max(0, ay0), min(h, ay1)
    if ax1 <= ax0 or ay1 <= ay0:
        return
    dx, dy = float(x1 - x0), float(y1 - y0)
    xs = np.arange(ax0, ax1, dtype=f32) - f32(x0)
    ys = (np.arange(ay0, ay1, dtype=f32) - f32(y0))[:, None]
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        d = np.hypot(xs, ys)
    else:
        u = (xs * f32(dx) + ys * f32(dy)) / f32(L2)
        np.clip(u, 0.0, 1.0, out=u)
        ex = xs - u * f32(dx)
        ey = ys - u * f32(dy)
        d = np.hypot(ex, ey)
    c = np.clip(f32(half + 0.5) - d, 0.0, 1.0)
    if amp != 1.0:
        c *= f32(amp)
    tile = cov[ay0:ay1, ax0:ax1]
    np.maximum(tile, c, out=tile)


MAX_DASH = 6


def dashed(cov, x0, y0, x1, y1, half, amp):
    """A dashed line, for travel: dashes are what say "the pen is not down".

    The dash count is capped rather than the dash length being fixed, because
    the per-frame cost of this demo is *number of strokes* and a travel move
    right across the sheet at a fixed 6 px period is thirty of them. Six
    dashes read as a dashed line at any length on a panel this size.
    """
    dx, dy = float(x1 - x0), float(y1 - y0)
    L = float(np.hypot(dx, dy))
    if L < 1.0:
        return
    n = max(1, min(MAX_DASH, int(L / 7.0)))
    period = L / n
    on = period * 0.55
    for k in range(n):
        a = k * period
        b = min(L, a + on)
        stroke(cov, x0 + dx * (a / L), y0 + dy * (a / L),
               x0 + dx * (b / L), y0 + dy * (b / L), half, amp)


# --------------------------------------------------------------------------
# Geometry helpers.
# --------------------------------------------------------------------------

def _shuffled(seq, rng):
    """A shuffled copy.

    Via permutation() rather than rng.shuffle(), which on a plain Python list
    goes down numpy's untyped path and has moved around between versions. The
    wall runs numpy 1.19 and a demo that raises at build time crash-loops the
    scheduler, so this stays on the one call that has meant the same thing
    throughout.
    """
    return [seq[i] for i in rng.permutation(len(seq))]


def resample(xs, ys, step):
    """A polyline resampled to roughly uniform `step` px segments.

    Uniform segments matter twice over: the tour's time is arc length, and the
    per-frame cost is a tile per segment, so a curve sampled densely in its
    parameter rather than in its length would be both slower and unevenly slow.
    """
    xs = np.asarray(xs, f32)
    ys = np.asarray(ys, f32)
    d = np.hypot(np.diff(xs), np.diff(ys))
    s = np.concatenate([[0.0], np.cumsum(d)]).astype(f32)
    total = float(s[-1])
    if total < step:
        return np.stack([xs[[0, -1]], ys[[0, -1]]], axis=1)
    n = max(2, int(round(total / step)) + 1)
    u = np.arange(n, dtype=f32) * f32(total / (n - 1))
    return np.stack([np.interp(u, s, xs), np.interp(u, s, ys)],
                    axis=1).astype(f32)


def optimise(paths, start):
    """Greedy nearest-endpoint ordering, allowed to reverse a path.

    This is the plotter's own problem: the file is a bag of paths and the pen
    has to visit all of them, so the order and the direction are free variables
    and the travel is what you pay. Greedy is not optimal and is what almost
    every real optimiser does anyway; on these pieces it takes the travel from a
    scribble covering the sheet down to short hops between neighbours.
    """
    if len(paths) < 3:
        return list(paths)
    first = np.array([p[0] for p in paths], f32)
    last = np.array([p[-1] for p in paths], f32)
    left = np.ones(len(paths), bool)
    cur = np.array(start, f32)
    out = []
    for _ in range(len(paths)):
        idx = np.flatnonzero(left)
        df = np.hypot(first[idx, 0] - cur[0], first[idx, 1] - cur[1])
        dl = np.hypot(last[idx, 0] - cur[0], last[idx, 1] - cur[1])
        jf = int(np.argmin(df))
        jl = int(np.argmin(dl))
        if dl[jl] < df[jf]:
            k = int(idx[jl])
            p = paths[k][::-1]
        else:
            k = int(idx[jf])
            p = paths[k]
        left[k] = False
        out.append(p)
        cur = p[-1]
    return out


# --------------------------------------------------------------------------
# The pieces. Each returns a list of (n, 2) float arrays in a box `w` by `h`,
# already resampled. Everything is generated from parameters; nothing is traced.
# --------------------------------------------------------------------------

def _hilbert_cells(order):
    """The Hilbert curve of this order as cell coordinates, via d2xy."""
    n = 1 << order
    pts = np.empty((n * n, 2), f32)
    for d in range(n * n):
        x = y = 0
        t = d
        s = 1
        while s < n:
            rx = 1 & (t // 2)
            ry = 1 & (t ^ rx)
            if ry == 0:
                if rx == 1:
                    x = s - 1 - x
                    y = s - 1 - y
                x, y = y, x
            x += s * rx
            y += s * ry
            t //= 4
            s *= 2
        pts[d] = (x, y)
    return pts


def piece_hilbert(w, h, rng, order=4):
    """Order-n Hilbert blocks chained across the sheet -- one single stroke.

    The standard construction enters a block at its bottom-left cell and leaves
    at its bottom-right, so blocks laid side by side join end to end with one
    ordinary step between them and the whole sheet is drawn without ever
    lifting the pen. That is the reason this piece is here: no travel at all,
    a line that just keeps going, and a sheet that fills up rather than being
    filled in.
    """
    n = 1 << order
    cells = _hilbert_cells(order)
    blocks = max(1, int(round(w / float(h))))
    pitch = h / float(n)
    x0 = (w - (blocks * n - 1) * pitch) * 0.5
    xs = np.empty(blocks * n * n, f32)
    ys = np.empty(blocks * n * n, f32)
    for b in range(blocks):
        sl = slice(b * n * n, (b + 1) * n * n)
        xs[sl] = x0 + (cells[:, 0] + b * n) * pitch
        # Cell y counts up from the bottom; the panel counts down.
        ys[sl] = h - pitch * 0.5 - cells[:, 1] * pitch
    return [np.stack([xs, ys], axis=1)]


def piece_spiro(w, h, rng):
    """Hypotrochoids: a small circle rolling inside a big one, pen at radius d.

    Closes after r / gcd(R, r) turns, which is where the petal count comes
    from, so the parameters are picked as integers and the curve is guaranteed
    to shut rather than to nearly shut -- a spirograph that misses its start by
    a pixel looks like a mistake and nothing else.
    """
    # Two families, alternated rather than drawn from one bag: a large pen
    # offset gives the dense woven disc everybody pictures, a small one gives
    # an open rosette, and four of the same kind in a row on one sheet is four
    # green blobs.
    dense = [(31, 8, 22), (29, 12, 25), (33, 7, 19), (25, 9, 24), (27, 11, 20)]
    open_ = [(30, 7, 8), (23, 5, 7), (32, 9, 11), (26, 7, 9), (35, 8, 10)]
    dense = _shuffled(dense, rng)
    open_ = _shuffled(open_, rng)
    combos = [c for pair in zip(dense, open_) for c in pair]
    k = max(2, min(4, int(w / (h * 1.15))))
    rad = h * 0.47
    paths = []
    for i in range(k):
        R, r, d = combos[i % len(combos)]
        cx = w * (i + 0.5) / k
        cy = h * 0.5
        turns = r // _gcd(R, r)
        n = 60 * turns
        tt = np.arange(n + 1, dtype=f32) * f32(TAU * turns / n)
        sc = rad / float(R - r + d)
        x = (R - r) * np.cos(tt) + d * np.cos((R - r) / float(r) * tt)
        y = (R - r) * np.sin(tt) - d * np.sin((R - r) / float(r) * tt)
        paths.append(resample(cx + x * sc, cy + y * sc, 2.2))
    return paths


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def piece_lissa(w, h, rng):
    """A row of Lissajous figures at rising frequency ratios.

    The classic plotter demo sheet, and the reason is that the ratio is legible
    at a glance from the number of lobes -- six little boxes across a 5:1 panel
    is a table of them, which is exactly what these were drawn on paper for.
    """
    ratios = [(1, 2), (2, 3), (3, 4), (3, 5), (4, 5), (5, 6), (5, 7)]
    k = max(3, min(len(ratios), int(round(w / (h * 0.95)))))
    box = w / float(k)
    rad_x = box * 0.40
    rad_y = h * 0.42
    paths = []
    for i in range(k):
        a, b = ratios[i % len(ratios)]
        delta = float(rng.uniform(0.15, 0.55)) * np.pi
        cx = box * (i + 0.5)
        cy = h * 0.5
        n = 90 * max(a, b)
        tt = np.arange(n + 1, dtype=f32) * f32(TAU / n)
        paths.append(resample(cx + rad_x * np.sin(a * tt + delta),
                              cy + rad_y * np.sin(b * tt), 2.0))
    return paths


def piece_flow(w, h, rng, n_seed=54):
    """Streamlines of a smooth vector field.

    The field is three sinusoids summed into an angle, which is enough to look
    like curl noise at this size and costs nothing. All the particles are
    stepped together, so the whole piece is a few dozen numpy calls at build
    time rather than a loop over seeds. It is the piece with the most pen
    lifts, so it is where the travel ghosts do most of the talking.
    """
    a1, a2, a3 = rng.uniform(0.020, 0.055, 3)
    p1, p2, p3 = rng.uniform(0.0, TAU, 3)
    k1, k2, k3 = rng.uniform(1.3, 2.6, 3)
    steps = 26
    step = 2.3

    # Seeds on a jittered grid, so the sheet is covered without the regularity
    # reading as a grid.
    ny = 5
    nx = int(round(n_seed / float(ny)))
    gx, gy = np.meshgrid((np.arange(nx) + 0.5) * (w / nx),
                         (np.arange(ny) + 0.5) * (h / ny))
    px = gx.ravel().astype(f32) + rng.uniform(-w / nx * 0.35, w / nx * 0.35, nx * ny)
    py = gy.ravel().astype(f32) + rng.uniform(-h / ny * 0.35, h / ny * 0.35, nx * ny)
    m = px.size
    xs = np.empty((steps + 1, m), f32)
    ys = np.empty((steps + 1, m), f32)
    xs[0], ys[0] = px, py
    for s in range(steps):
        th = (k1 * np.sin(xs[s] * a1 + p1) + k2 * np.cos(ys[s] * a2 + p2)
              + k3 * np.sin((xs[s] + ys[s]) * a3 + p3))
        xs[s + 1] = xs[s] + step * np.cos(th)
        ys[s + 1] = ys[s] + step * np.sin(th)

    # Each streamline runs for its own number of steps and stops at the edge.
    lens = rng.integers(11, steps + 1, m)
    paths = []
    for j in range(m):
        n = int(lens[j])
        x, y = xs[:n + 1, j], ys[:n + 1, j]
        ok = (x > 0.5) & (x < w - 0.5) & (y > 0.5) & (y < h - 0.5)
        cut = int(np.argmin(ok)) if not ok.all() else n + 1
        if cut < 5:
            continue
        paths.append(np.stack([x[:cut], y[:cut]], axis=1).copy())
    return paths


def piece_truchet(w, h, rng):
    """Smith tiles: two quarter arcs a cell, chained into long strokes.

    Every arc ends at the midpoint of a cell edge, and that midpoint is shared
    with exactly one arc in the neighbouring cell, so the arcs are not 200
    little paths -- they are a handful of long continuous curves and a few
    closed loops, found by walking the graph of shared endpoints. Doing that
    walk is the difference between a plot with two hundred pen lifts and one
    with a dozen, and it is precisely the optimisation a plotter file gets
    before it is sent.
    """
    ny = max(2, int(round(h / 15.0)))
    c = h / float(ny)
    nx = max(2, int(w / c))
    ox = (w - nx * c) * 0.5
    ori = rng.integers(0, 2, (ny, nx))

    arcs = []                      # (centre, r, a0, a1, key_start, key_end)
    for j in range(ny):
        for i in range(nx):
            x0, y0 = ox + i * c, j * c
            r = c * 0.5
            kl, kr = ("v", i, j), ("v", i + 1, j)
            kt, kb = ("h", i, j), ("h", i, j + 1)
            if ori[j, i]:
                arcs.append(((x0 + c, y0), r, np.pi, np.pi * 0.5, kt, kr))
                arcs.append(((x0, y0 + c), r, -np.pi * 0.5, 0.0, kl, kb))
            else:
                arcs.append(((x0, y0), r, np.pi * 0.5, 0.0, kl, kt))
                # pi -> 1.5*pi, not pi -> -0.5*pi: the two are the same angle
                # but linspace takes the long way round through pi/2 and the
                # arc bulges out of the cell and off the sheet. It still looks
                # like a pattern, which is why this survived a screenshot and
                # was caught by asserting no ink lands in the margins.
                arcs.append(((x0 + c, y0 + c), r, np.pi, np.pi * 1.5, kb, kr))

    # The graph: endpoint key -> the arcs that touch it.
    at = {}
    for idx, a in enumerate(arcs):
        at.setdefault(a[4], []).append(idx)
        at.setdefault(a[5], []).append(idx)

    def pts(idx, forward):
        (cxx, cyy), r, a0, a1 = arcs[idx][:4]
        if not forward:
            a0, a1 = a1, a0
        n = 7
        aa = np.linspace(a0, a1, n).astype(f32)
        return np.stack([cxx + r * np.cos(aa), cyy + r * np.sin(aa)], axis=1)

    used = [False] * len(arcs)
    chains = []

    def walk(idx, key):
        """Follow arcs from `idx`, entering it at `key`, until it dead-ends."""
        out = []
        while True:
            used[idx] = True
            forward = (key == arcs[idx][4])
            out.append(pts(idx, forward))
            key = arcs[idx][5] if forward else arcs[idx][4]
            nxt = [k for k in at.get(key, []) if not used[k]]
            if not nxt:
                return out, key
            idx = nxt[0]

    # Open chains first, from the boundary; whatever is left is closed loops.
    order = [k for k, v in at.items() if len(v) == 1] + list(at.keys())
    for key in order:
        for idx in at.get(key, []):
            if used[idx]:
                continue
            segs, _ = walk(idx, key)
            chains.append(np.concatenate(segs, axis=0))
    return [resample(ch[:, 0], ch[:, 1], 2.0) for ch in chains
            if len(ch) > 2]


BUILDERS = {"hilbert": piece_hilbert, "spiro": piece_spiro,
            "lissa": piece_lissa, "flow": piece_flow,
            "truchet": piece_truchet}


# --------------------------------------------------------------------------
# The tour: paths -> one array of timed moves.
# --------------------------------------------------------------------------

def make_tour(paths, park, ink_v, travel_v, t_drop, t_lift):
    """Flatten paths into moves with cumulative times. See the module docstring.

    Returns a dict of parallel arrays. Segment *times* rather than lengths is
    what lets the servo dwells sit in the same sequence as the motion, so
    `searchsorted(t1, now)` answers "what is the machine doing" for every part
    of the cycle including the parts where it is doing nothing.
    """
    x0, y0, x1, y1, kind, dur = [], [], [], [], [], []

    def add(k, a, b, d):
        x0.append(a[0]); y0.append(a[1])
        x1.append(b[0]); y1.append(b[1])
        kind.append(k)
        dur.append(d)

    cur = (float(park[0]), float(park[1]))
    for p in paths:
        a = (float(p[0][0]), float(p[0][1]))
        d = float(np.hypot(a[0] - cur[0], a[1] - cur[1]))
        if d > 0.25:
            add(TRAVEL, cur, a, d / travel_v)
        add(DROP, a, a, t_drop)
        seg = np.hypot(np.diff(p[:, 0]), np.diff(p[:, 1]))
        for i in range(len(p) - 1):
            add(INK, (p[i][0], p[i][1]), (p[i + 1][0], p[i + 1][1]),
                float(seg[i]) / ink_v)
        cur = (float(p[-1][0]), float(p[-1][1]))
        add(LIFT, cur, cur, t_lift)
    d = float(np.hypot(park[0] - cur[0], park[1] - cur[1]))
    if d > 0.25:
        add(TRAVEL, cur, (float(park[0]), float(park[1])), d / travel_v)

    dur = np.array(dur, f32)
    t1 = np.cumsum(dur).astype(f32)
    t0 = (t1 - dur).astype(f32)
    kind = np.array(kind, np.int8)
    ink_i = np.flatnonzero(kind == INK).astype(np.int32)
    trav_i = np.flatnonzero(kind == TRAVEL).astype(np.int32)
    return {
        "x0": np.array(x0, f32), "y0": np.array(y0, f32),
        "x1": np.array(x1, f32), "y1": np.array(y1, f32),
        "kind": kind, "t0": t0, "t1": t1, "dur": np.maximum(dur, 1e-6),
        "n": len(kind), "dur_total": float(t1[-1]) if len(t1) else 0.0,
        "ink_i": ink_i, "trav_i": trav_i,
        "trav_t1": t1[trav_i] if len(trav_i) else np.zeros(0, f32),
        "ink_len": float(np.hypot(np.array(x1, f32) - np.array(x0, f32),
                                  np.array(y1, f32) - np.array(y0, f32))[
                             kind == INK].sum()),
        "travel_len": float(np.hypot(np.array(x1, f32) - np.array(x0, f32),
                                     np.array(y1, f32) - np.array(y0, f32))[
                                kind == TRAVEL].sum()),
    }


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--piece", default="all", choices=list(PIECES) + ["all"],
                    help="what to plot; all cycles through them")
    ap.add_argument("--pen", default="auto",
                    choices=sorted(PENS) + ["auto"],
                    help="pen colour; auto changes it with the sheet")
    ap.add_argument("--paper", default="dark", choices=sorted(PAPERS),
                    help="sheet; light is white paper and is much less legible "
                         "from across the room")
    ap.add_argument("--speed", type=float, default=185.0,
                    help="pen-down feed rate, px/s on a 320 wide panel")
    ap.add_argument("--travel-mult", type=float, default=3.4,
                    help="how much faster the pen moves with the pen up")
    ap.add_argument("--pen-time", type=float, default=0.085,
                    help="seconds the servo takes to raise or lower the pen")
    ap.add_argument("--hold", type=float, default=2.6,
                    help="seconds the finished piece is held before the sheet "
                         "is changed")
    ap.add_argument("--line", type=float, default=1.1,
                    help="stroke width in px")
    ap.add_argument("--rotate", type=int, default=0,
                    help="rotate the running order, so two entries in the "
                         "wall's rotation need not open on the same piece")
    ap.add_argument("--no-ghost", dest="ghost", action="store_false",
                    default=True, help="no faint trails on the travel moves")
    ap.add_argument("--seed", type=int, default=7,
                    help="fixed on purpose: the plots must be reproducible")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    sc = H / 64.0
    sw = W / 320.0

    # ---- layout ----------------------------------------------------------
    rail_h = max(4, int(round(6 * sc)))
    sy0 = rail_h + max(2, int(round(3 * sc)))
    sy1 = H - max(2, int(round(3 * sc)))
    sx0 = max(2, int(round(5 * sw)))
    sx1 = W - sx0
    SH, SW = sy1 - sy0, sx1 - sx0
    inset = max(2, int(round(4 * sc)))
    # A footer band inside the sheet that the artwork never enters, so the
    # signature has clean paper to sit on. The Hilbert piece fills its box
    # edge to edge, and without this the caption is drawn into the middle of it
    # and neither is readable.
    foot = 6 if SH >= 34 else 0
    aw, ah = SW - 2 * inset, SH - 2 * inset - foot
    park = (float(inset * 0.5), float(SH * 0.5))     # pen home, in sheet coords
    half = max(0.35, float(args.line) * 0.5)

    feed_t = 1.0                                     # sheet in and out, seconds
    ghost_tau = 1.4                                  # travel trail half-life-ish

    # ---- the pieces ------------------------------------------------------
    names = list(PIECES) if args.piece == "all" else [args.piece]
    r = int(args.rotate) % len(names)
    names = names[r:] + names[:r]
    pen_names = _shuffled(sorted(PENS), rng)

    pieces = []
    for i, nm in enumerate(names):
        paths = BUILDERS[nm](aw, ah, rng)
        paths = [p + np.array([inset, inset], f32) for p in paths]
        if len(paths) > 2:
            paths = optimise(paths, park)
        tour = make_tour(paths, park, max(8.0, args.speed * sw),
                         max(8.0, args.speed * sw * args.travel_mult),
                         args.pen_time, args.pen_time * 0.8)
        pen = args.pen if args.pen != "auto" else pen_names[i % len(pen_names)]
        tour["name"] = nm
        tour["pen"] = pen
        tour["rgb"] = np.array(PENS[pen], f32)
        tour["label"] = text_mask("%s  %s" % (nm, pen))
        tour["span"] = feed_t + tour["dur_total"] + args.hold + feed_t
        pieces.append(tour)

    starts = np.cumsum([0.0] + [p["span"] for p in pieces]).astype(np.float64)
    cycle = float(starts[-1])
    starts = starts[:-1]

    # ---- the sheet, baked ------------------------------------------------
    lo, hi = PAPERS[args.paper]
    yy = np.arange(SH, dtype=f32)[:, None] / max(1.0, SH - 1.0)
    xx = np.arange(SW, dtype=f32)[None, :] / max(1.0, SW - 1.0)
    # A vignette plus a little tooth. The tooth is the only thing that stops a
    # large flat panel of one value from banding at 8 PWM bits.
    v = (1.0 - 0.55 * ((yy - 0.5) ** 2 * 2.4 + (xx - 0.5) ** 2 * 0.5))
    paper = (np.array(lo, f32)[None, None, :]
             + (np.array(hi, f32) - np.array(lo, f32))[None, None, :] * v[..., None])
    paper += rng.uniform(-2.0, 2.0, (SH, SW, 1)).astype(f32)
    # Sheet edges: lit along the top and left, shadowed along the bottom.
    paper[0, :] *= 1.35
    paper[:, 0] *= 1.2
    paper[-1, :] *= 0.55
    paper[:, -1] *= 0.7
    paper = np.clip(paper, 0, 255).astype(f32)

    # ---- the machine, baked ----------------------------------------------
    bg = np.zeros((H, W, 3), np.uint8)
    bgf = np.zeros((H, W, 3), f32)
    bgf[:] = np.array((7, 7, 9), f32)                # the table
    bgf[sy0 - 1:sy1 + 2, sx0 - 2:sx1 + 3] = np.array((3, 3, 4), f32)  # shadow
    bgf[:rail_h] = np.array(CAST, f32)
    bgf[0] = np.array((92, 94, 102), f32)            # lit top edge of the rail
    bgf[rail_h - 1] = np.array((14, 14, 17), f32)
    gr = rail_h // 2
    bgf[gr:gr + max(1, int(sc)), :] = np.array((20, 20, 24), f32)   # the groove
    for bx in (0, W - max(3, int(4 * sw))):
        bgf[:rail_h, bx:bx + max(3, int(4 * sw))] = np.array((70, 70, 78), f32)
    np.copyto(bg, bgf, casting="unsafe")

    # The carriage: a block that rides the rail, baked once and blitted.
    cw = max(7, int(round(11 * sw))) | 1
    car = np.zeros((rail_h, cw, 3), np.uint8)
    car[:] = np.array((96, 98, 108), np.uint8)
    car[0] = np.array(STEEL_LIT, np.uint8)
    car[-1] = np.array((28, 28, 33), np.uint8)
    car[:, 0] = car[:, -1] = np.array((52, 52, 60), np.uint8)
    car[gr:gr + max(1, int(sc)), 2:cw - 2] = np.array((10, 10, 12), np.uint8)

    # ---- per-frame buffers ------------------------------------------------
    out = np.empty((H, W, 3), np.uint8)
    cov = np.zeros((SH, SW), f32)                    # the memoised ink buffer
    scratch = np.empty((SH, SW), f32)                # ink + the partial segment
    ghost = np.zeros((SH, SW), f32)
    rgbbuf = np.empty((SH, SW, 3), f32)
    ghost_rgb = np.array((116, 126, 150), f32)
    if args.paper == "light":
        ghost_rgb = np.array((150, 146, 138), f32)

    # The ink cache. `cov` holds ink(i) for the piece and index in `cache`;
    # `snaps` are exact copies taken as the index sweeps past a multiple of
    # SNAP_EVERY, so a backwards jump costs at most that many segments.
    cache = {"piece": -1, "i": 0, "snaps": {}}

    def ink_upto(pi, i):
        """Make `cov` equal ink(i) for piece pi. Pure in (pi, i); memoised."""
        P = pieces[pi]
        if cache["piece"] != pi:
            cache["piece"] = pi
            cache["snaps"] = {}
            cache["i"] = 0
            cov[:] = 0.0
        if i < cache["i"]:
            # Backwards: a cold start, a loop wrap, or a preview rewind.
            j = (i // SNAP_EVERY) * SNAP_EVERY
            while j > 0 and j not in cache["snaps"]:
                j -= SNAP_EVERY
            if j and j in cache["snaps"]:
                np.copyto(cov, cache["snaps"][j])
            else:
                j = 0
                cov[:] = 0.0
            cache["i"] = j
        k = cache["i"]
        if k == i:
            return
        x0, y0 = P["x0"], P["y0"]
        x1, y1 = P["x1"], P["y1"]
        kind = P["kind"]
        while k < i:
            if kind[k] == INK:
                stroke(cov, x0[k], y0[k], x1[k], y1[k], half)
            k += 1
            if k % SNAP_EVERY == 0 and k not in cache["snaps"]:
                cache["snaps"][k] = cov.copy()
        cache["i"] = i

    def servo(u):
        """A servo settling on its stop: fast, then a little overshoot."""
        if u <= 0.0:
            return 0.0
        if u >= 1.0:
            return 1.0
        return float(1.0 - np.exp(-6.5 * u) * np.cos(7.5 * u))

    def draw_pen(px, py, lift, rgb, tip_on):
        """Carriage on the rail, the Y arm hanging off it, the pen on the end.

        The arm is drawn over the paper on purpose: a machine that does not
        occlude its own work does not read as a machine.
        """
        ix = int(round(px))
        # Carriage.
        a = ix - cw // 2
        da0, da1 = max(0, a), min(W, a + cw)
        if da1 > da0:
            out[:rail_h, da0:da1] = car[:, da0 - a:da1 - a]
        # The arm, and its shadow on the sheet a couple of px to the right.
        ay = int(round(py))
        if 0 <= ix < W:
            out[rail_h:max(rail_h, ay - 2), ix] = np.array(CAST, np.uint8)
            out[rail_h:max(rail_h, ay - 2), max(0, ix - 1)] = np.array(
                (26, 26, 30), np.uint8)
        # The pen's shadow on the sheet. It sits under the tip when the pen is
        # down and slides away from it as the pen rises, which is the whole of
        # the up/down cue at this size -- three pixels of shadow separating
        # from the tip reads as "lifted" from across the room, and a pen drawn
        # two pixels higher does not.
        sxs = ix + 2 + int(round(lift * 3.0))
        if sy0 <= ay < H:
            a, b = max(0, sxs - 1), min(W, sxs + 2)
            if b > a:
                band = out[ay, a:b].astype(f32)
                band *= 0.45 + 0.25 * lift
                out[ay, a:b] = np.clip(band, 0, 255)
        # The pen: a barrel in the pen's own colour, a steel holder clamped
        # round it, and a tip that either touches the paper or floats above it.
        h_off = int(round(lift * 3.0))
        body = np.array(rgb, f32)
        for k, wid, col in ((6, 1, body * 0.5), (5, 2, np.array(STEEL, f32)),
                            (4, 2, np.array(STEEL_LIT, f32)),
                            (3, 2, np.array(STEEL, f32)),
                            (2, 1, body * 0.85), (1, 1, body)):
            y = ay - h_off - k
            if rail_h <= y < H:
                a, b = max(0, ix - wid), min(W, ix + wid + 1)
                if b > a:
                    out[y, a:b] = np.clip(col, 0, 255).astype(np.uint8)
        y = ay - h_off
        if rail_h <= y < H and 0 <= ix < W:
            out[y, ix] = np.clip(body * (1.7 if tip_on else 0.55),
                                 0, 255).astype(np.uint8)

    def render(t, frame_idx):
        tt = float(t) % cycle
        pi = int(np.searchsorted(starts, tt, side="right")) - 1
        pi = max(0, min(len(pieces) - 1, pi))
        P = pieces[pi]
        lt = tt - float(starts[pi])
        dur = P["dur_total"]

        # ---- where in the sheet's life are we ----------------------------
        shift = 0
        amp = 1.0
        if lt < feed_t:                              # a fresh sheet coming in
            u = lt / feed_t
            u = u * u * (3.0 - 2.0 * u)
            shift = int(round((1.0 - u) * (W - sx0 + 4)))
            ptime = 0.0
        elif lt < feed_t + dur:                      # plotting
            ptime = lt - feed_t
        elif lt < feed_t + dur + args.hold:          # holding the finished piece
            ptime = dur
            # One slow swell of the ink over the whole hold, starting and
            # ending at exactly 1.0 so it is continuous with the drawing on one
            # side and the sheet change on the other. The machine has genuinely
            # stopped here, and a completely frozen panel for three seconds
            # reads as a crashed demo rather than as a finished print.
            k = (lt - feed_t - dur) / max(1e-3, args.hold)
            amp = 1.0 + 0.11 * (1.0 - np.cos(TAU * k)) * 0.5
        else:                                        # and out it goes
            u = (lt - feed_t - dur - args.hold) / feed_t
            u = u * u * (3.0 - 2.0 * u)
            shift = -int(round(u * (sx1 + 4)))
            ptime = dur

        # ---- the machine's state, by lookup ------------------------------
        i = int(np.searchsorted(P["t1"], ptime, side="right"))
        i = min(i, P["n"] - 1)
        u = float((ptime - P["t0"][i]) / P["dur"][i])
        u = min(1.0, max(0.0, u))
        k = int(P["kind"][i])
        px = float(P["x0"][i] + (P["x1"][i] - P["x0"][i]) * u)
        py = float(P["y0"][i] + (P["y1"][i] - P["y0"][i]) * u)
        if k == TRAVEL:
            lift = 1.0
        elif k == INK:
            lift = 0.0
        elif k == DROP:
            lift = max(0.0, 1.0 - servo(u))
        else:
            lift = min(1.35, servo(u))
        if ptime >= dur:                             # parked, pen up
            px, py, lift, k = park[0], park[1], 1.0, TRAVEL

        # ---- ink ----------------------------------------------------------
        ink_upto(pi, i)
        np.copyto(scratch, cov)
        if k == INK and u > 0.0:
            stroke(scratch, P["x0"][i], P["y0"][i], px, py, half)
        if amp != 1.0:
            # np.multiply(out=), not `*=`: an augmented assignment would make
            # `scratch` a local of render() and shadow the buffer built above.
            np.multiply(scratch, f32(amp), out=scratch)
            np.clip(scratch, 0.0, 1.0, out=scratch)

        # ---- travel ghosts -------------------------------------------------
        # The last few travel moves, fading. Found by lookup into the travel
        # moves' end times, so this is three strokes and never a scan.
        if args.ghost and P["trav_i"].size and ptime < dur:
            g = int(np.searchsorted(P["trav_t1"], ptime, side="left"))
            ghost[:] = 0.0
            drew = False
            for j in range(max(0, g - 2), min(P["trav_i"].size, g + 1)):
                m = int(P["trav_i"][j])
                if m > i:
                    break
                age = ptime - float(P["t1"][m])
                a = 1.0 if age < 0.0 else float(np.exp(-age / ghost_tau))
                if a < 0.05:
                    continue
                ex = px if m == i else float(P["x1"][m])
                ey = py if m == i else float(P["y1"][m])
                dashed(ghost, float(P["x0"][m]), float(P["y0"][m]), ex, ey,
                       0.45, 0.55 * a)
                drew = True
        else:
            drew = False

        # ---- compose -------------------------------------------------------
        np.copyto(out, bg)
        d0 = max(0, sx0 + shift)
        d1 = min(W, sx0 + shift + SW)
        if d1 > d0:
            s0 = d0 - (sx0 + shift)
            s1 = s0 + (d1 - d0)
            reg = rgbbuf[:, s0:s1]
            np.subtract(P["rgb"][None, None, :], paper[:, s0:s1], out=reg)
            reg *= scratch[:, s0:s1, None]
            reg += paper[:, s0:s1]
            if drew:
                # Ghosts go on top of the ink, since a travel move that crosses
                # a finished stroke does pass over it.
                gg = ghost[:, s0:s1, None]
                reg += (ghost_rgb[None, None, :] - reg) * gg
            # The signature: the piece's name and the pen, small, in the
            # bottom-left margin, dim while it is being drawn and bright once
            # it is finished -- a plotter print gets signed when it comes off.
            lab = P["label"]
            lh, lw = lab.shape
            ly, lx = SH - inset - lh + 1, inset - 1
            if 0 <= lx and lx + lw <= SW and ly >= 0:
                la = 0.34 if ptime < dur else 0.95
                sub = reg[ly:ly + lh, max(0, lx - s0):max(0, lx - s0) + lw]
                if sub.shape[:2] == (lh, lw):
                    sub += (P["rgb"][None, None, :] - sub) * (lab[..., None] * la)
            np.copyto(out[sy0:sy1, d0:d1], reg, casting="unsafe")

        draw_pen(px + sx0 + shift, py + sy0, lift, P["rgb"], k == INK)
        return out

    render.pieces = pieces
    render.cycle = cycle
    render.cache = cache
    render.layout = {"sx0": sx0, "sy0": sy0, "SW": SW, "SH": SH,
                     "rail_h": rail_h, "feed": feed_t}
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
