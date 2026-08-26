#!/usr/bin/env python3
"""The bouncing screensaver logo, and the wait for a corner hit.

Everyone who has ever sat in a meeting room knows the ritual: the logo drifts,
bounces off the edges, changes colour on every bounce, and the whole room
quietly waits for it to land *exactly* in a corner. That wait is the demo.

**Why this panel.** On a 4:3 monitor a corner hit is a party trick. On a 320x64
letterbox it is genuinely rare, because the logo crosses the long axis once
every 5.8 seconds and the short axis once every 2.2, and a corner needs both
extremes on the same instant. That rarity is the feature, so it is *designed*
rather than discovered -- see "The arithmetic" below.

**The logo is ours.** It is not the DVD Video mark and does not try to be: it
is "FT" in a sheared slab face over an ellipse reading TASCHEN, drawn in this
file as a character grid and a pair of conic sections. The joke is in the
silhouette and the behaviour, and it survives being ours perfectly well.

**The arithmetic.** Free travel is Sx = W - logo_w horizontally and
Sy = H - logo_h vertically, and the motion is ideal billiard reflection, so
each axis is a triangle wave and the position is closed form in time:

    x(t) = fold(vx * (t - H0), Sx),   fold(u, S) = |((u + S) mod 2S) - S|

x touches an edge whenever vx*(t-H0) is a whole multiple of Sx, y whenever
vy*(t-H0) is a whole multiple of Sy. A corner is both at once. Rather than
pick a velocity and then go looking for the corners, pick the corner period T
and two coprime integers -- q traverses of the long axis and p of the short
axis in that period -- and let the velocities fall out:

    vx = Sx * q / T        vy = Sy * p / T

Because gcd(p, q) = 1 the two edge-touch sets coincide only at whole multiples
of T, so corner hits are exactly every T seconds and never one second sooner.
The defaults are T = 180 s, q = 31, p = 83 (both prime, so coprime by
inspection), which gives 47.7 and 16.1 px/s, a 19-degree drift, a bounce off
the top or bottom every 2.2 seconds, and one corner every three minutes. A
rotation slot is 30-45 s, so a given slot has about a one-in-five chance of
containing a hit -- rare enough that seeing one is an event, common enough that
standing and watching pays off inside three minutes. Both q and p are odd,
which means successive hits alternate between diagonally opposite corners:
top-left, bottom-right, top-left. Near misses are frequent and are the whole
point; the closest one in a period comes within 1.1 px of a corner.

**It runs on wall-clock time, on purpose.** The trajectory is anchored to an
absolute epoch captured in build(), not to the segment's t=0. With segment time
the panel would show the identical three minutes every slot -- either every
appearance has a hit at the same second or no appearance ever does, and both
are deadly. Anchored to the clock, the logo is genuinely where it would be if
the screensaver had been running since before you walked up, the counter is
real, and walking past twice shows you two different states. `--epoch N` pins
it for tests and screenshots. render() is still a pure function of t for any
one build(), which is what the scheduler and the preview baker need.

**The counter** is the slow-burn part: corner hits since local midnight, and
time since the last one, in burn-in grey along the bottom. It makes the panel
something people check on rather than a loop, and it is the only number here.
A hit flashes the logo white, throws an expanding ring out of the corner it
landed in, prints CORNER across the middle for a second and a half, and ticks
the counter. Then it goes back to drifting, which is the correct emotional
arc.

**Cost.** A frame is a copy of the black ground, three small masked blits (two
of them dim, scanline-combed phosphor ghosts) and two short strings. Nothing
per-pixel over the whole panel except during the ring, which runs for 1.6 s in
every 180.

Run:  python3 dvd.py --host 127.0.0.1
      python3 dvd.py --corner-period 60          # impatient
      python3 dvd.py --epoch 41 --duration 20    # deterministic, hit at t=0
"""

import sys
import time

import numpy as np

import defcon
import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the same one caiso, propagation, sort and tide
# draw with: five rows a glyph, each row an octal digit whose three bits are
# the three columns. Nothing from a real typeface survives five pixels, and
# the Pi does not have the same faces installed as the machine this was
# written on.
#
# The glyph height is read off the array rather than assumed, and every layout
# below measures the mask it just built. A previous demo in this tree assumed
# the size and clipped the bottom off every capital E.
# --------------------------------------------------------------------------

_GLYPHS = {}
for _ch, _rows in defcon._FONT.items():
    _g = np.zeros((len(_rows), 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g

GLYPH_H, GLYPH_W = _GLYPHS[" "].shape
ADVANCE = GLYPH_W + 1                       # one blank column between glyphs


def text_mask(s, scale=1):
    """A boolean mask for a string, measured from the glyphs themselves."""
    s = str(s).upper()
    if not s:
        return np.zeros((GLYPH_H * scale, 1), bool)
    out = np.zeros((GLYPH_H, len(s) * ADVANCE - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * ADVANCE:i * ADVANCE + GLYPH_W] = _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


# --------------------------------------------------------------------------
# The logo.
#
# "FT" as a character grid, 16 rows of a 3px slab, then sheared right at the
# top so it leans like a logo instead of sitting there like a label. Under it
# an ellipse outline with TASCHEN inside, which is the same joke the original
# tells with a word in an oval under its wordmark.
#
# Everything about the logo is measured after it is drawn: the wordmark's own
# width sets where it is centred over the ellipse, and the assembled mask's
# shape is what the motion uses for its free travel. Nothing here is a
# hardcoded pixel count that could drift out of agreement with the art.
# --------------------------------------------------------------------------

WORDMARK = (
    "###########.#############",
    "###########.#############",
    "###########.#############",
    "###..............###.....",
    "###..............###.....",
    "###..............###.....",
    "#########........###.....",
    "#########........###.....",
    "#########........###.....",
    "###..............###.....",
    "###..............###.....",
    "###..............###.....",
    "###..............###.....",
    "###..............###.....",
    "###..............###.....",
    "###..............###.....",
)

SLANT = 0.25                 # columns of lean per row, top leaning right
# The ellipse. 9 rows tall was the first try and the ring cut through the top
# and bottom of the type inside it -- at +-2 rows off centre a 9-tall ellipse
# has already pinched in to 33 columns and TASCHEN needs 27 of them with the
# ring's own weight either side. Eleven rows pinches to 34 at the same offset
# and the word sits clear. The check is asserted in logo_mask() so a future
# edit to either number fails loudly instead of shaving the serifs off.
OVAL_W, OVAL_H = 43, 11      # the ellipse under the wordmark
OVAL_TEXT = "TASCHEN"
GAP = 2                      # rows between wordmark and ellipse


def wordmark_mask():
    """The sheared FT, as a bool mask. Width includes the lean."""
    rows = WORDMARK
    h = len(rows)
    w = len(rows[0])
    for i, row in enumerate(rows):
        if len(row) != w:
            raise ValueError("WORDMARK row %d is %d chars, expected %d"
                             % (i, len(row), w))
    lean = int(round((h - 1) * SLANT))
    out = np.zeros((h, w + lean), bool)
    for y, row in enumerate(rows):
        dx = int(round((h - 1 - y) * SLANT))
        out[y, dx:dx + w] = np.array([c == "#" for c in row], bool)
    return out


def ellipse_ring(w, h, thickness=1.2):
    """A 1px-ish ellipse outline in a w x h box.

    Thresholding the conic directly gives an outline three pixels thick at the
    ends of a 4:1 ellipse and holes at the top and bottom, because the level
    set is not a distance. Dividing by the gradient magnitude turns it into
    one, near enough, and the ring comes out an even weight all the way round.
    """
    cy, cx = (h - 1) * 0.5, (w - 1) * 0.5
    a, b = cx, cy
    yy, xx = np.mgrid[0:h, 0:w].astype(f32)
    px = (xx - cx) / a
    py = (yy - cy) / b
    fval = px * px + py * py - 1.0
    grad = np.sqrt((2.0 * px / a) ** 2 + (2.0 * py / b) ** 2) + 1e-6
    return np.abs(fval / grad) <= thickness * 0.5


def logo_mask():
    """Wordmark over ellipse-with-TASCHEN, centred on each other."""
    mark = wordmark_mask()
    mh, mw = mark.shape
    ring = ellipse_ring(OVAL_W, OVAL_H)
    inner = text_mask(OVAL_TEXT)
    th, tw = inner.shape

    # The word has to sit clear of the ring, not merely inside the box. Place
    # it in the ellipse's own frame and check for a one pixel gap all round;
    # an ellipse pinches fastest exactly where the top and bottom rows of the
    # type are, which is how the first draft ended up reading IASCHEI.
    oval = np.zeros((OVAL_H, OVAL_W), bool)
    ty0, tx0 = (OVAL_H - th) // 2, (OVAL_W - tw) // 2
    if ty0 < 0 or tx0 < 0:
        raise ValueError("%r does not fit the %dx%d ellipse"
                         % (OVAL_TEXT, OVAL_W, OVAL_H))
    oval[ty0:ty0 + th, tx0:tx0 + tw] = inner
    grown = oval.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            grown |= np.roll(np.roll(oval, dy, 0), dx, 1)
    if (grown & ring).any():
        raise ValueError("%r touches the %dx%d ellipse outline"
                         % (OVAL_TEXT, OVAL_W, OVAL_H))

    w = max(mw, OVAL_W)
    h = mh + GAP + OVAL_H
    out = np.zeros((h, w), bool)
    out[0:mh, (w - mw) // 2:(w - mw) // 2 + mw] = mark
    oy = mh + GAP
    out[oy:oy + OVAL_H, (w - OVAL_W) // 2:(w - OVAL_W) // 2 + OVAL_W] = ring
    ty = oy + (OVAL_H - th) // 2
    tx = (w - tw) // 2
    out[ty:ty + th, tx:tx + tw] |= inner
    return out


# --------------------------------------------------------------------------
# Colour. The original changes colour on every bounce; so does this, cycling a
# fixed list in order rather than drawing at random, so the sequence is
# deterministic and no two neighbours in the list are close in hue -- a random
# pick lands on near-identical colours often enough to look like a bug.
# --------------------------------------------------------------------------

PALETTE = (
    (255, 64, 64),        # red
    (72, 240, 255),       # cyan
    (255, 208, 48),       # amber
    (255, 72, 208),       # magenta
    (64, 255, 128),       # spring green
    (255, 128, 48),       # orange
    (110, 140, 255),      # blue
    (176, 255, 64),       # lime
    (200, 120, 255),      # violet
)

C_READOUT = (66, 74, 88)          # burn-in grey; the logo draws over it
C_FLASH = (255, 255, 255)


def add_arguments(ap):
    ap.add_argument("--corner-period", type=float, default=180.0,
                    help="seconds between corner hits (T). The whole design "
                         "decision: 30-45s slots mean ~1 in 5 shows a hit")
    ap.add_argument("--sweeps", type=int, default=31,
                    help="crossings of the long axis per corner period (q)")
    ap.add_argument("--bounces", type=int, default=83,
                    help="crossings of the short axis per corner period (p); "
                         "must be coprime with --sweeps or hits come early")
    ap.add_argument("--epoch", type=float, default=-1.0,
                    help="anchor the trajectory to this unix time instead of "
                         "the wall clock; a corner is hit at epoch+offset")
    ap.add_argument("--hit-offset", type=float, default=41.0,
                    help="seconds into each period at which the corner lands, "
                         "so hits do not fall on round wall-clock minutes")
    ap.add_argument("--celebrate", type=float, default=1.6,
                    help="seconds of ring, flash and CORNER after a hit")
    ap.add_argument("--trail", type=float, default=1.0,
                    help="brightness of the phosphor ghosts, 0 disables")
    ap.add_argument("--no-counter", action="store_true",
                    help="drop the readout and just bounce")


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def fold(u, span):
    """Ideal reflection as a triangle wave: |((u + S) mod 2S) - S|.

    This is the whole motion model. An integrator would accumulate a fraction
    of a pixel per bounce and the corner would quietly stop being a corner
    after a day of uptime; a fold of the elapsed time is exact at any t, and a
    dropped frame costs a frame rather than the trajectory.
    """
    m = (u + span) % (2.0 * span)
    return abs(m - span)


def build(args):
    W, H = args.width, args.height

    logo = logo_mask()
    lh, lw = logo.shape

    # Free travel of the logo's top-left corner. The reflection is of the
    # whole sprite, so the span is the panel less the sprite, and x = 0 means
    # the logo is flush into the left edge -- a real corner, not a near one.
    Sx = float(max(1, W - lw))
    Sy = float(max(1, H - lh))

    T = max(1.0, float(args.corner_period))
    q = max(1, int(args.sweeps))
    p = max(1, int(args.bounces))
    if _gcd(p, q) != 1:
        # Not fatal, but the actual corner period would be T/gcd, which is not
        # what was asked for. Bump p to the next coprime value and say so.
        while _gcd(p, q) != 1:
            p += 1
        sys.stderr.write("dvd: --bounces not coprime with --sweeps, using %d\n"
                         % p)
    vx = Sx * q / T
    vy = Sy * p / T

    # The clock. Absolute time, so the screensaver has a history; --epoch pins
    # it. Everything downstream is a function of (base + t).
    base = time.time() if args.epoch < 0 else float(args.epoch)
    h0 = float(args.hit_offset)
    # Local midnight, for the "today" in the counter. Fixed at build, which is
    # correct for any one segment and wrong only for a session that runs
    # through midnight, where it self-corrects on the next build.
    lt = time.localtime(base)
    midnight = base - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    # Both of these count hits along the same trajectory clock -- u = now - h0
    # -- so the subtraction is hits since midnight. Counting midnight from the
    # segment's own base instead was the first version and put nine million
    # corners on the panel, which is a lot even for this wall.
    hits_before_today = int(np.floor((midnight - h0) / T))

    celeb = max(0.0, float(args.celebrate))
    pal = np.array(PALETTE, np.uint8)
    npal = len(PALETTE)

    # Distance to each corner, for the shockwave. Four flips of one array;
    # 320 KB total, touched only during a celebration.
    yy, xx = np.mgrid[0:H, 0:W].astype(f32)
    d_tl = np.sqrt(xx * xx + yy * yy)
    dists = {
        (0, 0): d_tl,
        (1, 0): np.ascontiguousarray(d_tl[:, ::-1]),
        (0, 1): np.ascontiguousarray(d_tl[::-1, :]),
        (1, 1): np.ascontiguousarray(d_tl[::-1, ::-1]),
    }
    ring_reach = float(np.hypot(W, H))

    # Scanline combs for the ghosts, one per parity of the row they land on,
    # so the phosphor smear is combed against the panel's own rows and does not
    # crawl with the logo.
    combs = []
    for parity in (0, 1):
        rows = ((np.arange(lh) + parity) % 2 == 0)
        combs.append(logo & rows[:, None])

    # Ghost lag in seconds. Chosen against the actual speed so the trail is a
    # few pixels long whatever the velocity ends up being.
    speed = float(np.hypot(vx, vy)) or 1.0
    ghosts = ((3.0 / speed, 0.34), (6.0 / speed, 0.14))
    ghost_rgb = [(pal.astype(f32) * k).astype(np.uint8) for _, k in ghosts]

    bg = np.zeros((H, W, 3), np.uint8)
    buf = np.empty((H, W, 3), np.uint8)
    ring_f = np.empty((H, W), f32)
    ring_rgb = np.empty((H, W, 3), np.uint8)
    text_cache = {}

    def cached_text(s, scale=1):
        """Memoise the readout strings.

        The two strings change at most once a second, so rebuilding them every
        frame would be twenty small numpy calls a second for nothing. A couple
        of hundred distinct strings a day is nothing to hold, but the cache is
        bounded anyway: a build that somehow lived for weeks would otherwise
        keep every counter value it had ever drawn.
        """
        key = (s, scale)
        m = text_cache.get(key)
        if m is None:
            if len(text_cache) > 4096:
                text_cache.clear()
            m = text_mask(s, scale)
            text_cache[key] = m
        return m

    def blit(mask, y, x, rgb):
        """Draw a bool mask at (y, x) in one colour, clipped to the panel."""
        mh, mw = mask.shape
        y0, x0 = max(0, y), max(0, x)
        y1, x1 = min(H, y + mh), min(W, x + mw)
        if y1 <= y0 or x1 <= x0:
            return
        sub = mask[y0 - y:y1 - y, x0 - x:x1 - x]
        buf[y0:y1, x0:x1][sub] = rgb

    def place(u):
        """Position and bounce count at trajectory time u (seconds since H0)."""
        return (fold(u * vx, Sx), fold(u * vy, Sy),
                int(np.floor(u * vx / Sx)) + int(np.floor(u * vy / Sy)))

    def render(t, frame):
        u = (base + t) - h0            # seconds since a corner hit at u = 0
        n_hits = int(np.floor(u / T))  # which hit we are after
        since = u - n_hits * T         # and how long ago it was

        x, y, bounces = place(u)
        colour = pal[bounces % npal]

        np.copyto(buf, bg)

        # Phosphor ghosts first, oldest underneath. They follow the same
        # closed-form path, so they bend round a bounce by themselves.
        if args.trail > 0.0:
            for i in range(len(ghosts) - 1, -1, -1):
                lag = ghosts[i][0]
                gx, gy, gb = place(u - lag)
                iy = int(round(gy))
                rgb = ghost_rgb[i][gb % npal]
                if args.trail != 1.0:
                    rgb = (rgb.astype(f32) * args.trail).clip(0, 255).astype(np.uint8)
                blit(combs[iy & 1], iy, int(round(gx)), rgb)

        # The readout, under the logo: it is the panel's furniture, and the
        # logo sweeping over it is exactly what a screensaver does.
        if not args.no_counter:
            hot = celeb > 0.0 and since < celeb
            tint = C_FLASH if hot and since < celeb * 0.5 else C_READOUT
            count = max(0, n_hits - hits_before_today)
            left = cached_text("CORNERS TODAY %d" % count)
            secs = int(since)
            right = cached_text("LAST %d:%02d" % (secs // 60, secs % 60))
            ty = H - GLYPH_H - 1
            blit(left, ty, 4, tint)
            blit(right, ty, W - 4 - right.shape[1], tint)

        # The logo itself, flashing white for the first moments of a hit.
        flash = celeb > 0.0 and since < celeb * 0.22
        blit(logo, int(round(y)), int(round(x)),
             C_FLASH if flash else colour)

        # The celebration: a ring out of the corner it actually hit, and the
        # word. Which corner follows from the parity of the traverse counts,
        # so it is known without looking at the position.
        if celeb > 0.0 and since < celeb:
            k = since / celeb
            cxi = (n_hits * q) & 1
            cyi = (n_hits * p) & 1
            radius = ring_reach * 0.92 * k
            np.subtract(dists[(cxi, cyi)], radius, out=ring_f)
            np.abs(ring_f, out=ring_f)
            # A 2px core with a 1px shoulder either side, fading as it goes.
            # The overshoot before the clip is what gives the core its flat
            # top: a plain 1 - d/w ramp peaks at exactly one pixel and reads as
            # a dark scratch rather than a shockwave. Written with explicit
            # out= rather than `ring_f *= ...`, which would make the name local
            # to render() and shadow the buffer built above.
            np.multiply(ring_f, -1.0 / 2.5, out=ring_f)
            np.add(ring_f, 1.5, out=ring_f)
            np.clip(ring_f, 0.0, 1.0, out=ring_f)
            np.multiply(ring_f, (1.0 - k) ** 0.5, out=ring_f)
            np.multiply(ring_f[:, :, None], np.array(colour, f32), out=ring_rgb,
                        casting="unsafe")
            np.maximum(buf, ring_rgb, out=buf)

            word = cached_text("CORNER", 3)
            wh, ww = word.shape
            fade = float(max(0.0, 1.0 - k)) ** 0.6
            blit(word, (H - wh) // 2, (W - ww) // 2,
                 (np.array(C_FLASH, f32) * fade).astype(np.uint8))

        return buf

    # Handy for the test script and the README: the numbers this build chose.
    render.geometry = dict(logo=(lh, lw), span=(Sy, Sx), vx=vx, vy=vy,
                           period=T, q=q, p=p, base=base, hit_offset=h0,
                           midnight=midnight, before_today=hits_before_today,
                           readout_y=H - GLYPH_H - 1, readout_x=4)
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
