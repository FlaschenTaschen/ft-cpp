#!/usr/bin/env python3
"""A maze, carved and then solved, over and over.

The old C version drew a finished maze and left it there. A finished maze is a
texture; what is worth watching is the making of it, so this runs a cycle:

  carve    a depth-first walk knocks down walls, the head glowing at the
           frontier and a short tail of recently cut corridor behind it. It
           backtracks visibly when it paints itself into a corner, which is
           the part that reads as a thing thinking rather than a thing
           drawing.
  solve    a breadth-first flood fills the whole maze from the start, so you
           watch the wavefront pour down every dead end at once.
  path     the actual route lights up from start to finish, then holds.
  wipe     it fades and the next maze starts.

At 320x64 with two-pixel corridors the grid is about 79 by 15 cells, which is
wide enough that the flood takes a satisfying couple of seconds to reach the
far end and the solution has real corners in it.

Generation is a plain Python loop over the cells, which is the one place in
this file where numpy would not help -- a depth-first carve is inherently
sequential. It is also done once per maze rather than per frame, and 1,185
cells cost about 4 ms, so it disappears next to the animation.

Run:  python3 maze.py --host 127.0.0.1
      python3 maze.py --scale 3 --palette ice
      python3 maze.py --carve-rate 220 --hold 3
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

WALL = np.array((10, 10, 22), f32)


def add_arguments(ap):
    ds.palette_argument(ap, "ice")
    ap.add_argument("--scale", type=int, default=2,
                    help="pixels per maze cell wall; 2 is a 79x15 grid at 320x64")
    ap.add_argument("--carve-rate", type=float, default=160.0,
                    help="cells carved per second")
    ap.add_argument("--flood-rate", type=float, default=90.0,
                    help="flood wavefront steps per second")
    ap.add_argument("--path-rate", type=float, default=70.0,
                    help="cells per second the solution draws at")
    ap.add_argument("--hold", type=float, default=2.0,
                    help="seconds the finished route stays up")
    ap.add_argument("--tail", type=int, default=26,
                    help="cells of glow behind the carving head")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def carve(cols, rows, rng):
    """Depth-first maze on a cols x rows cell grid.

    Returns (open, order), where `open` is the (2*rows+1, 2*cols+1) boolean
    grid of passable pixels and `order` is the cell-grid coordinates in the
    sequence they were opened -- which is what lets the carve be replayed as
    an animation rather than appearing all at once.
    """
    gh, gw = 2 * rows + 1, 2 * cols + 1
    opened = np.zeros((gh, gw), bool)
    seen = np.zeros((rows, cols), bool)
    order = []

    cy, cx = int(rng.integers(rows)), int(rng.integers(cols))
    seen[cy, cx] = True
    opened[2 * cy + 1, 2 * cx + 1] = True
    order.append((2 * cy + 1, 2 * cx + 1))
    stack = [(cy, cx)]

    while stack:
        cy, cx = stack[-1]
        options = []
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < rows and 0 <= nx < cols and not seen[ny, nx]:
                options.append((ny, nx, dy, dx))
        if not options:
            stack.pop()                      # backtrack, visibly
            continue
        ny, nx, dy, dx = options[int(rng.integers(len(options)))]
        wall = (2 * cy + 1 + dy, 2 * cx + 1 + dx)
        cell = (2 * ny + 1, 2 * nx + 1)
        opened[wall] = True
        opened[cell] = True
        order.extend([wall, cell])
        seen[ny, nx] = True
        stack.append((ny, nx))
    return opened, order


def flood(opened, start):
    """BFS from `start`. Returns (distance, parent-index), both grid-shaped.

    Distance doubles as the animation clock: showing every pixel whose
    distance is below a rising threshold *is* the expanding wavefront, with no
    per-frame work beyond a comparison over the grid.
    """
    gh, gw = opened.shape
    dist = np.full((gh, gw), -1, np.int32)
    parent = np.full((gh, gw), -1, np.int64)
    dist[start] = 0
    frontier = [start]
    while frontier:
        nxt = []
        for y, x in frontier:
            d = dist[y, x] + 1
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ny, nx = y + dy, x + dx
                if (0 <= ny < gh and 0 <= nx < gw and opened[ny, nx]
                        and dist[ny, nx] < 0):
                    dist[ny, nx] = d
                    parent[ny, nx] = y * gw + x
                    nxt.append((ny, nx))
        frontier = nxt
    return dist, parent


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)
    lut = ds.named_palette(args.palette, 256).astype(f32)
    s = max(1, args.scale)

    # Odd cell counts so the grid has a wall all the way round.
    cols = max(2, (W // s - 1) // 2)
    rows = max(2, (H // s - 1) // 2)
    out = np.empty((H, W, 3), np.uint8)

    state = {}

    def new_maze():
        opened, order = carve(cols, rows, rng)
        start = (1, 1)
        goal = (2 * rows - 1, 2 * cols - 1)
        dist, parent = flood(opened, start)
        gh, gw = opened.shape

        # Walk the parents back from the goal for the route itself.
        path = []
        node = goal[0] * gw + goal[1]
        if dist[goal] >= 0:
            while node >= 0:
                path.append((node // gw, node % gw))
                node = int(parent[node // gw, node % gw])
            path.reverse()

        # The carve order and the route as grids rather than as lists: the
        # animation is then "everything whose step number is below the
        # playhead", which is one comparison over the grid per frame instead
        # of a Python loop over a few thousand cells per frame.
        opened_at = np.full(opened.shape, -1, np.int32)
        for i, (y, x) in enumerate(order):
            if opened_at[y, x] < 0:
                opened_at[y, x] = i
        path_at = np.full(opened.shape, -1, np.int32)
        for i, (y, x) in enumerate(path):
            path_at[y, x] = i

        state.update(opened=opened, order=order, dist=dist, path=path,
                     opened_at=opened_at, path_at=path_at,
                     reach=int(dist.max()), t0=None)

    new_maze()

    def grid_to_panel(values):
        """Blow the (gh, gw) cell grid up to the panel, centred."""
        big = np.repeat(np.repeat(values, s, 0), s, 1)
        canvas = np.zeros((H, W), f32)
        h, w = min(H, big.shape[0]), min(W, big.shape[1])
        y0, x0 = (H - h) // 2, (W - w) // 2
        canvas[y0:y0 + h, x0:x0 + w] = big[:h, :w]
        return canvas

    def render(t, frame):
        st = state
        if st["t0"] is None:
            st["t0"] = t
        elapsed = t - st["t0"]

        order = st["order"]
        opened = st["opened"]
        carve_time = len(order) / args.carve_rate
        flood_time = st["reach"] / args.flood_rate
        path_time = len(st["path"]) / args.path_rate

        level = np.zeros(opened.shape, f32)

        if elapsed < carve_time:
            cut = int(elapsed * args.carve_rate)
            # Everything cut so far is corridor; the last few cells are the
            # head, brightest at the tip, which is what shows the walk
            # doubling back on itself.
            at = st["opened_at"]
            body = (at >= 0) & (at < cut)
            level[body] = 0.45
            tail = max(args.tail, 1)
            head = body & (at >= cut - tail)
            level[head] = 0.55 + 0.45 * ((at[head] - (cut - tail)) / tail)
        elif elapsed < carve_time + flood_time:
            reached = (elapsed - carve_time) * args.flood_rate
            level[opened] = 0.34
            wave = (st["dist"] >= 0) & (st["dist"] <= reached)
            level[wave] = 0.62
            # A brighter band at the leading edge, so the flood has a front
            # rather than being a slowly growing stain.
            edge = wave & (st["dist"] > reached - 6)
            level[edge] = 1.0
        else:
            done = elapsed - carve_time - flood_time
            level[opened] = 0.30
            level[st["dist"] >= 0] = 0.42
            shown = int(min(done * args.path_rate, len(st["path"])))
            at = st["path_at"]
            level[(at >= 0) & (at < shown)] = 1.0
            if done > path_time + args.hold:
                new_maze()

        panel = grid_to_panel(level)
        rgb = lut[np.clip(panel * 255.0, 0, 255).astype(np.int32)]
        # Walls are not black: a maze reads as a maze because you can see the
        # walls, and pure black on this panel makes it read as scattered lines.
        rgb[panel <= 0.0] = WALL
        np.copyto(out, rgb.astype(np.uint8))
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
