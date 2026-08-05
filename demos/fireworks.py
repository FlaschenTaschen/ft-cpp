#!/usr/bin/env python3
"""A particle-system fireworks display.

Shells launch from the bottom at random x, rise with gravity slowing them,
and burst at the apex into a shower of sparks that fall and fade. Three shell
types keep successive bursts from looking alike: a plain sphere, a willow that
hangs and droops, and a crackle of short bright flecks.

Everything is one fixed-size pool of particles in flat numpy arrays — position,
velocity, remaining life, colour — integrated with whole-array operations and
recycled through a dead-slot mask. Nothing grows, nothing is allocated per
particle, so every frame costs the same whether one shell is up or six.

Trails come from a decay buffer: the previous frame is multiplied down by
--trail before the new particles are drawn over it with `np.maximum.at`, so the
brightest contribution wins where sparks overlap instead of array order.

Run:  python3 fireworks.py --host 127.0.0.1
      python3 fireworks.py --rate 2.5 --sparks 140 --trail 0.9 --palette fire
"""

import sys

import numpy as np

import demoscene as ds

DEAD, SHELL, SPARK = 0, 1, 2

# Per shell type: spark speed, drag, gravity scale, life range, whiteness,
# count multiplier.
#
# Speed and drag are tuned as a pair. What sets the size of a burst is the
# terminal radius speed/drag, and on 64 rows that has to stay near 16-18px or
# the shower just fills the panel and reads as a flash. What sets whether the
# burst *radiates* is the speed alone: below roughly 1px per frame the sparks
# creep outwards and the decay buffer paints a solid blob instead of rays. So
# both numbers are high and their ratio is small — a fast expansion that stops.
#
# The gravity scales are small for the same reason: a spark falling at a
# realistic 1g leaves a 64 row panel in well under a second.
SHELL_TYPES = {
    #           speed drag  grav  life-lo life-hi white count
    "sphere":  (100.0, 5.5, 0.20, 0.90, 1.50, 0.55, 1.00),
    "willow":  ( 62.0, 3.6, 0.13, 1.90, 3.00, 0.25, 0.85),
    "crackle": (140.0, 8.0, 0.12, 0.30, 0.60, 0.85, 1.20),
}


def add_arguments(ap):
    ap.add_argument("--rate", type=float, default=2.0, help="shells per second")
    ap.add_argument("--sparks", type=int, default=80,
                    help="sparks per burst (scaled per shell type)")
    ap.add_argument("--gravity", type=float, default=150.0,
                    help="px/s^2 on a 64 row panel; scaled with --height")
    ap.add_argument("--trail", type=float, default=0.93,
                    help="decay buffer multiplier per 1/60s; 0 = no trails")
    ap.add_argument("--glow", type=float, default=0.38,
                    help="spread each spark into its neighbours by this much")
    ap.add_argument("--types", default="sphere,willow,crackle",
                    help="comma separated subset of %s" % ",".join(SHELL_TYPES))
    ap.add_argument("--palette", default="random",
                    choices=sorted(ds.PALETTES) + ["rainbow", "random"],
                    help="'random' gives every shell its own hue")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def build(args):

    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)

    types = [s.strip() for s in args.types.split(",") if s.strip()]
    bad = [s for s in types if s not in SHELL_TYPES]
    if bad or not types:
        raise SystemExit("--types: unknown %s" % (bad or "(empty)",))

    # Everything below is written for the 320x64 wall and scaled from there, so
    # a different geometry keeps the same proportions rather than the same
    # pixel counts. Height is what matters: the vertical is the scarce axis.
    scale = ds.f32(H / 64.0)
    gravity = ds.f32(args.gravity) * scale

    lut = None if args.palette == "random" else ds.named_palette(args.palette)

    # One pool, sized for the shells that can plausibly be in the air at once
    # plus slack. Running out only drops sparks from the newest burst.
    cap = int(args.sparks * 1.2 * max(1.0, args.rate) * 3.5) + 256

    px = np.zeros(cap, ds.f32)
    py = np.zeros(cap, ds.f32)
    vx = np.zeros(cap, ds.f32)
    vy = np.zeros(cap, ds.f32)
    life = np.zeros(cap, ds.f32)
    life0 = np.ones(cap, ds.f32)
    gsc = np.zeros(cap, ds.f32)
    drag = np.zeros(cap, ds.f32)
    col = np.zeros((cap, 3), ds.f32)
    kind = np.zeros(cap, np.uint8)
    flick = np.zeros(cap, bool)
    stype = np.zeros(cap, np.uint8)          # shell type index, shells only
    hue = np.zeros(cap, ds.f32)

    accum = np.zeros((H, W, 3), ds.f32)      # the trail / decay buffer
    frame_rgb = np.zeros((H, W, 3), np.uint8)
    out = np.zeros((H, W, 3), ds.f32)

    def alloc(n):
        """Hand back up to n dead slots. Recycling beats growing a list."""
        free = np.flatnonzero(kind == DEAD)
        return free[:n]

    def pick_colours(base_hue, n, spread):
        if lut is None:
            h = (base_hue + rng.normal(0.0, spread, n)).astype(ds.f32)
            return ds.hsv_to_rgb(h, ds.f32(0.85), ds.f32(1.0)).astype(ds.f32) * 255.0
        return lut[rng.integers(150, 256, n)].astype(ds.f32)

    def launch():
        idx = alloc(1)
        if idx.size == 0:
            return
        i = idx[0]
        # Aim the apex at a random height in the upper part of the panel, and
        # solve for the launch speed rather than fixing it — that way the fuse
        # is exactly the time to apex and shells always burst at the top of
        # their arc, on any height.
        rise = H * rng.uniform(0.55, 0.85)
        v0 = float(np.sqrt(2.0 * gravity * rise))
        px[i] = rng.uniform(0.08, 0.92) * W
        py[i] = H - 1.0
        vx[i] = rng.uniform(-0.10, 0.10) * v0
        vy[i] = -v0
        life[i] = life0[i] = v0 / float(gravity) * rng.uniform(0.92, 1.02)
        gsc[i] = 1.0
        drag[i] = 0.12
        kind[i] = SHELL
        flick[i] = False
        stype[i] = types.index(rng.choice(types))
        hue[i] = rng.random()
        # The rising shell is a warm ember, not the burst colour: the reveal is
        # half the effect.
        col[i] = (255.0, 190.0, 110.0)

    def burst(i):
        name = types[stype[i]]
        speed, dr, gs, lo, hi, white, count = SHELL_TYPES[name]
        n = int(args.sparks * count * rng.uniform(0.75, 1.25))
        idx = alloc(n)
        n = idx.size
        if n == 0:
            return
        # Uniform directions on a sphere, projected to the screen. The limb of
        # the sphere piles up at the edge of the disc, which is what gives a
        # real burst its bright rim — a flat 2D circle reads as a uniform
        # spray, which was the failure mode to avoid here.
        u = rng.uniform(-1.0, 1.0, n)
        phi = rng.uniform(0.0, 2.0 * np.pi, n)
        s = np.sqrt(np.maximum(0.0, 1.0 - u * u))
        v = speed * scale * rng.uniform(0.55, 1.0, n)
        vx[idx] = vx[i] * 0.35 + v * s * np.cos(phi)
        # 0.7 on the vertical: a circular burst wide enough to read on a 320
        # wide panel is taller than the 64 rows we have, so squash it into an
        # ellipse. It still reads as round because the eye takes the panel's
        # aspect as the frame.
        vy[idx] = vy[i] * 0.35 + v * u * 0.70
        px[idx] = px[i]
        py[idx] = py[i]
        life[idx] = life0[idx] = rng.uniform(lo, hi, n)
        gsc[idx] = gs
        drag[idx] = dr
        kind[idx] = SPARK
        flick[idx] = name == "crackle"
        col[idx] = pick_colours(hue[i], n, 0.05 if name != "willow" else 0.02)
        if white > 0:                       # a flash of white at the core
            hot = (rng.random(n) < white * 0.35)[:, None]
            col[idx] = np.where(hot, np.minimum(255.0, col[idx] * 0.4 + 200.0),
                                col[idx])

    last_t = [0.0]
    next_launch = [0.25]

    def render(t, frame_idx):
        dt = ds.f32(min(0.1, max(0.0, t - last_t[0])))
        last_t[0] = t

        while t >= next_launch[0]:
            launch()
            # Exponential gaps rather than a metronome: fireworks arrive in
            # clumps, and a fixed interval reads as a machine.
            next_launch[0] += float(np.clip(rng.exponential(1.0 / max(args.rate, 1e-3)),
                                            0.12, 6.0))

        a = kind != DEAD                    # every update below is masked by it
        if a.any():
            # Linear drag, exact over the step, so a large dt cannot go
            # unstable the way an explicit (1 - drag*dt) can.
            damp = np.exp(-drag[a] * dt)
            vy[a] = (vy[a] + gravity * gsc[a] * dt) * damp
            vx[a] = vx[a] * damp
            px[a] = px[a] + vx[a] * dt
            py[a] = py[a] + vy[a] * dt
            life[a] = life[a] - dt

            # Off-panel particles are dead; keep a wide margin at the top so a
            # burst that overshoots can fall back into view.
            gone = ((life <= 0.0) | (py > H + 3.0) | (px < -12.0) | (px > W + 12.0)
                    | (py < -H)) & a
            if gone.any():
                # Shells that reached their fuse become a burst. Only a couple
                # per second, so a Python loop over them is free; everything
                # else stays whole-array.
                for i in np.flatnonzero(gone & (kind == SHELL)):
                    if life[i] <= 0.0 and -H < py[i] <= H:
                        burst(int(i))
                kind[gone] = DEAD

        # Fade the previous frame instead of clearing it. Raising the decay to
        # dt*60 keeps the trail the same length in seconds at any frame rate.
        if args.trail > 0.0:
            # In place: `accum *= ...` would rebind accum as a local.
            accum[:] *= ds.f32(max(0.0, args.trail) ** (float(dt) * 60.0))
        else:
            accum[:] = 0.0

        draw = kind != DEAD
        if draw.any():
            frac = np.clip(life[draw] / life0[draw], 0.0, 1.0)
            bright = frac ** 0.6
            if flick[draw].any():
                bright = np.where(flick[draw],
                                  bright * rng.uniform(0.25, 1.0, bright.size),
                                  bright)
            c = col[draw] * bright[:, None]
            x0 = px[draw]
            y0 = py[draw]
            sx = vx[draw] * dt
            sy = vy[draw] * dt
            is_shell = kind[draw] == SHELL

            def plot(fx, fy, cc):
                ix = fx.astype(np.int32)
                iy = fy.astype(np.int32)
                on = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < H)
                if on.any():
                    np.maximum.at(accum, (iy[on], ix[on]), cc[on])

            # Two samples along the step so a fast spark is a short streak
            # rather than a dotted line; the decay buffer carries the rest of
            # the trail.
            plot(x0, y0, c)
            plot(x0 - sx * 0.5, y0 - sy * 0.5, c * 0.75)
            # A rising shell is a single pixel in 20480 of them, and single
            # pixels carry no weight on an LED wall. Give it a core so the
            # launch is legible before the burst.
            if is_shell.any():
                hx, hy, hc = x0[is_shell], y0[is_shell], c[is_shell] * 0.75
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    plot(hx + dx, hy + dy, hc)

        np.copyto(out, accum)
        # Cheap spatial glow: each pixel also lights its neighbours a little.
        # Single pixel sparks otherwise read as noise on an LED wall.
        g = ds.f32(args.glow)
        if g > 0.0:
            np.maximum(out[:, 1:], accum[:, :-1] * g, out=out[:, 1:])
            np.maximum(out[:, :-1], accum[:, 1:] * g, out=out[:, :-1])
            np.maximum(out[1:], accum[:-1] * g, out=out[1:])
            np.maximum(out[:-1], accum[1:] * g, out=out[:-1])

        np.clip(out, 0.0, 255.0, out=out)
        np.copyto(frame_rgb, out.astype(np.uint8))
        return frame_rgb

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
