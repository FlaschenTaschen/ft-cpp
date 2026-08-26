#!/usr/bin/env python3
"""An aquarium. Just fish.

Every other panel on this wall is trying to tell you something. This one is
not. It is a tank: a big slow one crossing the whole panel while a cloud of
tiny ones flickers around it, weed swaying at the bottom, bubbles going up in
irregular strings, light rippling from above. There is nothing to read and
nothing to learn, which in a loud workshop is worth more than another chart.

Three things carry it, and everything else is furniture.

**The fish are analytic, not sprites that were drawn.** A fish is a centre line
and a half-height, both functions of one coordinate `u` running from the tail
tip to the nose. The swimming motion is a travelling sine added to the *centre
line* -- amplitude growing towards the tail, phase running head to tail -- so
the body flexes rather than sliding. That is the entire animation, and because
the wave is baked into the shape rather than applied to finished pixels, a
tail-beat frame costs nothing at run time: build() rasterises six phases and
render() picks one.

**A fish that turns is a fish that gets narrower.** The horizontal path is
`cx + Ax*(sin θ + a bit of sin 3θ)`, so velocity is `cos`-shaped: the fish
decelerates into the edge, hangs, and comes back. To *see* that as a turn
rather than as a reversal, each fish is also baked at several horizontal
squashes, and the frame picks one from the sign and size of dx/dt. Full
profile while cruising, a sliver head-on at the turn. It is the cheapest
possible three-quarter view and it is most of the character.

**Depth is sort order and nothing else.** Every fish carries a z in 0..1 that
sets its size, its speed, how far its colour is hazed toward the water, and
where it lands in the back-to-front blit. Four or five effective planes cost
one `sort()` in build() and transform the thing.

Purity: `render` is a pure function of `t`. A shoal is naturally an update
loop, which would desync the instant the scheduler built a segment ahead, so
every path here is closed form -- a base drift plus a couple of sinusoids with
per-fish frequencies drawn once from `--seed`. The frequencies are mutually
irrational, so the tank never actually repeats; the only periodic event is the
visitor, something large and slow that wanders in every `--visitor` seconds
and leaves. The one fish that is a jerk chases by *evaluating another fish's
closed form at t minus a lag* and blending towards it, which is an interaction
without a shred of state.

Cost is sprites per frame, so `--fish` is the knob: each sprite fish is about
six numpy calls, the whole shoal is fifteen regardless of how many there are,
and the tank around them is thirty.

Run:  python3 fish.py --host 127.0.0.1
      python3 fish.py --fish 14 --shoal 60      # busier tank
      python3 fish.py --fish 5 --no-shoal       # the cheap tank
      python3 fish.py --visitor 60              # see the big one sooner
"""

import math
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * math.pi

SUB = 3          # baked horizontal subpixel phases (see premul())
PH = 6           # baked tail-beat phases
MAX_SQUASH_STEP_PX = 2.0   # widest one-step change in sprite width, in pixels
WEED_PH = 24     # baked weed sway phases

# The water. Not black: fish on pure black read as fish in space, and the LED
# panel has plenty of headroom down here. A very dark blue-green with the
# surface lit from above is enough to say "tank".
WATER_TOP = (7, 34, 52)
WATER_MID = (4, 20, 34)
WATER_BOT = (2, 11, 19)
CAUSTIC_RGB = (38, 96, 120)      # the ripple from the surface, added
SAND_RGB = (44, 38, 26)

# Fish colourways: (back, flank, belly, fin). Saturated, because a hazed
# saturated colour still reads at three metres and a hazed pastel does not.
STYLES = (
    ((120, 44, 6), (255, 132, 20), (255, 206, 120), (255, 160, 60)),   # goldfish
    ((10, 44, 96), (60, 150, 240), (190, 232, 255), (110, 190, 255)),  # blue tang
    ((96, 84, 8), (236, 214, 60), (255, 250, 190), (240, 226, 110)),   # yellow
    ((78, 12, 60), (208, 60, 150), (255, 190, 226), (232, 110, 180)),  # magenta
    ((16, 70, 46), (52, 176, 118), (188, 248, 214), (96, 210, 156)),   # green
    ((92, 30, 16), (216, 92, 44), (255, 190, 150), (236, 130, 80)),    # rusty
)
# The jerk gets its own colourway so it is identifiable across the tank.
JERK_STYLE = ((120, 12, 4), (255, 70, 20), (255, 188, 120), (255, 120, 40))
VISITOR_STYLE = ((10, 18, 26), (44, 66, 84), (128, 152, 170), (52, 76, 94))


# --------------------------------------------------------------------------
# Sprite bakery.
#
# Everything a fish can look like is rasterised here. render() never draws a
# curve -- it indexes this table and blits.
# --------------------------------------------------------------------------

def premul(rgb, alpha):
    """(rgb*a, 1-a) at SUB horizontal subpixel phases, with one pad column.

    Same trick as grove.py's trunks, and for the same reason: a fish drifting
    at four pixels a second steps once every five frames if it is placed on
    integer columns, and that judder is exactly what the smooth body wave is
    there to avoid. Interpolating has to happen in premultiplied alpha or the
    silhouette bleeds toward black, and it has to happen at build time or it
    costs as much as the blit.
    """
    rgb = np.concatenate([rgb, np.zeros_like(rgb[:, :1])], axis=1)
    alpha = np.concatenate([alpha, np.zeros_like(alpha[:, :1])], axis=1)
    p = rgb * alpha[..., None]
    ps, ias = [], []
    for s in range(SUB):
        k = f32(s) / SUB
        ps.append(((1 - k) * p + k * np.roll(p, 1, axis=1)).astype(f32))
        a = (1 - k) * alpha + k * np.roll(alpha, 1, axis=1)
        ias.append((1.0 - a).astype(f32)[..., None])
    return np.stack(ps), np.stack(ias)


def fish_sprite(length, amp, style, phase, squash, haze, water, detail=True):
    """One fish, as (rgb, alpha) floats of shape (h, w) / (h, w, 3).

    `u` runs 0 at the tail tip to 1 at the nose, facing right. The shape is two
    curves per column -- an upper and a lower limit in rows -- and the fill
    between them, antialiased by a half-pixel ramp. Writing it that way means
    the swimming wave is just an offset added to both limits, which is why a
    tail beat is free: it changes the geometry, not the pixels of a finished
    sprite.

    `squash` compresses the body horizontally. A fish seen three-quarters on is
    a narrower fish, so the same generator at squash 0.2 is the head-on view at
    the top of a turn, and the transition through the squash levels reads as a
    bank. It is a cheat and it works better than it has any right to.
    """
    w = max(3, int(round(length * squash)))
    h = max(5, int(math.ceil(2.9 * amp)) + 4)
    u = (np.arange(w, dtype=f32) + 0.5) / w
    Y = np.arange(h, dtype=f32)[:, None]
    ymid = (h - 1) * 0.5

    UF = 0.30                                    # everything left of this is fin

    # The wave. Amplitude grows towards the tail and the phase runs head to
    # tail, so the fish looks like it is pushing water backwards rather than
    # wagging. A fish whose head moves as much as its tail reads as a leaf.
    sweep = (0.42 * amp) * (0.04 + (1.0 - u) ** 1.9)
    yc = ymid + sweep * np.sin(TAU * (phase + 0.95 * (1.0 - u)))

    # How steeply the centre line is running, per column. This matters more
    # than it sounds: the peduncle is two pixels thick, and a two-pixel band
    # measured *vertically* along a 45-degree path is barely one pixel wide on
    # screen, so the tail visibly detaches from the body at the extremes of the
    # beat. Inflating the half-thickness by sec(slope) is the same correction a
    # stroked path needs, and it is what keeps the fish in one piece.
    dy = np.empty_like(yc)
    dy[1:-1] = 0.5 * (yc[2:] - yc[:-2])
    dy[0] = yc[1] - yc[0]
    dy[-1] = yc[-1] - yc[-2]
    sec = np.sqrt(1.0 + dy * dy)

    # Body half-height: a teardrop s**0.55 * (1-s)**0.30, normalised. The two
    # exponents are the whole shape -- below one they give a blunt head and a
    # thin peduncle, and the peak lands about two thirds of the way forward,
    # which is where a fish is actually deepest. An earlier version used
    # sin(pi*s**k)**0.62 and produced a flat-topped lozenge: a fish with no
    # shoulder reads as a leaf however good the tail is.
    s = np.clip((u - UF) / (1.0 - UF), 0.0, 1.0)
    prof = s ** 0.55 * (1.0 - s) ** 0.30
    hb = amp * prof / 0.4325                     # analytic peak of that product
    # The floor is in *pixels* as well as in amp: 0.16 of a small fish's depth
    # is under half a pixel, the stalk antialiases away to nothing and the tail
    # comes off. This is the bug the sprite-connectivity check exists for.
    hb = np.maximum(hb, max(0.16 * amp, 0.62)) * sec

    # Fins. The dorsal is a short sail over the shoulder, the anal a smaller
    # one under the belly, and the pectoral a paddle behind the gill -- that
    # last one costs three pixels and is most of what says "fish" rather than
    # "fish-shaped thing" at this size.
    dorsal = 0.62 * amp * np.clip(np.sin(np.pi * (s - 0.20) / 0.40), 0.0, 1.0) ** 1.3
    anal = 0.34 * amp * np.clip(np.sin(np.pi * (s - 0.05) / 0.26), 0.0, 1.0) ** 1.2
    pect = 0.40 * amp * np.clip(np.sin(np.pi * (s - 0.62) / 0.24), 0.0, 1.0) ** 1.4
    body_top = yc - (0.88 * hb + dorsal)
    body_bot = yc + (1.12 * hb + np.maximum(anal, pect))

    # Caudal fin: a flare from the peduncle to the tip, forked by cutting a
    # wedge out of the middle. The fork is what makes a five-pixel tail read as
    # a tail and not as a smudge.
    v = np.clip((UF - u) / UF, 0.0, 1.0)
    fin_half = amp * (0.24 + 1.05 * v ** 0.90) * sec
    in_fin = u < UF
    top = np.where(in_fin, np.minimum(body_top, yc - fin_half), body_top)
    bot = np.where(in_fin, np.maximum(body_bot, yc + fin_half), body_bot)

    alpha = np.clip(np.minimum(bot - Y, Y - top) + 0.5, 0.0, 1.0)
    if amp >= 3.0:
        cut = np.where(in_fin, np.clip(0.62 * amp * (v - 0.55) / 0.45, 0.0, 99.0), 0.0)
        alpha *= 1.0 - np.clip(cut - np.abs(Y - yc) + 0.5, 0.0, 1.0)

    # Shading: one scalar down the body through a ramp, dark back to pale
    # belly. Doing it as a scalar field keeps the colourway in one place and is
    # far cheaper than three channels of arithmetic.
    back, flank, belly, fin = style
    lut = ds.gradient([(0.00, back), (0.36, back), (0.60, flank),
                       (0.92, belly), (1.00, belly)], 64, dtype=f32)
    q = 0.5 + 0.5 * (Y - yc) / np.maximum(1.7 * hb, 0.6)
    rgb = lut[np.clip(q * 63.0, 0, 63).astype(np.int32)]

    # Fins are thinner, paler and semi-transparent: they are the part of a fish
    # you can see through, and letting the water come through them is most of
    # what stops the silhouette looking die-cut.
    finmask = ((Y < body_top) | (Y > body_bot) | in_fin[None, :]).astype(f32)
    rgb += (np.asarray(fin, f32) - rgb) * finmask[..., None] * 0.8
    alpha *= 1.0 - 0.30 * finmask

    # Vertical barring. A flat flank at this size is a lozenge; two or three
    # bars give the eye something to track as the fish crosses.
    if detail and amp >= 2.2:
        nbars = min(6.0, 2.0 + length / 16.0)
        bars = 1.0 + 0.20 * np.sin(TAU * (u * nbars + 0.15))
        rgb *= np.where(in_fin, 1.0, bars)[None, :, None]

    # The eye. One dark pixel with one bright pixel over its shoulder, and it
    # is the single most load-bearing pixel in the whole demo: without it a
    # small fish is a coloured dash.
    if detail and w >= 7 and h >= 6:
        ex = min(w - 2, int(round(0.84 * w)))
        ey = int(round(ymid + (yc[ex] - ymid) - 0.30 * amp))
        if 0 <= ey < h:
            rgb[ey, ex] = (12, 10, 14)
            alpha[ey, ex] = 1.0
            if ey - 1 >= 0:
                rgb[ey - 1, ex] = (225, 235, 245)
                alpha[ey - 1, ex] = 1.0

    # Depth. Distance eats contrast before it eats colour, so the haze is a mix
    # toward the water and a straight dim, applied in that order.
    rgb += (np.asarray(water, f32) - rgb) * haze
    rgb *= 1.0 - 0.35 * haze
    return np.clip(rgb, 0.0, 255.0).astype(f32), alpha.astype(f32)


def bake_fish(length, amp, style, haze, water, nsq, detail=True):
    """The whole variant table for one fish: [squash][tail phase] -> (p, ia).

    nsq odd; index 0 is full profile swimming left, the middle index is head
    on, the last is full profile swimming right. render() picks the index
    straight out of dx/dt, so the turn animates itself.
    """
    table = []
    for k in range(nsq):
        # Levels are uniform in *width*, not in velocity. The foreshortening
        # curve (the 0.85 exponent) lives in render()'s lookup instead, which
        # leaves the fish's dynamics exactly as they were while making every
        # step of the table the same number of pixels. Spaced the other way,
        # the steps bunch up at full profile and stretch out near head-on --
        # so the widest jump lands precisely where the turn is fastest, which
        # is the one place it shows.
        c = -1.0 + 2.0 * k / (nsq - 1)
        squash = 0.22 + 0.78 * abs(c)
        row = []
        for j in range(PH):
            rgb, a = fish_sprite(length, amp, style, j / float(PH), squash,
                                 haze, water, detail)
            if c < 0:                       # baked facing right; mirror for left
                rgb = rgb[:, ::-1].copy()
                a = a[:, ::-1].copy()
            row.append(premul(rgb, a))
        table.append(row)
    return table


def crab_sprite(scale, leg_phase, haze, water):
    """A small crab: a shell, two claws, and legs that shuffle. Mostly it sits.

    Deliberately drawn with hard pixels rather than the fishes' antialiasing --
    a crustacean on the sand should look chitinous next to all that soft
    swimming, and at eight pixels wide, antialiasing is just mud.
    """
    w = 4 * scale + 4
    h = 2 * scale + 3
    rgb = np.zeros((h, w, 3), f32)
    a = np.zeros((h, w), f32)
    shell = np.asarray((176, 62, 34), f32)
    dark = np.asarray((96, 28, 14), f32)
    cx = (w - 1) * 0.5
    X = np.arange(w, dtype=f32)[None, :]
    Y = np.arange(h, dtype=f32)[:, None]
    body = ((X - cx) / (1.9 * scale)) ** 2 + ((Y - 1.0 * scale) / (0.95 * scale)) ** 2
    a[:] = np.clip(1.2 - body, 0.0, 1.0)
    rgb[:] = shell
    rgb += (dark - shell) * np.clip((Y - 0.4 * scale) / max(scale, 1.0), 0, 1)[..., None]
    # Legs: three a side, dropping to the sand, offset by the shuffle phase.
    for side in (-1, 1):
        for i in range(3):
            lx = int(round(cx + side * (1.4 * scale + i * 0.8)))
            ly = h - 1 - ((i + leg_phase) % 2)
            if 0 <= lx < w and 0 <= ly < h:
                a[ly, lx] = 1.0
                rgb[ly, lx] = dark
    # Eyes on stalks.
    for dx in (-1, 1):
        ex = int(round(cx + dx))
        if 0 <= ex < w:
            a[0, ex] = 1.0
            rgb[0, ex] = (240, 240, 240)
    rgb += (np.asarray(water, f32) - rgb) * haze
    return np.clip(rgb, 0, 255).astype(f32), a


def make_background(rng, W, H, sand_rows):
    """Water gradient, murk, sand and rocks -- everything that never moves.

    All static, so it is one baked image and one copy per frame. The rocks are
    on it rather than blitted because nothing ever passes behind them.
    """
    y = np.arange(H, dtype=f32)[:, None]
    k = np.clip(y / max(H - 1.0, 1.0), 0.0, 1.0)
    lo = ds.gradient([(0.0, WATER_TOP), (0.55, WATER_MID), (1.0, WATER_BOT)],
                     256, dtype=f32)
    bg = np.broadcast_to(lo[np.clip(k * 255, 0, 255).astype(np.int32)[:, 0]][:, None, :],
                         (H, W, 3)).copy()

    # A slow horizontal variation so the water is not a flat ramp; at 320 wide
    # a perfectly even gradient reads as a backdrop rather than as a volume.
    x = np.arange(W, dtype=f32)[None, :]
    murk = (1.0 + 0.10 * np.sin(x * 0.017 + 0.6) + 0.06 * np.sin(x * 0.041 - 1.9))
    bg *= murk[..., None]

    # Sand: a soft ridge with grain. Rows below it are floor.
    top = H - sand_rows + 1.6 * np.sin(x[0] * 0.021 + 1.1) + 1.0 * np.sin(x[0] * 0.052)
    cover = np.clip(np.arange(H, dtype=f32)[:, None] - top[None, :] + 0.5, 0.0, 1.0)
    grain = 0.72 + 0.55 * rng.random((sand_rows + 4, W)).astype(f32) ** 2
    gfull = np.zeros((H, W), f32)
    gfull[-(sand_rows + 4):] = grain
    depth = np.clip((np.arange(H, dtype=f32)[:, None] - top[None, :]) / 6.0, 0, 1)
    sand = (np.asarray(SAND_RGB, f32) * (0.45 + 0.55 * gfull)[..., None]
            * (1.0 - 0.45 * depth)[..., None])
    bg += (sand - bg) * cover[..., None]

    # Rocks. Two or three lumps sitting on the sand, dark and cool so they read
    # as mass rather than as more sand, with a rim of surface light on top.
    for _ in range(rng.integers(3, 6)):
        rx = float(rng.uniform(0, W))
        rw = float(rng.uniform(9, 26))
        rh = float(rng.uniform(4, 9))
        base = float(H - sand_rows + rng.uniform(0.0, 3.0))
        d = ((x - rx) / rw) ** 2 + ((np.arange(H, dtype=f32)[:, None] - base) / rh) ** 2
        m = np.clip(1.25 - d, 0.0, 1.0) * (np.arange(H, dtype=f32)[:, None] <= base + 1)
        tone = np.asarray((30, 34, 38), f32) * float(rng.uniform(0.7, 1.35))
        lit = tone[None, None, :] * (1.0 + 0.9 * np.clip(
            (base - rh * 0.7 - np.arange(H, dtype=f32)[:, None]) / max(rh, 1.0), 0, 1))[..., None]
        bg += (lit - bg) * m[..., None]

    return bg


def make_weed(rng, W, H, sand_rows, count, water):
    """Weed along the bottom, baked at WEED_PH sway phases.

    A blade is a quadratic bend rooted on the sand, and the sway is a phase
    offset on that bend. Baking the phases rather than shearing at run time
    turns the whole bed into one slice-and-blend per frame; each blade only
    touches a handful of columns, so the bake is a few hundred tiny arrays
    rather than a few hundred full-width ones.
    """
    rows = min(H, sand_rows + 22)
    strip_p = []
    strip_ia = []
    # Weed grows in clumps, not as an evenly spaced palisade. An early version
    # scattered blades uniformly and the bottom of the panel read as a fence;
    # rooting three or four blades at a shared point, each with its own bend
    # and sway phase, is the difference between a fence and a plant.
    blades = []
    n_clump = max(3, count // 4)
    while len(blades) < count:
        cx0 = float(rng.uniform(-6, W + 6))
        scale = float(rng.uniform(0.5, 1.0)) ** 0.7
        for _ in range(int(rng.integers(2, 6))):
            if len(blades) >= count:
                break
            blades.append(dict(
                x=cx0 + float(rng.uniform(-2.5, 2.5)),
                h=(7.0 + 12.0 * scale) * float(rng.uniform(0.7, 1.15)),
                bend=float(rng.uniform(-4.0, 4.0)),
                wob=float(rng.uniform(2.0, 5.5)) * scale,
                ph=float(rng.uniform(0, 1)),
                rate=float(rng.choice([1.0, 1.0, 2.0])),
                thick=float(rng.uniform(0.7, 1.5)),
                tone=float(rng.uniform(0.5, 1.3)),
                root=float(rows - sand_rows + rng.uniform(0.0, 4.0)),
            ))
    del n_clump
    for p in range(WEED_PH):
        rgb = np.zeros((rows, W, 3), f32)
        alpha = np.zeros((rows, W), f32)
        theta = TAU * p / WEED_PH
        for b in blades:
            n = max(3, int(round(b["h"])))
            yy = np.arange(n, dtype=f32)
            g = yy / max(n - 1.0, 1.0)                # 0 at root, 1 at tip
            lean = b["bend"] * g * g + b["wob"] * math.sin(theta * b["rate"] + TAU * b["ph"]) * g ** 1.6
            cxs = b["x"] + lean
            y0 = int(round(b["root"] - n))
            x0 = int(math.floor(cxs.min())) - 2
            x1 = int(math.ceil(cxs.max())) + 3
            x0c, x1c = max(0, x0), min(W, x1)
            if x1c <= x0c:
                continue
            r0 = max(0, y0)
            r1 = min(rows, y0 + n)
            if r1 <= r0:
                continue
            sub = cxs[r0 - y0:r1 - y0][:, None]
            gg = g[r0 - y0:r1 - y0][:, None]
            xs = np.arange(x0c, x1c, dtype=f32)[None, :]
            half = b["thick"] * (1.0 - 0.55 * gg) + 0.25
            a = np.clip(half - np.abs(xs - sub) + 0.5, 0.0, 1.0)
            # Tip lighter, root darker: weed is lit from the surface too.
            col = (np.asarray((18, 74, 34), f32) * b["tone"]
                   * (0.45 + 0.85 * gg)[..., None])
            col = col + (np.asarray(water, f32) - col) * 0.18
            reg = alpha[r0:r1, x0c:x1c]
            crgb = rgb[r0:r1, x0c:x1c]
            np.copyto(crgb, col, where=(a > reg)[..., None])
            np.maximum(reg, a, out=reg)
        strip_p.append((rgb * alpha[..., None]).astype(f32))
        strip_ia.append((1.0 - alpha).astype(f32)[..., None])
    return rows, strip_p, strip_ia


def make_shafts(rng, W, H, strength):
    """Light coming down through the surface, as a baked wide additive strip.

    Same construction as grove.py's sun shafts: soft edges are baked, never
    blurred per frame. Scrolling is a slice, so the whole lighting scheme is
    one add.
    """
    W2 = 2 * W
    x = np.arange(W2, dtype=f32)[None, :]
    y = np.arange(H, dtype=f32)[:, None]
    beam = np.zeros((H, W2), f32)
    for _ in range(6):
        cx = float(rng.uniform(0, W2))
        wd = float(rng.uniform(7, 22))
        slope = float(rng.uniform(-0.35, 0.35))
        amt = float(rng.uniform(0.35, 1.0))
        d = (x - cx - slope * y) / wd
        beam += amt * np.exp(-1.6 * d * d)
    # Strong at the surface, gone by two thirds down: the tank is deep and the
    # light does not reach the sand.
    fall = np.clip(1.0 - y / (0.72 * H), 0.0, 1.0) ** 1.6
    beam *= fall
    return (beam[..., None] * np.asarray(CAUSTIC_RGB, f32) * strength).astype(f32)


# --------------------------------------------------------------------------
# Closed-form motion.
#
# There is no simulation anywhere in this file. Every position is a function of
# t alone, so render(t) is exact at any t and the scheduler can build a segment
# ahead and start it at t=0 without the tank jumping.
# --------------------------------------------------------------------------

def _path(fi, t):
    """(x, y) for one sprite fish at time t.

    Horizontal is a sinusoid plus a third harmonic: pure `sin` cruises and
    turns at a constant-ish rate, and the harmonic flattens the middle so the
    fish spends most of its time crossing and turns briskly at the ends.
    """
    th = fi["w"] * t + fi["ph"]
    # Warping the *phase* rather than adding a harmonic. Both flatten the
    # middle of the crossing and sharpen the ends, but `sin(th + a*sin 2th)`
    # has derivative `cos(...) * (1 + 2a cos 2th)`, and with a < 0.5 the second
    # factor never changes sign -- so the fish turns exactly twice a period.
    # An added third harmonic gave four and six zero crossings for some phases,
    # which on the panel is a fish flickering round mid-cruise for no reason.
    x = fi["cx"] + fi["ax"] * math.sin(th + 0.28 * math.sin(2.0 * th + fi["d3"]))
    y = (fi["cy"] + fi["ay"] * math.sin(fi["wy"] * t + fi["phy"])
         + 0.42 * fi["ay"] * math.sin(2.7 * fi["wy"] * t + fi["phy2"]))
    return x, y


def _pursuit(fi, victim, t):
    """The jerk's path: its own patrol, blended towards where the victim was.

    A chase without any state. `lock` is a slow oscillator squashed to 0..1, so
    the jerk drifts about minding itself, latches onto a specific fish, harries
    it for twenty seconds or so and lets go. The lag means it arrives where the
    victim *was*, which is what makes it look like a pursuit rather than like
    two fish glued together.
    """
    ox, oy = _path(fi, t)
    g = math.sin(TAU * t / fi["lock_p"] + fi["lock_ph"])
    lock = min(1.0, max(0.0, (g - 0.25) / 0.45))
    lock = lock * lock * (3.0 - 2.0 * lock)
    if lock <= 0.0:
        return ox, oy
    vx, vy = _path(victim, t - fi["lag"])
    return ox + (vx - ox) * lock, oy + (vy - oy) * lock


def _visitor_path(fi, t, W):
    """Something large and slow that crosses now and then, and is gone.

    Off screen it costs nothing at all, which is the point: a fish this big
    would be the most expensive blit in the frame if it were always there.
    """
    u = (t % fi["period"]) / fi["cross"]
    if u > 1.0:
        return None
    span = W + 2.4 * fi["len"]
    x = -1.2 * fi["len"] + span * u if fi["dir"] > 0 else W + 1.2 * fi["len"] - span * u
    y = fi["cy"] + fi["ay"] * math.sin(TAU * u * 1.7 + fi["phy"])
    return x, y


# --------------------------------------------------------------------------

def blit(acc, p, ia, y, x):
    """Composite a premultiplied sprite over the accumulator, clipped."""
    H, W = acc.shape[:2]
    h, w = ia.shape[:2]
    sy0 = -y if y < 0 else 0
    sx0 = -x if x < 0 else 0
    sy1 = h - (y + h - H) if y + h > H else h
    sx1 = w - (x + w - W) if x + w > W else w
    if sy1 <= sy0 or sx1 <= sx0:
        return
    d = acc[y + sy0:y + sy1, x + sx0:x + sx1]
    d *= ia[sy0:sy1, sx0:sx1]
    d += p[sy0:sy1, sx0:sx1]


def add_arguments(ap):
    ap.add_argument("--fish", type=int, default=10,
                    help="sprite fish, the cost knob: each is ~6 numpy calls")
    ap.add_argument("--shoal", type=int, default=44,
                    help="tiny far fish, drawn vectorised at flat cost")
    ap.add_argument("--no-shoal", dest="shoal_on", action="store_false")
    ap.add_argument("--bubbles", type=int, default=26,
                    help="bubbles in flight, in a few strings")
    ap.add_argument("--weed", type=int, default=52, help="blades along the bottom")
    ap.add_argument("--no-weed", dest="weed_on", action="store_false")
    ap.add_argument("--no-crab", dest="crab_on", action="store_false")
    ap.add_argument("--visitor", type=float, default=210.0,
                    help="seconds between visits by the big slow one; 0 = never")
    ap.add_argument("--speed", type=float, default=1.0, help="overall pace")
    ap.add_argument("--caustics", type=float, default=1.0,
                    help="strength of the light rippling from above")
    ap.add_argument("--no-dither", dest="dither", action="store_false")
    ap.add_argument("--seed", type=int, default=7, help="which tank")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    sand_rows = max(4, H // 9)
    water_mid = np.asarray(WATER_MID, f32)

    bg = make_background(rng, W, H, sand_rows)
    shafts = make_shafts(rng, W, H, args.caustics)

    # ---- the fish --------------------------------------------------------
    # Depth is drawn from a power law rather than uniformly: a tank with an
    # even spread of sizes has no foreground, and the whole effect is one big
    # slow fish in front of a lot of small quick ones.
    fishes = []
    n = max(0, args.fish)
    for i in range(n):
        z = float(rng.random() ** 1.6)                     # 0 far .. 1 near
        length = 7.0 + 42.0 * z ** 1.25
        # Depth is roughly a seventh of length. Deeper than that and the fish
        # is a leaf; shallower and it is an eel. It is the one proportion the
        # eye checks without being asked.
        amp = 1.15 + 5.6 * z ** 1.2
        haze = float(np.clip(0.62 - 0.60 * z, 0.02, 0.66))
        # Near fish are faster in pixels per second *and* cover more of the
        # panel; far ones potter about in a small patch, which is exactly what
        # perspective does to a constant real speed.
        w0 = (0.030 + 0.075 * (1.0 - z)) * float(rng.uniform(0.75, 1.35)) * args.speed
        # Kept short enough that most fish turn *on screen*. A wider sweep
        # is more natural but hides the one moment that has any character in
        # it, which is the fish deciding to go back the other way.
        ax = W * (0.26 + 0.22 * z) * float(rng.uniform(0.85, 1.2))
        # Vertical range is solved from the sprite's own height against the
        # surface and the sand, not picked and hoped for: a big fish given a
        # small fish's band swims half out of the top of the tank, and the
        # frame it does that in is not the frame anyone screenshots.
        sprite_h = max(5, int(math.ceil(2.9 * amp)) + 4)
        half = sprite_h * 0.5 + 1.0
        lo, hi = half, (H - sand_rows) - half
        if hi <= lo:
            lo = hi = 0.5 * (H - sand_rows)
        ay = min((2.0 + 7.0 * float(rng.random())) * (0.4 + 0.8 * z),
                 (hi - lo) / 2.84)
        cy = lo + 1.42 * ay + float(rng.random()) * max(hi - lo - 2.84 * ay, 0.0)
        fishes.append(dict(
            z=z, len=length, amp=amp, haze=haze,
            cx=W * 0.5 + float(rng.uniform(-0.08, 0.08)) * W, ax=ax,
            w=TAU * w0, ph=float(rng.uniform(0, TAU)), d3=float(rng.uniform(0, TAU)),
            cy=cy, ay=ay,
            wy=TAU * float(rng.uniform(0.02, 0.075)) * args.speed,
            phy=float(rng.uniform(0, TAU)), phy2=float(rng.uniform(0, TAU)),
            # Tail beat, in turns per second, modulated by the swim oscillator
            # so the fish beats hardest mid-cruise and idles into the turn.
            beat=(2.6 - 1.5 * z) * float(rng.uniform(0.85, 1.2)) * args.speed,
            style=STYLES[i % len(STYLES)], jerk=False,
        ))

    # One fish is a jerk. It is a mid-depth fish, saturated red, faster than
    # anything its size, and it periodically decides to harass a specific
    # neighbour. No text is needed to explain it; people work it out.
    victim = None
    if n >= 2:
        # Pick a mid-sized victim: chasing the tiniest fish is invisible.
        order = sorted(range(n), key=lambda i: fishes[i]["z"])
        j = order[len(order) // 2]
        victim = fishes[order[max(0, len(order) // 2 - 1)]]
        f = fishes[j]
        f.update(jerk=True, style=JERK_STYLE, beat=f["beat"] * 1.55,
                 w=f["w"] * 1.45, lock_p=float(rng.uniform(46.0, 62.0)),
                 lock_ph=float(rng.uniform(0, TAU)), lag=float(rng.uniform(0.40, 0.85)),
                 victim=victim)
        if victim is f:                                     # never chase itself
            f["victim"] = fishes[order[0]]

    # Bake. The turn is quantised to this table, so what matters is not the
    # fish's size class but how many *pixels* of width one step moves. Sizing
    # the table by size class had it exactly backwards: it gave the big fish
    # the fewest levels, so a 37 px fish crossed the panel in five steps and
    # changed width by sixteen pixels at a time -- a snap, not a turn, and on
    # the most visible fish in the tank.
    #
    # So solve for the step instead. The widest sprite is `length` and the
    # narrowest is 0.22 of it, and the table covers that range *twice* -- once
    # swimming left and once swimming right -- so each half gets (nsq-1)/2
    # intervals and holding the step to MAX_SQUASH_STEP_PX needs
    # 2*(0.78*length/step)+1 levels. Seven stays as a floor: below about a
    # 13 px fish the step is already inside the target and more levels would be
    # baking sprites nobody can tell apart.
    for f in fishes:
        per_side = int(math.ceil(0.78 * f["len"] / MAX_SQUASH_STEP_PX))
        nsq = max(7, 2 * per_side + 1)
        f["nsq"] = nsq
        f["tab"] = bake_fish(f["len"], f["amp"], f["style"], f["haze"],
                             water_mid, nsq, detail=f["amp"] >= 2.0)
        f["vref"] = f["ax"] * f["w"] * 1.05

    # The visitor: one very large, very slow silhouette. It never turns on
    # screen, so it needs no squash table at all.
    visitor = None
    if args.visitor > 0:
        vlen = min(W * 0.27, 84.0)
        visitor = dict(
            len=vlen, amp=6.4, period=max(40.0, args.visitor),
            cross=max(30.0, min(0.42 * args.visitor, 85.0)) / max(args.speed, 0.05),
            dir=1 if rng.random() < 0.5 else -1,
            cy=H * float(rng.uniform(0.30, 0.52)), ay=H * 0.09,
            phy=float(rng.uniform(0, TAU)), beat=0.42 * args.speed, z=0.55,
        )
        rgbs = []
        for j in range(PH):
            rgb, a = fish_sprite(vlen, 6.4, VISITOR_STYLE, j / float(PH),
                                 1.0, 0.22, water_mid, detail=True)
            if visitor["dir"] < 0:          # baked facing right; mirror for left
                rgb = rgb[:, ::-1].copy()
                a = a[:, ::-1].copy()
            rgbs.append(premul(rgb, a))
        visitor["tab"] = [rgbs]

    # ---- the shoal -------------------------------------------------------
    # Tiny far fish. Three pixels each, drawn as three vectorised scatters, so
    # forty of them cost the same fifteen numpy calls as four would. Sprites
    # would be forty blits, which is the whole frame budget.
    ns = max(0, args.shoal) if args.shoal_on else 0
    sh = {}
    if ns:
        # They travel as two or three loose clouds, not as forty independent
        # specks. Each cloud shares a frequency and a phase and every member
        # gets a small offset on top, so the group moves together and still
        # boils inside itself. Scattering them uniformly -- which is what this
        # did first -- looks exactly like dust on the panel.
        ng = max(2, ns // 15)
        gi = rng.integers(0, ng, ns)
        gcx = (W * (0.2 + 0.6 * rng.random(ng))).astype(f32)
        gax = (W * rng.uniform(0.16, 0.40, ng)).astype(f32)
        gw = (TAU * rng.uniform(0.045, 0.10, ng) * args.speed).astype(f32)
        gph = rng.uniform(0, TAU, ng).astype(f32)
        gcy = (H * (0.14 + 0.58 * rng.random(ng))).astype(f32)
        sh["cx"] = (gcx[gi] + rng.uniform(-11, 11, ns)).astype(f32)
        sh["ax"] = (gax[gi] * rng.uniform(0.82, 1.18, ns)).astype(f32)
        sh["w"] = (gw[gi] * rng.uniform(0.94, 1.06, ns)).astype(f32)
        sh["ph"] = (gph[gi] + rng.uniform(-0.32, 0.32, ns)).astype(f32)
        sh["cy"] = np.clip(gcy[gi] + rng.uniform(-6, 6, ns), 2, H - 12).astype(f32)
        sh["ay"] = (H * rng.uniform(0.02, 0.11, ns)).astype(f32)
        sh["wy"] = (TAU * rng.uniform(0.05, 0.20, ns) * args.speed).astype(f32)
        sh["phy"] = rng.uniform(0, TAU, ns).astype(f32)
        sh["beat"] = (TAU * rng.uniform(2.2, 4.6, ns) * args.speed).astype(f32)
        # Hazed towards the water: these are the furthest things in the tank
        # and must never compete with a foreground fish.
        base = np.asarray([(160, 190, 205), (190, 200, 150), (200, 165, 175)], f32)
        pick = rng.integers(0, 3, ns)
        col = base[pick] * rng.uniform(0.45, 0.95, ns).astype(f32)[:, None]
        sh["col"] = (col + (water_mid - col) * 0.45).astype(f32)
        sh["tail"] = (sh["col"] * 0.62).astype(f32)

    # ---- bubbles ---------------------------------------------------------
    # Two or three vents, each producing an irregular string. Irregular is the
    # word that matters: evenly spaced bubbles read as a dotted line, so the
    # phases inside a string are jittered and the rise rates differ.
    nb = max(0, args.bubbles)
    bub = {}
    if nb:
        vents = rng.uniform(0.08, 0.92, max(2, nb // 9)) * W
        vi = rng.integers(0, len(vents), nb)
        bub["x0"] = (vents[vi] + rng.uniform(-2.5, 2.5, nb)).astype(f32)
        bub["rate"] = (rng.uniform(0.055, 0.16, nb) * args.speed).astype(f32)
        bub["ph"] = rng.random(nb).astype(f32)
        bub["wob"] = rng.uniform(0.8, 3.2, nb).astype(f32)
        bub["ww"] = (TAU * rng.uniform(0.25, 0.8, nb)).astype(f32)
        bub["wp"] = rng.uniform(0, TAU, nb).astype(f32)
        bub["big"] = rng.random(nb) < 0.22
        bub["top"] = f32(1.0)
        bub["bot"] = f32(H - sand_rows + 1)
        bub["col"] = np.clip(np.asarray((150, 200, 225), f32)
                             * rng.uniform(0.5, 1.0, nb).astype(f32)[:, None],
                             0, 255).astype(f32)

    # ---- crab ------------------------------------------------------------
    crab = None
    if args.crab_on:
        cs = 2
        crab = dict(
            x0=float(rng.uniform(0.15, 0.85)) * W,
            span=float(rng.uniform(24, 70)),
            period=float(rng.uniform(29.0, 41.0)) / max(args.speed, 0.05),
            move=0.30,                     # fraction of the period it walks in
            y=H - sand_rows - 2 * cs - 1,
            poses=[crab_sprite(cs, k, 0.18, water_mid) for k in range(2)],
        )
        crab["poses"] = [premul(r, a) for r, a in crab["poses"]]

    # ---- run-time buffers -------------------------------------------------
    if args.weed_on and args.weed > 0:
        weed_rows, weed_p, weed_ia = make_weed(rng, W, H, sand_rows, args.weed,
                                               water_mid)
    else:
        weed_rows, weed_p, weed_ia = 0, None, None

    acc = np.empty((H, W, 3), f32)
    out = np.empty((H, W, 3), np.uint8)
    caus = np.empty((H, W), f32)
    cy_grid = np.arange(H, dtype=f32)[:, None]
    cx_grid = np.arange(W, dtype=f32)[None, :]
    cy_buf = np.empty((H, 1), f32)
    cx_buf = np.empty((1, W), f32)
    # Caustics are a surface effect; fade them out with depth or the sand ends
    # up rippling as hard as the water, which reads as a scanline artefact.
    caus_fade = (np.clip(1.0 - cy_grid / (0.80 * H), 0.0, 1.0) ** 1.3
                 * args.caustics).astype(f32)
    caus_rgb = np.asarray(CAUSTIC_RGB, f32) * 0.22

    b = np.zeros((1, 1), f32)
    while b.shape[0] < 8:
        b = np.block([[4 * b, 4 * b + 2], [4 * b + 3, 4 * b + 1]])
    b /= b.size
    dith = np.stack([np.roll(b, k * 3, axis=1) for k in range(3)], axis=-1)
    dith = np.tile(dith, (H // 8 + 1, W // 8 + 1, 1))[:H, :W, :].astype(f32)
    if not args.dither:
        dith = np.zeros_like(dith)
    # The dither offset is folded into the baked background rather than added
    # as its own whole-frame pass. On the Pi the frame cost is dominated by how
    # many times the 320x64x3 buffer is walked, and this removes one walk for
    # nothing. The price is that an opaque sprite overwrites the offset, so the
    # fish themselves are undithered -- which is fine, because the banding this
    # is here to kill is in the big smooth water gradient, not in a twelve-row
    # fish drawn through a 64-entry ramp.
    bg = bg + dith

    # Sorted back to front once. This is the whole of the depth handling.
    order = sorted(range(len(fishes)), key=lambda i: fishes[i]["z"])

    def render(t, frame):
        # Background and the light coming down through the surface, in one
        # pass: `copy then add` walks the frame twice for the same answer.
        off = int(11.0 * args.speed * t) % W
        np.add(bg, shafts[:, off:off + W], out=acc)

        # Caustic ripple: the outer product of two travelling waves, clipped to
        # its positive half. The clip is what makes it look like caustics and
        # not like a plaid: real surface light is bright veins on an even
        # ground, never symmetric bright and dark blotches, and the negative
        # lobe was punching black holes in the water.
        np.sin(0.34 * cy_grid + 1.05 * t, out=cy_buf)
        np.sin(0.29 * cx_grid - 0.62 * t, out=cx_buf)
        np.multiply(cy_buf, cx_buf, out=caus)
        np.add(caus, 0.75 * np.sin(0.104 * cx_grid + 0.34 * t + 1.4 * cy_buf),
               out=caus)
        np.clip(caus, 0.0, 2.0, out=caus)
        np.multiply(caus, caus_fade, out=caus)
        np.add(acc, caus[..., None] * caus_rgb, out=acc)

        # --- shoal, behind everything -----------------------------------
        if ns:
            th = sh["w"] * t + sh["ph"]
            x = sh["cx"] + sh["ax"] * np.sin(th)
            y = sh["cy"] + sh["ay"] * np.sin(sh["wy"] * t + sh["phy"])
            # Direction straight out of the derivative; no state, no sign flap.
            d = np.where(np.cos(th) >= 0.0, 1, -1)
            xi = np.clip(x.astype(np.int32), 1, W - 3)
            yi = np.clip(y.astype(np.int32), 1, H - 2)
            wag = (np.sin(sh["beat"] * t) > 0).astype(np.int32)
            flat = acc.reshape(-1, 3)
            base = yi * W + xi
            flat[base] = sh["col"]
            flat[base + d] = sh["col"]
            flat[(yi + wag) * W + xi - 2 * d] = sh["tail"]

        # --- the visitor, mid depth --------------------------------------
        if visitor is not None:
            pv = _visitor_path(visitor, t, W)
            if pv is not None:
                vx, vy = pv
                ph = int(visitor["beat"] * t * PH) % PH
                p, ia = visitor["tab"][0][ph]
                xf = vx
                xi = int(math.floor(xf))
                s = int((xf - xi) * SUB) % SUB
                blit(acc, p[s], ia[s],
                     int(round(vy - ia.shape[1] * 0.5)), xi)

        # --- sprite fish, back to front ----------------------------------
        for i in order:
            f = fishes[i]
            if f["jerk"]:
                x, y = _pursuit(f, f["victim"], t)
                x2, _ = _pursuit(f, f["victim"], t + 0.05)
            else:
                x, y = _path(f, t)
                x2, _ = _path(f, t + 0.05)
            vx = (x2 - x) * 20.0

            # Velocity picks the squash level, and that is the turn. Clamped to
            # +-1 so a fish is at full profile for most of its crossing and
            # only narrows near the ends, where it is actually turning.
            c = vx / f["vref"]
            c = 1.0 if c > 1.0 else (-1.0 if c < -1.0 else c)
            # The 0.85 warp is applied here rather than baked into the table's
            # spacing, so the table stays uniform in width (see bake_fish) and
            # the foreshortening still eases the way it always did.
            m = math.copysign(abs(c) ** 0.85, c)
            k = int(round((m + 1.0) * 0.5 * (f["nsq"] - 1)))

            # Tail phase: a base rate plus a term in sin(2*theta), so the beat
            # speeds up mid-cruise and slows at both turning points. Written in
            # closed form rather than integrated, which is what keeps render()
            # pure.
            th = f["w"] * t + f["ph"]
            turns = f["beat"] * t + 0.14 * math.sin(2.0 * th)
            ph = int(turns * PH) % PH

            p, ia = f["tab"][k][ph]
            xi = int(math.floor(x))
            s = int((x - xi) * SUB) % SUB
            blit(acc, p[s], ia[s], int(round(y - ia.shape[1] * 0.5)), xi)

        # --- crab: mostly does nothing -----------------------------------
        if crab is not None:
            u = (t % crab["period"]) / crab["period"]
            # A long sit, then a scuttle one way; the next period it goes back.
            leg = (u % (2 * crab["move"])) / crab["move"]
            if u < crab["move"]:
                g = 0.5 - 0.5 * math.cos(math.pi * (u / crab["move"]))
                walking = True
            elif u > 1.0 - crab["move"]:
                g = 0.5 + 0.5 * math.cos(math.pi * ((u - 1.0 + crab["move"])
                                                    / crab["move"]))
                walking = True
            else:
                g = 1.0
                walking = False
            cx = crab["x0"] + crab["span"] * g
            pose = int(t * 7.0) % 2 if walking else 0
            p, ia = crab["poses"][pose]
            xi = int(math.floor(cx))
            s = int((cx - xi) * SUB) % SUB
            blit(acc, p[s], ia[s], crab["y"], xi)
            del leg

        # --- weed, in front of the fish that swim near the floor ----------
        if weed_p is not None:
            wp = int((t * 0.11 * args.speed) * WEED_PH) % WEED_PH
            reg = acc[H - weed_rows:]
            reg *= weed_ia[wp]
            reg += weed_p[wp]

        # --- bubbles ------------------------------------------------------
        if nb:
            u = np.mod(bub["rate"] * t + bub["ph"], 1.0)
            by = bub["bot"] + (bub["top"] - bub["bot"]) * u
            # A bubble accelerates as it rises and wobbles as it goes; both are
            # what stop a string looking like a dashed line.
            bx = bub["x0"] + bub["wob"] * np.sin(bub["ww"] * t + bub["wp"] + 5.0 * u)
            xi = np.clip(bx.astype(np.int32), 0, W - 2)
            yi = np.clip(by.astype(np.int32), 0, H - 2)
            flat = acc.reshape(-1, 3)
            base = yi * W + xi
            flat[base] = bub["col"]
            big = base[bub["big"]]
            bc = bub["col"][bub["big"]] * 0.72
            flat[big + 1] = bc
            flat[big + W] = bc

        np.clip(acc, 0.0, 255.0, out=acc)
        np.copyto(out, acc, casting="unsafe")
        return out

    # The tank itself, for the test script: none of this can be checked from a
    # screenshot, and several of the ways this demo can go quietly wrong -- a
    # fish that never turns, a chase that never happens, a visitor that never
    # arrives -- are about the parameters rather than the pixels.
    render.tank = dict(fishes=fishes, order=order, visitor=visitor, shoal=sh,
                       crab=crab, sand_rows=sand_rows, W=W, H=H)
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
