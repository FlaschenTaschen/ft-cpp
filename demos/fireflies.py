#!/usr/bin/env python3
"""A field of fireflies that spontaneously synchronise.

Each insect is an oscillator: a phase that runs at its own natural rate and
flashes when it wraps. Nothing tells them what to do together — a firefly only
nudges its phase towards the ones it can see, and out of a few hundred
independent blinkers waves of synchrony assemble themselves, sweep across the
panel, collide and fall apart again.

The coupling is *local*, not mean field. Every firefly feeling every other
equally is cheaper, but then the whole field snaps into unison at once and
stays there, which is a much duller thing to watch. Coupling by neighbourhood
instead lets separate patches agree on different rhythms, and the boundaries
between them are what travel. Getting that without an O(N^2) neighbour search:
splat each firefly's phase as a unit vector into a coarse grid, blur the grid,
and sample it back at each position. O(N) plus one tiny blur, and the blur
radius *is* the coupling range (--range).

It also never finishes. A field that locks perfectly is a static field, so the
natural frequencies wander slowly (an Ornstein-Uhlenbeck drift over --wander
seconds) and there is per-step phase noise. There is no fixed consensus to
converge on, so the field organises, holds for a while, and is pulled apart
again — any half-minute window catches some part of the arc.

The defaults are tuned by measurement, not by eye, since a single frame cannot
show synchrony. `render.state` exposes the phases, so the Kuramoto order
parameter — the length of the mean unit phase vector, 0 scattered, 1 locked —
can be traced over a long run. At these settings the *local* order sits around
0.73 within a few seconds (neighbourhoods lock fast), while the *global* order
roams the whole range 0.05 to 0.83 indefinitely, and every 30 second window
contains a swing of at least 0.2. Both extremes are worth watching: high
global order is the whole panel breathing in unison, low global order is two
or three domains out of step and a front visibly crossing between them.

Run:  python3 fireflies.py --host 127.0.0.1
      python3 fireflies.py --flies 500 --coupling 1.4 --range 70
      python3 fireflies.py --coupling 0 --spread 0.3   # never syncs, for contrast
"""

import sys

import numpy as np

import demoscene as ds

f32 = np.float32
TAU = f32(2.0 * np.pi)


def add_arguments(ap):
    ap.add_argument("--flies", type=int, default=0,
                    help="0 scales with the canvas, about one per 64 pixels")
    ap.add_argument("--rate", type=float, default=0.85,
                    help="base blink rate, Hz")
    ap.add_argument("--coupling", type=float, default=0.85,
                    help="how hard a neighbourhood pulls a phase, Hz; 0 = never syncs")
    ap.add_argument("--range", type=float, default=22.0,
                    help="coupling radius in pixels (sigma of the grid blur)")
    ap.add_argument("--spread", type=float, default=0.32,
                    help="natural frequency variation, as a fraction of --rate")
    ap.add_argument("--noise", type=float, default=0.07,
                    help="phase jitter, turns per sqrt(second)")
    ap.add_argument("--wander", type=float, default=20.0,
                    help="seconds over which natural frequencies re-drift; 0 = fixed")
    ap.add_argument("--decay", type=float, default=0.24,
                    help="flash decay time constant, seconds")
    ap.add_argument("--drift", type=float, default=2.6,
                    help="positional drift speed, pixels/sec")
    ap.add_argument("--glow", type=float, default=0.62,
                    help="halo brightness relative to a firefly's core")
    ap.add_argument("--no-grass", dest="grass", action="store_false",
                    help="skip the silhouette along the bottom")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


# --------------------------------------------------------------------------


def _blur_matrix(n, sigma):
    """Row-normalized Gaussian smoothing operator for a length-n axis.

    The coupling grid is tiny (tens of cells), so an explicit matrix is both
    exact and cheaper than iterating a separable kernel, and normalizing per
    row makes the edges behave without any padding.
    """
    i = np.arange(n, dtype=f32)
    m = np.exp(-0.5 * ((i[:, None] - i[None, :]) / max(sigma, 1e-3)) ** 2)
    return (m / m.sum(1, keepdims=True)).astype(f32)


def _blur5(a, out, tmp):
    """One pass of a separable [1,4,6,4,1]/16 blur over an (H,W) float array."""
    tmp[:] = 6.0 * a
    tmp[:, 1:] += 4.0 * a[:, :-1]
    tmp[:, :-1] += 4.0 * a[:, 1:]
    tmp[:, 2:] += a[:, :-2]
    tmp[:, :-2] += a[:, 2:]
    out[:] = 6.0 * tmp
    out[1:] += 4.0 * tmp[:-1]
    out[:-1] += 4.0 * tmp[1:]
    out[2:] += tmp[:-2]
    out[:-2] += tmp[2:]
    out *= 1.0 / 256.0
    return out


def _grass(W, H, rng):
    """A per-column height for the silhouette along the bottom.

    Smooth clumps with the odd taller blade over them. Purely to give the
    fireflies a floor to be in front of; it is drawn as black, so on an LED
    panel it costs nothing and reads as depth.
    """
    x = np.arange(W, dtype=f32)
    base = (0.075 * H
            + 0.035 * H * np.sin(x * 0.031 + 0.7)
            + 0.022 * H * np.sin(x * 0.091 + 2.1)
            + 0.015 * H * np.sin(x * 0.213 + 4.4))
    height = np.maximum(base, 1.0)
    for _ in range(max(3, W // 14)):          # blades standing proud of the mass
        c = int(rng.integers(0, W))
        tall = float(rng.uniform(0.14, 0.30)) * H
        span = int(rng.integers(0, 2))
        for d in range(-span, span + 1):
            height[(c + d) % W] = max(height[(c + d) % W],
                                      tall * (1.0 - 0.35 * abs(d)))
    return np.minimum(height, 0.34 * H).astype(np.int32)


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    # One firefly per 64 pixels: 320 on a 320x64 panel. A fixed count would
    # turn a small canvas into a solid mat of light.
    N = max(1, args.flies or int(round(W * H / 64.0)))

    # --- the swarm -------------------------------------------------------
    x = rng.uniform(0, W, N).astype(f32)
    # Weighted towards the lower half: fireflies hover over the grass, and it
    # also puts most of the light where the silhouette can cut into it.
    y = ((0.18 + 0.80 * rng.random(N) ** 0.7) * H).astype(f32)
    # Meandering rather than straight lines: each fly walks along a heading
    # that turns at its own slow rate.
    heading = rng.uniform(0, TAU, N).astype(f32)
    turn = rng.uniform(-0.7, 0.7, N).astype(f32)
    speed = (args.drift * rng.uniform(0.3, 1.0, N)).astype(f32)

    f0 = (args.rate * (1.0 + args.spread * rng.standard_normal(N))).astype(f32)
    f0 = np.maximum(f0, 0.08 * args.rate)
    freq = f0.copy()
    theta = rng.random(N).astype(f32)            # phase, in turns
    lum = rng.random(N).astype(f32) * 0.2        # flash envelope
    size = rng.uniform(0.55, 1.0, N).astype(f32)  # not every bug is as bright

    # --- coupling grid ---------------------------------------------------
    # Cell size is picked so the blur sigma lands around six cells: fine
    # enough that the neighbourhood has a shape, coarse enough that the grid
    # stays a few hundred cells and the blur is free.
    cell = max(2.0, args.range / 6.0)
    gw = max(2, int(np.ceil(W / cell)))
    gh = max(2, int(np.ceil(H / cell)))
    sig = args.range / cell
    Bx = _blur_matrix(gw, sig)
    By = _blur_matrix(gh, sig)
    gc = np.empty((gh, gw), f32)
    gs = np.empty((gh, gw), f32)
    gn = np.empty((gh, gw), f32)

    # --- output ----------------------------------------------------------
    # Warm yellow-green with a hot core. The ramp spends most of its length
    # dim: on an 8-bit-PWM panel the bright end is what carries the image and
    # a broad mid-tone would only band.
    lut = ds.gradient([(0.00, (0, 0, 0)), (0.10, (7, 11, 0)),
                       (0.34, (68, 82, 3)), (0.60, (152, 190, 18)),
                       (0.82, (232, 250, 95)), (1.00, (255, 255, 205))])
    core = np.zeros((H, W), f32)
    tight = np.zeros((H, W), f32)
    wide = np.zeros((H, W), f32)
    tmp = np.zeros((H, W), f32)
    field = np.zeros((H, W), f32)

    # A lone lit pixel is nothing on a panel this size — the earlier starfield
    # learned that the hard way — so each firefly is a small core plus a soft
    # halo, built by blurring the splat twice at different widths. Normalizing
    # each by its own peak response keeps --glow meaning "halo relative to
    # core" rather than an arbitrary gain.
    imp = np.zeros((H, W), f32)
    imp[H // 2, W // 2] = 1.0
    k_tight = 1.0 / float(_blur5(imp, np.empty_like(imp), tmp)[H // 2, W // 2])
    acc = imp
    for _ in range(4):
        acc = _blur5(acc, np.empty_like(imp), tmp)
    k_wide = 1.0 / float(acc[H // 2, W // 2])

    if args.grass:
        gheight = _grass(W, H, rng)
        rows = np.arange(H, dtype=np.int32)[:, None]
        grass_mask = rows >= (H - gheight)[None, :]
        # The tips catch light from anything close, which is what makes the
        # silhouette read as grass rather than as a bitten-off edge.
        tip_mask = (grass_mask & ~np.roll(grass_mask, 1, axis=0)).astype(f32)
        tip_mask[0] = 0.0
    else:
        grass_mask = None
        tip_mask = None

    state = {"phase": theta, "x": x, "y": y, "freq": freq, "local_r": 0.0}
    last_t = [0.0]

    def render(t, idx):
        # Clamp dt: a stall must not teleport every phase a full cycle, which
        # would look like the whole field glitching at once.
        dt = float(np.clip(t - last_t[0], 0.0, 0.1))
        last_t[0] = t

        cth = np.cos(TAU * theta)
        sth = np.sin(TAU * theta)

        # --- local coupling ---------------------------------------------
        gi = np.clip((x / cell).astype(np.int32), 0, gw - 1)
        gj = np.clip((y / cell).astype(np.int32), 0, gh - 1)
        flat = gj * gw + gi
        nb = gw * gh
        gc.ravel()[:] = np.bincount(flat, cth, nb)
        gs.ravel()[:] = np.bincount(flat, sth, nb)
        gn.ravel()[:] = np.bincount(flat, None, nb)
        bc = By @ gc @ Bx.T
        bs = By @ gs @ Bx.T
        bn = By @ gn @ Bx.T
        # Normalizing by the blurred count makes coupling a property of the
        # neighbourhood's *agreement*, not of how crowded it happens to be.
        np.reciprocal(np.maximum(bn, 1e-3, out=bn), out=bn)
        C = (bc * bn)[gj, gi]
        S = (bs * bn)[gj, gi]

        # Kuramoto pull: r_local * sin(2pi*(psi_local - theta)), expanded so
        # no atan2 is needed. Its magnitude is the local order parameter, so a
        # scattered neighbourhood exerts almost no pull and a coherent one
        # yanks hard — which is why a synced patch recruits its border and the
        # domain grows outward as a wave.
        pull = args.coupling * (S * cth - C * sth)

        step = (freq + pull) * dt
        if args.noise:
            step += args.noise * np.sqrt(dt) * rng.standard_normal(N).astype(f32)
        # `theta += ...` here would rebind a local; every state array has to
        # be updated through its buffer.
        theta[:] += step
        flashed = theta >= 1.0
        theta[:] -= np.floor(theta)

        # Natural frequencies wander, so there is never a fixed consensus for
        # the field to settle into permanently. Without this the panel locks
        # after half a minute and then does nothing for the rest of the hour.
        if args.wander > 0:
            k = dt / args.wander
            freq[:] += (f0 - freq) * k
            freq[:] += (args.spread * args.rate * np.sqrt(2.0 * k)
                        * rng.standard_normal(N)).astype(f32)
            np.maximum(freq, 0.08 * args.rate, out=freq)

        # --- flash envelope: instant attack, soft decay ------------------
        lum[:] *= np.exp(-dt / max(args.decay, 1e-3))
        np.copyto(lum, size, where=flashed)

        # --- drift --------------------------------------------------------
        heading[:] += turn * dt
        x[:] += speed * np.cos(heading) * dt
        y[:] += speed * np.sin(heading) * dt
        np.mod(x, W, out=x)
        # Bounce off the top and bottom rather than wrapping, so nothing
        # teleports through the grass line.
        over = (y < 0.5) | (y > H - 1.5)
        heading[over] = -heading[over]
        np.clip(y, 0.5, H - 1.5, out=y)

        # --- draw ---------------------------------------------------------
        # Bilinear splat: at 2-3 px/s a rounded position visibly ratchets, and
        # the whole point of the drift is that it should not be noticeable.
        core[:] = 0.0
        xi = x.astype(np.int32)
        yi = y.astype(np.int32)
        fx = x - xi
        fy = y - yi
        x1 = (xi + 1) % W
        y1 = np.minimum(yi + 1, H - 1)
        flatpix = core.ravel()
        for px, py, w in ((xi, yi, (1 - fx) * (1 - fy)), (x1, yi, fx * (1 - fy)),
                          (xi, y1, (1 - fx) * fy), (x1, y1, fx * fy)):
            flatpix += np.bincount(py * W + px, lum * w, W * H).astype(f32)

        _blur5(core, tight, tmp)          # one pass: the core, ~3 px across
        _blur5(tight, wide, tmp)
        _blur5(wide, field, tmp)          # field is scratch here, overwritten
        _blur5(field, wide, tmp)          # four passes: the halo, ~9 px across
        field[:] = (0.95 * k_tight) * tight
        field[:] += (args.glow * k_wide) * wide

        if grass_mask is not None:
            # Blades are opaque; their tips pick up whatever is glowing near.
            field[:] *= 0.9 * tip_mask + (1.0 - grass_mask)

        np.clip(field, 0.0, 1.0, out=field)
        state["local_r"] = float(np.hypot(C, S).mean())
        return lut[(field * 255.0).astype(np.uint8)]

    render.state = state          # lets a harness measure the order parameter
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
