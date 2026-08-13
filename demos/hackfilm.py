#!/usr/bin/env python3
"""The makerspace's own projects, advancing past on a strip of film.

Everything else on this wall is about somewhere else -- the grid, the bay, the
sky, the encyclopedia. This one is about the room the wall is bolted to.
Sequoia Fabrica documents what its members build on a MediaWiki at
wiki.sequoiafabrica.org, and this panel is seven of those pages redrawn as a
seven-frame filmstrip that pulls down one frame at a time, holds long enough to
read the name and the grove, and pulls down again.

**Every frame is drawn here, from the words on the wiki page.** Nothing is
traced and nothing is downloaded. The Maslow's four belts converge on its sled
because the wiki says it has four independently controlled steel reinforced
belts; the Weevil Eye has two LED eyes and a photoresistor between them because
the class handout lists exactly those components; the knitting machine's needle
bed is seven needles to the inch because the machine page says 7 gauge. Where
the page did not say what a thing looks like, the thing is not in the strip --
see below, that ruled out most of the wiki.

**One representation choice does all the work: the entire strip is a single
baked image, and render() is one slice of it.** Seven cells of 160 columns are
drawn once in build() into a 64 x 1120 image, complete with the film base, the
sprocket perforations and the edge print, then the array is padded with a copy
of its own first 320 columns so a horizontal offset can wrap without a seam.
A frame is `np.copyto(out, PAD[wy:wy+64, x0:x0+320])` -- one numpy call, the
same cost whatever is in the picture. That is the whole reason the panel can
afford seven detailed illustrations on a Pi 3: it never draws any of them.

The motion is the second half of that choice. `x0` comes from an ease-out-back
curve, which is fast off the mark, overshoots by about nine percent and settles
back -- a claw yanking the film down and the frame rocking into the gate,
rather than a carousel gliding. `wy` is a one pixel vertical weave taken from a
seeded sequence, at full amplitude while the film is moving and decaying over
the third of a second after it lands. Both are pure functions of t, and since
the weave is an index into a baked array rather than a call to the RNG, so is
the whole panel.

**What the wiki actually had.** 125 pages, most of them governance, policy and
tool operating instructions. Discounting those, the pages with enough
description to draw something honest from were: Maslow CNC, Electronics/
WeevilEye, Textiles/Industrial Knitting Machine, GlowProject, Spoonmaking &
Engraving, Riso EZ220U and FlaschenTaschen -- which is this wall, so it is in
the strip, showing itself. Several likely-sounding pages turned out to be one
sentence long (Electronics/GlowBoxen is "A shelf lighting project at Sequoia
Fabrica"; Electronics/Outatime is "Back to the Future") and are deliberately
absent rather than invented into something photogenic. Three of the seven are
Electronics because Electronics is the grove that writes things down.

No people are named. The wiki names members in places; projects and groves are
what reaches the wall.

Run:  python3 hackfilm.py --host 127.0.0.1
      python3 hackfilm.py --hold 3 --advance 0.6
      python3 hackfilm.py --weave 0 --seed 7
"""

import sys

import numpy as np

import defcon
import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The film. A cell is one frame of the strip; the panel is exactly two cells
# wide, so the held frame sits centred with half of its neighbours showing at
# either edge, which is what makes it read as a strip rather than a slideshow.
# --------------------------------------------------------------------------

CELL_W = 160                      # columns per film frame
IMG_X0, IMG_W = 4, 152            # the image inside the cell; 4 px frame line

BAND_H = 7                        # film base above and below the image
ART_Y0, ART_H = BAND_H, 38        # the illustration
CAP_Y0, CAP_H = ART_Y0 + ART_H, 12  # name and grove
assert BAND_H + ART_H + CAP_H + BAND_H == 64

# 16 mm has perforations down one edge only, which is lucky: two bright rows of
# holes on a 64 row panel would fight the picture. Four perfs a frame, in the
# top band; the bottom band carries the edge print instead.
PERF_X = (12, 50, 88, 126)
PERF_W, PERF_Y0, PERF_H = 6, 1, 5

C_BASE = (26, 22, 19)             # film base between the frames
C_PERF = (146, 136, 118)          # light through a sprocket hole
C_EDGE = (104, 76, 38)            # edge print, deliberately dim
C_FRAMELINE = (14, 12, 11)
C_NAME = (234, 228, 214)

# Grove colours. The wiki's Groves Table is the authority on which groves
# exist; these are just a consistent code so a passer-by learns that cyan means
# fabrication before they have read a single caption.
GROVES = {
    "ELECTRONICS":         (110, 230, 142),
    "DIGITAL FABRICATION": (92, 190, 240),
    "TEXTILES":            (240, 132, 190),
    "WOODWORKING":         (232, 162, 84),
    "PRINTMAKING":         (255, 92, 172),
}


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 font, the one every readout on this wall uses: five
# rows a glyph, each row an octal digit whose three bits are the three columns.
# It is measured here rather than assumed -- text_mask() reports its own height
# and the caption rows are laid out from that -- because a demo on this wall
# once clipped the bottom row off every capital E by assuming a size.
# --------------------------------------------------------------------------

_GLYPHS = {}
for _ch, _rows in defcon._FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g

FONT_H = _GLYPHS[" "].shape[0]
FONT_ADV = _GLYPHS[" "].shape[1] + 1


def text_mask(s):
    """A boolean (FONT_H, 4n-1) mask for a string, one blank column between."""
    s = str(s).upper()
    if not s:
        return np.zeros((FONT_H, 1), bool)
    out = np.zeros((FONT_H, len(s) * FONT_ADV - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * FONT_ADV:i * FONT_ADV + 3] = _GLYPHS.get(ch, _GLYPHS[" "])
    return out


def blit_text(dst, y, x, s, rgb):
    """Draw a string at (y, x), clipped. Returns the width it occupied."""
    m = text_mask(s)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 > y0 and x1 > x0:
        sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
        dst[y0:y1, x0:x1][sub] = rgb
    return gw


def centre_text(dst, y, s, rgb, x0=0, w=None):
    w = dst.shape[1] if w is None else w
    return blit_text(dst, y, x0 + (w - text_mask(s).shape[1]) // 2, s, rgb)


# --------------------------------------------------------------------------
# Drawing primitives. All of this runs once, in build(), onto a float32
# canvas -- so it can be as unhurried as it likes. The only thing render()
# ever does is slice the result.
# --------------------------------------------------------------------------

def _rect(img, y, x, h, w, c):
    img[max(y, 0):y + h, max(x, 0):x + w] = c


def _line(img, y0, x0, y1, x1, c, alpha=1.0):
    """A 1 px line, sampled densely enough that it never leaves gaps."""
    n = int(max(abs(y1 - y0), abs(x1 - x0))) * 2 + 2
    k = np.arange(n, dtype=f32) / (n - 1.0)
    ys = np.rint(y0 + (y1 - y0) * k).astype(np.int32)
    xs = np.rint(x0 + (x1 - x0) * k).astype(np.int32)
    ok = ((ys >= 0) & (ys < img.shape[0]) & (xs >= 0) & (xs < img.shape[1]))
    ys, xs = ys[ok], xs[ok]
    if alpha >= 1.0:
        img[ys, xs] = c
    else:
        img[ys, xs] = img[ys, xs] * (1.0 - alpha) + np.asarray(c, f32) * alpha


def _ellipse_mask(img, cy, cx, ry, rx, inner=0.0):
    """(slice_y, slice_x, bool mask) for an ellipse, clipped to img."""
    y0 = max(int(np.floor(cy - ry)), 0)
    y1 = min(int(np.ceil(cy + ry)) + 1, img.shape[0])
    x0 = max(int(np.floor(cx - rx)), 0)
    x1 = min(int(np.ceil(cx + rx)) + 1, img.shape[1])
    if y1 <= y0 or x1 <= x0:
        return None
    yy = (np.arange(y0, y1, dtype=f32)[:, None] - cy) / max(ry, 1e-6)
    xx = (np.arange(x0, x1, dtype=f32)[None, :] - cx) / max(rx, 1e-6)
    d = np.sqrt(yy * yy + xx * xx)
    m = d <= 1.0
    if inner > 0.0:
        m &= d >= inner
    return slice(y0, y1), slice(x0, x1), m


def _ellipse(img, cy, cx, ry, rx, c, inner=0.0, alpha=1.0):
    got = _ellipse_mask(img, cy, cx, ry, rx, inner)
    if got is None:
        return
    sy, sx, m = got
    sub = img[sy, sx]
    if alpha >= 1.0:
        sub[m] = c
    else:
        sub[m] = sub[m] * (1.0 - alpha) + np.asarray(c, f32) * alpha


def _disc(img, cy, cx, r, c, inner=0.0, alpha=1.0):
    _ellipse(img, cy, cx, r, r, c, inner, alpha)


def _glow(img, cy, cx, r, c, strength=1.0):
    """Additive falloff, for anything that emits: LEDs, a router bit, a lamp."""
    got = _ellipse_mask(img, cy, cx, r, r)
    if got is None:
        return
    sy, sx, _ = got
    yy = (np.arange(sy.start, sy.stop, dtype=f32)[:, None] - cy) / r
    xx = (np.arange(sx.start, sx.stop, dtype=f32)[None, :] - cx) / r
    d = np.clip(1.0 - np.sqrt(yy * yy + xx * xx), 0.0, 1.0) ** 2
    img[sy, sx] += d[:, :, None] * (np.asarray(c, f32) * strength)


def _polyline(pts, step=0.4):
    """Densely sample a list of (y, x) vertices into two float arrays."""
    ys, xs = [], []
    for i in range(len(pts) - 1):
        (ay, ax), (by, bx) = pts[i], pts[i + 1]
        n = max(int(max(abs(by - ay), abs(bx - ax)) / step), 1)
        k = np.arange(n, dtype=f32) / n
        ys.append(ay + (by - ay) * k)
        xs.append(ax + (bx - ax) * k)
    return np.concatenate(ys), np.concatenate(xs)


def _rounded_path(y0, x0, y1, x1, r, per_arc=14):
    """Vertices tracing a rounded rectangle clockwise from the top left."""
    pts = []
    corners = ((y0 + r, x0 + r, 180, 270), (y0 + r, x1 - r, 270, 360),
               (y1 - r, x1 - r, 0, 90), (y1 - r, x0 + r, 90, 180))
    for cy, cx, a0, a1 in corners:
        for j in range(per_arc + 1):
            a = np.radians(a0 + (a1 - a0) * j / float(per_arc))
            pts.append((cy + r * np.sin(a), cx + r * np.cos(a)))
    pts.append(pts[0])
    return pts


def _plot(img, ys, xs, c, alpha=1.0):
    ys = np.rint(ys).astype(np.int32)
    xs = np.rint(xs).astype(np.int32)
    ok = ((ys >= 0) & (ys < img.shape[0]) & (xs >= 0) & (xs < img.shape[1]))
    ys, xs = ys[ok], xs[ok]
    if alpha >= 1.0:
        img[ys, xs] = c
    else:
        img[ys, xs] = img[ys, xs] * (1.0 - alpha) + np.asarray(c, f32) * alpha


# --------------------------------------------------------------------------
# The seven illustrations. Each gets a float32 (ART_H, IMG_W) canvas and the
# build's RandomState; each is a homage drawn from what its wiki page says,
# named in the comment above it. Ordered so the three Electronics frames never
# land next to each other.
# --------------------------------------------------------------------------

def art_maslow(a, rng):
    """Maslow CNC. From wiki page 'Maslow CNC'.

    The page describes a large format router that cuts full sheets of plywood,
    held by four independently controlled steel reinforced belts, each on its
    own servo, measuring belt length to a hundredth of a millimetre. So: four
    belts from four corner anchors to a sled, over a sheet drawn at the real
    4x8 proportion, with the cut it has finished behind it and the toolpath it
    has not yet reached ahead. The machine is bigger than the sheet, which is
    the part that is easy to get wrong and the thing that makes it a Maslow.
    """
    a[:] = (9, 10, 13)

    # The sheet: 68 x 34 is exactly 2:1, i.e. 4 feet by 8 feet.
    sy0, sx0, sh, sw = 2, 42, 34, 68
    ply = np.array((150, 112, 66), f32)
    a[sy0:sy0 + sh, sx0:sx0 + sw] = ply
    # Veneer grain: a per-row tint plus a few darker streaks running the long
    # way, which is what tells the eye it is a sheet good and not a slab.
    tint = 1.0 + 0.055 * rng.randn(sh, 1).astype(f32)
    a[sy0:sy0 + sh, sx0:sx0 + sw] *= tint[:, :, None]
    for _ in range(9):
        r = rng.randint(0, sh)
        x = rng.randint(sx0, sx0 + sw - 20)
        w = rng.randint(10, 26)
        a[sy0 + r, x:x + w] *= 0.86
    _rect(a, sy0, sx0, 1, sw, ply * 1.14)          # lit top edge
    _rect(a, sy0 + sh - 1, sx0, 1, sw, ply * 0.55)  # shadowed bottom edge

    # The cut. A rounded rectangle is the honest first thing anyone cuts on a
    # sheet machine, and it makes the finished/unfinished split legible.
    path = _rounded_path(7, 50, 30, 101, 6)
    ys, xs = _polyline(path, 0.3)
    # Where the sled has got to. Two fifths of the way round puts it high in
    # the frame, which is the only place the four belts read as four belts --
    # low down, the two bottom ones lie along the same near-horizontal line.
    done = int(len(ys) * 0.42)
    _plot(a, ys[:done], xs[:done], (34, 22, 12))            # kerf
    _plot(a, ys[:done] - 1, xs[:done], (206, 164, 112))     # chamfer, lit side
    # The remaining toolpath, dashed, the way a CAM preview draws it.
    rest_y, rest_x = ys[done:], xs[done:]
    dash = (np.arange(len(rest_y)) // 4) % 3 != 0
    _plot(a, rest_y[dash], rest_x[dash], (96, 200, 226), 0.9)

    cy, cx = float(ys[done]), float(xs[done])
    _disc(a, cy, cx, 6.5, (46, 50, 58))
    _disc(a, cy, cx, 6.5, (168, 178, 196), inner=0.78)
    _disc(a, cy, cx, 1.8, (255, 244, 208))
    _glow(a, cy, cx, 9.0, (120, 92, 40), 1.0)
    for _ in range(22):                             # dust off the bit
        d = rng.uniform(2.0, 9.0)
        th = rng.uniform(0.0, 2.0 * np.pi)
        _plot(a, np.array([cy + d * np.sin(th)]), np.array([cx + d * np.cos(th)]),
              (208, 178, 130), 0.45)

    # Belts and anchors. The anchors are outside the sheet on all four sides.
    anchors = ((2, 3), (2, IMG_W - 4), (ART_H - 3, 3), (ART_H - 3, IMG_W - 4))
    for ay, ax in anchors:
        _line(a, ay, ax, cy, cx, (86, 94, 108))
        _line(a, ay + 1, ax, cy + 1, cx, (140, 150, 166), 0.5)
    for ay, ax in anchors:
        _rect(a, ay - 3, ax - 3, 7, 7, (24, 26, 31))
        _disc(a, ay, ax, 3.0, (96, 104, 118), inner=0.6)
        _disc(a, ay, ax, 1.0, (36, 210, 120))       # the servo's encoder LED
    return a


def art_weevil(a, rng):
    """Weevil Eye. From wiki page 'Electronics/WeevilEye'.

    The monthly beginner soldering class. The page lists the kit exactly --
    PCB, resistors, LEDs, battery holder, transistor, photoresistor -- and the
    kit's whole trick is that the two LEDs are eyes that come on when the
    photoresistor is covered. So: three boards on the bench at three stages,
    left one finished and bright, middle one lit and dimmer, right one still
    bare copper with the iron on it. Three, because it is a class, not a
    project.
    """
    a[:] = (16, 14, 13)
    a += (rng.rand(ART_H, IMG_W, 1) * 5.0).astype(f32)   # bench mat texture

    def board(x0, lit, populated=True):
        y0 = 4
        bw, bh = 34, 29
        pcb = (16, 78, 50)
        _rect(a, y0, x0, bh, bw, pcb)
        for cy, cx, r in ((y0 + 1, x0 + 1, 1), (y0 + 1, x0 + bw - 2, 1),
                          (y0 + bh - 2, x0 + 1, 1), (y0 + bh - 2, x0 + bw - 2, 1)):
            _disc(a, cy, cx, r + 1.4, (16, 14, 13))      # rounded corners
        _rect(a, y0, x0, 1, bw, (30, 118, 76))
        # Silkscreen: the outline the class solders inside.
        _line(a, y0 + 2, x0 + 3, y0 + 2, x0 + bw - 4, (176, 196, 180), 0.5)

        if populated:
            for ex, in ((x0 + 9,), (x0 + 24,)):          # the two LED eyes
                _disc(a, y0 + 8, ex, 3.4, (120, 26, 20))
                _disc(a, y0 + 8, ex, 3.4, (232, 66, 48), inner=0.72)
                if lit > 0:
                    _disc(a, y0 + 8, ex, 1.8, (255, 196, 170))
                    _glow(a, y0 + 8, ex, 9.0, (200, 40, 30), lit)
            # Photoresistor between the eyes: a pale disc with the meander.
            _disc(a, y0 + 8, x0 + 17, 2.6, (206, 190, 128))
            for k in range(3):
                _line(a, y0 + 7 + k, x0 + 15, y0 + 7 + k, x0 + 19, (60, 48, 30))
            # Transistor: the flat-faced half moon.
            _disc(a, y0 + 15, x0 + 17, 3.0, (28, 28, 32))
            _rect(a, y0 + 15, x0 + 14, 3, 7, (28, 28, 32))
            # Two resistors, bands and all.
            for rx in (x0 + 5, x0 + 23):
                _rect(a, y0 + 15, rx, 3, 7, (198, 176, 130))
                for b, col in enumerate(((90, 60, 30), (30, 30, 30), (180, 40, 40))):
                    _rect(a, y0 + 15, rx + 1 + b * 2, 3, 1, col)
                _rect(a, y0 + 16, rx - 2, 1, 2, (150, 150, 158))
                _rect(a, y0 + 16, rx + 7, 1, 2, (150, 150, 158))
            # Coin cell holder.
            _disc(a, y0 + 23, x0 + 17, 5.0, (166, 172, 184), inner=0.66)
            _disc(a, y0 + 23, x0 + 17, 3.2, (198, 204, 216))
        # Solder pads, gold, whether or not anything is in them yet.
        for px in range(x0 + 4, x0 + bw - 3, 5):
            _disc(a, y0 + 26, px, 1.0, (206, 164, 62))

    board(6, 1.0)
    board(44, 0.45)
    board(82, 0.0, populated=False)

    # The iron: barrel in from the right, hot wedge tip on the bare board,
    # one wisp of smoke. Nothing else in the frame is this colour.
    _line(a, 6, 151, 20, 124, (60, 62, 70))
    _line(a, 7, 151, 21, 124, (92, 96, 108))
    _line(a, 8, 151, 22, 124, (40, 42, 48))
    _line(a, 20, 124, 26, 112, (170, 174, 186))
    _disc(a, 26.0, 112.0, 1.6, (255, 208, 130))
    _glow(a, 26.0, 112.0, 8.0, (180, 90, 20), 1.0)
    sm_y = np.arange(26, 8, -1, dtype=f32)
    sm_x = 112 + 4.0 * np.sin((sm_y - 26) * 0.42)
    _plot(a, sm_y, sm_x, (150, 150, 156), 0.28)
    return a


def art_knit(a, rng):
    """Industrial knitting machine. From 'Textiles/Industrial Knitting Machine'.

    The page is a full operating manual: a single system flatbed, seven needles
    per inch, patterns drafted in HQPDS or Raynen, jacquard as well as single
    bed. A flatbed is natively this panel's shape -- a long needle bed with the
    knit head traversing it and the fabric coming off underneath -- so it is
    drawn straight on, at its own gauge, with a jacquard on the fabric because
    jacquard is the thing the page spends the most words on.
    """
    a[:] = (12, 12, 15)

    bed_y = 9
    _rect(a, bed_y, 2, 6, IMG_W - 4, (44, 46, 54))
    _rect(a, bed_y, 2, 1, IMG_W - 4, (96, 100, 112))
    # 7 gauge: the needles are drawn at their real relative spacing, close
    # enough together to read as a comb rather than as a row of posts.
    for x in range(4, IMG_W - 4, 2):
        _rect(a, bed_y + 1, x, 4, 1, (140, 146, 160))

    # Yarn cones, and the threads running from them to the carriage.
    head_x = 92
    cones = ((6, 24, (216, 74, 96)), (6, 46, (238, 226, 198)))
    for cy, cx, col in cones:
        for k in range(7):
            _line(a, cy - 6 + k, cx - 1 - k // 2, cy - 6 + k, cx + 1 + k // 2, col)
        _rect(a, cy + 1, cx - 4, 1, 9, (70, 70, 78))
        _line(a, cy - 5, cx, bed_y - 1, head_x - 4, col, 0.55)

    # The knit head: a box that straddles the bed, with a yarn feeder under it.
    _rect(a, bed_y - 5, head_x - 11, 11, 23, (58, 62, 72))
    _rect(a, bed_y - 5, head_x - 11, 1, 23, (128, 134, 148))
    _rect(a, bed_y - 3, head_x - 8, 3, 17, (28, 30, 36))
    _rect(a, bed_y - 3, head_x - 7, 1, 6, (80, 200, 150))     # its little display
    _disc(a, bed_y - 1.0, head_x + 5.0, 1.2, (230, 120, 60))

    # The fabric. A stitch is 2x2 px; the jacquard is a diamond lattice worked
    # in two yarns, which is what a two-colour jacquard on this machine is.
    fy0 = bed_y + 7
    fh, fw = ART_H - fy0 - 1, 132
    fx0 = (IMG_W - fw) // 2
    sy = np.arange(fh)[:, None] // 2
    sx = np.arange(fw)[None, :] // 2
    d = (np.abs(((sx + sy) % 10) - 5) + np.abs(((sx - sy) % 10) - 5))
    yarn_a = np.array((222, 210, 182), f32)
    yarn_b = np.array((176, 54, 74), f32)
    fab = np.where((d < 4)[:, :, None], yarn_b, yarn_a)
    fab[d == 4] = (206, 150, 150)          # the float where the yarns swap
    # Stitch texture: every stitch is darker along its top and left, which at
    # 2 px a stitch is the whole of what makes knitted fabric look knitted.
    fab *= np.where((np.arange(fh)[:, None] % 2 == 0), 0.74, 1.0)[:, :, None]
    fab *= np.where((np.arange(fw)[None, :] % 2 == 0), 0.90, 1.0)[:, :, None]
    # It hangs, so it is dimmer and slightly narrower at the bottom.
    fab *= np.clip(1.06 - np.arange(fh, dtype=f32)[:, None] / (fh * 2.4), 0, 1)[:, :, None]
    a[fy0:fy0 + fh, fx0:fx0 + fw] = fab
    # Take-down: the fabric edge is uneven and the corners curl.
    for x in range(fw):
        cut = int(1.5 + 1.5 * np.sin(x * 0.09) + (abs(x - fw / 2) / (fw / 2)) ** 3 * 4)
        a[fy0 + fh - cut:fy0 + fh, fx0 + x] = (12, 12, 15)
    return a


def art_glow(a, rng):
    """Glow Lights. From wiki pages 'GlowProject' and
    'Electronics/RaspberryPiWorkstations'.

    Five LED strips around the space -- tv, desk, kitchen, workbench,
    recycling -- each on an ESP32-C3 taking MQTT, cycling six named themes:
    Green, Rainbow, Pink Pony, Ocean, Sunset, Forest. The themes are the only
    part of the project a passer-by ever sees, so the frame is a single run of
    strip carrying all six in order, with the wash they throw on the wall
    underneath, and the controller at the left end with its radio on. It is a
    swatch card of the room's own lighting.
    """
    a[:] = (10, 10, 14)

    themes = ((60, 220, 96), None, (255, 82, 178), (34, 142, 224),
              (255, 122, 46), (36, 116, 60))
    sx0, sw = 26, 120
    cols = np.zeros((sw, 3), f32)
    seg = sw / float(len(themes))
    for i, col in enumerate(themes):
        lo, hi = int(i * seg), int((i + 1) * seg)
        if col is None:                       # RAINBOW is the one that is not a colour
            h = np.linspace(0.0, 0.92, hi - lo, dtype=f32)
            cols[lo:hi] = ds.hsv_to_rgb(h, f32(0.95), f32(1.0)) * 255.0
        else:
            cols[lo:hi] = col
    # A short blend at each join, so it reads as one strip fading between looks
    # rather than six unrelated bars.
    smooth = cols.copy()
    for k in (1, 2, 3):
        smooth[k:] += cols[:-k]
        smooth[:-k] += cols[k:]
    cols = smooth / 7.0

    shelf_y = 13
    _rect(a, shelf_y, 8, 4, IMG_W - 16, (38, 34, 30))     # the shelf itself
    _rect(a, shelf_y, 8, 1, IMG_W - 16, (78, 70, 60))
    # The strip: individual emitters, not a continuous line. Three on, one off.
    for i in range(0, sw, 4):
        a[shelf_y + 4:shelf_y + 6, sx0 + i:sx0 + i + 3] = cols[i] * 0.9
    # The wash below. One vertical falloff, applied to every column at once.
    fall = np.clip(1.0 - np.arange(ART_H - shelf_y - 6, dtype=f32) / 19.0, 0, 1) ** 2
    a[shelf_y + 6:, sx0:sx0 + sw] += fall[:, None, None] * cols[None, :, :] * 0.72
    # And a little spill up onto the shelf face.
    up = np.clip(1.0 - np.arange(5, dtype=f32) / 5.0, 0, 1) ** 2
    a[shelf_y - 5:shelf_y, sx0:sx0 + sw] += up[::-1, None, None] * cols[None, :, :] * 0.20

    # The ESP32-C3: module, ceramic antenna, and three arcs saying it is on wifi.
    _rect(a, shelf_y - 4, 9, 8, 14, (36, 38, 46))
    _rect(a, shelf_y - 4, 9, 8, 14, (36, 38, 46))
    _rect(a, shelf_y - 3, 11, 5, 6, (86, 90, 102))
    _disc(a, shelf_y + 1.0, 20.0, 1.0, (80, 210, 255))
    for r, al in ((4, 0.55), (7, 0.35), (10, 0.2)):
        th = np.linspace(-0.9, 0.9, 30, dtype=f32)
        _plot(a, (shelf_y - 4) - r * np.cos(th), 16 + r * np.sin(th),
              (90, 190, 240), al)
    return a


def art_spoon(a, rng):
    """Spoonmaking & Engraving. From wiki page 'Spoonmaking & Engraving'.

    A class that is half Laser and half Woodworking: a template is laser cut
    with a bullseye pattern that centres the scoop, the blank is shaped with a
    dremel and carbide burrs, the handle goes back to the laser to be engraved,
    and the whole thing is finished in a wax of mineral oil, carnauba and
    beeswax. Every one of those five steps is in the frame: the rings behind
    the bowl, the scoop, the engraving down the handle, and the wax highlight
    along the rim.
    """
    a[:] = (22, 18, 15)
    a += (rng.rand(ART_H, IMG_W, 1) * 6.0).astype(f32)

    by, bx = 19.0, 112.0
    # The template's bullseye, which the page says is there to centre the scoop.
    for r in (17, 12, 7):
        _disc(a, by, bx, r, (94, 74, 50), inner=(r - 1.0) / r)
    _line(a, by, bx - 19, by, bx + 19, (94, 74, 50), 0.6)
    _line(a, by - 19, bx, by + 19, bx, (94, 74, 50), 0.6)

    maple = np.array((198, 154, 100), f32)
    # Handle: a taper, drawn as a run of vertical spans so it can narrow.
    for x in range(20, 96):
        k = (x - 20) / 76.0
        half = 2.0 + 3.4 * k * k
        y0 = int(round(by - half))
        y1 = int(round(by + half)) + 1
        a[y0:y1, x] = maple * (0.86 + 0.2 * np.sin(x * 0.7))
        a[y0, x] = maple * 1.16
        a[y1 - 1, x] = maple * 0.5
    _ellipse(a, by, bx, 14.0, 21.0, maple)
    _ellipse(a, by - 1, bx, 14.0, 21.0, maple * 1.12, inner=0.86)
    _ellipse(a, by + 0.5, bx + 0.5, 10.5, 16.0, maple * 0.60)     # the scoop
    _ellipse(a, by + 1.5, bx + 1.0, 7.5, 11.5, maple * 0.44)
    # Grain, following the length of the blank.
    for _ in range(11):
        gy = by + rng.uniform(-11.0, 11.0)
        gx = np.arange(22, 130, dtype=f32)
        gys = gy + 1.6 * np.sin(gx * 0.05 + gy)
        keep = (np.abs(gys - by) < 12) & (gx < 96 + 30)
        _plot(a, gys[keep], gx[keep], (120, 88, 54), 0.22)
    # Laser engraving down the handle: a chevron run, burnt dark.
    for x in range(26, 92, 6):
        k = (x - 20) / 76.0
        h = 1.0 + 2.4 * k * k
        _line(a, by - h, x, by, x + 3, (58, 34, 18))
        _line(a, by + h, x, by, x + 3, (58, 34, 18))
    # Wax: one bright specular streak on the rim, which is the whole reason
    # anybody finishes a spoon.
    th = np.linspace(3.5, 5.4, 60, dtype=f32)
    _plot(a, by + 13.0 * np.sin(th), bx + 20.0 * np.cos(th), (255, 240, 208), 0.8)
    return a


def art_riso(a, rng):
    """Riso EZ220. From wiki pages 'Riso EZ220U' and 'Printmaking'.

    One drum, one colour, one pass. The machine came with a Fluorescent Pink
    drum, and the space has a black one and a blue one besides, so a two-colour
    print means two separate plates and two runs through the machine -- and the
    second run will not land exactly on the first. That misregistration is the
    look. The example print on the wiki page is flowers, so this is flowers:
    the blue plate is stems and leaves, the pink plate is the heads, offset by
    the couple of pixels a real second pass drifts. Neither plate is a solid,
    because the page's hard rule is to keep fills under about 75% or the drum
    jams -- a riso solid is a dot field with holidays in it.
    """
    a[:] = (14, 13, 16)

    py0, px0, ph, pw = 1, 20, ART_H - 2, 112
    a[py0:py0 + ph, px0:px0 + pw] = (234, 230, 220)
    a[py0:py0 + ph, px0:px0 + pw] *= (0.97 + 0.05 * rng.rand(ph, pw, 1)).astype(f32)

    yy = np.arange(ph)[:, None]
    xx = np.arange(pw)[None, :]

    def run(plate, col, dy, dx, screen):
        """One pass through the machine: draw the plate, screen it, multiply."""
        tmp = np.zeros((ph, pw, 3), f32)
        plate(tmp, dy, dx)
        ink = tmp[:, :, 0]
        # A 3x3 dot screen with a per-drum phase, which is why the two plates'
        # dots interleave instead of sitting on top of each other, plus a few
        # random holidays for the ink the roller did not lay down.
        dots = ((yy + screen) % 3 != 0) | ((xx + screen * 2) % 3 != 0)
        ink = np.clip(ink, 0, 1) * dots * (rng.rand(ph, pw) > 0.05)
        cov = ink[:, :, None] * 0.92
        sub = a[py0:py0 + ph, px0:px0 + pw]
        sub *= (1.0 - cov) + cov * (np.asarray(col, f32) / 255.0)

    def stems(tmp, dy, dx):
        for bx, lean, ht in ((28, -3.0, 20), (58, 1.5, 24), (86, 4.0, 17)):
            st = np.arange(ht, dtype=f32)
            sy = ph - 3 - st + dy
            sx = bx + dx + lean * st / ht
            _plot(tmp, sy, sx, (1, 0, 0))
            _plot(tmp, sy, sx + 1, (1, 0, 0))
            _ellipse(tmp, ph - 8 - ht * 0.35 + dy, bx + dx - 6, 2.6, 6.0, (1, 0, 0))
            _ellipse(tmp, ph - 12 - ht * 0.35 + dy, bx + dx + 7, 2.6, 6.0, (1, 0, 0))

    def heads(tmp, dy, dx):
        for bx, lean, ht, r in ((28, -3.0, 20, 1.0), (58, 1.5, 24, 1.25),
                                (86, 4.0, 17, 0.85)):
            cy = ph - 3 - ht + dy
            cx = bx + dx + lean
            for k in range(5):
                th = k * 2.0 * np.pi / 5.0 - 1.57
                _ellipse(tmp, cy + 4.6 * r * np.sin(th), cx + 5.4 * r * np.cos(th),
                         3.4 * r, 3.8 * r, (1, 0, 0))
            _ellipse(tmp, cy, cx, 2.2 * r, 2.4 * r, (0, 0, 0))   # open centre

    run(stems, (26, 108, 196), 0.0, 0.0, 0)        # blue drum, first pass
    run(heads, (255, 72, 176), -1.0, 2.0, 1)       # fluorescent pink, 2 px out

    # Roller marks: the faint horizontal banding an EZ leaves on a long run.
    for _ in range(3):
        r = rng.randint(2, ph - 2)
        a[py0 + r, px0:px0 + pw] *= 0.94
    _rect(a, py0, px0, ph, 1, (198, 194, 186))
    _rect(a, py0, px0 + pw - 1, ph, 1, (198, 194, 186))
    return a


def art_ft(a, rng):
    """Flaschen Taschen. From wiki page 'FlaschenTaschen'.

    The wall documenting itself, which the page makes easy: two displays named
    after stars, both in the electronics space -- Polaris at 64x64 and
    Betelgeuse at 320x64, the one this is running on. So the frame is the two
    of them on the wall at their real relative proportions, five to one beside
    one to one, drawn as what they are: matrices of discrete emitters with the
    dark gaps between them showing. Betelgeuse is showing a strip of colour,
    because that is what it is doing right now.
    """
    a[:] = (11, 11, 14)

    def panel(y0, x0, h, w, content):
        _rect(a, y0 - 1, x0 - 1, h + 2, w + 2, (48, 50, 58))     # the frame
        _rect(a, y0, x0, h, w, (6, 6, 8))
        sub = a[y0:y0 + h, x0:x0 + w]
        lit = ((np.arange(h)[:, None] % 2 == 0) & (np.arange(w)[None, :] % 2 == 0))
        sub += lit[:, :, None] * content
        # A little bloom, which is what an LED matrix actually looks like.
        sub[1:] += lit[:-1, :, None] * content[:-1] * 0.22
        sub[:, 1:] += lit[:, :-1, None] * content[:, :-1] * 0.22

    # Betelgeuse: 100 x 20 is the 5:1 of a 320x64 panel.
    bh, bw = 20, 100
    hue = (np.arange(bw, dtype=f32) / bw + np.arange(bh, dtype=f32)[:, None] * 0.004)
    val = 0.35 + 0.65 * np.clip(np.sin(np.arange(bw, dtype=f32) * 0.09) ** 2, 0, 1)
    content = ds.hsv_to_rgb(hue, f32(0.85), val[None, :] * np.ones((bh, 1), f32)) * 255.0
    panel(5, 7, bh, bw, content.astype(f32))

    # Polaris: 24 x 24, square, showing a conifer -- a sequoia is the one thing
    # this room is named for, and a triangle and a trunk is all 24 rows allow.
    ph = pw = 24
    tree = np.zeros((ph, pw, 3), f32)
    for r in range(3, 19):
        half = int((r - 2) * 0.62)
        tree[r, 12 - half:12 + half + 1] = (28, 168, 74)
    tree[19:22, 11:14] = (128, 78, 34)
    panel(3, 121, ph, pw, tree)

    blit_text(a, 30, 7, "BETELGEUSE 320x64", (150, 158, 172))
    blit_text(a, 30, 121, "POLARIS", (150, 158, 172))
    return a


# --------------------------------------------------------------------------
# The strip. Name, grove, illustration, and the wiki page the description came
# from -- that last one is not drawn, it is here so the next person to extend
# this knows where to read.
# --------------------------------------------------------------------------

FRAMES = (
    ("MASLOW CNC",        "DIGITAL FABRICATION", art_maslow, "Maslow CNC"),
    ("WEEVIL EYE",        "ELECTRONICS",         art_weevil, "Electronics/WeevilEye"),
    ("KNITTING MACHINE",  "TEXTILES",            art_knit,
     "Textiles/Industrial Knitting Machine"),
    ("GLOW LIGHTS",       "ELECTRONICS",         art_glow,   "GlowProject"),
    ("SPOONMAKING",       "WOODWORKING",         art_spoon,  "Spoonmaking & Engraving"),
    ("RISO EZ220",        "PRINTMAKING",         art_riso,   "Riso EZ220U"),
    ("FLASCHEN TASCHEN",  "ELECTRONICS",         art_ft,     "FlaschenTaschen"),
)


def bake_strip(rng):
    """Draw every cell once into one (64, N*CELL_W, 3) uint8 image."""
    n = len(FRAMES)
    strip = np.zeros((64, n * CELL_W, 3), f32)
    strip[:] = C_BASE

    for i, (name, grove, draw, _page) in enumerate(FRAMES):
        cell = strip[:, i * CELL_W:(i + 1) * CELL_W]
        gcol = GROVES[grove]

        # Perforations, and the edge print under them.
        for px in PERF_X:
            cell[PERF_Y0:PERF_Y0 + PERF_H, px:px + PERF_W] = C_PERF
            for cy in (PERF_Y0, PERF_Y0 + PERF_H - 1):        # rounded ends
                cell[cy, px] = C_BASE
                cell[cy, px + PERF_W - 1] = C_BASE
        blit_text(cell, 58, 8, "SEQUOIA FABRICA", C_EDGE)
        blit_text(cell, 58, 128, "%02d" % (i + 1), C_EDGE)

        # The frame line, and then the picture inside it.
        cell[BAND_H:64 - BAND_H, :IMG_X0] = C_FRAMELINE
        cell[BAND_H:64 - BAND_H, IMG_X0 + IMG_W:] = C_FRAMELINE
        art = np.zeros((ART_H, IMG_W, 3), f32)
        draw(art, rng)
        cell[ART_Y0:ART_Y0 + ART_H, IMG_X0:IMG_X0 + IMG_W] = np.clip(art, 0, 255)

        # The caption. Two lines, centred, on the frame's own dark ground, with
        # a tab of the grove colour at the left so the code is learnable.
        cap = cell[CAP_Y0:CAP_Y0 + CAP_H, IMG_X0:IMG_X0 + IMG_W]
        cap[:] = (10, 10, 12)
        cap[0, :] = np.asarray(gcol, f32) * 0.22
        cap[1:CAP_H, 0:2] = gcol
        # Measured, not assumed: the two baselines are placed from FONT_H, with
        # a blank row between them and one to spare under the second.
        centre_text(cap, 1, name, C_NAME)
        centre_text(cap, 1 + FONT_H + 1, grove, gcol)
        assert 1 + FONT_H + 1 + FONT_H <= CAP_H

    return np.clip(strip, 0, 255).astype(np.uint8)


def _ease(k):
    """Ease-out-back: a claw yanking the frame down and it rocking to a stop."""
    c = 1.28
    k = k - 1.0
    return 1.0 + (c + 1.0) * k * k * k + c * k * k


def add_arguments(ap):
    ap.add_argument("--hold", type=float, default=5.0,
                    help="seconds a frame sits in the gate")
    ap.add_argument("--advance", type=float, default=0.85,
                    help="seconds to pull down one frame")
    ap.add_argument("--weave", type=float, default=1.0,
                    help="vertical gate weave, in pixels (0 disables)")
    ap.add_argument("--seed", type=int, default=3,
                    help="seeds the grain, the dust and the weave")


def build(args):
    rng = np.random.RandomState(args.seed)
    strip = bake_strip(rng)

    n = len(FRAMES)
    total = n * CELL_W

    # Pad right by a panel width with a copy of the start, so a wrapping
    # offset is still one contiguous slice; pad two rows top and bottom with
    # film base so the weave has somewhere to go. After this, render() never
    # has to think about an edge.
    pad = np.empty((68, total + ds.WIDTH, 3), np.uint8)
    pad[2:66, :total] = strip
    pad[2:66, total:] = strip[:, :ds.WIDTH]
    pad[0:2] = pad[2:3]
    pad[66:68] = pad[65:66]

    # The weave, drawn once. An index into this is a pure function of t; a call
    # to the RNG in render() would not be.
    weave = rng.choice(np.array([-1, 0, 0, 0, 1]), 97).astype(f32)

    hold = max(args.hold, 0.1)
    adv = max(args.advance, 0.05)
    period = hold + adv
    settle = min(0.35, hold)          # the film is still shivering after it lands
    amp = max(args.weave, 0.0)

    # Cell i is centred when the panel's left edge is here. The panel is
    # exactly two cells wide, so that is half a cell to the left of the cell.
    base = [i * CELL_W - (ds.WIDTH - CELL_W) // 2 for i in range(n)]

    out = np.empty((ds.HEIGHT, ds.WIDTH, 3), np.uint8)

    def render(t, frame):
        tt = t % (n * period)
        i = int(tt // period)
        u = tt - i * period
        if u < hold:
            x = float(base[i])
            gain = max(0.0, 1.0 - u / settle) if settle > 0 else 0.0
        else:
            x = base[i] + CELL_W * _ease((u - hold) / adv)
            gain = 1.0
        x0 = int(round(x)) % total
        wy = 2
        if amp > 0.0:
            wy += int(round(gain * amp * weave[int(tt * 20.0) % 97]))
        np.copyto(out, pad[wy:wy + ds.HEIGHT, x0:x0 + ds.WIDTH])
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
