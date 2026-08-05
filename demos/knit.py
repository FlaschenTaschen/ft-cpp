#!/usr/bin/env python3
"""An Aran sweater being knitted, one stitch at a time.

A knitting chart is already a pixel grid, so the whole demo is that
correspondence taken literally: every stitch is a small sprite -- five by five
at scale 1 -- authored in the source as rows of characters over a five level
shading ramp, and the fabric is those sprites blitted into a chart.

The chart is a classic Aran trellis, generated rather than drawn. Two families
of two stitch cables travel diagonally, one right and one left, at a stitch a
row; where a pair meets they cross, and the rhombi between the crossings are
filled alternately with seed stitch and reverse stockinette, which is what
gives the diamond lattice. Everything follows from --diamond, the width of a
diamond in stitches: the ropes gain two stitches at each crossing, so crossings
land T = P/2 - 2 rows apart and the chart repeats every Q = 2T rows. Those Q
row images are baked once in build().

What the demo is actually about is the *working row*. It advances one stitch at
a time across the fabric at a few stitches a second, each stitch popping into
existence as it is worked, with the needles and the working yarn at the live
stitch. At the end of the row the work turns, the fabric hangs a row further
down, and the next row is worked back the other way -- knitting goes back and
forth, and the alternation is the strongest tell that this is being made rather
than scrolled.

Cable crossings are worked, not stamped. When the row reaches one the cursor
stops and the four stitches are crossed the way a knitter does it: the pair
that will lie in front is lifted clear of the fabric onto an imaginary cable
needle, the other pair slides across through the hole it left, and then the
held pair drops back into the columns the other one vacated. Both ropes end up
skewed two stitches sideways over a single row, the front one drawn last with a
dark halo and a pixel of overhang onto the row below. The lift is what makes it
readable: a straight horizontal slide of ten pixels is a smudge, but stitches
leaving the fabric and coming back cannot be mistaken for anything else. At the
end of the animation the drawing is committed to the fabric exactly as it
stands, so nothing pops.

The panel starts with a swatch already on the needles -- a row takes several
seconds to work, and bare needles would mean a minute of nearly empty panel
before the lattice appeared. Everything below the working row is history, so
build() simply paints it.

Run:  python3 knit.py --host 127.0.0.1
      python3 knit.py --diamond 16 --motif bobble --colour indigo
      python3 knit.py --stitch-rate 25 --colour heather --diamond 10
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The stitches, as data.
# --------------------------------------------------------------------------

# Five shades, valley to highlight. Undyed wool has almost no hue range to work
# with, so the whole sprite vocabulary is carried by these five steps -- deep
# shadow between stitches up to the lit crown of the yarn.
LEVELS = {" ": 0, ".": 1, "-": 2, "+": 3, "#": 4}

# Knit: the interlocking V of stockinette. Two legs run from the top corners
# down to the middle of the bottom edge, where the point tucks into the gap
# between the legs of the stitch below -- which is why the top centre is dark
# and the bottom centre is not.
KNIT = (
    "#-.-#",
    "#-.-#",
    "-#.#-",
    ".-#-.",
    "..#..",
)

# Purl: the wrong side of the same stitch, a horizontal bump with a trough
# under it. Wider than tall on purpose; a round blob reads as a bead.
PURL = (
    ".....",
    "-+#+-",
    "####+",
    ".-+-.",
    " ... ",
)

# A bobble, worked over two stitches: five stitches into one and back again,
# which on the fabric is a fat raised nub.
BOBBLE = (
    "..-++++-..",
    ".-+####+-.",
    "-+##+###+-",
    ".-+####+-.",
    "..-++++-..",
)

# Undyed cream first, since that is what Aran is; the rest are dyed yarns.
# Each is five steps, darkest to lightest.
COLOURWAYS = {
    "cream":   ((16, 14, 11), (56, 48, 34), (126, 110, 80), (200, 182, 142),
                (247, 239, 216)),
    "oatmeal": ((14, 13, 12), (52, 47, 42), (116, 106, 94), (182, 170, 152),
                (236, 228, 212)),
    "heather": ((10, 12, 16), (44, 50, 60), (98, 110, 126), (158, 172, 190),
                (222, 232, 244)),
    "indigo":  ((6, 8, 18), (24, 32, 66), (52, 72, 132), (96, 126, 196),
                (176, 202, 250)),
    "moss":    ((10, 14, 8), (40, 54, 30), (86, 112, 60), (140, 172, 100),
                (206, 232, 168)),
    "berry":   ((16, 6, 12), (62, 20, 38), (128, 44, 74), (192, 88, 118),
                (244, 168, 186)),
}

NEEDLE = (150, 154, 166)
NEEDLE_LIT = (226, 232, 244)


def char_index(art):
    """(rows of chars) -> (h, w) uint8 of shade levels."""
    return np.array([[LEVELS[c] for c in row] for row in art], np.uint8)


def rasterize(art, lut, scale):
    """(rows of chars) -> (h*scale, w*scale, 3) uint8 through a shade ramp."""
    rgb = lut[char_index(art)]
    if scale > 1:
        rgb = np.repeat(np.repeat(rgb, scale, 0), scale, 1)
    return rgb


def rope_unit(lut, scale):
    """The two stitch cable rope, as one sprite.

    Two knit stitches side by side, with the shade ramp pushed up in the middle
    and down at the outer columns. That cross section is what makes a rope look
    round and raised rather than like two stitches that happen to be adjacent,
    and it costs nothing at draw time because the whole rope is one blit per
    pixel row.
    """
    idx = char_index(KNIT)
    unit = np.concatenate([idx, idx], axis=1).astype(np.int16)
    bulge = np.array([-1, 0, 0, 1, 1, 1, 1, 0, 0, -1], np.int16)
    rgb = lut[np.clip(unit + bulge[None, :], 0, 4)]
    if scale > 1:
        rgb = np.repeat(np.repeat(rgb, scale, 0), scale, 1)
    return rgb


# --------------------------------------------------------------------------
# Drawing.
# --------------------------------------------------------------------------

def draw_bar(dst, y, x, unit, dx_top, overhang=0, halo=None):
    """A cable rope crossing one row of the chart, skewed by dx_top pixels.

    The bar's bottom edge sits at x and its top edge at x + dx_top, so a rope
    that travels a stitch a row is drawn with dx_top = one stitch and joins the
    row below it seamlessly. A crossing is the same call with twice the skew.

    overhang draws extra pixel rows past the bottom, onto the row worked before
    this one; halo lays a shadow around the whole thing first. Together they are
    the entire "this strand is in front" cue.
    """
    h, uw = unit.shape[:2]
    dh, dw = dst.shape[:2]
    rows = range(-1 if halo is not None else 0,
                 h + overhang + (1 if halo is not None else 0))
    for j in rows:
        jj = min(max(j, 0), h - 1)
        yy = y + j
        if not (0 <= yy < dh):
            continue
        x0 = x + int(round(dx_top * (h - 1 - jj) / float(h - 1)))
        if halo is not None:
            a, b = max(0, x0 - 1), min(dw, x0 + uw + 1)
            if a < b:
                dst[yy, a:b] = halo
        if 0 <= j < h + overhang:
            a, b = max(0, x0), min(dw, x0 + uw)
            if a < b:
                dst[yy, a:b] = unit[jj, a - x0:b - x0]


def draw_cross(dst, y, x, k, right_over, unit, under, shadow, sw, sh):
    """One cable crossing, k of the way through being worked.

    Two ropes, four stitches wide in total, exchanging places. This is the
    cable needle move as a knitter does it: the pair that will cross in front
    is lifted clear of the row, the other pair slides across underneath it, and
    then it is dropped back into the columns the other pair left. The lift is
    what sells it -- a purely horizontal slide at five pixels a stitch reads as
    a smudge, whereas stitches leaving the fabric and coming back cannot be
    mistaken for anything else.

    At k = 0 both ropes hang straight down from the row below; at k = 1 the
    lift is back to zero and the drawing is exactly what gets committed to the
    fabric, so nothing moves when the animation ends.
    """
    # Trapezoid: up over the first third, held, down over the last third.
    lift = int(round(0.6 * sh * min(1.0, k / 0.3, (1.0 - k) / 0.3)))
    d = 2 * sw * k
    over_dx, under_dx = (d, -d) if right_over else (-d, d)
    over_x, under_x = (x, x + 2 * sw) if right_over else (x + 2 * sw, x)
    if lift > 0:
        # The hole the held stitches came out of, for the other pair to slide
        # through -- so it goes down before the under rope, not after it.
        a, b = max(0, over_x), min(dst.shape[1], over_x + 2 * sw)
        if a < b:
            dst[y:y + sh, a:b] = shadow
    draw_bar(dst, y, under_x, under, under_dx)
    draw_bar(dst, y - lift, over_x, unit, over_dx, overhang=1, halo=shadow)


def draw_strand(out, x0, y0, x1, y1, sag, colour, dim):
    """The working yarn, running off the edge of the panel with a droop."""
    w = out.shape[1]
    h = out.shape[0]
    span = float(x1) - float(x0)
    if abs(span) < 2.0:
        return
    a, b = (x0, x1) if x0 <= x1 else (x1, x0)
    a, b = max(0, int(a)), min(w - 1, int(b))
    if b <= a:
        return
    xs = np.arange(a, b + 1)
    u = (xs - x0) / span
    ys = y0 + (y1 - y0) * u + sag * np.sin(np.pi * np.clip(u, 0, 1))
    ys = np.clip(ys.astype(np.int32), 0, h - 2)
    out[ys, xs] = colour
    out[ys + 1, xs] = dim


def add_arguments(ap):
    ap.add_argument("--stitch-rate", type=float, default=14.0,
                    help="stitches worked per second")
    ap.add_argument("--scale", type=int, default=1,
                    help="stitch magnification; 1 is a 5x5 px stitch")
    ap.add_argument("--diamond", type=int, default=12,
                    help="diamond width in stitches (even, >=10); the height "
                         "follows as width-4 rows")
    ap.add_argument("--colour", "--color", default="cream",
                    choices=sorted(COLOURWAYS), help="yarn")
    ap.add_argument("--motif", default="seed", choices=("seed", "bobble", "plain"),
                    help="what fills the diamonds")
    ap.add_argument("--cross-time", type=float, default=0.62,
                    help="seconds a cable crossing takes to work")
    ap.add_argument("--turn-time", type=float, default=0.45,
                    help="seconds to turn the work at the end of a row")
    ap.add_argument("--no-needles", action="store_true",
                    help="fabric only, no needles or yarn")
    ap.add_argument("--seed", type=int, default=3,
                    help="rng for the hand-knitter timing jitter")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)

    # --- stitch geometry ---------------------------------------------------
    scale = max(1, int(args.scale))
    while scale > 1 and 5 * scale * 8 > H:      # keep at least eight rows of fabric
        scale -= 1
    sw = sh = 5 * scale
    ncols = max(6, (W + sw - 1) // sw)
    wf = ncols * sw

    lut = np.array(COLOURWAYS[args.colour], np.uint8)
    # Reverse stockinette is the recessed ground the cables sit on, so it is
    # the same sprites through a darker ramp rather than a different sprite.
    lut_bg = (lut.astype(f32) * 0.50).astype(np.uint8)
    # The cables have to be the brightest thing on the fabric or the lattice
    # stops reading as raised, so the diamond fill is knocked back a little and
    # the ground a lot.
    lut_fill = (lut.astype(f32) * 0.86).astype(np.uint8)
    knit = rasterize(KNIT, lut_fill, scale)
    purl = rasterize(PURL, lut_fill, scale)
    purl_bg = rasterize(PURL, lut_bg, scale)
    bobble = rasterize(BOBBLE, lut, scale)
    unit = rope_unit(lut, scale)
    under = (unit.astype(f32) * 0.66).astype(np.uint8)
    shadow = tuple(int(v) for v in (lut[0].astype(f32) * 0.5))
    yarn = tuple(int(v) for v in lut[3])
    yarn_dim = tuple(int(v) for v in lut[1])

    # --- the chart ---------------------------------------------------------
    # P columns to a diamond; the ropes travel a stitch a row and gain two more
    # at every crossing, so crossings are T rows apart and the chart repeats
    # every Q = 2T rows. Shrink the diamond rather than run one taller than the
    # panel, which would look like plain diagonal stripes.
    p = max(10, int(args.diamond) + (int(args.diamond) & 1))
    while p > 10 and (p - 4) * sh > H:
        p -= 2
    t_cross = p // 2 - 2
    q = 2 * t_cross
    ropes = range(-2, ncols // p + 3)

    rows = []
    xcol = np.arange(ncols)
    for r in range(q):
        m, s = divmod(r, t_cross)
        # Left edge of right travelling rope i is i*P + c_a; of left travelling
        # rope j is j*P + c_b. On a crossing row (s == 0) these are the
        # positions the pair has *before* it crosses.
        if s == 0:
            c_a, c_b = m * (p // 2), 2 - m * (p // 2)
        else:
            c_a, c_b = m * (p // 2) + 2 + s, -m * (p // 2) - s

        # Which rhombus of the lattice a stitch falls in: count the ropes of
        # each family to its left. Adjacent rhombi differ by one crossing, so
        # the sum's parity is a checkerboard over the lattice -- seed filled
        # diamonds separated by reverse stockinette ones.
        par = (np.floor_divide(xcol - c_a, p) + np.floor_divide(xcol - c_b, p)) & 1
        seed_knit = ((r + xcol) & 1) == 0

        img = np.zeros((sh, wf, 3), np.uint8)
        for x in range(ncols):
            if par[x] or args.motif == "plain":
                spr = purl_bg
            else:
                spr = knit if seed_knit[x] else purl
            img[:, x * sw:(x + 1) * sw] = spr

        if args.motif == "bobble" and r == 0:
            for i in ropes:                     # one at the heart of each diamond
                bx = (i * p + p // 2 + 1) * sw
                a, b = max(0, bx), min(wf, bx + 2 * sw)
                if a < b:
                    img[:, a:b] = bobble[:, a - bx:b - bx]

        crosses = []
        for i in ropes:
            if s == 0:
                c = i * p + m * (p // 2)
                if c + 4 > 0 and c < ncols:
                    # Weave: alternate which rope is in front, along the row and
                    # up the fabric, or every crossing tilts the same way and
                    # the lattice stops looking woven.
                    crosses.append((c, (m + i) % 2 == 0))
            else:
                draw_bar(img, 0, (i * p + c_a) * sw, unit, sw)
                draw_bar(img, 0, (i * p + c_b) * sw, unit, -sw)
        crosses.sort()
        rows.append((img, crosses))

    # --- the fabric --------------------------------------------------------
    # Knitting hangs off the needles, so the working row is near the top and
    # everything already worked is below it, drifting off the bottom. The
    # buffer is scrolled by a whole stitch when a row finishes; the smooth part
    # of that scroll is done by moving the viewport, then the two cancel.
    # Panel rows above the working row, for the needles and the yarn. A short
    # panel cannot spare a whole stitch of slack for them.
    head = sh + (4 if H >= 48 else 2) * scale
    view0 = 2 * sh
    y_work = view0 + head                 # working row, in fabric coordinates
    y_screen = head                       # ... and on the panel
    hf = y_work + H + sh
    fab = np.zeros((hf, wf, 3), np.uint8)
    out = np.zeros((H, W, 3), np.uint8)
    nb = 2 * scale                        # needle thickness
    tip = 3 * scale                       # length of its taper

    # A swatch already on the needles at frame zero. A row takes a few seconds
    # to work, so starting from bare needles would mean a minute of near empty
    # panel before the lattice appeared -- the fabric below the working row is
    # history, so it can simply be painted.
    def paint_row(y, chart_row):
        img, crosses = rows[chart_row % q]
        fab[y:y + sh] = img
        for c, over in crosses:
            draw_cross(fab, y, c * sw, 1.0, over, unit, under, shadow, sw, sh)

    start_row = 0
    for i in range((hf - y_work) // sh - 1, 0, -1):
        if y_work + (i + 1) * sh <= hf:
            paint_row(y_work + i * sh, start_row - i)

    state = {
        "row": 0, "dir": 1, "worked": 0, "acc": 0.0, "cost": 1.0,
        "phase": "knit", "clock": 0.0, "last_t": None,
        "cross": None, "flash": 0.0, "flash_x": 0,
    }

    def stitch_cost():
        """Hands are not a metronome: jitter, plus the odd hesitation."""
        c = float(rng.uniform(0.8, 1.25))
        if rng.random() < 0.03:
            c += float(rng.uniform(1.0, 2.5))
        return c

    def commit(x0, x1):
        """Copy a span of the working row's baked image into the fabric."""
        a, b = max(0, x0 * sw), min(wf, x1 * sw)
        if a < b:
            img = rows[state["row"] % q][0]
            fab[y_work:y_work + sh, a:b] = img[:, a:b]

    def cross_at(col):
        """The crossing this stitch begins, if any, as (left column, over)."""
        for c, over in rows[state["row"] % q][1]:
            if (state["dir"] > 0 and col == c) or (state["dir"] < 0 and col == c + 3):
                return c, over
        return None

    def turn():
        """Row finished: hang the fabric a stitch lower and work back."""
        fab[y_work + sh:] = fab[y_work:hf - sh].copy()
        fab[:y_work + sh] = 0
        state["row"] += 1
        state["dir"] = -state["dir"]
        state["worked"] = 0
        state["acc"] = 0.0

    def render(t, frame):
        last = state["last_t"]
        dt = 0.0 if last is None else min(0.1, max(0.0, t - last))
        state["last_t"] = t

        # --- work the row --------------------------------------------------
        if state["phase"] == "knit":
            state["acc"] += dt * max(0.5, args.stitch_rate)
            while state["acc"] >= state["cost"]:
                if state["worked"] >= ncols:
                    state["phase"] = "turn"
                    state["clock"] = 0.0
                    break
                col = (state["worked"] if state["dir"] > 0
                       else ncols - 1 - state["worked"])
                hit = cross_at(col)
                if hit is not None:
                    # Lay down the ground the crossing sits on, then hand the
                    # four stitches to the crossing animation.
                    commit(hit[0], hit[0] + 4)
                    state["cross"] = hit
                    state["phase"] = "cross"
                    state["clock"] = 0.0
                    break
                commit(col, col + 1)
                state["acc"] -= state["cost"]
                state["cost"] = stitch_cost()
                state["worked"] += 1
                state["flash"] = 1.0
                state["flash_x"] = col
        elif state["phase"] == "cross":
            state["clock"] += dt
            if state["clock"] >= args.cross_time:
                c, over = state["cross"]
                draw_cross(fab, y_work, c * sw, 1.0, over, unit, under,
                           shadow, sw, sh)
                state["worked"] += 4
                state["acc"] = 0.0
                state["cost"] = stitch_cost()
                state["flash"] = 1.0
                state["flash_x"] = c + (3 if state["dir"] > 0 else 0)
                state["phase"] = "turn" if state["worked"] >= ncols else "knit"
                state["clock"] = 0.0
                state["cross"] = None
        elif state["phase"] == "turn":
            state["clock"] += dt
            if state["clock"] >= args.turn_time:
                turn()
                state["phase"] = "knit"
                state["clock"] = 0.0

        # --- the fabric, scrolled -------------------------------------------
        if state["phase"] == "turn":
            k = min(1.0, state["clock"] / max(1e-3, args.turn_time))
            k = k * k * (3.0 - 2.0 * k)
            slide = int(round(k * sh))
        else:
            slide = 0
        top = view0 - slide
        np.copyto(out, fab[top:top + H, :W])

        if state["phase"] == "cross":
            k = min(1.0, state["clock"] / max(1e-3, args.cross_time))
            k = k * k * (3.0 - 2.0 * k)
            c, over = state["cross"]
            draw_cross(out, y_screen, c * sw, k, over, unit, under,
                       shadow, sw, sh)

        # --- needles and working yarn ---------------------------------------
        state["flash"] = max(0.0, state["flash"] - dt * 6.0)
        if args.no_needles:
            return out

        if state["phase"] == "turn":
            # The work turns over: the yarn swings across to the far edge and
            # the cursor with it.
            k = min(1.0, state["clock"] / max(1e-3, args.turn_time))
            k = k * k * (3.0 - 2.0 * k)
            cx = float(W - 1) if state["dir"] > 0 else 0.0
            done = W
        else:
            col = (min(ncols - 1, state["worked"]) if state["dir"] > 0
                   else max(0, ncols - 1 - state["worked"]))
            if state["phase"] == "cross":
                col = state["cross"][0] + (1 if state["dir"] > 0 else 2)
            cx = (col + 0.5) * sw
            done = state["worked"]
        cx = float(np.clip(cx, 0, W - 1))
        ix = int(cx)

        # Two needles whose tips meet at the live stitch: the near one carries
        # the stitches already worked, the far one those still to come. They
        # sit at different heights so the tips cross rather than collide.
        def needle(x0, x1, y, point_right):
            for k in range(nb):
                cut = (k * tip) // nb
                a, b = (x0, x1 - cut) if point_right else (x0 + cut, x1)
                a, b = max(0, int(a)), min(W, int(b))
                if a < b:
                    out[y + k, a:b] = NEEDLE_LIT if k == 0 else NEEDLE

        ny = 4 * scale
        if state["phase"] == "turn":
            worked = (0, W)
            needle(0, W, ny, state["dir"] > 0)
        elif state["dir"] > 0:
            worked = (0, ix + tip)
            needle(worked[0], worked[1], ny, True)        # worked, behind
            needle(ix - tip, W, 0, False)                 # to work, ahead
        else:
            worked = (ix - tip, W)
            needle(worked[0], worked[1], ny, False)
            needle(0, ix + tip, 0, True)

        # Live loops: every stitch on the working needle is still a loop of
        # yarn, so the fabric hangs off the needle by a picket of them rather
        # than floating under it.
        a, b = max(0, worked[0]), min(W, worked[1])
        if a < b and ny + nb < y_screen:
            out[ny + nb:y_screen, a - a % sw:b:sw] = yarn_dim

        # The strand feeds in over the far needle, with a droop that breathes;
        # a straight line reads as a scratch on the panel rather than as yarn.
        anchor = float(W - 1 if state["dir"] > 0 else 0)
        if state["phase"] == "turn":
            # Turning takes the yarn over to the other side of the work.
            k = min(1.0, state["clock"] / max(1e-3, args.turn_time))
            anchor += (W - 1 - 2 * anchor) * (k * k * (3.0 - 2.0 * k))
        sag = min(4.0 * scale, 0.035 * abs(anchor - cx)) + 1.2 * scale * np.sin(t * 7.0)
        draw_strand(out, cx, y_screen - 1, anchor, 1.0 * scale, sag, yarn, yarn_dim)

        # The loop on the needle at the live stitch, then a glow on the stitch
        # just made, decaying over a fifth of a second.
        lx0, lx1 = max(0, ix - 2 * scale), min(W, ix + 2 * scale + 1)
        ly = 4 * scale + nb
        if lx0 < lx1 and ly < y_screen:
            out[ly:y_screen, lx0:lx0 + scale] = yarn
            out[ly:y_screen, lx1 - scale:lx1] = yarn
            out[y_screen - scale:y_screen, lx0:lx1] = yarn
        if state["flash"] > 0.02 and done:
            fx = state["flash_x"] * sw
            a, b = max(0, fx), min(W, fx + sw)
            if a < b:
                band = out[y_screen:y_screen + sh, a:b].astype(f32)
                band *= 1.0 + 0.55 * state["flash"]
                out[y_screen:y_screen + sh, a:b] = np.clip(band, 0, 255)
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
