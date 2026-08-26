#!/usr/bin/env python3
"""Wireworld: a real logic circuit, with the electrons visible.

Four states -- empty, conductor, electron head, electron tail -- and one rule
worth the name: a conductor becomes a head if exactly one or two of its eight
neighbours are heads. Head becomes tail, tail becomes conductor, empty stays
empty. That "one or two" is the whole of it. A head has a tail immediately
behind it, so the cell it just came from cannot fire again, and the cell ahead
sees exactly one head and does. Three heads is a junction being driven from
several sides at once and nothing happens there at all -- which is what turns a
grid of live cells into wire carrying a signal in one direction, and is the
mechanism behind every part below.

What is on the panel is not soup, it is a circuit that computes:

  * Two clocks. A closed loop of N conductor cells carries one electron round
    it forever, emitting a pulse every N generations, so the loop's
    circumference *is* the period. Clock A is 24 cells, clock B is 36; they
    coincide every 72, and 72 generations is the whole board's cycle.
  * Diodes, on the two inputs of the OR gate. The standard one-way junction: a
    gap in the wire bridged by two pairs of prongs. Forward, the near side
    lights one pair, that pair lights the next, and the far side sees exactly
    two heads and fires. Backwards, the far side lights both pairs at once and
    they put three heads around the near side, which is one too many.
  * Three gates, all built from the same nine cells. A centre cell with a
    signal input on one side and a control line arriving on the other. If the
    control arrives as a single cell it is another input and the centre fires
    on either: that is OR. If it arrives forked into three cells that all touch
    the centre, a control pulse puts three heads around it and it stays dark
    while a signal pulse alone still gets through: that is AND-NOT. Two cells
    between an OR gate and an AND-NOT gate.
  * And AND, because A and B is A and not (A and not B). The AND-NOT gate's
    output rail runs east across the panel and climbs into the next gate as its
    control, so the third gate is the second one feeding on itself.

So you can watch it work. Clock A's pulses run east along the top rail, clock
B's along the bottom; the OR fires on every one of them, the AND-NOT on the two
per cycle that clock A has to itself, and the AND only on the single one where
the two clocks land together. Three rails, three rhythms, one truth table.

Cells are 2x2 pixels by default (--cell). The circuit is a hand-laid 160x32
that fills a 320x64 wall exactly at 2; at 1 the same circuit sits in the middle
quarter of the panel with a black border, which reads far worse from any
distance, and a panel of another shape gets it centred and cropped.

The automaton is not run live. The board repeats every 72 generations, so
build() settles it and stores the cycle, and render() is a pure function of t
that indexes into it -- a seek or a restart lands on the same picture rather
than drifting. Steps are per second and independent of --fps; a frame that
lands on the same generation as the last one returns the same buffer and costs
nothing at all.

Run:  python3 wireworld.py --host 127.0.0.1
      python3 wireworld.py --rate 6 --palette ice     # slow enough to follow
      python3 wireworld.py --cell 1 --glow 0.7
      python3 scripts/wireworld-check.py              # the assertions
"""

import sys

import numpy as np

import demoscene as ds

EMPTY, HEAD, TAIL, WIRE = 0, 1, 2, 3

# head -> tail, tail -> conductor, conductor -> conductor unless it fires.
DECAY = np.array([EMPTY, TAIL, WIRE, WIRE], np.uint8)


# --------------------------------------------------------------------------
# The automaton.
# --------------------------------------------------------------------------

def step(g):
    """One Wireworld generation of an (H, W) uint8 grid."""
    h, w = g.shape
    pad = np.zeros((h + 2, w + 2), np.uint8)
    pad[1:-1, 1:-1] = (g == HEAD)
    n = pad[:-2, :-2] + pad[:-2, 1:-1]
    n += pad[:-2, 2:]
    n += pad[1:-1, :-2]
    n += pad[1:-1, 2:]
    n += pad[2:, :-2]
    n += pad[2:, 1:-1]
    n += pad[2:, 2:]
    out = DECAY[g]
    # 1 or 2 head neighbours, and only a conductor can fire.
    np.subtract(n, 1, out=n)
    fire = n < 2
    fire &= (g == WIRE)
    out[fire] = HEAD
    return out


# --------------------------------------------------------------------------
# Geometry helpers for laying the circuit out.
# --------------------------------------------------------------------------

def kingline(a, b):
    """Cells from a to b inclusive, one king move (incl. diagonals) apiece."""
    (r0, c0), (r1, c1) = a, b
    out = []
    r, c = r0, c0
    while (r, c) != (r1, c1):
        out.append((r, c))
        if r < r1:
            r += 1
        elif r > r1:
            r -= 1
        if c < c1:
            c += 1
        elif c > c1:
            c -= 1
    out.append((r1, c1))
    return out


def route(*points):
    """A path through waypoints, chamfered by construction.

    Turns must be 45 degrees: a right-angle corner would leave the cell before
    it diagonally adjacent to the cell after it, and the electron would cut the
    corner rather than travel round it.
    """
    path = []
    for a, b in zip(points, points[1:]):
        seg = kingline(a, b)
        path.extend(seg[1:] if path else seg)
    return path


def check_path(path, name="path"):
    """A wire must be a simple chain: no cell adjacent to a non-neighbour."""
    seen = {}
    for i, cell in enumerate(path):
        if cell in seen:
            raise AssertionError("%s revisits %r" % (name, cell))
        seen[cell] = i
    for i, (r, c) in enumerate(path):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                j = seen.get((r + dr, c + dc))
                if j is not None and abs(j - i) > 1:
                    raise AssertionError(
                        "%s: %r and %r touch but are %d apart"
                        % (name, path[i], path[j], abs(j - i)))
    return path


def ring_path(top, left, run, side):
    """A closed loop with chamfered corners, `run` across and `side` down.

    Length is 2*run + 2*side + 4. Consecutive cells touch and nothing else
    does, so an electron goes round it once every that many generations --
    which is the clock period.
    """
    r, c = top, left
    cells = [(r, c + i) for i in range(run)]                       # top, ->
    cells += [(r + 1 + i, c + run) for i in range(side)]           # right, v
    cells += [(r + side + 1, c + run - 1 - i) for i in range(run)]  # bottom
    cells += [(r + side - i, c - 1) for i in range(side)]           # left, ^
    return cells


def check_ring(cells, name="ring"):
    """Same rule as check_path, but the ends join."""
    n = len(cells)
    seen = {cell: i for i, cell in enumerate(cells)}
    if len(seen) != n:
        raise AssertionError("%s revisits a cell" % name)
    for i, (r, c) in enumerate(cells):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                j = seen.get((r + dr, c + dc))
                if j is None:
                    continue
                d = min((j - i) % n, (i - j) % n)
                if d > 1:
                    raise AssertionError("%s: %r and %r touch, %d apart"
                                         % (name, cells[i], cells[j], d))
    return cells


# --------------------------------------------------------------------------
# The circuit.
# --------------------------------------------------------------------------

GRID_W, GRID_H = 160, 32

GATE_ROW = 15                    # the row every gate's centre cell sits on
RAIL_ROW = 22                    # where the first output rail runs east
BUS_A_ROW = 1                    # clock A's bus, along the top
BUS_B_ROW = 30                   # clock B's bus, along the bottom
GATES = (40, 80, 120)            # centre column of the OR, ANDNOT and AND


class Board:
    """A grid plus the named nets laid on it, so shorts can be found."""

    def __init__(self, h, w):
        self.g = np.zeros((h, w), np.uint8)
        self.nets = {}

    def lay(self, name, cells, ring=False):
        (check_ring if ring else check_path)(cells, name)
        for cell in cells:
            self.g[cell] = WIRE
        self.nets[name] = list(cells)
        return cells

    def add(self, name, cells):
        """Extra cells belonging to a net that is not a simple chain."""
        for cell in cells:
            self.g[cell] = WIRE
        self.nets.setdefault(name, []).extend(cells)

    def cut(self, cell):
        self.g[cell] = EMPTY

    def diode(self, path, k):
        """Turn path[k]..path[k+1] into a one-way junction.

        The gap plus two pairs of prongs: forward, the cell before the gap
        lights one pair and that pair lights the next, so the far side sees
        exactly two heads and fires. Backwards, the far side lights both pairs
        and they light all three cells on the near side at once, which puts
        three heads around the wire beyond and stops it dead.
        """
        (r0, c0), (r1, c1) = path[k], path[k + 1]
        dr, dc = r1 - r0, c1 - c0
        pr, pc = dc, dr                              # unit perpendicular
        self.cut((r1, c1))
        prongs = [(r0 + s * pr, c0 + s * pc) for s in (-1, 1)]
        prongs += [(r1 + s * pr, c1 + s * pc) for s in (-1, 1)]
        self.add("diode", prongs)
        return prongs


def descend(cell, row):
    """A waypoint `row` below `cell`, offset east by the same amount.

    Turns have to be 45 degrees, so a wire dropping n rows has to travel n
    columns while it does it -- which is also why dropping costs nothing: a
    diagonal step is one generation, the same as a straight one.
    """
    r, c = cell
    return (row, c + (row - r))


def flare(end):
    """Three cells fanned off the end of a rail, as a lamp.

    A dead-end wire swallows its pulse in one pixel, which from any distance is
    nothing at all. Fanning the last cell into three that all touch it lights
    all three at once -- one head neighbour each -- so the answer arrives as a
    visible flash and then goes out.
    """
    r, c = end
    return [(r - 1, c + 1), (r, c + 1), (r + 1, c + 1)]


def gate(board, name, col, inhibit):
    """One gate: three control neighbours (inhibit) or one (merge).

    The two gates differ by exactly two cells. A conductor fires on one or two
    head neighbours, so a control line that arrives as three cells all touching
    the centre puts three heads around it and it stays dark -- while the same
    line arriving as a single cell is just another input, and the centre fires
    on either. That is the whole logic family: a merge is OR, and a fork of
    three is AND-NOT.
    """
    r, c = GATE_ROW, col
    ks = [(r - 1, c - 1), (r, c - 1), (r + 1, c - 1)] if inhibit \
        else [(r, c - 1)]
    board.add(name + ".k", ks + [(r, c)])
    ctrl = board.lay(name + ".ctrl", [(r, c - 2)])       # fed from the west
    sig = (r - 1, c + 1)                                 # fed from the north
    out = (r + 1, c + 1)                                 # leaves to the south
    board.add(name + ".ports", [sig, out])
    return {"t": (r, c), "x": ctrl[0], "sig": sig, "out": out}


def circuit():
    """Lay the whole thing out. Returns the board and its interesting cells."""
    b = Board(GRID_H, GRID_W)

    # --- clock A: a 24-cell loop, so a pulse every 24 generations ----------
    ring_a = b.lay("clock.a", ring_path(3, 5, 8, 4), ring=True)
    # --- clock B: 36 cells, so the two coincide every 72 ------------------
    ring_b = b.lay("clock.b", ring_path(22, 5, 13, 5), ring=True)

    # --- bus A along the top, bus B along the bottom ----------------------
    bus_a = b.lay("bus.a", route((2, 13), (BUS_A_ROW, 14),
                                 (BUS_A_ROW, GATES[2] + 1)))
    bus_b = b.lay("bus.b", route((29, 4), (BUS_B_ROW, 5),
                                 (BUS_B_ROW, GATES[1] - 9)))

    g0 = gate(b, "or", GATES[0], inhibit=False)
    g1 = gate(b, "andnot", GATES[1], inhibit=True)
    g2 = gate(b, "and", GATES[2], inhibit=True)

    # --- clock A drops out of the top bus into each gate's signal port ----
    drops = []
    for i, g in enumerate((g0, g1, g2)):
        col = g["sig"][1]
        d = b.lay("drop.%d" % i, route((BUS_A_ROW, col), g["sig"]))
        drops.append(d)
    # Only the merge can push a pulse back up its own inputs: when either
    # input fires the centre cell, the centre cell's head is a neighbour of
    # the *other* input's last cell, which duly fires and runs away backwards
    # up the wire. Left alone it meets the next real pulse coming the other
    # way and the two annihilate, which silently deletes a quarter of the
    # circuit's events. Both diodes therefore sit hard against the gate: the
    # reverse pulse has to be dead before the forward one arrives, and here
    # they are only ten generations apart.
    b.diode(drops[0], len(drops[0]) - 3)

    # --- clock B rises out of the bottom bus into the two B-fed gates -----
    rises = []
    for i, g in enumerate((g0, g1)):
        col = g["x"][1]
        rise = b.lay("rise.%d" % i, route((BUS_B_ROW, col - 7),
                                          (GATE_ROW + 1, col - 5),
                                          (GATE_ROW, col - 4), g["x"]))
        rises.append(rise)
    b.diode(rises[0], len(rises[0]) - 3)

    # --- the output rails, and the AND's control line ---------------------
    # Each gate's answer is a pulse running east down a rail of its own, so
    # the three rhythms can be read off against each other: the OR fires on
    # every pulse from either loop, the AND-NOT on the two that clock A has to
    # itself, and the AND only on the one where the loops land together.
    rails = [b.lay("rail.0", route(g0["out"], descend(g0["out"], RAIL_ROW),
                                   (RAIL_ROW, GATES[1] - 12))),
             b.lay("rail.1", route(g2["out"], descend(g2["out"], RAIL_ROW + 4),
                                   (RAIL_ROW + 4, GRID_W - 3)))]
    for i, rail in enumerate(rails):
        b.add("lamp.%d" % i, flare(rail[-1]))

    # ANDNOT's rail is also the AND's control: it runs east and climbs back up
    # into the next gate, which is what makes AND = A and not (A and not B).
    # It can wander through as much of the empty space as it likes without
    # upsetting the timing, because a diagonal step covers a row and a column
    # at once -- any path that keeps heading east takes exactly as many
    # generations as the straight one would.
    turn = RAIL_ROW + 2
    chain = b.lay("chain", route(g1["out"], descend(g1["out"], turn),
                                 (turn, g2["x"][1] - (turn - GATE_ROW)),
                                 g2["x"]))

    ports = {"ring_a": ring_a, "ring_b": ring_b, "g0": g0, "g1": g1, "g2": g2,
             "rails": rails, "chain": chain, "bus_a": bus_a, "bus_b": bus_b}
    return b, ports


PHASE_A, PHASE_B = 0, 6          # where each clock's electron starts
PERIOD = 72                      # generations before the whole board repeats
WARMUP = 300                     # generations to settle onto that cycle


def seed(g, ring, phase):
    """Put one electron on a loop, travelling towards increasing index."""
    g[ring[phase % len(ring)]] = HEAD
    g[ring[(phase - 1) % len(ring)]] = TAIL


def orbit():
    """The board's whole repeating cycle, as PERIOD grids.

    The circuit is periodic -- two loops of 24 and 36 cells, so everything on
    the board repeats every 72 generations -- which is what lets render() stay
    a pure function of t. Running the automaton live would make the picture
    depend on how many frames had gone by rather than on when it is now, and a
    seek or a restart would land somewhere else. Precomputing the cycle once
    costs about a third of a second on a Pi and 370 kB, and turns every later
    frame into an array index.
    """
    b, _ = circuit()
    g = b.g.copy()
    seed(g, b.nets["clock.a"], PHASE_A)
    seed(g, b.nets["clock.b"], PHASE_B)
    for _ in range(WARMUP):
        g = step(g)
    frames = []
    for _ in range(PERIOD):
        frames.append(g)
        g = step(g)
    if not np.array_equal(g, frames[0]):
        raise AssertionError("circuit missed its %d-step cycle" % PERIOD)
    return np.array(frames)


def levels(frames, glow):
    """Turn states into one brightness per cell, with a decaying wake.

    A head is a single pixel moving one cell a generation, which at any sane
    step rate is a blink rather than a movement. Giving it a few generations of
    afterglow behind it is what makes the direction of travel readable across a
    room -- the electron reads as an arrow rather than as a dot.
    """
    n = len(frames)
    base = np.array([0, 255, 150, 36], np.float32)     # empty head tail wire
    lvl = np.zeros(frames[0].shape, np.float32)
    out = np.zeros(frames.shape, np.uint8)
    for pass_ in range(3):              # go round twice so the wake wraps
        for i in range(n):
            np.multiply(lvl, glow, out=lvl)
            np.maximum(lvl, base[frames[i]], out=lvl)
            if pass_ == 2:
                out[i] = lvl.astype(np.uint8)
    return out


# --------------------------------------------------------------------------
# The demo.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ds.palette_argument(ap, "fire")
    ap.add_argument("--cell", type=int, default=2, choices=(1, 2, 3),
                    help="pixels per cell of the automaton")
    ap.add_argument("--rate", type=float, default=14.0,
                    help="generations per second, independent of --fps")
    ap.add_argument("--glow", type=float, default=0.5,
                    help="fraction of an electron's wake kept per generation")


def build(args):
    W, H = args.width, args.height
    cell = args.cell
    lut = ds.named_palette(args.palette, 256)
    lvl = levels(orbit(), np.float32(np.clip(args.glow, 0.0, 0.95)))

    # The circuit is a fixed hand-laid 160x32, so a panel that is not exactly
    # cell*160 by cell*32 gets it centred, and cropped if it will not fit.
    gh, gw = GRID_H * cell, GRID_W * cell
    dy, dx = (H - gh) // 2, (W - gw) // 2
    sy, sx = max(0, -dy) // cell, max(0, -dx) // cell
    dy, dx = max(0, dy), max(0, dx)
    rows = min(GRID_H - sy, (H - dy) // cell)
    cols = min(GRID_W - sx, (W - dx) // cell)

    out = np.zeros((H, W, 3), np.uint8)
    last = [-1]

    def render(t, frame):
        i = int(t * args.rate) % PERIOD
        if i != last[0]:                # a frame that steps nothing is free
            last[0] = i
            img = lut[lvl[i, sy:sy + rows, sx:sx + cols]]
            if cell > 1:
                img = np.repeat(np.repeat(img, cell, 0), cell, 1)
            out[dy:dy + rows * cell, dx:dx + cols * cell] = img
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
