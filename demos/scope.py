#!/usr/bin/env python3
"""A bench oscilloscope.

A 320x64 panel is a 5:1 letterbox, which is very nearly the aspect of a real
CRT scope screen with its 10x8 graticule stretched out. So: an etched
graticule, a swept trace crawling left to right and fading behind itself, a
trigger that mostly holds and occasionally does not, and every half minute or
so the timebase drops out and the thing flips to X-Y for Lissajous figures.

The one detail that separates a scope trace from a plotted line is that **the
beam moves at constant speed along the path, so a pixel's brightness is
inversely proportional to how fast the spot crossed it**. Peaks and flat tops
glow because the beam dwells there; steep edges go thin and dim because the
same charge is smeared over thirty rows. That falls out of the drawing method
for free rather than being a shading pass: the path is cut into equal-*time*
segments, each carrying the same charge, and each segment's charge is divided
between the pixels it crosses and accumulated with bincount. A flat top puts
three sub-samples into one pixel; a vertical edge puts one third of a
sub-sample into each of forty. Nothing computes dy/dx explicitly and nothing
is shaded — the histogram is the physics.

Persistence is one float32 accumulation buffer, decayed as a half-life in
*seconds* (`--persist`, default 0.42) so the tail is the same length in wall
time at 8 fps and at 30. At the default the trace has faded to a fifth of peak
by the time the beam has crossed the screen, which is what makes the sweep
read as a sweep rather than as a static plot. The buffer is mapped through a
phosphor ramp — P31 green, P3 amber, or the blue-white of a storage tube — and
the graticule is composited in the same index space as a baked static layer,
so it costs one uint8 maximum a frame and never changes.

It runs in about 5.8 ms a frame on the wall's Pi 3, which is throttled to
600 MHz. Two things got it there. The palette is gathered as packed 32-bit
pixels into an RGBA frame whose first three channels are handed back — one
four-byte word a pixel instead of three separate bytes, and 1.0 ms of the
2.3 ms that a row gather out of a (256, 3) palette costs on that machine. And
the beam samples are laid straight into the histogram when no segment jumps
more than a couple of rows, which is nearly always, so the machinery for
filling in a vertical edge only runs on the frames that contain one.

Content cycles through sine, a square with real ringing and overshoot, a
decaying exponential, an AM carrier, band-limited noise and a digital burst,
each locked to a trigger level so it stands still. Not all of them lock: the
exponential creeps right a few pixels a second the way a scope does when the
trigger is a hair off, and noise cannot be triggered at all and jumps about.

Run:  python3 scope.py --host 127.0.0.1
      python3 scope.py --phosphor amber --timebase 0.05 --persist 0.8
"""

import bisect
import math
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32
TAU = 2.0 * np.pi

# The phosphor buffer is carried in palette-index units, 0..255. 256 levels is
# what the panel drives anyway, and holding the buffer in the units the
# palette wants saves a whole-frame scale every frame.
PMAX = 255

# Phosphor ramps. The stops are bunched at the bottom on purpose: the whole
# interesting range of a scope trace is the dim end, since a fast edge lands
# two per cent of the way up and still has to be visible as a line rather than
# as nothing. The top end whitens rather than clipping to flat colour, which
# is what makes a dwelling spot read as overdriven.
P31 = [(0.00, (0, 0, 0)), (0.020, (0, 16, 5)), (0.060, (0, 40, 12)),
       (0.140, (2, 86, 24)), (0.300, (18, 152, 46)), (0.550, (74, 214, 100)),
       (0.780, (152, 244, 164)), (1.000, (226, 255, 232))]

P3 = [(0.00, (0, 0, 0)), (0.020, (18, 10, 0)), (0.060, (44, 22, 0)),
      (0.140, (96, 50, 0)), (0.300, (168, 96, 4)), (0.550, (230, 158, 26)),
      (0.780, (250, 206, 110)), (1.000, (255, 240, 206))]

P11 = [(0.00, (0, 0, 0)), (0.020, (4, 10, 20)), (0.060, (8, 24, 48)),
       (0.140, (18, 54, 104)), (0.300, (40, 104, 180)), (0.550, (92, 166, 236)),
       (0.780, (164, 212, 250)), (1.000, (232, 244, 255))]

PHOSPHORS = {"green": P31, "amber": P3, "blue": P11}

# Graticule and furniture, in phosphor units. Everything here is baked once.
G_DOT = 0.030          # dotted interior division lines
G_LINE = 0.052         # solid border and centre axes
G_TICK = 0.078         # 0.2-division ticks along the centre lines
G_TEXT = 0.105         # the s/div and V/div readouts
G_TRIG = 0.165         # the trigger level marker

SUB = 3.0              # beam sub-samples per pixel of path
HOLDOFF = 0.06         # retrace + holdoff, as a fraction of the sweep

# Charge spread over a long segment is divided by m**SPEED_EXP rather than by
# m. At a true 1/m a square edge lands at two per cent of the flat top and
# disappears entirely, leaving a row of disconnected bars; the exponent lifts
# it to about six per cent, a faint but continuous line, which is what the
# edge actually looks like on a scope once the spot's finite size is allowed
# for. It is a legibility fudge and it only touches segments longer than a
# pixel — everything gentler is exact, since there m == 1 and the brightness
# comes entirely from how the sub-samples pile up in the histogram.
SPEED_EXP = 0.74

SIGNALS = ("sine", "square", "expo", "am", "noise", "burst")

# Lissajous ratios, four to an X-Y visit, taken from a different quarter of
# this list each time so the second visit is not a rerun of the first.
#
# Each is detuned on Y by a few hundredths, which is what makes the figure
# precess instead of standing still — a static Lissajous reads as a printed
# shape rather than as two oscillators slipping past each other. The detuning
# has to shrink as the ratio gets busier: a 5:4 that precesses as fast as a
# 1:1 sweeps its whole envelope inside one phosphor half-life and fills the
# panel with solid green.
XY_RATIOS = ((1.0, 2.0, 0.020), (3.0, 2.0, -0.014), (2.0, 3.0, 0.010),
             (1.0, 1.0, 0.028), (1.0, 3.0, 0.016), (3.0, 4.0, -0.008),
             (2.0, 5.0, -0.011), (5.0, 4.0, 0.006))
XY_SUB = 4                                              # figures per X-Y visit


# --------------------------------------------------------------------------
# A 3x5 pixel font, same trick as defcon.py: five rows a glyph, each row an
# octal digit whose three bits are the three columns. TrueType at five pixels
# is mush, and the Pi does not have the faces this was written on.
# --------------------------------------------------------------------------

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


def _text(s):
    """A (5, 4n-1) float mask for a string; 1 px between glyphs."""
    s = s.upper()
    if not s:
        return np.zeros((5, 1), f32)
    out = np.zeros((5, len(s) * 4 - 1), f32)
    for i, ch in enumerate(s):
        rows = _FONT.get(ch, _FONT[" "])
        for r, digit in enumerate(rows):
            v = int(digit, 8)
            for c in range(3):
                if v & (4 >> c):
                    out[r, i * 4 + c] = 1.0
    return out


def _si_time(sec):
    """A per-division time as a scope would print it."""
    for scale, unit in ((1.0, "S"), (1e-3, "MS"), (1e-6, "US")):
        if sec >= scale:
            v = sec / scale
            return ("%g" % round(v, 2)) + unit + "/DIV"
    return ("%g" % round(sec / 1e-9, 2)) + "NS/DIV"


# --------------------------------------------------------------------------
# Signals. Each is baked once as a table of one screen width, so a frame costs
# one np.interp no matter how ugly the waveform is to generate, and so the
# whole demo is exactly reproducible from a seed.
#
# Every signal carries a whole number of cycles across the screen. That is not
# cosmetic: the table is sampled with wraparound once the trigger drifts, and
# a fractional cycle puts a step in the middle of an otherwise smooth trace.
# --------------------------------------------------------------------------

def _bake_signals(n, width, rng):
    u = np.linspace(0.0, 1.0, n, endpoint=False)
    per_px = n / float(max(1, width))
    out = {}

    out["sine"] = np.sin(TAU * 3.0 * u)

    # Square wave with the ringing a real edge has: an overshoot that decays
    # over about a twentieth of the screen. Sign it with the level so the
    # overshoot goes the way the edge went.
    ph = (2.0 * u) % 1.0
    level = np.where(ph < 0.5, 1.0, -1.0)
    since = ph % 0.5                                    # screens since the edge
    ring = 0.22 * np.exp(-since / 0.020) * np.cos(TAU * 44.0 * since)
    out["square"] = level * (1.0 + ring)

    # Four relaxation pulses: snap up, decay away.
    frac = (4.0 * u) % 1.0
    out["expo"] = 2.0 * np.exp(-frac / 0.16) - 1.0

    # AM: a 24-cycle carrier under a 2-cycle envelope, 70% depth.
    out["am"] = ((1.0 + 0.70 * np.sin(TAU * 2.0 * u))
                 * np.sin(TAU * 24.0 * u) / 1.70)

    # Band-limited noise. Smoothed circularly over about two thirds of a pixel
    # so it reads as grass rather than as a solid filled band.
    v = rng.standard_normal(n)
    k = 2 * int(round(per_px * 0.35)) + 1
    sm = np.zeros_like(v)
    for j in range(-(k // 2), k // 2 + 1):
        sm += np.roll(v, j)
    sm /= k
    out["noise"] = np.clip(sm / max(1e-6, np.abs(sm).max()) * 0.85, -1.0, 1.0)

    # A digital burst: 24 NRZ bits with idle either side and a finite rise
    # time, so the edges slope instead of being infinitely fast.
    nb = 24
    bits = rng.integers(0, 2, nb) * 2.0 - 1.0
    b = bits[np.minimum((u * nb).astype(int), nb - 1)]
    b = np.where((u < 0.17) | (u > 0.85), -1.0, b)
    sm = np.zeros_like(b)
    k = max(3, int(round(per_px * 2.5)))
    for j in range(-(k // 2), k // 2 + 1):
        sm += np.roll(b, j)
    out["burst"] = sm / (2 * (k // 2) + 1)

    return dict((name, y.astype(f32)) for name, y in out.items())


def _align(y, level):
    """Roll a table so column 0 is a rising crossing of the trigger level.

    This is the whole of the trigger: with the table rolled, a sweep that
    starts at table index 0 starts at the same point of the waveform every
    time, so a repetitive signal stands dead still. Drift is then a matter of
    adding an offset to the lookup rather than of anything stateful.
    """
    hit = np.flatnonzero((y < level) & (np.roll(y, -1) >= level))
    return np.roll(y, -int(hit[0])) if hit.size else y


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--phosphor", default="green",
                    choices=sorted(PHOSPHORS),
                    help="screen colour: P31 green, P3 amber, P11 storage blue")
    ap.add_argument("--timebase", type=float, default=0.10,
                    help="seconds per division; ten divisions make a sweep")
    ap.add_argument("--persist", type=float, default=0.42,
                    help="phosphor half-life in seconds, so the tail is the "
                         "same length in wall time at any --fps")
    ap.add_argument("--beam", type=float, default=0.92,
                    help="brightness a flat part of the trace reaches, 0..1")
    ap.add_argument("--amplitude", type=float, default=2.5,
                    help="signal amplitude in divisions (peak, not pp)")
    ap.add_argument("--trigger", type=float, default=0.20,
                    help="trigger level, in units of the signal amplitude")
    ap.add_argument("--dwell", type=float, default=7.0,
                    help="seconds spent on each waveform")
    ap.add_argument("--xy-dwell", type=float, default=13.0,
                    help="seconds spent in X-Y mode, twice a cycle")
    ap.add_argument("--signals", default=",".join(SIGNALS),
                    help="comma separated subset of %s" % ",".join(SIGNALS))
    ap.add_argument("--graticule", dest="graticule", action="store_true",
                    default=True, help="the 10x8 division grid")
    ap.add_argument("--no-graticule", dest="graticule", action="store_false")
    ap.add_argument("--readout", dest="readout", action="store_true",
                    default=True, help="s/div and V/div type, trigger marker")
    ap.add_argument("--no-readout", dest="readout", action="store_false")
    ap.add_argument("--seed", type=int, default=7,
                    help="seeds the noise and the burst pattern")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)

    cx, cy = (W - 1) * 0.5, (H - 1) * 0.5
    div_y = H / 8.0
    ampl = max(1.0, args.amplitude * div_y)
    # Clip the tables rather than clipping pixel indices every frame: the only
    # thing that can leave the panel is a signal drawn too big, and catching it
    # here costs nothing and keeps two np.clip calls out of the inner path.
    span = max(1.0, cy - 1.0)

    T = max(1e-3, args.timebase) * 10.0                 # seconds across
    sweep_len = T * (1.0 + HOLDOFF)
    v_ref = f32(W / T)                                  # sweep speed, px/s
    half_life = max(0.02, float(args.persist))
    beam = float(args.beam) * PMAX               # charge, in index units

    # ---- signal tables ---------------------------------------------------
    names = [s.strip() for s in args.signals.split(",") if s.strip()]
    bad = [s for s in names if s not in SIGNALS]
    if bad or not names:
        raise SystemExit("--signals: unknown %s" % (bad or "(empty)",))

    # Fifty samples a pixel, which is what lets the frame path sample the
    # table nearest-neighbour and skip the interpolation entirely.
    NT = 16384
    baked = _bake_signals(NT, W, rng)
    tables = {}
    for name in names:
        y = _align(baked[name], f32(args.trigger))
        # Baked in rows, and clipped here rather than clipping pixel indices
        # every frame: the only thing that can leave the panel is a signal
        # drawn too big, and catching it once keeps two np.clip calls out of
        # the inner path.
        tables[name] = (f32(cy) - np.clip(y * ampl, -span, span)).astype(f32)

    # How each signal fails, or does not, to hold the trigger. Drift is in
    # screens per second; noise instead jumps to a fresh place every sweep,
    # because there is nothing in it to lock onto.
    DRIFT = {"sine": 0.0, "square": 0.0, "expo": 0.030, "am": 0.0,
             "noise": 0.0, "burst": 0.0}
    jitter = (rng.random(512) * T).astype(f32)

    # ---- X-Y -------------------------------------------------------------
    # X runs at a wider volts/division than Y, which is a real setting and the
    # only way a Lissajous figure uses a panel five times wider than it is
    # tall. A circle comes out as an ellipse; so does a real scope's, whenever
    # the two channels are not matched.
    rx = f32(0.42 * W)
    ry = f32(min(2.8 * div_y, cy - 3.0))
    # Figures per second. Slower than it first was: the arc a frame has to
    # draw is proportional to it, and at 2.4 an X-Y frame was binning a
    # third of the panel. At 1.7 the whole figure is still lit — one figure
    # takes 0.59 s against a 0.42 s half-life — and the head is clearer.
    xy_rate = 1.7                                       # figures per second
    # Normalise each figure's brightness by its own mean beam speed, so a
    # dense 5:4 is not three times dimmer than a 1:1 ellipse. Slow parts of a
    # given figure still glow — that variation is the point.
    us = np.linspace(0.0, 1.0, 2048, endpoint=False)
    xy_norm = []
    for a, b, _ in XY_RATIOS:
        vx = TAU * a * rx * np.cos(TAU * a * us)
        vy = TAU * b * ry * np.cos(TAU * b * us)
        xy_norm.append(float(np.hypot(vx, vy).mean()) * xy_rate)

    # ---- schedule --------------------------------------------------------
    # Durations are whole numbers of sweeps, so a waveform never changes in the
    # middle of a trace; the picture is always one signal all the way across.
    def quant(sec):
        return max(1, int(round(sec / sweep_len))) * sweep_len

    dwell, xy_dwell = quant(args.dwell), quant(args.xy_dwell)
    segs = []
    half = (len(names) + 1) // 2
    for i, name in enumerate(names):
        segs.append(("yt", name, dwell))
        if i == half - 1 or i == len(names) - 1:
            if not segs or segs[-1][0] != "xy":
                segs.append(("xy", None, xy_dwell))
    starts = [0.0]
    visit = []                                          # X-Y visit number
    for mode, _, d in segs:
        visit.append(sum(1 for s in segs[:len(visit)] if s[0] == "xy"))
        starts.append(starts[-1] + d)
    cycle = starts[-1]

    def schedule(t):
        tc = math.fmod(t, cycle)
        i = bisect.bisect_right(starts, tc) - 1
        i = min(max(i, 0), len(segs) - 1)
        mode, name, _ = segs[i]
        # One dead sweep whenever the mode changes: the beam goes off, the
        # screen fades, and the new mode arrives on a clean field. Switching
        # straight from a sweep to a Lissajous with both still lit reads as a
        # fault rather than as a knob being turned. Not on the very first
        # segment of the very first cycle, though — a demo that opens on a
        # second of empty screen has already looked broken by the time it
        # starts.
        blank = (segs[i - 1][0] != mode and (tc - starts[i]) < sweep_len
                 and not (i == 0 and t < cycle))
        return mode, name, starts[i] + (t - tc), blank, visit[i]

    # ---- furniture -------------------------------------------------------
    def bake_furniture(xy):
        g = np.zeros((H, W), f32)
        if args.graticule:
            gx = np.rint(np.linspace(0, W - 1, 11)).astype(int)
            gy = np.rint(np.linspace(0, H - 1, 9)).astype(int)
            for x in gx[1:-1]:
                g[::2, x] = G_DOT
            for y in gy[1:-1]:
                g[y, ::2] = G_DOT
            g[gy[0], :] = g[gy[-1], :] = G_LINE
            g[:, gx[0]] = g[:, gx[-1]] = G_LINE
            mx, my = gx[5], gy[4]
            g[my, :] = G_LINE
            g[:, mx] = G_LINE
            # 0.2-division ticks along the horizontal centre line and
            # 0.5-division ones up the vertical: any finer than that and 64
            # rows turn the vertical axis into a solid bar.
            tx = np.rint(np.linspace(0, W - 1, 51)).astype(int)
            g[max(0, my - 1):my + 2, tx] = G_TICK
            ty = np.rint(np.linspace(0, H - 1, 17)).astype(int)
            g[ty, max(0, mx - 1):mx + 2] = G_TICK
        # The readouts live in the strip between the last division line and the
        # border. They are drawn only if the trace cannot reach that strip:
        # 64 rows have no room for a caption *over* a waveform, and a smaller
        # panel has none at all, in which case going without is better than
        # printing type through the signal.
        row = H - 7
        # 1.25 * ampl is where a square wave's overshoot actually reaches.
        if args.readout and row > cy + max(ampl * 1.25, ry):
            left = _text("X-Y" if xy else _si_time(args.timebase))
            right = _text("%dMV/DIV" % int(round(1000.0 / max(0.1, args.amplitude))))
            def put(mask, x):
                g[row:row + 5, x:x + mask.shape[1]] = np.maximum(
                    g[row:row + 5, x:x + mask.shape[1]], mask * G_TEXT)
            if left.shape[1] + right.shape[1] + 10 <= W:
                put(left, 3)
                put(right, W - 4 - right.shape[1])
            elif left.shape[1] + 6 <= W:
                put(left, 3)
            if not xy:
                # Trigger level marker: a three pixel arrowhead on the left
                # edge, at the level the sweep actually starts from.
                ty0 = int(round(cy - args.trigger * ampl))
                if 1 <= ty0 < H - 1:
                    g[ty0, 0:3] = G_TRIG
                    g[ty0 - 1, 0:2] = G_TRIG
                    g[ty0 + 1, 0:2] = G_TRIG
        return np.clip(g * PMAX, 0, PMAX).astype(np.uint8)

    furniture = (bake_furniture(False), bake_furniture(True))

    # ---- buffers ---------------------------------------------------------
    # The phosphor buffer is kept in palette-index units, 0..255, rather than
    # in 0..1: it is the same array either way, and it saves a whole-frame
    # multiply every frame — 0.14 ms of the budget on the Pi.
    phos = np.zeros((H, W), f32)
    phos_t = phos.T                                     # for column-major bins
    index = np.zeros((H, W), np.uint8)

    # The palette as packed 32-bit pixels, gathered into an RGBA frame whose
    # first three channels are handed back. On the Pi a row gather out of a
    # (256, 3) uint8 palette costs 2.25 ms — far and away the most expensive
    # thing in the frame — and gathering one 4-byte word per pixel instead of
    # three separate bytes costs 1.26. Building the word through a uint8 view
    # rather than by shifting keeps it right on a big-endian machine.
    pal = np.zeros((PMAX + 1, 4), np.uint8)
    pal[:, :3] = ds.gradient(PHOSPHORS[args.phosphor], PMAX + 1)
    lut = pal.view(np.uint32).reshape(PMAX + 1)
    frame4 = np.zeros((H, W, 4), np.uint8)
    frame_u32 = frame4.view(np.uint32).reshape(H, W)
    frame_rgb = frame4[:, :, :3]                        # a strided view; see run()

    MSTEP = int(max(8, 2 * H))
    # m ** -SPEED_EXP as a table. m is a whole number of steps, and np.power
    # with a fractional exponent is one of the few genuinely per-element
    # expensive things left in the frame.
    POW = np.empty(MSTEP + 1, f32)
    POW[0] = 1.0
    POW[1:] = np.arange(1, MSTEP + 1, dtype=f32) ** -SPEED_EXP
    # Scratch for the path, sized for a whole sweep in one frame.
    NP = int(SUB * (W + H)) + 8
    PX = np.zeros(NP, f32)
    PY = np.zeros(NP, f32)
    TX = np.zeros(NP, f32)
    TY = np.zeros(NP, f32)
    ramp = [np.arange(1 << 15, dtype=f32)]              # grows if ever short
    head = (pal[PMAX, :3].astype(np.uint16)[None, None, :]
            * np.array([[40, 70, 40], [70, 100, 70], [40, 70, 40]],
                       np.uint16)[:, :, None] // 100).astype(np.uint8)

    def deposit(xs, ys, w):
        """Bin a cloud of beam samples into the phosphor buffer.

        xs must already be rounded to whole columns; ys is a real row. Both
        are scratch and get overwritten. `w` is the charge a sample carries,
        scalar or per sample.

        Two details earn their keep. The bin is column-major over only the
        columns the beam touched — a 30 fps sweep covers eleven of them, so
        this is a 700 element histogram rather than a 20480 element one — and
        each sample is split between the two rows it falls between, which is
        the beam spot straddling a scan line rather than snapping to one. That
        second one is not only prettier: a flat top landing on exactly half a
        pixel, which the default amplitude does since the panel has an even
        number of rows, otherwise rounds up or down on float noise alone and
        draws the top of a square wave as a comb.
        """
        x0 = int(xs.min())
        lw = int(xs.max()) + 1 - x0
        np.subtract(xs, f32(x0), out=xs)
        np.multiply(xs, f32(H), out=xs)
        fl = np.floor(ys)
        np.subtract(ys, fl, out=ys)                     # ys is now the fraction
        np.add(fl, xs, out=fl)
        idx = fl.astype(np.intp)
        hi = np.multiply(ys, w)
        lo = np.subtract(w, hi)
        acc = np.bincount(np.concatenate([idx, idx + 1]),
                          weights=np.concatenate([lo, hi]),
                          minlength=lw * H + 1)[:lw * H].reshape(lw, H)
        # Accumulate rather than take the maximum, and clip afterwards. The
        # maximum is tempting — it bounds the buffer for free — but a column
        # the beam crosses on a frame boundary is then lit by whichever of the
        # two frames covered more of it instead of by both, and the trace gets
        # a one pixel dropout every eleven pixels that reads as a dashed line.
        # Charge has to be conserved across the seam. Clipping here rather
        # than over the whole frame is what keeps the cost local.
        sl = phos_t[x0:x0 + lw]
        np.add(sl, acc, out=sl)
        np.minimum(sl, f32(PMAX), out=sl)

    def draw(px, py, charge, cut=-1):
        """Deposit a beam path into the phosphor buffer.

        px, py are n+1 points; the n segments between them are equal slices of
        *time*, each carrying `charge`. `cut` blanks one segment, which is how
        a frame that spans a retrace draws both halves of the sweep in one
        pass instead of paying for the whole path twice.

        The two paths through here are the whole performance story. Sampling
        at three points a pixel, most segments are a third of a pixel long in
        both axes, and then the samples *are* the drawing: no walking, no
        expansion, twenty numpy calls. Only where the signal genuinely jumps —
        the edge of a square wave, the reset of a sawtooth, the retrace — does
        a segment need filling in, and then it is walked in whole pixels with
        its charge divided between them, which is where the intensity of a
        fast edge comes from. Two rows apart is the threshold rather than one,
        because the row split above already covers two rows a sample, so
        anything closer than that leaves no gap to fill.
        """
        dx = px[1:] - px[:-1]
        dy = py[1:] - py[:-1]
        m = np.abs(dx)
        np.maximum(m, np.abs(dy), out=m)
        n = m.size
        if float(m.max()) < 2.0:
            np.rint(px[:n], out=TX[:n])
            np.copyto(TY[:n], py[:n])
            return deposit(TX[:n], TY[:n], charge)

        np.ceil(m, out=m)
        np.clip(m, 1.0, f32(MSTEP), out=m)
        if cut >= 0:
            m[cut] = 1.0
        mi = m.astype(np.int32)
        total = int(mi.sum())
        if total <= 0:
            return
        if total > ramp[0].size:
            ramp[0] = np.arange(total * 2, dtype=f32)

        # Offset of each sub-step within its own segment, built without a
        # Python loop: repeat the run starts and subtract them off a ramp.
        ends = np.cumsum(m)
        np.subtract(ends, m, out=ends)
        u = np.repeat(ends, mi)
        np.subtract(ramp[0][:total], u, out=u)
        inv = np.divide(f32(1.0), m)
        np.multiply(u, np.repeat(inv, mi), out=u)

        xs = np.repeat(dx, mi)
        np.multiply(xs, u, out=xs)
        np.add(xs, np.repeat(px[:-1], mi), out=xs)
        ys = np.repeat(dy, mi)
        np.multiply(ys, u, out=ys)
        np.add(ys, np.repeat(py[:-1], mi), out=ys)
        np.rint(xs, out=xs)

        w = np.take(POW, mi)
        np.multiply(w, charge, out=w)
        if cut >= 0:
            w[cut] = 0.0
        deposit(xs, ys, np.repeat(w, mi))

    def yt_path(pieces, name):
        """Points for one frame of swept trace, as [(u0, u1, sweep), ...].

        Tables are baked in *rows*, not volts, and sampled nearest-neighbour
        rather than interpolated: at fifty samples a pixel the difference is a
        fiftieth of a pixel of jitter in x, and dropping the interpolation
        takes five numpy calls out of a path that runs every frame. np.linspace
        goes the same way — it costs four bare calls on the Pi — in favour of
        a cached ramp scaled in place.
        """
        table = tables[name]
        drift = DRIFT.get(name, 0.0)
        jit = name == "noise"
        n_total, cut, at = 0, -1, []
        for u0, u1, sw in pieces:
            n = int(min(NP - 2, max(1, math.ceil(SUB * (u1 - u0) * W))))
            at.append((u0, u1, sw, n))
            n_total += n + 1
        i = 0
        for u0, u1, sw, n in at:
            sl = slice(i, i + n + 1)
            rr = ramp[0][:n + 1]
            step = (u1 - u0) / n
            np.multiply(rr, f32(step * (W - 1)), out=PX[sl])
            np.add(PX[sl], f32(u0 * (W - 1)), out=PX[sl])
            off = drift * sw * sweep_len
            if jit:
                off += float(jitter[sw % jitter.size])
            # Reduce the drift before it is used, not after: an offset that
            # grows without bound loses float32 precision inside a few hours
            # and the trace starts stepping.
            base = (u0 + math.fmod(off / T, 1.0)) * NT
            k = TX[sl]
            np.multiply(rr, f32(step * NT), out=k)
            np.add(k, f32(base), out=k)
            np.mod(k, f32(NT), out=k)
            ki = k.astype(np.int32)
            np.minimum(ki, NT - 1, out=ki)
            np.take(table, ki, out=PY[sl])
            if i:
                cut = i - 1
            i += n + 1
        return PX[:n_total], PY[:n_total], cut

    state = {"t": 0.0, "frame": -1}

    def render(t, frame_idx):
        # Two calls at the same t must give the same frame. With a persistence
        # buffer that is not automatic — a second call would decay by zero and
        # deposit the same trace again — so a non-advancing call returns the
        # buffer untouched.
        if t <= state["t"] and state["frame"] >= 0:
            return frame_rgb
        dt = float(min(0.25, max(1e-6, t - state["t"])))
        t0 = state["t"]
        state["t"] = t
        state["frame"] = frame_idx

        np.multiply(phos, f32(0.5 ** (dt / half_life)), out=phos)

        mode, name, seg_t0, blank, visit_i = schedule(t)
        spot = None
        if not blank:
            if mode == "yt":
                # Where the beam was, and where it is. A frame that crosses a
                # retrace draws the tail of the old sweep and the head of the
                # new one, with the jump between them blanked.
                i0, i1 = int(t0 // sweep_len), int(t // sweep_len)
                p0 = min(1.0, (t0 - i0 * sweep_len) / T)
                p1 = min(1.0, (t - i1 * sweep_len) / T)
                if i1 == i0:
                    pieces = [(p0, p1, i1)] if p1 > p0 else []
                elif i1 == i0 + 1:
                    pieces = ([(p0, 1.0, i0)] if p0 < 1.0 else []) + \
                             ([(0.0, p1, i1)] if p1 > 0.0 else [])
                else:
                    pieces = [(0.0, p1, i1)] if p1 > 0.0 else []
                if pieces:
                    px, py, cut = yt_path(pieces, name)
                    n = px.size - 1
                    draw(px, py, f32(beam * v_ref * dt / max(1, n)), cut)
                    spot = (float(px[-1]), float(py[-1]))
            else:
                j = min(int((t - seg_t0) / (xy_dwell / XY_SUB)), XY_SUB - 1)
                k = (visit_i * XY_SUB + j) % len(XY_RATIOS)
                a, b, eps = XY_RATIOS[k]
                b += eps
                s0 = (t0 - seg_t0) * xy_rate
                s1 = (t - seg_t0) * xy_rate
                n = int(min(NP - 2, max(2, math.ceil(
                    SUB * xy_norm[k] / xy_rate * (s1 - s0)))))
                ss = TX[:n + 1]
                np.multiply(ramp[0][:n + 1], f32(TAU * (s1 - s0) / n), out=ss)
                np.add(ss, f32(TAU * s0), out=ss)
                px = PX[:n + 1]
                py = PY[:n + 1]
                np.multiply(ss, f32(a), out=px)
                np.sin(px, out=px)
                np.multiply(px, rx, out=px)
                np.add(px, f32(cx), out=px)
                np.multiply(ss, f32(b), out=py)
                np.sin(py, out=py)
                np.multiply(py, -ry, out=py)
                np.add(py, f32(cy), out=py)
                draw(px, py, f32(beam * xy_norm[k] * dt / n))
                spot = (float(px[-1]), float(py[-1]))

        np.copyto(index, phos, casting="unsafe")
        np.maximum(index, furniture[mode == "xy"], out=index)
        np.take(lut, index, out=frame_u32)

        # The spot itself, drawn rather than stored: it belongs to the beam,
        # not to the phosphor, and storing it would smear a bright dot along
        # the whole trail.
        if spot is not None:
            hx, hy = int(round(spot[0])), int(round(spot[1]))
            if 1 <= hx < W - 1 and 1 <= hy < H - 1:
                np.maximum(frame_rgb[hy - 1:hy + 2, hx - 1:hx + 2], head,
                           out=frame_rgb[hy - 1:hy + 2, hx - 1:hx + 2])
        return frame_rgb

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
