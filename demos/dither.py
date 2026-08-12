#!/usr/bin/env python3
"""One photograph, quantised five ways, with the boundary sliding across it.

An LED matrix *is* a dithering device. Every picture on this wall is already a
quantisation of something continuous, and this panel is the wall showing its
own working: a real satellite image with a wipe travelling across it, and on
either side of the wipe the same image rendered by a different quantiser.
Left of the boundary the new one, right of it the old one, and the boundary
moving so the eye is forced to compare the two along a line it can actually
see.

The ladder, in order, is **continuous tone -> Floyd-Steinberg -> Atkinson ->
ordered Bayer 8x8 -> hard threshold**, which is a ladder of how much of the
quantisation error a method bothers to account for. Floyd-Steinberg pushes all
of it into the neighbours it has not visited yet and reproduces the picture's
average brightness exactly. Atkinson -- Bill Atkinson's, out of the 1984
Macintosh -- pushes only six eighths of it and throws the other quarter away,
which loses shadow and highlight detail and in exchange gives that bright,
open, crisp look that the whole of early Mac graphics is made of. Bayer does
not diffuse at all; it compares each pixel against a fixed 8x8 threshold
matrix, so the error is not accounted for anywhere and instead of texture you
get a woven crosshatch that is the same everywhere. Threshold accounts for
nothing and has no dither at all: over 50% white, under 50% black, and the
gradients go to slabs.

**Monochrome, and only monochrome.** The obvious temptation is to dither to a
small colour palette on half the panel and to 1-bit on the other, and that is
two ideas on one panel. What makes these four algorithms *visible* at three
metres is the black-and-white dot texture, so everything here is one ink on
black -- 1 bit -- and the only colour on the panel is the wipe edge, which is
furniture rather than picture. The continuous-tone region uses the same ink at
256 levels, so the two sides of the first wipe differ in exactly one property.

**The dithering happens once, in build().** Error diffusion is inherently
sequential -- each pixel's threshold decision depends on the residue left by
the pixel before it -- so there is no vectorised form, and a Python loop over
20480 pixels is a tenth of a second on a laptop and about a second on the
wall's Pi. That is fine *once*, in the segment builder's worker thread, and
impossible thirty times a second. So build() produces five finished 320x64
uint8 panels, captions and all, and render() does nothing but copy the left
part of one and the right part of another and draw a single bright column
between them: three numpy calls a frame, no arithmetic, no allocation. The
performance design and the purity requirement turn out to be the same design.

**The second act is a punch-in, because Atkinson and Bayer look identical at
1:1.** At one dot per LED, on a picture as busy as a satellite frame, the
difference between error diffusion and an ordered matrix is a texture nobody
can resolve from across a room. So after the ladder the panel steps into a
80x16 detail of itself -- the region with the most midtone in it, found at
build time -- at 2x and then 4x, and runs the same wipe again between
Bayer and Atkinson at four LEDs per dot, where Bayer's regular weave and
Atkinson's clumpy organic dots are unmistakable. The zoom is a step and not a
smooth scale: the magnifications are 1x, 2x and 4x, they are separate baked
panels, and a demo about the visible pixel grid ought to zoom in integers.

**Where the picture comes from.** `goes.py`'s cached GOES-18 GeoColor time
lapse, which `ftdata.py` has already cropped to exactly 320x64 -- a satellite
image of the eastern Pacific and California is the ideal dithering subject,
being enormous smooth gradients (ocean, haze, the Central Valley) with hard
bright detail on top (cloud tops, the coastline). One frame is picked out of
the seventy-odd in the window -- the one with the most midtone in it, which is
not the one with the most contrast, see `midtone_score` -- and held; the
picture is still
because the *wipe* is the motion here and a time lapse underneath it would be
a second idea. Nothing here fetches: `ftdata.py` does that on a timer, this
reads its cache at build time and never touches the network. See the ftdata
docstring for why that split is absolute.

**With no cache at all it draws a test image rather than a card.** A no-data
card would be a waste of a panel that does not actually need today's weather
to make its point -- so if the cache is empty, build() generates a lit sphere
over a graded ground with a ramp bar under it, which is the classic thing you
dither to show off a dithering algorithm, and says TEST IMAGE in the caption
where the satellite's name would be. Stale imagery is shown as it is with the
age in red, because the point of this panel is the arithmetic, not the
weather, and two-day-old cloud dithers exactly as well as this morning's.

    $ python3 dither.py --source test        # the synthetic image
    $ python3 dither.py --wipe 6 --no-zoom   # slow, ladder only
"""

import bisect
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

# Ink and ground. One warm off-white on black: this is a panel about two
# levels, so there are two levels. The ink is very slightly warm because a
# dithered field of pure 255,255,255 dots on an LED wall reads clinical, and
# every reference image people have of Atkinson dithering is on a warm CRT.
INK = (255, 246, 230)
GROUND = (0, 0, 0)

# Furniture. The wipe edge is the only saturated colour anywhere on the panel
# -- cold blue against a warm monochrome picture -- which is what makes a
# 1-pixel line read as a moving boundary from three metres rather than as a
# scratch in the image.
C_EDGE = (110, 190, 255)
C_TEXT = (232, 226, 214)
C_DIM = (120, 128, 140)
C_WARN = (255, 88, 64)
C_RULE = (26, 28, 34)
C_TICK = (52, 55, 62)

# How much of the picture survives under the caption strips. Not zero: this is
# a full-bleed image and a solid black bar across the top of it would cut the
# picture, where a smoked one lets the dither texture run through and keeps
# the thing looking like one photograph.
STRIP_DIM = 0.30

# The strips, in rows, at the design height of 64. Seven at the top -- six of
# type plus a rule and a pixel of air -- and five at the bottom for ticks.
TOP_H = 8
BOT_H = 5

# The ladder. Order matters: it is monotone in how much of the quantisation
# error the method accounts for, which is the whole argument of the panel.
CONT, FS, ATK, BAYER, THRESH = 0, 1, 2, 3, 4
STAGE_NAMES = ["CONTINUOUS", "FLOYD-STEINBERG", "ATKINSON", "BAYER 8X8",
               "THRESHOLD"]

# Error-diffusion kernels as (dx, dy, weight), divisor. dy is always >= 0 and
# a dx < 0 only ever appears with dy > 0: a diffusion kernel may only push
# error into pixels the scan has not reached yet, which is exactly what makes
# it sequential and exactly why this cannot be vectorised.
#
# Floyd-Steinberg (1975) distributes all sixteen sixteenths, so the output's
# mean is the input's mean to within one pixel's worth.
FS_KERNEL = (((1, 0, 7.0), (-1, 1, 3.0), (0, 1, 5.0), (1, 1, 1.0)), 16.0)
# Atkinson (Apple, 1984) distributes six eighths and discards two. That is not
# a bug: throwing a quarter of the error away stops it accumulating across
# large flat areas, which is what keeps highlights clean and whites white, at
# the cost of crushing detail in the darkest and lightest few percent. It is
# the reason MacPaint pictures look like MacPaint pictures.
ATK_KERNEL = (((1, 0, 1.0), (2, 0, 1.0), (-1, 1, 1.0), (0, 1, 1.0),
               (1, 1, 1.0), (0, 2, 1.0)), 8.0)

# Bayer 8x8, generated rather than typed: the recursive construction is four
# lines and a typo in a 64-entry literal is invisible until it stipples wrong.
def _bayer(n):
    m = np.zeros((1, 1), np.int64)
    size = 1
    while size < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
        size *= 2
    return m


BAYER8 = _bayer(8)


# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 table, the same one goes.py, tide.py and propagation.py
# use. The glyph height and advance are *read off the table* rather than
# assumed -- an earlier panel in this tree clipped the bottom off every capital
# E by assuming a size -- so GLYPH_H/GLYPH_W below are measurements.
# --------------------------------------------------------------------------

_GLYPHS = {}
for _ch, _rows in defcon._FONT.items():
    _g = np.zeros((len(_rows), 3), bool)
    for _r, _digit in enumerate(_rows):
        _v = int(_digit, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g

GLYPH_H, GLYPH_W = _GLYPHS["E"].shape          # measured, not assumed
ADVANCE = GLYPH_W + 1


def text_mask(s):
    """A (GLYPH_H, n*ADVANCE-1) bool mask for a string, 1px between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((GLYPH_H, 1), bool)
    out = np.zeros((GLYPH_H, len(s) * ADVANCE - 1), bool)
    blank = _GLYPHS[" "]
    for i, ch in enumerate(s):
        out[:, i * ADVANCE:i * ADVANCE + GLYPH_W] = _GLYPHS.get(ch, blank)
    return out


def text_w(s, scale=1):
    return max(0, (ADVANCE * len(str(s)) - 1) * scale)


def stamp(buf, x, y, s, colour, scale=1):
    """Draw text into a uint8 (H, W, 3) buffer, clipped. Returns its width.

    Clipped rather than asserted: the layout is designed for 320x64 but has to
    survive being asked for something else, and a demo that raises on an odd
    canvas takes the whole rotation down with it.
    """
    m = text_mask(s)
    if scale > 1:
        m = np.repeat(np.repeat(m, scale, 0), scale, 1)
    h, w = m.shape
    H, W = buf.shape[:2]
    x, y = int(x), int(y)
    sx, sy = max(0, -x), max(0, -y)
    ex, ey = min(w, W - x), min(h, H - y)
    if ex > sx and ey > sy:
        buf[y + sy:y + ey, x + sx:x + ex][m[sy:ey, sx:ex]] = colour
    return w


# --------------------------------------------------------------------------
# The quantisers. All four run once, at build time, over a float image in
# 0..255 and return a uint8 field of 0 or 1.
#
# They all quantise the *coded* value rather than a linearised one, which is
# deliberate and is what makes the comparison honest. The panel's PWM is close
# enough to linear in the code value that a continuous pixel of 128 emits half
# of full; a 1-bit region whose dot density is 128/255 also emits half of full.
# Diffusing in the coded domain therefore makes the dithered halves match the
# continuous half in average brightness, which is precisely the claim the wipe
# is making. Diffusing in linear light would be more "correct" by one theory
# and would visibly darken every dithered region against its neighbour.
# --------------------------------------------------------------------------

def diffuse(src, kernel, divisor, serpentine=False):
    """Error-diffusion dither. `src` is float 0..255; returns uint8 0/1.

    Written over a flat Python list rather than over the numpy array on
    purpose. This loop touches every pixel and then every kernel entry, and
    numpy's scalar indexing costs a good hundred nanoseconds per element
    access where a list costs a few; on 20480 pixels that difference is the
    difference between a build that takes a moment and one that takes ten
    seconds on the Pi. numpy is the wrong tool for a strictly sequential
    dependency and this is what it looks like to admit that.

    The interior of the image is handled by a bounds-check-free fast path --
    the kernel's reach is at most two columns and two rows, so away from the
    edges no neighbour can fall off -- because the bounds tests were a third
    of the total time.
    """
    h, w = src.shape
    buf = src.astype(np.float64).reshape(-1).tolist()
    out = bytearray(h * w)
    # Kernel pre-resolved to (flat offset, weight/divisor) for the fast path,
    # and to (dx, dy, weight/divisor) for the slow edge path.
    fast = [(dy * w + dx, wt / divisor) for dx, dy, wt in kernel]
    slow = [(dx, dy, wt / divisor) for dx, dy, wt in kernel]
    maxdx = max(abs(dx) for dx, _, _ in kernel)
    maxdy = max(dy for _, dy, _ in kernel)
    for y in range(h):
        row = y * w
        interior_row = y + maxdy < h
        for x in range(w):
            i = row + x
            old = buf[i]
            if old >= 128.0:
                out[i] = 1
                err = old - 255.0
            else:
                err = old
            if err == 0.0:
                continue
            if interior_row and maxdx <= x < w - maxdx:
                for off, k in fast:
                    buf[i + off] += err * k
            else:
                for dx, dy, k in slow:
                    nx = x + dx
                    ny = y + dy
                    if 0 <= nx < w and ny < h:
                        buf[ny * w + nx] += err * k
    return np.frombuffer(bytes(out), np.uint8).reshape(h, w)


def bayer_dither(src):
    """Ordered dither against a tiled 8x8 Bayer matrix. One vectorised pass.

    The thresholds are (index + 0.5) / 64 of full scale, so the matrix spans
    the interval evenly and a flat mid-grey comes out at exactly half density.
    """
    h, w = src.shape
    tile = np.tile(BAYER8, (-(-h // 8), -(-w // 8)))[:h, :w]
    thresh = (tile.astype(f32) + 0.5) * (255.0 / 64.0)
    return (src > thresh).astype(np.uint8)


def threshold_dither(src):
    """No dither at all. The control: this is what quantising without a plan
    does, and it is on the panel so the others have something to beat."""
    return (src >= 128.0).astype(np.uint8)


# --------------------------------------------------------------------------
# The picture. Either one frame out of goes.py's cached window, or a generated
# one when there is no cache.
# --------------------------------------------------------------------------

def load_goes(cache_dir, product):
    """(frames, stamps, meta, age, problem). Any of the first four may be None.

    Deliberately forgiving about the record: this panel does not need today's
    weather, it needs *a picture*, so anything wrong with the cache falls
    through to the test image rather than to a card.
    """
    got = ftdata.load(product, cache_dir)
    if got is None:
        return None, None, None, None, "no cached goes imagery"
    payload, age = got
    if not isinstance(payload, dict):
        return None, None, None, age, "goes record is malformed"
    blob = ftdata.load_blob(payload.get("blob"), cache_dir)
    if blob is None:
        return None, None, None, age, "frame sidecar is missing"
    try:
        frames = blob["frames"]
        stamps = np.asarray(blob["stamps"], np.float64)
    except Exception:                                          # noqa: BLE001
        return None, None, None, age, "frame sidecar has no frames"
    if (frames.ndim != 4 or frames.shape[3] != 3 or frames.dtype != np.uint8
            or len(frames) == 0 or len(frames) != len(stamps)):
        return None, None, None, age, "frame sidecar is malformed"
    return frames, stamps, payload, age, None


def luminance(rgb):
    """Rec.601 luma of a uint8 (H, W, 3) frame, as float 0..255.

    601 and not 709 because the source is a JPEG browse product in the old
    coefficients, and because on this subject the difference is under a level:
    cloud is neutral and the Central Valley is a broad brown that both agree
    about.
    """
    f = rgb.astype(f32)
    return (f[:, :, 0] * 0.299 + f[:, :, 1] * 0.587 + f[:, :, 2] * 0.114)


def midtone_score(v):
    """What fraction of an already-stretched image is in the middle of the
    range. This is *the* figure of merit for a dithering subject, and it took
    a bad-looking first attempt to work that out.

    The obvious pick is the highest-contrast frame -- and the highest-contrast
    frame in a GOES window is a bimodal one, black ocean under white cloud,
    which has almost no pixels a dither has any choice about. It quantises to
    a silhouette and every one of the four methods produces the same
    silhouette. What shows a dither off is *midtone*: haze, thin cirrus, the
    Central Valley, the gradient at the edge of a cloud deck, everything that
    has to be represented as a density of dots because it cannot be
    represented as a dot or an absence of one.
    """
    return float(((v > 45.0) & (v < 210.0)).mean())


def pick_frame(frames, gamma):
    """The most ditherable frame in the window.

    The window is a whole night and day, and the frames from the small hours
    are GeoColor's infrared night rendering: dark, low contrast, and mostly
    empty. Scoring on midtone area after the same tone curve the panel will
    use picks a daylit frame with weather in it, and does so without needing
    to know anything about where the sun is.
    """
    best, best_s = 0, -1.0
    for i in range(len(frames)):
        s = midtone_score(stretch(luminance(frames[i]), 1.0, 99.0, gamma))
        if s > best_s:
            best, best_s = i, s
    return best


def resample(img, w, h):
    """Nearest-neighbour a (H, W) float image onto the panel. Once, at build."""
    fh, fw = img.shape
    if (fw, fh) == (w, h):
        return img
    ry = np.clip(((np.arange(h) + 0.5) * fh / h).astype(np.intp), 0, fh - 1)
    rx = np.clip(((np.arange(w) + 0.5) * fw / w).astype(np.intp), 0, fw - 1)
    return np.ascontiguousarray(img[ry][:, rx])


def stretch(lum, lo_pct, hi_pct, gamma):
    """Percentile stretch to 0..255, then a gamma.

    Both halves of every wipe get exactly the same tone curve, so this cannot
    flatter one algorithm over another; it exists because a satellite frame
    uses maybe two thirds of the range and a 1-bit rendering of a picture that
    never reaches white is a picture with no white in it.
    """
    lo = float(np.percentile(lum, lo_pct))
    hi = float(np.percentile(lum, hi_pct))
    if hi - lo < 8.0:
        lo, hi = float(lum.min()), max(float(lum.max()), float(lum.min()) + 1.0)
    out = (lum - lo) * (255.0 / (hi - lo))
    np.clip(out, 0.0, 255.0, out=out)
    if abs(gamma - 1.0) > 1e-3:
        out = 255.0 * np.power(out / 255.0, gamma, dtype=f32)
    return out


def test_image(w, h):
    """A generated subject for when there is no cache: a lit sphere over a
    graded ground, with a linear ramp bar beneath it.

    This is the standard thing to dither, and for a good reason -- a sphere
    under a single light is a smooth gradient in two directions with a
    specular highlight and a terminator in it, which is where error diffusion
    and an ordered matrix visibly disagree, and a linear ramp along the bottom
    is a direct readout of how each method handles density. Everything here is
    drawn from arithmetic; nothing is traced or loaded.
    """
    y, x = np.mgrid[0:h, 0:w].astype(f32)
    u = x / max(1.0, w - 1.0)
    v = y / max(1.0, h - 1.0)

    # Ground: a vertical gradient with a slow horizontal wash over it, so the
    # background is never flat and every method has something to be wrong
    # about.
    img = 40.0 + 110.0 * (1.0 - v) + 26.0 * np.sin(u * 3.3 + 0.6)

    # The sphere. Centred a third of the way in, as tall as the panel allows.
    cx, cy = w * 0.30, h * 0.46
    r = h * 0.40
    dx = (x - cx) / r
    dy = (y - cy) / r
    d2 = dx * dx + dy * dy
    inside = d2 < 1.0
    nz = np.sqrt(np.clip(1.0 - d2, 0.0, None))
    # Lambert from the upper left plus a tight specular, and a rim term so the
    # limb does not disappear into the ground.
    lx, ly, lz = -0.55, -0.62, 0.56
    lam = np.clip(-dx * lx - dy * ly + nz * lz, 0.0, 1.0)
    spec = np.power(lam, 26.0, dtype=f32)
    ball = 14.0 + 208.0 * lam + 210.0 * spec
    img = np.where(inside, ball, img)

    # A soft shadow on the ground under the sphere.
    sh = np.exp(-(((x - cx - r * 0.5) / (r * 1.5)) ** 2
                  + ((y - cy - r * 1.05) / (r * 0.34)) ** 2))
    img = np.where(inside, img, img * (1.0 - 0.75 * sh))

    # The ramp bar: a strip of pure linear ramp across the bottom third of the
    # panel, right of the sphere, framed by a dark gutter.
    x0, x1 = int(w * 0.52), int(w * 0.97)
    y0, y1 = int(h * 0.62), int(h * 0.80)
    if x1 > x0 + 4 and y1 > y0 + 2:
        img[y0 - 1:y1 + 1, x0 - 1:x1 + 1] = 8.0
        ramp = np.linspace(0.0, 255.0, x1 - x0, dtype=f32)
        img[y0:y1, x0:x1] = ramp[None, :]
    return np.clip(img, 0.0, 255.0).astype(f32)


def detail_window(src, w, h, dw, dh):
    """Where to punch in: the (x, y) of the most midtone dw x dh window.

    Same argument as pick_frame, and the same mistake was available here: the
    highest-*variance* window in a satellite frame is a piece of coastline,
    which is a hard edge that all four methods render identically, and
    magnifying it forty times produces two identical black-and-white shapes
    either side of the wipe. The window worth magnifying is the one with the
    most pixels that no single decision can represent, so it is scored on
    midtone area with variance only as a tie-break.
    """
    best, best_s = (0, 0), -1.0
    ys = range(TOP_H, max(TOP_H + 1, h - BOT_H - dh + 1), 2)
    xs = range(0, max(1, w - dw + 1), 4)
    for y0 in ys:
        for x0 in xs:
            win = src[y0:y0 + dh, x0:x0 + dw]
            s = midtone_score(win) + float(win.std()) * 1e-4
            if s > best_s:
                best, best_s = (x0, y0), s
    return best


# --------------------------------------------------------------------------
# Baking a finished panel: a 0/1 field (or a continuous one), magnified,
# inked, and captioned. Everything the wall ever shows is one of these, whole.
# --------------------------------------------------------------------------

def ink_field(field, levels_255=False):
    """(H, W) field -> (H, W, 3) uint8 in ink on ground.

    `levels_255` treats the field as 0..255 continuous rather than 0/1, which
    is the only difference between the continuous-tone panel and the rest --
    same ink, same ground, different number of steps between them.
    """
    h, w = field.shape
    out = np.empty((h, w, 3), np.uint8)
    if levels_255:
        a = np.clip(field, 0.0, 255.0).astype(f32) * (1.0 / 255.0)
    else:
        a = field.astype(f32)
    for c in range(3):
        np.multiply(a, float(INK[c]), out=out[:, :, c], casting="unsafe")
    if not levels_255:
        # A 0/1 field has to land exactly on the ink, not on a rounded-down
        # approximation of it, or the two sides of the first wipe differ by a
        # level for no reason.
        m = field.astype(bool)
        out[m] = INK
        out[~m] = GROUND
    return out


def caption(panel, name, stage, parts, tag):
    """Smoke the two strips into a panel and set the type in them.

    The algorithm's name is set at *both* ends. That looks redundant on a
    panel showing one algorithm, and it is exactly what makes the wipe work:
    when a panel is composited as the left half its right-hand name is hidden
    under the other one and vice versa, so during a wipe the incoming name
    reads at the left and the outgoing name at the right, and when the wipe
    finishes the label simply stays where it is instead of jumping. Two
    labels, no per-frame typesetting, no pop.
    """
    h, w = panel.shape[:2]
    top = min(TOP_H, max(0, h // 4))
    bot = min(BOT_H, max(0, h // 6))

    if top:
        strip = panel[:top]
        np.multiply(strip, STRIP_DIM, out=strip, casting="unsafe")
        panel[top - 1] = C_RULE
        ty = max(0, (top - 1 - GLYPH_H) // 2)
        nw = text_w(name)
        stamp(panel, 2, ty, name, C_TEXT)
        if 2 + nw + 6 < w - 2 - nw:
            stamp(panel, w - 2 - nw, ty, name, C_TEXT)
        # The source and its age go in the middle, identical on every panel,
        # so the wipe passes over them without them appearing to move. Two
        # coloured runs rather than one string: the source is furniture and
        # stays dim, the age is the only thing on this panel anybody has to
        # act on and goes red when it is past its TTL.
        sw = sum(text_w(s) for s, _ in parts) + ADVANCE * (len(parts) - 1)
        sx = (w - sw) // 2
        if sx > 2 + nw + 4 and sx + sw < w - 3 - nw:
            for s, colour in parts:
                stamp(panel, sx, ty, s, colour)
                sx += text_w(s) + ADVANCE

    if bot:
        y0 = h - bot
        strip = panel[y0:]
        np.multiply(strip, STRIP_DIM, out=strip, casting="unsafe")
        panel[y0] = C_RULE
        # The ladder: five ticks, the current one lit. Baked into the panel,
        # so as the wipe crosses, the marker steps across with it and the
        # position on the ladder is part of the picture rather than a widget.
        n = len(STAGE_NAMES)
        tw, gap = 22, 6
        total = n * tw + (n - 1) * gap
        x0 = (w - total) // 2
        ty0 = y0 + 2
        for i in range(n):
            xs = x0 + i * (tw + gap)
            if xs < 0 or xs + tw > w or ty0 + 2 > h:
                continue
            if i == stage:
                panel[ty0:ty0 + 2, xs:xs + tw] = INK
            else:
                panel[ty0:ty0 + 1, xs:xs + tw] = C_TICK
        if tag:
            stamp(panel, 3, ty0 - 1, tag, C_DIM)
    return panel


def magnify(field, x0, y0, dw, dh, k):
    """Blow a dw x dh window of a field up by an integer k. np.repeat twice.

    The magnification is of the *dithered output*, not of the source followed
    by a re-dither, which is the entire point: what you are looking at when
    this is on screen is the actual dot pattern the algorithm produced at 1:1,
    with each dot four LEDs across.
    """
    win = field[y0:y0 + dh, x0:x0 + dw]
    return np.repeat(np.repeat(win, k, 0), k, 1)


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--product", default=ftdata.GOES_PRODUCT,
                    help="ftdata product to take the picture from")
    ap.add_argument("--source", default="auto",
                    choices=("auto", "goes", "test"),
                    help="auto falls back to the generated test image")
    ap.add_argument("--frame", type=int, default=-1,
                    help="which cached frame to dither (-1 = best contrast)")
    ap.add_argument("--wipe", type=float, default=3.2,
                    help="seconds for one boundary to cross the panel")
    ap.add_argument("--hold", type=float, default=0.7,
                    help="seconds held on a finished stage")
    ap.add_argument("--no-zoom", dest="zoom", action="store_false",
                    help="ladder only, no punch-in to the dot pattern")
    ap.add_argument("--zoom-factor", type=int, default=4,
                    help="magnification of the second act (2 or 4)")
    ap.add_argument("--gamma", type=float, default=1.25,
                    help="tone curve applied to the source before quantising; "
                         "above 1 darkens, which moves a bright satellite "
                         "frame off the white clip and back into dots")
    ap.add_argument("--serpentine", action="store_true",
                    help="boustrophedon scan for the diffusion kernels")
    ap.add_argument("--stats", action="store_true",
                    help="print build timings and per-method error, then run")


def smoothstep(u):
    return u * u * (3.0 - 2.0 * u)


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    t_start = time.time()

    # ---- the picture -----------------------------------------------------
    source_name, problem, age = "TEST IMAGE", None, None
    lum = None
    if args.source != "test":
        frames, stamps, meta, age, problem = load_goes(args.cache_dir,
                                                       args.product)
        if frames is not None:
            k = args.frame
            if not (0 <= k < len(frames)):
                k = pick_frame(frames, args.gamma)
            lum = resample(luminance(frames[k]), w, h)
            sat = str((meta or {}).get("sat", "GOES"))
            sector = str((meta or {}).get("sector", "")).upper()
            source_name = ("%s %s" % (sat, sector)).strip()
        elif args.source == "goes":
            # Asked for the satellite explicitly and it is not there. Still no
            # card -- the panel says TEST IMAGE, which is true and is more use
            # than an error.
            pass
    if lum is None:
        lum = test_image(w, h)

    src = stretch(lum, 1.0, 99.0, args.gamma)

    # ---- the caption everything shares -----------------------------------
    # Three states in one line. Fresh: the satellite and how old the frame is,
    # all dim. Stale: the same, with the age in red and the word STALE, which
    # is the goes.py convention and the only honest way to leave two-day-old
    # cloud on a wall. Absent: TEST IMAGE, which says what it is without
    # pretending anything is wrong with the demo.
    parts = [(source_name, C_DIM)]
    if age is not None and lum is not None:
        if not ftdata.is_fresh(args.product, age):
            parts.append(("STALE " + ftdata.describe_age(age), C_WARN))
        else:
            parts.append((ftdata.describe_age(age), C_DIM))
    line = "  ".join(s for s, _ in parts)

    # ---- the four quantisers, once ---------------------------------------
    fields = [None] * 5
    timings = {}
    t0 = time.time()
    fields[FS] = diffuse(src, FS_KERNEL[0], FS_KERNEL[1], args.serpentine)
    timings["floyd-steinberg"] = time.time() - t0
    t0 = time.time()
    fields[ATK] = diffuse(src, ATK_KERNEL[0], ATK_KERNEL[1], args.serpentine)
    timings["atkinson"] = time.time() - t0
    t0 = time.time()
    fields[BAYER] = bayer_dither(src)
    fields[THRESH] = threshold_dither(src)
    timings["bayer+threshold"] = time.time() - t0

    # ---- inked, captioned panels at 1:1 ----------------------------------
    panels = []
    for i in range(5):
        if i == CONT:
            p = ink_field(src, levels_255=True)
        else:
            p = ink_field(fields[i])
        panels.append(caption(p, STAGE_NAMES[i], i, parts, ""))

    # ---- the punch-in ----------------------------------------------------
    # Integer magnifications only, and the window is chosen once so 2x and 4x
    # frame the same piece of picture.
    zoom = {}
    kz = 4 if args.zoom_factor not in (2, 4) else args.zoom_factor
    dw, dh = max(8, w // kz), max(4, h // kz)
    zx, zy = detail_window(src, w, h, dw, dh)
    if args.zoom:
        # Half-scale window is centred on the same middle so the step in feels
        # like one movement rather than two unrelated crops.
        hw, hh = max(8, w // 2), max(4, h // 2)
        hx = int(min(max(0, zx + dw // 2 - hw // 2), max(0, w - hw)))
        hy = int(min(max(0, zy + dh // 2 - hh // 2), max(0, h - hh)))
        for stage in (ATK, BAYER, THRESH):
            f = fields[stage]
            tag4 = "%dX" % kz
            p4 = caption(ink_field(magnify(f, zx, zy, dw, dh, kz)),
                         STAGE_NAMES[stage], stage, parts, tag4)
            p2 = caption(ink_field(magnify(f, hx, hy, hw, hh, 2)),
                         STAGE_NAMES[stage], stage, parts, "2X")
            zoom[(stage, kz)] = p4
            zoom[(stage, 2)] = p2

    # ---- the timeline ----------------------------------------------------
    # A list of (duration, incoming panel, outgoing panel, direction). A hold
    # is a segment whose two panels are the same, which means render() has one
    # code path and no special cases.
    wipe, hold = max(0.4, args.wipe), max(0.0, args.hold)
    zwipe = wipe * 0.78          # magnified wipes cross less picture per pixel
    segs = []

    def add(dur, panel, d=1):
        segs.append((dur, panel, panel, d))

    # Act one: down the ladder at 1:1, alternating direction so the boundary
    # squeegees back and forth instead of snapping back to the left edge.
    d = 1
    for i in range(4):
        segs.append((wipe, panels[i + 1], panels[i], d))
        add(hold, panels[i + 1], d)
        d = -d

    if args.zoom and zoom:
        # Act two: step into the threshold panel -- the least interesting one
        # magnified, which is the joke, because there is nothing there to see
        # -- and then wipe back up through Bayer to Atkinson at 4x where the
        # difference between an ordered matrix and error diffusion is finally
        # visible.
        add(hold * 0.7, zoom[(THRESH, 2)], d)
        add(hold * 1.0, zoom[(THRESH, kz)], d)
        segs.append((zwipe, zoom[(BAYER, kz)], zoom[(THRESH, kz)], d))
        add(hold, zoom[(BAYER, kz)], d)
        d = -d
        segs.append((zwipe, zoom[(ATK, kz)], zoom[(BAYER, kz)], d))
        add(hold * 1.6, zoom[(ATK, kz)], d)
        # And back out, to the same Atkinson panel at 1:1.
        add(hold * 0.7, zoom[(ATK, 2)], d)
        add(hold * 0.7, panels[ATK], d)
        d = -d

    # Close the loop: back to continuous tone, which is where t=0 starts, so
    # the segment can be cut anywhere and repeat without a seam.
    segs.append((wipe, panels[CONT], panels[ATK if args.zoom and zoom
                                             else THRESH], d))
    add(hold * 1.4, panels[CONT], d)

    starts = []
    acc = 0.0
    for dur, _, _, _ in segs:
        starts.append(acc)
        acc += max(1e-3, dur)
    cycle = acc

    out = np.empty((h, w, 3), np.uint8)
    build_ms = (time.time() - t_start) * 1000.0

    if args.stats:
        print("dither: source %s%s" % (source_name,
                                       "" if problem is None
                                       else " (%s)" % problem))
        for k2 in ("floyd-steinberg", "atkinson", "bayer+threshold"):
            print("  %-16s %7.1f ms" % (k2, timings[k2] * 1000.0))
        print("  %-16s %7.1f ms" % ("build total", build_ms))
        print("  cycle %.1f s in %d segments, detail window %dx%d at (%d,%d)"
              % (cycle, len(segs), dw, dh, zx, zy))
        mean = src.mean()
        for i, nm in enumerate(STAGE_NAMES):
            if i == CONT:
                continue
            got = fields[i].mean() * 255.0
            print("  %-16s mean %6.2f  (source %6.2f, error %+6.2f)"
                  % (nm.lower(), got, mean, got - mean))

    def render(t, frame):
        """Copy the left part of one baked panel and the right part of another.

        Three numpy calls on a normal frame, two on a hold, and no arithmetic
        at all. Everything that could be computed has been.
        """
        u = t % cycle
        i = bisect.bisect_right(starts, u) - 1
        if i < 0:
            i = 0
        dur, new, old, d = segs[i]
        if new is old:
            np.copyto(out, new)
            return out
        p = smoothstep(min(1.0, max(0.0, (u - starts[i]) / dur)))
        x = int(round(p * w))
        if d > 0:
            b = x
            left, right = new, old
        else:
            b = w - x
            left, right = old, new
        if b <= 0:
            np.copyto(out, right)
            return out
        if b >= w:
            np.copyto(out, left)
            return out
        out[:, :b] = left[:, :b]
        out[:, b:] = right[:, b:]
        out[:, b] = C_EDGE
        return out

    # Tests reach in here for the timeline and the fields; nothing else does.
    render.cycle = cycle
    render.segs = segs
    render.starts = starts
    render.panels = panels
    render.zoom = zoom
    render.fields = fields
    render.source = src
    render.source_name = source_name
    render.problem = problem
    render.age = age
    render.caption_line = line
    render.build_ms = build_ms
    render.detail = (zx, zy, dw, dh)
    return render


def main():
    # 20 fps: nothing here is smoother than a hard-edged boundary stepping one
    # column at a time, and at 20 fps a 3.2-second crossing moves the edge five
    # columns a frame, which reads as a slide rather than a stutter.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
