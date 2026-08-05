#!/usr/bin/env python3
"""Dali clock.

HH:MM:SS where each digit *melts* into the next, after the old `dclock`
screensaver. The digits are generated here as seven-segment shapes -- no font
file to go missing -- and every one is turned into a signed distance field at
startup: negative inside the glyph, positive outside, zero on its outline.

The morph is a deformation, not a crossfade. Interpolating the two distance
fields and re-thresholding at zero moves the *outline* from the old shape to
the new one, so at the halfway point you see a single solid figure caught
between two digits: strokes stretch, bend and pinch off. A crossfade would
instead show both digits at once at half brightness, which looks nothing like
this. Only the digits that actually change are interpolated.

The animation is driven from the fractional part of the wall clock, not from
the demo's own elapsed time, so it stays locked to the second no matter what
the frame rate does.

Run:  python3 daliclock.py --host 127.0.0.1
      python3 daliclock.py --palette green --12h --morph 0.5
"""

import sys
import time

import numpy as np

import demoscene as ds

f32 = ds.f32

# Which of the seven segments each digit lights.
#
#      aaa
#     f   b
#      ggg
#     e   c
#      ddd
SEGMENTS = {
    "0": "abcdef", "1": "bc",    "2": "abdeg",  "3": "abcdg", "4": "bcfg",
    "5": "acdfg",  "6": "acdefg", "7": "abc",   "8": "abcdefg", "9": "abcdfg",
}

# A digit cell, in units of the big digit's width. Seconds are drawn smaller
# than hours and minutes, which is the usual treatment and buys the big digits
# room on a panel only 64 rows tall.
ASPECT = 0.66      # digit width / digit height
SMALL = 0.62       # seconds size, relative to hours and minutes
GAP = 0.14         # between the two digits of a group
CGAP = 0.18        # either side of a colon
CW = 0.24          # colon width

# hue, saturation. Solid colour reads far better on a wall than a busy ramp.
COLOURS = {
    "amber":   (0.085, 1.00),
    "red":     (0.000, 1.00),
    "green":   (0.330, 1.00),
    "cyan":    (0.500, 0.90),
    "blue":    (0.620, 0.85),
    "magenta": (0.870, 0.85),
    "white":   (0.000, 0.00),
}


def add_arguments(ap):
    ap.add_argument("--palette", default="amber",
                    choices=sorted(COLOURS) + ["rainbow"], help="digit colour")
    ap.add_argument("--drift", type=float, default=0.0,
                    help="hue drift, turns per minute")
    ap.add_argument("--morph", type=float, default=0.45,
                    help="seconds a digit takes to melt into the next")
    ap.add_argument("--no-morph", dest="no_morph", action="store_true",
                    help="switch digits instantly, for comparison")
    ap.add_argument("--12h", dest="ampm", action="store_true",
                    help="12 hour clock, leading zero blanked")
    ap.add_argument("--glow", type=float, default=0.30,
                    help="halo around the strokes, 0 disables")
    ap.add_argument("--blink", action="store_true",
                    help="pulse the colons once a second")
    ap.add_argument("--fill", type=float, default=0.84,
                    help="big digit height as a fraction of the panel")


# --------------------------------------------------------------------------
# Digit shapes.
# --------------------------------------------------------------------------

def seven_segment(w, h, t):
    """Boolean masks for the seven segments of a w x h digit, stroke t wide.

    Each segment is a hexagon: full length along its middle and tapering to a
    point at both ends, so the corners of a digit meet the way a real LED
    display's do -- and, more to the point here, so the outline has no
    right-angle notches for the distance field to snag on.
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(f32)
    half = t / 2.0

    def hbar(cy, x0, x1):
        dy = np.abs(yy - cy)
        return (dy <= half) & (xx >= x0 + dy) & (xx <= x1 - dy)

    def vbar(cx, y0, y1):
        dx = np.abs(xx - cx)
        return (dx <= half) & (yy >= y0 + dx) & (yy <= y1 - dx)

    mid = (h - 1) / 2.0
    return {
        "a": hbar(half, 0, w - 1),
        "g": hbar(mid, 0, w - 1),
        "d": hbar(h - 1 - half, 0, w - 1),
        "f": vbar(half, 0, mid),
        "b": vbar(w - 1 - half, 0, mid),
        "e": vbar(half, mid, h - 1),
        "c": vbar(w - 1 - half, mid, h - 1),
    }


def digit_masks(w, h, t, pad):
    """The ten digits as boolean masks, inset into a padded cell.

    The padding matters: a melting digit bulges past where either of its
    endpoints reached, and without room around it the bulge would be clipped
    square against the cell edge.
    """
    segs = seven_segment(w, h, t)
    out = {}
    for ch, names in SEGMENTS.items():
        m = np.zeros((h, w), bool)
        for n in names:
            m |= segs[n]
        cell = np.zeros((h + 2 * pad, w + 2 * pad), bool)
        cell[pad:pad + h, pad:pad + w] = m
        out[ch] = cell
    return out


def colon_mask(w, h, t, pad):
    """Two dots, at a third and two thirds of the digit height."""
    yy, xx = np.mgrid[0:h, 0:w].astype(f32)
    r = max(w, t) / 2.0
    cx = (w - 1) / 2.0
    m = np.zeros((h, w), bool)
    for cy in ((h - 1) / 3.0, (h - 1) * 2.0 / 3.0):
        m |= (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    cell = np.zeros((h + 2 * pad, w + 2 * pad), bool)
    cell[pad:pad + h, pad:pad + w] = m
    return cell


# --------------------------------------------------------------------------
# Signed distance fields.
# --------------------------------------------------------------------------

def _dist_to(shape, pts):
    """Euclidean distance from every pixel of `shape` to the nearest of pts."""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(f32)
    best = np.full((h, w), f32(1e6), f32)
    # In chunks, so a big glyph never materializes an (H*W, N) array.
    for i in range(0, len(pts), 64):
        py = pts[i:i + 64, 0][:, None, None]
        px = pts[i:i + 64, 1][:, None, None]
        d = (yy - py) ** 2 + (xx - px) ** 2
        np.minimum(best, d.min(axis=0), out=best)
    return np.sqrt(best)


def _rim(mask):
    """The pixels of `mask` that touch a pixel outside it."""
    inner = mask.copy()
    for shift in (1, -1):
        inner &= np.roll(mask, shift, 0)
        inner &= np.roll(mask, shift, 1)
    inner[0] = inner[-1] = False
    inner[:, 0] = inner[:, -1] = False
    return mask & ~inner


def signed_distance(mask):
    """Signed distance to the outline of `mask`: negative inside, positive out.

    Brute force against the outline pixels only -- the nearest set pixel to
    anywhere is always one of them -- which keeps this cheap enough to run for
    every digit at startup, no scipy needed.
    """
    on = np.argwhere(_rim(mask)).astype(f32)
    off = np.argwhere(_rim(~mask)).astype(f32)
    d_out = _dist_to(mask.shape, on) - 0.5
    d_in = _dist_to(mask.shape, off) - 0.5
    return np.where(mask, -d_in, d_out).astype(f32)


def field_table(w, h, t, pad):
    """char -> signed distance field, for the ten digits and a blank.

    A blank has no outline to measure from, so it gets a small positive
    constant instead. Interpolated against a digit that is at most t/2 deep,
    the zero crossing appears part way through the melt and then swells
    outward -- the digit grows out of the middle of its own strokes rather
    than popping in whole.
    """
    tab = {c: signed_distance(m) for c, m in digit_masks(w, h, t, pad).items()}
    tab[" "] = np.full((h + 2 * pad, w + 2 * pad), f32(max(2.0, t * 0.5)), f32)
    return tab


# --------------------------------------------------------------------------

def build(args):

    W, H = args.width, args.height

    # Size the digits to whichever of height and width runs out first. The
    # panel is 320x64, so it is nearly always height -- but --width 128 or a
    # squarer canvas has to keep the eight glyphs on screen too.
    units = 4 + 2 * SMALL + 2 * CW + 3 * GAP + 4 * CGAP
    bh = max(7, int(round(H * args.fill)))
    bw = max(3, int(round(min(bh * ASPECT, W * 0.94 / units))))
    sh, sw = max(5, int(round(bh * SMALL))), max(3, int(round(bw * SMALL)))
    cw = max(1, int(round(bw * CW)))
    gap = max(1, int(round(bw * GAP)))
    cgap = max(1, int(round(bw * CGAP)))

    bt = max(2, int(round(bh * 0.155)))
    st = max(2, int(round(sh * 0.170)))
    # The cell has to hold the halo as well as the glyph, or the halo ends in
    # a hard rectangle where the field runs out. Cells overlapping each other
    # is harmless -- they are combined with a maximum, not added.
    glow_r = f32(max(1.5, bh * 0.06))
    pad = max(3, int(round(bt * 0.6)), int(round(4 * glow_r)))

    big = field_table(bw, bh, bt, pad)
    small = field_table(sw, sh, st, pad)
    colon = signed_distance(colon_mask(cw, bh, bt, pad))

    # Everything sits on one baseline, so the small seconds hang off the
    # bottom of the big digits rather than floating in the middle.
    top = (H - bh) // 2
    base = top + bh

    slots = []          # (x, y, field table) for the six digits
    colons = []         # (x, y)
    x = (W - (4 * bw + 2 * sw + 2 * cw + 3 * gap + 4 * cgap)) // 2
    for group, (tab, gw, gh) in enumerate(
            [(big, bw, bh), (big, bw, bh), (small, sw, sh)]):
        if group:
            x += cgap
            colons.append((x - pad, top - pad))
            x += cw + cgap
        for i in range(2):
            slots.append((x - pad, base - gh - pad, tab))
            x += gw + (gap if i == 0 else 0)

    # Colour. One hue for the whole clock, shaded a little darker down the
    # glyph; --palette rainbow spreads the hue across the panel instead.
    hue0, sat = COLOURS.get(args.palette, (0.0, 0.9))
    grade = np.linspace(1.0, 0.76, H, dtype=f32)[:, None] * np.ones(W, f32)
    xhue = np.linspace(0.0, 0.55, W, dtype=f32)[None, :] * np.ones((H, 1), f32)

    def colour(t):
        h = xhue if args.palette == "rainbow" else np.zeros((H, W), f32)
        h = h + f32(hue0 + args.drift * t / 60.0)
        return np.clip(ds.hsv_to_rgb(h, f32(sat), grade) * 255.0, 0, 255)

    static = None if args.drift else colour(0.0)

    alpha = np.zeros((H, W), f32)
    out = np.empty((H, W, 3), np.uint8)

    def stamp(field, x0, y0, scale=1.0):
        """Threshold a distance field into the alpha buffer at (x0, y0)."""
        ch, cw_ = field.shape
        sy, sx = max(0, -y0), max(0, -x0)
        ey, ex = min(ch, H - y0), min(cw_, W - x0)
        if ey <= sy or ex <= sx:
            return
        f = field[sy:ey, sx:ex]
        # Half a pixel of soft edge: coverage, not opacity -- the shape's
        # interior is always fully lit.
        a = np.clip(0.5 - f, 0.0, 1.0)
        if args.glow > 0.0:
            a = np.maximum(a, args.glow * np.exp(-np.maximum(f, 0.0) / glow_r))
        if scale != 1.0:
            a = a * scale
        view = alpha[y0 + sy:y0 + ey, x0 + sx:x0 + ex]
        np.maximum(view, a, out=view)

    def digits(when):
        lt = time.localtime(when)
        hour = lt.tm_hour
        if args.ampm:
            hour = hour % 12 or 12
            lead = " " if hour < 10 else "1"
            return lead + "%d%02d%02d" % (hour % 10, lt.tm_min, lt.tm_sec)
        return "%02d%02d%02d" % (hour, lt.tm_min, lt.tm_sec)

    def render(t, frame):
        # Wall clock, not demo time: the melt has to land on the second even
        # if the frame loop drifts or stalls.
        now = time.time()
        frac = now % 1.0
        new, old = digits(now), digits(now - 1.0)

        # Linear, deliberately: easing spends the extra time at the ends,
        # where the shape is nearly a digit already, and rushes the middle,
        # which is the part worth watching.
        k = 1.0 if (args.no_morph or args.morph <= 0.0) \
            else min(frac / args.morph, 1.0)

        alpha[:] = 0.0
        for i, (x0, y0, tab) in enumerate(slots):
            a, b = old[i], new[i]
            if a == b or k >= 1.0:
                field = tab[b]
            else:
                field = tab[a] * (1.0 - k) + tab[b] * k
            stamp(field, x0, y0)

        lit = 1.0
        if args.blink:
            lit = 0.45 + 0.55 * (0.5 + 0.5 * np.cos(2.0 * np.pi * frac))
        for x0, y0 in colons:
            stamp(colon, x0, y0, lit)

        rgb = static if static is not None else colour(t)
        np.multiply(rgb, alpha[:, :, None], out=out, casting="unsafe")
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
