#!/usr/bin/env python3
"""A 3-axis CNC mill cutting a part, seen from straight above.

A billet of aluminium is clamped to the table. The spindle drops in and
adaptive-clears a big pocket with trochoidal loops, drills through the island
it leaves behind, finishes the floor with a raster pass, contours the walls,
engraves a name into the boss, and then the finished part slides off the
pallet and a fresh blank comes in.

Everything is one array: **a height Z per panel pixel**, initialised to the
top of the stock. The endmill is a disc, and cutting is

    Z[disc footprint] = minimum(Z, tool_bottom)

which is not a model of milling, it *is* milling -- a min-composite of the
tool swept along the path is the exact definition of the machined surface.
Nothing here draws the part. The part is whatever is left of the field.

Everything visible falls out of that. Shading is the **gradient of Z**: a
shifted difference in each axis, dotted with a light direction, on top of a
depth term. So the pocket walls, the raised boss, the through-holes and the
individual tool marks all appear as a consequence of the representation
rather than as things that had to be drawn. In particular the roughing pass
cuts each trochoidal loop a few hundredths of a millimetre off the others, so
the loops stay *in the surface* as scallops after the tool has gone -- and the
finishing raster, which cuts 0.2 mm lower, erases them, because a lower
minimum wins. That is the arc of the whole cycle and it costs one line:
chaotic loops, then a disciplined raster that wipes them out, and half the
floor smooth while the other half is still a field of loops.

This is the opposite of printer.py in both senses -- subtractive, and seen
from above -- and it is the same trick lathe.py gets from storing a radius per
column.

Purity
------
The whole toolpath is generated once in `build()` from the seed: position,
tool-bottom Z, feed rate and operation for every sample along it, plus the
cumulative time at each sample. `render(t)` looks up where on that path the
tool is and advances a cursor, stamping the samples it crossed. Z only ever
decreases, and stamping a sample twice is a no-op, so the field at time t
depends only on t. If t goes backwards, or lands in a different cycle, the
field is reset and replayed -- which is what makes a cold `render(t0)` equal
to the same t0 reached frame by frame. Chips are ballistic from a birth
sample rather than integrated, so they are pure and frame-rate independent
too.

Cost
----
Two whole-panel operations a frame: the table copy and the stock blit. The
shading is only recomputed over the bounding box the tool actually touched
since the last frame, which is a couple of hundred pixels, so a frame is
about thirty small numpy calls rather than a dozen big ones.

Run:  python3 cnc.py --host 127.0.0.1
      python3 cnc.py --speed 1.6 --text 'MADE HERE'
      python3 cnc.py --stepover 1.2 --no-chips
"""

import sys

import numpy as np

import defcon
import demoscene as ds
import ftsite

f32 = np.float32
TAU = 2.0 * np.pi

# Sentinel height for "the tool is not here". Anything far above the stock
# works; it just has to survive a np.minimum without ever winning.
BIG = f32(1e4)


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, propagation, sort and tide
# draw with: five rows a glyph, each row an octal digit whose three bits are
# the three columns. It is imported read-only. A real typeface is mush at
# five pixels, and the readout here is a machine DRO, which wants a fixed
# pitch anyway.
# --------------------------------------------------------------------------

_GLYPHS = {}
for _ch, _rows in defcon._FONT.items():
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


# --------------------------------------------------------------------------
# The material, as a luminance ramp. One scalar per pixel goes through this,
# which is both cheaper than mixing RGB per pixel and much easier to tune.
# Aluminium is a cool grey that goes almost white where a wall catches the
# light; the dark end has to stay slightly blue or the shadowed side of every
# wall reads as a hole.
# --------------------------------------------------------------------------

ALU = [(0.00, (5, 6, 9)), (0.14, (22, 26, 34)), (0.34, (60, 68, 82)),
       (0.58, (112, 122, 140)), (0.80, (170, 181, 200)),
       (0.93, (214, 224, 242)), (1.00, (246, 250, 255))]

# The DRO. Amber on black, because it has to sit on the table below the work
# and never be mistaken for part of the machining.
DRO = (255, 168, 44)
DRO_DIM = (120, 74, 16)

# Operation labels. The index into this is carried per path sample.
OP_ADAPTIVE, OP_DRILL, OP_FINISH, OP_CONTOUR, OP_ENGRAVE = range(5)


# --------------------------------------------------------------------------
# Geometry helpers.
# --------------------------------------------------------------------------

def _rr_frame(a, b, rn, step):
    """Walk the boundary of a rounded rectangle once, in screen coords.

    A rounded rectangle is the Minkowski sum of a plain rectangle (the *core*,
    half extents a and b) with a disc. Offsetting it inward only shrinks the
    disc -- the core never moves. So one traversal of the core, carrying an
    outward normal, generates the whole family of offset curves as
    `core + normal * r`, which is exactly what an inward spiral needs: vary r
    as you go round and you have a spiral, with no geometry work per turn.

    Returns (cx, cy, nx, ny, tx, ty), each (M,), sampled at roughly `step`
    apart measured on the curve at radius `rn`.
    """
    prims = [("e", (-a, -b), (a, -b), (0.0, -1.0)),
             ("c", (a, -b), -0.5 * np.pi, 0.0),
             ("e", (a, -b), (a, b), (1.0, 0.0)),
             ("c", (a, b), 0.0, 0.5 * np.pi),
             ("e", (a, b), (-a, b), (0.0, 1.0)),
             ("c", (-a, b), 0.5 * np.pi, np.pi),
             ("e", (-a, b), (-a, -b), (-1.0, 0.0)),
             ("c", (-a, -b), np.pi, 1.5 * np.pi)]
    cx, cy, nx, ny, tx, ty = [], [], [], [], [], []
    for p in prims:
        if p[0] == "e":
            _, p0, p1, nrm = p
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            length = float(np.hypot(dx, dy))
            n = max(1, int(round(length / step)))
            u = np.arange(n, dtype=f32) / f32(n)
            cx.append(p0[0] + dx * u)
            cy.append(p0[1] + dy * u)
            nx.append(np.full(n, nrm[0], f32))
            ny.append(np.full(n, nrm[1], f32))
            tx.append(np.full(n, dx / max(length, 1e-6), f32))
            ty.append(np.full(n, dy / max(length, 1e-6), f32))
        else:
            _, c, a0, a1 = p
            n = max(1, int(round(abs(a1 - a0) * rn / step)))
            th = (a0 + (a1 - a0) * (np.arange(n, dtype=f32) / f32(n))).astype(f32)
            cx.append(np.full(n, c[0], f32))
            cy.append(np.full(n, c[1], f32))
            nx.append(np.cos(th))
            ny.append(np.sin(th))
            tx.append(-np.sin(th))
            ty.append(np.cos(th))
    return tuple(np.concatenate(v).astype(f32)
                 for v in (cx, cy, nx, ny, tx, ty))


def _disc(radius):
    """A tool footprint: 0 inside the disc, BIG outside, plus its half-width.

    Adding the tool-bottom Z to this and taking a minimum against the field is
    one flat-bottomed endmill sitting at one place.
    """
    R = max(1, int(np.ceil(radius)))
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
    d = np.hypot(yy.astype(f32), xx.astype(f32))
    s = np.full(d.shape, BIG, f32)
    s[d <= radius] = 0.0
    return s, R


def _runs(row):
    """Spans of consecutive True in a 1-D boolean array, as (start, stop)."""
    idx = np.flatnonzero(row)
    if idx.size == 0:
        return []
    brk = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate([[0], brk + 1])
    stops = np.concatenate([brk + 1, [idx.size]])
    return [(int(idx[a]), int(idx[b - 1]) + 1) for a, b in zip(starts, stops)]


# --------------------------------------------------------------------------
# The toolpath, built once. This is the whole program: a list of samples, each
# with a position, a tool-bottom Z, which tool is in the spindle, whether it
# is cutting, and how fast it is moving. render() only ever walks along it.
# --------------------------------------------------------------------------

class _Program(object):

    def __init__(self, z_safe, mm2px):
        self.z_safe = float(z_safe)
        self.mm2px = float(mm2px)      # how much a mm of Z counts as travel
        self._x, self._y, self._z = [], [], []
        self._tool, self._cut, self._op, self._spd, self._lab = [], [], [], [], []
        self.labels = []
        self._lab_ix = {}
        self.x = self.y = 0.0
        self.z = float(z_safe)

    def _label(self, s):
        if s not in self._lab_ix:
            self._lab_ix[s] = len(self.labels)
            self.labels.append(s)
        return self._lab_ix[s]

    def _emit(self, xs, ys, zs, tool, cut, op, spd, lab):
        n = xs.size
        if n == 0:
            return
        self._x.append(xs.astype(f32))
        self._y.append(ys.astype(f32))
        self._z.append(zs.astype(f32))
        self._tool.append(np.full(n, tool, np.int8))
        self._cut.append(np.full(n, cut, bool))
        self._op.append(np.full(n, op, np.int8))
        self._spd.append(np.full(n, spd, f32))
        self._lab.append(np.full(n, lab, np.int16))
        self.x, self.y, self.z = float(xs[-1]), float(ys[-1]), float(zs[-1])

    # -- moves ------------------------------------------------------------

    def rapid(self, x, y, op, lab, spd, step=3.0, up=None):
        """Lift to a clearance plane, traverse, and stay there. Never cuts.

        `up` is how high to lift. Engraving passes a plane a millimetre over
        the work rather than the full safe height: a hundred and twenty tiny
        letter strokes each retracting to +2 mm is most of a minute of nothing
        happening, which is the sort of thing that only shows up in the total.
        """
        top = self.z_safe if up is None else float(up)
        if self.z < top - 1e-6:
            n = max(2, int(round((top - self.z) * self.mm2px / step)))
            u = (np.arange(1, n + 1, dtype=f32) / f32(n))
            self._emit(np.full(n, self.x, f32), np.full(n, self.y, f32),
                       self.z + (top - self.z) * u,
                       0, False, op, spd, lab)
        d = float(np.hypot(x - self.x, y - self.y))
        n = max(1, int(round(d / step)))
        u = (np.arange(1, n + 1, dtype=f32) / f32(n))
        self._emit(self.x + (x - self.x) * u, self.y + (y - self.y) * u,
                   np.full(n, self.z, f32), 0, False, op, spd, lab)

    def plunge(self, z, tool, op, lab, spd, step=0.30):
        """Straight down into the work, cutting all the way."""
        n = max(2, int(round(abs(self.z - z) / step)))
        u = (np.arange(1, n + 1, dtype=f32) / f32(n))
        self._emit(np.full(n, self.x, f32), np.full(n, self.y, f32),
                   self.z + (z - self.z) * u, tool, True, op, spd, lab)

    def cut_to(self, x, y, z, tool, op, lab, spd, step=1.4):
        d = float(np.hypot(x - self.x, y - self.y))
        n = max(1, int(round(d / step)))
        u = (np.arange(1, n + 1, dtype=f32) / f32(n))
        self._emit(self.x + (x - self.x) * u, self.y + (y - self.y) * u,
                   self.z + (z - self.z) * u, tool, True, op, spd, lab)

    def cut_path(self, xs, ys, zs, tool, op, lab, spd):
        """A polyline that is already sampled at the right spacing."""
        self._emit(xs, ys, zs, tool, True, op, spd, lab)

    # -- finish -----------------------------------------------------------

    def finish(self):
        cat = np.concatenate
        x = cat(self._x)
        y = cat(self._y)
        z = cat(self._z)
        # Time per sample from a genuine 3-D step length over the commanded
        # feed, so a plunge takes as long as it should and everything is a
        # rate per second rather than per frame.
        dx = np.empty_like(x)
        dy = np.empty_like(y)
        dz = np.empty_like(z)
        dx[0] = dy[0] = dz[0] = 0.0
        dx[1:] = np.diff(x)
        dy[1:] = np.diff(y)
        dz[1:] = np.diff(z) * f32(self.mm2px)
        step = np.sqrt(dx * dx + dy * dy + dz * dz)
        spd = cat(self._spd)
        tt = np.cumsum(step / np.maximum(spd, 1e-3)).astype(f32)
        tt -= tt[0]
        return {"x": x, "y": y, "z": z, "t": tt,
                "tool": cat(self._tool), "cut": cat(self._cut),
                "op": cat(self._op), "lab": cat(self._lab),
                "spd": spd, "dx": dx, "dy": dy, "labels": self.labels}


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=1.0,
                    help="overall rate; the whole program scales with it")
    ap.add_argument("--stepover", type=float, default=1.8,
                    help="radial stepover of the adaptive spiral, px per turn "
                         "-- smaller means more laps and a longer roughing")
    ap.add_argument("--trochoid", type=float, default=2.6,
                    help="radius of the trochoidal loops, px")
    ap.add_argument("--text", default=ftsite.NAME,
                    help="what gets engraved into the boss")
    ap.add_argument("--chips", dest="chips", action="store_true", default=True,
                    help="chips thrown off the cut")
    ap.add_argument("--no-chips", dest="chips", action="store_false")
    ap.add_argument("--seed", type=int, default=7,
                    help="0 picks one at random")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    speed = max(0.15, float(args.speed))

    # ---- layout ----------------------------------------------------------
    # The stock is a wide billet on a wide table, with a strip of table above
    # it and a deeper strip below carrying the readout. 320x64 is very nearly
    # the proportion of a real machine's work envelope seen from above, which
    # is why this subject was picked for the panel.
    sx = max(4, int(round(W * 0.081)))
    sy = max(1, int(round(H * 0.031)))
    bot = max(8, int(round(H * 0.145)))
    SW = W - 2 * sx
    SH = H - sy - bot
    PAD = 6                              # so a stamp never needs clipping
    ZW, ZH = SW + 2 * PAD, SH + 2 * PAD

    hw, hh = SW * 0.5, SH * 0.5          # stock half extents, stock-local

    # ---- the part --------------------------------------------------------
    # Depths in mm. The billet is 6 mm of aluminium; the pocket goes 3.2 deep
    # and the holes go through.
    Z_TOP = 0.0
    Z_ROUGH = -3.02                      # roughing leaves 0.18 on the floor
    Z_FLOOR = -3.20
    Z_HOLE = -7.0
    Z_ENGRAVE = -1.55                    # deep enough that the depth term,
    #                                      not just the relief, reads the type
    Z_SAFE = 2.0
    MM2PX = 3.0                          # 1 mm of Z is 3 px of travel

    WALL = max(4.0, SW * 0.0187)         # stock left outside the pocket
    PR = max(6.0, SH * 0.23)             # pocket corner radius
    PA, PB = hw - WALL, hh - WALL        # pocket half extents
    ca, cb = PA - PR, PB - PR            # its core rectangle

    RT = max(1.6, SH * 0.047)            # 5 mm endmill -> 2.5 px radius
    RD = max(1.0, RT * 0.62)             # 3 mm drill
    RE = 0.75                            # 0.9 mm engraving cutter, 1 px wide
    TROCH = max(1.0, float(args.trochoid))
    STEP = max(0.6, float(args.stepover))

    # The island the spiral leaves behind is not drawn and not decided: it is
    # whatever the innermost loop fails to reach. Choose how big it should be,
    # and the spiral's last inset follows.
    BOSS_IN = min(PB - 3.0, PB - SH * 0.115)     # inset of the boss wall
    d0 = RT + TROCH                              # first guide inset
    dmax = max(d0 + STEP, BOSS_IN - TROCH - RT)
    bossa, bossb = PA - BOSS_IN, PB - BOSS_IN

    tools = [_disc(RT), _disc(RD), _disc(RE)]
    stamps = [t[0] for t in tools]
    stamp_r = [t[1] for t in tools]
    scratch = [np.empty_like(s) for s in stamps]

    # ---- feeds -----------------------------------------------------------
    # px/s for the animation, mm/min for the readout. Adaptive roughing runs
    # fast and shallow-engagement, the finish faster still, the engraver slow.
    F_RAPID, D_RAPID = 320.0 * speed, 0
    F_ADAPT, D_ADAPT = 128.0 * speed, 1800
    F_PLUNGE, D_PLUNGE = 26.0 * speed, 250
    F_FINISH, D_FINISH = 250.0 * speed, 2400
    F_CONTOUR, D_CONTOUR = 195.0 * speed, 1100
    F_ENGRAVE, D_ENGRAVE = 130.0 * speed, 600
    F_EPLUNGE = 330.0 * speed

    p = _Program(Z_SAFE, MM2PX)
    L_ADAPT = p._label("ADAPTIVE Z%.1f" % Z_ROUGH)
    L_DRILL = p._label("DRILL 3.0MM")
    L_FINISH = p._label("FINISH Z%.1f" % Z_FLOOR)
    L_CONTOUR = p._label("CONTOUR 5.0EM")
    L_ENGRAVE = p._label("ENGRAVE 0.9V")
    L_DONE = p._label("PART COMPLETE")
    L_LOAD = p._label("PALLET CHANGE")

    # ---- 1. adaptive clearing -------------------------------------------
    # The guide is the pocket boundary spiralling inward; the tool runs
    # trochoidal loops around it, which is what keeps the engagement constant
    # and is the whole reason modern roughing looks like this.
    gstep = 0.85
    CX, CY, NX, NY, TX, TY = _rr_frame(ca, cb, PR - 0.5 * (d0 + dmax), gstep)
    M = CX.size
    turns = max(1.0, (dmax - d0) / STEP)
    n = int(turns * M)
    ii = np.arange(n, dtype=f32)
    u = (np.arange(n) % M)
    prog = ii / f32(max(n - 1, 1))
    r = f32(PR) - (f32(d0) + (f32(dmax) - f32(d0)) * prog)
    gx = np.take(CX, u) + np.take(NX, u) * r
    gy = np.take(CY, u) + np.take(NY, u) * r
    # One loop every LOOP_ADV px of guide. It must advance less than the band
    # the loop sweeps (tool diameter plus twice the loop radius) or the spiral
    # leaves uncut ribs between the loops.
    LOOP_ADV = min(2.0 * (RT + TROCH) * 0.85, 9.0)
    phi = ii * f32(TAU * gstep / LOOP_ADV)
    cs, sn = np.cos(phi).astype(f32), np.sin(phi).astype(f32)
    ax = gx + TROCH * (cs * np.take(TX, u) + sn * np.take(NX, u))
    ay = gy + TROCH * (cs * np.take(TY, u) + sn * np.take(NY, u))
    # The roughing Z carries a small *positive* offset, so the floor it leaves
    # is not flat and keeps the loops in it after the tool has gone. This is
    # the line that makes the whole finishing pass mean something.
    #
    # The offset has to vary per *loop*, not per sample. White noise along the
    # path is invisible: the field is a minimum over a five pixel disc, which
    # takes the deepest of a dozen neighbouring samples and averages the noise
    # away to almost nothing -- measured, a per-sample jitter of 0.075 mm left
    # a floor with a standard deviation of 0.011 mm, which is three levels of
    # brightness. One offset per trochoidal loop survives the minimum, because
    # a whole loop's worth of samples agrees, and it leaves the floor patched
    # in loop-shaped scallops.
    loop_len = max(4, int(round(LOOP_ADV / gstep)))
    loop_id = (np.arange(n) // loop_len)
    az = (f32(Z_ROUGH) + rng.uniform(0.0, 0.30, loop_id.max() + 1
                                     ).astype(f32)[loop_id]
          + np.abs(rng.standard_normal(n).astype(f32)) * f32(0.018))
    # Ramp in over the first stretch rather than plunging: an open-ended entry
    # is what a real adaptive does, and a vertical plunge with a 5 mm endmill
    # would be a lie.
    ramp = min(n // 3, int(80 / gstep))
    az[:ramp] += (1.0 - np.arange(ramp, dtype=f32) / f32(ramp)) * f32(-Z_ROUGH + 0.4)

    p.rapid(float(ax[0]), float(ay[0]), OP_ADAPTIVE, L_ADAPT, F_RAPID)
    p.cut_path(ax, ay, az, 0, OP_ADAPTIVE, L_ADAPT, F_ADAPT)
    p.rapid(float(ax[-1]), float(ay[-1]), OP_ADAPTIVE, L_ADAPT, F_RAPID)

    # ---- 2. drilling -----------------------------------------------------
    holes = [(-bossa * 0.79, 0.0), (bossa * 0.79, 0.0),
             (-PA * 0.30, PB * 0.66), (PA * 0.30, -PB * 0.66)]
    for hx_, hy_ in holes:
        p.rapid(hx_, hy_, OP_DRILL, L_DRILL, F_RAPID)
        p.plunge(Z_HOLE, 1, OP_DRILL, L_DRILL, F_PLUNGE)

    # ---- 3. finishing raster --------------------------------------------
    # Parallel lanes at the exact floor depth. Because a lower minimum always
    # wins, this flattens everything the roughing left behind; because the
    # lanes carry a much smaller jitter of their own, the surface keeps a
    # faint parallel grain instead of going dead flat.
    def x_limit(yy):
        """How far in x the tool centre may go at this y, inside the pocket."""
        rr = PR - RT
        dy = abs(yy) - cb
        if dy <= 0.0:
            return ca + rr
        if dy >= rr:
            return 0.0
        return ca + float(np.sqrt(rr * rr - dy * dy))

    lane_y = []
    y_out = PB - RT
    y_in = bossb + RT
    nl = max(2, int(np.ceil((y_out - y_in) / (2.0 * RT * 0.72))))
    for k in range(nl + 1):
        lane_y.append(-(y_in + (y_out - y_in) * k / float(nl)))
    for k in range(nl + 1):
        lane_y.append(y_in + (y_out - y_in) * (nl - k) / float(nl))
    d = 1
    for yy in lane_y:
        xl = x_limit(yy)
        if xl < 2.0:
            continue
        zs_lane = Z_FLOOR + 0.022 * abs(float(rng.standard_normal()))
        p.rapid(-d * xl, yy, OP_FINISH, L_FINISH, F_RAPID)
        # Plunge to depth *before* traversing. Letting cut_to ramp the Z down
        # over the length of the lane looks plausible on paper and leaves a
        # wedge of uncut stock at the start of every lane -- which is exactly
        # what the height field showed the first time this ran.
        p.plunge(zs_lane, 0, OP_FINISH, L_FINISH, F_PLUNGE * 4.0)
        p.cut_to(d * xl, yy, zs_lane, 0, OP_FINISH, L_FINISH, F_FINISH)
        d = -d
    # The two ends, left and right of the boss, in short vertical lanes.
    for side in (-1, 1):
        x_end = side * (bossa + RT)
        x_far = side * (ca + PR - RT)
        nx_ = max(1, int(np.ceil(abs(x_far - x_end) / (2.0 * RT * 0.72))))
        dd = 1
        for k in range(nx_ + 1):
            xx = x_end + (x_far - x_end) * k / float(nx_)
            yl = bossb + RT if abs(xx) > bossa else 0.0
            yl = min(PB - RT, max(yl, 1.0))
            zs_lane = Z_FLOOR + 0.022 * abs(float(rng.standard_normal()))
            p.rapid(xx, -dd * yl, OP_FINISH, L_FINISH, F_RAPID)
            p.plunge(zs_lane, 0, OP_FINISH, L_FINISH, F_PLUNGE * 4.0)
            p.cut_to(xx, dd * yl, zs_lane, 0, OP_FINISH, L_FINISH, F_FINISH)
            dd = -dd

    # ---- 4. contour ------------------------------------------------------
    # One lap round the boss and one round the pocket wall, both offset by the
    # tool radius, which is what leaves a clean vertical wall where the
    # roughing left scallops.
    for (aa, bb, rr) in ((bossa, bossb, RT), (ca, cb, PR - RT)):
        fx, fy, fnx, fny, _, _ = _rr_frame(aa, bb, rr, 1.4)
        px_ = fx + fnx * rr
        py_ = fy + fny * rr
        px_ = np.concatenate([px_, px_[:1]])
        py_ = np.concatenate([py_, py_[:1]])
        p.rapid(float(px_[0]), float(py_[0]), OP_CONTOUR, L_CONTOUR, F_RAPID)
        p.plunge(Z_FLOOR, 0, OP_CONTOUR, L_CONTOUR, F_PLUNGE)
        p.cut_path(px_, py_, np.full(px_.size, Z_FLOOR, f32),
                   0, OP_CONTOUR, L_CONTOUR, F_CONTOUR)

    # ---- 5. engraving ----------------------------------------------------
    # The name of the shop, milled into the top of the boss with a tiny
    # cutter. The glyph bitmap is the toolpath: each row of it becomes spans
    # the engraver traces, with a rapid across the gaps between letters.
    tscale = 2 if bossb * 2.0 >= 11 else 1
    tm = text_mask(args.text, tscale)
    if tm.shape[1] > 2.0 * bossa - 6:
        tm = text_mask(args.text, 1)
    th, tw = tm.shape
    tx0 = -0.5 * tw
    ty0 = -0.5 * th
    for row in range(th):
        spans = _runs(tm[row])
        if row % 2:
            spans = spans[::-1]
        yy = ty0 + row + 0.5
        for (c0, c1) in spans:
            xa, xb = tx0 + c0 + 0.5, tx0 + c1 - 0.5
            if row % 2:
                xa, xb = xb, xa
            p.rapid(xa, yy, OP_ENGRAVE, L_ENGRAVE, F_RAPID, step=2.0,
                    up=Z_TOP + 0.5)
            p.plunge(Z_ENGRAVE, 2, OP_ENGRAVE, L_ENGRAVE, F_EPLUNGE)
            if abs(xb - xa) > 0.01:
                p.cut_to(xb, yy, Z_ENGRAVE, 2, OP_ENGRAVE, L_ENGRAVE,
                         F_ENGRAVE, step=1.0)
    p.rapid(0.0, -PB - WALL * 0.5, OP_ENGRAVE, L_ENGRAVE, F_RAPID)

    prog_ = p.finish()
    N = prog_["t"].size
    T_CUT = float(prog_["t"][-1])
    T_DWELL = 1.6 / speed
    # The pallet change is one move, not two: the finished part leaving and
    # the next blank arriving are driven off the same eased parameter, a fixed
    # gap apart, so the bed is never empty for long. Easing them separately
    # left two seconds of bare table, which reads as the demo having ended.
    T_EJ0 = T_CUT + T_DWELL
    T_CHANGE = 2.9 / speed
    CYCLE = T_EJ0 + T_CHANGE
    GAP = max(16, sx)

    PX, PY = prog_["x"], prog_["y"]
    PZ, PT = prog_["z"], prog_["t"]
    PCUT, PTOOL = prog_["cut"], prog_["tool"]
    POP, PLAB, PSPD = prog_["op"], prog_["lab"], prog_["spd"]
    labels = prog_["labels"]
    # mm/min for the readout, keyed off the px/s the sample was given.
    disp_for = {round(F_RAPID, 3): D_RAPID, round(F_ADAPT, 3): D_ADAPT,
                round(F_PLUNGE, 3): D_PLUNGE, round(F_EPLUNGE, 3): D_ENGRAVE,
                round(F_FINISH, 3): D_FINISH, round(F_CONTOUR, 3): D_CONTOUR,
                round(F_ENGRAVE, 3): D_ENGRAVE}
    PFEED = np.array([disp_for.get(round(float(s), 3), D_ADAPT) for s in PSPD],
                     np.int32)

    # ---- chips -----------------------------------------------------------
    # One chip per cutting sample, drawn once here and thereafter ballistic
    # from the sample's own time. Nothing is integrated, so a chip is a pure
    # function of t and looks identical at 8 fps and at 30.
    heading = np.arctan2(prog_["dy"], prog_["dx"]).astype(f32)
    spray = rng.uniform(-2.5, 2.5, N).astype(f32)
    cspd = rng.uniform(50.0, 160.0, N).astype(f32) * f32(max(0.6, speed))
    CVX = np.cos(heading + spray) * cspd
    CVY = np.sin(heading + spray) * cspd
    CLIFE = rng.uniform(0.22, 0.52, N).astype(f32) / f32(max(0.6, speed))
    CHIPW = 96                            # samples of history to consider
    CHIP_TAU = f32(0.18)                  # drag time constant, seconds

    # ---- the height field -----------------------------------------------
    Z = np.empty((ZH, ZW), f32)
    # The blank: a sawn billet, so the top is not perfectly flat. The
    # roughness is tiny in mm but the gradient shading multiplies it up, which
    # is what makes uncut stock read as a different surface from a machined
    # floor without any extra state.
    grain = rng.standard_normal((SH, SW)).astype(f32)
    grain = (grain + np.roll(grain, 1, 1) + np.roll(grain, 1, 0)) * f32(0.33)
    Z0 = np.full((ZH, ZW), -6.0, f32)     # the pad sits at table level, which
    Z0[PAD:PAD + SH, PAD:PAD + SW] = f32(Z_TOP) + grain * f32(0.022)
    #                                     ... gives the billet a lit top edge

    img = np.empty((SH, SW, 3), np.uint8)
    LUT = ds.gradient(ALU, 256, dtype=np.uint8)

    # Shading constants. Two terms, and the balance between them is the whole
    # look. The depth term is what tells a pocket floor from the top of the
    # stock at a glance from three metres, so it gets most of the range: the
    # 3.2 mm pocket is a clear step down, and a through-hole is nearly black.
    # The relief term is the gradient, and it saturates at about a third of a
    # millimetre per pixel -- so a wall pins bright on one side and dark on
    # the other, while a few hundredths of surface roughness still shows as a
    # readable tool mark.
    Z_RANGE = 7.0
    KDEPTH = f32(255.0 * 0.66 / Z_RANGE)
    CDEPTH = f32(255.0 * 0.72)
    KREL = f32(255.0 * 0.28)
    GSAT = 1.0 / 0.35
    GX = f32(-GSAT * 0.80)                # light from the upper left
    GY = f32(-GSAT * 0.60)

    sc_a = np.empty((SH, SW), f32)
    sc_b = np.empty((SH, SW), f32)

    def shade(y0, y1, x0, x1):
        """Re-derive the picture from Z over one box. Padded coordinates in."""
        h, w = y1 - y0, x1 - x0
        a = sc_a[:h, :w]
        b = sc_b[:h, :w]
        np.subtract(Z[y0:y1, x0 + 1:x1 + 1], Z[y0:y1, x0 - 1:x1 - 1], out=a)
        np.subtract(Z[y0 + 1:y1 + 1, x0:x1], Z[y0 - 1:y1 - 1, x0:x1], out=b)
        a *= GX
        b *= GY
        a += b
        np.clip(a, -1.0, 1.0, out=a)
        a *= KREL
        np.multiply(Z[y0:y1, x0:x1], KDEPTH, out=b)
        a += b
        a += CDEPTH
        np.clip(a, 0.0, 255.0, out=a)
        np.take(LUT, a.astype(np.int32), axis=0, mode="clip",
                out=img[y0 - PAD:y1 - PAD, x0 - PAD:x1 - PAD])

    blank = np.empty((SH, SW, 3), np.uint8)

    # ---- static table ----------------------------------------------------
    bg = _table(W, H, sx, sy, SW, SH, bot, rng)

    # ---- tool sprites ----------------------------------------------------
    tool_rgb, tool_msk, shadow_msk, TR = _tool_sprites(RT)
    NPH = len(tool_rgb)
    mist = _mist_stamp(RT)
    MR = mist.shape[0] // 2

    # ---- readout ---------------------------------------------------------
    lab_masks = [text_mask(s) for s in labels]
    feed_masks = {}
    for v in set(PFEED.tolist()):
        feed_masks[int(v)] = text_mask("RAPID" if v == 0 else "F%d" % v)
    feed_masks[-1] = text_mask("M30")
    ty_dro = H - bot + 3

    # ---- state -----------------------------------------------------------
    st = {"i": 0, "tc": -1.0, "ci": -1,
          "d": [ZH, 0, ZW, 0]}            # dirty box, padded coords
    frame = np.empty((H, W, 3), np.uint8)

    def reset():
        np.copyto(Z, Z0)
        st["i"] = 0
        st["d"] = [PAD, PAD + SH, PAD, PAD + SW]

    def stamp(i):
        t_ = PTOOL[i]
        R = stamp_r[t_]
        iy = int(PY[i] + 0.5 + hh) + PAD
        ix = int(PX[i] + 0.5 + hw) + PAD
        y0, y1 = iy - R, iy + R + 1
        x0, x1 = ix - R, ix + R + 1
        if y0 < 0 or x0 < 0 or y1 > ZH or x1 > ZW:
            return
        np.add(stamps[t_], PZ[i], out=scratch[t_])
        sub = Z[y0:y1, x0:x1]
        np.minimum(sub, scratch[t_], out=sub)
        d = st["d"]
        if y0 < d[0]:
            d[0] = y0
        if y1 > d[1]:
            d[1] = y1
        if x0 < d[2]:
            d[2] = x0
        if x1 > d[3]:
            d[3] = x1

    def advance(tc):
        target = int(np.searchsorted(PT, tc, side="right"))
        i = st["i"]
        while i < target:
            if PCUT[i]:
                stamp(i)
            i += 1
        st["i"] = i
        return target

    def blit(dst, y, x, src):
        h, w = src.shape[:2]
        sy0, sx0 = max(0, -y), max(0, -x)
        dy0, dx0 = max(0, y), max(0, x)
        hh_ = min(h - sy0, dst.shape[0] - dy0)
        ww_ = min(w - sx0, dst.shape[1] - dx0)
        if hh_ <= 0 or ww_ <= 0:
            return
        dst[dy0:dy0 + hh_, dx0:dx0 + ww_] = src[sy0:sy0 + hh_, sx0:sx0 + ww_]

    def blit_mask(dst, y, x, m, colour):
        h, w = m.shape
        sy0, sx0 = max(0, -y), max(0, -x)
        dy0, dx0 = max(0, y), max(0, x)
        hh_ = min(h - sy0, dst.shape[0] - dy0)
        ww_ = min(w - sx0, dst.shape[1] - dx0)
        if hh_ <= 0 or ww_ <= 0:
            return
        sub = m[sy0:sy0 + hh_, sx0:sx0 + ww_]
        dst[dy0:dy0 + hh_, dx0:dx0 + ww_][sub] = colour

    # Prime the blank picture once, for the stock that slides in at the end.
    reset()
    shade(PAD, PAD + SH, PAD, PAD + SW)
    np.copyto(blank, img)
    st["tc"] = -1.0

    def render(t, frame_idx):
        ci = int(t // CYCLE)
        tc = t - ci * CYCLE
        # A cold call anywhere, or a jump backwards, replays the program from
        # the start. Z only ever decreases and a stamp is idempotent, so the
        # replayed field is bit-identical to the one reached frame by frame.
        if ci != st["ci"] or tc < st["tc"]:
            reset()
            st["ci"] = ci
        st["tc"] = tc

        cutting = tc <= T_CUT
        target = advance(min(tc, T_CUT))

        d = st["d"]
        if d[1] > d[0]:
            shade(max(PAD, d[0] - 1), min(PAD + SH, d[1] + 1),
                  max(PAD, d[2] - 1), min(PAD + SW, d[3] + 1))
            st["d"] = [ZH, 0, ZW, 0]

        np.copyto(frame, bg)

        # ---- the stock, and the pallet change ---------------------------
        dx = 0
        if tc > T_EJ0:
            u = min(1.0, (tc - T_EJ0) / T_CHANGE)
            u = u * u * (3.0 - 2.0 * u)
            dx = int(round(u * (W - sx + 6)))
        blit(frame, sy, sx + dx, img)
        if dx > 0:
            blit(frame, sy, sx - (SW + GAP) + int(round(u * (SW + GAP))), blank)
        if dx == 0:
            _clamps(frame, sx, sy, SW, SH)

        if cutting or tc <= T_CUT + T_DWELL:
            tcl = min(tc, T_CUT)
            i = min(target, N - 1)
            fx = float(np.interp(tcl, PT, PX))
            fy = float(np.interp(tcl, PT, PY))
            lifted = (not cutting) or (not bool(PCUT[i]))
            _draw_particles(frame, tc, target, PT, PX, PY, CVX, CVY, CLIFE,
                            PCUT, CHIPW, CHIP_TAU, sx, sy, hw, hh,
                            bool(args.chips))
            tix = int(round(fx + hw)) + sx
            tiy = int(round(fy + hh)) + sy
            # Coolant, as a soft cloud around the cutter rather than as
            # particles: at this scale a mist is a glow, not a spray.
            _add_stamp(frame, tiy - MR, tix - MR,
                       mist * (0.55 if lifted else 1.0))
            off = 4 if lifted else 2
            _darken(frame, tiy - TR + off, tix - TR + off, shadow_msk,
                    0.42 if lifted else 0.30)
            ph = int(t * 11.0 * NPH) % NPH
            blit_mask(frame, tiy - TR, tix - TR, tool_msk, 0)
            _add_stamp(frame, tiy - TR, tix - TR, tool_rgb[ph])
            if not lifted:
                _spark(frame, tiy, tix)
            if cutting:
                lab = lab_masks[PLAB[i]]
                feed = feed_masks[int(PFEED[i])]
            else:
                lab, feed = lab_masks[L_DONE], feed_masks[-1]
        else:
            lab, feed = lab_masks[L_LOAD], feed_masks[-1]

        blit_mask(frame, ty_dro, 3, lab, DRO)
        blit_mask(frame, ty_dro, W - 3 - feed.shape[1], feed, DRO)
        # A one-row progress bar for the program, which is the only other
        # thing a machine operator would actually look at.
        w_bar = int(W * min(1.0, tc / CYCLE))
        if w_bar > 0:
            frame[H - 1, :w_bar] = DRO_DIM
            frame[H - 1, max(0, w_bar - 2):w_bar] = DRO
        return frame

    return render


# --------------------------------------------------------------------------
# Small drawing primitives. All of these touch a few hundred pixels at most.
# --------------------------------------------------------------------------

def _add_stamp(dst, y, x, add):
    """Saturating add of a float (h, w, 3) stamp, clipped to the frame."""
    h, w = add.shape[:2]
    sy0, sx0 = max(0, -y), max(0, -x)
    dy0, dx0 = max(0, y), max(0, x)
    hh = min(h - sy0, dst.shape[0] - dy0)
    ww = min(w - sx0, dst.shape[1] - dx0)
    if hh <= 0 or ww <= 0:
        return
    sub = dst[dy0:dy0 + hh, dx0:dx0 + ww]
    a = add[sy0:sy0 + hh, sx0:sx0 + ww].astype(np.uint8)
    np.minimum(sub, 255 - a, out=sub)
    sub += a


def _darken(dst, y, x, msk, k):
    h, w = msk.shape
    sy0, sx0 = max(0, -y), max(0, -x)
    dy0, dx0 = max(0, y), max(0, x)
    hh = min(h - sy0, dst.shape[0] - dy0)
    ww = min(w - sx0, dst.shape[1] - dx0)
    if hh <= 0 or ww <= 0:
        return
    m = msk[sy0:sy0 + hh, sx0:sx0 + ww]
    sub = dst[dy0:dy0 + hh, dx0:dx0 + ww]
    sub[m] = (sub[m].astype(np.float32) * k).astype(np.uint8)


def _spark(dst, y, x):
    """The hot point where the edge is actually engaged."""
    H, W = dst.shape[:2]
    if 0 <= y < H and 0 <= x < W:
        dst[y, x] = (255, 250, 235)


def _draw_particles(dst, tc, target, PT, PX, PY, CVX, CVY, CLIFE, PCUT,
                    win, tau, sx, sy, hw, hh, on):
    """Chips thrown off the cut, as ballistics from their birth sample.

    Nothing is integrated between frames: a chip's whole trajectory is a
    function of how long ago its sample was cut, which is why this survives
    being restarted at t=0 or stepped at a different rate.
    """
    if not on:
        return
    lo = max(0, target - win)
    if target <= lo:
        return
    sl = slice(lo, target)
    cut = PCUT[sl]
    if not cut.any():
        return
    age = tc - PT[sl]
    live = cut & (age >= 0.0) & (age < CLIFE[sl])
    if not live.any():
        return
    age = age[live]
    # Exponential drag, not gravity: seen from above there is no down, and a
    # chip that shoots out and stops reads as swarf rather than as sparks.
    k = tau * (1.0 - np.exp(-age / tau))
    px = PX[sl][live] + CVX[sl][live] * k + hw
    py = PY[sl][live] + CVY[sl][live] * k + hh
    ix = np.rint(px).astype(np.int32) + sx
    iy = np.rint(py).astype(np.int32) + sy
    ok = ((ix >= 0) & (ix < dst.shape[1]) & (iy >= 0) & (iy < dst.shape[0]))
    if not ok.any():
        return
    fade = np.clip(1.0 - age[ok] / CLIFE[sl][live][ok], 0.0, 1.0) ** 0.6
    col = (fade[:, None] * np.array([255.0, 238.0, 206.0], np.float32)
           ).astype(np.uint8)
    iy, ix = iy[ok], ix[ok]
    dst[iy, ix] = np.maximum(dst[iy, ix], col)


def _tool_sprites(rt):
    """The cutter seen end-on: holder, body, and flutes that visibly turn.

    Seen from directly above there is nothing else to say that the spindle is
    running, so the flutes are the only moving part on the panel that is not
    the toolpath itself. Three of them, at eight phases, blitted -- a
    per-frame arctan over even this many pixels is not worth it.
    """
    R = int(np.ceil(rt)) + 4
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1].astype(np.float32)
    d = np.hypot(yy, xx)
    th = np.arctan2(yy, xx).astype(np.float32)
    holder = d <= rt + 3.0
    body = d <= rt + 0.5
    rgbs = []
    for k in range(8):
        ph = k * (TAU / 24.0)
        flute = body & (np.cos(3.0 * (th + ph)) > 0.35)
        img = np.zeros(d.shape + (3,), np.float32)
        img[holder] = (30, 32, 40)
        img[(d <= rt + 3.0) & (d > rt + 1.8)] = (128, 118, 96)   # warm collet
        img[body] = (96, 104, 122)
        img[flute] = (250, 252, 255)
        rgbs.append(img)
    return rgbs, holder, holder, R


def _mist_stamp(rt):
    """Flood coolant around the cutter: a soft cool cloud, not a spray."""
    R = int(np.ceil(rt)) + 6
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1].astype(np.float32)
    g = np.exp(-((np.hypot(yy, xx) / (rt + 3.4)) ** 2))
    g[g < 0.04] = 0.0
    return (g[..., None] * np.array([30.0, 44.0, 58.0], np.float32))


def _table(W, H, sx, sy, SW, SH, bot, rng):
    """The machine table under the work: cast iron, T-slots, a little swarf."""
    bg = np.zeros((H, W, 3), np.uint8)
    base = np.array([15, 16, 20], np.float32)
    img = np.tile(base, (H, W, 1))
    # Speckled cast iron. A flat dark field bands at 8 PWM bits; stipple does
    # not, and is cheaper than dithering the whole panel.
    n = int(W * H * 0.06)
    flat = np.unique(rng.integers(0, H * W, n))
    img[flat // W, flat % W] += rng.uniform(0.0, 9.0, flat.size)[:, None]
    # T-slots run along the long axis, which is why this view fits the panel.
    # Only two strips of table are ever visible past the billet, so the slots
    # go there; the point is that during a pallet change, when the whole panel
    # is table, it is recognisably a machine table and not a void.
    for y in (max(0, sy - 2), sy + SH):
        if 0 <= y < H - 2:
            img[y:y + 2, :] = (6, 7, 9)
            img[y, :] = (40, 44, 54)
            img[y + 2, :] = (28, 30, 37)
    # Pallet rails. They are hidden under the billet the whole time it is
    # being cut, and only show in the gap during a change -- which is exactly
    # when the eye needs something stationary to read the movement against.
    for y in (sy + 9, sy + SH - 10):
        if 0 <= y < H:
            img[y, :] = (36, 39, 47)
            img[y + 1, :] = (13, 14, 18)
    # A dark band under the readout so amber type never sits on speckle.
    img[H - bot + 2:H, :] *= 0.40
    return np.clip(img, 0, 255).astype(np.uint8)


def _clamps(dst, sx, sy, SW, SH):
    """Toe clamps holding the billet down at its four corners."""
    H, W = dst.shape[:2]
    for cy in (sy + 1, sy + SH - 4):
        for cx in (sx + 1, sx + SW - 7):
            y0, x0 = max(0, cy), max(0, cx)
            y1, x1 = min(H, cy + 3), min(W, cx + 6)
            if y1 <= y0 or x1 <= x0:
                continue
            dst[y0:y1, x0:x1] = (58, 62, 74)
            dst[y0, x0:x1] = (126, 134, 152)
            dst[y0:y1, x1 - 1] = (30, 32, 40)


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
