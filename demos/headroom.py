#!/usr/bin/env python3
"""M-M-Max Headroom: a shiny plastic head stuttering in front of a neon room.

The 1985 character was sold as a computer-generated presenter, which he was
not -- he was an actor under latex, shot against a rotating backdrop of
coloured lines, and the "computer" of it was entirely in the *edit*: the
picture jumps, holds a frame too long, snaps back to something it already
showed, tears sideways, and stammers. That editing is the character. So this
demo spends almost all of its effort on the stutter and treats the head and
the room as cheap things to put behind it.

**The room.** A vanishing point sits just behind the head, and everything in
the backdrop is a periodic function of two per-pixel fields baked once in
`build()`: the angle around that point, and 1/r. Spokes are a function of
`angle * n + spin * t`, depth rings a function of `1/r + rush * t`. Both are
turned into 7-bit indices, packed into one integer along with a radial
brightness level and the row parity, and the whole frame is then a single
`np.take` from a baked 393 kB texture. Nothing in the backdrop computes a
colour at runtime -- there is one multiply, two adds, two truncations and one
gather per frame. That matters because the wall is driven by a Raspberry Pi 3,
which runs this kind of numpy between 30x and 100x slower than a laptop, and
the whole frame budget there is about 20 ms.

Folding the radial fade and the scanline into the *index* rather than
multiplying them over the frame afterwards is the trick that keeps it to one
gather. The cost is a bigger table, so the table is deliberately small in each
axis -- 128 spoke phases, 128 ring phases, 4 distance bands, 2 row parities --
and 393 kB stays inside a cache the Pi actually has. A finer table measured
slower there despite doing identical arithmetic.

**The head.** Modelled as a union of eight ellipsoids -- cranium, jaw, nose,
two ears, and three for the hair -- and rendered by solving the ray/ellipsoid
quadratic per pixel for the nearest surface and its normal. That is closed
form; there is no ray marching anywhere. It is lit the way the character
always was, by a studio full of coloured lamps: a white key, a magenta fill
from below right, a hard specular lobe and a cyan rim borrowed from the room.
A single white key was the first attempt and it reads as a bald egg at 64
rows; the character has to read as *moulded shiny plastic* or there is no
joke. The skin is a pale waxy off-white and the magenta comes in through the
fill rather than through the base colour: a face pigmented pink enough to
show at this size stops being a mannequin and starts being meat, and then it
competes with the backdrop instead of sitting in it.

Four features are not geometry at all but bands in head-space coordinates,
applied to whatever surface the ray hit. The wraparound glasses, which is why
they wrap correctly round the temples to the ears. The grin -- a wide ellipse
of lip with a bright band of teeth cut out of its middle and two dark seams
in that, its centre line rising with x**2, because at twenty pixels across
the only thing separating a smile from a slot is that the ends are higher
than the middle. The hairline, which rakes back with z and drops with
x**2, so the hair leaves the brow and keeps its hard edge down at the
temples; two ellipsoids can only meet along a level curve, and a line ruled
straight across the forehead reads as a swim cap. And the crest, a second
plane through the hair that splits it into a lit front and a dark back: gold
shaded smoothly is a helmet at this size whatever the lighting does, and the
only thing that says which way the mass is combed is one hard value break
raking down towards the nape. All of that happens in
`build()`: 20 yaw poses over about a hundred degrees, times 2 mouth states,
are baked to 52x64 sprites, and a frame is a masked blit. The arc is that
wide because a narrow one bakes twenty poses that all look front-on -- at
52 px across, a cheek has to foreshorten by whole pixels before a turn reads,
and the stutter schedule cuts between non-adjacent poses anyway.

Each sprite carries a one-pixel black outline, baked in. Without it the head's
edge and the neon lines behind it are the same brightness and the silhouette
dissolves; the vanishing point is also placed so the dimmest part of the room
is where the head is.

**The stutter.** A schedule of 600 steps at a fixed 30 Hz is baked in
`build()` from `--seed`: mostly a smooth yaw sweep, cut with holds, two-frame
ratchets, jumps back to a pose already shown, and long freezes. `render()`
indexes it by `int(t * 30)`, so it is a pure function of `t` -- which it must
be, since ftsched builds effects ahead of time and starts them at t=0, and the
preview baker steps them at a fixed rate. On the steps the schedule marks as
glitched, and only those, the frame also gets a few horizontal row-bands
rolled sideways, an RGB channel split of a pixel or two, a brightness pop, and
a two-or-three-pixel jog of the head. The jog matters more than it sounds:
tearing the frame while the face stays pinned exactly where it was reads as a
clean overlay on top of a fault, rather than as one broken signal. Every one
of those parameters is baked; nothing calls a random number generator after
build().

Costs 0.2 ms/frame p95 here and 0.05 s to build. Against the calibration
demos' 30x-to-100x scaling that is somewhere between 6 and 20 ms on the wall's
Pi, and the pessimistic end of that range is the gather, which is why the room
texture is 393 kB and not the 3 MB the first version of it was.

Run:  python3 headroom.py --host 127.0.0.1
      python3 headroom.py --glitch 1 --stutter 1     # unwatchable, correctly
      python3 headroom.py --glitch 0 --stutter 0     # the smooth sweep under it
      python3 headroom.py --say 'BLIPVERT' --room ice
"""

import sys

import numpy as np

import demoscene as ds

f32 = np.float32

# Texture axes. 7 bits each is more phase resolution than 64 rows can show,
# and keeps the packed table inside a cache line budget the Pi has -- see the
# module docstring. LEVELS is the radial brightness quantisation.
SPOKE_N = 128
RING_N = 128
LEVELS = 4
PARITY = 2
PLANE = SPOKE_N * RING_N                       # entries per (level, parity)

# The schedule ticks at a fixed rate that has nothing to do with --fps: the
# stutter must look the same whether ftsched runs this at 30 or the preview
# baker steps it at 20.
STEP_HZ = 30.0
STEPS = 600                                    # a 20 s cycle, then it repeats

# Head sprite. Nearly the full panel height, which is the framing the
# character always had -- he filled the frame.
SPR_W = 52
SPR_H = 64
NYAW = 20                                      # baked yaw poses
NMOUTH = 2
YAW_MAX = 0.90                                 # half the baked arc, radians

# Room colour sets: four spoke colours and one ring colour. Saturated, but
# kept well under full brightness so the head stays in front of them.
ROOMS = {
    "neon":    [(255, 40, 190), (0, 210, 255), (90, 70, 255), (255, 130, 20)],
    "ice":     [(60, 130, 255), (0, 220, 240), (140, 90, 255), (220, 240, 255)],
    "magenta": [(255, 30, 140), (200, 0, 255), (255, 90, 60), (120, 0, 220)],
    "acid":    [(180, 255, 0), (0, 255, 140), (255, 220, 0), (0, 190, 255)],
}
RING_COLOUR = {"neon": (120, 90, 200), "ice": (90, 140, 220),
               "magenta": (170, 60, 160), "acid": (110, 200, 110)}


def add_arguments(ap):
    ap.add_argument("--spin", type=float, default=1.0,
                    help="how fast the room rotates; negative reverses it")
    ap.add_argument("--stripes", type=int, default=28,
                    help="radiating spokes around the vanishing point "
                         "(snapped to a multiple of 4)")
    ap.add_argument("--rush", type=float, default=1.0,
                    help="how fast the depth rings rush past; negative recedes")
    ap.add_argument("--room", default="neon", choices=sorted(ROOMS),
                    help="colour set for the backdrop")
    ap.add_argument("--glitch", type=float, default=0.55,
                    help="how often and how hard the picture tears, 0..1")
    ap.add_argument("--stutter", type=float, default=0.6,
                    help="how much the head holds, ratchets and jumps, 0..1")
    # Not his name. A head is a tailspace's opposite in both halves, which is
    # the sort of joke the character would have made about himself, and it
    # keeps the wall from claiming to be someone it is not.
    ap.add_argument("--say", default="MAX TAILSPACE",
                    help="stuttered caption along the bottom; empty for none")
    ap.add_argument("--side", default="right", choices=("left", "right"),
                    help="which side of the panel the head sits on")
    ap.add_argument("--scanlines", dest="scanlines", action="store_true",
                    default=True)
    ap.add_argument("--no-scanlines", dest="scanlines", action="store_false",
                    help="drop the dimmed alternate rows over the backdrop")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


# --------------------------------------------------------------------------
# The room: per-pixel fields, and one baked texture to gather from.
# --------------------------------------------------------------------------

def _camera(w, h, vx, vy, scanlines):
    """Bake the per-pixel fields for one vanishing point.

    Returns (ang_k, ring_k, const_off): the angle already scaled so that
    multiplying by the spoke period lands in 0..SPOKE_N, 1/r likewise, and the
    constant part of the packed index -- radial brightness band and row parity
    -- which costs nothing extra to carry because it is added into the same
    integer the gather uses.
    """
    x = np.arange(w, dtype=f32) - f32(vx)
    y = np.arange(h, dtype=f32) - f32(vy)
    xx = x[None, :]
    yy = y[:, None]
    r = np.sqrt(xx * xx + yy * yy).astype(f32)
    ang = np.arctan2(yy + np.zeros_like(xx), xx + np.zeros_like(yy)).astype(f32)

    # +pi so the field is positive everywhere: the cast to int truncates
    # toward zero, which around a sign change would mirror the pattern and put
    # a hard seam through the middle of the screen.
    ang_k = (ang + f32(np.pi)) * f32(SPOKE_N / (2.0 * np.pi))
    # 1/r, the whole of the perspective. Clamped near the vanishing point,
    # where the rings would otherwise alias into noise.
    # The constant is the whole feel of the corridor and was picked by looking
    # at it: much smaller and less than two rings fit across the outer half of
    # the panel, which reads as a smear rather than as depth.
    ring_k = f32(RING_N * 700.0) / np.maximum(r, f32(5.0))

    lev = np.clip((r * f32(LEVELS / 190.0)).astype(np.int32), 0, LEVELS - 1)
    par = (np.arange(h, dtype=np.int32) & 1)[:, None] if scanlines else 0
    const_off = ((lev * PARITY) + par) * PLANE
    return ang_k, ring_k.astype(f32), const_off.astype(np.int32)


def _room_texture(room, periods):
    """(LEVELS*PARITY*PLANE, 3) uint8, indexed by the packed field.

    Spokes and rings are added rather than multiplied. Multiplied, the two
    patterns only survive where they overlap and the room reads as a grid of
    dots; added, it reads as a wireframe corridor, which is what the backdrop
    actually was.
    """
    spoke = np.zeros((SPOKE_N, 3), f32)
    cols = ROOMS[room]
    # Four bars per texture period, so one period of the table is four
    # differently-coloured spokes and --stripes counts the real ones.
    bar = SPOKE_N // 4
    u = np.arange(bar, dtype=f32) / bar
    # A soft-shouldered bar rather than a hard one: at this size a hard edge
    # crawls badly as the spoke sweeps past a pixel column.
    # The bar profile is a fraction of the *period*, so at a low --stripes the
    # same fraction is an enormous soft wedge and the room reads as coloured
    # smoke. Scaling the width with the period keeps a spoke about as thick on
    # screen however many of them there are; --stripes 8 was unusable without
    # this and is fine with it.
    width = float(np.clip(0.333 * periods / 7.0, 0.10, 0.45))
    prof = np.clip(1.0 - np.abs(u - 0.5) * (2.0 / width), 0.0, 1.0) ** f32(1.6)
    for i, c in enumerate(cols):
        spoke[i * bar:(i + 1) * bar] = prof[:, None] * np.array(c, f32)

    v = np.arange(RING_N, dtype=f32) / RING_N
    rprof = np.clip(1.0 - np.abs(v - 0.5) * 7.0, 0.0, 1.0) ** f32(1.8)
    ring = rprof[:, None] * np.array(RING_COLOUR[room], f32)

    fade = np.array([0.30, 0.62, 0.88, 1.0], f32)[:LEVELS]
    dim = np.array([1.0, 0.62], f32)[:PARITY]

    field = spoke[:, None, :] + ring[None, :, :]        # (SPOKE, RING, 3)
    both = (fade[:, None] * dim[None, :]).reshape(-1)   # (LEVELS*PARITY,)
    tex = field[None, :, :, :] * both[:, None, None, None]
    # 0.42 overall: the room is competing with a head lit to 255 and has to
    # lose. Anything brighter and the silhouette stops reading.
    tex = np.clip(tex * 0.55, 0, 255).astype(np.uint8)
    return tex.reshape(-1, 3)


# --------------------------------------------------------------------------
# The head: a union of ellipsoids, solved in closed form.
# --------------------------------------------------------------------------

# (centre, radii, material). Head space is +y up, +z toward the viewer, one
# unit ~ the half-width of the cranium.
SKIN, HAIR, EAR = 0, 1, 2
PARTS = [
    ((0.00, 0.06, 0.00), (0.58, 0.72, 0.60), SKIN),   # cranium
    ((0.00, -0.56, 0.04), (0.44, 0.46, 0.52), SKIN),  # jaw
    ((0.00, -0.22, 0.42), (0.10, 0.16, 0.20), SKIN),  # nose
    ((-0.57, -0.06, -0.06), (0.09, 0.18, 0.13), EAR),
    ((0.57, -0.06, -0.06), (0.09, 0.18, 0.13), EAR),
    # Three hair blobs. The hairline itself is painted, not modelled -- see
    # _pose() -- so these only have to supply a silhouette. The one that does
    # the work is the second: it sits *forward* of the cranium, so the mass
    # juts out past the brow and the profile has a break in it. Stacking it
    # high and behind instead, which is what the hair actually does, gave a
    # taller dome and a taller dome is still a dome; three or four pixels of
    # overhang at the front is the only version of this that survived being
    # looked at from across a room.
    ((0.00, 0.26, -0.34), (0.54, 0.44, 0.54), HAIR),   # shell over the skull
    ((0.00, 0.50, 0.10), (0.50, 0.44, 0.52), HAIR),    # the quiff, over the brow
    ((0.00, 0.14, -0.52), (0.44, 0.50, 0.38), HAIR),   # the nape
]

# Materials: base colour, ambient, diffuse, specular strength, specular
# exponent, rim strength. The glasses, the mouth, the teeth and the lit side
# of the hair are materials too, even though none of them is geometry -- see
# _pose(). The skin is deliberately a pale waxy off-white with very little
# pigment of its own: a mannequin, not a face. Its specular is what carries
# the plastic, and the magenta arrives through MAT_FIL rather than the base,
# which keeps the head in the room's palette without turning it into meat.
GLASS, MOUTH, TEETH, CREST = 3, 4, 5, 6
MAT_RGB = np.array([
    (232, 194, 178),      # skin: pale moulded latex, barely tinted
    (152, 100, 40),       # hair, unlit: the mass behind the crest
    (222, 186, 180),      # ear
    (18, 14, 30),         # glasses
    (74, 18, 30),         # mouth
    (240, 232, 226),      # teeth
    (238, 180, 72),       # hair, lit: the crest
], f32)
MAT_AMB = np.array([0.17, 0.10, 0.14, 0.05, 0.06, 0.34, 0.26], f32)
MAT_DIF = np.array([0.72, 0.56, 0.78, 0.26, 0.40, 0.66, 0.82], f32)
MAT_SPC = np.array([0.50, 0.16, 0.44, 1.60, 0.30, 0.35, 0.30], f32)
MAT_EXP = np.array([28.0, 18.0, 26.0, 90.0, 40.0, 26.0, 70.0], f32)
MAT_RIM = np.array([0.95, 0.70, 0.70, 1.20, 0.15, 0.30, 0.80], f32)
MAT_FIL = np.array([0.44, 0.34, 0.42, 0.55, 0.18, 0.22, 0.28], f32)

# Two coloured lights and a rim, because the character was always shot in a
# studio full of them and because a single white key on a head this small
# reads as a bald egg. The fill comes from below right in magenta, the rim
# from behind in cyan; between them the silhouette stays lit even when the
# key side is facing away.
LIGHT = np.array([-0.52, 0.55, 0.66], f32)
FILL = np.array([0.72, -0.45, 0.52], f32)
SPEC_RGB = np.array([255, 244, 226], f32)
FILL_RGB = np.array([255, 70, 170], f32)
RIM_RGB = np.array([70, 150, 255], f32)          # cyan, borrowed from the room


def _unit(v):
    return (v / np.sqrt(float(np.dot(v, v)))).astype(f32)


def _pose(yaw, tilt, mouth_open):
    """Ray trace one head pose. -> (rgb uint8 (SPR_H,SPR_W,3), mask bool)."""
    # Orthographic camera down +z, so every ray shares a direction and the
    # quadratic's leading coefficient is a scalar per ellipsoid.
    scale = f32(SPR_H / 1.86)
    px = (np.arange(SPR_W, dtype=f32) - (SPR_W - 1) * 0.5) / scale
    py = ((SPR_H - 1) * 0.5 - np.arange(SPR_H, dtype=f32)) / scale - f32(0.02)

    cy, sy = np.cos(yaw), np.sin(yaw)
    ct, st = np.cos(tilt), np.sin(tilt)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], f32)
    rx = np.array([[1, 0, 0], [0, ct, -st], [0, st, ct]], f32)
    rot = ry.dot(rx)                    # object -> world
    inv = rot.T                         # world -> object

    # The camera sits on the +z side and looks back along -z. Putting it on
    # the far side instead is the obvious thing and is wrong in a way that is
    # not obvious: you still get a silhouette, but every normal faces away
    # from you, so the lambert term clips to zero and the rim term saturates,
    # and the head comes out a flat blue cutout.
    ox = px[None, :] + np.zeros((SPR_H, 1), f32)
    oy = py[:, None] + np.zeros((1, SPR_W), f32)
    oz = np.full((SPR_H, SPR_W), 6.0, f32)
    # Ray origin and direction in object space.
    o = [inv[i, 0] * ox + inv[i, 1] * oy + inv[i, 2] * oz for i in range(3)]
    d = (-inv[:, 2]).astype(f32)

    best = np.full((SPR_H, SPR_W), 1e9, f32)
    hit = [np.zeros((SPR_H, SPR_W), f32) for _ in range(3)]
    nrm = [np.zeros((SPR_H, SPR_W), f32) for _ in range(3)]
    mat = np.zeros((SPR_H, SPR_W), np.int32)

    for centre, radii, material in PARTS:
        a = [(o[i] - centre[i]) / radii[i] for i in range(3)]
        dd = np.array([d[i] / radii[i] for i in range(3)], f32)
        qa = float(np.dot(dd, dd))
        qb = a[0] * dd[0] + a[1] * dd[1] + a[2] * dd[2]
        qc = a[0] * a[0] + a[1] * a[1] + a[2] * a[2] - 1.0
        disc = qb * qb - qa * qc
        ok = disc > 0.0
        root = np.sqrt(np.maximum(disc, 0.0))
        s = (-qb - root) / qa
        take = ok & (s < best) & (s > 0.0)
        best = np.where(take, s, best)
        for i in range(3):
            u = a[i] + s * dd[i]                 # unit-sphere surface point
            hit[i] = np.where(take, u * radii[i] + centre[i], hit[i])
            nrm[i] = np.where(take, u / radii[i], nrm[i])
        mat = np.where(take, material, mat)

    mask = best < 1e8
    hx, hy, hz = hit
    length = np.sqrt(nrm[0] ** 2 + nrm[1] ** 2 + nrm[2] ** 2) + 1e-6
    nx, ny, nz = [n / length for n in nrm]

    # The glasses are a band in head space rather than a solid, which is what
    # makes them wrap round the temples to the ears the way the prop did. A
    # modelled slab would have had to be unioned in and would have shown a
    # seam where it met the cranium.
    band = (np.abs(hy - 0.03 - 0.10 * hz) < 0.112) & (hz > -0.30) & mask
    mat = np.where(band, GLASS, mat)

    # The hairline, likewise a plane in head space rather than a solid, and
    # for the same reason the glasses are: painted on, it can slope and arch,
    # where the boundary between two ellipsoids can only be a level curve.
    # It rakes back with hz -- so the hair leaves the brow and sweeps up --
    # and drops with hx**2, which keeps the hard edge down at the temples
    # where a real quiff has it, instead of ruling it across the forehead.
    line = (hy > f32(0.16) + f32(0.32) * hz - f32(0.30) * hx * hx) & (mat == SKIN)
    mat = np.where(line & mask, HAIR, mat)

    # The crest, and the thing that finally gave the quiff a direction. A
    # single gold material shades smoothly however it is lit, and smooth reads
    # as *helmet* at this size; what reads as combed is one hard-edged value
    # break across the mass. So the hair is two materials divided by a painted
    # plane: lit at the front and over the top, dark from there back, with the
    # boundary raking down towards the nape. The slope is the whole point --
    # a level boundary is a stripe painted round a helmet.
    cap = (hz + f32(0.45) * hy) > f32(0.10)
    mat = np.where(cap & (mat == HAIR) & mask, CREST, mat)

    # The grin, and the whole reason anyone recognises him. A small dark oval
    # is what a mouth *is*, and it reads as a mannequin; what has to survive
    # down to about six pixels is a wide band of teeth with a dark line above
    # and below it. So the lips are drawn as the ellipse and the teeth are cut
    # out of its middle, rather than the teeth being drawn on top.
    # The centre line rises with hx**2, which is the smile: at this size the
    # only thing that separates a grin from a slot is that the ends are higher
    # than the middle.
    mc = f32(-0.505) + f32(0.62) * hx * hx
    my = 0.115 if mouth_open else 0.075
    lips = (((hx / 0.31) ** 2 + ((hy - mc) / my) ** 2) < 1.0) & (hz > 0.02)
    if mouth_open:
        # Open: only the upper teeth show, and the dark below them is the gap.
        teeth = lips & (hy > mc + f32(0.030))
    else:
        teeth = lips & (np.abs(hy - mc) < f32(0.042))
    # Two seams. More than that and they merge into a grey smear; none at all
    # and the teeth read as a strip of gaffer tape.
    for sx in (-0.115, 0.115):
        teeth = teeth & (np.abs(hx - f32(sx)) > f32(0.014))
    mat = np.where(lips & mask, MOUTH, mat)
    mat = np.where(teeth & mask, TEETH, mat)

    lt = _unit(rot.T.dot(LIGHT))         # light, expressed in object space
    lam = np.clip(nx * lt[0] + ny * lt[1] + nz * lt[2], 0.0, 1.0)
    fl = _unit(rot.T.dot(FILL))
    fill = np.clip(nx * fl[0] + ny * fl[1] + nz * fl[2], 0.0, 1.0) ** f32(1.5)
    hv = _unit(LIGHT + np.array([0, 0, 1], f32))
    hv = rot.T.dot(hv)
    spec = np.clip(nx * hv[0] + ny * hv[1] + nz * hv[2], 0.0, 1.0)
    # Facing ratio needs the *world* normal's z, which is row 2 of rot.
    nzw = np.clip(rot[2, 0] * nx + rot[2, 1] * ny + rot[2, 2] * nz, 0.0, 1.0)
    rim = (1.0 - nzw) ** f32(3.0)

    base = MAT_RGB[mat]
    amb = MAT_AMB[mat][..., None]
    dif = MAT_DIF[mat][..., None]
    rgb = base * (amb + dif * lam[..., None])
    rgb = rgb + SPEC_RGB * (MAT_SPC[mat] * spec ** MAT_EXP[mat])[..., None]
    rgb = rgb + FILL_RGB * (MAT_FIL[mat] * fill)[..., None] * (base / 255.0)
    rgb = rgb + RIM_RGB * (MAT_RIM[mat] * rim)[..., None]
    rgb = np.clip(rgb, 0, 255) * mask[..., None]

    # A one-pixel black outline. The room behind is bright enough that without
    # it the silhouette dissolves into the spokes.
    grow = mask.copy()
    for axis in (0, 1):
        for shift in (1, -1):
            grow |= np.roll(mask, shift, axis=axis)
    return rgb.astype(np.uint8), grow


# --------------------------------------------------------------------------
# The stutter schedule.
# --------------------------------------------------------------------------

def _schedule(rng, stutter, glitch):
    """Bake STEPS worth of (pose, mouth, camera, tear, split, pop).

    Everything random about this demo lives here and is decided once, so that
    render() can be a pure function of t.
    """
    pose = np.zeros(STEPS, np.int32)
    mouth = np.zeros(STEPS, np.int32)
    cam = np.zeros(STEPS, np.int32)
    hard = np.zeros(STEPS, np.float32)          # glitch severity, 0 = clean

    p, step = NYAW // 2, 1
    i = 0
    talk = 0
    while i < STEPS:
        # A pure sweep at stutter=0; at 1 the smooth segments are rare and
        # short and everything else is a fault.
        # float64 deliberately: Generator.choice checks that p sums to one to
        # a float64 tolerance, and a float32 normalisation does not always.
        w = np.array([1.0 - 0.75 * stutter, 0.55 * stutter, 0.50 * stutter,
                      0.40 * stutter, 0.25 * stutter], np.float64)
        w = w / w.sum()
        kind = int(rng.choice(5, p=w))
        if kind == 0:                            # smooth sweep
            n = int(rng.integers(6, 22))
            sev = 0.0
        elif kind == 1:                          # hold on one frame
            n = int(rng.integers(2, 9))
            sev = 0.25
        elif kind == 2:                          # two-frame ratchet
            n = int(rng.integers(4, 12))
            sev = 0.55
        elif kind == 3:                          # jump back to an earlier pose
            n = int(rng.integers(1, 5))
            p = int(rng.integers(0, NYAW))
            sev = 0.9
        else:                                    # freeze
            n = int(rng.integers(8, 26))
            sev = 0.7
        if kind in (3, 4) and rng.random() < 0.5:
            cam_now = int(rng.integers(0, 3))
        else:
            cam_now = cam[i - 1] if i else 0
        back = max(0, p - int(rng.integers(2, 5)))
        for k in range(n):
            if i >= STEPS:
                break
            if kind == 0:
                p += step
                if p >= NYAW - 1 or p <= 0:
                    p = int(np.clip(p, 0, NYAW - 1))
                    step = -step
            elif kind == 2:
                p = p if (k & 1) else back
            pose[i] = p
            cam[i] = cam_now
            hard[i] = sev
            # Talking: mouth flaps in runs, with gaps, independent of the pose.
            if talk <= 0:
                talk = int(rng.integers(-14, 26))
            talk -= 1
            mouth[i] = 1 if (talk > 0 and (i >> 1) & 1) else 0
            i += 1

    # Tear bands, channel split and brightness pop, all decided here. A step
    # glitches if its severity plus a bernoulli draw crosses the --glitch bar.
    # Scaled entirely by --glitch, with no floor: at 0 the picture never tears
    # at all, which is the only reading of "0" worth having.
    fire = (rng.random(STEPS) < glitch * (0.06 + 0.30 + hard))
    tear_y = rng.integers(0, 64, size=(STEPS, 3)).astype(np.int32)
    tear_h = rng.integers(2, 14, size=(STEPS, 3)).astype(np.int32)
    amp = 2.0 + 46.0 * glitch
    tear_d = (rng.integers(-100, 101, size=(STEPS, 3)) * amp / 100.0)
    tear_d = tear_d.astype(np.int32)
    nband = np.where(fire, rng.integers(1, 4, size=STEPS), 0).astype(np.int32)
    split = np.where(fire & (rng.random(STEPS) < 0.6 + 0.4 * glitch),
                     rng.integers(1, 2 + int(2 + 3 * glitch), size=STEPS),
                     0).astype(np.int32)
    pop = np.where(fire, rng.integers(0, 8, size=STEPS), 4).astype(np.int32)
    # A couple of pixels of horizontal displacement on the head itself. Tears
    # alone leave the face pinned exactly where it was, which reads as a clean
    # overlay on top of a fault rather than as one broken signal.
    jog = np.where(fire, rng.integers(-3, 4, size=STEPS), 0).astype(np.int32)
    return pose, mouth, cam, nband, tear_y, tear_h, tear_d, split, pop, jog


# --------------------------------------------------------------------------
# Caption.
# --------------------------------------------------------------------------

def _caption(text, w):
    """Bake the stuttered forms of a caption: 'M', 'M-M', ..., full string.

    The repeat comes off the text itself rather than being hardcoded, so
    --say BLIPVERT stammers on its own first letter.
    """
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 10)
    except OSError:
        font = ImageFont.load_default()
    head = text.strip()[:1].upper() or "M"
    forms = [head, head + "-" + head, head + "-" + head + "-" + head,
             text.upper()]
    out = []
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    for s in forms:
        tw = max(2, int(probe.textlength(s, font=font)) + 2)
        img = Image.new("RGB", (min(tw, w), 12), (0, 0, 0))
        # Dimmer than the head on purpose: it is a caption, not the subject,
        # and full white text on a 320x64 panel pulls the eye off the face.
        ImageDraw.Draw(img).text((0, 0), s, font=font, fill=(176, 202, 232))
        a = np.asarray(img, np.uint8)
        out.append((a, a.max(axis=2) > 24))
    return out


def _caption_schedule(rng, forms):
    """Which caption form each step shows; -1 is blank.

    Weighted so the finished string is on screen most of the time and the
    stammer is a short run in front of it -- the joke is that he cannot get
    the word out, and a permanent 'M-M-M' is not that joke.
    """
    out = np.full(STEPS, -1, np.int32)
    i = 0
    while i < STEPS:
        i += int(rng.integers(6, 30))                # a gap
        for k in range(len(forms) - 1):              # the stammer
            n = int(rng.integers(2, 6))
            out[i:i + n] = k
            i += n
        n = int(rng.integers(24, 70))                # and the whole thing
        out[i:i + n] = len(forms) - 1
        i += n
    return out


# --------------------------------------------------------------------------

def build(args):
    W, H = args.width, args.height
    seed = args.seed or int(np.random.SeedSequence().entropy % (2 ** 31))
    rng = np.random.default_rng(seed)
    glitch = float(np.clip(args.glitch, 0.0, 1.0))
    stutter = float(np.clip(args.stutter, 0.0, 1.0))

    # Where the head sits, and with it the vanishing point: the room's dimmest
    # region is the middle of the radial fade, so putting the two together is
    # free contrast for the silhouette.
    hx = int(W * (0.72 if args.side == "right" else 0.28))
    # The 3 px margin is the jog below: a blit that ran off the edge would
    # silently truncate and the head would lose a strip rather than move.
    sx0 = int(np.clip(hx - SPR_W // 2, 3, W - SPR_W - 3))
    sy0 = 0

    cams = [_camera(W, H, hx - 26, 30, args.scanlines),
            _camera(W, H, hx + 18, 12, args.scanlines),
            _camera(W, H, hx + 4, 44, args.scanlines)]
    periods = max(1, int(round(args.stripes / 4.0)))
    tex = _room_texture(args.room, periods)

    # Poses. Twenty yaws over about a hundred degrees, with the head tilting
    # as it turns -- a pure yaw sweep reads as a turntable, and he never sat
    # still. The first version swept fifty degrees and every baked pose came
    # out front-on: at 52 px across, a cheek has to foreshorten by several
    # whole pixels before anyone sees it move, and the schedule cuts between
    # non-adjacent poses anyway, so the arc is worth far more than the density.
    yaws = np.linspace(-YAW_MAX, YAW_MAX, NYAW, dtype=f32)
    tilts = (0.12 * np.sin(np.linspace(0.0, 2.4 * np.pi, NYAW))).astype(f32)
    sprites = [[_pose(float(yaws[i]), float(tilts[i]), m)
                for i in range(NYAW)] for m in range(NMOUTH)]

    sched = _schedule(rng, stutter, glitch)
    (pose_s, mouth_s, cam_s, nband, tear_y, tear_h, tear_d, split_s, pop_s,
     jog_s) = sched

    # Brightness pops, as eight uint8 ramps: applying them as a table lookup
    # keeps the whole frame in integers. Index 4 is identity.
    pops = np.empty((8, 256), np.uint8)
    ramp = np.arange(256, dtype=f32)
    for i, k in enumerate([0.55, 0.7, 0.82, 0.92, 1.0, 1.2, 1.5, 1.9]):
        pops[i] = np.clip(ramp * k, 0, 255).astype(np.uint8)

    caption = _caption(args.say, W) if args.say.strip() else None
    cap_s = _caption_schedule(rng, caption) if caption else None

    out = np.empty((H, W, 3), np.uint8)
    tmp = np.empty((H, W, 3), np.uint8)
    fa = np.empty((H, W), f32)
    fb = np.empty((H, W), f32)
    ia = np.empty((H, W), np.int32)
    ib = np.empty((H, W), np.int32)

    spin_k = f32(args.spin * SPOKE_N * 0.35)
    rush_k = f32(args.rush * RING_N * 0.9)

    def render(t, frame):
        step = int(t * STEP_HZ) % STEPS
        cam = cam_s[step]
        ang_k, ring_k, off = cams[cam]

        # --- the room, in one gather ---------------------------------------
        # The +8192 keeps the field positive: the cast below truncates toward
        # zero, and a sign change in the middle of the screen would mirror the
        # pattern rather than continue it.
        # Every one of these writes through out= into a buffer built once;
        # `fa += x` here would bind a fresh local and allocate a frame.
        np.multiply(ang_k, f32(periods), out=fa)
        np.add(fa, f32(8192.0 + spin_k * t), out=fa)
        np.copyto(ia, fa, casting="unsafe")
        np.bitwise_and(ia, SPOKE_N - 1, out=ia)
        np.multiply(ia, RING_N, out=ia)

        np.add(ring_k, f32(8192.0 + rush_k * t), out=fb)
        np.copyto(ib, fb, casting="unsafe")
        np.bitwise_and(ib, RING_N - 1, out=ib)
        np.add(ia, ib, out=ia)
        np.add(ia, off, out=ia)
        np.take(tex, ia, axis=0, out=out, mode="clip")

        # --- the head, a masked blit ---------------------------------------
        rgb, mask = sprites[mouth_s[step]][pose_s[step]]
        x0 = sx0 + int(jog_s[step])
        np.copyto(out[sy0:sy0 + SPR_H, x0:x0 + SPR_W], rgb,
                  where=mask[:, :, None])

        if caption is not None and cap_s[step] >= 0:
            img, cmask = caption[cap_s[step]]
            ch, cw = img.shape[:2]
            cx = 8 if args.side == "right" else W - cw - 8
            np.copyto(out[H - ch - 2:H - 2, cx:cx + cw], img,
                      where=cmask[:, :, None])

        # --- and the fault ---------------------------------------------------
        n = nband[step]
        for j in range(n):
            y0 = int(tear_y[step, j])
            y1 = min(H, y0 + int(tear_h[step, j]))
            if y1 > y0:
                band = out[y0:y1]
                out[y0:y1] = np.roll(band, int(tear_d[step, j]), axis=1)
        sx = int(split_s[step])
        if sx:
            out[:, :, 0] = np.roll(out[:, :, 0], sx, axis=1)
            out[:, :, 2] = np.roll(out[:, :, 2], -sx, axis=1)
        p = int(pop_s[step])
        if p != 4:
            # take() cannot read and write the same array, hence the scratch.
            np.take(pops[p], out, out=tmp, mode="clip")
            np.copyto(out, tmp)
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
