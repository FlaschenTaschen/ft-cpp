#!/usr/bin/env python3
"""Physarum slime mould: an emergent transport network.

Thousands of agents wander a trail map. Each step an agent samples the map at
three points ahead — left, centre, right, at --sensor-angle and --sensor-dist —
turns towards whichever reads strongest, moves forward, and deposits a little
trail where it lands. The map is then blurred a touch and decayed. That is the
whole rule; nothing in here draws a line. The veins, junctions and loops are
what a few thousand agents following each other's leavings settle into, and
they keep rewiring for as long as you leave it running.

Three things beyond the basic rule earn their keep, and each fixes a specific
way this fails on a 320x64 wrapping panel:

  --cap    clamps the map, so a fat strand is no more attractive than a thin
           one and cannot monopolise the whole population.
  --food   a few slow-drifting attractant sources. Without them the network
           relaxes into motionless vertical stripes within a couple of
           minutes; foraging is what keeps it re-solving.
  --spore  periodic local inoculations, which nucleate new colonies that grow
           and fuse into the network while starved branches are pruned.

Everything is whole-array. Agents are flat x/y/heading arrays; sensing is three
gathers into the flattened map; depositing is one np.bincount; the blur is a
separable three-tap done as shifted adds. Nothing iterates over agents, which
is what keeps this at under 2 ms a frame.

Run:  python3 slime.py --host 127.0.0.1
      python3 slime.py --agents 24000 --sensor-dist 5 --palette ice
      python3 slime.py --food 0 --spore-rate 0   # watch it die into stripes
"""

import sys

import numpy as np

import demoscene as ds


def add_arguments(ap):
    ap.add_argument("--agents", type=int, default=16000,
                    help="agent count; scaled with panel area")
    ap.add_argument("--speed", type=float, default=0.7,
                    help="px moved per step")
    ap.add_argument("--turn", type=float, default=25.0,
                    help="degrees turned per step toward the best sensor")
    ap.add_argument("--sensor-angle", type=float, default=35.0,
                    help="degrees off-heading for the side sensors")
    ap.add_argument("--sensor-dist", type=float, default=3.0,
                    help="px ahead the sensors look; sets filament spacing")
    ap.add_argument("--deposit", type=float, default=1.0,
                    help="trail laid per agent, in units of the equilibrium "
                         "mean (so it means the same at any --agents)")
    ap.add_argument("--decay", type=float, default=0.94,
                    help="map multiplier per step; lower prunes harder")
    ap.add_argument("--blur", type=float, default=0.18,
                    help="fraction of each cell replaced by its 3x3 blur")
    ap.add_argument("--cap", type=float, default=3.0,
                    help="clamp on the trail map, in equilibrium means; the "
                         "anti-monopoly rule, 0 = uncapped")
    ap.add_argument("--noise", type=float, default=0.02,
                    help="trail sprinkled everywhere each step, same units")
    ap.add_argument("--gain", type=float, default=3.0,
                    help="display exposure; lower is darker and thinner")
    ap.add_argument("--gamma", type=float, default=1.3,
                    help="display curve; >1 darkens the halo around a strand")
    ap.add_argument("--steps", type=int, default=1,
                    help="simulation steps per frame")
    ap.add_argument("--warmup", type=int, default=220,
                    help="steps run in build() so frame 0 already has network")
    ap.add_argument("--food", type=int, default=18,
                    help="drifting attractant sources; 0 = none")
    ap.add_argument("--food-strength", type=float, default=0.18,
                    help="peak attractant, in units of --cap")
    ap.add_argument("--food-radius", type=float, default=2.0, help="px")
    ap.add_argument("--food-speed", type=float, default=4.0,
                    help="px per second a source drifts")
    ap.add_argument("--spore-rate", type=float, default=1.4,
                    help="new colonies nucleated per second; 0 = none")
    ap.add_argument("--spore-size", type=float, default=0.03,
                    help="fraction of the agents each new colony takes")
    ap.add_argument("--spore-radius", type=float, default=5.0,
                    help="px across which a new colony is inoculated")
    ap.add_argument("--wander", type=float, default=0.06,
                    help="chance per step of a random flick of heading")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")
    ds.palette_argument(ap, "toxic")


def build(args):

    W, H = args.width, args.height
    N = W * H
    rng = np.random.default_rng(args.seed or None)

    # Agent count is a density, not an absolute: the defaults are for the
    # 320x64 wall, and a smaller canvas wants proportionally fewer or the map
    # saturates instantly.
    n = max(64, int(round(args.agents * (N / (320.0 * 64.0)))))

    # --- The deposit / decay balance --------------------------------------
    #
    # This is the one thing that decides whether you get a network, a smear or
    # a white sheet, and the two knobs are not independent. Each step the map
    # loses (1 - decay) of its mass and gains n * deposit; the fixed point is
    # a total of n * deposit / (1 - decay). Written that way the useful
    # quantity is the *mean* trail value at equilibrium, so --deposit is
    # expressed in exactly that unit and the raw per-agent amount is derived:
    #
    #     raw = deposit * (1 - decay) * pixels / agents
    #
    # which means changing --agents or --decay rescales brightness not at all,
    # and only changes how sharply the map prunes. That decoupling is what
    # made this tunable — with a raw deposit the two knobs fight, and every
    # attempt to sharpen the structure also blew out the exposure.
    #
    # What is left to choose is decay alone. At 0.98 the map keeps ~50 steps
    # of history: strands merge faster than they can be pruned and inside
    # twenty seconds the panel is a uniform lit field. At 0.85 a strand is
    # gone in seven steps, no agent downstream ever smells it, and you get
    # drifting noise that never organises. 0.94 is ~16 steps of memory: long
    # enough that a strand outlives the gap between agents crossing it,
    # short enough that a strand nobody uses is invisible within half a
    # second. That is what gives the pruning its liveliness.
    #
    # The display then only has to expose it: filaments run several times the
    # equilibrium mean, so --gain maps that mean to about a third of the ramp
    # and the busy strands take the bright end. Background is genuinely near
    # zero, which is exactly what the LED wall wants — an unlit LED is true
    # black, so bright structure on black beats any full-field glow.
    decay = ds.f32(np.clip(args.decay, 0.0, 0.999))
    deposit = ds.f32(args.deposit * (1.0 - float(decay)) * N / n)

    # The cap is the anti-monopoly rule, and it does more for the look than any
    # other single number. Uncapped, trail value is winner-take-all: the first
    # strand to get busy reads brighter, so it out-attracts its neighbours, so
    # it gets busier, and within a minute two or three fat strands hold the
    # whole population and everything else is dark. Clamping the map at a few
    # times the equilibrium mean makes a busy strand no more attractive than a
    # merely-used one, and the traffic spreads over a network instead. It also
    # fixes the exposure for free: nothing can exceed the cap, so --gain can
    # map the cap to white and be right at every moment of the run.
    #
    # The noise floor is much smaller but does a related job: it keeps a faint
    # everywhere-gradient so an agent in an empty region has something to
    # follow, and it roughens strands enough that they keep branching.
    cap = ds.f32(max(0.0, args.cap) * args.deposit)
    noise = ds.f32(max(0.0, args.noise) * args.deposit * (1.0 - float(decay)))
    speed = ds.f32(args.speed)
    turn = ds.f32(np.radians(args.turn))
    sens_a = ds.f32(np.radians(args.sensor_angle))
    sens_d = ds.f32(args.sensor_dist)
    blur = ds.f32(np.clip(args.blur, 0.0, 1.0))
    wander = float(np.clip(args.wander, 0.0, 1.0))

    # Rates are given per second and converted to steps here, so a demo run at
    # a different --fps or --steps behaves the same way in wall-clock terms.
    steps_per_sec = max(1.0, args.fps * max(1, args.steps))
    spore_every = (int(round(steps_per_sec / args.spore_rate))
                   if args.spore_rate > 0.0 else 0)
    clock = [0]

    trail = np.zeros((H, W), ds.f32)
    flat = trail.reshape(-1)                     # a view; sensing gathers here
    scratch = np.empty((H, W), ds.f32)
    scratch2 = np.empty((H, W), ds.f32)

    # --- Agents ------------------------------------------------------------
    ax = rng.uniform(0.0, W, n).astype(ds.f32)
    ay = rng.uniform(0.0, H, n).astype(ds.f32)
    ah = rng.uniform(0.0, 2.0 * np.pi, n).astype(ds.f32)

    # Scratch, allocated once. A frame does no allocation beyond the odd
    # boolean temporary numpy makes for us.
    sx = np.empty(n, ds.f32)
    sy = np.empty(n, ds.f32)
    idx = np.empty(n, np.int64)
    rows = np.empty(n, np.int64)
    cols = np.empty(n, np.int64)

    def sample(angle_off, out):
        """Trail value sens_d ahead of each agent, angle_off off its heading."""
        a = ah + angle_off
        np.cos(a, out=sx)
        np.sin(a, out=sy)
        # Wrapping rather than bouncing. On a 5:1 panel a wrap is invisible —
        # a strand simply continues off one edge and on at the other, and the
        # network reads as a piece of a larger one. Bouncing piles agents into
        # bright ridges along all four walls, which is the first thing that
        # went wrong here.
        np.multiply(sx, sens_d, out=sx)
        np.multiply(sy, sens_d, out=sy)
        np.add(sx, ax, out=sx)
        np.add(sy, ay, out=sy)
        np.mod(np.floor(sx).astype(np.int64), W, out=cols)
        np.mod(np.floor(sy).astype(np.int64), H, out=rows)
        np.multiply(rows, W, out=idx)
        np.add(idx, cols, out=idx)
        np.take(flat, idx, out=out)

    sl = np.empty(n, ds.f32)
    sc = np.empty(n, ds.f32)
    sr = np.empty(n, ds.f32)

    def step():
        sample(-sens_a, sl)
        sample(ds.f32(0.0), sc)
        sample(sens_a, sr)

        # Classic Jones steering: hold course when the centre reads best, turn
        # towards the better side when it does not, and pick a side at random
        # when the centre is the worst of the three (an agent sitting in a
        # trough has no reason to prefer either). The random case is what lets
        # a strand split and a dead end give up.
        best_c = (sc >= sl) & (sc >= sr)
        left = sl > sr
        rand = (~best_c) & (sl < sc) & (sr < sc)
        delta = np.where(left, -turn, turn)
        delta = np.where(rand, np.where(rng.random(n) < 0.5, -turn, turn), delta)
        ah[:] += np.where(best_c, ds.f32(0.0), delta)

        # A trickle of random heading changes. Without it every agent ends up
        # captive to some strand and the network freezes into a still picture
        # after a minute or two; with it there is always a little traffic
        # prospecting off-network, which is what keeps it rerouting all night.
        if wander > 0.0:
            flick = rng.random(n) < wander
            ah[:] += flick * rng.uniform(-0.6, 0.6, n).astype(ds.f32)

        np.cos(ah, out=sx)
        np.sin(ah, out=sy)
        ax[:] += sx * speed
        ay[:] += sy * speed
        np.mod(ax, ds.f32(W), out=ax)
        np.mod(ay, ds.f32(H), out=ay)

        # Deposit. bincount over the flattened index is the cheap whole-array
        # scatter-add — np.add.at on the same data is roughly twenty times
        # slower and would not fit the budget on its own.
        np.mod(np.floor(ax).astype(np.int64), W, out=cols)
        np.mod(np.floor(ay).astype(np.int64), H, out=rows)
        np.multiply(rows, W, out=idx)
        np.add(idx, cols, out=idx)
        flat[:] += np.bincount(idx, minlength=N).astype(ds.f32) * deposit

        diffuse()
        trail[:] *= decay
        feed()
        if noise > 0.0:
            trail[:] += rng.random((H, W), dtype=ds.f32) * noise
        if cap > 0.0:
            np.minimum(trail, cap, out=trail)
        if spore_every:
            clock[0] += 1
            if clock[0] >= spore_every:
                clock[0] = 0
                spore()

    # --- Food: what stops the network relaxing into stripes ------------------
    #
    # A colony left alone on a wrapping panel does not stay a network. Strands
    # merge, curvature costs an agent turns it would rather not make, and the
    # whole thing undergoes something very like curve shortening: loops shrink
    # away, bends straighten, and after two or three minutes all that is left
    # is a set of dead-straight vertical lines — the shortest closed paths the
    # 64 row wrap admits. It is stable, it is symmetrical, and it is dull, and
    # no amount of decay tuning fixes it because it is the *end state* of the
    # tuning, not a failure of it.
    #
    # Real Physarum does not do that because it is foraging. So there are a
    # few attractant sources drifting slowly around the panel, each pushing a
    # small gaussian of trail into the map every step. The network has to
    # reach them, which forces junctions and bends that curve shortening
    # cannot remove, and because they keep moving it has to keep re-solving
    # the problem — a branch is abandoned and a new one grows to replace it,
    # continuously, for as long as it runs. They also read on the panel as the
    # bright nodes of the network, which is what the shape wants anyway.
    #
    # Each source is one separable outer product over the panel, which is why
    # a handful of them costs almost nothing.
    #
    # Strength is the delicate part. At --food-strength 1 a source paints a
    # solid white disc that dwarfs the filaments and looks drawn on, which is
    # exactly what this effect must not look like; a fifth of the cap is enough
    # to bend traffic towards it while still reading as a node of the network
    # rather than a blob. And like the agent count it is a *density*: the same
    # 18 sources on a quarter-size canvas is four times the attractant.
    nfood = 0 if args.food <= 0 else max(1, int(round(args.food * N / (320.0 * 64.0))))
    fx = rng.uniform(0.0, W, nfood)
    fy = rng.uniform(0.0, H, nfood)
    fdir = rng.uniform(0.0, 2.0 * np.pi, nfood)
    food_step = args.food_speed / steps_per_sec
    food_peak = ds.f32(args.food_strength * max(args.cap, 1.0) * args.deposit)
    fsig2 = ds.f32(2.0 * max(0.5, args.food_radius) ** 2)
    gx = np.arange(W, dtype=ds.f32)
    gy = np.arange(H, dtype=ds.f32)

    def feed():
        if nfood == 0:
            return
        # A slowly meandering heading, not a straight line: sources that fly
        # in straight lines drag the network into parallel streaks.
        fdir[:] += rng.normal(0.0, 0.12, nfood)
        fx[:] = np.mod(fx + np.cos(fdir) * food_step, W)
        fy[:] = np.mod(fy + np.sin(fdir) * food_step, H)
        for i in range(nfood):
            dx = np.abs(gx - fx[i])
            np.minimum(dx, W - dx, out=dx)
            dy = np.abs(gy - fy[i])
            np.minimum(dy, H - dy, out=dy)
            trail[:] += (np.exp(-(dy * dy) / fsig2)[:, None]
                         * np.exp(-(dx * dx) / fsig2)[None, :]) * food_peak

    # --- Spores: new colonies -----------------------------------------------
    #
    # Food keeps the network from relaxing into stripes, but it does not add
    # anything new. Every so often a batch of agents is therefore inoculated at
    # one random spot, which nucleates a colony that grows its own filaments,
    # reaches out and fuses with the network — while the branches it steals
    # traffic from starve and are pruned. That is the visible growth: something
    # appears in an empty part of the panel and joins up.
    #
    # It has to be a batch in one place. Scattering the same agents
    # individually does nothing at all: a lone agent's deposits decay before a
    # second one crosses them, so it simply joins the nearest existing strand.
    spore_n = max(8, int(n * args.spore_size))
    blob = max(2.0, args.spore_radius)

    def spore():
        cx = rng.uniform(0.0, W)
        cy = rng.uniform(0.0, H)
        # A disc of agents all heading *outwards*. Dropping them on a point
        # with random headings instead gives a tiny closed orbit that traps
        # the whole batch: it shows up as a bright dot that never becomes
        # anything, and the panel slowly fills with confetti. Facing them out
        # makes the batch expand into a patch and then break into filaments,
        # which is what a real inoculation does.
        a = rng.uniform(0.0, 2.0 * np.pi, spore_n)
        r = blob * np.sqrt(rng.random(spore_n))
        who = rng.integers(0, n, spore_n)
        ax[who] = np.mod(cx + r * np.cos(a), W).astype(ds.f32)
        ay[who] = np.mod(cy + r * np.sin(a), H).astype(ds.f32)
        ah[who] = a.astype(ds.f32)

    def diffuse(mix=None):
        """Separable 3-tap [1,2,1]/4 blur, wrapping, as shifted adds.

        Without any diffusion a strand is one pixel wide and no sensor a few
        px off it ever finds it, so nothing aggregates. Full replacement by
        the blur melts the strands together. --blur mixes the two, and the
        mix fraction is effectively how wide a strand is allowed to get.
        """
        mix = blur if mix is None else ds.f32(mix)
        if mix <= 0.0:
            return
        s = scratch
        # Horizontal.
        np.multiply(trail, ds.f32(2.0), out=s)
        s[:, 1:] += trail[:, :-1]
        s[:, 0] += trail[:, -1]
        s[:, :-1] += trail[:, 1:]
        s[:, -1] += trail[:, 0]
        np.multiply(s, ds.f32(0.25), out=s)
        # Vertical, back into trail as a weighted mix with the original.
        blurred = scratch2
        np.multiply(s, ds.f32(2.0), out=blurred)
        blurred[1:] += s[:-1]
        blurred[0] += s[-1]
        blurred[:-1] += s[1:]
        blurred[-1] += s[0]
        np.multiply(blurred, ds.f32(0.25), out=blurred)
        trail[:] *= ds.f32(1.0) - mix
        trail[:] += blurred * mix

    # --- Seeding: structure has to be there before anyone looks -------------
    #
    # From an empty map it takes a couple of minutes for anything worth
    # watching to appear, and this gets a 45 second slot on the wall. Two
    # things fix that. First the map starts as blurred noise rather than
    # zeros, so agents have gradients to follow from step one and begin
    # aggregating immediately instead of doing a random walk until they
    # happen to cross. Second, build() runs --warmup steps before returning,
    # which costs a fraction of a second here and puts frame 0 well past the
    # noisy phase — so the panel shows a real network the instant the demo
    # starts, and then goes on developing for as long as it runs.
    trail[:] = rng.random((H, W), dtype=ds.f32)
    for _ in range(6):                       # noise -> smooth blobs to follow
        diffuse(1.0)
    trail[:] *= ds.f32(args.deposit / max(float(trail.mean()), 1e-6))

    for _ in range(max(0, args.warmup)):
        step()

    lut = ds.named_palette(args.palette)
    gain = ds.f32(1.0 / max(args.gain, 1e-6))
    gamma = ds.f32(args.gamma)
    shade = np.empty((H, W), ds.f32)
    frame_rgb = np.empty((H, W, 3), np.uint8)

    def render(t, frame_idx):
        for _ in range(max(1, args.steps)):
            step()

        # Expose, curve, and look up. One scalar field through a palette is
        # both the cheap way and the good-looking way; the ramp carries the
        # art, and its black end keeps the background LEDs genuinely off.
        np.multiply(trail, gain, out=shade)
        np.clip(shade, 0.0, 1.0, out=shade)
        if gamma != 1.0:
            np.power(shade, gamma, out=shade)
        np.take(lut, (shade * 255.0).astype(np.uint8), axis=0, out=frame_rgb)
        return frame_rgb

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
