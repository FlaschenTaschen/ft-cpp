#!/usr/bin/env python3
"""The trench run: a rectangular canyon rushing past, and the targeting computer.

Star Wars, 1977. Two walls and a floor converging on a vanishing point dead
centre, studded with greebles, with the orange wireframe of the targeting
computer swinging down over the view, locking on, and then swinging away again
so the pilot can finish the run on instinct.

A 320x64 panel is almost exactly the shape of that shot. A round tunnel wastes
a 5:1 letterbox -- the corners have nothing in them -- but a rectangular trench
fills it: the two wall-top lines and the two floor lines leave the vanishing
point as an X, the walls own the left and right thirds, and the only sky is a
narrow wedge above the middle. That wedge is what makes it read as a canyon
rather than as a scrolling texture.

**The geometry is one gather.** For every pixel, build() works out which of the
four surfaces its ray hits (sky, left wall, right wall, floor), where across
that surface it lands, and how deep. None of that depends on time. Each surface
is stored twice back to back in one texture, so flying forward is a single
integer add to a precomputed flat index and one np.take -- no per-pixel ray
march, no per-frame trigonometry. Depth fog is a second precomputed per-pixel
array and one multiply. Three passes over the frame, total.

**Roll is baked, not computed.** The ship rolls and shakes, and rotating the
inverse map per frame would cost more than everything else here put together.
Instead build() bakes the map at a handful of roll angles and render() picks the
nearest; the maps carry a few pixels of padding on every side, so camera shake
is a slice offset and costs nothing at all. The whole thing is a pure function
of t, which is what lets ftsched start it at t=0 and the preview baker step it.

**The computer is an instrument, not a filter.** It is baked once as a small RGB
strip and composited with np.maximum at whatever row the swing has reached, so
the trench stays visible behind the wireframe -- which is the entire reason the
palette is cold grey-blue below and saturated amber on top. The blips and the
lock flash are a dozen rectangle writes.

The 45 s cycle is one complete run: trench alone, computer down and locking,
blips converging, computer away, the shot, and the exhaust port going up.

Run:  python3 trench.py --host 127.0.0.1
      python3 trench.py --speed 340 --shake 1.6
      python3 trench.py --no-computer --seed 7
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

TEXU = 64                      # across a surface: wall height, or floor width
TEXV = 256                     # along the trench; power of two so it wraps
NSURF = 4                      # 0 sky, 1 left wall, 2 right wall, 3 floor
PAD = 5                        # spare pixels round the inverse map, for shake

# Trench cross-section, in units of the half-width. The camera sits a little
# above the middle: more floor than sky is what a low, fast pass looks like.
HW = 1.0
FLOOR_Y = 0.55                 # floor, below the camera
TOP_Y = 0.80                   # wall rim, above the camera
VSCALE = 42.0                  # texels of texture per unit of depth
ZMAX = 3000.0                  # clamp before the int cast at the vanishing point

# Cold structural greys for the trench, warm amber for anything that emits.
SLATE = (15, 19, 27)
PANEL = (31, 39, 52)
RIB = (72, 86, 110)
RIM = (120, 140, 172)
RECESS = (6, 8, 12)
LAMP_A = (255, 150, 40)
LAMP_C = (90, 190, 235)

# The instrument. Two tones only: a dim bezel and a hot reticle, because a
# wireframe with a gradient in it stops looking like a vector display.
ORANGE = (255, 146, 26)
AMBER = (150, 82, 12)

COMP_H = 50                    # rows of the baked overlay strip


def smoothstep(a, b, x):
    """0 below a, 1 above b, eased in between."""
    u = min(max((x - a) / (b - a), 0.0), 1.0) if b != a else float(x >= b)
    return u * u * (3.0 - 2.0 * u)


def rect(tex, v0, dv, u0, du, col):
    """Paint a block into a surface texture, wrapping in depth, clipping across.

    Depth wraps because the texture tiles as you fly; the across axis is a real
    edge (the trench rim, the corner where wall meets floor) and must not.
    """
    u0 = max(0, min(TEXU - 1, u0))
    u1 = max(0, min(TEXU, u0 + du))
    if u1 <= u0 or dv <= 0:
        return
    vi = np.arange(v0, v0 + dv) % TEXV
    tex[vi[:, None], np.arange(u0, u1)[None, :]] = np.asarray(col, f32)


def hline(img, y, x0, x1, col):
    img[y, x0:x1] = col


def vline(img, x, y0, y1, col):
    img[y0:y1, x] = col


def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=250.0,
                    help="forward texels/sec at the start of the run; it "
                         "builds by about half as much again by the end")
    ap.add_argument("--cycle", type=float, default=45.0,
                    help="seconds for one complete run, hit and reset")
    ap.add_argument("--greebles", type=float, default=1.0,
                    help="density of the surface detail, 0 leaves bare panels")
    ap.add_argument("--shake", type=float, default=1.0,
                    help="camera roll and knock, 0..2")
    ap.add_argument("--fog-scale", type=float, default=200.0,
                    help="depth in texels at which the walls are half faded")
    ap.add_argument("--no-computer", dest="computer", action="store_false",
                    default=True,
                    help="leave the targeting display switched off throughout")
    ap.add_argument("--no-lasers", dest="lasers", action="store_false",
                    default=True, help="no cannon fire")
    ap.add_argument("--focal", type=float, default=0.30,
                    help="focal length as a fraction of the width; larger "
                         "narrows the field of view and flattens the walls")
    ap.add_argument("--seed", type=int, default=1977,
                    help="greeble layout and the pattern of fire; 0 is random")


# --------------------------------------------------------------------------
# The trench surfaces.
# --------------------------------------------------------------------------

def make_textures(rng, density):
    """(NSURF, TEXV, TEXU, 3) float: sky palette, two walls, floor.

    The ribs -- the transverse structural bands -- are placed once and used at
    the same depths on all three solid surfaces, so they read as rings round
    the cross-section rather than as three independent scrolls. That is most of
    what sells the walls and the floor as one object.
    """
    tex = np.zeros((NSURF, TEXV, TEXU, 3), f32)
    ribs = np.arange(0, TEXV, 24)

    for surf in (1, 2, 3):
        this = tex[surf]
        this[:] = np.asarray(SLATE, f32)

        # Blocky plate variation, not smooth noise: 4x8 cells quantised to a
        # few shades. Hard edges are the difference between panelling and a
        # texture wash, and a wash is what tunnel.py already does.
        cells = rng.integers(0, 4, (TEXV // 8, TEXU // 4))
        plate = np.repeat(np.repeat(cells, 8, 0), 4, 1).astype(f32)
        this += plate[:, :, None] * f32(2.6)

        # Longitudinal seams: panel joins running away down the trench.
        for u in rng.choice(np.arange(3, TEXU - 3), size=7, replace=False):
            this[:, u] = np.asarray(PANEL, f32)

        # A lit leading edge and a shadow behind it, not a plain stripe: the
        # asymmetry is what gives the ribs a direction as they sweep past, and
        # it is most of the sense of speed on a panel this shallow.
        for v in ribs:
            rect(this, v, 1, 0, TEXU, RIM)
            rect(this, v + 1, 2, 0, TEXU, RIB)
            rect(this, v + 3, 2, 0, TEXU, RECESS)

        n = int(70 * max(density, 0.0))
        for _ in range(n):
            dv = int(rng.integers(3, 22))
            du = int(rng.integers(2, 11))
            v0 = int(rng.integers(0, TEXV))
            u0 = int(rng.integers(0, TEXU - du))
            roll = rng.random()
            if roll < 0.34:
                col = RECESS                       # a hatch sunk into the wall
            elif roll < 0.78:
                col = PANEL
            else:
                col = RIB                          # a raised block catching light
            rect(this, v0, dv, u0, du, col)
            # A lit top edge turns a flat rectangle into something with relief.
            if roll >= 0.78:
                rect(this, v0, 1, u0, du, RIM)

        # Gun emplacements: a big recessed block with a couple of warm lamps in
        # it. Rare, so that spotting one is an event as the wall goes past.
        for _ in range(int(5 * max(density, 0.0))):
            v0 = int(rng.integers(0, TEXV))
            u0 = int(rng.integers(2, TEXU - 14))
            rect(this, v0, 14, u0, 12, RECESS)
            rect(this, v0, 1, u0, 12, RIM)
            rect(this, v0 + 5, 3, u0 + 3, 3, LAMP_A)
            rect(this, v0 + 5, 3, u0 + 7, 2, LAMP_C)

        for _ in range(int(26 * max(density, 0.0))):
            rect(this, int(rng.integers(0, TEXV)), int(rng.integers(1, 3)),
                 int(rng.integers(1, TEXU - 2)), 1,
                 LAMP_A if rng.random() < 0.6 else LAMP_C)

    # The rim of the trench, and the corner where the wall meets the floor.
    # Without a bright line at u = 0 the wall tops dissolve into the sky and
    # the whole canyon reads as a flat backdrop.
    for surf in (1, 2):
        tex[surf][:, 0] = np.asarray(RIM, f32)
        tex[surf][:, 1] = np.asarray(RIB, f32) * f32(0.7)
        tex[surf][:, TEXU - 1] = np.asarray(RECESS, f32)

    # The floor gets a centre channel: a target line down the middle of the
    # trench, which is what the run is following.
    tex[3][:, TEXU // 2 - 1:TEXU // 2 + 1] = np.asarray(PANEL, f32)
    tex[3][:, 0] = np.asarray(RECESS, f32)
    tex[3][:, TEXU - 1] = np.asarray(RECESS, f32)

    # Key the two walls apart. Equal lighting on both makes the corridor look
    # like a mirror trick; a stop of difference makes it look like a place.
    tex[1] *= f32(1.18)
    tex[2] *= f32(0.80)
    tex[3] *= f32(0.62)

    # Surface 0 is the sky, and every row of it is the same 64-entry palette:
    # indices 0..47 are a gradient by screen row, 56..63 are stars. Holding it
    # constant along the depth axis is what lets the sky come out of the same
    # gather as the walls without the scroll dragging it.
    sky = np.zeros((TEXU, 3), f32)
    ramp = np.linspace(0.0, 1.0, 48, dtype=f32)[:, None]
    sky[:48] = np.asarray((2, 3, 6), f32) + np.asarray((7, 9, 16), f32) * ramp
    sky[56:] = np.linspace(70, 210, 8, dtype=f32)[:, None] * np.asarray(
        (0.86, 0.92, 1.0), f32)
    tex[0][:] = sky[None, :, :]
    return np.clip(tex, 0, 255)


def build_maps(args, rng, rolls):
    """Per roll angle: the flat texture index and the fog for every pixel.

    Padded by PAD on all four sides, so render() can shake the camera by
    slicing at an offset instead of rebuilding anything.
    """
    W, H = args.width, args.height
    focal = args.focal * W
    ph, pw = H + 2 * PAD, W + 2 * PAD
    cx, cy = W / 2.0, H / 2.0

    ys = (np.arange(ph, dtype=f32) - PAD - cy + 0.5)[:, None] / focal
    xs = (np.arange(pw, dtype=f32) - PAD - cx + 0.5)[None, :] / focal

    # The sky, once: a gradient by row plus sparse stars. Both are fixed to the
    # panel rather than to the camera, which nobody can tell at this density
    # and which saves a map per roll angle.
    grad = np.clip((np.arange(ph, dtype=f32) - PAD) / max(H - 1, 1) * 47.0,
                   0, 47).astype(np.int32)
    sky_u = np.repeat(grad[:, None], pw, axis=1)
    stars = rng.random((ph, pw)) < 0.010
    sky_u[stars] = 56 + rng.integers(0, 8, int(stars.sum()))

    big = f32(1e9)
    flats, fogs = [], []
    for ang in rolls:
        ca, sa = f32(np.cos(ang)), f32(np.sin(ang))
        rx = xs * ca - ys * sa
        ry = xs * sa + ys * ca
        rx = np.broadcast_to(rx, (ph, pw)).copy()
        ry = np.broadcast_to(ry, (ph, pw)).copy()

        # Each surface is a plane; the ray hits it at one depth. Only the hits
        # that land inside the surface's extent count, and the nearest wins.
        hit_z = np.full((ph, pw), big, f32)
        hit_s = np.zeros((ph, pw), np.int32)
        hit_u = np.zeros((ph, pw), f32)

        for sign, surf in ((-1.0, 1), (1.0, 2)):
            side = rx * sign > 1e-5
            z = np.where(side, HW / np.where(side, np.abs(rx), 1.0), big)
            yh = ry * z
            ok = side & (yh >= -TOP_Y) & (yh <= FLOOR_Y) & (z < hit_z)
            u = (yh + TOP_Y) / (TOP_Y + FLOOR_Y) * (TEXU - 1)
            hit_z = np.where(ok, z, hit_z)
            hit_u = np.where(ok, u, hit_u)
            hit_s = np.where(ok, surf, hit_s)

        down = ry > 1e-5
        z = np.where(down, FLOOR_Y / np.where(down, ry, 1.0), big)
        xh = rx * z
        ok = down & (np.abs(xh) <= HW) & (z < hit_z)
        u = (xh + HW) / (2.0 * HW) * (TEXU - 1)
        hit_z = np.where(ok, z, hit_z)
        hit_u = np.where(ok, u, hit_u)
        hit_s = np.where(ok, 3, hit_s)

        solid = hit_s > 0
        depth = np.clip(hit_z, 0.0, ZMAX) * f32(VSCALE)
        v0 = np.where(solid, depth.astype(np.int64) % TEXV, 0).astype(np.int64)
        uu = np.where(solid, np.clip(hit_u, 0, TEXU - 1), sky_u).astype(np.int64)

        # Every surface is stored twice, back to back, so adding the scroll
        # offset (always < TEXV) to a row index in [0, TEXV) can never leave
        # the surface's block. That is what makes flying forward a single add.
        flat = ((hit_s.astype(np.int64) * (2 * TEXV) + v0) * TEXU + uu)
        flats.append(flat.astype(np.int32))

        # Raised to a power, and not gently. A plain 1/(1+z) still leaves a
        # quarter of the brightness at the vanishing point, and since one
        # screen pixel there spans dozens of texels what survives is a cloud of
        # random bright samples -- noise, exactly where the eye is looking.
        lit = (args.fog_scale / (args.fog_scale + depth)) ** 2.1
        fogs.append(np.where(solid, lit, 1.0).astype(f32))
    return flats, fogs


# --------------------------------------------------------------------------
# The instrument.
# --------------------------------------------------------------------------

def comp_half(width):
    """Half-width of the instrument bezel. Narrower than the panel, so the
    trench still runs past outside it and the display reads as something in
    front of the view rather than as a border round the whole frame."""
    return max(24, min(118, width // 2 - 4))


def make_computer(width):
    """The targeting display, baked as a (COMP_H, width, 3) strip.

    Everything static lives here: bezel, corner ticks, the reticle brackets,
    the crosshair arms and the scale along the bottom. The blips and the lock
    flash are drawn per frame because they move.
    """
    img = np.zeros((COMP_H, width, 3), f32)
    cx = width // 2
    half = comp_half(width)
    x0, x1 = cx - half, cx + half
    y0, y1 = 3, COMP_H - 4

    hline(img, y0, x0, x1, AMBER)
    hline(img, y1, x0, x1, AMBER)
    vline(img, x0, y0, y1, AMBER)
    vline(img, x1 - 1, y0, y1, AMBER)
    # Corner ticks in the hot colour: the bezel is dim so it does not fight
    # the reticle, and the corners are what stop it reading as a plain box.
    for sx, sy in ((x0, y0), (x1 - 1, y0), (x0, y1), (x1 - 1, y1)):
        hline(img, sy, min(sx, sx - 6), max(sx + 1, sx + 7), ORANGE)
        vline(img, sx, min(sy, sy - 5), max(sy + 1, sy + 6), ORANGE)

    my = COMP_H // 2
    # Reticle: four corner brackets, open in the middle of every side, which is
    # how the prop reads at a glance and how a closed box does not.
    bx, by = 22, 11
    for dx in (-1, 1):
        for dy in (-1, 1):
            ex, ey = cx + dx * bx, my + dy * by
            hline(img, ey, min(ex, ex - dx * 9), max(ex + 1, ex + 1 - dx * 9),
                  ORANGE)
            vline(img, ex, min(ey, ey - dy * 6), max(ey + 1, ey + 1 - dy * 6),
                  ORANGE)

    # Crosshair arms, standing off from the reticle so the centre stays clear
    # for the trench to show through.
    vline(img, cx, my - by - 7, my - by - 1, ORANGE)
    vline(img, cx, my + by + 2, my + by + 8, ORANGE)
    hline(img, my, cx - bx - 16, cx - bx - 3, ORANGE)
    hline(img, my, cx + bx + 4, cx + bx + 17, ORANGE)

    # The scale the blips run along, and label blocks in the corners.
    for x in range(x0 + 8, x1 - 6, 8):
        img[y1 - 4:y1 - 1, x] = AMBER
    for x in range(x0 + 8, x1 - 6, 32):
        img[y1 - 7:y1 - 1, x] = ORANGE
    for k in range(5):
        img[y0 + 3:y0 + 6, x0 + 5 + 5 * k:x0 + 8 + 5 * k] = AMBER
    for k in range(3):
        img[y0 + 3:y0 + 6, x1 - 20 + 6 * k:x1 - 17 + 6 * k] = AMBER
    return np.clip(img, 0, 255)


def build(args):

    W, H = args.width, args.height
    focal = args.focal * W
    cx, cy = W / 2.0, H / 2.0
    total = max(args.cycle, 8.0)
    beat = total / 45.0                    # the schedule below is written for 45 s
    rng = np.random.default_rng(args.seed or None)

    tex = make_textures(rng, args.greebles)
    # Two copies of every surface, back to back. See build_maps() on why.
    doubled = np.concatenate([np.concatenate([tex[s], tex[s]], axis=0)
                              for s in range(NSURF)], axis=0)
    atlas = doubled.reshape(-1, 3).astype(np.uint8)

    swing = float(np.clip(args.shake, 0.0, 2.0))
    roll_max = 0.075 * swing
    rolls = np.linspace(-roll_max, roll_max, 13) if roll_max > 1e-4 else [0.0]
    nroll = len(rolls)
    maps, fogmaps = build_maps(args, rng, rolls)

    comp = make_computer(W)
    # Six pre-scaled copies rather than a multiply per frame: the swing and the
    # flicker only ever need a brightness, and picking one is free.
    comp_lv = [np.clip(comp * (k / 5.0), 0, 255).astype(np.uint8)
               for k in range(6)]
    blip_col = np.asarray(ORANGE, np.uint8)
    dim_col = np.asarray(AMBER, np.uint8)
    blip_far = max(26, comp_half(W) - 16)      # where the blips start out
    lock_x = min(26, max(4, W // 2 - 2))       # the lock verticals, just outside

    # Radial falloff at the vanishing point, for the exhaust port going up.
    gy = (np.arange(H, dtype=f32) - cy)[:, None]
    gx = (np.arange(W, dtype=f32) - cx)[None, :]
    glow = np.exp(-(gx * gx + gy * gy) / f32(2.0 * 15.0 ** 2)).astype(f32)
    hot = np.asarray((255, 205, 120), f32)

    # ------------------------------------------------------------- schedule
    t_fade = 1.5 * beat                    # up from black
    t_down = 8.0 * beat                    # computer swings into view
    t_lock = 9.8 * beat                    # brackets snap, blips start closing
    t_conv = 25.5 * beat                   # blips reach the centre
    t_up = 26.2 * beat                     # and it swings away again
    t_fire = 36.5 * beat                   # torpedoes away
    t_hit = 38.6 * beat                    # impact at the vanishing point
    t_peak = 39.3 * beat
    t_dark = 43.0 * beat                   # black, holding to the loop point
    dur_down, dur_up = 1.1 * beat, 1.2 * beat

    # ---------------------------------------------------------------- fire
    # Baked once: each bolt is a lateral position and a depth window, so where
    # it is on screen at time t follows from t alone. Two dozen entries is few
    # enough that render() can simply walk the list.
    bolts = []
    if args.lasers:
        n_enemy = 26
        starts = np.sort(rng.uniform(2.0, t_fire / beat - 1.5, n_enemy)) * beat
        for k in range(n_enemy):
            side = 1.0 if rng.random() < 0.5 else -1.0
            bolts.append((float(starts[k]), 0.62 * beat,
                          side * float(rng.uniform(0.35, 0.95)),
                          float(rng.uniform(-0.55, 0.45)),
                          0.55, 26.0, (110, 255, 130)))
        # The pilot's own cannon: paired bolts from below the camera, in bursts.
        for burst in np.linspace(4.0, t_fire / beat - 3.0, 7) * beat:
            for k in range(2):
                for side in (-1.0, 1.0):
                    bolts.append((float(burst) + 0.16 * k * beat, 0.75 * beat,
                                  side * 0.30, 0.44, 0.5, 30.0, (255, 78, 52)))
    # The torpedoes: a pair down the middle of the trench, and then a beat of
    # nothing before the port goes. The wait is the point of the scene.
    for side in (-1.0, 1.0):
        bolts.append((t_fire, 1.3 * beat, side * 0.42, 0.40, 0.7, 20.0,
                      (255, 205, 120)))

    idx = np.empty((H, W), np.int32)
    gathered = np.empty((H, W, 3), np.uint8)
    work = np.empty((H, W, 3), f32)
    out = np.empty((H, W, 3), np.uint8)

    def draw_streak(tgt, bx, by, z_near, col):
        """A bolt as a foreshortened line: its own length shrinks with depth."""
        za, zb = z_near, z_near * 1.35
        ax, ay = cx + focal * bx / za, cy + focal * by / za
        bx2, by2 = cx + focal * bx / zb, cy + focal * by / zb
        n = 20
        px = np.linspace(ax, bx2, n).astype(np.int32)
        py = np.linspace(ay, by2, n).astype(np.int32)
        keep = (px >= 0) & (px < W) & (py >= 0) & (py < H - 1)
        if not keep.any():
            return
        px, py = px[keep], py[keep]
        tgt[py, px] = col
        tgt[py + 1, px] = col

    def render(t, frame):
        tt = t % total

        # Camera. Roll is a slow beat of two incommensurate sines; the knock is
        # fast and integer, and both grow as the run tightens.
        drive = 0.55 + 0.75 * (tt / total)
        rl = (0.62 * np.sin(2.0 * np.pi * 0.083 * tt)
              + 0.38 * np.sin(2.0 * np.pi * 0.191 * tt + 1.3))
        m = int(round((rl * drive * 0.5 + 0.5) * (nroll - 1)))
        m = 0 if nroll == 1 else min(max(m, 0), nroll - 1)
        amp = swing * drive * (1.0 + 3.0 * smoothstep(t_hit, t_peak, tt))
        ox = int(round(amp * 1.7 * np.sin(2.0 * np.pi * 3.7 * tt + 0.4)))
        oy = int(round(amp * 1.3 * np.sin(2.0 * np.pi * 5.3 * tt)))
        ox = min(max(ox, -PAD), PAD)
        oy = min(max(oy, -PAD), PAD)

        # Distance flown, closed form so any t lands in the same place. The run
        # accelerates, which is the only thing that distinguishes the second
        # half of a corridor from the first.
        dist = args.speed * (0.78 * tt + 0.30 * tt * tt / total)
        off = int(dist) % TEXV

        np.add(maps[m][PAD + oy:PAD + oy + H, PAD + ox:PAD + ox + W],
               np.int32(off * TEXU), out=idx)
        np.take(atlas, idx, axis=0, out=gathered)
        fog = fogmaps[m][PAD + oy:PAD + oy + H, PAD + ox:PAD + ox + W, None]
        np.multiply(gathered, fog, out=work)

        # The exhaust port: a warm point at the vanishing point that the run is
        # heading for, then the hit itself.
        port = 0.10 * smoothstep(t_up, t_fire, tt)
        port += 1.9 * smoothstep(t_hit, t_peak, tt)
        # Never `work += ...` here: an augmented assignment would rebind the
        # name as a local of render() and the preallocated buffer would vanish.
        if port > 0.004:
            np.add(work, glow[:, :, None] * (hot[None, None, :] * f32(port)),
                   out=work)
        blast = smoothstep(t_hit + 0.15 * beat, t_peak, tt) * (
            1.0 - smoothstep(t_peak, t_peak + 0.85 * beat, tt))
        if blast > 0.004:
            np.add(work, f32(blast * 230.0), out=work)

        # Up from black at the top of the cycle, down to it after the hit.
        # It comes up *from a third*, not from nothing: the scheduler crossfades
        # into this at t=0, and a demo whose first second is black gives that
        # crossfade nothing to land on.
        fade = (0.34 + 0.66 * smoothstep(0.0, t_fade, tt)) * (
            1.0 - smoothstep(t_peak + 1.1 * beat, t_dark, tt))
        if fade < 0.999:
            np.multiply(work, f32(fade), out=work)

        np.clip(work, 0, 255, out=work)
        np.copyto(out, work, casting="unsafe")

        # Depth linear in time, which is what a bolt at constant velocity does:
        # on screen that is fast where it is close and slowing as it nears the
        # vanishing point. Anything eased the other way reads as a firework.
        for b_t0, b_dur, b_x, b_y, b_z0, b_z1, b_col in bolts:
            u = (tt - b_t0) / b_dur
            if 0.0 <= u < 1.0:
                draw_streak(out, b_x, b_y, b_z0 + (b_z1 - b_z0) * u, b_col)

        if args.computer:
            # One swing down, one swing up. Between them the display is parked
            # at its resting row and only the blips move.
            down = smoothstep(t_down, t_down + dur_down, tt)
            away = smoothstep(t_up, t_up + dur_up, tt)
            place = down - away
            if place > 0.002:
                # Overshoot on the way down: it drops onto its stop.
                bounce = 0.0
                if t_down < tt < t_down + 2.2 * dur_down:
                    bounce = 2.4 * np.sin(np.pi * 2.0 * (tt - t_down) /
                                          (2.2 * dur_down)) * (1.0 - down)
                top = int(round(-COMP_H * (1.0 - place) + 4.0 + bounce))
                lvl = 5 if place > 0.85 else max(1, int(round(place * 5)))
                y0 = max(0, top)
                y1 = min(H, top + COMP_H)
                if y1 > y0:
                    src = comp_lv[lvl][y0 - top:y1 - top]
                    np.maximum(out[y0:y1], src, out=out[y0:y1])

                    # Blips: they start out at the edges of the scale and close
                    # on the reticle, and the brackets flash when they arrive.
                    if tt >= t_lock:
                        k = smoothstep(t_lock, t_conv, tt)
                        sep = int(round(blip_far - (blip_far - 18) * k))
                        brow = top + COMP_H // 2 - 2
                        if brow >= 0 and brow + 5 <= H:
                            for side in (-1, 1):
                                bx0 = min(max(int(cx) + side * sep - 2, 0),
                                          W - 5)
                                # A dim tail pointing back the way it came, so
                                # a 5 px block reads as a blip closing rather
                                # than as one more mark on the bezel.
                                tail = min(max(bx0 + side * 5, 0), W - 5)
                                out[brow + 2, min(tail, bx0):
                                    max(tail, bx0) + 5] = dim_col
                                out[brow:brow + 5, bx0:bx0 + 5] = blip_col
                        # Locked: the reticle brackets are answered by two hard
                        # verticals closing on the target, blinking.
                        if k >= 1.0 and (tt * 5.0) % 1.0 < 0.5:
                            ly0, ly1 = max(top + 13, 0), min(top + COMP_H - 13, H)
                            if ly1 > ly0:
                                out[ly0:ly1, int(cx) - lock_x] = blip_col
                                out[ly0:ly1, int(cx) + lock_x] = blip_col
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
