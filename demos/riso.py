#!/usr/bin/env python3
"""A Risograph duplicator printing, seen along the paper path.

A Riso is a stencil duplicator: one drum per ink colour, and a multi-colour
print means running the same sheet through the machine once for every colour.
Its entire visual identity comes from that one fact. Each pass lands a pixel or
two off from the last, so edges fringe and overlaps shift, and the inks are
soy-based and semi-transparent, so where two of them land on top of each other
they multiply -- fluorescent pink over blue is purple, and the whole secondary
palette comes out of the physics rather than out of a colour picker.

The representation
------------------
The artwork is never an image. It is **N separate ink channels**, one per
colour, each a coverage map in 0..1 over the printable area of the sheet, and
"rendering" is compositing those channels with a per-pass integer offset and a
multiply blend:

    sheet = paper
    for each channel k:
        sheet[shift(screen(cov_k), dy_k, dx_k)] *= (1 - d) + d * ink_k / 255

Everything else falls out of that. Misregistration is the shift. Overprint
colour is the multiply -- there is no table of "pink over blue is purple"
anywhere in this file, it just happens. And the state of the sheet after k
passes is the k-th partial product, so `build()` bakes the whole cumulative
stack once, K+1 finished sheet images, and a frame that is halfway through
laying the third colour is literally `cum[3]` on the far side of the nip --
the part of the sheet that has already gone under the drum -- and `cum[2]` on
the near side. Two blits. That is the whole print engine.

Halftone
--------
Riso screens are coarse and openly visible -- a real one is around 45 degrees
and you can count the dots -- so every channel is thresholded through an angled
dot screen before it is composited, each channel on its own angle the way a
real separation is, to keep the passes from moiring against each other. Flat
areas stay flat (coverage 1 beats every threshold), and a ramp turns into a
visible field of dots, which is what makes the print read as printed rather
than as a drawn rectangle.

The machine
-----------
The panel is the paper path, left to right: feed deck, the ink drum in the
middle, catch tray at the right. A sheet slides out of the feed deck, passes
under the drum, and the ink wipes onto it *at the nip* -- the column where the
drum touches the paper. The nip is a fixed column and the paper is what moves,
so the sheet's leading (right) edge is inked first and the seam between the new
colour and the old sweeps backwards across the sheet, from its leading edge to
its trailing one, while the sheet itself slides rightwards. Then it sits in the
catch tray long enough to be looked at, whips back to the feed deck, and goes
again. Between passes the drum is swapped (it drops out of frame and comes back
a different colour) and a new master is burnt on the thermal head, because on a
real Riso every colour needs both.

Colour
------
The inks are the real published Riso colours by their actual hex values.  Not
all of them survive an LED wall: multiply is a darkening operator, so a dark
ink over anything is nearly black, and Federal Blue, Black, Teal and Purple are
excluded from the default set for that reason (--inks will put them back). The
passes are also ordered lightest-first, which is what a real print shop does
and which here keeps the last, darkest colour reading as type on top of a
field rather than as mud.

Determinism
-----------
The job list -- artwork, inks, and the registration offset of every pass -- is
drawn once in build() from --seed and baked into a timeline. render() is a pure
function of t: it finds which segment t lands in and evaluates that segment.

Run:  python3 riso.py --host 127.0.0.1
      python3 riso.py --art poster --seed 7
      python3 riso.py --misreg 4 --screen 6      # sloppy registration, coarse
      python3 riso.py --inks pink,federal,black  # authentic, and much too dark
"""

import sys

import numpy as np

import demoscene as ds

f32 = np.float32


# --------------------------------------------------------------------------
# The ink drawer.
#
# These are the published Riso ink colours, by their documented hex values. The
# `dark` flag is not editorial -- it is what governs whether an ink can be used
# under multiply. A job is allowed at most one of them, and it always goes
# last.
# --------------------------------------------------------------------------

INKS = {
    # key         hex        label on the panel      dark under multiply?
    "pink":     ("FF48B0", "FLUOR PINK",  False),
    "orange":   ("FF6C2F", "ORANGE",      False),
    "red":      ("F15060", "BRIGHT RED",  False),
    "yellow":   ("FFE800", "YELLOW",      False),
    "green":    ("00A95C", "GREEN",       True),
    "blue":     ("0078BF", "BLUE",        True),
    "medblue":  ("3255A4", "MED BLUE",    True),
    "teal":     ("00838A", "TEAL",        True),
    "purple":   ("765BA7", "PURPLE",      True),
    "federal":  ("3D5588", "FEDERAL BLU", True),
    "black":    ("000000", "BLACK",       True),
}

# The ones that survive being multiplied together on a lit wall. Federal Blue,
# Black, Teal and Purple are real Riso inks and are left out on purpose: two of
# them over each other is a black rectangle at three metres.
DEFAULT_INKS = "yellow,pink,orange,red,green,blue,medblue"

# Unbleached Riso paper, not white. It matters: every ink is multiplied into
# it, so the paper is the top of the whole tonal range.
PAPER = (243, 238, 226)

# defcon.py's 3x5 font, the same one caiso, propagation, sort and tide draw
# with: five rows a glyph, each row an octal digit whose three bits are its
# three columns. Copied rather than imported so this module has no dependency
# on another demo.
_FONT = {
    "0": "75557", "1": "26227", "2": "71747", "3": "71717", "4": "55711",
    "5": "74717", "6": "74757", "7": "71222", "8": "75757", "9": "75717",
    "A": "25755", "B": "65656", "C": "34443", "D": "65556", "E": "74647",
    "F": "74644", "G": "34553", "H": "55755", "I": "72227", "J": "11152",
    "K": "55655", "L": "44447", "M": "57755", "N": "65555", "O": "25552",
    "P": "65644", "Q": "25573", "R": "65655", "S": "34216", "T": "72222",
    "U": "55557", "V": "55552", "W": "55775", "X": "55255", "Y": "55222",
    "Z": "71247", " ": "00000", "-": "00700", ".": "00002", ":": "02020",
    "/": "11244",
}

_GLYPHS = {}
for _ch, _rows in _FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g

GLYPH_H = 5


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((GLYPH_H * scale, 1), bool)
    out = np.zeros((GLYPH_H, len(s) * 4 - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def hex_rgb(h):
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def luma(rgb):
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


# --------------------------------------------------------------------------
# Artwork. Three separations, each generated here rather than traced from
# anything: a coverage map per ink channel over the printable area, which is
# the sheet inset by its unprintable margin.
#
# Every one of these is written to have a big flat area in one channel and
# something with an edge in another, because flat-over-flat is what shows the
# overprint colour and an edge is what shows the misregistration.
# --------------------------------------------------------------------------

def _text_into(cov, s, y, x, scale, value=1.0):
    m = text_mask(s, scale)
    gh, gw = m.shape
    h, w = cov.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(h, y + gh), min(w, x + gw)
    if y1 <= y0 or x1 <= x0:
        return
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    cov[y0:y1, x0:x1][sub] = value


def art_wordmark(ah, aw, YY, XX):
    """The Flaschen Taschen wordmark over a screened field, plus a bottle.

    Three separations: a graded field, the type, and the mark. The field is
    the one that shows the halftone -- it ramps from solid at the left to about
    a third at the right, so the same channel carries both a flat and a dot
    pattern.
    """
    field = np.clip(1.02 - 0.72 * (XX / max(1.0, aw - 1.0)), 0.0, 1.0)

    # Two lines of 3x5 type need 4*8*s - 2 columns and 2*5*s + 2 rows, so the
    # scale is measured off the plate rather than assumed. Assuming it is how
    # a past panel clipped the bottom off every capital E.
    sc = 2 if (aw >= 64 and ah >= 24) else 1
    lh = GLYPH_H * sc
    type_ = np.zeros((ah, aw), f32)
    _text_into(type_, "FLASCHEN", 1, 0, sc)
    _text_into(type_, "TASCHEN", 2 + lh, 0, sc)
    type_[ah - 3:ah - 1, 0:4 * 8 * sc - 2] = 1.0    # the rule under it

    mark = np.zeros((ah, aw), f32)
    bw = max(6, aw // 5)
    bx = aw - bw - 2
    body = max(4, int(ah * 0.55))
    mark[ah - 4 - body:ah - 4, bx:bx + bw] = 1.0            # bottle body
    mark[2:ah - 4 - body, bx + bw // 3:bx + bw - bw // 3] = 1.0   # neck
    mark[1:3, bx + bw // 4:bx + bw - bw // 4] = 1.0         # cap
    # Type last, so it gets the darkest ink of the job: the passes are ordered
    # lightest first, and a wordmark printed in the middle colour of three
    # disappears into the field behind it.
    return [field, mark, type_]


def art_poster(ah, aw, YY, XX):
    """A bold three-colour poster: disc, wordmark, frame and bar.

    The frame and the bar are the third pass on purpose. A rule that runs the
    whole way round the sheet is the most unforgiving possible registration
    target: two pixels of misregistration on a straight edge that long is
    impossible to miss.
    """
    cy, cx, r = 8.0, aw - 24.0, 8.5
    disc = ((YY - cy) ** 2 + (XX - cx) ** 2 <= r * r).astype(f32)
    # Three bars under the disc, fading, so this channel carries a tone ramp
    # as well as a solid.
    for i, (y0, x0, v) in enumerate(((ah - 8, aw - 40, 1.0),
                                     (ah - 5, aw - 34, 0.65),
                                     (ah - 2, aw - 28, 0.35))):
        disc[y0:y0 + 2, x0:aw - 2] = v

    sc = 3 if (aw >= 48 and ah >= 22) else (2 if aw >= 32 else 1)
    type_ = np.zeros((ah, aw), f32)
    _text_into(type_, "RISO", max(1, (ah - GLYPH_H * sc) // 2 - 3), 1, sc)

    rules = np.zeros((ah, aw), f32)
    rules[0:2, :] = 1.0
    rules[ah - 2:ah, :] = 1.0
    rules[:, 0:2] = 1.0
    rules[:, aw - 2:aw] = 1.0
    rules[ah - 12:ah - 10, 2:aw - 2] = 1.0
    return [disc, rules, type_]


def art_landscape(ah, aw, YY, XX):
    """Two inks, big flat shapes, and a sun deliberately behind a peak.

    The whole point of a two-colour job is the overlap, so the shapes are
    arranged to overlap a lot: a graded sky that gets *lighter* downward, a
    solid sun disc in the same channel, and ridge lines in the other that cut
    straight across both.
    """
    sky = np.clip(0.85 - 0.80 * (YY / max(1.0, ah - 1.0)), 0.0, 1.0)
    sky[((YY - 8.0) ** 2 + ((XX - aw * 0.34) * 0.9) ** 2) <= 7.0 ** 2] = 1.0
    sky[ah - 5:, :] = 0.0

    land = np.zeros((ah, aw), f32)
    base = ah - 4
    for px, ph, hw in ((aw * 0.30, 15.0, 22.0), (aw * 0.62, 21.0, 30.0),
                       (aw * 0.86, 11.0, 18.0)):
        ridge = base - ph * np.clip(1.0 - np.abs(XX - px) / hw, 0.0, 1.0)
        land[YY >= ridge] = 1.0
    land[base:, :] = 1.0
    return [sky, land]


ARTWORKS = {
    "wordmark": art_wordmark,
    "poster": art_poster,
    "landscape": art_landscape,
}

# The screen angle per pass. A real separation gives every colour its own angle
# so the dot grids do not beat against each other; 45 degrees is the classic
# "least visible" one and goes to the pass that carries the biggest flat.
SCREEN_ANGLES = (45.0, 15.0, 75.0, 0.0)


def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=1.0,
                    help="overall rate: paper travel and phase lengths")
    ap.add_argument("--misreg", type=int, default=2,
                    help="max registration error per pass, in pixels")
    ap.add_argument("--screen", type=float, default=4.6,
                    help="halftone screen pitch in pixels")
    ap.add_argument("--density", type=float, default=0.88,
                    help="ink opacity 0..1; 1 is a full multiply")
    ap.add_argument("--art", default="all",
                    help="comma list of %s, or 'all'" % ",".join(sorted(ARTWORKS)))
    ap.add_argument("--inks", default=DEFAULT_INKS,
                    help="comma list of Riso inks to draw jobs from")
    ap.add_argument("--seed", type=int, default=5,
                    help="picks the inks, the artwork order and every "
                         "registration offset")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    speed = max(0.15, float(args.speed))
    density = float(np.clip(args.density, 0.0, 1.0))
    pitch = max(2.0, float(args.screen))
    misreg = int(np.clip(args.misreg, 0, 6))

    order = ([k for k in ARTWORKS] if args.art.strip().lower() in ("all", "")
             else [a.strip() for a in args.art.split(",") if a.strip()])
    for a in order:
        if a not in ARTWORKS:
            raise SystemExit("riso: unknown artwork %r (have %s)"
                             % (a, ", ".join(sorted(ARTWORKS))))
    order = order or list(ARTWORKS)

    drawer = [k.strip() for k in args.inks.split(",") if k.strip()]
    for k in drawer:
        if k not in INKS:
            raise SystemExit("riso: unknown ink %r (have %s)"
                             % (k, ", ".join(sorted(INKS))))
    if len(drawer) < 2:
        raise SystemExit("riso: need at least two inks")

    # ----------------------------------------------------------------------
    # Geometry. The panel is the paper path; everything is a fraction of it so
    # a smaller wall is a smaller machine rather than a crop.
    # ----------------------------------------------------------------------
    base_h = 7 if H >= 56 else 5              # deck plus the readout strip
    y_deck = H - base_h                       # the deck the paper rides on
    y_sheet = int(round(H * 0.42))            # top edge of the sheet
    hs = y_deck - y_sheet                     # sheet height
    ws = max(24, int(round(W * 0.2625)))      # sheet width
    margin = 2 if hs >= 20 else 1             # Riso cannot print to the edge
    ah, aw = hs - 2 * margin, ws - 2 * margin

    x_feed = max(2, W // 80)                  # sheet at rest in the feed deck
    x_catch = W - ws - 4                      # ... and in the catch tray
    nip = W // 2                              # column where the drum touches
    r_drum = max(5, (y_sheet - 1) // 2)
    cy_drum = r_drum
    cx_drum = nip

    YY, XX = np.mgrid[0:ah, 0:aw].astype(f32)

    # ----------------------------------------------------------------------
    # The screen. One threshold field per pass index, on that pass's angle.
    #
    # The field is squeezed into (0.02, 0.98) rather than left at (0, 1): a
    # coverage of exactly 1.0 has to beat every threshold in the grid or a
    # solid picks up pinholes at the dot centres, and a coverage of 0.0 has to
    # lose to all of them or the paper gets speckled.
    # ----------------------------------------------------------------------
    def screen_field(angle_deg):
        a = np.radians(angle_deg)
        u = XX * np.cos(a) + YY * np.sin(a)
        v = -XX * np.sin(a) + YY * np.cos(a)
        k = 2.0 * np.pi / pitch
        th = 0.5 - 0.5 * np.cos(k * u) * np.cos(k * v)
        return (0.02 + 0.96 * th).astype(f32)

    SCREENS = [screen_field(a) for a in SCREEN_ANGLES]

    # Registration marks in every channel, at the corners of the printable
    # area. They are the tell: three passes means three sets of ticks a pixel
    # or two apart, which is exactly what a real misregistered print looks
    # like along its trim edge.
    def add_regmarks(cov):
        for cy_, cx_ in ((0, 0), (0, aw - 1), (ah - 1, 0), (ah - 1, aw - 1)):
            y0, y1 = max(0, cy_ - 2), min(ah, cy_ + 3)
            x0, x1 = max(0, cx_ - 2), min(aw, cx_ + 3)
            cov[y0:y1, cx_] = 1.0
            cov[cy_, x0:x1] = 1.0

    def compose(channels, inks, offsets):
        """The whole print engine: channels -> K+1 finished sheet images.

        cum[k] is the sheet after k passes, so a frame mid-pass is cum[k+1]
        left of the nip and cum[k] right of it and nothing has to be
        recomputed while the paper moves.
        """
        cur = np.empty((hs, ws, 3), f32)
        cur[:] = PAPER
        cums = []
        for k, cov in enumerate(channels):
            cums.append(np.clip(cur, 0, 255).astype(np.uint8))
            work = np.array(cov, f32)
            add_regmarks(work)
            dot = work > SCREENS[k % len(SCREENS)]
            # Place the separation on the sheet, offset by this pass's
            # registration error. Everything about misregistration is here.
            dy, dx = offsets[k]
            m = np.zeros((hs, ws), bool)
            sy0, sx0 = margin + dy, margin + dx
            ty0, tx0 = max(0, sy0), max(0, sx0)
            ty1 = min(hs, sy0 + ah)
            tx1 = min(ws, sx0 + aw)
            if ty1 > ty0 and tx1 > tx0:
                m[ty0:ty1, tx0:tx1] = dot[ty0 - sy0:ty1 - sy0,
                                          tx0 - sx0:tx1 - sx0]
            ink = np.array(hex_rgb(INKS[inks[k]][0]), f32)
            cur[m] *= (1.0 - density) + density * ink / 255.0
        out = np.clip(cur, 0, 255).astype(np.uint8)
        cums.append(out)
        # A hair of edge shading all round, so the sheet reads as an object
        # sitting on the deck rather than as a hole cut in the machine.
        for img in cums:
            img[0] = (img[0].astype(f32) * 0.88).astype(np.uint8)
            img[-1] = (img[-1].astype(f32) * 0.72).astype(np.uint8)
            img[:, 0] = (img[:, 0].astype(f32) * 0.88).astype(np.uint8)
            img[:, -1] = (img[:, -1].astype(f32) * 0.72).astype(np.uint8)
        return cums

    # ----------------------------------------------------------------------
    # The jobs, and the timeline. Drawn once, here, from the seed.
    # ----------------------------------------------------------------------
    def pick_inks(n):
        """n inks, at most one dark, ordered lightest first.

        Light-to-dark is how a print shop actually orders the passes -- the
        light ink cannot cover the dark one, so it has to go down first -- and
        it is also the only order in which the last pass still reads as type.
        """
        for _ in range(24):
            pick = list(rng.permutation(len(drawer))[:n])
            keys = [drawer[i] for i in pick]
            if sum(1 for k in keys if INKS[k][2]) <= 1:
                return sorted(keys, key=lambda k: -luma(hex_rgb(INKS[k][0])))
        return sorted(drawer[:n], key=lambda k: -luma(hex_rgb(INKS[k][0])))

    # Phase lengths, in seconds at --speed 1. The dwell on the last pass is the
    # long one on purpose: the finished print landing in the tray is the thing
    # this demo is for, and rushing past it wastes the whole build-up.
    D_LOAD0, D_LOAD = 3.8 / speed, 2.6 / speed
    D_PRINT = 5.0 / speed
    D_DWELL, D_DWELL_LAST = 1.6 / speed, 3.4 / speed
    D_RETURN, D_EJECT = 0.85 / speed, 1.1 / speed

    jobs = []
    segs = []                                  # (t0, dur, kind, job, pass)
    t_at = 0.0
    sheets = int(rng.integers(60, 900))         # the machine's running count
    spin = 0.0                                  # cumulative drum rotation
    prev_ink = None
    for j, name in enumerate(order):
        channels = ARTWORKS[name](ah, aw, YY, XX)
        k = len(channels)
        inks = pick_inks(k)
        # The one draw that makes this demo what it is. Pass 0 defines the
        # register -- it cannot be wrong, it is what "wrong" is measured
        # against -- and every pass after it misses by a pixel or two.
        offsets = [(0, 0)]
        for _ in range(k - 1):
            offsets.append((int(rng.integers(-misreg, misreg + 1)),
                            int(rng.integers(-misreg, misreg + 1))))
        cums = compose(channels, inks, offsets)

        # Text is baked per pass: three strings a pass and a handful of passes
        # a loop, so none of it costs anything at frame time.
        labels = []
        for p in range(k):
            short = INKS[inks[p]][1]
            labels.append((
                text_mask("MASTER %d/%d %s" % (p + 1, k, short)),
                text_mask("PASS %d/%d %s" % (p + 1, k, short)),
                text_mask("SHEETS %04d" % (sheets + p + 1)),
            ))
        jobs.append({
            "name": name, "inks": inks, "k": k, "offsets": offsets,
            "cums": cums, "labels": labels,
            "rgb": [hex_rgb(INKS[i][0]) for i in inks],
        })

        for p in range(k):
            d_load = D_LOAD0 if p == 0 else D_LOAD
            for kind, dur in (("load", d_load), ("print", D_PRINT),
                              ("dwell", D_DWELL_LAST if p == k - 1 else D_DWELL),
                              ("eject" if p == k - 1 else "back",
                               D_EJECT if p == k - 1 else D_RETURN)):
                segs.append((t_at, dur, kind, j, p, spin,
                             prev_ink if kind == "load" else None))
                # The drum idles slowly and runs at printing speed under the
                # paper. Baking the cumulative angle at each segment boundary
                # is what lets render() stay a pure function of t while the
                # rotation still looks continuous.
                spin += dur * (3.4 if kind == "print" else 0.75) * speed
                t_at += dur
            prev_ink = jobs[j]["rgb"][p]
        sheets += k

    total = t_at
    seg_t0 = np.array([s[0] for s in segs], f32)

    # ----------------------------------------------------------------------
    # Static background: chassis, thermal head bay, deck, trays.
    # ----------------------------------------------------------------------
    bg = np.zeros((H, W, 3), np.uint8)
    bg[:y_deck] = (13, 15, 20)                          # machine interior
    bg[0:2] = (46, 50, 62)                              # top cover
    bg[0] = (78, 84, 102)
    bg[y_deck:] = (20, 22, 29)                          # base casting
    bg[y_deck] = (96, 102, 118)                         # the deck itself
    bg[H - 1] = (38, 42, 52)

    # Feed deck at the left and catch tray at the right: a backstop at each
    # end and a lip the paper runs up against.
    bg[y_sheet + 4:y_deck, 0:2] = (58, 63, 76)
    bg[y_sheet + 4:y_deck, W - 2:W] = (58, 63, 76)
    bg[y_deck - 1, 2:x_feed + 2] = (70, 76, 90)
    bg[y_deck - 1, W - ws // 3:W - 2] = (70, 76, 90)

    # The thermal head bay: the master is burnt here before it goes on the
    # drum. A recessed slot with a rail.
    bay_x0 = 6
    bay_x1 = cx_drum - r_drum - 4
    bay_y = max(3, cy_drum - 6)
    bay_h = 11 if H >= 56 else 7
    bg[bay_y:bay_y + bay_h, bay_x0:bay_x1] = (26, 29, 37)
    bg[bay_y, bay_x0:bay_x1] = (54, 59, 72)
    bg[bay_y + bay_h - 1, bay_x0:bay_x1] = (44, 48, 60)
    bg[bay_y + 2, bay_x0 + 2:bay_x1 - 2] = (40, 44, 56)   # head rail

    # Ink cartridge to the right of the drum, with a pipe into the hub. Its
    # colour is the loaded ink, so it is drawn per frame, but the housing is
    # static.
    car_x0 = cx_drum + r_drum + 6
    car_x1 = min(W - 6, car_x0 + int(W * 0.13))
    car_y = max(2, cy_drum - 7)
    car_h = 13 if H >= 56 else 9
    bg[car_y:car_y + car_h, car_x0:car_x1] = (30, 33, 42)
    bg[car_y, car_x0:car_x1] = (58, 63, 76)
    bg[car_y + car_h - 1, car_x0:car_x1] = (44, 48, 60)
    bg[cy_drum - 1:cy_drum + 2, cx_drum + r_drum:car_x0 + 3] = (36, 40, 50)

    # The used-master bin. A Riso peels the old stencil off the drum and drops
    # it in a box, so by the third colour of a job there is a small pile of
    # inky crumpled masters in there -- which is both true and the only thing
    # on the panel that remembers the passes already run.
    bin_x0 = car_x1 + 6
    bin_x1 = W - 6
    bin_y = car_y + 1
    bin_h = car_h - 1
    if bin_x1 - bin_x0 >= 12:
        bg[bin_y:bin_y + bin_h, bin_x0:bin_x1] = (24, 27, 35)
        bg[bin_y, bin_x0:bin_x1] = (52, 57, 70)
        bg[bin_y + bin_h - 1, bin_x0:bin_x1] = (44, 48, 60)
        bg[bin_y:bin_y + bin_h, bin_x0] = (44, 48, 60)
        bg[bin_y:bin_y + bin_h, bin_x1 - 1] = (44, 48, 60)
    # Where each discarded master lies in the bin: a fixed scatter, drawn once
    # so a frame only has to decide how many of them are there yet.
    bin_slots = []
    _bw = max(4, (bin_x1 - bin_x0 - 4) // 4)
    for _i in range(4):
        _y = bin_y + bin_h - 3 - (_i % 2)
        _x = bin_x0 + 2 + _i * _bw
        bin_slots.append((_y, min(_x, bin_x1 - 3), min(_bw - 1, bin_x1 - 2 - _x)))

    # ----------------------------------------------------------------------
    # The drum, as a gather. The angle around the disc is quantised once into
    # NSEG segments; a frame adds the rotation, takes the modulus, and gathers
    # colours out of a (shade level, segment) table. Three numpy calls on a
    # 27x27 patch, and no trigonometry per frame.
    # ----------------------------------------------------------------------
    NSEG = 48
    dy_, dx_ = np.mgrid[-r_drum:r_drum + 1, -r_drum:r_drum + 1].astype(f32)
    dist = np.sqrt(dy_ * dy_ + dx_ * dx_)
    disc = dist <= r_drum + 0.3
    ang = np.arctan2(dy_, dx_)
    seg_idx = (np.floor((ang / (2.0 * np.pi) % 1.0) * NSEG)
               .astype(np.int32) % NSEG)
    # Lit from the upper left, plus a falloff towards the silhouette, so the
    # drum reads as a cylinder rather than as a coloured circle.
    lit = (0.62 + 0.30 * (-dy_ - dx_) / max(1.0, 2.0 * r_drum)
           + 0.22 * (1.0 - dist / max(1.0, float(r_drum))))
    shade_idx = np.clip((lit * 4.0).astype(np.int32), 0, 3)
    SHADE = np.array([0.46, 0.66, 0.84, 1.0], f32)

    rim = disc & (dist > r_drum - 2.0)
    hub = dist <= max(2.0, r_drum * 0.26)
    hub_hi = dist <= max(1.0, r_drum * 0.14)
    static_rgb = np.zeros((2 * r_drum + 1, 2 * r_drum + 1, 3), np.uint8)
    static_msk = rim | hub
    static_rgb[rim] = (120, 128, 148)
    static_rgb[hub] = (70, 76, 92)
    static_rgb[hub_hi] = (150, 158, 178)
    # The rim reads better with a shadowed underside.
    static_rgb[rim & (dy_ > r_drum * 0.35)] = (64, 68, 82)

    def drum_table(rgb):
        """(4, NSEG, 3): the master wrapped round the drum, per shade level.

        Forty of the forty-eight segments are the inked master, four are the
        clamp that holds it on, and the ribs every sixth segment are what makes
        the rotation legible -- a plain coloured disc spinning looks static.
        """
        base = np.empty((NSEG, 3), f32)
        col = np.array(rgb, f32)
        for s in range(NSEG):
            if s < 4:
                base[s] = (150, 156, 174)             # the clamp bar
            elif s < 8:
                base[s] = col * 0.42                  # the gap behind it
            else:
                base[s] = col * (0.72 if s % 6 == 0 else 1.0)
        tab = base[None, :, :] * SHADE[:, None, None]
        return np.clip(tab, 0, 255).astype(np.uint8)

    DRUMS = {}
    for jb in jobs:
        for rgb in jb["rgb"]:
            if rgb not in DRUMS:
                DRUMS[rgb] = drum_table(rgb)
    DRUM_EMPTY = drum_table((70, 74, 88))

    # ----------------------------------------------------------------------
    # Rollers and the deck belt. Both are baked; a frame picks a phase.
    # ----------------------------------------------------------------------
    rr = 3
    ry, rx = np.mgrid[-rr:rr + 1, -rr:rr + 1].astype(f32)
    rd = np.sqrt(ry * ry + rx * rx)
    rmask = rd <= rr + 0.2
    rang = np.arctan2(ry, rx)
    ROLLERS = []
    for ph in range(6):
        img = np.zeros((2 * rr + 1, 2 * rr + 1, 3), np.uint8)
        img[rmask] = (52, 57, 70)
        spoke = rmask & (np.cos(2.0 * (rang + ph * np.pi / 6.0)) > 0.72)
        img[spoke] = (104, 112, 132)
        ROLLERS.append(img)
    roller_x = [x for x in (int(W * 0.10), int(W * 0.28),
                            int(W * 0.70), int(W * 0.88))
                if abs(x - cx_drum) > r_drum + rr + 2]
    roller_y = y_sheet - rr - 1

    BELT = []
    cols = np.arange(W)
    for ph in range(8):
        BELT.append(((cols + ph) % 8) < 2)

    # ----------------------------------------------------------------------
    # Small blitters.
    # ----------------------------------------------------------------------
    out = np.zeros((H, W, 3), np.uint8)

    def paste(dst, y, x, img, msk=None):
        h, w = img.shape[:2]
        sy0, sx0 = max(0, -y), max(0, -x)
        ty0, tx0 = max(0, y), max(0, x)
        hh = min(h - sy0, dst.shape[0] - ty0)
        ww = min(w - sx0, dst.shape[1] - tx0)
        if hh <= 0 or ww <= 0:
            return
        src = img[sy0:sy0 + hh, sx0:sx0 + ww]
        if msk is None:
            dst[ty0:ty0 + hh, tx0:tx0 + ww] = src
        else:
            np.copyto(dst[ty0:ty0 + hh, tx0:tx0 + ww], src,
                      where=msk[sy0:sy0 + hh, sx0:sx0 + ww, None])

    def blit_mask(dst, y, x, m, rgb):
        h, w = m.shape
        y0, x0 = max(0, y), max(0, x)
        y1, x1 = min(dst.shape[0], y + h), min(dst.shape[1], x + w)
        if y1 <= y0 or x1 <= x0:
            return
        dst[y0:y1, x0:x1][m[y0 - y:y1 - y, x0 - x:x1 - x]] = rgb

    def smooth(u):
        return u * u * (3.0 - 2.0 * u)

    # The sheet's position through a printing pass, as three straight runs.
    #
    # A constant feed rate is what a real duplicator does, and it is the wrong
    # answer here: the whole point of the demo is the ink landing, and at a
    # constant rate the nip crosses the sheet in a third of the pass while the
    # remaining two thirds are a rectangle sliding to the right. So the run
    # under the drum takes about half the pass and the approach and the exit
    # split the rest. The knots are deliberately mild -- a factor of about 1.6
    # in speed reads as a machine easing through the nip, and much more than
    # that reads as dropped frames.
    def path_x(u):
        # leading edge reaches the nip; clamped so a narrow panel cannot make
        # this run backwards.
        x_nip0 = float(min(max(nip - ws, x_feed + 1), max(x_feed + 2, nip - 1)))
        if u < 0.25:
            v = x_feed + (x_nip0 - x_feed) * (u / 0.25)
        elif u < 0.72:
            v = x_nip0 + (nip - x_nip0) * ((u - 0.25) / 0.47)
        else:
            v = nip + (x_catch - nip) * ((u - 0.72) / 0.28)
        return int(round(v))

    # ----------------------------------------------------------------------
    def render(t, frame):
        tt = float(t) % total
        i = int(np.searchsorted(seg_t0, tt, side="right")) - 1
        if i < 0:
            i = 0
        t0, dur, kind, ji, pi, spin0, prev = segs[i]
        u = float(np.clip((tt - t0) / max(dur, 1e-6), 0.0, 1.0))
        job = jobs[ji]
        k = job["k"]
        ink_rgb = job["rgb"][pi]

        np.copyto(out, bg)

        # --- where the sheet is, and how much of this pass is on it --------
        #
        # `inked` is how many of the sheet's ws columns have been under the
        # drum, counted from the *leading* edge. The paper travels left to
        # right, so the leading edge is the sheet's RIGHT edge at x + ws and it
        # is the right-hand part of the sheet that carries the new colour. A
        # sheet-local column u sits at panel column x + u and has passed the
        # nip once x + u >= nip, i.e. u >= nip - x; so exactly
        # (x + ws) - nip columns are inked, clipped into 0..ws. Getting this
        # backwards is not a subtle error -- see README-fragment-riso.md.
        if kind == "load":
            x = x_feed
            inked = 0                       # nothing of this colour yet
            done = pi
        elif kind == "print":
            x = path_x(u)
            inked = int(np.clip(x + ws - nip, 0, ws))
            done = pi
        elif kind == "dwell":
            x, inked, done = x_catch, ws, pi
        elif kind == "back":
            x = int(round(x_catch + (x_feed - x_catch) * smooth(u)))
            inked, done = ws, pi
        else:                                # eject, off to the right
            x = int(round(x_catch + (W + 8 - x_catch) * (u ** 1.6)))
            inked, done = ws, k - 1

        # --- the sheet: two blits out of the baked cumulative stack --------
        # Trailing part first, still carrying only `done` passes; then the
        # leading part, which has been under the drum and carries done + 1.
        # The seam between them is at panel column x + dry, which during a
        # print is the nip itself.
        cums = job["cums"]
        dry = ws - inked
        if dry > 0:
            paste(out, y_sheet, x, cums[done][:, :dry])
        if inked > 0:
            paste(out, y_sheet, x + dry, cums[done + 1][:, dry:])

        # --- the deck, running while the paper moves -----------------------
        if kind in ("print", "back", "eject"):
            out[y_deck, BELT[int(tt * 22.0 * speed) % 8]] = (150, 158, 176)

        for j2, rxp in enumerate(roller_x):
            ph = int((spin0 + (tt - t0) * (3.4 if kind == "print" else 0.75)
                      * speed) * 6.0 + j2) % 6
            paste(out, roller_y - rr, rxp - rr, ROLLERS[ph], rmask)

        # --- ink cartridge and its feed pipe -------------------------------
        swapping = kind == "load" and u < 0.34
        drum_rgb = ink_rgb
        if swapping and prev is not None:
            # Mid-swap the old drum is still in the machine until it is clear
            # of the frame, which is when the colour changes.
            drum_rgb = prev if u < 0.17 else ink_rgb
        out[car_y + 3:car_y + car_h - 3, car_x0 + 3:car_x1 - 3] = drum_rgb
        out[car_y + 3, car_x0 + 3:car_x1 - 3] = tuple(
            min(255, int(c * 0.55 + 110)) for c in drum_rgb)
        out[car_y + car_h - 4, car_x0 + 3:car_x1 - 3] = tuple(
            int(c * 0.55) for c in drum_rgb)
        out[cy_drum, cx_drum + r_drum:car_x0 + 3] = drum_rgb

        # One discarded master per pass already run in this job.
        for q in range(min(pi, len(bin_slots))):
            by, bx, bw = bin_slots[q]
            if bw > 0:
                c = job["rgb"][q]
                out[by:by + 2, bx:bx + bw] = tuple(int(v * 0.62) for v in c)
                out[by, bx:bx + bw:2] = c

        # --- the drum ------------------------------------------------------
        # Drops out of the bottom of the frame and comes back, which is the
        # drum swap; a Riso really does hand you a different cylinder per
        # colour.
        drop = 0
        if swapping:
            drop = int(round(np.sin(np.pi * (u / 0.34)) * (2 * r_drum + 8)))
        spun = spin0 + (tt - t0) * (3.4 if kind == "print" else 0.75) * speed
        step = int(spun * NSEG / (2.0 * np.pi) * 0.9) % NSEG
        tab = DRUMS.get(drum_rgb, DRUM_EMPTY)
        face = tab[shade_idx, (seg_idx + step) % NSEG]
        np.copyto(face, static_rgb, where=static_msk[..., None])
        paste(out, cy_drum - r_drum + drop, cx_drum - r_drum, face, disc)

        # --- the thermal head, burning the master --------------------------
        if kind == "load" and u >= 0.34:
            b = (u - 0.34) / 0.66
            hx = int(round(bay_x0 + 3 + (bay_x1 - bay_x0 - 9) * b))
            # The burnt master grows behind the head, in the ink it will carry.
            out[bay_y + 5:bay_y + 8, bay_x0 + 3:max(bay_x0 + 4, hx)] = ink_rgb
            out[bay_y + 3:bay_y + 5, hx:hx + 6] = (255, 214, 120)
            out[bay_y + 2:bay_y + 3, hx:hx + 6] = (200, 208, 226)
            out[bay_y + 8, max(bay_x0 + 3, hx - 3):hx + 6] = (255, 150, 40)

        # --- the nip: where the ink actually lands -------------------------
        if kind == "print" and 0 < inked < ws:
            out[y_sheet:y_deck, nip] = ink_rgb
            out[y_sheet - 2:y_sheet, nip - 1:nip + 2] = ink_rgb

        # --- readout ------------------------------------------------------
        burn, pas, cnt = job["labels"][pi]
        lab = burn if kind == "load" else pas
        ty = y_deck + 2
        blit_mask(out, ty, 3, lab, ink_rgb)
        blit_mask(out, ty, W - 3 - cnt.shape[1], cnt, (118, 124, 142))
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
