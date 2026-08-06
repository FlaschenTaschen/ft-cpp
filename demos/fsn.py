#!/usr/bin/env python3
"""fsn -- the Jurassic Park file system navigator, flown over a letterbox.

"It's a UNIX system, I know this." The thing on that screen was real software:
SGI's `fsn`, which laid a directory tree out as a landscape of extruded blocks
-- directories as raised pedestals, files as small flat slabs ranked on top of
them, walkways drawn on the ground from a parent to each of its children --
and let you fly over it. It was low-poly and lightly shaded because a 1993
Indigo had to draw it in real time, and that austerity is exactly what a
320x64 LED panel wants: thin bright wire edges plus flat dim faces survive
being reduced to sixty-four rows in a way that a shaded, textured scene does
not.

A 5:1 strip is the right shape for it too. This is a *map you fly over*, not a
corridor: the letterbox reads as a wide field of ground with structure spread
across it, and the interesting stuff -- the next directory you are heading
for, the ranks of files either side -- is off to the sides where a wide panel
has room. tunnel.py fills the panel with something radial, floor.py with a
texture; this one has recognisable objects standing on the ground and a
gateway you fly through, which is a different impression at any distance.

The camera never rotates. That is a rendering decision, not an artistic one:
with the view direction locked to +z every box stays axis-aligned in camera
space, which makes each of its three visible faces a quad whose screen-space
boundaries are exactly straight lines (both sx and sy are of the form
a + b/z along any edge, so sy is linear in sx). Faces are therefore filled by
row spans with linear interpolation and no per-pixel work at all. Banking is
put back in as a shear applied during projection -- sy += shear*(sx - cx) --
which is a rotation to first order, keeps vertical edges vertical, and costs
one multiply-add on the vertex arrays rather than a resample of the frame.

Everything is drawn by three things and nothing else:

  * a convex-quad filler that takes *all* quads at once, expands them to row
    spans and then to pixels, and lands them in the frame with a single
    fancy-index assignment. Occlusion is array order: numpy's fancy-index
    assignment writes duplicate indices in order, so a back-to-front list is a
    painter's algorithm and no depth buffer is needed. Each row emits its
    interior span in the face colour and then its two end spans in the edge
    colour, so a wire box costs an area plus a perimeter, stays properly
    opaque, and is rasterised once rather than twice. A face only a couple of
    pixels across skips its interior span and reads as a bright speck, which
    is what a distant box should look like anyway.
  * a batched line drawer with a vectorised Liang-Barsky clip, for the ground
    grid, the walkways and the cursor.
  * the ground plane, sky and frame clear together as one copy: under a shear
    a pixel's height above the horizon is an integer per-column offset from
    its row, which depends on nothing but the bank, so every distinguishable
    bank angle gets its whole panel baked once in build().

The world is periodic in z, so the flythrough loops seamlessly rather than
running off the end of a finite tree: `--depth` directories spaced STRIDE
apart, and the camera's x path is two harmonics whose period is exactly that
of the world, which means the path passes precisely over every gateway with no
spline to fit. Altitude dips to a skim as it crosses each one -- a long
descent, a fast climb away -- so each pass reads as dropping *into* that
directory while the next rank of children rises into view ahead. A cursor
slides on ahead to bracket the directory being entered, which is what makes it
look driven rather than merely animated.

Run:  python3 fsn.py --host 127.0.0.1
      python3 fsn.py --caption --density 1.4 --seed 12
"""

import math
import sys
from bisect import bisect

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * np.pi
GONE = 1e7          # a span start beyond any column: the span is dropped
_NEXT = np.array([1, 2, 3, 0])   # the vertex after each one, around a quad
_NEUT_L = np.array([[[-np.inf]], [[0.0]]], np.float32)  # a line that bounds
_NEUT_R = np.array([[[np.inf]], [[0.0]]], np.float32)   # ... nothing, either way

STRIDE = 90.0            # world z from one directory to the next
SLAB_H = 3.0             # a directory's platform is a low plinth, not the floor
NEAR = 6.0               # near plane; without it a face straddling the camera
                         # explodes across the whole panel
FAR = 140.0              # a directory and a half ahead; past that the boxes
                         # are sub-pixel confetti that only costs frame time
FADE = 50.0              # ... and they haze out over the last of it, because a
                         # draw distance this short would otherwise pop
SLACK = 45.0             # half-depth of the deepest box, so a slice taken by
                         # centre z cannot miss one whose front edge is in shot
CRUISE = 32.0
DIP = 13.0               # skim height over a gateway; under the lintel at 19
GLUT = 160               # ground shades, indexed by rows below the horizon
GPAD = 70                # ... offset so a banked horizon cannot index outside
MAX_SHEAR = 0.28         # and the bank that GPAD is sized for
NBANK = 41               # baked ground panels, one per distinguishable bank

# A 3x5 bitmap font, baked because render() may not touch a font file and
# because rasterising glyphs per frame for a label this small is absurd.
# Each glyph is five rows of three bits, MSB leftmost.
_FONT = {
    " ": (0, 0, 0, 0, 0),
    "A": (0b010, 0b101, 0b111, 0b101, 0b101),
    "B": (0b110, 0b101, 0b110, 0b101, 0b110),
    "C": (0b011, 0b100, 0b100, 0b100, 0b011),
    "D": (0b110, 0b101, 0b101, 0b101, 0b110),
    "E": (0b111, 0b100, 0b110, 0b100, 0b111),
    "F": (0b111, 0b100, 0b110, 0b100, 0b100),
    "G": (0b011, 0b100, 0b101, 0b101, 0b011),
    "H": (0b101, 0b101, 0b111, 0b101, 0b101),
    "I": (0b111, 0b010, 0b010, 0b010, 0b111),
    "J": (0b001, 0b001, 0b001, 0b101, 0b010),
    "K": (0b101, 0b101, 0b110, 0b101, 0b101),
    "L": (0b100, 0b100, 0b100, 0b100, 0b111),
    "M": (0b101, 0b111, 0b111, 0b101, 0b101),
    "N": (0b101, 0b111, 0b111, 0b111, 0b101),
    "O": (0b010, 0b101, 0b101, 0b101, 0b010),
    "P": (0b110, 0b101, 0b110, 0b100, 0b100),
    "Q": (0b010, 0b101, 0b101, 0b111, 0b011),
    "R": (0b110, 0b101, 0b110, 0b101, 0b101),
    "S": (0b011, 0b100, 0b010, 0b001, 0b110),
    "T": (0b111, 0b010, 0b010, 0b010, 0b010),
    "U": (0b101, 0b101, 0b101, 0b101, 0b011),
    "V": (0b101, 0b101, 0b101, 0b101, 0b010),
    "W": (0b101, 0b101, 0b111, 0b111, 0b101),
    "X": (0b101, 0b101, 0b010, 0b101, 0b101),
    "Y": (0b101, 0b101, 0b010, 0b010, 0b010),
    "Z": (0b111, 0b001, 0b010, 0b100, 0b111),
    "0": (0b111, 0b101, 0b101, 0b101, 0b111),
    "1": (0b010, 0b110, 0b010, 0b010, 0b111),
    "2": (0b110, 0b001, 0b010, 0b100, 0b111),
    "3": (0b110, 0b001, 0b010, 0b001, 0b110),
    "4": (0b101, 0b101, 0b111, 0b001, 0b001),
    "5": (0b111, 0b100, 0b110, 0b001, 0b110),
    "6": (0b011, 0b100, 0b110, 0b101, 0b010),
    "7": (0b111, 0b001, 0b010, 0b010, 0b010),
    "8": (0b010, 0b101, 0b010, 0b101, 0b010),
    "9": (0b010, 0b101, 0b011, 0b001, 0b110),
    "/": (0b001, 0b001, 0b010, 0b100, 0b100),
    ".": (0b000, 0b000, 0b000, 0b000, 0b010),
    "-": (0b000, 0b000, 0b111, 0b000, 0b000),
    "'": (0b010, 0b010, 0b000, 0b000, 0b000),
}


def text_mask(text, scale=1):
    """A boolean (5*scale, n*4*scale - scale) stamp for a short string."""
    glyphs = [_FONT.get(ch, _FONT[" "]) for ch in text.upper()]
    if not glyphs:
        return np.zeros((0, 0), bool)
    out = np.zeros((5, len(glyphs) * 4 - 1), bool)
    for i, rows in enumerate(glyphs):
        for r, bits in enumerate(rows):
            for c in range(3):
                if bits & (1 << (2 - c)):
                    out[r, i * 4 + c] = True
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


# --------------------------------------------------------------------------
# Batched rasterisation. Both of these take every primitive of their kind for
# the frame in one call; the Python-level cost is then a fixed few dozen numpy
# operations regardless of how many boxes are on screen, which is the only way
# a scene like this fits an ARMv7 frame budget.
#
# A numpy call on an array of the size this scene deals in -- a few hundred to
# a few thousand elements -- costs a Pi 3 something like eighty microseconds
# almost regardless of the elements in it, so what a frame buys is *calls*.
# Two hundred of them is the whole budget, and that is why the code below is
# shaped the way it is:
#
#   * np.clip is never used. Under numpy 1.19 it carries a deprecation shim
#     that costs 0.4 ms per call whatever the array size -- eight calls a
#     frame was three milliseconds of pure overhead. Every clamp here is a
#     minimum/maximum pair, which is two ordinary ufuncs.
#   * np.repeat is never used on a per-pixel array. It runs an order of
#     magnitude slower than an ordinary pass, and all three ragged expansions
#     in here used to be built from it. They are running sums now; see _seq.
#   * no float32 *scalars* survive into render(). Arithmetic on one costs
#     fifty microseconds where the same arithmetic on a Python float costs
#     under two, and numpy's value-based casting keeps a float32 result when a
#     Python float meets a float32 array anyway.
#   * float32 arrays are kept end to end. `int_array + 0.5` promotes to
#     float64, and one such slip put the whole scanline loop in double.
#   * a boolean index or a gather into the *second* axis of a stacked block
#     runs three times slower than the same work done a row at a time; where a
#     loop over rows is unavoidable, the rows are the fast axis instead.
#
# Colours move as a single packed int32 word, not as (N, 3) uint8. The scatter
# and the gather that feed it are the two largest operations in the frame, and
# packing makes both of them one third the size; the frame is unpacked once at
# the end. Byte order is native, so the packing here has to match the
# little-endian layout of both x86 and the Pi.
# --------------------------------------------------------------------------

def pack(rgb):
    """(..., 3) float colour -> native-order int32, as the buffer stores it."""
    c = np.minimum(np.maximum(rgb, 0.0), 255.0).astype(np.int32)
    return c[..., 0] | (c[..., 1] << 8) | (c[..., 2] << 16)


def _heads(counts):
    """Where each run starts in the concatenation of ragged runs, and the total.

    Every count must be at least one, so the heads are distinct and can be
    scattered into without collisions -- which is what the three expansions
    below rely on.
    """
    ends = np.cumsum(counts)
    return ends - counts, int(ends[-1])


def _room(scratch, total):
    """A scratch slice of `total` int32, or a fresh array if it will not fit.

    The expansions below are the only per-pixel arrays in the frame and they
    are the same size every frame to within a factor of two, so they are cut
    from a buffer that build() allocated once. The fallback is for the
    pathological frame; it is correctness insurance, not a path worth tuning.
    """
    return scratch[:total] if total <= scratch.shape[0] else np.empty(total,
                                                                     np.int32)


def _seq(head, total, start, scratch):
    """Concatenated integer ranges start[i] .. start[i] + count[i] - 1.

    np.repeat is the obvious way to expand a run-length structure and is by
    far the dearest array operation on an ARMv7. This is the same result as a
    running sum of first differences: one step inside a run, and at each run
    boundary whatever step lands on the next run's first element.
    """
    d = _room(scratch, total)
    d[:] = 1
    step = np.empty(head.shape[0], np.int32)
    step[0] = start[0]
    step[1:] = start[1:] - start[:-1] - head[1:] + head[:-1] + 1
    d[head] = step
    return np.cumsum(d, out=d)


def _hold(head, total, val, scratch):
    """val[i] repeated across run i -- a gather written as the same running sum.

    Only sound for values that fit comfortably inside int32, which packed
    colours (24 bits) do.
    """
    d = _room(scratch, total)
    d[:] = 0
    d[0] = val[0]
    d[head[1:]] = val[1:] - val[:-1]
    return np.cumsum(d, out=d)


def _runs(head, total, scratch):
    """Which run each element of the concatenation belongs to."""
    d = _room(scratch, total)
    d[:] = 0
    d[head[1:]] = 1
    return np.cumsum(d, out=d)


def fill_quads(flat, W, H, xv, yv, cfill, cwire, scr):
    """Paint convex quads, first to last, into a flat (H*W,) int32 view.

    xv, yv are (4, N): the four screen-space corners of each quad, in order
    around it. Vertex-major, not quad-major, so that every column of the
    working set stays contiguous -- a strided column view costs three to four
    times an equivalent contiguous one on a Pi.

    Occlusion is array order: numpy's fancy-index assignment writes duplicate
    indices in order, so a single assignment of a back-to-front list is a
    painter's algorithm with no depth buffer and no per-quad Python.

    Every quad is both filled and outlined. Each of its rows emits three
    spans -- its interior in cfill, then its left and right ends in cwire --
    in that order, so the outline lands on top of its own fill for the price
    of one pass over the rows rather than two. A quad only a couple of pixels
    across drops the interior span: it is all outline anyway, and most of the
    scene is distant, so most of the quads go that way.
    """
    if xv.shape[1] == 0:
        return
    ymin = np.minimum(np.minimum(yv[0], yv[1]), np.minimum(yv[2], yv[3]))
    ymax = np.maximum(np.maximum(yv[0], yv[1]), np.maximum(yv[2], yv[3]))
    xmin = np.minimum(np.minimum(xv[0], xv[1]), np.minimum(xv[2], xv[3]))
    xmax = np.maximum(np.maximum(xv[0], xv[1]), np.maximum(xv[2], xv[3]))
    # floor() on both ends, not ceil/floor: a quad thinner than a pixel still
    # gets exactly one row, which is how far-off boxes stay visible as specks
    # instead of blinking out. A quad off the top or the bottom comes out of
    # this with r1 < r0 and is dropped by the same test.
    r0 = np.maximum(np.floor(ymin), 0.0).astype(np.int32)
    r1 = np.minimum(np.floor(ymax), (H - 1.0)).astype(np.int32)
    count = r1 - r0 + 1
    live = (count > 0) & (xmax >= 0.0) & (xmin <= (W - 1.0))
    if not live.any():
        return
    xv, yv = xv[:, live], yv[:, live]
    ymin, ymax, xmin, xmax = ymin[live], ymax[live], xmin[live], xmax[live]
    cfill, cwire = cfill[live], cwire[live]
    r0, count = r0[live], count[live]

    # A convex quad is the intersection of its four edges' half-planes, and
    # each of those, written as x = A + B*y, bounds the row span from one side
    # for *every* row -- not just the rows the edge itself spans. So the span
    # is a max of two lines against a min of two lines with no clipping, no
    # parameter test and no per-edge branch: the whole scanline step is a
    # multiply-add and a minimum per edge. An edge running horizontally
    # constrains y rather than x and is neutralised with an infinity, which
    # the bounding box behind it then covers.
    dx = xv.take(_NEXT, 0) - xv
    dy = yv.take(_NEXT, 0) - yv
    # An edge counts as sloped only if it moves less than a thousand columns
    # per row. Anything flatter cannot usefully bound a span, and admitting it
    # would put a slope of 1e6 into a float32 intercept.
    tilt = np.abs(dy) * 1000.0 > np.abs(dx)
    B = dx / np.where(tilt, dy, 1.0)
    A = xv - B * yv
    # The interior is on the centroid's side of each edge, which is what says
    # whether the edge caps the span on the left or on the right.
    xc = xv.sum(0) * 0.25
    yc = yv.sum(0) * 0.25
    left = (xc > A + B * yc) & tilt
    right = tilt & ~left
    # One block, so the per-row working set is a single gather: the four left
    # lines, the four right lines, the bounding box, and a flag folded into an
    # offset that pushes a speck's interior span off the panel. An edge that is
    # not a bound of its side goes in as an infinity with a zero slope, so it
    # survives the reduction below without ever being tested for.
    AB = np.stack((A, B))
    G = np.concatenate((
        np.where(left, AB, _NEUT_L).reshape(8, -1),
        np.where(right, AB, _NEUT_R).reshape(8, -1),
        np.stack((ymin, ymax, xmin, xmax,
                  np.where((xmax - xmin > 2.5) & (ymax - ymin > 2.5),
                           0.0, GONE)))))

    head, m = _heads(count)
    rows = _seq(head, m, r0, scr[0])
    qi = _runs(head, m, scr[1])
    Q = G[:, qi]
    # Sample at the row centre, pulled just inside the quad so a nearly flat
    # quad still gets a span rather than an empty intersection.
    y = rows.astype(f32)
    y += 0.5
    np.maximum(y, Q[16] + 1e-4, out=y)
    np.minimum(y, Q[17] - 1e-4, out=y)
    # All four lines at once, then one reduction each way. Four separate
    # multiply-add-compare steps move the same bytes in three times the numpy
    # calls, and on this machine the calls are what the frame is made of.
    lo = np.maximum((Q[0:4] + Q[4:8] * y).max(0), Q[18])
    hi = np.minimum((Q[8:12] + Q[12:16] * y).min(0), Q[19])

    # The outline has to be as wide as the edge moved between this row and the
    # next, or a shallow edge comes out as a row of dots rather than a line.
    # An inner row always has its successor in the same quad, so the next row's
    # span can be read straight off without checking.
    nlo = np.empty(m, f32)
    nhi = np.empty(m, f32)
    nlo[:-1], nlo[-1] = lo[1:], lo[-1]
    nhi[:-1], nhi[-1] = hi[1:], hi[-1]
    inner = np.ones(m, bool)
    inner[head] = False                 # the quad's own top and bottom rows
    inner[head + count - 1] = False     # ... stay solid: they are its end caps

    # Interleaved, not concatenated: the three spans of one row have to stay
    # adjacent in the array or the back-to-front ordering that the whole
    # painter's algorithm rests on is lost.
    n3 = 3 * m
    lo3 = np.empty(n3, f32)
    hi3 = np.empty(n3, f32)
    cap_l = np.maximum(lo, nlo)
    cap_r = np.minimum(hi, nhi)
    # The interior span stops one column short of each cap rather than running
    # under it. Nothing looks different -- the caps are opaque -- but it is a
    # fifth of the frame's pixels not written twice.
    lo3[0::3] = np.where(inner, cap_l + (1.0 + Q[20]), GONE)
    hi3[0::3] = cap_r - 1.0
    lo3[1::3] = lo
    hi3[1::3] = np.where(inner, cap_l, hi)
    lo3[2::3] = np.where(inner, cap_r, GONE)
    hi3[2::3] = hi

    rows3 = np.empty(n3, np.int32)
    rows3[0::3] = rows
    rows3[1::3] = rows
    rows3[2::3] = rows
    col3 = np.empty(n3, np.int32)
    col3[0::3] = cfill[qi]
    wire = cwire[qi]
    col3[1::3] = wire
    col3[2::3] = wire

    c0 = np.maximum(np.floor(lo3 + 0.5), 0.0).astype(np.int32)
    c1 = np.minimum(np.floor(hi3 + 0.5), (W - 1.0)).astype(np.int32)
    span = c1 - c0 + 1
    wide = span > 0
    if not wide.any():
        return
    span = span[wide]
    # Fold the row and the span's colour into per-span values *before* the
    # expansion. Done afterwards, the same arithmetic is two more passes over
    # the pixel array, which is by a distance the longest array in the frame.
    start = rows3[wide] * W + c0[wide]
    span_col = col3[wide]

    head, total = _heads(span)
    flat[_seq(head, total, start, scr[2])] = _hold(head, total, span_col, scr[3])


def draw_lines(flat, W, H, x0, y0, x1, y1, lcol, scr, fscr, lim):
    """Paint screen-space segments, clipped to the panel, all in one pass."""
    if x0.shape[0] == 0:
        return
    dx = x1 - x0
    dy = y1 - y0
    # Liang-Barsky without the sign tests, both axes stacked into one array:
    # each axis contributes the parameter interval in which the segment is
    # inside the panel, and the two ends of that interval sort themselves with
    # a minimum/maximum pair. A segment parallel to an axis divides by an
    # epsilon of fixed sign instead, which sends its interval off to +-1e9 --
    # covering [0, 1] when the segment is inside that axis's slab and missing
    # it entirely when it is not.
    d = np.stack((dx, dy))
    e = np.where(np.abs(d) < 1e-6, 1e-6, d)
    p0 = np.stack((x0, y0))
    ea = -p0 / e
    eb = (lim - p0) / e
    elo = np.minimum(ea, eb)
    ehi = np.maximum(ea, eb)
    t0 = np.maximum(np.maximum(elo[0], elo[1]), 0.0)
    t1 = np.minimum(np.minimum(ehi[0], ehi[1]), 1.0)
    keep = t0 <= t1
    if not keep.any():
        return
    x0, y0, dx, dy = x0[keep], y0[keep], dx[keep], dy[keep]
    t0, t1 = t0[keep], t1[keep]
    ux = dx * (t1 - t0)
    uy = dy * (t1 - t0)
    lcol = lcol[keep]

    steps = (np.maximum(np.abs(ux), np.abs(uy)) + 1.5).astype(np.int32)
    head, total = _heads(steps)
    li = _runs(head, total, scr[4])
    inv = 1.0 / np.maximum(steps - 1, 1).astype(f32)
    if total > fscr.shape[1]:
        fscr = np.empty((4, total), f32)
    # Gathered a row at a time -- a fancy index into the second axis of a
    # stacked pair runs three times slower than two flat takes -- and walked
    # as one array, since the arithmetic is the same for x and y.
    p = fscr[:2, :total]
    (ux * inv).take(li, out=p[0])
    (uy * inv).take(li, out=p[1])
    p *= _seq(head, total, np.zeros_like(steps), scr[5]).astype(f32)
    q = fscr[2:4, :total]
    (x0 + dx * t0).take(li, out=q[0])
    (y0 + dy * t0).take(li, out=q[1])
    p += q
    p += 0.5
    # Liang-Barsky already put both ends inside the panel, so the walk can
    # only leave it by a rounding error -- and the cast truncates towards
    # zero, which covers the low end. Only the high end needs a guard.
    np.minimum(p, lim, out=p)
    pi = p.astype(np.int32)
    flat[pi[1] * W + pi[0]] = lcol.take(li)


def blit(buf, mask, x, y, col):
    """Stamp a baked glyph mask, clipped to the panel."""
    h, w = mask.shape
    H, W = buf.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sub = mask[y0 - y:y1 - y, x0 - x:x1 - x]
    region = buf[y0:y1, x0:x1]
    region[sub] = col


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cycle", type=float, default=45.0,
                    help="seconds for one loop of the tree, at --speed 1")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="rate multiplier; 2 flies twice as fast and loops "
                         "in half the time")
    ap.add_argument("--depth", type=int, default=11,
                    help="directories visited per loop")
    ap.add_argument("--density", type=float, default=0.6,
                    help="how many file blocks rank up beside each directory")
    ap.add_argument("--no-labels", dest="labels", action="store_false",
                    help="drop the name tag over the directory being entered")
    ap.add_argument("--caption", action="store_true",
                    help="spell out the line; the geometry carries it without")
    ap.add_argument("--no-grid", dest="grid", action="store_false",
                    help="drop the ground grid")
    ap.add_argument("--bank", type=float, default=0.010,
                    help="how hard the camera rolls into its drift")
    ap.add_argument("--horizon", type=float, default=0.34,
                    help="eye level as a fraction of the panel height")
    ap.add_argument("--focal", type=float, default=0.30,
                    help="focal length as a fraction of the width; larger is "
                         "a narrower field of view")
    ap.add_argument("--fog-scale", type=float, default=170.0,
                    help="depth at which the scene is half faded out")
    ap.add_argument("--seed", type=int, default=5, help="0 picks one at random")


def build(args):

    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)

    levels = max(3, int(args.depth))
    period = levels * STRIDE
    speed = period / max(args.cycle, 1.0) * max(args.speed, 1e-3)
    total = period / speed                  # sheet.py reads this for its span

    focal = float(args.focal) * W
    cx_s = W * 0.5
    cy_s = float(args.horizon) * H
    fog_k = float(max(args.fog_scale, 1.0))

    # The camera's ground track is two harmonics of the loop, so it is exactly
    # periodic and passes exactly over lat(k) for integer k -- which is why
    # every directory can simply be placed at lat(k) with no spline to fit.
    amp = (26.0 + 6.0 * rng.random(), 13.0 + 6.0 * rng.random())
    pha = rng.uniform(0.0, TAU, 2)
    wk = TAU / levels

    # math, not numpy: render() only ever wants these at a single scalar u,
    # and a numpy sin on a scalar costs eighty microseconds against one.
    def lat(u):
        """Lateral world x of the trunk of the tree at level coordinate u."""
        return (amp[0] * math.sin(wk * u + pha[0])
                + amp[1] * math.sin(2.0 * wk * u + pha[1]))

    def lat_du(u):
        return (amp[0] * wk * math.cos(wk * u + pha[0])
                + 2.0 * amp[1] * wk * math.cos(2.0 * wk * u + pha[1]))

    def lat_arr(u):
        return (amp[0] * np.sin(wk * u + pha[0])
                + amp[1] * np.sin(2.0 * wk * u + pha[1]))

    # ---- the tree, laid out once ----
    SLAB_W, SLAB_D = 52.0, 32.0
    COL_SLAB = ((16, 27, 38), (46, 118, 138))
    COL_GATE = ((20, 30, 34), (255, 168, 64))     # warm: the way in
    COL_DIR = ((14, 32, 36), (60, 220, 190))
    COL_FILE = ((13, 26, 32), (40, 168, 134))
    COL_PYLON = ((10, 18, 26), (34, 92, 116))

    bx0, bx1, by0, by1, bz0, bz1, bface, bedge = [], [], [], [], [], [], [], []

    def box(x, z, hw, hd, y0, y1, col):
        bx0.append(x - hw)
        bx1.append(x + hw)
        by0.append(y0)
        by1.append(y1)
        bz0.append(z - hd)
        bz1.append(z + hd)
        bface.append(col[0])
        bedge.append(col[1])

    lseg = []
    WALK = SLAB_H + 0.15                    # walkways lie on the plinth

    def link(xa, za, xb, zb, col):
        lseg.append((xa, WALK, za, xb, WALK, zb) + tuple(col))

    ncol = max(1, int(round(2 * args.density)))
    nrow = max(2, int(round(2 * args.density)))
    names = ["/usr", "/bin", "/dev", "/etc", "/var", "/lib", "/opt", "/tmp",
             "/proc", "/home", "/mnt", "/srv", "/boot", "/sbin", "/root",
             "/sys", "/net", "/share", "/local", "/spool"]
    pick = rng.permutation(len(names))
    level_name = [names[pick[k % len(names)]] for k in range(levels)]

    for k in range(levels):
        cxk = float(lat(k))
        czk = k * STRIDE
        box(cxk, czk, SLAB_W, SLAB_D, 0.0, SLAB_H, COL_SLAB)

        # The gateway: two pillars and a lintel, wide enough and tall enough
        # that the camera passes between and under them rather than into them.
        for side in (-1.0, 1.0):
            box(cxk + side * 14.0, czk, 3.0, 4.0, SLAB_H, 22.0, COL_GATE)
        box(cxk, czk, 17.0, 4.0, 19.0, 22.5, COL_GATE)

        # Files rank up either side of the way through, on the plinth. One
        # spine each side rather than a stub to every block: at this size a
        # line per file is a hedge, not a set of connections.
        for side in (-1.0, 1.0):
            spine = cxk + side * 21.0
            link(cxk, czk, spine, czk - 20.0, COL_FILE[1])
            link(spine, czk - 20.0, spine, czk + 20.0, COL_FILE[1])
            for i in range(ncol):
                fx = cxk + side * (27.0 + i * 11.0)
                for j in range(nrow):
                    fz = czk - 20.0 + j * (40.0 / max(nrow - 1, 1))
                    box(fx, fz, 4.5, 4.5, SLAB_H,
                        SLAB_H + 1.5 + 3.5 * rng.random(), COL_FILE)
                    link(spine, fz, fx - side * 4.5, fz, COL_FILE[1])

        # Two child directories that are not the one being entered: pedestals
        # at the far corners, so the tree visibly branches rather than being a
        # single chain of gateways. Beyond them a pylon each side, off the
        # plinth entirely -- on a 5:1 panel the outer thirds are otherwise
        # nothing but receding grid, and something has to sweep through them.
        for side in (-1.0, 1.0):
            dx = cxk + side * 45.0
            dz = czk + side * 20.0
            box(dx, dz, 6.0, 6.0, SLAB_H, SLAB_H + 8.0 + 6.0 * rng.random(),
                COL_DIR)
            link(cxk, czk, dx, dz, COL_DIR[1])
            box(cxk + side * 78.0, czk + side * 34.0, 3.0, 3.0, 0.0,
                14.0 + 9.0 * rng.random(), COL_PYLON)

        # The trunk: the walkway to the child that *is* entered next is the
        # camera's own ground track, which is the clearest possible statement
        # of where this flight is going.
        us = k + np.linspace(0.0, 1.0, 7)
        xs = lat_arr(us)
        for i in range(6):
            link(float(xs[i]), float(us[i] * STRIDE),
                 float(xs[i + 1]), float(us[i + 1] * STRIDE), COL_DIR[1])

    bx0 = np.asarray(bx0, f32)
    bx1 = np.asarray(bx1, f32)
    by0 = np.asarray(by0, f32)
    by1 = np.asarray(by1, f32)
    bz0 = np.asarray(bz0, f32)
    bz1 = np.asarray(bz1, f32)
    bface = np.asarray(bface, f32)
    bedge = np.asarray(bedge, f32)

    # Faces are lit by which way they point, and only by that: a top catches
    # the (imaginary) overhead light, a front less, a side least. Three
    # constants is the whole shading model, which is all the original had.
    FACE_LIT = (0.62, 1.0, 0.45)     # front, top, side

    # Both colours are stored per box *and per face*, already lit, so a frame
    # scales them by fog and packs the pair in one call with no expansion step
    # at all: the (n, 2, 3, 3) block is exactly the two (3n,) runs of face and
    # edge colours the filler wants.
    nbox = bface.shape[0]
    bcol = np.empty((nbox, 2, 3, 3), f32)
    for e in range(3):
        bcol[:, 0, e, :] = bface * FACE_LIT[e]
        bcol[:, 1, e, :] = bedge

    # ---- world laid out for the slice that render() actually draws ----
    # The camera never rotates and never leaves the +z axis, so a box's
    # painter's-algorithm depth is just its centre z. That order is fixed, so
    # it is sorted here rather than argsorted every frame: a stable sort keeps
    # the build order among boxes at the same z, which is what puts a plinth
    # under the pillars standing on it. Three copies of the world laid end to
    # end then turn the periodic wrap into a plain contiguous slice.
    bzc = 0.5 * (bz0 + bz1)
    order = np.argsort(-bzc, kind="stable")
    tri = np.concatenate([order, order, order])
    shift = np.repeat(np.asarray([period, 0.0, -period], f32), nbox)
    # Kept as flat arrays and masked one at a time: a boolean index into the
    # second axis of a stacked block runs three times slower than the same
    # work done a row at a time.
    bx0_t, bx1_t = bx0[tri], bx1[tri]
    by0_t, by1_t = by0[tri], by1[tri]
    bz0_t, bz1_t = bz0[tri] + shift, bz1[tri] + shift
    bcol_t = bcol[tri]
    # Descending centre z, so a slice of it is already back to front; bisect
    # wants ascending, and on a list it costs a microsecond against a hundred
    # for np.searchsorted on the array.
    bkey = list(-(bzc[tri] + shift))

    seg = np.asarray(lseg, f32)
    seg_key = np.minimum(seg[:, 2], seg[:, 5])
    sorder = np.argsort(seg_key)
    seg_t = np.concatenate([seg[sorder], seg[sorder], seg[sorder]])
    zoff = np.repeat(np.asarray([-period, 0.0, period], f32), sorder.shape[0])
    seg_t[:, 2] += zoff
    seg_t[:, 5] += zoff
    seg_key = list(np.concatenate([seg_key[sorder] - period, seg_key[sorder],
                                   seg_key[sorder] + period]))
    SEG_LEN = float(np.abs(seg[:, 5] - seg[:, 2]).max()) + 1.0

    grid_col = np.asarray((26, 56, 70), f32)
    cursor_col = np.asarray((255, 255, 235), f32)
    label_col = np.asarray((255, 190, 90), np.uint8)

    # Each name is kept as the coordinates of its lit pixels rather than as a
    # mask. A boolean-mask assignment has to build those coordinates anyway,
    # and it does it every frame into a region the unpack has just walked
    # past; two short integer arrays cost a tenth of that.
    name_tags = []
    for n in level_name:
        mask = text_mask(n)
        rr, cc = np.nonzero(mask)
        name_tags.append((rr.astype(np.int32), cc.astype(np.int32),
                          mask.shape[1]))
    cap_a = text_mask("IT'S A UNIX SYSTEM", 2)
    cap_b = text_mask("I KNOW THIS", 2)

    # ---- the segments that move with the camera, in one preallocated block ----
    # Ground grid then cursor, laid out as the same nine columns as seg_t so
    # the whole frame's line work is a single concatenate.
    GRID = 30.0
    gz_n = 6
    gz_step = np.arange(gz_n, dtype=f32) * GRID
    gx_line = np.arange(-3, 4, dtype=f32) * GRID     # constant-x ground lines
    ng = gx_line.shape[0]
    arm, rad = 12.0, 22.0
    corner = np.array([-1.0, -1.0, 1.0, 1.0], f32)
    cz_s = np.array([-1.0, 1.0, -1.0, 1.0], f32)
    # Two arms and a mast at each corner. The mast is what makes the cursor
    # findable: flat brackets on the ground disappear into the walkways as
    # soon as anything else is drawn near them.
    cur_x0 = np.concatenate([corner * rad] * 3)
    cur_z0 = np.concatenate([cz_s * rad] * 3)
    cur_x1 = np.concatenate([corner * (rad - arm), corner * rad, corner * rad])
    cur_z1 = np.concatenate([cz_s * rad, cz_s * (rad - arm), cz_s * rad])

    NG = gz_n + ng
    dyn = np.zeros((NG + 12, 9), f32)
    dyn[NG - ng:NG, 0] = gx_line
    dyn[NG - ng:NG, 3] = gx_line
    dyn[:NG, 6:9] = grid_col
    dyn[NG:, 1] = WALK + 0.2
    dyn[NG:NG + 8, 4] = WALK + 0.2
    dyn[NG + 8:, 4] = WALK + 13.0
    dyn_cur = dyn[NG:]
    dyn_all = dyn if args.grid else dyn_cur

    # Ground: shaded by how far a pixel sits below the horizon, which under a
    # shear is (row - cy) - shear*(col - cx). That is one expression over the
    # whole frame and one lookup, so the ground, the sky and the frame clear
    # come out together -- far cheaper than drawing the plane as geometry, and
    # it comes out smooth instead of banded.
    # The lookup is padded either side of the panel's own range of rows so
    # that no index can fall outside it whatever the bank does, which is what
    # lets the whole ground be an add and a take with no clip in between.
    gd = np.arange(GLUT, dtype=f32) - GPAD
    edge_soft = np.minimum(np.maximum(gd - 0.4, 0.0), 1.0)[:, None]  # soft horizon
    near_up = np.minimum(np.maximum(gd / 44.0, 0.0), 1.0)[:, None] ** 0.75
    ground_lut = pack((np.asarray((3, 7, 13), f32)
                       + np.asarray((20, 31, 43), f32) * near_up) * edge_soft)
    ground_base = np.repeat(
        (np.arange(H, dtype=np.int32) - int(round(cy_s)) + GPAD)[:, None], W, 1)
    col_of = (np.arange(W, dtype=f32) - cx_s)[None, :]

    # ... and since that add and take depend on nothing but the bank angle,
    # every bank the flight can reach gets its whole panel baked here. The
    # shear is quantised to the same ladder and used for the geometry too, so
    # the horizon and the boxes standing on it never disagree.
    smax = min(MAX_SHEAR, abs(args.bank) * (abs(amp[0]) * wk + 2 * abs(amp[1]) * wk)
               * speed / STRIDE)
    smax = max(smax, 1e-6)
    shear_of = list(np.linspace(-smax, smax, NBANK, dtype=f32).astype(float))
    bank_scale = (NBANK - 1) / (2.0 * smax)
    ground = np.empty((NBANK, H, W), np.int32)
    for i in range(NBANK):
        ground[i] = ground_lut.take(
            ground_base + np.rint(col_of * -shear_of[i]).astype(np.int32))

    # Drawn into RGBA so a pixel is one int32; unpacked to the returned RGB
    # frame once, at the end.
    rgba = np.zeros((H, W, 4), np.uint8)
    frame32 = rgba.view(np.int32).reshape(H, W)
    flat = frame32.reshape(-1)
    buf = np.zeros((H, W, 3), np.uint8)
    # Working room for the ragged expansions. Two panels' worth is about three
    # times what a pass-through frame asks for, and _room falls back to a
    # fresh array rather than truncating if a frame ever wants more.
    scr = np.empty((6, 2 * H * W), np.int32)
    fscr = np.empty((4, 2 * H * W), f32)
    lim = np.array([[W - 1], [H - 1]], f32)

    def render(t, frame):
        u = t * speed / STRIDE
        k = int(math.floor(u))
        frac = u - k
        # The world repeats every `period`, so the camera is kept inside one
        # copy of it: everything downstream works in that frame and keeps its
        # float32 precision however long the demo has been running.
        cz = (frac + k % levels) * STRIDE
        cam_x = lat(u)

        # Altitude: a long descent into the gateway at frac 0 and a fast climb
        # away from it. The warp on frac is what makes it asymmetric, and the
        # asymmetry is what reads as descending rather than as bobbing.
        warp = frac ** 0.70
        cam_y = DIP + (CRUISE - DIP) * (0.5 - 0.5 * math.cos(TAU * warp)) ** 0.8
        # Roll into the drift. Right-hand drift tips the right of the horizon
        # up, hence the sign.
        gi = int(round((-args.bank * lat_du(u) * speed / STRIDE + smax)
                       * bank_scale))
        gi = min(max(gi, 0), NBANK - 1)
        shear = shear_of[gi]

        # ---- ground, sky and frame clear, in one copy ----
        frame32[:] = ground[gi]

        # ---- world-space lines: grid, then walkways, then the cursor ----
        if args.grid:
            gz = math.ceil((cz + 20.0) / GRID) * GRID + gz_step
            dyn[:gz_n, 0] = cam_x - 210.0
            dyn[:gz_n, 3] = cam_x + 210.0
            dyn[:gz_n, 2] = gz
            dyn[:gz_n, 5] = gz
            # The constant-x lines are fixed in the world, not carried along
            # with the camera: the flight path only ever drifts about fifty
            # units either side of the axis, so a fixed set covers it, and a
            # grid that slid sideways would cancel the sense of drift that the
            # bank is there to give.
            dyn[gz_n:NG, 2] = cz + 20.0
            dyn[gz_n:NG, 5] = cz + FAR

        # The cursor slides ahead to bracket the directory being entered.
        homing = min(frac / 0.55, 1.0)
        homing = homing * homing * (3.0 - 2.0 * homing)
        tgt_x = lat(k + homing)
        tgt_z = cz + (homing - frac) * STRIDE
        dyn_cur[:, 0] = tgt_x + cur_x0
        dyn_cur[:, 2] = tgt_z + cur_z0
        dyn_cur[:, 3] = tgt_x + cur_x1
        dyn_cur[:, 5] = tgt_z + cur_z1
        # Blinking while it travels, steady once it has locked on.
        pulse = 1.0 if homing >= 1.0 else \
            0.45 + 0.55 * (0.5 + 0.5 * math.sin(TAU * 2.5 * t))
        dyn_cur[:, 6:9] = cursor_col * pulse

        # Periodic images: the world is laid out three copies deep and sorted,
        # so the segments anywhere near the camera are one contiguous slice.
        S = np.concatenate((dyn_all, seg_t[bisect(seg_key, cz - SEG_LEN):
                                           bisect(seg_key, cz + FAR)]))

        sz0 = S[:, 2] - cz
        sz1 = S[:, 5] - cz
        live = (np.maximum(sz0, sz1) > NEAR) & (np.minimum(sz0, sz1) < FAR)
        S = S[live]
        sz0, sz1 = sz0[live], sz1[live]
        sx0 = S[:, 0] - cam_x
        sx1 = S[:, 3] - cam_x
        sy0 = cam_y - S[:, 1]
        sy1 = cam_y - S[:, 4]
        # Clip against the near plane in world z before dividing by it.
        dzs = sz1 - sz0
        cross = (NEAR - sz0) / np.where(np.abs(dzs) < 1e-6,
                                             1e-6, dzs)
        cross = np.minimum(np.maximum(cross, 0.0), 1.0)
        ta = np.where(sz0 < NEAR, cross, 0.0)
        tb = np.where(sz1 < NEAR, cross, 1.0)
        za = sz0 + dzs * ta
        zb = sz0 + dzs * tb
        ia = focal / np.maximum(za, NEAR)
        ib = focal / np.maximum(zb, NEAR)
        pax = cx_s + (sx0 + (sx1 - sx0) * ta) * ia
        pbx = cx_s + (sx0 + (sx1 - sx0) * tb) * ib
        pay = cy_s + (sy0 + (sy1 - sy0) * ta) * ia
        pby = cy_s + (sy0 + (sy1 - sy0) * tb) * ib
        pay += shear * (pax - cx_s)
        pby += shear * (pbx - cx_s)
        zm = 0.5 * (za + zb)
        lfog = (fog_k / (fog_k + zm)
                * np.minimum((FAR - zm) * (1.0 / FADE), 1.0))[:, None]
        draw_lines(flat, W, H, pax, pay, pbx, pby, pack(S[:, 6:9] * lfog),
                   scr, fscr, lim)

        # ---- boxes ----
        j0 = bisect(bkey, -(cz + FAR + SLACK))
        j1 = bisect(bkey, -(cz - SLACK))
        dz0 = bz0_t[j0:j1] - cz
        dz1 = bz1_t[j0:j1] - cz
        vis = (dz1 > NEAR) & (dz0 < FAR)
        dz0, dz1 = dz0[vis], dz1[vis]
        ax0 = bx0_t[j0:j1][vis] - cam_x
        ax1 = bx1_t[j0:j1][vis] - cam_x
        ay0 = cam_y - by0_t[j0:j1][vis]
        ay1 = cam_y - by1_t[j0:j1][vis]

        dz0c = np.maximum(dz0, NEAR)
        i0 = focal / dz0c
        i1 = focal / dz1
        zm = 0.5 * (dz0c + dz1)
        fog = (fog_k / (fog_k + zm)
               * np.minimum((FAR - zm) * (1.0 / FADE), 1.0)
               )[:, None, None, None]

        # Front, top and side. Each is emitted for every box and then masked,
        # which costs a little arithmetic on invisible faces and saves the
        # branching that would otherwise land in Python.
        x0i0 = cx_s + ax0 * i0
        x1i0 = cx_s + ax1 * i0
        x0i1 = cx_s + ax0 * i1
        x1i1 = cx_s + ax1 * i1
        y0i0 = cy_s + ay0 * i0
        y1i0 = cy_s + ay1 * i0
        y0i1 = cy_s + ay0 * i1
        y1i1 = cy_s + ay1 * i1
        axs = np.where(ax1 > 0.0, ax1, ax0)
        xsi0 = cx_s + axs * i0
        xsi1 = cx_s + axs * i1

        n = dz0.shape[0]
        xv = np.empty((4, 3 * n), f32)
        yv = np.empty((4, 3 * n), f32)
        xv[0, 0::3], xv[1, 0::3] = x0i0, x1i0
        xv[2, 0::3], xv[3, 0::3] = x1i0, x0i0
        yv[0, 0::3], yv[1, 0::3] = y1i0, y1i0
        yv[2, 0::3], yv[3, 0::3] = y0i0, y0i0
        xv[0, 1::3], xv[1, 1::3] = x0i0, x1i0
        xv[2, 1::3], xv[3, 1::3] = x1i1, x0i1
        yv[0, 1::3], yv[1, 1::3] = y1i0, y1i0
        yv[2, 1::3], yv[3, 1::3] = y1i1, y1i1
        xv[0, 2::3], xv[1, 2::3] = xsi0, xsi1
        xv[2, 2::3], xv[3, 2::3] = xsi1, xsi0
        yv[0, 2::3], yv[1, 2::3] = y1i0, y1i1
        yv[2, 2::3], yv[3, 2::3] = y0i1, y0i0
        yv += shear * (xv - cx_s)

        shown = np.empty(3 * n, bool)
        shown[0::3] = dz0 > NEAR                 # front face not behind us
        shown[1::3] = ay1 > 0.0                  # looking down on the top
        shown[2::3] = (ax1 > 0.0) | (ax0 < 0.0)  # a side is turned to us

        qcol = pack(bcol_t[j0:j1][vis] * fog)
        fill_quads(flat, W, H, xv[:, shown], yv[:, shown],
                   qcol[:, 0].reshape(-1)[shown], qcol[:, 1].reshape(-1)[shown],
                   scr)

        buf[:] = rgba[:, :, :3]

        # ---- name tag over the directory being entered ----
        if args.labels:
            nz = (1.0 - frac) * STRIDE
            if nz > NEAR:
                inv = focal / nz
                tx = cx_s + (lat(k + 1) - cam_x) * inv
                ty = cy_s + (cam_y - 30.0) * inv
                ty += shear * (tx - cx_s)
                rr, cc, tw = name_tags[(k + 1) % levels]
                # Pinned inside the panel rather than allowed to sail off it:
                # a name half cut off by the edge reads as a bug.
                x = min(max(int(tx - tw * 0.5), 0), W - tw)
                y = int(min(max(ty - 5.0, 1.0), H - 12.0))
                buf[rr + y, cc + x] = label_col

        if args.caption:
            phase = (t / total) % 1.0
            if 0.10 < phase < 0.26:
                blit(buf, cap_a, (W - cap_a.shape[1]) // 2, H - 14, label_col)
            elif 0.28 < phase < 0.42:
                blit(buf, cap_b, (W - cap_b.shape[1]) // 2, H - 14, label_col)

        return buf

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
