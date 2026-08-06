#!/usr/bin/env python3
"""Conway's Game of Life, one cell per pixel, with the dead left glowing.

Life on a 320x64 panel is 20,480 cells, which is enough for gliders to travel
and for the usual still lifes and oscillators to settle out all over the
board. The rule itself is four lines of numpy; everything else here is about
making a black and white cellular automaton worth looking at on an LED wall.

Two things do that. Cells are coloured by how long they have been alive, so a
freshly born cell is bright and a block that has sat there for a minute has
faded back to a deep ember -- the eye reads the difference immediately and the
board stops looking uniform. And a cell that dies does not vanish; it leaves a
decaying trail. A glider then draws its own wake, and the boundary between the
churning early generations and the settled late ones is visible as a field of
fading ash rather than as nothing at all.

Life always dies down. After a few hundred generations almost everything is
still lifes and blinkers, which is a screensaver nobody looks at twice, so
this watches for that: when the population stops changing (allowing for
period-2 oscillators, which never stop changing but never go anywhere) it
seeds a fresh patch somewhere. The board therefore keeps being reinvaded at
the edges rather than being reset, and the old structures survive the new
arrivals for a while.

Run:  python3 life.py --host 127.0.0.1
      python3 life.py --density 8 --palette ice --step-ms 120
      python3 life.py --no-trails --palette toxic
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32


def add_arguments(ap):
    ds.palette_argument(ap, "fire")
    ap.add_argument("--density", type=int, default=6,
                    help="seed one live cell in every N")
    ap.add_argument("--step-ms", type=float, default=90.0,
                    help="ms per generation; the render rate is separate")
    ap.add_argument("--age-scale", type=float, default=24.0,
                    help="generations for a cell to reach its settled colour")
    ap.add_argument("--trails", dest="trails", action="store_true", default=True)
    ap.add_argument("--no-trails", dest="trails", action="store_false",
                    help="dead cells go black at once")
    ap.add_argument("--decay", type=float, default=0.86,
                    help="how much of a trail survives each generation, 0..1")
    ap.add_argument("--reseed", type=int, default=40,
                    help="stagnant generations before seeding a fresh patch")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    lut = ds.named_palette(args.palette, 256).astype(f32)

    alive = rng.random((H, W)) < (1.0 / max(args.density, 1))
    age = np.zeros((H, W), np.float32)
    trail = np.zeros((H, W), np.float32)
    out = np.empty((H, W, 3), np.uint8)

    # Population history, for spotting a board that has stopped going
    # anywhere. Period-2 oscillators make the count alternate forever, so the
    # test is against the count two generations back as well as one.
    history = []
    stagnant = 0
    generation = [0]                     # boxed, so render() can advance it

    # One padded scratch board, filled with a wrapped copy of the grid each
    # generation. The eight neighbour shifts are then plain slices of it and
    # allocate nothing -- np.roll would build sixteen full-size temporaries per
    # generation, which on a Pi is most of the cost of the rule.
    pad = np.zeros((H + 2, W + 2), np.int8)

    def neighbours(grid):
        """Live neighbour count, wrapping at the edges."""
        g = grid.view(np.int8)
        pad[1:-1, 1:-1] = g
        pad[0, 1:-1] = g[-1]                 # top edge <- bottom row
        pad[-1, 1:-1] = g[0]
        pad[1:-1, 0] = g[:, -1]              # left edge <- right column
        pad[1:-1, -1] = g[:, 0]
        pad[0, 0], pad[0, -1] = g[-1, -1], g[-1, 0]        # and the corners
        pad[-1, 0], pad[-1, -1] = g[0, -1], g[0, 0]
        return (pad[:-2, :-2] + pad[:-2, 1:-1] + pad[:-2, 2:] +
                pad[1:-1, :-2] + pad[1:-1, 2:] +
                pad[2:, :-2] + pad[2:, 1:-1] + pad[2:, 2:])

    def seed_patch():
        """Drop a fresh soup into part of the board, not over all of it."""
        ph, pw = rng.integers(10, H // 2), rng.integers(20, W // 3)
        y = rng.integers(0, H - ph)
        x = rng.integers(0, W - pw)
        patch = rng.random((ph, pw)) < (1.0 / max(args.density, 1))
        alive[y:y + ph, x:x + pw] |= patch
        age[y:y + ph, x:x + pw][patch] = 0.0

    def step():
        nonlocal stagnant
        n = neighbours(alive)
        # The rule: three neighbours births, two or three survives.
        born = (~alive) & (n == 3)
        survives = alive & ((n == 2) | (n == 3))
        died = alive & ~survives

        if args.trails:
            trail[died] = 1.0
            # np.multiply(out=) rather than `trail *= x`: the augmented form
            # binds the name locally and would shadow the closure variable.
            np.multiply(trail, args.decay, out=trail)
        age[survives] += 1.0
        age[born] = 0.0
        np.copyto(alive, survives | born)

        count = int(alive.sum())
        history.append(count)
        del history[:-3]
        # Stalled, or oscillating with period two: neither is going anywhere.
        if len(history) == 3 and (history[0] == history[2] or
                                  history[1] == history[2]):
            stagnant += 1
        else:
            stagnant = 0
        if count == 0 or stagnant >= args.reseed:
            seed_patch()
            stagnant = 0

    def render(t, frame):
        want = int(t * 1000.0 / args.step_ms)
        # Catch up at most a few generations: if the loop has been starved
        # there is no point replaying the whole gap, and doing so would starve
        # it further.
        for _ in range(min(max(want - generation[0], 0), 4)):
            step()
            generation[0] += 1

        # Live cells run from the bright end of the palette down towards its
        # settled middle as they age; trails carry on down to black.
        heat = np.where(alive, 1.0 - 0.55 * np.minimum(age / args.age_scale, 1.0),
                        trail * 0.5)
        np.multiply(heat, 255.0, out=heat)
        np.clip(heat, 0.0, 255.0, out=heat)
        np.copyto(out, lut[heat.astype(np.int32)].astype(np.uint8))
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
