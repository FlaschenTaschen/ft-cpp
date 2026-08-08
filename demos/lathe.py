#!/usr/bin/env python3
"""A woodturning lathe at work.

A blank spins between centres, a gouge walks the length of it on the tool
rest, shavings arc off the cut, and a turned shape emerges pass by pass. When
the last pass is done the piece is sanded, the lathe spins down, the work is
lifted off and a fresh blank goes in.

Everything is one array: **a radius per column** along the axis, `r[x]`. The
silhouette is that profile mirrored about the axis, so drawing the blank is a
column fill; cutting is the gouge writing a shallower radius as it passes;
shading is `sqrt(1 - (dy/r)^2)`, the true cylinder normal, which gives the
round form for the price of a lookup. Nothing here is a sprite and nothing
knows what shape is being turned.

The growth rings are the reason to build it this way. Rings live in the log's
cross section, indexed by distance from the pith, and every visible point on a
solid of revolution at column x is at exactly radius `r[x]` from the axis --
so the ring texture is a 1-D lookup on the radius, and reducing the radius
*reveals inner rings*. Bands crowd where the profile falls steeply and spread
along a taper, exactly as they do on real turned work, and they move as the
cut deepens. That is not decoration bolted on afterwards; it falls out of the
representation.

Spin is sold three ways. The pith is a little off the axis, so the ring radius
is `r - e*cos(alpha - beta)` where alpha is the material angle at that pixel
-- which expands to `r - e*(c*cos(wt+beta) + s*sin(wt+beta))` and needs no
per-pixel trigonometry at all, since `s` and `c` are the shading terms already
in hand. That makes the ring bands breathe once per revolution. The specular
band sits at a fixed angle and stays put while the surface moves under it,
which is what a highlight on a spinning cylinder does. And the headstock
pulley turns on the same phase, so there is an unambiguous rotating thing on
screen.

Rates are per second, never per frame -- feed in px/s, the fresh-cut glow as a
half-life in seconds -- so the demo looks the same at 8 fps and at 30.

Run:  python3 lathe.py --host 127.0.0.1
      python3 lathe.py --profile baluster --species walnut --feed 24
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * np.pi

# Wood, as luminance ramps rather than as colours mixed per pixel: the demo
# computes one scalar per pixel and the palette carries the species. Each runs
# from the shadow side of the cylinder to the highlight on top.
SPECIES = {
    "walnut": [(0.00, (7, 5, 4)), (0.25, (38, 23, 17)), (0.50, (76, 47, 32)),
               (0.75, (124, 83, 55)), (1.00, (190, 145, 105))],
    "cherry": [(0.00, (10, 4, 3)), (0.25, (52, 20, 12)), (0.50, (108, 49, 28)),
               (0.75, (170, 95, 58)), (1.00, (236, 170, 122))],
    "maple":  [(0.00, (13, 10, 7)), (0.25, (66, 50, 33)), (0.50, (130, 105, 70)),
               (0.75, (200, 170, 120)), (1.00, (248, 232, 192))],
    "redwood": [(0.00, (12, 5, 3)), (0.25, (60, 22, 11)), (0.50, (124, 53, 26)),
                (0.75, (188, 96, 48)), (1.00, (242, 162, 108))],
}

# Turned profiles as control points (position along the blank, radius as a
# fraction of the blank). They are interpolated and then smoothed, which is
# what turns a corner into a bead and a notch into a cove -- authoring the
# curves directly would be a lot of numbers for a shape that is 17 px tall.
CONTROL = {
    # A table leg: pommel left square, a bead-and-cove transition, a long
    # taper, and a foot ring near the bottom.
    "spindle": [(0.00, 1.00), (0.10, 1.00), (0.13, 0.55), (0.18, 0.82),
                (0.23, 0.48), (0.30, 0.80), (0.37, 0.74), (0.72, 0.46),
                (0.78, 0.39), (0.83, 0.68), (0.87, 0.41), (0.93, 0.54),
                (0.97, 1.00), (1.00, 1.00)],
    # A stair baluster: fat vase low down, long neck, small collar at the top.
    "baluster": [(0.00, 1.00), (0.07, 1.00), (0.10, 0.44), (0.14, 0.74),
                 (0.18, 0.43), (0.24, 0.63), (0.31, 0.88), (0.38, 0.96),
                 (0.46, 0.87), (0.54, 0.62), (0.63, 0.41), (0.69, 0.33),
                 (0.74, 0.62), (0.78, 0.35), (0.85, 0.45), (0.90, 0.74),
                 (0.94, 1.00), (1.00, 1.00)],
    # A rolling pin: thin handles, fillets, a long straight barrel.
    "pin": [(0.00, 1.00), (0.06, 1.00), (0.09, 0.34), (0.14, 0.30),
            (0.19, 0.34), (0.23, 0.56), (0.28, 0.82), (0.33, 0.88),
            (0.67, 0.88), (0.72, 0.82), (0.77, 0.56), (0.81, 0.34),
            (0.86, 0.30), (0.91, 0.34), (0.94, 1.00), (1.00, 1.00)],
}
PROFILES = sorted(list(CONTROL) + ["beads"])

CHIP_N = 96                                 # shavings alive at once, at most
LUT_S = 512                                 # samples across the cylinder
RING_N = 2048                               # samples across the blank radius


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------

def _blur1(a, w):
    """Box blur a 1-D array, edges extended."""
    w = int(w)
    if w < 2:
        return a.astype(f32)
    pad = w // 2
    k = np.ones(w, f32) / f32(w)
    return np.convolve(np.pad(a.astype(f32), pad, mode="edge"),
                       k, mode="same")[pad:pad + a.size].astype(f32)


def _beads_control(n):
    """A row of beads with coves between them, sized in columns, not in u."""
    cps = [(0.00, 1.00), (0.055, 1.00)]
    lo, hi = 0.085, 0.915
    step = (hi - lo) / n
    for k in range(n):
        cps.append((lo + k * step, 0.37))
        cps.append((lo + (k + 0.5) * step, 0.89))
    cps += [(hi, 0.37), (0.945, 1.00), (1.00, 1.00)]
    return cps


def _profile(name, n_cols, rng):
    """Target radius per column, as a fraction of the blank radius."""
    if name == "beads":
        cps = _beads_control(max(3, int(round(n_cols / 34.0))))
    else:
        cps = CONTROL[name]
    u = np.linspace(0.0, 1.0, n_cols, dtype=f32)
    r = np.interp(u, [p[0] for p in cps], [p[1] for p in cps]).astype(f32)
    w = max(2, int(round(n_cols * 0.028)))
    r = _blur1(_blur1(r, w), w)
    # The last few columns are held by the drive and the live centre and never
    # get cut, so they stay at full stock however the smoothing came out.
    ends = max(1, int(round(n_cols * 0.022)))
    r[:ends] = 1.0
    r[-ends:] = 1.0
    return np.clip(r, 0.16, 1.0).astype(f32)


def _ring_lut(rng, r_max, scale):
    """Ring darkness against distance from the pith.

    Growth rings are a wide soft band of earlywood and a narrow dark band of
    latewood, at irregular spacing, and the darkness varies from year to year.
    Sampling this by radius is the whole trick: the demo never has to know
    where a ring is on screen.
    """
    rho = np.linspace(0.0, r_max, RING_N, dtype=f32)
    widths = rng.uniform(1.9, 4.2, 64).astype(f32) * scale
    bounds = np.concatenate([[0.0], np.cumsum(widths)]).astype(f32)
    idx = np.interp(rho, bounds, np.arange(bounds.size, dtype=f32))
    ring = np.floor(idx).astype(np.int32)
    g = idx - ring                                   # phase within the ring

    dark = rng.uniform(0.55, 1.0, bounds.size + 2).astype(f32)[ring]
    # A wide latewood band rather than a knife edge: at this scale one pixel
    # of radius is a sizeable fraction of a ring, and a sharp band turns
    # neighbouring columns into black scratches instead of into figure.
    late = np.exp(-0.5 * ((g - 0.78) / f32(0.135)) ** 2)
    v = 0.82 + 0.18 * np.cos(TAU * g) - 0.44 * late * dark
    # A little fibre along the radius, so a wide earlywood band is not a flat
    # field of one colour on a panel that bands in the dark end anyway.
    fib = _blur1(rng.standard_normal(RING_N).astype(f32), max(2, int(RING_N / r_max * 0.35)))
    v += 0.10 * fib / max(float(np.abs(fib).max()), 1e-6)
    v -= v.min()
    return (v / max(float(v.max()), 1e-6)).astype(f32)


def _shade_luts(polish_gain):
    """Diffuse and specular against s = dy/r, the position across the cylinder.

    The axis is horizontal, so the normal is (0, s, c) with c = sqrt(1-s^2)
    pointing at the viewer. Light is above and in front. Both terms depend
    only on s, so they are lookups and the frame never evaluates a power.
    """
    s = np.linspace(-1.0, 1.0, LUT_S, dtype=f32)
    c = np.sqrt(np.maximum(1.0 - s * s, 0.0)).astype(f32)
    diff = np.clip(-0.62 * s + 0.79 * c, 0.0, 1.0) ** 1.20
    bounce = 0.09 * np.clip(s, 0.0, 1.0) ** 3        # off the bed, underneath
    hn = np.clip(-0.327 * s + 0.945 * c, 0.0, 1.0)   # half vector, V = +z
    spec = hn ** 36
    # The ceiling matters: the whole point of sanding is that the polished
    # highlight is the *only* thing near the top of the ramp, so everything
    # else has to leave room above it.
    base = (0.11 + 0.60 * diff + bounce + 0.13 * spec).astype(f32)
    return base, (polish_gain * spec + 0.05 * polish_gain * diff).astype(f32), c


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--profile", default="random",
                    choices=PROFILES + ["random"],
                    help="what gets turned; random picks per blank")
    ap.add_argument("--species", default="random",
                    choices=sorted(SPECIES) + ["random"],
                    help="wood, as a luminance palette")
    ap.add_argument("--feed", type=float, default=32.0,
                    help="gouge traverse, px/s on a 320 wide panel")
    ap.add_argument("--passes", type=int, default=4,
                    help="roughing passes before the finish cut")
    ap.add_argument("--spin", type=float, default=2.2,
                    help="spindle speed in revolutions per second")
    ap.add_argument("--grain", type=float, default=1.0,
                    help="growth ring contrast, 0 = plain wood")
    ap.add_argument("--pith", type=float, default=0.75,
                    help="how far the pith sits off the axis, px; this is what "
                         "makes the rings breathe once per revolution")
    ap.add_argument("--chips", dest="chips", action="store_true", default=True,
                    help="shavings off the cut")
    ap.add_argument("--no-chips", dest="chips", action="store_false")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    sc = f32(H / 64.0)                      # vertical scale: radii, rings
    sw = f32(W / 320.0)                     # horizontal scale: feed, layout

    # ---- layout ----------------------------------------------------------
    cy = f32(H * 0.355)                     # the spindle axis
    r0 = f32(H * 0.240)                     # blank radius
    head_w = max(8, int(round(W * 0.085)))
    tail_w = max(7, int(round(W * 0.072)))
    x0 = head_w + 2                         # the drive centre
    x1 = max(x0 + 16, W - tail_w - 2)       # the live centre
    N = x1 - x0
    bed_y0 = int(cy + r0 + 12 * sc)
    bed_y1 = min(H, int(cy + r0 + 22 * sc))
    rest_y0 = int(cy + r0 + 4 * sc)
    rest_y1 = max(rest_y0 + 1, int(cy + r0 + 6 * sc))

    # Rows the blank can touch. Tight on purpose: this window is the whole
    # per-frame cost, and padding it for the once-a-cycle lift would make
    # every frame pay for it. The lift moves the snapshot's blit target
    # instead, which costs nothing the rest of the time.
    LIFT = f32(15.0 * sc)
    ytop = max(0, int(np.floor(cy - r0 - 2)))
    ybot = min(H, int(np.ceil(cy + r0 + 3)))
    M = ybot - ytop

    # ---- tables ----------------------------------------------------------
    ring_max = f32(r0 * 1.12)
    ring_lut = _ring_lut(rng, float(ring_max), float(sc))
    ring_k = f32((RING_N - 1) / ring_max)
    shade_lut, spec_lut, c_lut = _shade_luts(0.34)
    s_k = f32((LUT_S - 1) * 0.5)

    species_names = sorted(SPECIES) if args.species == "random" else [args.species]
    wood_luts = {n: ds.gradient(SPECIES[n], 256, dtype=f32) for n in species_names}
    profile_names = PROFILES if args.profile == "random" else [args.profile]

    # The rough-sawn surface of an uncut blank. It varies along the axis and
    # is smeared around the circumference, which is what a spinning rough
    # cylinder actually shows: only the axial variation survives the blur.
    rough = rng.random((M, N)).astype(f32)
    rough = _boxblur2(rough, max(3, int(7 * sc)), 2)
    # Duller and darker than any cut surface: the first pass has to visibly
    # clean the blank up, or the roughing reads as nothing happening.
    rough = 0.44 + 0.34 * (rough - rough.min()) / max(float(np.ptp(rough)), 1e-6)

    # Pith wander: a smooth offset and direction along the log.
    pith_e = np.abs(_blur1(rng.standard_normal(N), max(3, N // 7)))
    pith_e = (f32(args.pith) * sc * (0.45 + 0.55 * pith_e /
                                     max(float(pith_e.max()), 1e-6))).astype(f32)
    pith_b = (_blur1(rng.standard_normal(N), max(3, N // 5)) * 9.0).astype(f32)

    # ---- static shop, baked once ----------------------------------------
    bg = _bake_shop(W, H, rng, cy, r0, head_w, tail_w, x0, x1,
                    bed_y0, bed_y1, rest_y0, rest_y1, sc)
    bgf = bg.astype(f32)
    bg_sub = bgf[ytop:ybot, x0:x1]

    pulley = _bake_pulley(head_w, cy, r0, sc)

    # ---- per-column state ------------------------------------------------
    r = np.zeros(N, f32)
    target = np.zeros(N, f32)
    goals = []
    polish = np.zeros(N, f32)
    cutamt = np.zeros(N, f32)
    fresh = np.zeros(N, f32)
    chatter = []

    feed = f32(max(2.0, args.feed) * sw)
    n_rough = int(np.clip(args.passes, 1, 8))
    grain_depth = f32(np.clip(args.grain, 0.0, 2.0) * 0.55)
    fresh_hl = 2.2                           # seconds; see the module docstring

    ops = ([("cut", k) for k in range(n_rough)]
           + [("sand", 0), ("spindown", 2.0), ("dwell", 2.2), ("lift", 1.9)])
    st = {"op": 0, "s": 0.0, "gx": float(x0), "cutting": False,
          "phase": 0.0, "tool": 1.0, "removed": 0.0}

    def new_blank():
        name = profile_names[int(rng.integers(len(profile_names)))]
        sp = species_names[int(rng.integers(len(species_names)))]
        st["wood"] = wood_luts[sp]
        frac = _profile(name, N, rng)
        target[:] = frac * r0
        # Roughing takes more off early and creeps up on the shape: the last
        # pass must land exactly on the target, or the piece is never finished.
        goals[:] = []
        chatter[:] = []
        for k in range(n_rough):
            f = ((k + 1) / float(n_rough)) ** 0.72
            goals.append((r0 + (target - r0) * f32(f)).astype(f32))
            chatter.append(f32(0.22 * (1.0 - f) + 0.04) * sc)
        # A sawn blank is not a true cylinder, and starting from one makes the
        # first pass invisible.
        r[:] = r0 * (1.0 - 0.035 * np.abs(rng.standard_normal(N)).astype(f32))
        polish[:] = 0.0
        cutamt[:] = 0.0
        fresh[:] = 0.0
        st["gx"] = float(x0)

    new_blank()

    # ---- chips -----------------------------------------------------------
    cx_ = np.zeros(CHIP_N, f32)
    cy_ = np.zeros(CHIP_N, f32)
    cvx = np.zeros(CHIP_N, f32)
    cvy = np.zeros(CHIP_N, f32)
    clife = np.zeros(CHIP_N, f32)
    clife0 = np.ones(CHIP_N, f32)
    cphase = np.zeros(CHIP_N, f32)
    chip_budget = [0.0]
    gravity = 160.0 * float(sc)

    # ---- the toolpath ----------------------------------------------------
    def sweep(a, b, kind, k):
        """Cut every column the gouge crossed between a and b."""
        contact = 1.6 * float(sc)
        i0 = max(0, int(np.floor(min(a, b) - x0 - contact)))
        i1 = min(N, int(np.ceil(max(a, b) - x0 + contact)) + 1)
        if i1 <= i0:
            return
        sl = slice(i0, i1)
        before = r[sl].copy()
        if kind == "sand":
            r[sl] = target[sl]
            polish[sl] = 1.0
        else:
            ch = rng.normal(0.0, float(chatter[k]), i1 - i0).astype(f32)
            r[sl] = np.clip(goals[k][sl] + ch, target[sl], r[sl])
            polish[sl] = np.minimum(polish[sl], 0.12)
        cutamt[sl] = 1.0
        fresh[sl] = 1.0
        st["removed"] += float((before - r[sl]).sum())

    def advance(dt):
        guard = 0
        while dt > 1e-6 and guard < 16:
            guard += 1
            kind, val = st["ops"][st["op"]]
            if kind in ("cut", "sand"):
                # Sanding runs faster than a cut, because it is taking dust
                # off rather than wood, and forty seconds is not long.
                # Plain Python floats, deliberately. The toolpath position is
                # accumulated across frames and then rounded to a column, so it
                # is the one place in here where scalar precision is visible:
                # keeping it in float32 makes the sweep land a column early or
                # late, which changes how many chatter samples get drawn, which
                # desynchronises the RNG and turns out a different piece of
                # wood. numpy 1.x promoted these to float64 by accident (scalar
                # arithmetic ignored value-based casting); NEP 50 in numpy 2.0
                # keeps them float32. float() says what we actually meant, and
                # reads the same on both.
                v = float(feed if kind == "cut" else feed * f32(2.6))
                step = min(dt * v, N - st["s"])
                s1 = st["s"] + step
                # Alternate direction per pass: a real turner works back and
                # forth, and a rapid return would be dead time on a 45 s slot.
                back = (kind == "cut" and val % 2 == 1)
                g0 = x0 + (N - st["s"] if back else st["s"])
                g1 = x0 + (N - s1 if back else s1)
                sweep(g0, g1, kind, val)
                st["gx"] = float(g1)
                st["cutting"] = True
                if s1 < N - 1e-6:
                    st["s"] = s1
                    return
                dt -= step / v
            else:
                st["cutting"] = False
                if dt < val - st["s"]:
                    st["s"] += dt
                    return
                dt -= val - st["s"]
            st["op"] += 1
            st["s"] = 0.0
            if st["op"] >= len(st["ops"]):
                st["op"] = 0
            elif st["ops"][st["op"]][0] == "lift":
                # The finished piece is already a rendered bitmap by now (see
                # the snapshot in render), so the fresh blank can go straight
                # in underneath it and the lathe is never empty. A cut to a
                # bare machine between pieces is the one thing that would make
                # this read as a loop rather than as a shift's work.
                new_blank()

    st["ops"] = ops

    # ---- frame -----------------------------------------------------------
    frame = np.empty((H, W, 3), np.uint8)
    dyc = np.arange(ytop, ybot, dtype=f32)[:, None] - cy
    last_t = [None]
    # Last frame's piece, kept as colours and coverage rather than as state, so
    # the work can be lifted off while a fresh blank is already turning.
    snap_rgb = np.zeros((M, N, 3), f32)
    snap_a = np.zeros((M, N), f32)

    def render(t, frame_idx):
        if last_t[0] is None:
            last_t[0] = t
        dt = float(min(0.1, max(0.0, t - last_t[0])))
        last_t[0] = t

        advance(dt)
        kind, val = st["ops"][st["op"]]
        u = float(st["s"] / val) if kind not in ("cut", "sand") and val else 0.0

        # The lathe runs at speed except at the end of a piece, where it spins
        # down, sits still to be lifted off, and spins back up on the next
        # blank. That beat is the resolution -- see the laser dropping a part.
        if kind == "spindown":
            spin_f = 1.0 - u
        elif kind == "dwell":
            spin_f = 0.0
        elif kind == "lift":
            spin_f = max(0.0, (u - 0.40) / 0.60)     # back up on the new blank
        else:
            spin_f = 1.0
        st["phase"] += TAU * float(args.spin) * spin_f * dt

        # During the handover the blank being drawn is already the *next* one,
        # fading up; the piece just finished is the snapshot, on its way out.
        lifting = kind == "lift"
        alpha_g = (f32(np.clip((u - 0.34) / 0.45, 0.0, 1.0))
                   if lifting else f32(1.0))

        fresh[:] *= f32(0.5 ** (dt / fresh_hl))

        # Tool in and out on an exponential approach rather than a step, so it
        # eases at a pass boundary at any frame rate.
        want = 0.0 if st["cutting"] else 1.0
        # float(), because np.exp() of a Python float hands back a numpy
        # scalar, and a numpy float64 mixed with a float32 array promotes
        # differently before and after NEP 50. Keeping the eased tool
        # position a plain Python float keeps the gouge the same colour on
        # both.
        st["tool"] += float((want - st["tool"]) * (1.0 - np.exp(-dt / 0.20)))

        np.copyto(frame, bg)
        _draw_pulley(frame, pulley, st["phase"])

        # ---- the blank ---------------------------------------------------
        if alpha_g > 0.004:
            rr = np.maximum(r, 0.6)[None, :]
            dy = dyc
            s = np.clip(dy / rr, -1.0, 1.0)
            si = ((s + 1.0) * s_k).astype(np.int32)
            c = np.take(c_lut, si)

            # rho: the ring radius of the material now facing us. See the
            # docstring -- this is cos(asin(s) - wt - beta) expanded onto the
            # s and c that shading already needed.
            ph = st["phase"] + pith_b
            rho = rr - pith_e[None, :] * (c * np.cos(ph)[None, :]
                                          + s * np.sin(ph)[None, :])
            gi = np.clip(rho * ring_k, 0, RING_N - 1).astype(np.int32)
            surf = np.take(ring_lut, gi)
            ca = cutamt[None, :]
            surf = surf * ca + rough * (1.0 - ca)

            lum = (np.take(shade_lut, si)
                   + polish[None, :] * np.take(spec_lut, si))
            lum *= 1.0 - grain_depth * (1.0 - surf)
            lum *= 1.0 + 0.22 * fresh[None, :]

            li = np.clip(lum * 255.0, 0, 255).astype(np.int32)
            wood = np.take(st["wood"], li, axis=0)

            alpha = np.clip(rr - np.abs(dy) + 0.5, 0.0, 1.0)
            if kind == "dwell":
                # The piece is finished and standing still; one copy a cycle is
                # all the lift needs, so this is not in the hot path.
                np.copyto(snap_rgb, wood)
                np.copyto(snap_a, alpha)
            alpha *= alpha_g
            wood -= bg_sub
            wood *= alpha[:, :, None]
            wood += bg_sub
            np.copyto(frame[ytop:ybot, x0:x1], wood, casting="unsafe")

        if lifting:
            # The finished piece, coming up off the centres and out of frame.
            rise = int(round(float(LIFT) * min(1.0, u / 0.62) ** 2))
            fade = f32(np.clip((0.92 - u) / 0.34, 0.0, 1.0))
            dy0, dy1 = max(0, ytop - rise), max(0, ybot - rise)
            if dy1 > dy0 and fade > 0.004:
                sy = slice(dy0 - ytop + rise, dy1 - ytop + rise)
                dst = frame[dy0:dy1, x0:x1].astype(f32)
                a = (snap_a[sy] * fade)[:, :, None]
                dst += (snap_rgb[sy] - dst) * a
                np.copyto(frame[dy0:dy1, x0:x1], dst, casting="unsafe")

        # ---- shavings ----------------------------------------------------
        gx = st["gx"]
        gi_col = int(np.clip(gx - x0, 0, N - 1))
        gy = float(cy + r[gi_col])
        if args.chips:
            clife[:] -= dt
            live = clife > 0.0
            if live.any():
                cvy[live] += gravity * dt
                cx_[live] += cvx[live] * dt
                cy_[live] += cvy[live] * dt
            # Emission follows how much wood is actually coming off, so a
            # finish pass makes a wisp and a roughing pass makes a shower.
            if st["cutting"] and st["removed"] > 0.0:
                chip_budget[0] += min(28.0, 9.0 * st["removed"] / max(dt, 1e-3) / 60.0 + 4.0) * dt
                n = int(chip_budget[0])
                if n > 0:
                    chip_budget[0] -= n
                    free = np.flatnonzero(clife <= 0.0)[:n]
                    if free.size:
                        m = free.size
                        cx_[free] = gx + rng.normal(0.0, 1.0, m)
                        cy_[free] = gy + rng.normal(0.0, 0.8, m)
                        cvx[free] = rng.normal(0.0, 26.0, m) * sw
                        cvy[free] = -rng.uniform(28.0, 95.0, m) * sc
                        clife[free] = clife0[free] = rng.uniform(0.5, 1.35, m)
                        cphase[free] = rng.uniform(0.0, TAU, m)
        st["removed"] = 0.0

        # ---- the gouge ---------------------------------------------------
        _draw_gouge(frame, gx, gy + 1.0 + 7.0 * float(sc) * st["tool"],
                    float(sc), 1.0 - st["tool"])

        if args.chips:
            live = np.flatnonzero(clife > 0.0)
            if live.size:
                iy = np.rint(cy_[live]).astype(np.int32)
                ix = np.rint(cx_[live]).astype(np.int32)
                ok = (iy >= 0) & (iy < H) & (ix >= 0) & (ix < W)
                if ok.any():
                    iy, ix = iy[ok], ix[ok]
                    frac = clife[live][ok] / clife0[live][ok]
                    # A shaving tumbles, so it catches the light and loses it;
                    # a constant dot reads as a spark instead of as wood.
                    tumble = 0.55 + 0.45 * np.abs(np.sin(
                        t * 13.0 + cphase[live][ok]))
                    a = np.clip(np.minimum(frac * 3.0, 1.0), 0, 1) * tumble
                    col = (a[:, None] * st["wood"][236]).astype(np.uint8)
                    frame[iy, ix] = np.maximum(frame[iy, ix], col)

        return frame

    return render


# --------------------------------------------------------------------------
# Static furniture. All of this is baked once; none of it moves.
# --------------------------------------------------------------------------

def _boxblur2(a, wy, wx):
    """Separable box blur of a 2-D float array, edges extended."""
    if wy > 1:
        a = np.apply_along_axis(_blur1, 0, a, wy)
    if wx > 1:
        a = np.apply_along_axis(_blur1, 1, a, wx)
    return a.astype(f32)


def _bake_shop(W, H, rng, cy, r0, head_w, tail_w, x0, x1,
               bed_y0, bed_y1, rest_y0, rest_y1, sc):
    """The shop around the work: wall, bed, headstock, tailstock, tool rest."""
    y = np.arange(H, dtype=f32)[:, None]
    x = np.arange(W, dtype=f32)[None, :]

    # Warm wall, darker low down, with a pool of shop light over the work.
    wall = 1.0 - 0.55 * np.clip(y / max(H - 1.0, 1.0), 0.0, 1.0)
    glow = np.exp(-(((x - W * 0.5) / (W * 0.42)) ** 2
                    + ((y - cy) / (H * 0.85)) ** 2))
    lum = 11.0 * wall + 15.0 * glow
    img = lum[..., None] * np.array([1.0, 0.86, 0.70], f32)
    # Speckle rather than a smooth field: a large dark gradient bands at 8 PWM
    # bits, and this is cheaper than dithering the whole wall.
    n = int(W * H * 0.05)
    flat = np.unique(rng.integers(0, H * W, n))
    img[flat // W, flat % W] += rng.uniform(0.0, 5.0, flat.size)[:, None]

    def box(y0, y1, xa, xb, base, top=None, edge=None):
        y0, y1 = max(0, int(y0)), min(H, int(y1))
        xa, xb = max(0, int(xa)), min(W, int(xb))
        if y1 <= y0 or xb <= xa:
            return
        img[y0:y1, xa:xb] = np.asarray(base, f32)
        if top is not None:
            img[y0, xa:xb] = np.asarray(top, f32)
        if edge is not None and xb - 1 >= xa:
            img[y0:y1, xb - 1] = np.asarray(edge, f32)

    CAST = (30.0, 30.0, 34.0)
    CAST_TOP = (74.0, 76.0, 82.0)
    CAST_DARK = (17.0, 17.0, 20.0)

    # Bed and its ways, running the whole width -- the reason this scene fits
    # a 5:1 panel at all.
    box(bed_y0, bed_y1, 0, W, CAST, CAST_TOP)
    img[min(H - 1, bed_y0 + max(1, int(2 * sc))), :] = np.array((50.0, 51.0, 56.0), f32)
    box(bed_y1 - max(1, int(2 * sc)), bed_y1, 0, W, CAST_DARK)
    # Cabinet legs under it.
    for lx in (int(W * 0.10), int(W * 0.72)):
        box(bed_y1, H, lx, lx + int(W * 0.055) + 2, (20.0, 20.0, 23.0))

    # Headstock, tailstock.
    head_y0 = int(cy - r0 - 4 * sc)
    box(head_y0, bed_y0, 0, head_w, (44.0, 42.0, 44.0), (78.0, 76.0, 78.0),
        (22.0, 22.0, 25.0))
    tail_y0 = int(cy - r0 * 0.85)
    box(tail_y0, bed_y0, W - tail_w, W, (44.0, 42.0, 44.0), (78.0, 76.0, 78.0))
    # Quill and live centre reaching in to the work.
    q = max(1, int(1.6 * sc))
    box(cy - q, cy + q + 1, x1 - 1, W - tail_w + 2, (128.0, 130.0, 138.0))
    box(cy - q - 1, cy + q + 2, x0 - 3, x0 + 1, (120.0, 122.0, 130.0))
    # Tailstock handwheel: static, unlike the pulley, because it does not turn
    # while the lathe runs.
    _disc(img, W - int(tail_w * 0.42), int(cy - r0 * 0.35), max(2.0, 3.4 * sc),
          (96.0, 96.0, 102.0), (58.0, 58.0, 64.0))

    # Tool rest: a bar under the work on two posts, which is what the gouge
    # rides along.
    box(rest_y0, rest_y1 + 1, x0 + 2, x1 - 2, (58.0, 58.0, 63.0),
        (104.0, 104.0, 110.0))
    for px in (x0 + int((x1 - x0) * 0.22), x0 + int((x1 - x0) * 0.72)):
        box(rest_y1, bed_y0, px, px + max(2, int(3 * sc)), (36.0, 36.0, 40.0))

    return np.clip(img, 0, 255).astype(np.uint8)


def _disc(img, cx, cy, rad, face, rim):
    H, W = img.shape[:2]
    y0, y1 = max(0, int(cy - rad - 1)), min(H, int(cy + rad + 2))
    x0, x1 = max(0, int(cx - rad - 1)), min(W, int(cx + rad + 2))
    if y1 <= y0 or x1 <= x0:
        return
    yy = np.arange(y0, y1, dtype=f32)[:, None] - cy
    xx = np.arange(x0, x1, dtype=f32)[None, :] - cx
    d = np.hypot(yy, xx)
    img[y0:y1, x0:x1][d <= rad] = np.asarray(face, f32)
    img[y0:y1, x0:x1][(d <= rad) & (d > rad - 1.2)] = np.asarray(rim, f32)


def _bake_pulley(head_w, cy, r0, sc):
    """The headstock pulley: the one thing on screen that visibly rotates.

    Baked as a face plus a polar coordinate, so a frame is one modulo and one
    where() over a couple of hundred pixels.
    """
    rad = float(max(3.0, min(head_w * 0.40, r0 * 0.70)))
    cx = int(head_w * 0.50)
    icy = int(cy)
    y0, x0 = icy - int(rad) - 1, cx - int(rad) - 1
    n = 2 * (int(rad) + 1) + 1
    yy = np.arange(n, dtype=f32)[:, None] - (rad + 1)
    xx = np.arange(n, dtype=f32)[None, :] - (rad + 1)
    d = np.hypot(yy, xx)
    theta = np.arctan2(yy, xx).astype(f32)

    face = np.zeros((n, n, 3), np.uint8)
    face[:] = np.array((26, 25, 28), np.uint8)
    face[d <= rad] = np.array((40, 38, 41), np.uint8)
    face[(d <= rad) & (d > rad - 1.4)] = np.array((92, 90, 96), np.uint8)
    face[d <= rad * 0.30] = np.array((70, 68, 74), np.uint8)
    web = (d < rad - 1.4) & (d > rad * 0.30)
    return {"y0": y0, "x0": x0, "n": n, "face": face, "web": web,
            "theta": theta, "spokes": 3,
            "col": np.array((104, 96, 88), np.uint8)}


def _draw_pulley(frame, p, phase):
    H, W = frame.shape[:2]
    y0, x0, n = p["y0"], p["x0"], p["n"]
    dy0, dy1 = max(0, y0), min(H, y0 + n)
    dx0, dx1 = max(0, x0), min(W, x0 + n)
    if dy1 <= dy0 or dx1 <= dx0:
        return
    sy = slice(dy0 - y0, dy1 - y0)
    sx = slice(dx0 - x0, dx1 - x0)
    step = TAU / p["spokes"]
    a = (p["theta"][sy, sx] + phase) % step
    spoke = p["web"][sy, sx] & (a < 0.34)
    out = p["face"][sy, sx].copy()
    out[spoke] = p["col"]
    frame[dy0:dy1, dx0:dx1] = out


def _draw_gouge(frame, tip_x, tip_y, sc, engaged):
    """The tool: steel at the bevel, a ferrule, then a handle running off.

    Drawn as an indexed write down a straight line rather than as a sprite,
    because it moves sub-pixel along the rest and a blitted sprite would step.
    """
    H, W = frame.shape[:2]
    length = 26.0 * float(sc)
    dx, dy = 0.50, 0.87
    n = int(length) + 1
    u = np.linspace(0.0, 1.0, n)
    xs = tip_x + dx * length * u
    ys = tip_y + dy * length * u
    steel = np.array([176, 182, 194], f32)
    ferr = np.array([132, 108, 62], f32)
    wood = np.array([84, 50, 28], f32)
    col = np.where(u[:, None] < 0.34, steel,
                   np.where(u[:, None] < 0.46, ferr, wood))
    col = col * (0.55 + 0.45 * engaged)
    for off in (0.0, 1.0):
        iy = np.rint(ys + off).astype(np.int32)
        ix = np.rint(xs).astype(np.int32)
        ok = (iy >= 0) & (iy < H) & (ix >= 0) & (ix < W)
        if not ok.any():
            continue
        c = (col[ok] * (1.0 if off == 0.0 else 0.55)).astype(np.uint8)
        # Assignment, not np.maximum(out=...): a fancy-indexed destination is
        # a copy, so an out= write would land in a temporary and vanish.
        frame[iy[ok], ix[ok]] = np.maximum(frame[iy[ok], ix[ok]], c)
    # The bevel rubbing: a small hot point where the edge meets the wood.
    if engaged > 0.02:
        px, py = int(round(tip_x)), int(round(tip_y))
        v = np.array([255, 232, 190], f32) * engaged
        for oy in (-1, 0):
            for ox in (-1, 0, 1):
                yy, xx = py + oy, px + ox
                if 0 <= yy < H and 0 <= xx < W:
                    w = 1.0 if (oy, ox) == (0, 0) else 0.45
                    np.maximum(frame[yy, xx], (v * w).astype(np.uint8),
                               out=frame[yy, xx])


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
