#!/usr/bin/env python3
"""Bicycle drivetrain.

A cropped side view of a bike, as if the camera were tracking alongside: a
big spoked rear wheel running off the bottom of the panel, the chainring and
cranks ahead of it, the chain working around both sprockets, and the front
wheel entering at the right edge. The frame tubes run off the top.

The point of the demo is the *wagon-wheel effect*. Thin bright spokes crossing
a discrete pixel grid at a discrete frame rate produce real aliasing, and on a
wall of individually visible LEDs that artifact is the show rather than a
defect. `--sweep` walks the rate slowly up and down through the strobing zone
so the spokes stall, crawl and run backwards while the cranks and the chain
keep turning honestly, which is what makes the illusion legible.

Spokes are laced, not radial. With N spokes and a k-cross pattern the hub
anchor sits k*(720/N) degrees round from the rim anchor -- 90 degrees for the
24-spoke 3-cross default -- so each spoke crosses three others on its way out
and the wheel shows a proper basketweave.

A wheel costs a dozen whole-array operations over the pixels inside its rim,
and never loops over spokes. Every spoke of one flange is tangent to the same
circle of radius d about the hub and differs only in where that tangent point
sits, so for a pixel at (r, a) the tangent angle of the spoke that would pass
exactly through it is `a -+ arccos(d/r)` -- a function of the pixel alone,
baked once at startup. Each frame the angular distance to the nearest real
spoke is that baked value reduced modulo the spoke spacing, and a baked
sqrt(r^2 - d^2) turns the angle into pixels. Brightness goes through a colour
table rather than an RGB multiply, and the two flanges share the table, the
far one landing in its dim end.

Run:  python3 wheel.py --host 127.0.0.1
      python3 wheel.py --speed 5 --sweep 0 --spokes 32 --cross 3
      python3 wheel.py --no-drivetrain --sweep 3 --sweep-period 30
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * np.pi


# --------------------------------------------------------------------------
# Palette. Bright thin structure on true black is what this hardware does
# best, so everything is a light on dark and nothing is a fill.
# --------------------------------------------------------------------------

SPOKE_NEAR = (236, 242, 255)      # near flange, lit
SPOKE_FAR = (104, 116, 148)       # far flange, seen through the wheel
SPOKE_DEPTH = 0.45                # how much dimmer the far flange comes out
RIM = (176, 196, 232)
TYRE = (54, 54, 62)
HUB = (206, 214, 230)
REFLECTOR = (255, 176, 48)
TUBE = (208, 78, 28)              # frame, warm against all that silver
COG = (96, 104, 120)
TOOTH = (140, 150, 172)
CHAIN_PLATE = (128, 128, 132)
CHAIN_ROLLER = (255, 255, 255)
CRANK_NEAR = (224, 230, 242)
CRANK_FAR = (78, 82, 94)
PEDAL = (236, 132, 40)


# --------------------------------------------------------------------------
# Small drawing helpers. Everything that is not a wheel is a few dozen points.
# --------------------------------------------------------------------------

def splat(buf, xs, ys, rgb):
    """Write rgb at rounded (xs, ys), dropping anything off the canvas.

    rgb is one colour, or one per point.
    """
    H, W = buf.shape[:2]
    xi = np.rint(xs).astype(np.int32)
    yi = np.rint(ys).astype(np.int32)
    ok = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
    buf[yi[ok], xi[ok]] = rgb[ok] if np.ndim(rgb) == 2 else rgb


def bake_points(xs, ys, W, H):
    """Round and clip a fixed set of points once, for redrawing every frame."""
    xi = np.rint(xs).astype(np.int32)
    yi = np.rint(ys).astype(np.int32)
    ok = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
    return yi[ok], xi[ok]


def line_points(p0, p1, width=1.0):
    """Points covering a thick segment, as (xs, ys).

    Sampled at half-pixel steps along and across rather than walked with
    Bresenham: the tubes and cranks are short, and this keeps the ends square
    without a special case.
    """
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = float(np.hypot(dx, dy))
    n = max(2, int(length * 2.0) + 1)
    t = np.linspace(0.0, 1.0, n)
    xs = p0[0] + t * dx
    ys = p0[1] + t * dy
    if width <= 1.0 or length < 1e-6:
        return xs, ys
    nx, ny = -dy / length, dx / length
    half = (width - 1.0) * 0.5
    offs = np.linspace(-half, half, max(2, int(round(width * 2))))
    return ((xs[None, :] + nx * offs[:, None]).ravel(),
            (ys[None, :] + ny * offs[:, None]).ravel())


def crank_template(length, width, pedal_len, pedal_w, arm_rgb, pedal_rgb):
    """Bake a crank arm and its pedal; return draw(buf, bb, angle).

    The arm's points are held in the crank's own frame and rotated each frame,
    but the pedal's are held as a fixed offset from the arm's *end*, because a
    pedal hangs level however far round the crank has gone. Both live in one
    array with one colour per point, so the whole crank is a single scatter.
    """
    ax, ay = line_points((0.0, 0.0), (length, 0.0), width)
    px, py = line_points((-pedal_len * 0.5, 0.0), (pedal_len * 0.5, 0.0), pedal_w)
    # Rotated part: the arm's own points, and the arm tip for every pedal point.
    rx = np.concatenate([ax, np.full(px.size, length)]).astype(f32)
    ry = np.concatenate([ay, np.zeros(px.size)]).astype(f32)
    # Fixed part, in screen axes: nothing for the arm, the bar for the pedal.
    fx = np.concatenate([np.zeros(ax.size), px]).astype(f32)
    fy = np.concatenate([np.zeros(ay.size), py]).astype(f32)
    rgb = np.concatenate([np.tile(arm_rgb, (ax.size, 1)),
                          np.tile(pedal_rgb, (px.size, 1))]).astype(np.uint8)
    sx = np.empty(rx.size, f32)
    sy = np.empty(rx.size, f32)
    xi = np.empty(rx.size, np.int32)
    yi = np.empty(rx.size, np.int32)
    reach = length + max(pedal_len * 0.5, pedal_w) + 1.0

    def draw(buf, origin, angle):
        c, s = f32(np.cos(angle)), f32(np.sin(angle))
        np.multiply(rx, c, out=sx)
        np.subtract(sx, ry * s, out=sx)
        np.add(sx, fx + f32(origin[0]), out=sx)
        np.multiply(rx, s, out=sy)
        np.add(sy, ry * c, out=sy)
        np.add(sy, fy + f32(origin[1]), out=sy)
        H, W = buf.shape[:2]
        if (reach <= origin[0] < W - reach) and (reach <= origin[1] < H - reach):
            # The crank cannot leave the canvas from here, so skip the clip.
            np.rint(sx, out=sx)
            np.rint(sy, out=sy)
            np.copyto(xi, sx, casting="unsafe")
            np.copyto(yi, sy, casting="unsafe")
            buf[yi, xi] = rgb
        else:
            splat(buf, sx, sy, rgb)

    return draw


def clip_box(cx, cy, radius, W, H):
    """Integer pixel box covering a disc, intersected with the canvas."""
    x0 = max(0, int(np.floor(cx - radius)) - 1)
    y0 = max(0, int(np.floor(cy - radius)) - 1)
    x1 = min(W, int(np.ceil(cx + radius)) + 2)
    y1 = min(H, int(np.ceil(cy + radius)) + 2)
    return x0, y0, x1, y1


def polar_box(cx, cy, radius, W, H):
    """(box, r, ang) over the pixels of a disc's bounding box."""
    x0, y0, x1, y1 = clip_box(cx, cy, radius, W, H)
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(f32)
    dx = xx - f32(cx)
    dy = yy - f32(cy)
    return (x0, y0, x1, y1), np.hypot(dx, dy), np.arctan2(dy, dx).astype(f32)


def ring(r, lo, hi):
    """Antialiased coverage of the annulus lo..hi, as a 0..1 factor."""
    return np.clip(np.minimum(r - lo, hi - r) + 0.5, 0.0, 1.0).astype(f32)


# --------------------------------------------------------------------------
# The wheel.
# --------------------------------------------------------------------------

def make_wheel(cx, cy, R, spokes, cross, W, H, args, reflector=False):
    """Bake a wheel; return draw(buf, turns).

    `turns` is the wheel's absolute rotation in turns, positive clockwise on
    screen, which is the direction a bike moving right turns.
    """
    r_tyre_o = float(R)
    r_tyre_i = r_tyre_o - max(2.0, 0.10 * R)
    r_rim_o = r_tyre_i
    r_rim_i = r_rim_o - max(1.6, 0.075 * R)
    r_lace = r_rim_i                       # where the spokes meet the rim
    r_flange = max(2.0, 0.19 * R)          # hub flange, where they leave it
    r_hub = max(1.5, 0.135 * R)

    box, r, ang = polar_box(cx, cy, r_tyre_o, W, H)
    x0, y0, x1, y1 = box
    dst = (slice(y0, y1), slice(x0, x1))

    # Static parts: tyre, rim, hub barrel and flange. All rotationally
    # symmetric, so they are baked flat and never recomputed.
    img = (np.array(TYRE, f32) * ring(r, r_tyre_i, r_tyre_o)[..., None]
           + np.array(RIM, f32) * ring(r, r_rim_i, r_rim_o)[..., None]
           + np.array(HUB, f32) * ring(r, -1.0, r_hub)[..., None]
           + np.array(HUB, f32) * 0.55 * ring(r, r_flange - 1.1,
                                              r_flange - 0.2)[..., None])
    img = np.clip(img, 0, 255).astype(np.uint8)

    # Spokes live only inside the rim, so they get their own smaller box, and
    # everything per-frame happens over that box whole and contiguous. Picking
    # out just the flange-to-rim annulus would touch half as many pixels, but
    # putting the result back would then be a scatter, and a scatter costs more
    # than the arithmetic it saves.
    sbox, rs, as_ = polar_box(cx, cy, r_lace, W, H)
    sdst = (slice(sbox[1], sbox[3]), slice(sbox[0], sbox[2]))
    rs = rs.ravel()
    as_ = as_.ravel()
    inside = (rs >= r_flange) & (rs <= r_lace)

    # Lacing. k-cross puts the hub anchor k*(720/N) degrees round from the rim
    # anchor; consecutive spokes alternate which way, and that alternation is
    # what makes them cross rather than fan.
    n = max(4, spokes - (spokes & 1))               # even, so the flanges match
    step = TAU / n                                  # rim hole spacing
    delta = 2.0 * step                              # same-flange spacing
    # Hub-to-rim lead angle. Never exactly zero: at zero the perpendicular foot
    # that the whole scheme is built on collapses onto the hub centre and its
    # angle stops being defined, so --cross 0 is drawn as very nearly radial.
    off = min(max(cross * delta, 0.02), 2.0)

    # Geometry of one spoke, with its rim anchor on the +x axis. Every spoke of
    # that flange is this line rotated, so its distance from the hub centre, d,
    # is shared and only the angle of the perpendicular foot moves.
    ph = np.array([r_flange * np.cos(off), r_flange * np.sin(off)])
    pr = np.array([r_lace, 0.0])
    e = pr - ph
    e /= np.hypot(*e)
    foot = ph - np.dot(ph, e) * e
    d = float(np.hypot(*foot))
    gamma0 = float(np.arctan2(foot[1], foot[0]))

    # A point at (r, a) lies on a tangent to the circle of radius d whose foot
    # is at a - s*arccos(d/r), for s = +1 or -1: the two tangents from that
    # point. One branch is the spoke, the other its mirror through the foot --
    # which is the half of the line that does not exist. Pick the branch by
    # testing the midpoint of a real spoke, and the phantom half is excluded
    # for free.
    mid = 0.5 * (ph + pr)
    acos_mid = np.arccos(np.clip(d / np.hypot(*mid), -1.0, 1.0))
    a_mid = np.arctan2(mid[1], mid[0])
    branch = 1.0 if abs(((a_mid - acos_mid - gamma0 + np.pi) % TAU) - np.pi) < \
        abs(((a_mid + acos_mid - gamma0 + np.pi) % TAU) - np.pi) else -1.0

    acos = np.arccos(np.clip(d / rs, -1.0, 1.0)).astype(f32)
    q_lead = (as_ - branch * acos).astype(f32)
    q_trail = (as_ + branch * acos).astype(f32)
    # d(distance)/d(angle) at this radius: converts an angular miss to pixels.
    gain = np.sqrt(np.maximum(rs * rs - d * d, 0.0)).astype(f32)

    # Brightness profile: a solid core with about a pixel of ramp. A pure ramp
    # from the centre line -- the obvious antialias -- makes every spoke a
    # different shade of grey depending on where it fell between pixel
    # centres, and the wheel reads as a smudge rather than as wire.
    #
    # Brightness is carried as one scalar per pixel and turned into colour by a
    # table lookup, never by multiplying an RGB triple: that keeps two thirds
    # of the arithmetic off the hot path, and the ramp doubles as the depth cue
    # because the far flange, scaled down, lands in the table's cool dim end.
    core = 255.0 * (max(0.30, args.spoke_width * 0.5) + 0.55)
    lut = ds.gradient([(0.0, (0, 0, 0)), (0.20, (24, 28, 40)),
                       (0.45, SPOKE_FAR), (1.0, SPOKE_NEAR)], 256)

    # Everything below is in units of the spoke spacing, because reducing an
    # angle modulo that spacing is then x - rint(x). np.mod is the obvious way
    # to write it and is twenty times dearer than the five cheap operations it
    # replaces -- on this wheel it was a third of the whole frame.
    inv = 1.0 / delta
    q_lead = (q_lead * inv).astype(f32)
    q_trail = (q_trail * inv).astype(f32)

    # Angular miss -> brightness: the derivative of the point-line distance,
    # scaled so the table index falls out directly. Pixels outside the annulus
    # get an offset that no spoke can lift above zero, which masks them without
    # costing a test.
    gain_a = (-255.0 * delta * gain).astype(f32)
    gain_b = (gain_a * SPOKE_DEPTH).astype(f32)
    core_a = np.where(inside, core, -1e6).astype(f32)
    core_b = np.where(inside, core * SPOKE_DEPTH, -1e6).astype(f32)

    n_pix = rs.size
    t1 = np.empty(n_pix, f32)
    t2 = np.empty(n_pix, f32)
    t3 = np.empty(n_pix, f32)
    i8 = np.empty(n_pix, np.uint8)
    spokes_rgb = np.zeros((sbox[3] - sbox[1], sbox[2] - sbox[0], 3), np.uint8)
    spokes_flat = spokes_rgb.reshape(-1, 3)

    ca0 = f32(gamma0 * inv)
    cb0 = f32((-gamma0 + step) * inv)
    psi_inv = f32(TAU * inv)

    # The reflector only ever lives in one thin band of radii, so it is tested
    # over those pixels alone rather than over the whole wheel.
    if reflector:
        ref_sub = np.flatnonzero(np.abs(rs - 0.62 * r_lace) < 1.1)
        ref_a = as_[ref_sub]
        ref_r = rs[ref_sub]
    else:
        ref_sub = None

    def draw(buf, turns):
        psi = f32(turns * psi_inv)
        # Leading flange: perpendicular feet at gamma0 + psi + m*delta.
        np.subtract(q_lead, ca0 + psi, out=t1)
        np.rint(t1, out=t2)
        np.subtract(t1, t2, out=t1)
        np.abs(t1, out=t1)
        np.multiply(t1, gain_a, out=t1)
        np.add(t1, core_a, out=t1)
        # Trailing flange: mirrored lacing, and its rim holes interleave.
        np.subtract(q_trail, cb0 + psi, out=t3)
        np.rint(t3, out=t2)
        np.subtract(t3, t2, out=t3)
        np.abs(t3, out=t3)
        np.multiply(t3, gain_b, out=t3)
        np.add(t3, core_b, out=t3)

        np.maximum(t1, t3, out=t1)
        np.clip(t1, 0.0, 255.0, out=t1)
        np.copyto(i8, t1, casting="unsafe")
        np.take(lut, i8, axis=0, out=spokes_flat)
        if ref_sub is not None:
            # A rim reflector: an honest rotation reference that does not
            # strobe, so the eye can tell a stalled wheel from a stopped one.
            da = np.abs(np.mod(ref_a - turns * TAU - args.reflector_at + np.pi,
                               TAU) - np.pi)
            spokes_flat[ref_sub[da * ref_r < 2.0]] = REFLECTOR
        buf[dst] = img
        np.maximum(buf[sdst], spokes_rgb, out=buf[sdst])

    return draw


# --------------------------------------------------------------------------
# Sprockets: a rear cog on the hub and a chainring with a spider.
# --------------------------------------------------------------------------

def make_sprocket(cx, cy, pitch_r, teeth, W, H, spider=0):
    """Bake a toothed wheel; return draw(buf, turns)."""
    # Tooth height off the chain pitch, not off the radius: teeth are the
    # same size on both sprockets because the chain that meshes them is.
    cp = TAU * pitch_r / max(5, teeth)
    tip = pitch_r + 0.30 * cp
    root = pitch_r - 0.38 * cp
    box, r, ang = polar_box(cx, cy, tip, W, H)
    x0, y0, x1, y1 = box
    bh, bw = y1 - y0, x1 - x0

    sel = (r <= tip + 0.5).ravel()
    idx = np.flatnonzero(sel)
    rs = r.ravel()[idx]
    as_ = ang.ravel()[idx]
    # Scatter to absolute pixels rather than blitting the box: a sprocket sits
    # on top of the wheel and the frame tubes, and anything it does not cover
    # has to show through untouched.
    py = (idx // bw + y0).astype(np.int32)
    px = (idx % bw + x0).astype(np.int32)

    # One byte per pixel indexing a three-entry palette, rather than an RGB
    # triple per pixel: a sprocket is only ever two colours and a hole, and
    # this keeps the per-frame arithmetic one third the width.
    pal = np.array([(0, 0, 0), COG, TOOTH], np.uint8)
    body = np.zeros(idx.size, np.uint8)
    if spider:
        # A ring and arms rather than a disc: at fourteen pixels of radius a
        # filled chainring is a grey blob and the crank vanishes into it.
        body[ring(rs, root - max(1.6, 0.20 * pitch_r), root) > 0.5] = 1
        body[rs <= max(1.6, 0.22 * pitch_r)] = 1
        # Arms sweep with the ring, so they are the one part recomputed, and
        # only over the pixels inside the ring that could hold one.
        arm_sub = np.flatnonzero(rs <= root)
        arm_a = as_[arm_sub]
        arm_w = (TAU / spider) * rs[arm_sub]
    else:
        body[rs <= root] = 1
        arm_sub = None

    tsub = np.flatnonzero((rs > root - 0.6) & (rs <= tip + 0.5))
    t_ang = as_[tsub]
    t_half = (0.32 - 0.17 * np.clip((rs[tsub] - root) / max(tip - root, 1e-3),
                                    0.0, 1.0)).astype(f32)

    code = np.empty(idx.size, np.uint8)
    scale = f32(teeth / TAU)

    def draw(buf, turns):
        np.copyto(code, body)
        psi = f32(turns * TAU)
        if arm_sub is not None:
            u = (arm_a - psi) * (spider / TAU)
            np.subtract(u, np.rint(u), out=u)
            np.abs(u, out=u)
            np.multiply(u, arm_w, out=u)
            code[arm_sub[u < 1.1]] = 1
        # Teeth: the angular cell of the tooth pitch, tapering to a point.
        v = (t_ang - psi) * scale
        np.subtract(v, np.rint(v), out=v)
        np.abs(v, out=v)
        code[tsub[v < t_half]] = 2
        solid = code != 0
        buf[py[solid], px[solid]] = pal[code[solid]]

    return draw


# --------------------------------------------------------------------------
# The chain: a closed belt over both pitch circles, sampled by arc length.
# --------------------------------------------------------------------------

def chain_path(c1, r1, c2, r2, step=0.4):
    """Uniformly resampled points around the external tangent belt.

    Arc length increases in the direction the chain travels: right along the
    top run toward the chainring, round its front, back along the bottom.
    """
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    dist = float(np.hypot(dx, dy))
    base = np.arctan2(dy, dx)
    # Tangent normal n satisfies n.D = r1 - r2, which has two solutions.
    ang = np.arccos(np.clip((r1 - r2) / dist, -1.0, 1.0))
    a_up = base - ang                       # screen y grows down, so this is up
    a_dn = base + ang

    def arc(c, rad, a_from, a_to):
        while a_to < a_from:
            a_to += TAU
        n = max(2, int(rad * (a_to - a_from) / step) + 1)
        a = np.linspace(a_from, a_to, n)
        return c[0] + rad * np.cos(a), c[1] + rad * np.sin(a)

    def seg(p0, p1):
        n = max(2, int(np.hypot(p1[0] - p0[0], p1[1] - p0[1]) / step) + 1)
        t = np.linspace(0.0, 1.0, n)
        return p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1])

    p_a = (c1[0] + r1 * np.cos(a_up), c1[1] + r1 * np.sin(a_up))
    p_b = (c2[0] + r2 * np.cos(a_up), c2[1] + r2 * np.sin(a_up))
    p_c = (c2[0] + r2 * np.cos(a_dn), c2[1] + r2 * np.sin(a_dn))
    p_d = (c1[0] + r1 * np.cos(a_dn), c1[1] + r1 * np.sin(a_dn))

    xs, ys = [], []
    for gx, gy in (seg(p_a, p_b), arc(c2, r2, a_up, a_dn),
                   seg(p_c, p_d), arc(c1, r1, a_dn, a_up)):
        xs.append(gx)
        ys.append(gy)
    xs = np.concatenate(xs)
    ys = np.concatenate(ys)

    # Resample by cumulative length so a link is a constant arc-length step.
    seglen = np.hypot(np.diff(xs), np.diff(ys))
    s = np.concatenate([[0.0], np.cumsum(seglen)])
    total = float(s[-1])
    n = int(total / step)
    u = np.linspace(0.0, total, n, endpoint=False)
    return np.interp(u, s, xs), np.interp(u, s, ys), total, total / n


def make_chain(c1, r1, c2, r2, pitch, W, H):
    px, py, total, step = chain_path(c1, r1, c2, r2)
    # The belt spans hub to bottom bracket, both well inside the canvas, so
    # clamping is only a guard against an absurd --radius.
    xi = np.clip(np.rint(px), 0, W - 1).astype(np.int32)
    yi = np.clip(np.rint(py), 0, H - 1).astype(np.int32)
    n_link = max(4, int(round(total / pitch)))
    link_s = np.arange(n_link, dtype=f32) * (total / n_link)
    inv = f32(1.0 / step)
    ntab = xi.size
    plate = np.array(CHAIN_PLATE, np.uint8)
    roller = np.array(CHAIN_ROLLER, np.uint8)

    def draw(buf, travel):
        # The side plates first, as one dim continuous run, then the rollers
        # on top: that reads as a chain rather than as a dotted line, and only
        # the rollers move, which is exactly what you see on a real one.
        buf[yi, xi] = plate
        k = (((travel + link_s) * inv).astype(np.int32)) % ntab
        buf[yi[k], xi[k]] = roller

    return draw


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=3.0,
                    help="wheel rate, turns/sec (mean, if sweeping)")
    ap.add_argument("--sweep", type=float, default=2.5,
                    help="turns/sec the rate swings either side of --speed")
    ap.add_argument("--sweep-period", type=float, default=26.0,
                    help="seconds for one full sweep up and back")
    ap.add_argument("--spokes", type=int, default=24, help="spokes per wheel")
    ap.add_argument("--cross", type=int, default=3,
                    help="lacing: spokes each spoke crosses (0 = radial)")
    ap.add_argument("--spoke-width", type=float, default=1.1,
                    help="spoke thickness in pixels; thin is what strobes")
    ap.add_argument("--radius", type=int, default=0,
                    help="wheel radius in pixels (0 = fit the panel)")
    ap.add_argument("--teeth", type=int, default=20, help="chainring teeth")
    ap.add_argument("--sprocket-teeth", type=int, default=10, help="rear cog teeth")
    ap.add_argument("--reflector-at", type=float, default=0.0,
                    help="rim reflector angle, radians")
    ap.add_argument("--no-drivetrain", dest="drivetrain", action="store_false",
                    help="wheels and frame only")
    ap.add_argument("--no-front", dest="front", action="store_false",
                    help="drop the front wheel")


def build(args):
    W, H = args.width, args.height

    # A 5:1 letterbox will not hold a correctly proportioned bike at this wheel
    # size -- the whole machine would sit in the middle third. The wheelbase is
    # stretched instead, which reads as a lowrider and fills the panel, and
    # incidentally gives the chain a long run where its motion is easy to see.
    R = args.radius or max(6, int(round(H * 0.60)))
    cy = H * 0.62
    rear = (W * 0.26, cy)
    front = (W * 0.925, cy)
    bb = (W * 0.61, H * 0.68)

    draw_rear = make_wheel(rear[0], rear[1], R, args.spokes, args.cross,
                           W, H, args, reflector=True)
    draw_front = None
    if args.front:
        draw_front = make_wheel(front[0], front[1], R, args.spokes,
                                args.cross, W, H, args)

    # Frame tubes, cropped by the top edge. Drawn per frame rather than baked
    # into a background because the chainstay passes in front of the wheel.
    tube_w = max(1.0, R * 0.045)
    tubes = []
    for p0, p1, wdt in (
            (bb, rear, tube_w),                                  # chainstay
            (rear, (W * 0.505, -H * 0.05), tube_w * 0.8),        # seatstay
            (bb, (W * 0.50, -H * 0.06), tube_w),                 # seat tube
            (bb, (W * 0.855, -H * 0.10), tube_w),                # down tube
            ((W * 0.855, -H * 0.10), front, tube_w * 0.85)):     # fork
        tubes.append(line_points(p0, p1, wdt))
    tube_yi, tube_xi = bake_points(np.concatenate([t[0] for t in tubes]),
                                   np.concatenate([t[1] for t in tubes]), W, H)

    # Drivetrain. Both sprockets share a chain pitch, so their pitch radii are
    # fixed by their tooth counts and the gear ratio falls out of that: the
    # chainring turns teeth_rear/teeth_front as fast as the wheel, and the
    # chain runs at the rim speed of the rear cog.
    ring_r = 0.34 * R
    pitch = TAU * ring_r / max(6, args.teeth)
    cog_r = pitch * max(5, args.sprocket_teeth) / TAU
    ratio = float(max(5, args.sprocket_teeth)) / max(6, args.teeth)

    draw_cog = draw_ring = draw_chain = None
    if args.drivetrain:
        draw_cog = make_sprocket(rear[0], rear[1], cog_r,
                                 max(5, args.sprocket_teeth), W, H)
        draw_ring = make_sprocket(bb[0], bb[1], ring_r, max(6, args.teeth),
                                  W, H, spider=5)
        draw_chain = make_chain(rear, cog_r, bb, ring_r, pitch, W, H)

    # Crank a good half again as long as the chainring is round, as on a
    # real bike -- any shorter and the arm never emerges from between the
    # teeth. It does mean the pedal dips off the bottom edge at the bottom of
    # its stroke, which suits a view already cropped at the wheel.
    crank_len = R * 0.60
    crank_w = max(1.0, R * 0.065)
    pedal_w = max(1.6, R * 0.05)
    pedal_len = max(3.0, R * 0.19)
    near_crank = crank_template(crank_len, crank_w, pedal_len, pedal_w,
                                CRANK_NEAR, PEDAL)
    far_crank = crank_template(crank_len, crank_w, pedal_len, pedal_w,
                               CRANK_FAR, CRANK_FAR)

    # Rate is swept, and the phase is the analytic integral of it so a dropped
    # frame costs a frame rather than drifting the whole drivetrain.
    base = float(args.speed)
    amp = float(args.sweep)
    period = max(0.5, float(args.sweep_period))
    k = amp * period / TAU

    buf = np.zeros((H, W, 3), np.uint8)

    def render(t, frame):
        turns = base * t + k * (1.0 - np.cos(TAU * t / period))
        buf[:] = 0

        draw_rear(buf, turns)
        if draw_front is not None:
            draw_front(buf, turns)
        buf[tube_yi, tube_xi] = TUBE

        if not args.drivetrain:
            return buf

        draw_cog(buf, turns)
        ring_turns = turns * ratio
        ca = ring_turns * TAU

        # Far crank behind the chainring, near crank in front of it, which is
        # what stops the two arms reading as one bar through the middle.
        far_crank(buf, bb, ca + np.pi)
        draw_ring(buf, ring_turns)
        draw_chain(buf, turns * TAU * cog_r)
        near_crank(buf, bb, ca)
        return buf

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
