#!/usr/bin/env python3
"""Light cycles: the game grid seen from above, played out for real.

The arena in TRON is a wide flat rectangle with two riders drawing solid walls
of light behind them, and that is the one classic image that wants a 5:1 panel
rather than merely tolerating it. 320x64 is not a compromise here -- it is the
board. A square screen would have to shrink the arena to fit; this one just
runs the ribbon the long way and lets the eye follow a bike across two metres
of wall.

**It is a game, not an animation.** Nothing about the riders' paths is drawn or
scripted. Each rider looks one move ahead down three directions -- straight,
left, right; a light cycle cannot reverse -- casts a ray along each to see how
far the lane runs before it hits the edge or somebody's ribbon, adds two shorter
probes sideways from the *far end* of that lane so a lane that dead-ends into a
pocket scores badly, and takes the best. Nine rays a rider a step, each an
argmax over at most twenty cells, which is why this can afford to look like it
is thinking.

Four terms decide what that looks like, and each of them was put in to fix
something the panel made obvious:

  * a cap on how far the ray counts. Uncapped, the longest lane always wins,
    and on a board five times wider than it is tall the longest lane is always
    horizontal -- two coloured rails running the width of the wall
  * a straight-ahead bonus that decays into a turn urge, so a bike holds a line
    for twenty-odd cells and then wants a corner. Without the first half they
    saw up and down on the spot; without the second they never turn at all
  * wall hugging, and only against ribbons, never the board's own edge. Running
    a cell off an existing wall is what makes the free space actually close,
    and it is what a light cycle bot looks like; crediting the frame instead
    just tiles a rider into a corner
  * aggression, which ramps from nothing to everything across the round: it
    pulls a rider toward whoever else is still up and at the same time cuts its
    lookahead from twenty cells to five. That contraction is what ends rounds.
    A bike that can see twenty cells ahead essentially never traps itself on a
    5120-cell board inside eight seconds, so an unpanicked race would run to
    its deadline every time and the crash would be a stage direction

**The payoff is the derezz.** A rider with nowhere left to go drives into the
wall, the panel flashes in that rider's colour, the bike bursts into a shower
of lit blocks that scatter and fade, a ring travels out from the impact, and
then its ribbon dissolves -- from the far end, oldest cell first, with a bright
front running along it the way a fuse burns. That last part is what sells it: a
trail that simply switched off would read as a bug. The survivor keeps riding
its lap while that happens, then powers down and dissolves too, and the arena
lights up empty for the next round.

If the racing runs to its deadline with more than one rider up, the round is
called -- but not by detonating somebody in clear air, which is what a
scheduled kill looks like and it looks wrong. The rider with the shortest way
out has its steering taken away and is pointed at that wall, so it crashes
within a few cells into something the eye can see it hit.

**The payoff is the derezz.** A rider that has nowhere left to go drives into
the wall, the panel flashes, the bike bursts into a shower of lit blocks that
scatter and fade, a ring travels out from the impact, and then its ribbon
dissolves -- from the far end, oldest cell first, with a bright front running
along it the way a fuse burns. That last part is the bit that sells it: a trail
that simply switched off would read as a bug. The survivor keeps riding through
its own victory for a beat, then powers down and dissolves too, and the arena
lights up empty for the next round.

**Everything happens in cell space.** The board is a small integer array --
160x32 cells at the default 2 px pitch -- holding who owns each cell, plus a
float array holding how bright that cell is. A frame is one gather through a
five-entry palette, one multiply, a handful of scattered writes for the bike
heads, the debris and the shockwaves, and then a single broadcast into the
output buffer reshaped as (rows, cell, cols, cell, 3), which is the whole
nearest-neighbour upscale with no intermediate array. The faint grid is baked
once at full resolution and composited with np.maximum, so it shows through the
black and never dims a ribbon. Per frame that is two passes over the 61 kB
frame and a handful over the 5 kB board, plus the riders' rays: 0.2 ms at p95
here with a 0.01 s build, which against the rotation's calibration demos is
something like 5-10 ms on the wall's Pi 3. Almost all of it is the rays and the
upscale, and neither grows with how much of the board is covered.

**render() is a pure function of t, and has to be**, because ftsched builds an
effect ahead of time and starts it at t=0 while the preview baker steps it at a
fixed rate. The simulation therefore advances in fixed steps of 1/--speed
seconds and the state is a function of the step index alone: step N is whatever
you get from a freshly seeded board after N steps, no matter which frames asked
for it. So a forward jump in t is just extra steps -- identical to replaying
from zero, only cheaper -- and t going backwards (or wrapping at the end of the
cycle) reseeds the RNG, clears the board and replays from step 0. Nothing here
integrates a wall-clock delta.

**Length.** --cycle is divided into --rounds equal rounds, in whole simulation
steps, so every round boundary *and* the loop point land on an empty arena
rather than mid-race. The default 45 s over four rounds is 11.2 s a round: up
to about 8 s of racing, then the crash, the lap, the dissolve and a beat. Each
round draws its own deadline out of the last quarter of that racing budget,
because rounds that all end on the same second read as a timer rather than as a
game; the slack goes to the survivor's lap, which is worth more on a wall than
an empty grid. A round that ends with *everybody* dead does leave the grid
empty for a second or two, so the frame breathes while it waits -- a black
panel reads as a crashed demo, not as a pause.

Run:  python3 tron.py --host 127.0.0.1
      python3 tron.py --riders 4 --colour neon
      python3 tron.py --grid 4 --speed 16        # chunkier board, slower game
      python3 tron.py --rounds 6 --derez 1.8     # more rounds, bigger bursts
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# (dy, dx), in turn order: east, south, west, north. A left turn is index-1 and
# a right turn index+1, mod 4, which is the only reason this ordering matters.
DIRS = ((0, 1), (1, 0), (0, -1), (-1, 0))

# Rider colours. `classic` is the film's cyan against the yellow-orange, which
# is also the pairing with the most hue separation an RGB panel can give at
# full saturation -- from across a room the two ribbons never get confused.
# The third and fourth entries are only reached with --riders 3 or 4.
RIDER_SETS = {
    "classic": ((60, 220, 255), (255, 140, 20), (140, 255, 120),
                (255, 90, 220)),
    "neon": ((0, 255, 190), (255, 40, 190), (255, 225, 60), (130, 140, 255)),
    "ember": ((255, 150, 30), (255, 55, 25), (255, 230, 110), (255, 105, 165)),
}

# Where riders come in. (x fraction, y fraction, initial direction). The first
# two face each other off-axis rather than head-on: two bikes on the same row
# closing at a combined 50 cells a second resolve in the first second and the
# round is over before anyone has looked up.
SPAWNS = ((0.18, 0.30, 0), (0.82, 0.70, 2),
          (0.18, 0.72, 0), (0.82, 0.28, 2))


def add_arguments(ap):
    ap.add_argument("--riders", type=int, default=2,
                    help="bikes on the grid, 2-4; more means shorter rounds "
                         "and a busier board")
    ap.add_argument("--speed", type=float, default=26.0,
                    help="cells a second each bike travels, which is also the "
                         "simulation's fixed step rate")
    ap.add_argument("--grid", type=int, default=2,
                    help="cell size in pixels, and so how thick a ribbon is; "
                         "snapped down to a divisor of the panel")
    ap.add_argument("--derez", type=float, default=1.0,
                    help="scale of the death burst, 0-2: block count, how far "
                         "they scatter, and the size of the flash")
    ap.add_argument("--rounds", type=int, default=4,
                    help="rounds inside one cycle; the cycle is rounded to "
                         "divide exactly into them, so the loop point and "
                         "every round boundary land on an empty arena")
    ap.add_argument("--cycle", type=float, default=45.0,
                    help="seconds for the whole loop")
    ap.add_argument("--colour", default="classic",
                    choices=tuple(sorted(RIDER_SETS)),
                    help="rider palette")
    ap.add_argument("--seed", type=int, default=7,
                    help="the game is deterministic in this; 0 picks one at "
                         "random per process")


def build(args):
    W, H = args.width, args.height

    # The upscale is a broadcast into out.reshape(gh, cell, gw, cell, 3), which
    # needs the pitch to divide the panel exactly. Snap down rather than reject
    # so --grid 3 gives a working board instead of a traceback on the wall.
    cell = max(1, int(args.grid))
    while cell > 1 and (W % cell or H % cell):
        cell -= 1
    gw, gh = W // cell, H // cell

    nrid = int(np.clip(args.riders, 2, 4))
    speed = float(max(4.0, args.speed))
    sdt = 1.0 / speed
    nround = max(1, int(args.rounds))
    derez = float(np.clip(args.derez, 0.0, 2.0))
    seed = args.seed if args.seed else None

    # Everything is counted in whole simulation steps: the round is a whole
    # number of them and the cycle is a whole number of rounds, so the loop
    # cannot wrap half a cell into a move or half way through a race.
    decay_steps = max(8, int(0.55 * speed))      # ribbons dissolving
    beat_steps = max(3, int(0.45 * speed))       # empty grid before the next
    lap_steps = max(4, int(1.1 * speed))         # lap after the last kill
    boot_steps = max(3, int(0.45 * speed))       # arena lighting up
    # What a round has to keep in reserve after the racing stops: the doomed
    # rider's run at the wall, the lap, the dissolve and the beat.
    tail_steps = 24 + lap_steps + decay_steps + beat_steps
    spr = max(2 * tail_steps, int(round(float(args.cycle) * speed / nround)))
    cyc_steps = spr * nround
    total = cyc_steps * sdt
    max_race = spr - tail_steps
    reach = max(12, gw // 2)                     # scale of "near the other"

    # ------------------------------------------------------------- the board
    # owner: 0 empty, i+1 the ribbon of rider i. heat: how lit that cell is,
    # which carries the hot few cells behind a bike and the dissolve front.
    owner = np.zeros((gh, gw), np.int16)
    heat = np.zeros((gh, gw), f32)
    own_flat = owner.reshape(-1)
    heat_flat = heat.reshape(-1)

    cols = RIDER_SETS[args.colour]
    pal = np.zeros((nrid + 1, 3), f32)
    for i in range(nrid):
        pal[i + 1] = cols[i]
    # A bike has to out-read its own ribbon or the eye loses it in the line it
    # just drew, so the head is the rider's hue pushed most of the way to white
    # and the ribbon sits at two thirds brightness. Same colour, different
    # temperature -- which is what a light cycle looks like in every frame of
    # the film anybody remembers.
    head_col = np.clip(pal[1:] * 0.35 + 190.0, 0, 255).astype(f32)
    spark_col = np.clip(pal[1:] * 0.55 + 70.0, 0, 255).astype(f32)
    halo_col = (pal[1:] * f32(0.40)).astype(f32)
    debris_col = np.clip(pal[1:] * 0.75 + 60.0, 0, 255).astype(f32)
    wave_col = np.clip(pal[1:] * 0.5 + 40.0, 0, 255).astype(f32)
    boot_col = np.array((30, 90, 130), f32)
    TRAIL = f32(0.68)
    # The four cells behind the head cool off to the ribbon's level, which
    # gives the bike a short exhaust and, more usefully, a direction.
    TAIL = np.array([1.0, 0.95, 0.86, 0.77, TRAIL], f32)

    # ------------------------------------------------------------ the riders
    ry = [0] * nrid                              # head row, column, direction
    rx = [0] * nrid
    rd = [0] * nrid
    moving = [False] * nrid                      # still driving
    derezzed = [False] * nrid                    # burst, versus powered down
    erase_at = [-1] * nrid                       # step its ribbon starts going
    rstraight = [0] * nrid                       # steps since its last turn
    doomed = [False] * nrid                      # aimed at a wall, no steering
    # A ribbon grows by at most a cell a step, and a race cannot outlast its
    # deadline plus the doomed rider's run at the wall plus the victory lap.
    path_max = max_race + 4 * lap_steps + 32
    path = [np.zeros(path_max, np.int32) for _ in range(nrid)]
    plen = [0] * nrid
    pcut = [0] * nrid                            # cells already dissolved

    # --------------------------------------------------------- debris blocks
    nburst = max(6, int(round(30 * max(derez, 0.15))))
    npart = nburst * nrid + 8
    px = np.zeros(npart, f32)
    py = np.zeros(npart, f32)
    pvx = np.zeros(npart, f32)
    pvy = np.zeros(npart, f32)
    plife = np.zeros(npart, f32)
    pwho = np.zeros(npart, np.int32)

    # Shockwaves: centre, age, rider. Four at once is more than a round needs.
    wave = np.zeros((4, 4), f32)
    ring = np.stack([np.sin(np.linspace(0, 2 * np.pi, 22, endpoint=False)),
                     np.cos(np.linspace(0, 2 * np.pi, 22, endpoint=False))])
    ring = ring.astype(f32)

    # ------------------------------------------------------------ the buffers
    colbuf = np.zeros((gh, gw, 3), f32)
    cflat = colbuf.reshape(-1, 3)
    small = np.zeros((gh, gw, 3), np.uint8)
    out = np.zeros((H, W, 3), np.uint8)
    out5 = out.reshape(gh, cell, gw, cell, 3)

    # The grid, baked. Spacing follows the cell pitch so it stays a grid rather
    # than becoming a texture at --grid 1, and it is dim enough that np.maximum
    # against a ribbon never touches the ribbon.
    bg = np.zeros((H, W, 3), np.uint8)
    gstep = max(4, cell * 4)
    bg[::gstep, :] = (7, 13, 21)
    bg[:, ::gstep] = (7, 13, 21)
    bg[0, :] = bg[-1, :] = (16, 40, 60)
    bg[:, 0] = bg[:, -1] = (16, 40, 60)

    # phase: 0 racing, 1 ribbons dissolving, 2 holding the empty grid until the
    # cycle wraps. step -1 means "nothing has happened yet", which is what a
    # reset returns to and what the first advance() spawns from.
    state = {"step": -1, "rng": np.random.default_rng(seed), "flash": 0.0,
             "flash_col": np.zeros(3, f32), "phase": 2, "race_at": 0,
             "race_len": max_race, "end_at": -1, "phase_end": 0}

    # ------------------------------------------------------------------ rays
    def lane(y, x, d, cap):
        """Free cells ahead of (y, x) in direction d, up to cap or the wall.

        The wall falls out of the slicing for nothing: a ray that runs off the
        board just returns a short line, so the edge scores exactly like a
        ribbon does without anything having to test for it.
        """
        dy, dx = DIRS[d]
        if dy == 0:
            if dx > 0:
                line = owner[y, x + 1:x + 1 + cap]
            else:
                line = owner[y, max(0, x - cap):x][::-1]
        else:
            if dy > 0:
                line = owner[y + 1:y + 1 + cap, x]
            else:
                line = owner[max(0, y - cap):y, x][::-1]
        n = line.shape[0]
        if n == 0:
            return 0
        hit = line != 0
        i = int(hit.argmax())
        return i if hit[i] else n

    def choose(i, aggr):
        """Rider i's next direction, or None if every way out is blocked."""
        rng = state["rng"]
        best, best_d = -1e18, None
        # How far a rider bothers to look. It contracts as the round tightens,
        # and that contraction is what ends rounds: a bike that can see sixteen
        # cells ahead and probe the far end of the lane essentially never traps
        # itself on a board this size, so an unpanicked race would run to its
        # deadline every single time and the crash would be a stage direction
        # rather than a mistake. Five cells of vision, chasing the other rider,
        # is a bike that will eventually turn into a pocket -- which is the
        # whole point of the last few seconds of a round.
        fcap = max(5, int(round(20.0 - 15.0 * aggr)))
        scap = max(2, int(round(12.0 - 9.0 * aggr)))
        swgt = 0.7 * (1.0 - 0.6 * aggr)
        # Wall hugging ramps with the panic too: early on the riders should be
        # crossing the board, and a bike that hugs from the first second tiles
        # itself a small solid rectangle and never leaves it.
        hugw = 0.4 + 1.2 * aggr
        for turn in (0, -1, 1):
            d = (rd[i] + turn) % 4
            dy, dx = DIRS[d]
            ny, nx = ry[i] + dy, rx[i] + dx
            if not (0 <= ny < gh and 0 <= nx < gw) or owner[ny, nx]:
                continue
            # The forward cap is the single most important number in the file.
            # Uncapped, the longest lane always wins and on a board five times
            # wider than it is tall the longest lane is always horizontal: the
            # riders sprint the length of the panel, turn, sprint back, and the
            # game reads as two coloured rails. Capped at a dozen or so cells,
            # every direction with room to breathe scores alike and the tie is
            # broken by the side probes, the turn urge and the noise -- which
            # is what produces right angles all over the board.
            run = lane(ny, nx, d, fcap)
            # Probe sideways from the end of the lane, not from here: a lane
            # that is clear for a dozen cells and then walled on all sides is a
            # coffin, and only the far end knows that.
            ey, ex = ny + dy * run, nx + dx * run
            ey = min(max(ey, 0), gh - 1)
            ex = min(max(ex, 0), gw - 1)
            score = run + swgt * (lane(ey, ex, (d + 1) % 4, scap)
                                  + lane(ey, ex, (d + 3) % 4, scap))
            if turn == 0:
                score += 1.0                     # no jitter without a reason
            else:
                # ...but a bike that has been going straight for a while is
                # increasingly willing to turn. Without this they run rails;
                # with the threshold too low they saw up and down on the spot.
                # A dozen cells of patience is the difference between a maze
                # and a comb.
                score += 0.25 * max(rstraight[i] - 12, 0)
            # Hug what is already there. This is the one behaviour that makes a
            # light cycle bot look like a light cycle bot: running a cell off
            # your own wall wastes no room, so the ribbons stack into slabs
            # instead of wandering, the free space closes for real, and riders
            # box themselves in -- which is where the derezzes come from. On a
            # 5120 cell board two bikes that merely avoided each other would
            # never run out of anywhere to go inside a round.
            # Ribbons only: the board's own edge deliberately does not count,
            # because a rider that gets credit for hugging the frame ends up
            # tiling itself into a corner, and a solid rectangle of one colour
            # is the least TRON-looking thing this demo can draw.
            hug = 0
            if (ny > 0 and owner[ny - 1, nx]) \
                    or (ny < gh - 1 and owner[ny + 1, nx]):
                hug += 1
            if (nx > 0 and owner[ny, nx - 1]) \
                    or (nx < gw - 1 and owner[ny, nx + 1]):
                hug += 1
            score += hugw * hug
            score += float(rng.random()) * 2.5
            if aggr > 0.0:
                # Steer at whoever else is still up -- gently from the start,
                # hard by the deadline. Without it the riders spend the round
                # tiling their own half of a very wide board and never meet;
                # with it they converge, and closing the distance is what
                # produces a cut-off. Nobody has to plan one.
                for j in range(nrid):
                    if j == i or not moving[j]:
                        continue
                    # Manhattan distance, on a scale of half the board: over
                    # the whole board width the term is so flat that the lane
                    # scores swamp it and the riders spend the round in
                    # separate halves, politely, which is not the game.
                    near = 1.0 - min(abs(ny - ry[j]) + abs(nx - rx[j]),
                                     reach) / float(reach)
                    score += aggr * 14.0 * near * near
            if score > best:
                best, best_d = score, d
        return best_d

    # -------------------------------------------------------------- the round
    def start_round(s):
        owner[:] = 0
        heat[:] = 0.0
        plife[:] = 0.0
        wave[:] = 0.0
        state["flash"] = 0.0
        state["phase"] = 0                       # racing
        state["race_at"] = s
        # Rounds that all end on the same second read as a timer rather than as
        # a game, so each one draws its own deadline out of the last quarter of
        # the budget. The slack goes to the empty grid at the end of the round.
        state["race_len"] = max_race - int(state["rng"].integers(
            0, max(2, max_race // 4)))
        state["end_at"] = -1                     # when the race stops
        rng = state["rng"]
        for i in range(nrid):
            fx, fy, d = SPAWNS[i]
            # A little scatter on the spawn so the rounds are not the same
            # opening every time; the seed still makes the whole cycle repeat.
            ry[i] = int(np.clip(fy * gh + rng.integers(-2, 3), 1, gh - 2))
            rx[i] = int(np.clip(fx * gw + rng.integers(-3, 4), 1, gw - 2))
            rd[i] = d
            moving[i] = True
            derezzed[i] = False
            erase_at[i] = -1
            rstraight[i] = 0
            doomed[i] = False
            plen[i] = 0
            pcut[i] = 0
            lay(i)

    def lay(i):
        """Claim the cell under rider i's head and warm the tail behind it."""
        k = ry[i] * gw + rx[i]
        own_flat[k] = i + 1
        n = plen[i]
        if n >= path_max:
            return                               # cannot happen; not a crash
        path[i][n] = k
        plen[i] = n + 1
        m = min(n + 1, TAIL.shape[0])
        heat_flat[path[i][n + 1 - m:n + 1][::-1]] = TAIL[:m]

    def burst(i, s):
        """Derezz rider i: blocks, a ring, a flash, and a ribbon on a fuse."""
        rng = state["rng"]
        moving[i] = False
        derezzed[i] = True
        erase_at[i] = s + max(2, int(0.30 * speed))
        j = int(rng.integers(0, npart - nburst))
        sel = slice(j, j + nburst)
        ang = rng.uniform(0.0, 2.0 * np.pi, nburst)
        mag = rng.uniform(5.0, 26.0, nburst) * (0.55 + 0.5 * derez)
        px[sel] = rx[i] + 0.5
        py[sel] = ry[i] + 0.5
        pvx[sel] = np.cos(ang) * mag
        # The board is five times wider than it is tall, so an isotropic burst
        # is off the top and bottom edges before it has read as a burst.
        pvy[sel] = np.sin(ang) * mag * 0.55
        plife[sel] = rng.uniform(0.65, 1.0, nburst)
        pwho[sel] = i
        k = int(np.argmin(wave[:, 2]))
        wave[k] = (ry[i], rx[i], 1.0, i)
        state["flash"] = min(1.0, 0.55 + 0.45 * derez)
        # How much the whole panel lifts at the moment of the burst, in output
        # levels. A quarter of the rider's colour is a room-filling flash on an
        # LED wall; the arithmetic here is an add to a 0-255 buffer, not a
        # fraction of one, which is a mistake worth only making once.
        state["flash_col"][:] = pal[i + 1] * f32(0.26)

    def retire(i, s):
        """Power down a survivor: it stops, its ribbon goes out behind it."""
        moving[i] = False
        erase_at[i] = s

    def advance():
        """One fixed step. Pure in the step index -- see the docstring."""
        s = state["step"] + 1
        state["step"] = s
        if s % spr == 0:
            start_round(s)
            return

        if state["phase"] == 0:
            # Aggression is off for the first third of a race and full well
            # before its deadline: the opening reads as riders claiming space,
            # then there are several seconds of them hunting each other in
            # which somebody can actually crash, rather than a panic that
            # arrives with the round already over.
            age = s - state["race_at"]
            rlen = state["race_len"]
            aggr = min(max((age - 0.30 * rlen) / (0.40 * rlen), 0.0), 1.0)
            if state["end_at"] >= 0:
                # The race is decided and the survivor is on its lap. Left at
                # full panic it hugs its own wall and tiles itself into a solid
                # rectangle, which is the least interesting thing on the panel;
                # calmed down it goes back to drawing open ribbons.
                aggr = 0.3
            for i in range(nrid):
                if not moving[i]:
                    continue
                # A doomed rider has stopped steering and is running at the
                # thing it is going to hit; everyone else picks a line.
                d = rd[i] if doomed[i] else choose(i, aggr)
                if d is not None and doomed[i]:
                    ny, nx = ry[i] + DIRS[d][0], rx[i] + DIRS[d][1]
                    if not (0 <= ny < gh and 0 <= nx < gw) or owner[ny, nx]:
                        d = None
                if d is None:
                    burst(i, s)                  # boxed in, nowhere to turn
                    continue
                rstraight[i] = rstraight[i] + 1 if d == rd[i] else 0
                rd[i] = d
                ry[i] += DIRS[d][0]
                rx[i] += DIRS[d][1]
                lay(i)

            up = [i for i in range(nrid) if moving[i]]
            # The deadline. Everything after it -- the crash, the lap, the
            # dissolve -- has to fit in the round's reserve, because the next
            # round (or the loop point) starts on the step it says it does.
            hard = state["race_at"] + rlen
            if len(up) > 1 and s >= hard and not any(doomed):
                # Time. Rather than detonating somebody in clear air -- which
                # is what a scheduled kill looks like, and it looks wrong --
                # pick the rider with the shortest way out, point it at that
                # wall and take its steering away. It crashes within a few
                # cells, into something the eye can see it hit.
                worst, worst_i, worst_d = 1 << 30, up[0], rd[up[0]]
                for i in up:
                    for turn in (0, -1, 1):
                        d = (rd[i] + turn) % 4
                        ny, nx = ry[i] + DIRS[d][0], rx[i] + DIRS[d][1]
                        if not (0 <= ny < gh and 0 <= nx < gw) \
                                or owner[ny, nx]:
                            continue
                        run = lane(ny, nx, d, 24)
                        if run < worst:
                            worst, worst_i, worst_d = run, i, d
                doomed[worst_i] = True
                rd[worst_i] = worst_d
            elif len(up) > 1 and s >= hard + 24:
                # Backstop: a doomed rider that somehow found open road. 24 is
                # the longest lane the pick above will accept, so this is the
                # step after the crash was due.
                while len(up) > 1:
                    burst(up.pop(), s)
            if len(up) <= 1:
                # The victory lap runs to the last moment the round can still
                # fit its dissolve. Cutting it short instead would leave the
                # slack as an empty grid, and a survivor still drawing is worth
                # more on a wall than two seconds of nothing.
                if state["end_at"] < 0:
                    state["end_at"] = max(
                        s + lap_steps,
                        state["race_at"] + spr - decay_steps - beat_steps - 2)
                if not up:
                    # Nobody left to watch: whoever was on the lap has just
                    # gone too, so stop waiting for a lap that cannot happen.
                    state["end_at"] = min(state["end_at"], s + beat_steps)
            if 0 <= state["end_at"] <= s:
                for i in range(nrid):
                    if moving[i]:
                        retire(i, s)
                state["phase"] = 1
                state["phase_end"] = s + decay_steps + beat_steps
        elif state["phase"] == 1 and s >= state["phase_end"]:
            state["phase"] = 2                   # empty grid until next round

        # The dissolve. Each ribbon goes out from its oldest cell forward, on
        # its own clock, so a rider that died early is gone long before the
        # survivor starts to fade.
        for i in range(nrid):
            if erase_at[i] < 0 or s < erase_at[i] or pcut[i] >= plen[i]:
                continue
            frac = min((s - erase_at[i]) / float(decay_steps), 1.0)
            want = int(frac * plen[i])
            if want > pcut[i]:
                gone = path[i][pcut[i]:want]
                own_flat[gone] = 0
                heat_flat[gone] = 0.0
                pcut[i] = want
            # A bright front where it is burning back to, four cells long.
            front = path[i][want:min(want + 4, plen[i])]
            if front.shape[0]:
                heat_flat[front] = 1.6

        # Debris and shockwaves, on the same fixed step.
        live = plife > 0.0
        if live.any():
            px[live] += pvx[live] * sdt
            py[live] += pvy[live] * sdt
            pvx[live] *= 0.90
            pvy[live] *= 0.90
            plife[live] -= sdt / 0.95
            # A block that leaves the arena is gone. Clamping it to the edge
            # instead -- which is what the draw does to whatever survives here
            # -- stacks the outbound half of every burst into a dotted line
            # along the border, and that reads as a bug, because it is one.
            np.copyto(plife, f32(0.0),
                      where=(px < 0) | (px >= gw) | (py < 0) | (py >= gh))
        act = wave[:, 2] > 0.0
        if act.any():
            wave[act, 2] -= sdt / 0.45
        if state["flash"] > 0.0:
            state["flash"] = max(0.0, state["flash"] - sdt / 0.16)

    def reset():
        state["step"] = -1
        state["rng"] = np.random.default_rng(seed)
        state["phase"] = 2
        state["end_at"] = -1
        owner[:] = 0
        heat[:] = 0.0
        plife[:] = 0.0
        wave[:] = 0.0
        state["flash"] = 0.0
        for i in range(nrid):
            moving[i] = False
            erase_at[i] = -1
            plen[i] = 0
            pcut[i] = 0

    def render(t, frame):
        # The simulation's state is a function of the step index and nothing
        # else, so catching up is just running the missing steps; only t moving
        # backwards (or wrapping the cycle) needs the board thrown away and
        # replayed from a freshly seeded step 0.
        tt = t % total
        target = min(int(tt * speed), cyc_steps - 1)
        if target < state["step"]:
            reset()
        while state["step"] < target:
            advance()

        # Board -> colour: one gather through the palette, one multiply.
        np.take(pal, owner, axis=0, out=colbuf)
        np.multiply(colbuf, heat[:, :, None], out=colbuf)

        age = state["step"] - state["race_at"]
        edge = None
        if state["phase"] == 0 and age < boot_steps:
            # The arena powering up. Without it a round starts on an empty
            # board and the reset reads as a dropped frame, not an event.
            edge = boot_col * f32(1.4 * (1.0 - age / float(boot_steps)))
        elif state["phase"] == 2:
            # Between rounds. A round that ends early -- both riders going in
            # the same second, which happens -- would otherwise leave a black
            # panel for a couple of seconds, and a black panel on a wall reads
            # as a crashed demo rather than as a pause. Breathing the frame
            # says the arena is still there and something is about to use it.
            edge = boot_col * f32(0.30 + 0.14 * np.sin(t * 2.4))
        if edge is not None:
            colbuf[0, :] = edge
            colbuf[-1, :] = edge
            colbuf[:, 0] = edge
            colbuf[:, -1] = edge

        live = np.nonzero(plife > 0.0)[0]
        if live.shape[0]:
            iy = np.clip(py[live].astype(np.int32), 0, gh - 1)
            ix = np.clip(px[live].astype(np.int32), 0, gw - 1)
            lif = plife[live]
            cflat[iy * gw + ix] = (debris_col[pwho[live]]
                                   * np.minimum(lif, 1.0)[:, None])

        act = np.nonzero(wave[:, 2] > 0.0)[0]
        for w in act:
            rad = 1.5 + (1.0 - wave[w, 2]) * 26.0
            iy = (wave[w, 0] + ring[0] * rad * 0.55).astype(np.int32)
            ix = (wave[w, 1] + ring[1] * rad).astype(np.int32)
            # Drop the parts of the ring that have left the board rather than
            # clamping them, which would bunch half a ring into one edge cell.
            on = (iy >= 0) & (iy < gh) & (ix >= 0) & (ix < gw)
            cflat[iy[on] * gw + ix[on]] = (wave_col[int(wave[w, 3])]
                                           * f32(wave[w, 2] * 0.8))

        # The bikes, last, so nothing paints over them.
        for i in range(nrid):
            if erase_at[i] >= 0 and derezzed[i]:
                continue                         # this one is a cloud now
            if erase_at[i] >= 0 and pcut[i] >= plen[i]:
                continue                         # powered down and gone
            hy, hx = ry[i], rx[i]
            nb = []
            if hy > 0:
                nb.append((hy - 1) * gw + hx)
            if hy < gh - 1:
                nb.append((hy + 1) * gw + hx)
            if hx > 0:
                nb.append(hy * gw + hx - 1)
            if hx < gw - 1:
                nb.append(hy * gw + hx + 1)
            if nb:
                v = cflat[nb]
                np.maximum(v, halo_col[i], out=v)
                cflat[nb] = v
            if moving[i]:
                # A spark two cells out in front. It is what tells you which
                # way a bike is going in a still frame, and on a 320 wide panel
                # a still frame is what half the audience gets.
                sy, sx = hy + DIRS[rd[i]][0] * 2, hx + DIRS[rd[i]][1] * 2
                if 0 <= sy < gh and 0 <= sx < gw and not owner[sy, sx]:
                    cflat[sy * gw + sx] = spark_col[i]
            cflat[hy * gw + hx] = head_col[i]

        if state["flash"] > 0.0:
            np.add(colbuf, state["flash_col"] * f32(state["flash"]),
                   out=colbuf)

        np.clip(colbuf, 0.0, 255.0, out=colbuf)
        np.copyto(small, colbuf, casting="unsafe")
        # The whole nearest-neighbour upscale, as one broadcast store into the
        # output buffer viewed as (rows, cell, cols, cell, 3) -- no temporary.
        out5[...] = small[:, None, :, None, :]
        # maximum, not add: the grid shows through the black and leaves every
        # lit ribbon exactly the colour the palette said it was.
        np.maximum(out, bg, out=out)
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
