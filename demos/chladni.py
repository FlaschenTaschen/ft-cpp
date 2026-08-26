#!/usr/bin/env python3
"""Cymatics: sand on a vibrating plate, finding the nodal lines.

Drive a metal plate at one of its resonances and the sand on it does not
scatter -- it migrates. Grains sitting on an antinode are thrown up and land
somewhere else; grains that happen to land on a nodal line, where the plate is
not moving, stay put. Run that for a few seconds and the sand has drawn the
mode shape. Sweep the drive frequency to the next resonance and the whole
figure comes apart and reassembles into a different one, which is the part
worth watching.

The plate here is the wall: 320x64, so **5:1**, and that is what makes this
worth doing. The Chladni figures everybody has seen are square-plate ones,
where the two modes that superpose are (n,m) and (m,n) -- degenerate by
symmetry, which is why the closed form is the familiar

    s = cos(n.pi.x).cos(m.pi.y) - cos(m.pi.x).cos(n.pi.y)

On a 5:1 plate that symmetry is gone: swapping the indices no longer gives you
the same frequency, so you cannot pair a mode with its own transpose. What you
can do is solve for the pairs that *are* degenerate. A mode with `a` half-waves
along the long axis and `b` across sits at a frequency proportional to
(a/5)^2 + b^2, so (a,b) and (c,d) are degenerate exactly when

    a^2 + 25.b^2 = c^2 + 25.d^2

and the plate's figure at that frequency is the difference of the two:

    s = cos(a.pi.u).cos(b.pi.v) - cos(c.pi.u).cos(d.pi.v)

Whole families fall out of that -- (10,1)/(5,2), (20,2)/(10,4), (25,4)/(20,5)
-- and because the two members of a pair can have wildly different aspect (one
term fine across and coarse along, the other the reverse) the figures are far
more varied than the square-plate ones. See MODES for the six that were picked
out of a survey of every exactly-degenerate pair up to 30 half-waves.

The sand is a few thousand particles. Each frame a grain reads the precomputed
gradient of |s| under it, steps down it -- away from the antinodes, toward the
still parts -- and is kicked by a random amount *proportional to the local
amplitude*. That last term is what makes it read as sand rather than as a
plotted contour: a grain on a node is in dead air and stops moving, a grain on
an antinode is being thrown around. Grains land in a deposit buffer that
decays over about a third of a second, so a nodal line builds up bright
instead of flickering as a few loose pixels.

The frequency sweep is an interpolation between two mode fields rather than a
cut, so the nodal set of the blend moves continuously and the grains follow it
-- and the jitter is boosted through the middle of a sweep, since a plate
being driven between two resonances is mostly just shaking. That is the
reorganisation, and it is the show.

Everything per-mode is precomputed: the field, |s|, and both components of its
gradient, all as flat arrays. A frame during a hold is a couple of gathers per
grain, a bincount, a decay and a palette lookup -- no trigonometry at all. Only
during a sweep is a field derived, and that is a dozen whole-array passes over
20480 pixels.

Run:  python3 chladni.py --host 127.0.0.1
      python3 chladni.py --grains 6000 --palette copper
      python3 chladni.py --hold 3 --sweep 4    # more sweeping, less sitting
"""

import math
import sys

import numpy as np

import demoscene as ds


# --------------------------------------------------------------------------
# The modes.
#
# Each entry is (a, b, c, d) for
#
#     s = cos(a.pi.u).cos(b.pi.v) - cos(c.pi.u).cos(d.pi.v)
#
# with u across the long axis and v across the short one, both in 0..1, and
# every pair satisfying a^2 + 25b^2 = c^2 + 25d^2 so the two terms really are
# the same frequency on a 5:1 plate. They are listed in ascending frequency,
# because the demo is a sweep and a sweep should go somewhere.
#
# Chosen by rendering all 24 exactly-degenerate pairs with a,c <= 30 and
# b,d <= 6 and looking at them. The rejects were mostly too fine -- anything
# with six half-waves across 64 rows is a 10 px feature that turns to hash once
# grains land on it -- plus several near-duplicates of ones already here. What
# is left alternates deliberately between lattice figures (diamonds, X's,
# rings) and organic ones (ribs, arches, waves), so no two consecutive
# transitions look like the same trick.
# --------------------------------------------------------------------------
MODES = [
    (10, 1,  5, 2),   # f=5.0    big diagonals with arches slung under them
    (14, 1, 11, 2),   # f=8.8    wavy vertical ribs, tuning-fork junctions
    (15, 1,  5, 3),   # f=10.0   starbursts strung along a bright centre line
    (16, 3,  9, 4),   # f=19.2   horizontal waves crossed by vertical spines
    (20, 2, 10, 4),   # f=20.0   diamond lattice with a ring inside each cell
    (25, 4, 20, 5),   # f=41.0   clover shapes caged in a finer diamond grid
]

# Sand on dark metal. The bottom of the ramp is not black but a very dark blue
# steel, which is where the plate's own vibration lives -- an antinode catching
# the light. Above that it is dry quartz sand: grey-brown where it is thin,
# warm tan where it has piled up, and near-white on the ridge of a nodal line
# that has been collecting for a second.
SAND = [(0.00, (0, 0, 0)), (0.10, (10, 13, 20)), (0.22, (34, 32, 34)),
        (0.45, (120, 92, 56)), (0.72, (214, 172, 104)),
        (0.90, (245, 220, 168)), (1.00, (255, 250, 235))]

# Iron filings on a copper plate, which is the other honest version of this.
COPPER = [(0.00, (0, 0, 0)), (0.12, (22, 8, 4)), (0.30, (86, 30, 10)),
          (0.58, (190, 88, 26)), (0.82, (245, 165, 70)),
          (1.00, (255, 236, 200))]

# The photograph: white powder, black plate, no colour at all.
MONO = [(0.00, (0, 0, 0)), (0.14, (16, 16, 18)), (0.35, (70, 70, 74)),
        (0.65, (160, 162, 168)), (1.00, (255, 255, 255))]

LOCAL = {"sand": SAND, "copper": COPPER, "mono": MONO}


def add_arguments(ap):
    ap.add_argument("--grains", type=int, default=5000,
                    help="sand grains; scaled with panel area")
    ap.add_argument("--hold", type=float, default=5.5,
                    help="seconds held on each resonance")
    ap.add_argument("--sweep", type=float, default=2.5,
                    help="seconds spent sliding to the next one")
    ap.add_argument("--speed", type=float, default=26.0,
                    help="px/s a grain drifts down the amplitude gradient")
    ap.add_argument("--jitter", type=float, default=13.0,
                    help="px/s of random walk at full amplitude; 0 on a node")
    ap.add_argument("--creep", type=float, default=1.5,
                    help="px/s of random walk everywhere, including on a "
                         "node; keeps settled grains sliding along the line")
    ap.add_argument("--shake", type=float, default=2.2,
                    help="extra jitter through the middle of a sweep, as a "
                         "multiple; this is what scatters the old figure")
    ap.add_argument("--persist", type=float, default=0.35,
                    help="half-life in seconds of the deposit buffer")
    ap.add_argument("--gain", type=float, default=1.3,
                    help="exposure; how many grains per pixel reads as white")
    ap.add_argument("--plate", type=float, default=0.11,
                    help="brightness of the plate's own vibration under the "
                         "sand, 0..1; 0 leaves the background black")
    ap.add_argument("--palette", default="sand",
                    choices=sorted(LOCAL) + sorted(ds.PALETTES) + ["rainbow"],
                    help="colour ramp")
    ap.add_argument("--settle", type=int, default=110,
                    help="steps run inside build() so frame 0 is a figure "
                         "rather than a cloud of loose sand")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def _palette(name, size=256):
    if name in LOCAL:
        return ds.gradient(LOCAL[name], size)
    return ds.named_palette(name, size)


def build(args):

    W, H = args.width, args.height
    N = W * H
    f32 = ds.f32
    rng = np.random.default_rng(args.seed or None)

    # Grain count is a density. The defaults are for the 320x64 wall; the same
    # 3600 grains on a quarter of the area would bury every nodal line.
    n = max(64, int(round(args.grains * (N / (320.0 * 64.0)))))
    fps = max(1.0, float(args.fps))

    # --- The mode fields ---------------------------------------------------
    #
    # Separable: each term is an outer product of two 1-D cosine tables, so a
    # whole field is two outer products and a subtract. Six of them cost about
    # a millisecond to build, which is the entire argument for precomputing
    # them -- evaluating this per pixel per frame would be four cosines over
    # 20480 pixels and would not fit the budget on a Pi.
    u = (np.arange(W, dtype=f32) + f32(0.5)) / f32(W)
    v = (np.arange(H, dtype=f32) + f32(0.5)) / f32(H)

    def mode_field(a, b, c, d):
        s = (np.cos(a * np.pi * u)[None, :] * np.cos(b * np.pi * v)[:, None]
             - np.cos(c * np.pi * u)[None, :] * np.cos(d * np.pi * v)[:, None])
        s = s.astype(f32)
        return s / f32(max(float(np.abs(s).max()), 1e-6))

    fields = [mode_field(*m) for m in MODES]
    nmodes = len(fields)

    # Scratch for the sweep, allocated once.
    s_mix = np.empty((H, W), f32)
    s_add = np.empty((H, W), f32)
    amp_s = np.empty((H, W), f32)
    gx_s = np.empty((H, W), f32)
    gy_s = np.empty((H, W), f32)

    def derive(s, amp, gx, gy):
        """|s| and its gradient, normalised so the steepest slope is 1.

        Grains descend the gradient of the *amplitude*, not of the signed
        field: |s| has a valley along every nodal line and a peak at every
        antinode, which is exactly the landscape a shaken grain rolls down.
        The kink in |s| at a node is not a problem -- it is the reason a grain
        arrives at a node at full speed and then sits in a one pixel trough.

        Normalising by the steepest slope in the field is what lets --speed
        mean the same thing for a coarse mode and a fine one; without it the
        fine figures would advect five times faster and settle before the
        transition into them had finished.
        """
        np.abs(s, out=amp)
        gx[:, 1:-1] = amp[:, 2:]
        gx[:, 1:-1] -= amp[:, :-2]
        gx[:, 1:-1] *= f32(0.5)
        gx[:, 0] = amp[:, 1] - amp[:, 0]
        gx[:, -1] = amp[:, -1] - amp[:, -2]
        gy[1:-1] = amp[2:]
        gy[1:-1] -= amp[:-2]
        gy[1:-1] *= f32(0.5)
        gy[0] = amp[1] - amp[0]
        gy[-1] = amp[-1] - amp[-2]
        # max of |gx| and |gy| rather than the true magnitude: it avoids a
        # sqrt over the whole field on every sweep frame and is within a
        # factor of root two, which --speed absorbs.
        scale = f32(1.0 / max(float(np.abs(gx).max()),
                              float(np.abs(gy).max()), 1e-6))
        gx *= scale
        gy *= scale

    # Per mode: the amplitude field and both gradient components, kept flat so
    # a grain's sample is one np.take with no index arithmetic beyond y*W+x.
    AMP, GXF, GYF = [], [], []
    for s in fields:
        amp = np.empty((H, W), f32)
        gx = np.empty((H, W), f32)
        gy = np.empty((H, W), f32)
        derive(s, amp, gx, gy)
        AMP.append(amp)
        GXF.append(gx.reshape(-1))
        GYF.append(gy.reshape(-1))
    AMPF = [a.reshape(-1) for a in AMP]

    # --- Grains ------------------------------------------------------------
    px = rng.uniform(0.0, W - 1.0, n).astype(f32)
    py = rng.uniform(0.0, H - 1.0, n).astype(f32)
    ix = np.empty(n, np.int64)
    iy = np.empty(n, np.int64)
    idx = np.empty(n, np.int64)
    gsx = np.empty(n, f32)
    gsy = np.empty(n, f32)
    amps = np.empty(n, f32)
    kick = np.empty(n, f32)
    tmp = np.empty(n, f32)

    hi_x = f32(W - 1.001)
    hi_y = f32(H - 1.001)
    two_x = f32(2.0 * (W - 1.001))
    two_y = f32(2.0 * (H - 1.001))

    # --- The deposit buffer ------------------------------------------------
    #
    # Drawing grains as discrete dots gives a nodal line that flickers: a
    # single pixel is lit only on the frames a grain happens to be standing on
    # it. A buffer that grains add into and that decays with a half-life in
    # *seconds* fixes that and is also cheaper than any kind of dot sprite --
    # it is one bincount and one multiply.
    #
    # The amount deposited is derived from the decay rather than given: each
    # frame the buffer loses (1 - decay) of its mass and gains n * amount, so
    # setting amount = (1 - decay) makes a cell's steady-state value equal the
    # number of grains standing on it per frame. --gain then means "grains per
    # pixel that read as white" and stays true whatever --grains and --persist
    # are set to, which is the only way these two knobs stop fighting.
    decay = f32(0.5 ** (1.0 / max(args.persist * fps, 1e-3)))
    amount = f32(1.0 - float(decay))
    dep = np.zeros(N, f32)
    depv = dep.reshape(H, W)                 # a view, for the palette pass
    land = np.empty(N, f32)

    step_len = f32(args.speed / fps)
    # A random walk's spread grows as the square root of the number of steps,
    # so the per-step sigma has to scale as 1/sqrt(fps) for the sand to be
    # equally lively at 20 fps and at 30.
    jitter = f32(args.jitter / math.sqrt(fps))
    # A grain that reaches a node sees amplitude zero, so the term above goes
    # to zero and it stops dead -- forever, in exactly the pixel it landed in.
    # The nodal lines that result are legible but they are *dotted*, with the
    # gaps frozen in place for the whole hold, which reads as a dashed line
    # somebody drew rather than as sand. A small amplitude-independent term
    # keeps settled grains shuffling *along* the trough they are sitting in
    # (across it they are pushed straight back), so the line fills in and
    # keeps breathing. It has to stay well under the gradient step or it
    # widens the line into a band.
    creep = f32(args.creep / math.sqrt(fps))

    # Every array operation from here down writes through `out=`, and not only
    # to avoid allocating. `dep *= decay` inside one of these closures would
    # make `dep` a *local* of that closure and raise on the first frame -- and
    # the same mistake on a buffer that is read before it is written would not
    # raise at all, it would quietly render a valid frame of the wrong thing.
    # There is no augmented assignment to a closed-over array in this file.
    def step(gxf, gyf, ampf, shake):
        # Cell under each grain. Clipping first means the cast to int is a
        # plain truncation of a non-negative number, ie a floor, with no
        # separate np.floor pass.
        np.clip(px, f32(0.0), hi_x, out=px)
        np.clip(py, f32(0.0), hi_y, out=py)
        ix[:] = px
        iy[:] = py
        np.multiply(iy, W, out=idx)
        np.add(idx, ix, out=idx)

        # Deposit where the grain is standing now. bincount is the cheap
        # whole-array scatter-add; np.add.at on the same data is an order of
        # magnitude slower.
        np.multiply(dep, decay, out=dep)
        np.multiply(np.bincount(idx, minlength=N), amount, out=land)
        np.add(dep, land, out=dep)

        np.take(gxf, idx, out=gsx)
        np.take(gyf, idx, out=gsy)
        np.take(ampf, idx, out=amps)

        # Downhill on |s|: away from the antinodes, into the still parts.
        np.multiply(gsx, step_len, out=gsx)
        np.multiply(gsy, step_len, out=gsy)
        np.subtract(px, gsx, out=px)
        np.subtract(py, gsy, out=py)

        # Amplitude-proportional noise. This is the whole difference between
        # sand and a contour plot: where the plate is moving the grains are
        # being thrown around, and where it is not they are simply lying
        # there. A constant jitter blurs the nodal lines into bands and never
        # lets anything settle.
        np.multiply(amps, f32(float(jitter) * float(shake)), out=kick)
        np.add(kick, f32(float(creep) * float(shake)), out=kick)
        rng.standard_normal(n, dtype=np.float32, out=tmp)
        np.multiply(tmp, kick, out=tmp)
        np.add(px, tmp, out=px)
        rng.standard_normal(n, dtype=np.float32, out=tmp)
        np.multiply(tmp, kick, out=tmp)
        np.add(py, tmp, out=py)

        # Reflect off the edges rather than clamping. A clamp pins every grain
        # that reaches an edge against it, and since the long edges of this
        # plate are antinodes for most of these modes, that would paint two
        # bright rails along the top and bottom within seconds -- which is the
        # first thing that went wrong here.
        np.abs(px, out=px)
        np.subtract(two_x, px, out=tmp)
        np.minimum(px, tmp, out=px)
        np.abs(px, out=px)
        np.abs(py, out=py)
        np.subtract(two_y, py, out=tmp)
        np.minimum(py, tmp, out=py)
        np.abs(py, out=py)

    # --- Timeline ----------------------------------------------------------
    hold = max(0.0, args.hold)
    sweep = max(0.05, args.sweep)
    period = hold + sweep
    cycle = period * nmodes

    # Settle the sand before anyone looks. From a uniform scatter it takes a
    # couple of seconds for a figure to appear, and on the wall that is two
    # seconds of a segment spent looking broken -- and in a preview it is the
    # entire clip.
    for _ in range(max(0, args.settle)):
        step(GXF[0], GYF[0], AMPF[0], f32(1.0))

    lut = _palette(args.palette)
    gain = f32(args.gain)
    plate = f32(np.clip(args.plate, 0.0, 1.0))
    shade = np.empty((H, W), f32)
    glow = np.empty((H, W), f32)
    out = np.empty((H, W, 3), np.uint8)
    index = np.empty((H, W), np.uint8)
    # Ordered dither on the palette *index* rather than on the RGB: same
    # effect for a third of the arithmetic, since it is one add over (H, W)
    # instead of over (H, W, 3). The plate glow is a smooth ramp living in the
    # bottom tenth of the ramp, which is exactly where an LED panel bands.
    tile = np.tile(ds._BAYER8, (-(-H // 8), -(-W // 8)))[:H, :W].astype(f32)

    def render(t, frame_idx):
        phase = t % cycle
        i = int(phase // period)
        if i >= nmodes:                      # only reachable on a float edge
            i = nmodes - 1
        local = phase - i * period
        j = (i + 1) % nmodes

        if local < hold:
            # Holding a resonance: nothing to derive, the tables are already
            # right. This is roughly two thirds of the run.
            gxf, gyf, ampf, amp = GXF[i], GYF[i], AMPF[i], AMP[i]
            shake = f32(1.0)
        else:
            # Sweeping. Blend the two *fields* and take the gradient of the
            # blend, rather than blending the two gradient fields: the nodal
            # set of a superposition is a real curve that moves continuously
            # from one figure to the other, whereas averaging two gradients
            # gives grains two places to go at once and they split the
            # difference into a smear. k is smoothstepped so the sweep eases
            # in and out instead of starting and stopping with a jolt.
            r = (local - hold) / sweep
            k = r * r * (3.0 - 2.0 * r)
            np.multiply(fields[i], f32(1.0 - k), out=s_mix)
            np.multiply(fields[j], f32(k), out=s_add)
            np.add(s_mix, s_add, out=s_mix)
            derive(s_mix, amp_s, gx_s, gy_s)
            gxf, gyf, ampf, amp = (gx_s.reshape(-1), gy_s.reshape(-1),
                                   amp_s.reshape(-1), amp_s)
            # Off resonance the plate is mostly just shaking, so everything
            # loose gets thrown about. This is what makes a transition read as
            # the sand coming apart and re-settling rather than as a
            # crossfade between two pictures.
            shake = f32(1.0 + args.shake * math.sin(math.pi * k))

        step(gxf, gyf, ampf, shake)

        # One scalar field through one palette. The sand is the deposit; the
        # plate's own motion is a dim floor under it, so an antinode is dark
        # steel rather than dead black and the figure sits on something.
        np.multiply(depv, gain, out=shade)
        np.multiply(amp, plate, out=glow)
        np.add(shade, glow, out=shade)
        np.multiply(shade, f32(255.0), out=shade)
        np.add(shade, tile, out=shade)
        np.clip(shade, f32(0.0), f32(255.0), out=shade)
        index[:] = shade
        np.take(lut, index, axis=0, out=out)
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
