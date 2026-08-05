#!/usr/bin/env python3
"""A 3D printer at work.

Side elevation of a bed-slinger: a wide heated bed along the bottom, vertical
Z rails at each end, a gantry beam that rises a layer at a time, and the
hotend riding it. The nozzle is the brightest thing on the panel and lays
material one pixel row per layer, so the part grows upward with visible layer
lines and a visible infill lattice inside its perimeter walls. Prints finish,
sit and cool, the bed clears, and the next one starts.

And roughly one print in three turns into spaghetti.

The part
--------
Five silhouettes -- calibration cube, vase, Benchy-ish boat, spur gear, and an
L-bracket -- are functions of normalized (u, v) rather than sprites, so they
rasterize to whatever height the panel affords. Each is classified once per
print into three materials: *perimeter* (the mask minus its 4-neighbour
erosion, so holes get walls too), *skin* (anything within two rows of a
horizontal surface, which is what makes bottoms and tops solid and gives the
boat's cabin a real roof), and *infill* everywhere else, where a diagonal
grid or a gyroid is stencilled through. Only that stencil is drawn, so the
cross-section reads as a lattice rather than a filled outline. Layer lines
come free from a per-row brightness alternation on top of it.

A layer is one pixel row. The nozzle sweeps between the extents of the row it
is on, so a wide layer takes longer than a narrow one and the vase's neck
prints visibly faster than its belly. Material appears only behind the
nozzle, which is what makes the current layer read as being drawn rather than
switched on. A heat field decayed each frame carries the fresh-extrusion glow:
the span just laid is set to 1.0 and everything cools with a ~1.6 s time
constant and a squared ramp index, so the row under the nozzle is white hot,
the two or three below it are orange, and the rest is cold filament. The
partly-drawn layer is that glow alone: material proper appears when the pass
finishes and the row joins the part.

The spaghetti
-------------
Failure is two *independent* random draws per print and nothing else -- no
counter, no schedule. `rng.random() < --fail-rate` decides whether this one
fails at all, and if it does, `rng.uniform(0.08, 0.94)` of the layer count
decides where. A failure at 10% and a failure at 90% look nothing alike and
both turn up; anything derived from a print index would become predictable on
a wall people walk past several times a day, which is the one thing to avoid.

When it lets go, the part is dragged off its footing -- translated along the
bed and skewed in bands so it leans -- and the nozzle carries on extruding
into the air. The filament is a ring buffer of points emitted at the tip and
connected in order. Each falls slowly -- heavy air drag, weak gravity --
under an acceleration that rotates in both axes, and that is the whole trick:
the phase is read off *one running clock* shared by the strand rather than
drawn per point, so neighbours differ by a fixed offset and trace out the same
curling curve. Sample the phase independently and you get confetti; make the
curl fast enough to close two or three turns before the strand reaches the bed
and you get coils. They settle onto a per-column pile heightmap that grows and
spreads as it fills, and points evicted from the ring are baked into the
static bed layer, so the nest keeps accumulating at constant cost. The gantry
sweeps on obliviously.

Cost
----
The only whole-frame passes are the background copy and the heat glow, which
is a LUT gather and a saturating add. The nozzle halo is a precomputed stamp
blitted over ~11x11 px rather than a blur -- a whole-frame blur is two orders
of magnitude more expensive on the Pi than on a desktop. Everything else is
proportional to the part, which is small.

Run:  python3 printer.py --host 127.0.0.1
      python3 printer.py --parts boat,vase --speed 1.5
      python3 printer.py --fail-rate 0.6 --seed 11
      python3 printer.py --no-fail --parts gear
"""

import sys
from types import SimpleNamespace

import numpy as np

import demoscene as ds

f32 = np.float32


# --------------------------------------------------------------------------
# Silhouettes. Each takes u in [-1, 1] across the width, v in [0, 1] from the
# bed up, and the aspect ratio, and returns a boolean mask. Circles are
# written against x = u * aspect / 2 so they come out round rather than as
# ellipses, whatever the part's proportions.
# --------------------------------------------------------------------------

def _sil_cube(u, v, a):
    """A calibration cube, with the top corners knocked off."""
    chamfer = np.clip((v - 0.88) / 0.12, 0.0, 1.0) * 0.20
    return np.abs(u) <= 1.0 - chamfer


def _sil_vase(u, v, a):
    """A solid of revolution: foot, belly, waist, flared lip."""
    r = np.interp(v, [0.00, 0.05, 0.30, 0.55, 0.78, 0.90, 1.00],
                     [0.55, 0.68, 1.00, 0.78, 0.42, 0.48, 0.74])
    return np.abs(u) <= r


def _sil_boat(u, v, a):
    """The benchmark boat: raked bow, cabin with a porthole, funnel."""
    x = u * a * 0.5
    hull = (v < 0.36) & (u >= -0.55 - 1.15 * v) & (u <= 0.40 + 1.60 * v)
    cabin = (v >= 0.36) & (v < 0.74) & (u >= -0.58) & (u <= 0.30)
    roof = (v >= 0.74) & (v < 0.82) & (u >= -0.70) & (u <= 0.42)
    stack = (v >= 0.82) & (u >= -0.46) & (u <= -0.16)
    port = (x - 0.02 * a * 0.5) ** 2 + (v - 0.55) ** 2 < 0.085 ** 2
    return (hull | cabin | roof | stack) & ~port


def _sil_gear(u, v, a):
    """A spur gear stood on edge: twelve teeth, four spokes, a bore."""
    x = u * a * 0.5
    y = v - 0.5
    rr = np.sqrt(x * x + y * y) * 2.0
    ang = np.arctan2(y, x)
    tooth = 0.84 + 0.16 * (np.cos(12.0 * ang) > 0.0)
    web = (rr < 0.44) | (rr > 0.72) | (np.cos(4.0 * ang + 0.4) > 0.55)
    return (rr <= tooth) & (rr >= 0.20) & web


def _sil_bracket(u, v, a):
    """An L-bracket with a bolt hole through each leg."""
    m = ((u <= -0.34) | (v <= 0.30)) & (np.abs(u) <= 1.0)

    def hole(cu, cv, r):
        return ((u - cu) * a * 0.5) ** 2 + (v - cv) ** 2 < r * r

    return m & ~hole(-0.67, 0.84, 0.072) & ~hole(0.56, 0.15, 0.072)


# name -> (silhouette, width/height)
PARTS = {
    "cube":    (_sil_cube,    1.00),
    "vase":    (_sil_vase,    0.80),
    "boat":    (_sil_boat,    1.45),
    "gear":    (_sil_gear,    1.00),
    "bracket": (_sil_bracket, 1.15),
}

# Filament base colours. The perimeter gets the full value, the skin 82% and
# the infill 58%, so the lattice sits behind the walls rather than competing
# with them.
FILAMENTS = (
    (255, 112, 20),    # orange PLA
    (0, 195, 180),     # teal
    (230, 55, 60),     # red
    (220, 220, 208),   # natural
    (70, 205, 95),     # green
    (165, 95, 240),    # purple
    (245, 200, 45),    # yellow
    (70, 130, 255),    # blue
)

# The hotend, as characters. 'H' heater block, 'h' its lit interior, 'N' the
# nozzle cone. The tip pixel is the extrusion point and everything is placed
# relative to it.
NOZZLE_BIG = (
    "HHHHHHHHH",
    "HhhhhhhhH",
    "HhhhhhhhH",
    "HHHHHHHHH",
    "..NNNNN..",
    "....N....",
)

NOZZLE_SMALL = (
    "HHHHH",
    "HhhhH",
    ".NNN.",
    "..N..",
)

NOZZLE_COLOURS = {"H": (58, 62, 74), "h": (170, 66, 18), "N": (255, 186, 74)}


def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=1.0,
                    help="overall rate: gantry travel and phase lengths")
    ap.add_argument("--fail-rate", type=float, default=0.3,
                    help="probability a given print turns into spaghetti")
    ap.add_argument("--no-fail", action="store_true",
                    help="every print succeeds")
    ap.add_argument("--parts", default="all",
                    help="comma list of %s, or 'all'" % ",".join(sorted(PARTS)))
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    speed = max(0.1, float(args.speed))
    fail_rate = 0.0 if args.no_fail else float(np.clip(args.fail_rate, 0.0, 1.0))

    if args.parts.strip().lower() in ("all", ""):
        catalogue = sorted(PARTS)
    else:
        catalogue = [p.strip() for p in args.parts.split(",") if p.strip()]
        for p in catalogue:
            if p not in PARTS:
                raise SystemExit("printer: unknown part %r (have %s)"
                                 % (p, ", ".join(sorted(PARTS))))
    if not catalogue:
        catalogue = sorted(PARTS)

    # ----------------------------------------------------------------------
    # Geometry. Everything derives from the panel so --width 128 --height 32
    # is a smaller machine rather than a cropped one.
    # ----------------------------------------------------------------------
    plate_h = 3 if H >= 48 else 2
    base_h = 3 if H >= 48 else 2
    under = 2 if H >= 48 else 1
    bed_y = H - base_h - under - plate_h        # top row of the print surface
    rail_top = 1 if H >= 48 else 0
    rail_w = 3 if W >= 200 else 2

    # A spool needs somewhere to live outside the frame; only wide panels have
    # it, and the frame shifts right to make room.
    has_spool = W >= 200 and H >= 48
    rx0 = 34 if has_spool else 2                # left rail, first column
    rx1 = W - 2 - rail_w                        # right rail, first column
    bed_x0 = rx0 + rail_w + 2
    bed_x1 = rx1 - 3                            # inclusive

    noz_art = NOZZLE_BIG if H >= 48 else NOZZLE_SMALL
    noz_h = len(noz_art)
    noz_w = len(noz_art[0])
    noz_tipx = noz_w // 2
    beam_h = 3 if H >= 48 else 2

    # The beam must clear the top crossbar, which fixes how tall a part can be.
    y_tip_min = rail_top + 2 + beam_h + noz_h - 1
    max_ph = max(6, bed_y - y_tip_min)
    min_ph = max(6, int(0.55 * max_ph))
    max_pw = max(9, min((bed_x1 - bed_x0) // 4, int(1.6 * max_ph)))

    # Travel rates. Scaled with the panel so a small machine is not frantic.
    px_scale = max(0.42, W / 320.0)
    # Everything the spaghetti does is a length per second, so it has to be
    # measured against the height it falls through: a coil six pixels across
    # is a detail on 64 rows and fills a third of the panel on 32.
    hs = H / 64.0
    sweep_v = 118.0 * speed * px_scale          # px/s while extruding
    travel_v = 210.0 * speed * px_scale         # px/s while not

    purge_x0 = bed_x0 + 2
    purge_x1 = min(bed_x0 + 2 + max(14, (bed_x1 - bed_x0) // 6), bed_x1 - 4)

    # ----------------------------------------------------------------------
    # Static background: frame, rails, bed, base, spool bracket.
    # ----------------------------------------------------------------------
    bg = np.zeros((H, W, 3), np.uint8)
    frame_c = (46, 51, 62)
    frame_hi = (78, 86, 102)
    bg[rail_top:bed_y + plate_h, rx0:rx0 + rail_w] = frame_c
    bg[rail_top:bed_y + plate_h, rx1:rx1 + rail_w] = frame_c
    bg[rail_top:bed_y + plate_h, rx0] = frame_hi
    bg[rail_top:bed_y + plate_h, rx1] = frame_hi
    bg[rail_top:rail_top + 2, rx0:rx1 + rail_w] = frame_c
    bg[rail_top, rx0:rx1 + rail_w] = frame_hi
    # Base rail the whole machine stands on.
    bg[H - base_h:H, rx0:rx1 + rail_w] = (34, 38, 47)
    bg[H - base_h, rx0:rx1 + rail_w] = (60, 66, 80)
    # The bed: dark plate, one bright sheet row on top.
    bg[bed_y:bed_y + plate_h, bed_x0 - 2:bed_x1 + 3] = (44, 48, 58)
    bg[bed_y, bed_x0 - 2:bed_x1 + 3] = (96, 102, 114)
    bg[bed_y + plate_h - 1, bed_x0 - 2:bed_x1 + 3] = (66, 40, 30)   # heater
    # Bed carriage stubs.
    for xx in (bed_x0 + 6, (bed_x0 + bed_x1) // 2, bed_x1 - 6):
        bg[bed_y + plate_h:H - base_h, xx - 1:xx + 2] = (38, 42, 52)

    spool_r = 0
    spool_cx = spool_cy = 0
    spool_sprites = ()
    if has_spool:
        spool_r = min(11, (rx0 - 6) // 2, H // 5)
        spool_cx = max(spool_r + 2, (rx0 - 4) // 2)
        spool_cy = rail_top + spool_r + 4
        bg[spool_cy:H - base_h, spool_cx - 1:spool_cx + 2] = (40, 45, 55)
        bg[H - base_h:H, spool_cx - spool_r:spool_cx + spool_r + 1] = (34, 38, 47)
        # Eight rotation phases of the spool: rim, filament wound on it, and
        # spokes, baked once and blitted.
        yy, xx = np.mgrid[-spool_r:spool_r + 1, -spool_r:spool_r + 1]
        rr = np.sqrt(yy * yy + xx * xx).astype(f32)
        ang = np.arctan2(yy, xx).astype(f32)
        rim = (rr <= spool_r) & (rr > spool_r - 1.6)
        hub = rr <= 2.2
        sprites = []
        for k in range(8):
            phase = k * (np.pi / 12.0)
            spokes = (np.cos(3.0 * (ang + phase)) > 0.88) & (rr < spool_r - 1.6)
            wound = (rr <= spool_r - 2.0) & (rr > 3.0)
            msk = rim | hub | spokes | wound
            img = np.zeros(msk.shape + (3,), np.uint8)
            img[wound] = (96, 62, 30)
            img[spokes] = (120, 128, 145)
            img[rim] = (150, 158, 176)
            img[hub] = (70, 76, 90)
            sprites.append((img, msk))
        spool_sprites = tuple(sprites)

    # ----------------------------------------------------------------------
    # Lookup tables and stamps.
    # ----------------------------------------------------------------------
    # The heat ramp is indexed by a *gamma'd* heat so the glow falls away
    # quickly: with a linear index the whole part sits under a dull red wash
    # for ten seconds, and the top layer stops being special.
    _ramp = ds.gradient([(0.00, (0, 0, 0)), (0.16, (60, 6, 0)),
                         (0.42, (205, 58, 0)), (0.70, (255, 148, 30)),
                         (1.00, (255, 236, 172))], 256)
    HEAT_LUT = _ramp[(np.linspace(0.0, 1.0, 256) ** 2.0 * 255).astype(np.int32)]
    # Strand colour by how recently it left the nozzle: white hot to cold
    # filament (tinted in per print).
    STRAND_HOT = ds.gradient([(0.00, (52, 52, 52)), (0.35, (150, 90, 40)),
                              (0.70, (255, 150, 40)), (1.00, (255, 244, 210))],
                             64)

    halo_r = 5 if H >= 48 else 3
    hy, hx = np.mgrid[-halo_r:halo_r + 1, -halo_r:halo_r + 1]
    hd = np.sqrt(hy * hy + hx * hx).astype(f32)
    halo_f = np.exp(-(hd / (halo_r * 0.52)) ** 2)
    halo_f[halo_r, halo_r] = 1.0
    HALO = (halo_f[..., None] * np.array([255, 170, 60], f32)).clip(0, 255)
    HALO = HALO.astype(np.uint8)

    noz_rgb = np.zeros((noz_h, noz_w, 3), np.uint8)
    noz_msk = np.zeros((noz_h, noz_w), bool)
    for r, row in enumerate(noz_art):
        for c, ch in enumerate(row):
            if ch != ".":
                noz_rgb[r, c] = NOZZLE_COLOURS[ch]
                noz_msk[r, c] = True

    # ----------------------------------------------------------------------
    # Small helpers.
    # ----------------------------------------------------------------------
    def shift(a, dy, dx):
        """a shifted so out[y, x] = a[y - dy, x - dx], zero filled."""
        out = np.zeros_like(a)
        h, w = a.shape
        ys = slice(max(dy, 0), h + min(dy, 0))
        ys2 = slice(max(-dy, 0), h + min(-dy, 0))
        xs = slice(max(dx, 0), w + min(dx, 0))
        xs2 = slice(max(-dx, 0), w + min(-dx, 0))
        out[ys, xs] = a[ys2, xs2]
        return out

    def paste(buf, y0, x0, rgb, msk):
        """Blit a masked sprite, clipped to the panel."""
        h, w = msk.shape
        sy0, sx0 = max(0, -y0), max(0, -x0)
        dy0, dx0 = max(0, y0), max(0, x0)
        hh = min(h - sy0, H - dy0)
        ww = min(w - sx0, W - dx0)
        if hh <= 0 or ww <= 0:
            return
        m = msk[sy0:sy0 + hh, sx0:sx0 + ww]
        np.copyto(buf[dy0:dy0 + hh, dx0:dx0 + ww],
                  rgb[sy0:sy0 + hh, sx0:sx0 + ww], where=m[..., None])

    def sat_add(region, add):
        """region += add, saturating, staying uint8."""
        np.minimum(region, 255 - add, out=region)
        region += add

    def rasterize(name, ph):
        fn, aspect = PARTS[name]
        pw = max(5, int(round(ph * aspect)))
        u = np.linspace(-1.0, 1.0, pw, dtype=f32)
        v = np.linspace(1.0, 0.0, ph, dtype=f32)     # row 0 is the top
        U, V = np.meshgrid(u, v)
        m = np.asarray(fn(U, V, f32(aspect)), bool)
        rows = np.flatnonzero(m.any(axis=1))
        cols = np.flatnonzero(m.any(axis=0))
        if rows.size == 0 or cols.size == 0:
            return np.ones((ph, pw), bool)
        return m[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]

    def classify(m, kind, period, base):
        """Split a silhouette into perimeter / skin / infill and colour it.

        Perimeter is the mask minus its 4-neighbour erosion, so every hole is
        walled as well as the outside. Skin is anything within two rows of a
        horizontal surface, which is what gives solid tops and bottoms. The
        rest is where the infill stencil applies, and only the stencil is
        drawn -- that is what makes the cross-section a lattice.
        """
        ph, pw = m.shape
        eroded = (m & shift(m, 1, 0) & shift(m, -1, 0)
                  & shift(m, 0, 1) & shift(m, 0, -1))
        per = m & ~eroded
        skin = m & ~(shift(m, 2, 0) & shift(m, -2, 0))
        yy, xx = np.mgrid[0:ph, 0:pw].astype(f32)
        yy = (ph - 1) - yy                        # measure layers from the bed
        if kind == "gyroid":
            k = 6.2832 / max(6.0, period * 2.4)
            pat = np.abs(np.sin(xx * k) * np.cos(yy * k)) < 0.22
        elif kind == "lines":
            pat = ((xx + yy) % period) < 1.0
        else:
            pat = (((xx + yy) % period) < 1.0) | (((xx - yy) % period) < 1.0)
        inner = m & ~per & ~skin
        present = per | skin | (inner & pat)

        col = np.array(base, f32)
        rgb = np.zeros((ph, pw, 3), np.uint8)
        rgb[inner & pat] = np.clip(col * 0.58, 0, 255).astype(np.uint8)
        rgb[skin] = np.clip(col * 0.82, 0, 255).astype(np.uint8)
        rgb[per] = np.clip(col, 0, 255).astype(np.uint8)
        # Layer lines: alternate rows slightly darker, with a little jitter so
        # the banding is not a perfect comb.
        band = 0.74 + 0.26 * (np.arange(ph) % 2)
        band = band * rng.uniform(0.95, 1.05, ph)
        rgb = np.clip(rgb.astype(f32) * band[:, None, None], 0, 255).astype(np.uint8)
        return present, rgb

    # ----------------------------------------------------------------------
    # State.
    # ----------------------------------------------------------------------
    CAP = 900 if W >= 200 else 360
    st = SimpleNamespace(
        t=0.0,
        phase="prime", timer=0.0,
        nx=float(purge_x0), y_tip=bed_y - 1, sweep_dir=1, travelling=False,
        target=float(purge_x1),
        L=0, layer_t=0.0, ph=1, pw=1, px0=bed_x0, part_top=bed_y - 1,
        present=np.zeros((1, 1), bool), part_rgb=np.zeros((1, 1, 3), np.uint8),
        name=catalogue[0], base=FILAMENTS[0],
        fail_layer=-1, failed=False, fail_time=0.0, fail_rows=0,
        part_dx=0.0, part_skew=0.0, topple_dir=1,
        curl_phase=0.0, curl_rate=7.0, curl_until=0.0,
        clear_dx=0.0,
        heat=np.zeros((H, W), f32),
        bed_rgb=np.zeros((H, W, 3), np.uint8),
        bed_msk=np.zeros((H, W), bool),
        bb=[H, 0, W, 0],
        pile=np.full(W, float(bed_y), f32),
        pos=np.zeros((CAP, 2), f32), vel=np.zeros((CAP, 2), f32),
        cph=np.zeros(CAP, f32),
        live=np.zeros(CAP, bool), used=np.zeros(CAP, bool),
        link=np.zeros(CAP, bool), born=np.zeros(CAP, f32),
        wi=0, emit_acc=0.0,
        spool_phase=0.0,
    )
    buf = np.zeros((H, W, 3), np.uint8)
    seg_s = np.array([0.0, 0.34, 0.67], f32)

    def mark(y, x):
        bb = st.bb
        if y < bb[0]:
            bb[0] = y
        if y + 1 > bb[1]:
            bb[1] = y + 1
        if x < bb[2]:
            bb[2] = x
        if x + 1 > bb[3]:
            bb[3] = x + 1

    def bed_paint(ys, xs, colour):
        if ys.size == 0:
            return
        st.bed_msk[ys, xs] = True
        st.bed_rgb[ys, xs] = colour
        mark(int(ys.min()), int(xs.min()))
        mark(int(ys.max()), int(xs.max()))

    def new_print():
        st.phase = "prime"
        st.timer = 0.0
        st.nx = float(purge_x0)
        st.y_tip = bed_y - 1
        st.sweep_dir = 1
        st.travelling = False
        st.L = 0
        st.layer_t = 0.0
        st.part_dx = 0.0
        st.part_skew = 0.0
        st.clear_dx = 0.0
        st.failed = False
        st.fail_rows = 0
        st.topple_dir = 1
        st.heat[:] = 0.0
        st.bed_msk[:] = False
        st.bb = [H, 0, W, 0]
        st.pile[:] = float(bed_y)
        st.used[:] = False
        st.live[:] = False
        st.link[:] = False
        st.wi = 0
        st.emit_acc = 0.0

        choices = [c for c in catalogue if c != st.name] or catalogue
        st.name = choices[int(rng.integers(len(choices)))]
        st.base = FILAMENTS[int(rng.integers(len(FILAMENTS)))]

        # Pick a height, then shrink it if the silhouette would be too wide
        # for a quarter of the bed.
        ph = int(rng.integers(min_ph, max_ph + 1))
        aspect = PARTS[st.name][1]
        if ph * aspect > max_pw:
            ph = max(6, int(max_pw / aspect))
        mask = rasterize(st.name, ph)
        kind = ("grid", "grid", "lines", "gyroid")[int(rng.integers(4))]
        period = float(rng.integers(4, 8))
        st.present, st.part_rgb = classify(mask, kind, period, st.base)
        st.ph, st.pw = mask.shape
        st.part_top = bed_y - st.ph

        # Somewhere on the bed, clear of the purge line, different each time.
        lo = purge_x1 + 8 + st.pw // 2
        hi = bed_x1 - 4 - st.pw // 2
        cx = int(rng.integers(lo, hi + 1)) if hi > lo else (lo + hi) // 2
        st.px0 = int(np.clip(cx - st.pw // 2, bed_x0 + 1, bed_x1 - st.pw))

        # Two independent draws, and nothing else: whether, and how far up.
        # A print index or a counter here is what would make it predictable.
        if rng.random() < fail_rate:
            st.fail_layer = int(round(st.ph * rng.uniform(0.08, 0.94)))
            st.fail_layer = int(np.clip(st.fail_layer, 1, st.ph - 1))
            st.fail_time = float(rng.uniform(7.0, 13.0)) / speed
        else:
            st.fail_layer = -1

    def layer_span(row):
        """Sweep limits for one array row, widened so no layer is a flicker."""
        idx = np.flatnonzero(st.present[row])
        if idx.size == 0:
            return None
        a = st.px0 + int(idx[0]) - 2
        b = st.px0 + int(idx[-1]) + 2
        want = max(12.0, 10.0 * px_scale)
        if b - a < want:
            mid = 0.5 * (a + b)
            a, b = mid - want * 0.5, mid + want * 0.5
        return (float(np.clip(a, bed_x0, bed_x1)),
                float(np.clip(b, bed_x0, bed_x1)))

    def start_layer():
        st.layer_t = 0.0
        cur = st.ph - 1 - st.L
        if cur < 0:
            return
        span = layer_span(cur)
        if span is None:
            return
        a, b = span
        # Come in at whichever end is nearer, then sweep to the other, so a
        # widening part never leaves a stripe undrawn.
        if abs(st.nx - a) <= abs(st.nx - b):
            st.target, st.sweep_dir, near = b, 1, a
        else:
            st.target, st.sweep_dir, near = a, -1, b
        st.travelling = abs(st.nx - near) > 0.5
        st.travel_to = near

    st.travel_to = float(purge_x0)

    # ----------------------------------------------------------------------
    # Spaghetti.
    # ----------------------------------------------------------------------
    def bake(i):
        """Fold the segment leaving slot i into the static bed layer."""
        j = (i + 1) % CAP
        if not (st.used[j] and st.link[j]):
            return
        a, b = st.pos[i], st.pos[j]
        pts = a + (b - a) * seg_s[:, None]
        xs = np.clip(pts[:, 0].astype(np.int32), 0, W - 1)
        ys = np.clip(pts[:, 1].astype(np.int32), 0, H - 1)
        col = np.clip(np.array(st.base, f32) * 0.80, 0, 255).astype(np.uint8)
        bed_paint(ys, xs, col)
        st.heat[ys, xs] = np.maximum(st.heat[ys, xs], 0.26)   # the nest is warm

    def emit(x, y, vx, dt):
        rate = 170.0 * speed
        st.emit_acc += (rate + 0.20 * abs(vx)) * dt
        n = int(st.emit_acc)
        if n <= 0:
            return
        st.emit_acc -= n
        for _ in range(min(n, 12)):
            i = st.wi
            if st.used[i]:
                bake(i)
            st.pos[i] = (x, y)
            # Dragged sideways by the moving nozzle, pushed down by extrusion.
            # The jitter is deliberately tiny: points a few milliseconds apart
            # have to leave the tip on nearly the same trajectory or the
            # polyline between them is a spray of dots instead of a strand.
            st.vel[i] = (vx * 0.30 + rng.uniform(-2.0, 2.0),
                         rng.uniform(10.0, 16.0) * hs)
            # The coil phase is sampled from one running clock rather than
            # drawn per point. That is the whole trick: neighbours then differ
            # by a fixed phase offset and trace out the *same* curling curve,
            # which is what a strand of filament is. Independent phases give
            # confetti.
            st.cph[i] = st.curl_phase
            st.live[i] = True
            st.used[i] = True
            # A strand snaps now and then, which is what turns one long coil
            # into a tangle of separate loops. Both this and the change of
            # curl are per *second*, not per point: tie either to the
            # emission rate and a denser strand simply reverses direction
            # every few pixels, which averages the coil away into a wander.
            st.link[i] = rng.random() > (0.8 / rate)
            st.born[i] = st.t
            st.wi = (st.wi + 1) % CAP
            st.link[st.wi] = False        # the ring seam is never a segment

    def step_strands(dt):
        if st.t > st.curl_until:
            st.curl_rate = float(rng.uniform(6.0, 10.0)
                                 * (1 if rng.random() < 0.5 else -1))
            st.curl_until = st.t + float(rng.uniform(1.2, 3.0))
        st.curl_phase += st.curl_rate * dt
        lv = st.used & st.live
        n = int(lv.sum())
        if n == 0:
            return
        p = st.pos[lv]
        v = st.vel[lv]
        ph_ = st.cph[lv] + st.curl_rate * dt
        st.cph[lv] = ph_
        # A rotating acceleration, quarter turn out of phase between the two
        # axes, is what makes a falling strand *coil* instead of raining
        # down. It has to beat gravity or the loops open out into a wiggle,
        # which is why the fall is deliberately slow: heavy air drag, weak
        # gravity, and a curl fast enough to close two or three turns in the
        # time it takes to reach the bed.
        v[:, 0] += (380.0 * hs * np.cos(ph_)) * dt
        v[:, 1] += (hs * (56.0 + 380.0 * np.sin(ph_))) * dt
        v *= f32(0.93) ** f32(dt * 60.0)
        p += v * dt
        np.clip(p[:, 0], bed_x0 + 1, bed_x1 - 1, out=p[:, 0])
        xi = p[:, 0].astype(np.int32)
        floor = st.pile[xi]
        hit = p[:, 1] >= floor
        if hit.any():
            p[hit, 1] = floor[hit]
            v[hit] = 0.0
            # Scatter sideways as it lands, so the heap is loose rather
            # than a stack of columns.
            hx = np.clip(xi[hit] + rng.integers(-1, 2, int(hit.sum())),
                         bed_x0 + 1, bed_x1 - 1)
            p[hit, 0] = hx
            # The pile spreads into its neighbours as it grows, or a busy
            # column turns into a single tall spike instead of a mound.
            np.subtract.at(st.pile, hx, 0.60)
            for off, amt in ((1, 0.22), (2, 0.10)):
                np.subtract.at(st.pile, np.clip(hx - off, 0, W - 1), amt)
                np.subtract.at(st.pile, np.clip(hx + off, 0, W - 1), amt)
            np.maximum(st.pile, float(bed_y - max(5, max_ph // 3)), out=st.pile)
            live = st.live[lv]
            live[hit] = False
            st.live[lv] = live
        st.pos[lv] = p
        st.vel[lv] = v

    def draw_strands(dst):
        i = np.flatnonzero(st.used & st.link)
        if i.size == 0:
            return
        a = st.pos[i - 1]
        b = st.pos[i]
        # A long segment is never real filament -- it is the join across a
        # point that settled while its neighbour was still falling. Drawn, it
        # reads as a taut wire strung through the nest.
        keep = np.abs(b - a).max(axis=1) < max(7.0, 14.0 * hs)
        if not keep.all():
            i, a, b = i[keep], a[keep], b[keep]
            if i.size == 0:
                return
        pts = a[:, None, :] + (b - a)[:, None, :] * seg_s[None, :, None]
        xs = np.clip(pts[..., 0].astype(np.int32), 0, W - 1).ravel()
        ys = np.clip(pts[..., 1].astype(np.int32), 0, H - 1).ravel()
        # Colour by how long ago it left the nozzle: white hot at the tip,
        # through orange, to cold filament in the nest.
        age = np.clip((st.t - st.born[i]) * 0.55, 0.0, 1.0)
        hot = ((1.0 - age) * 63.0).astype(np.int32)
        cols = STRAND_HOT[hot].astype(f32)
        cold = np.array(st.base, f32) * 0.86
        k = (1.0 - age)[:, None] ** 1.5
        mix = (cols * k + cold * (1.0 - k)).astype(np.uint8)
        dst[ys, xs] = np.repeat(mix, seg_s.size, axis=0)

    # ----------------------------------------------------------------------
    # The state machine. Every phase is bounded in time, so nothing can wedge:
    # a print that somehow stops making progress still ages out into cooling.
    # ----------------------------------------------------------------------
    def topple(dt):
        """Drag the part off its footing once it lets go."""
        if st.part_skew < 1.0:
            st.part_skew = min(1.0, st.part_skew + dt * 1.1)
            st.part_dx += dt * 13.0 * st.topple_dir * px_scale
        lo = int(np.clip(st.px0 + st.part_dx - 3, 0, W - 1))
        hi = int(np.clip(st.px0 + st.pw + st.part_dx + 3, 1, W))
        top = bed_y - max(2, int(st.fail_rows * 0.55))
        np.minimum(st.pile[lo:hi], float(top), out=st.pile[lo:hi])

    def step(dt):
        st.timer += dt
        st.spool_phase += dt * 3.0 * speed

        if st.phase == "prime":
            # The prime line every printer draws down one edge of the bed.
            prev = st.nx
            st.nx = min(st.nx + travel_v * 0.55 * dt, purge_x1)
            lo, hi = int(prev), int(st.nx) + 1
            if hi > lo:
                xs = np.arange(max(lo, 0), min(hi, W))
                ys = np.full(xs.size, bed_y - 1)
                bed_paint(ys, xs, np.clip(np.array(st.base, f32) * 0.85, 0, 255)
                          .astype(np.uint8))
                st.heat[bed_y - 1, max(lo, 0):min(hi, W)] = 1.0
            st.y_tip = bed_y - 1
            if st.nx >= purge_x1 - 0.01 or st.timer > 12.0 / speed:
                st.phase = "print"
                st.timer = 0.0
                st.L = 0
                start_layer()
            return

        if st.phase == "print":
            cur = st.ph - 1 - st.L
            st.y_tip = bed_y - 1 - st.L
            st.layer_t += dt
            if cur < 0:
                st.phase = "cool"
                st.timer = 0.0
                return
            if st.travelling:
                d = travel_v * dt
                if abs(st.travel_to - st.nx) <= d:
                    st.nx = st.travel_to
                    st.travelling = False
                else:
                    st.nx += d * (1 if st.travel_to > st.nx else -1)
                return
            prev = st.nx
            st.nx += st.sweep_dir * sweep_v * dt
            done = (st.nx >= st.target) if st.sweep_dir > 0 else (st.nx <= st.target)
            if done:
                st.nx = st.target
            # Material appears only over the span the nozzle just crossed.
            lo = int(np.clip(min(prev, st.nx), st.px0, st.px0 + st.pw))
            hi = int(np.clip(max(prev, st.nx), st.px0, st.px0 + st.pw)) + 1
            if hi > lo:
                seg = st.present[cur, lo - st.px0:hi - st.px0]
                if seg.size:
                    row = st.heat[st.y_tip, lo:lo + seg.size]
                    row[seg] = 1.0
            if done and st.layer_t >= 0.10 / speed:
                st.L += 1
                if st.fail_layer >= 0 and st.L >= st.fail_layer:
                    st.phase = "fail"
                    st.failed = True
                    st.fail_rows = st.L
                    st.topple_dir = st.sweep_dir
                    st.timer = 0.0
                    # The part is no longer under the nozzle, so the warmth
                    # it left behind must go with it rather than hang in the
                    # air as a red smear where it used to be.
                    st.heat[:] = 0.0
                    return
                if st.L >= st.ph:
                    st.phase = "cool"
                    st.timer = 0.0
                    return
                start_layer()
            elif st.timer > 90.0 / speed:      # belt and braces
                st.phase = "cool"
                st.timer = 0.0
            return

        if st.phase == "fail":
            topple(dt)
            # The machine has no idea. It keeps climbing and sweeping.
            if st.layer_t >= 0.34 / speed:
                st.layer_t = 0.0
                st.L = min(st.L + 1, bed_y - 1 - y_tip_min)
            st.layer_t += dt
            st.y_tip = max(y_tip_min, bed_y - 1 - st.L)
            span_lo = max(bed_x0 + 2, st.px0 - 14)
            span_hi = min(bed_x1 - 2, st.px0 + st.pw + 14)
            prev = st.nx
            # Slower than a real print pass on purpose: at full sweep speed
            # the strand is stretched into one long line instead of piling.
            st.nx += st.sweep_dir * sweep_v * 0.55 * dt
            if st.nx >= span_hi:
                st.nx, st.sweep_dir = span_hi, -1
            elif st.nx <= span_lo:
                st.nx, st.sweep_dir = span_lo, 1
            emit(st.nx, st.y_tip + 1.0, (st.nx - prev) / max(dt, 1e-4), dt)
            step_strands(dt)
            if st.timer > st.fail_time:
                st.phase = "cool"
                st.timer = 0.0
            return

        if st.phase == "cool":
            step_strands(dt)
            # Home the head out of the way while the part cools.
            tx, ty = float(bed_x0 + 6), float(y_tip_min)
            st.nx += np.clip(tx - st.nx, -travel_v * dt, travel_v * dt)
            st.y_tip = int(round(st.y_tip + np.clip(ty - st.y_tip,
                                                    -travel_v * dt, travel_v * dt)))
            if st.timer > 4.0 / speed:
                st.phase = "clear"
                st.timer = 0.0
            return

        # clear: everything on the bed slides off to the right.
        st.clear_dx += dt * 210.0 * speed * px_scale
        st.heat *= f32(0.5) ** f32(dt / 0.25)
        if st.clear_dx > (W - st.px0) + st.pw + 40 or st.timer > 6.0 / speed:
            new_print()

    # ----------------------------------------------------------------------
    # Drawing.
    # ----------------------------------------------------------------------
    def draw_part(dst):
        # Once it has let go, the part stops growing: freeze the row count at
        # the failure layer rather than tracking the nozzle, which keeps
        # climbing.
        rows = st.fail_rows if st.failed else min(st.L, st.ph)
        if rows <= 0:
            return
        dx = int(round(st.part_dx + st.clear_dx))
        if st.part_skew <= 0.0:
            src_r = st.part_rgb[st.ph - rows:st.ph]
            src_m = st.present[st.ph - rows:st.ph]
            paste(dst, bed_y - rows, st.px0 + dx, src_r, src_m)
            return
        # Toppled: horizontal bands, each dragged a little further than the
        # one below it, which reads as a lean without needing a rotation.
        nb = min(6, max(2, rows // 3))
        lean = st.part_skew * 1.7 * st.topple_dir
        for k in range(nb):
            r0 = st.ph - rows + (rows * k) // nb
            r1 = st.ph - rows + (rows * (k + 1)) // nb
            if r1 <= r0:
                continue
            off = dx + int(round(lean * (nb - k)))
            paste(dst, bed_y - rows + (rows * k) // nb, st.px0 + off,
                  st.part_rgb[r0:r1], st.present[r0:r1])

    def draw_bed_layer(dst):
        y0, y1, x0, x1 = st.bb
        if y1 <= y0:
            return
        dx = int(round(st.clear_dx))
        dx0 = x0 + dx
        dx1 = x1 + dx
        sx0 = x0 + max(0, -dx0)
        dx0 = max(0, dx0)
        dx1 = min(W, dx1)
        if dx1 <= dx0:
            return
        w = dx1 - dx0
        m = st.bed_msk[y0:y1, sx0:sx0 + w]
        np.copyto(dst[y0:y1, dx0:dx0 + w], st.bed_rgb[y0:y1, sx0:sx0 + w],
                  where=m[..., None])

    def draw_machine(dst):
        y_beam = st.y_tip - noz_h - beam_h + 1
        y_beam = max(rail_top + 2, y_beam)
        dst[y_beam:y_beam + beam_h, rx0:rx1 + rail_w] = (58, 64, 78)
        dst[y_beam, rx0:rx1 + rail_w] = (104, 112, 130)
        cx = int(round(st.nx))
        cw = noz_w + 4
        c0 = int(np.clip(cx - cw // 2, 0, W - cw))
        dst[y_beam - 1:y_beam + beam_h + 1, c0:c0 + cw] = (86, 94, 112)
        dst[y_beam - 1, c0:c0 + cw] = (140, 150, 172)
        paste(dst, st.y_tip - noz_h + 1, cx - noz_tipx, noz_rgb, noz_msk)
        return y_beam

    def draw_filament(dst, y_beam):
        if not has_spool:
            return
        ph_i = int(st.spool_phase) % 8
        img, msk = spool_sprites[ph_i]
        paste(dst, spool_cy - spool_r, spool_cx - spool_r, img, msk)
        # A drooping loop from the spool to the carriage, which moves with it.
        ax, ay = spool_cx + spool_r, spool_cy
        bx, by = float(st.nx), float(y_beam - 1)
        # Sample by path length, not a fixed count, or the drape breaks into
        # a dotted line as the carriage runs away to the far end.
        n = int(np.clip(1.6 * (abs(bx - ax) + abs(by - ay)) + 24, 24, 700))
        s = np.linspace(0.0, 1.0, n, dtype=f32)
        sag = min(0.34 * abs(bx - ax), 26.0) + 12.0
        cxp = 0.5 * (ax + bx)
        cyp = 0.5 * (ay + by) + sag * 0.45
        om = 1.0 - s
        xs = om * om * ax + 2 * om * s * cxp + s * s * bx
        ys = om * om * ay + 2 * om * s * cyp + s * s * by
        xi = np.clip(xs.astype(np.int32), 0, W - 1)
        yi = np.clip(ys.astype(np.int32), 0, H - 1)
        dst[yi, xi] = np.clip(np.array(st.base, f32) * 0.7, 0, 255).astype(np.uint8)

    def render(t, frame):
        dt = t - st.t
        st.t = t
        if dt < 0.0 or dt > 0.1:
            dt = min(max(dt, 0.0), 0.1)

        step(dt)
        if st.phase != "clear":
            st.heat *= f32(0.5) ** f32(dt / 1.6)

        np.copyto(buf, bg)
        draw_bed_layer(buf)
        draw_part(buf)
        if st.phase in ("fail", "cool"):
            draw_strands(buf)

        # One whole-frame pass: the heat field, as a LUT gather and a
        # saturating add. Fresh extrusion goes white, a second later orange,
        # and after a few seconds it is just filament colour.
        glow = HEAT_LUT[(st.heat * 255.0).astype(np.uint8)]
        sat_add(buf, glow)

        y_beam = draw_machine(buf)
        # The halo is a stamp, not a blur: on the Pi a whole-frame blur costs
        # more than the rest of the demo put together.
        if st.phase in ("prime", "print", "fail"):
            hy0 = st.y_tip - halo_r
            hx0 = int(round(st.nx)) - halo_r
            sy0, sx0 = max(0, -hy0), max(0, -hx0)
            dy0, dx0 = max(0, hy0), max(0, hx0)
            hh = min(HALO.shape[0] - sy0, H - dy0)
            ww = min(HALO.shape[1] - sx0, W - dx0)
            if hh > 0 and ww > 0:
                sat_add(buf[dy0:dy0 + hh, dx0:dx0 + ww],
                        HALO[sy0:sy0 + hh, sx0:sx0 + ww])
        draw_filament(buf, y_beam)
        return buf

    new_print()
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
