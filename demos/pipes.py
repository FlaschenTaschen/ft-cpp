#!/usr/bin/env python3
"""The Windows 3D Pipes screensaver, growing through a lattice.

The third pillar of the screensaver genre, after the bouncing logo (`dvd`) and
the flying toasters (`toasters`), and the only one of the three with *depth*.
Pipes grow through an invisible 3D lattice, turning at right angles, joined by a
gleaming ball at every elbow; when the volume is full enough the screen holds
for a beat, an eraser sweeps it black, and a new run starts in a new colour
scheme. Chrome and enamel, Gouraud-shaded, unmistakably 1995.

The one representation
----------------------
**A self-avoiding walk on an integer lattice, flattened into a list of
primitives with timestamps.** The head sits in a cell, picks a direction, runs
a few cells, turns ninety degrees, repeats, and never revisits an occupied
cell. That gives exactly two things to draw -- a straight *run* between two
lattice nodes, and a *joint* ball at each turn -- and everything else is
bookkeeping. `build()` walks the whole lattice for every scheme up front and
emits `(kind, p0, p1, colour, t0, t1)` in view-space coordinates, sorted by
finish time. `render(t)` then only reveals more of a list that already exists,
which is why the demo is a pure function of `t` and why the walk can be
*guaranteed* to fill the space nicely rather than hoped to.

Two primitives, drawn as impostors
----------------------------------
There is no mesh and no triangle anywhere. A run is rasterised as a screen-space
*capsule*: over the segment's bounding tile, `perp` is the signed perpendicular
distance to the projected axis and `u = perp / radius` runs -1..1 across the
tube. From `u` alone the surface falls out -- `w = sqrt(1 - u*u)` is the
component facing the viewer, the depth is the axis depth minus `w * R`, and the
normal is `u * p + w * v` for the segment's screen perpendicular `p` and the
view direction `v`. A ball is the same idea in two dimensions.

That is what makes the shading cheap enough for a Pi. Lambert and a tight
specular are a function of `u` and of the *angle of the segment on screen*, and
nothing else -- so both are baked in `build()` into a table indexed by
`[colour][angle bin][u bin]` and a frame does one `np.take` where it would
otherwise do a dozen vector operations per pixel. Balls get a 2D table of the
same kind indexed by `(nx, ny)`. The specular highlight running down the length
of a tube, which is the whole reason chrome reads as chrome, costs one lookup.

Occlusion is the point, so there is a real **z-buffer**: a float depth per
panel pixel, tested per fragment. Pipes crossing in front of each other is most
of what makes a flat panel read as a volume, and no amount of shading
substitutes for it.

Perspective, not isometric
--------------------------
Isometric is cheaper and was tried first. It fails here for a specific reason:
every tube is the same width at every depth, so two tubes crossing are
distinguished *only* by which one occludes the other, and at 64 rows that is a
one-pixel cue. A mild perspective -- the far plane one and a half times the
distance of the near one -- makes near tubes visibly fatter and far tubes
thinner and, with a little depth fog, dimmer. That is a second and a third depth
cue that survive being seen from three metres, and the frustum costs one divide
per lattice node at build time. The camera is also yawed and pitched a few
degrees, so that runs along the lattice axes do not land exactly horizontal and
vertical on screen; a perfectly axis-aligned projection reads as a flat maze.

The lattice is 20 x 4 x 9 cells, which is deliberately not a cube: this panel is
a 5:1 letterbox and a wide, shallow, *short* volume fills it. The camera is
fitted to the lattice in `build()` by projecting all 720 nodes and solving for
the focal length, then over-scanned slightly, so the near layer runs off the
edges the way it does in the original.

Purity, and the accumulation problem
------------------------------------
Cost scales with how much pipe is on screen, so a frame must not redraw the
couple of hundred primitives already there -- but a persistent frame buffer is
accumulated state and `render` may not accumulate. The resolution is the one
`plotter` uses: the buffer is defined as a **pure function of one integer**.
`world(i)` is "every primitive with index < i, rasterised into colour and
depth", and it is *memoised* rather than accumulated. The cache holds
`(scheme, i, rgb, z)`; a frame asking for a larger `i` -- the usual case, zero
to two primitives -- draws the difference, and a frame asking for a smaller one,
which is what a cold start, a loop wrap or a preview baker's rewind looks like,
restores from the nearest snapshot below it and walks forward. Snapshots are
taken every 32 primitives as the cache sweeps past them, so they cost memory and
no work.

Because the z-test is a strict `<` and the primitives are always applied in
increasing index order, "restore and walk forward" is *bit identical* to "walk
forward from zero" -- which is what makes the purity assertion in
`scripts/test-pipes.py` compare with `array_equal` and pass rather than nearly
pass. The growing tips are never put in the buffer at all: they are drawn into a
scratch copy each frame, so the pipe advances smoothly rather than a cell at a
time.

So a frame is two panel copies plus one capsule per growing pipe, which is why
`--pipes` is the knob that controls the cost, and it is flat: the frame at the
end of a run costs the same as the frame at the start.

Run:  python3 pipes.py --host 127.0.0.1
      python3 pipes.py --scheme brass --pipes 2
      python3 pipes.py --fill 0.45 --speed 3.0
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# --------------------------------------------------------------------------
# The lattice, the camera and the material.
# --------------------------------------------------------------------------

# Wide, shallow, short. A cube projected into 320x64 wastes most of the panel;
# this is chosen so that a full lattice just fills the frame.
NX, NY, NZ = 20, 4, 9

# Tube radius in lattice-spacing units. At 0.18 the tube is a bit over a third
# of the cell pitch, which is roughly the original's proportion and leaves clear
# black between two pipes running in adjacent cells -- and clear black between
# them is what lets the eye separate the layers.
TUBE_R = 0.18
JOINT_R = 0.26          # the elbow ball, fatter than the tube it joins

# Light in screen space, y down, z into the panel. Up and to the left and well
# in front of the viewer: negative z is towards the eye, so a surface facing the
# camera is lit, which keeps the tubes bright where they are nearest.
LIGHT = (-0.42, -0.55, -0.72)

AMBIENT = 0.16
DIFFUSE = 0.92
SPECULAR = 0.95
SHININESS = 22.0        # tight: a hard little highlight, not a soft sheen

# Shading tables. NA angle bins around the circle for the tube's screen
# direction, NU samples across the tube. 24 bins is 15 degrees apart, which is
# invisible on a 6px tube, and the whole table is 14 kB.
NA = 24
NU = 65                 # u = -1 .. +1 in steps of 1/32

SNAP_EVERY = 32         # world-buffer snapshots, in primitives; see the docstring

FAR = 1e9               # the empty z-buffer

# Colour schemes. The original picked a random material per pipe from a small
# palette and changed the whole scheme when the screen cleared; these are four
# such schemes, and each pipe in a run takes the next colour in its scheme.
# They are albedos, not final colours -- the specular is added white on top, so
# a saturated albedo still ends up with a chrome highlight.
SCHEMES = {
    "chrome": ((188, 204, 228), (128, 146, 172), (222, 232, 248)),
    "enamel": ((214, 58, 46), (48, 176, 92), (66, 112, 226)),
    "brass": ((206, 156, 52), (150, 100, 34), (232, 202, 128)),
    "candy": ((236, 84, 150), (96, 214, 214), (238, 206, 82)),
    "copper": ((198, 108, 62), (120, 172, 176), (226, 168, 110)),
}
SCHEME_ORDER = ("chrome", "enamel", "brass", "candy", "copper")

# The eraser bar that clears the screen between runs.
ERASER = (196, 232, 255)

DIRS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
OPPOSITE = (1, 0, 3, 2, 5, 4)

TUBE, JOINT, TEAPOT = 0, 1, 2

# The Utah teapot, in units of its own body radius, lying in the world XY plane
# so that the camera's yaw shows it three-quarters on. Body, lid, knob, spout
# and a two-piece handle: six parts, drawn with the same capsule and ball the
# pipes are drawn with, which is what lets it shrink with distance.
TEAPOT_PARTS = (
    (JOINT, (0.00, 0.00, 0.00), None, 1.00, 1.00),                  # body
    (TUBE, (-0.70, 0.84, 0.00), (0.70, 0.84, 0.00), 0.30, 0.30),    # lid
    (JOINT, (0.00, 1.22, 0.00), None, 0.24, 0.24),                  # knob
    (TUBE, (-0.66, -0.06, 0.00), (-1.90, 0.66, 0.00), 0.44, 0.14),  # spout
    (TUBE, (0.78, 0.62, 0.00), (1.62, 0.50, 0.00), 0.24, 0.24),     # handle
    (TUBE, (1.62, 0.50, 0.00), (1.92, -0.10, 0.00), 0.24, 0.24),
    (TUBE, (1.92, -0.10, 0.00), (1.52, -0.66, 0.00), 0.24, 0.24),
    (TUBE, (1.52, -0.66, 0.00), (0.80, -0.74, 0.00), 0.24, 0.24),
)
TEAPOT_S = 0.52         # body radius in lattice spacings: about 26 px across


def add_arguments(ap):
    ap.add_argument("--seed", type=int, default=7,
                    help="the walk, the schemes and the starting cells")
    ap.add_argument("--pipes", type=int, default=3,
                    help="pipes growing at once. This is the cost knob: a frame "
                         "rasterises one capsule per growing pipe and nothing else")
    ap.add_argument("--speed", type=float, default=2.4,
                    help="lattice cells per second, per pipe")
    ap.add_argument("--fill", type=float, default=0.30,
                    help="fraction of the lattice's cells a run consumes before "
                         "it stops. Above about 0.45 the panel turns to spaghetti")
    ap.add_argument("--runs", type=int, default=3,
                    help="distinct walks generated at build time, cycled in order")
    ap.add_argument("--scheme", default="",
                    help="force one colour scheme (%s)" % ", ".join(SCHEME_ORDER))
    ap.add_argument("--teapot", type=int, default=1,
                    help="chrome teapots per run, in place of an elbow, as the "
                         "original does. 0 turns the easter egg off")
    ap.add_argument("--hold", type=float, default=3.5,
                    help="seconds the finished lattice is held before the wipe")
    ap.add_argument("--wipe", type=float, default=1.6,
                    help="seconds for the eraser bar to cross the panel")
    ap.add_argument("--max-run", type=int, default=6,
                    help="longest straight run, in cells")
    ap.add_argument("--straight", type=float, default=0.62,
                    help="chance of carrying straight on rather than turning")
    ap.add_argument("--yaw", type=float, default=15.0,
                    help="camera yaw in degrees; 0 puts the lattice axes exactly "
                         "on the screen axes, which reads as a flat maze")
    ap.add_argument("--pitch", type=float, default=9.0, help="camera pitch, degrees")
    ap.add_argument("--depth-ratio", type=float, default=1.5,
                    help="far plane distance / near plane distance. 1.0 is "
                         "isometric; much above 1.6 and the near tubes bloat")
    ap.add_argument("--zoom", type=float, default=1.14,
                    help="over-scan: >1 lets the near layer run off the edges")
    ap.add_argument("--fog", type=float, default=0.42,
                    help="how much the far plane is darkened, 0..1")
    ap.add_argument("--no-aa", action="store_true",
                    help="hard pipe edges; cheaper, and much worse on a diagonal")


# --------------------------------------------------------------------------
# The camera.
#
# World -> yaw about Y -> pitch about X -> push away along Z -> perspective
# divide. The focal length and the screen centre are then *solved* from the
# projected lattice rather than guessed, so changing the lattice or the angles
# cannot silently push half the volume off the panel.
# --------------------------------------------------------------------------

class Camera(object):

    def __init__(self, args, width, height):
        yaw = np.radians(float(args.yaw))
        pitch = np.radians(float(args.pitch))
        self._cy, self._sy = np.cos(yaw), np.sin(yaw)
        self._cp, self._sp = np.cos(pitch), np.sin(pitch)

        # Every lattice node, in the rotated frame, to find the depth extent.
        i, j, k = np.meshgrid(np.arange(NX, dtype=f32),
                              np.arange(NY, dtype=f32),
                              np.arange(NZ, dtype=f32), indexing="ij")
        x = i.ravel() - (NX - 1) * 0.5
        y = j.ravel() - (NY - 1) * 0.5
        z = k.ravel() - (NZ - 1) * 0.5
        xr, yr, zr = self._rotate(x, y, z)

        # Pick the camera distance from the wanted near/far ratio rather than
        # nominating a number of cells, which means --depth-ratio is a
        # statement about how strong the perspective looks and stays true if
        # the lattice or the angles change.
        ratio = max(1.001, float(args.depth_ratio))
        dmin, dmax = float(zr.min()), float(zr.max())
        self.dist = (dmax - ratio * dmin) / (ratio - 1.0)
        zv = zr + self.dist

        # Fit: q and p are the projected coordinates at unit focal length.
        q = xr / zv
        p = -yr / zv
        margin = 1.0
        fx = (width - 2 * margin) / max(1e-6, float(q.max() - q.min()))
        fy = (height - 2 * margin) / max(1e-6, float(p.max() - p.min()))
        self.focal = min(fx, fy) * float(args.zoom)
        self.cx = width * 0.5 - self.focal * float(q.max() + q.min()) * 0.5
        self.cy = height * 0.5 - self.focal * float(p.max() + p.min()) * 0.5

        self.znear = float(zv.min())
        self.zfar = float(zv.max())

    def _rotate(self, x, y, z):
        xr = x * self._cy + z * self._sy
        zt = z * self._cy - x * self._sy
        yr = y * self._cp - zt * self._sp
        zr = y * self._sp + zt * self._cp
        return xr, yr, zr

    def offset(self, v):
        """A world-space *direction* into view space; rotation only, no shift."""
        return np.array(self._rotate(v[0], v[1], v[2]), f32)

    def view(self, i, j, k):
        """A lattice cell -> view-space (x, y, z), z increasing away."""
        xr, yr, zr = self._rotate(i - (NX - 1) * 0.5,
                                  j - (NY - 1) * 0.5,
                                  k - (NZ - 1) * 0.5)
        return xr, yr, zr + self.dist


# --------------------------------------------------------------------------
# The walk.
#
# A self-avoiding walk per pipe, all of them advanced in time order so that the
# pipes genuinely interleave rather than one finishing before the next starts.
# A pipe that paints itself into a corner dies and a new one is born elsewhere,
# which is exactly what the original does and is most of its character.
# --------------------------------------------------------------------------

def _walk(rng, args, ncolours):
    """One run: a list of primitives, each (kind, cell0, cell1, colour, t0, t1)."""
    occ = np.zeros((NX, NY, NZ), np.bool_)
    total = NX * NY * NZ
    budget = max(8, int(round(float(args.fill) * total)))
    speed = max(0.2, float(args.speed))
    npipes = max(1, int(args.pipes))
    maxrun = max(1, int(args.max_run))

    prims = []
    used = [0]
    colour_next = [0]

    def free(c):
        return (0 <= c[0] < NX and 0 <= c[1] < NY and 0 <= c[2] < NZ
                and not occ[c[0], c[1], c[2]])

    def step(c, d):
        v = DIRS[d]
        return (c[0] + v[0], c[1] + v[1], c[2] + v[2])

    def spawn(t):
        """A fresh pipe at a random free cell, or None if the lattice is full."""
        for _ in range(400):
            c = (rng.randint(NX), rng.randint(NY), rng.randint(NZ))
            if free(c):
                occ[c[0], c[1], c[2]] = True
                used[0] += 1
                colour = colour_next[0] % ncolours
                colour_next[0] += 1
                prims.append((JOINT, c, c, colour, t, t))
                return {"c": c, "d": -1, "t": t, "colour": colour}
        return None

    # Staggered starts, so three pipes do not pop into existence together.
    pipes = []
    for n in range(npipes):
        p = spawn(n * 0.55)
        if p is not None:
            pipes.append(p)

    while pipes and used[0] < budget:
        # Advance whichever pipe is furthest behind: the timeline stays
        # interleaved and every pipe stops at about the same moment.
        p = min(pipes, key=lambda q: q["t"])
        cand = [d for d in range(6) if free(step(p["c"], d))]
        if not cand:
            # Stuck. The pipe dies here; try to start another one somewhere
            # else, at the same moment, so the screen never stops growing.
            pipes.remove(p)
            fresh = spawn(p["t"])
            if fresh is not None:
                pipes.append(fresh)
            continue

        if p["d"] in cand and rng.rand() < float(args.straight):
            d = p["d"]
        else:
            turns = [d for d in cand if d != p["d"] and d != OPPOSITE[p["d"]]]
            pick = turns if turns else cand
            d = pick[rng.randint(len(pick))]

        want = 1 + rng.randint(maxrun)
        c = p["c"]
        n = 0
        while n < want:
            nxt = step(c, d)
            if not free(nxt):
                break
            occ[nxt[0], nxt[1], nxt[2]] = True
            used[0] += 1
            c = nxt
            n += 1
            if used[0] >= budget:
                break
        if n == 0:                      # should not happen; cand said it was free
            pipes.remove(p)
            continue

        t0 = p["t"]
        t1 = t0 + n / speed
        prims.append((TUBE, p["c"], c, p["colour"], t0, t1))
        prims.append((JOINT, c, c, p["colour"], t1, t1))
        p["c"] = c
        p["d"] = d
        p["t"] = t1

    return prims


# --------------------------------------------------------------------------
# Shading tables, baked once.
# --------------------------------------------------------------------------

def _tube_tables(colours):
    """[colour][angle bin][u bin] -> RGB, for a cylinder seen side on.

    The normal at lateral position u is `u * p + w * v` where p is the tube's
    screen perpendicular and v points at the viewer, so both the Lambert and the
    specular terms are (a shifted cosine of) u alone once the tube's screen
    angle is fixed. Hence a table rather than arithmetic.
    """
    lx, ly, lz = LIGHT
    n = np.sqrt(lx * lx + ly * ly + lz * lz)
    lx, ly, lz = lx / n, ly / n, lz / n
    # Blinn halfway vector between the light and the viewer at (0, 0, -1).
    hx, hy, hz = lx, ly, lz - 1.0
    n = np.sqrt(hx * hx + hy * hy + hz * hz)
    hx, hy, hz = hx / n, hy / n, hz / n

    u = np.linspace(-1.0, 1.0, NU).astype(f32)
    w = np.sqrt(np.maximum(0.0, 1.0 - u * u))

    tab = np.empty((len(colours), NA, NU, 3), f32)
    for a in range(NA):
        th = (a + 0.5) * 2.0 * np.pi / NA - np.pi
        px, py = np.cos(th), np.sin(th)
        lam = np.maximum(0.0, u * (px * lx + py * ly) + w * -lz)
        spc = np.maximum(0.0, u * (px * hx + py * hy) + w * -hz) ** SHININESS
        shade = AMBIENT + DIFFUSE * lam
        for ci, base in enumerate(colours):
            rgb = np.array(base, f32)[None, :] * shade[:, None]
            rgb = rgb + (255.0 * SPECULAR) * spc[:, None]
            tab[ci, a] = np.clip(rgb, 0.0, 255.0)
    return tab


def _joint_tables(colours):
    """[colour][ny*NU + nx] -> RGB for a sphere, plus the matching bulge table."""
    lx, ly, lz = LIGHT
    n = np.sqrt(lx * lx + ly * ly + lz * lz)
    lx, ly, lz = lx / n, ly / n, lz / n
    hx, hy, hz = lx, ly, lz - 1.0
    n = np.sqrt(hx * hx + hy * hy + hz * hz)
    hx, hy, hz = hx / n, hy / n, hz / n

    g = np.linspace(-1.0, 1.0, NU).astype(f32)
    nx = g[None, :] * np.ones((NU, 1), f32)
    ny = g[:, None] * np.ones((1, NU), f32)
    s = 1.0 - nx * nx - ny * ny
    inside = s > 0.0
    nz = -np.sqrt(np.maximum(s, 0.0))           # towards the viewer

    lam = np.maximum(0.0, nx * lx + ny * ly + nz * lz)
    spc = np.maximum(0.0, nx * hx + ny * hy + nz * hz) ** SHININESS
    shade = AMBIENT + DIFFUSE * lam

    tab = np.empty((len(colours), NU * NU, 3), f32)
    for ci, base in enumerate(colours):
        rgb = np.array(base, f32)[None, None, :] * shade[..., None]
        rgb = rgb + (255.0 * SPECULAR) * spc[..., None]
        tab[ci] = np.clip(rgb, 0.0, 255.0).reshape(NU * NU, 3)
    # -nz is the height of the surface above the equator, 0 outside the disc,
    # which doubles as the coverage mask.
    return tab, (-nz).reshape(NU * NU).astype(f32)


# --------------------------------------------------------------------------

def build(args):
    W, H = args.width, args.height
    rng = np.random.RandomState(args.seed & 0x7fffffff)
    cam = Camera(args, W, H)

    aa = not args.no_aa
    fog = float(np.clip(args.fog, 0.0, 1.0))
    fog_lo = 1.0 - fog
    span = max(1e-6, cam.zfar - cam.znear)
    fog_a = f32(-fog / span)
    fog_b = f32(1.0 + fog * cam.znear / span)

    # ---- the runs -----------------------------------------------------------
    names = [args.scheme] * int(args.runs) if args.scheme else None
    if names is None:
        start = rng.randint(len(SCHEME_ORDER))
        names = [SCHEME_ORDER[(start + n) % len(SCHEME_ORDER)]
                 for n in range(max(1, int(args.runs)))]
    for nm in names:
        if nm not in SCHEMES:
            raise ValueError("unknown scheme %r; have %s"
                             % (nm, ", ".join(SCHEME_ORDER)))

    hold = max(0.0, float(args.hold))
    wipe = max(0.2, float(args.wipe))

    runs = []
    starts = [0.0]
    for n, nm in enumerate(names):
        colours = SCHEMES[nm]
        prims = _walk(np.random.RandomState((args.seed * 7919 + n * 104729)
                                            & 0x7fffffff), args, len(colours))
        prims.sort(key=lambda pr: pr[5])        # stable: a joint follows its tube

        m = len(prims)
        kind = np.empty(m, np.int32)
        colour = np.empty(m, np.int32)
        t0 = np.empty(m, f32)
        t1 = np.empty(m, f32)
        p0 = np.empty((m, 3), f32)
        p1 = np.empty((m, 3), f32)
        for idx, (k, c0, c1, ci, a, b) in enumerate(prims):
            kind[idx] = k
            colour[idx] = ci
            t0[idx], t1[idx] = a, b
            p0[idx] = cam.view(*c0)
            p1[idx] = cam.view(*c1)

        # The teapot replaces an elbow, as it does in the original. It is put in
        # the second half of the sequence and in the nearer half of the volume,
        # for the plain reason that a teapot buried behind forty pipes is a
        # teapot nobody sees: late means little is drawn over it, near means it
        # is at its biggest.
        want_pots = max(0, int(args.teapot))
        if want_pots and m:
            mid = float(np.median(p0[:, 2]))
            sx = cam.cx + cam.focal * p0[:, 0] / p0[:, 2]
            sy = cam.cy - cam.focal * p0[:, 1] / p0[:, 2]
            pick = [i for i in range(m // 2, m)
                    if kind[i] == JOINT and p0[i, 2] < mid
                    and 22 < sx[i] < W - 22 and 15 < sy[i] < H - 15]
            if not pick:                        # a very sparse run: take anything
                pick = [i for i in range(m) if kind[i] == JOINT]
            trng = np.random.RandomState((args.seed * 31337 + n) & 0x7fffffff)
            for _ in range(min(want_pots, len(pick))):
                i = pick.pop(trng.randint(len(pick)))
                kind[i] = TEAPOT

        grow = float(t1.max()) if m else 1.0
        runs.append({
            "kind": kind, "colour": colour, "t0": t0, "t1": t1,
            "is_tube": kind == TUBE, "p0": p0, "p1": p1, "grow": grow,
            "tube": _tube_tables(colours),
            "joint": _joint_tables(colours),
        })
        starts.append(starts[-1] + grow + hold + wipe)
    period = starts[-1]
    cyc_start = np.array(starts[:-1], np.float64)

    # ---- per-frame buffers --------------------------------------------------
    out = np.zeros((H, W, 3), np.uint8)
    world_rgb = np.zeros((H, W, 3), np.uint8)       # the memoised world
    world_z = np.full((H, W), FAR, f32)
    scr_z = np.empty((H, W), f32)
    cols = np.arange(W, dtype=f32)
    rows = np.arange(H, dtype=f32)
    eraser = np.zeros((H, 2, 3), np.uint8)
    eraser[:] = np.array(ERASER, np.uint8)

    focal = f32(cam.focal)
    ccx, ccy = f32(cam.cx), f32(cam.cy)
    tube_r = f32(TUBE_R)
    joint_r = f32(JOINT_R)

    # The teapot's parts, rotated into view space once. They are offsets from
    # the elbow they replace, so the same list serves every teapot.
    teapot_parts = []
    for kind_, o0, o1, w0, w1 in TEAPOT_PARTS:
        v0 = cam.offset([c * TEAPOT_S for c in o0])
        v1 = cam.offset([c * TEAPOT_S for c in o1]) if o1 else None
        teapot_parts.append((kind_, v0, v1, w0 * TEAPOT_S, w1 * TEAPOT_S))

    def project(p):
        """View-space point -> (screen x, screen y, depth)."""
        inv = 1.0 / p[2]
        return ccx + focal * p[0] * inv, ccy - focal * p[1] * inv, p[2]

    # ---- the two rasterisers ------------------------------------------------
    #
    # Both write colour and depth into whatever pair of buffers they are given:
    # the memoised world for finished primitives, a scratch copy of it for the
    # growing tips. Neither allocates anything panel sized.

    def draw_tube(rgb, zb, a, b, tab, r_a, r_b, wr=tube_r):
        ax, ay, az = a
        bx, by, bz = b
        ex, ey = bx - ax, by - ay
        l2 = ex * ex + ey * ey
        if l2 < 1e-6:
            return
        ln = np.sqrt(l2)
        rr = max(r_a, r_b) + 1.0
        x_lo = max(0, int(np.floor(min(ax, bx) - rr)))
        x_hi = min(W, int(np.ceil(max(ax, bx) + rr)) + 1)
        y_lo = max(0, int(np.floor(min(ay, by) - rr)))
        y_hi = min(H, int(np.ceil(max(ay, by) + rr)) + 1)
        if x_hi <= x_lo or y_hi <= y_lo:
            return

        dx = cols[x_lo:x_hi] - ax
        dy = rows[y_lo:y_hi] - ay
        # Position along the axis, 0..1, and signed distance across it.
        u_ax = (dy[:, None] * (ey / l2)) + (dx[None, :] * (ex / l2))
        perp = (dx[None, :] * (ey / ln)) - (dy[:, None] * (ex / ln))
        rad = u_ax * (r_b - r_a) + r_a
        lat = perp / rad

        m = u_ax >= 0.0
        m &= u_ax <= 1.0
        alat = np.abs(lat)
        m &= alat <= 1.0

        bulge = 1.0 - lat * lat
        np.maximum(bulge, 0.0, out=bulge)
        np.sqrt(bulge, out=bulge)
        depth = u_ax * (bz - az) + az
        depth -= bulge * wr

        zt = zb[y_lo:y_hi, x_lo:x_hi]
        near = depth < zt
        if aa:
            # Coverage from the distance to the silhouette, in pixels. Depth is
            # only claimed by pixels more than half covered, so an antialiased
            # edge cannot occlude what is behind it.
            cov = (1.0 - alat) * rad + 0.5
            np.clip(cov, 0.0, 1.0, out=cov)
            m &= cov > 0.0
        m &= near
        if not m.any():
            return

        # The angle bin is the tube's screen perpendicular, which is constant
        # over the segment: one slice of the table serves the whole capsule.
        ang = np.arctan2(-ex / ln, ey / ln)
        ab = int((ang + np.pi) * (NA / (2.0 * np.pi))) % NA

        sel = lat[m]
        sel += 1.0
        sel *= (NU - 1) * 0.5
        col = tab[ab].take(sel.astype(np.int32), axis=0)

        d = depth[m]
        fade = d * fog_a + fog_b
        np.clip(fade, fog_lo, 1.0, out=fade)
        col *= fade[:, None]

        tile = rgb[y_lo:y_hi, x_lo:x_hi]
        if aa:
            c = cov[m]
            col *= c[:, None]
            col += tile[m] * (1.0 - c)[:, None]
            zm = m & (cov > 0.5)
            zt[zm] = depth[zm]
        else:
            zt[m] = d
        tile[m] = col.astype(np.uint8)

    def draw_joint(rgb, zb, p, tab, ztab, radius, wr=joint_r):
        px, py, pz = p
        rr = radius + 1.0
        x_lo = max(0, int(np.floor(px - rr)))
        x_hi = min(W, int(np.ceil(px + rr)) + 1)
        y_lo = max(0, int(np.floor(py - rr)))
        y_hi = min(H, int(np.ceil(py + rr)) + 1)
        if x_hi <= x_lo or y_hi <= y_lo:
            return
        k = (NU - 1) * 0.5 / radius
        ix = (cols[x_lo:x_hi] - px) * k + ((NU - 1) * 0.5 + 0.5)
        iy = (rows[y_lo:y_hi] - py) * k + ((NU - 1) * 0.5 + 0.5)
        np.clip(ix, 0, NU - 1, out=ix)
        np.clip(iy, 0, NU - 1, out=iy)
        flat = iy.astype(np.int32)[:, None] * NU + ix.astype(np.int32)[None, :]

        bulge = ztab.take(flat)
        m = bulge > 0.0
        depth = pz - bulge * wr
        zt = zb[y_lo:y_hi, x_lo:x_hi]
        m &= depth < zt
        if not m.any():
            return
        col = tab.take(flat[m], axis=0)
        d = depth[m]
        fade = d * fog_a + fog_b
        np.clip(fade, fog_lo, 1.0, out=fade)
        col *= fade[:, None]
        rgb[y_lo:y_hi, x_lo:x_hi][m] = col.astype(np.uint8)
        zt[m] = d

    def draw_teapot(rgb, zb, centre, ci, R):
        """The chrome teapot, assembled out of the two primitives we already have.

        The original spawns a Utah teapot instead of an elbow once in a while,
        and it is the single best easter egg available -- but a 22x15 pixel
        sprite of one cannot be scaled to the depth it lands at without falling
        apart, and a fixed-size sprite in a scene with perspective reads as a
        decal. So the teapot is *modelled*, in the only two shapes this renderer
        knows: a fat ball for the body, a small one for the knob, a squat
        capsule for the lid, a tapered one for the spout and a two-piece elbow
        for the handle. Seven draws, in the pipe's own colour, into the same
        z-buffer, and it shrinks with distance for free.
        """
        ball, ztab = R["joint"]
        tubetab = R["tube"][ci]
        for kind, o0, o1, w0, w1 in teapot_parts:
            a = centre + o0
            if kind == JOINT:
                x, y, z = project(a)
                draw_joint(rgb, zb, (x, y, z), ball[ci], ztab,
                           max(1.2, float(focal * w0 / z)), f32(w0))
            else:
                b = centre + o1
                ax, ay, az = project(a)
                bx, by, bz = project(b)
                draw_tube(rgb, zb, (ax, ay, az), (bx, by, bz), tubetab,
                          max(1.0, float(focal * w0 / az)),
                          max(1.0, float(focal * w1 / bz)), f32(w0))

    def draw_prim(R, i, rgb, zb, frac=1.0):
        """Primitive i of run R, revealed a fraction `frac` of the way along."""
        p0 = R["p0"][i]
        if R["kind"][i] == TEAPOT:
            draw_teapot(rgb, zb, p0, int(R["colour"][i]), R)
            return
        if R["kind"][i] == JOINT:
            x, y, z = project(p0)
            r = focal * joint_r / z
            draw_joint(rgb, zb, (x, y, z), R["joint"][0][R["colour"][i]],
                       R["joint"][1], max(1.2, float(r)))
            return
        p1 = R["p1"][i]
        if frac < 1.0:
            p1 = p0 + (p1 - p0) * frac
        ax, ay, az = project(p0)
        bx, by, bz = project(p1)
        ra = max(1.0, float(focal * tube_r / az))
        rb = max(1.0, float(focal * tube_r / bz))
        draw_tube(rgb, zb, (ax, ay, az), (bx, by, bz),
                  R["tube"][R["colour"][i]], ra, rb)

    # ---- the memoised world -------------------------------------------------
    #
    # world_rgb/world_z hold "every primitive with index < i of run `run`".
    # `snaps` are exact copies taken as the index sweeps past a multiple of
    # SNAP_EVERY, kept per run so that returning to an earlier run is cheap.
    cache = {"run": -1, "i": 0}
    snaps = {}

    def world_upto(ri, i):
        R = runs[ri]
        if cache["run"] != ri or i < cache["i"]:
            j = (i // SNAP_EVERY) * SNAP_EVERY
            while j > 0 and (ri, j) not in snaps:
                j -= SNAP_EVERY
            if j and (ri, j) in snaps:
                srgb, sz = snaps[(ri, j)]
                np.copyto(world_rgb, srgb)
                np.copyto(world_z, sz)
            else:
                j = 0
                world_rgb[:] = 0
                world_z[:] = FAR
            cache["run"] = ri
            cache["i"] = j
        k = cache["i"]
        while k < i:
            draw_prim(R, k, world_rgb, world_z)
            k += 1
            if k % SNAP_EVERY == 0 and (ri, k) not in snaps:
                snaps[(ri, k)] = (world_rgb.copy(), world_z.copy())
        cache["i"] = i

    def render(t, frame):
        tt = float(t) % period
        ri = int(np.searchsorted(cyc_start, tt, side="right")) - 1
        if ri < 0:
            ri = 0
        R = runs[ri]
        lt = tt - cyc_start[ri]
        grow = R["grow"]

        done = int(np.searchsorted(R["t1"], min(lt, grow), side="right"))
        world_upto(ri, done)
        np.copyto(out, world_rgb)

        if lt < grow:
            # The tips: at most one capsule per pipe, and the only thing in the
            # frame that is redrawn from one frame to the next. The live set is
            # found with a vectorised test over the whole primitive list rather
            # than by scanning a window forward from `done`. A window looks
            # safe and is not: the list is ordered by *finish* time, so a pipe
            # part way through a six-cell run can sit sixteen entries behind
            # three other pipes turning every cell, and it would stall.
            t0 = R["t0"]
            live = (t0 <= lt) & (lt < R["t1"]) & R["is_tube"]
            idx = np.nonzero(live)[0]
            if len(idx):
                np.copyto(scr_z, world_z)
                t1 = R["t1"]
                for j in idx:
                    frac = (lt - t0[j]) / max(1e-6, t1[j] - t0[j])
                    draw_prim(R, int(j), out, scr_z, frac)
        elif lt >= grow + hold:
            # The eraser. A bright bar crosses the panel and leaves black
            # behind it -- the original just blanked, but at 20 fps a blank
            # frame reads as a dropped frame rather than as a decision. The bar
            # is off the right edge four fifths of the way through the wipe, so
            # the cycle ends on a beat of pure black rather than cutting from a
            # half-erased panel straight into the next run.
            k = (lt - grow - hold) / wipe * 1.25
            e = int(k * (W + 4))
            if e > 0:
                out[:, :min(e, W)] = 0
            if e < W:
                out[:, e:min(e + 2, W)] = eraser[:, :min(2, W - e)]
        return out

    render.camera = cam
    render.runs = runs
    render.period = period
    render.cycle_starts = cyc_start
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
