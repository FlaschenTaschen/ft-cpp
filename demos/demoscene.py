"""Shared scaffolding for the numpy demos.

Every demo here is the same shape: parse the usual options, precompute what it
can, then hand a `render(t, frame) -> (H, W, 3)` callback to a fixed-rate loop
that pushes frames with api/python's send_array_banded(). This module holds
that common part so each demo file is just its effect.

A demo module implements two functions:

  def add_arguments(ap):        # optional: its own options
      ap.add_argument("--radius", type=float, default=6.0)

  def build(args):              # required: precompute, return the callback
      def render(t, frame):
          ...                   # -> (H, W, 3) uint8
      return render

and a `main()` that wires them to run(). Keeping the setup in build() rather
than in main() is what lets megademo.py sequence effects: it can construct a
render callback without running anyone's frame loop. Anything expensive
belongs in build(), which is called once; render() is called every frame.

render() may return a buffer it reuses between calls, which several of these
do rather than allocate per frame. A caller must therefore use the frame
before calling that same render again, and copy it if it needs to keep it.
Blending the output of two *different* effects is fine, since each owns its
own buffer.

The colour helpers matter more than they look on an LED wall: an effect that
computes a scalar field per pixel and maps it through a palette is both far
cheaper than computing RGB directly and much easier to make look good, since
the palette carries the art.
"""

import argparse
import os
import sys
import time

import numpy as np

# The packaged client lives next door. Prefer an installed copy, but fall back
# to the checkout so the demos run straight from a clone.
try:
    import flaschen
except ImportError:                                     # pragma: no cover
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "api", "python"))
    import flaschen

WIDTH = 320
HEIGHT = 64
HOST = "localhost"
PORT = 1337

f32 = np.float32


# --------------------------------------------------------------------------
# Options and the frame loop.
# --------------------------------------------------------------------------

def parser(description, fps=60, width=WIDTH, height=HEIGHT):
    """An ArgumentParser carrying the options every demo takes."""
    ap = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--width", type=int, default=width, help="canvas width")
    ap.add_argument("--height", type=int, default=height, help="canvas height")
    ap.add_argument("--layer", "-l", type=int, default=0, help="canvas layer (0-15)")
    ap.add_argument("--band", type=int, default=0,
                    help="rows per UDP datagram (0 = whole frame in one)")
    ap.add_argument("--fps", type=int, default=fps)
    ap.add_argument("--duration", type=float, default=0,
                    help="auto-stop after N sec (0 = forever)")
    ap.add_argument("--quiet", "-q", action="store_true", help="no rate reporting")
    return ap


def run(render, args):
    """Drive `render(t, frame_idx) -> (H,W,3)` at a steady frame rate.

    --duration auto-stops, a safety valve so a launched stream can never
    become a runaway flooder. Either exit path blanks the display, repeated a
    few times so a lost packet cannot leave a stale frame frozen on the wall.

    transparent=True: a demo owns its layer and wants literal black, and it
    skips the per-frame black->(1,1,1) rewrite that would otherwise scan every
    pixel of every frame.
    """
    ft = flaschen.Flaschen(args.host, args.port, args.width, args.height,
                           transparent=True)
    offset = (0, 0, args.layer)
    shape = (args.height, args.width, 3)

    def clear():
        black = np.zeros(shape, np.uint8)
        for _ in range(4):
            ft.send_array_banded(black, offset, args.band)
            time.sleep(0.02)

    dt = 1.0 / args.fps
    start = time.monotonic()
    i = 0
    last_report = start
    try:
        while True:
            t = time.monotonic() - start
            if args.duration and t >= args.duration:
                clear()
                if not args.quiet:
                    print("\ndone — %gs elapsed, cleared display" % args.duration)
                return
            ft.send_array_banded(render(t, i), offset, args.band)
            i += 1
            if not args.quiet and time.monotonic() - last_report >= 2.0:
                print("\r%5.1f fps   " % (i / t), end="", flush=True)
                last_report = time.monotonic()
            # Steady pacing without drift.
            slack = start + i * dt - time.monotonic()
            if slack > 0:
                time.sleep(slack)
    except KeyboardInterrupt:
        clear()
        if not args.quiet:
            print("\nbye — cleared display")


def standalone(module, description, **parser_kw):
    """Run a demo module as a script: parse argv, build, loop."""
    ap = parser(description, **parser_kw)
    if hasattr(module, "add_arguments"):
        module.add_arguments(ap)
    args = ap.parse_args()
    run(module.build(args), args)


def options(module, **overrides):
    """The defaults a demo module would get with no command line.

    Parsing an empty argv is how a sequencer gets a fully populated options
    object without duplicating any demo's defaults, which would then rot.
    """
    ap = parser(getattr(module, "__name__", "demo"))
    if hasattr(module, "add_arguments"):
        module.add_arguments(ap)
    args = ap.parse_args([])
    for key, value in overrides.items():
        if not hasattr(args, key):
            raise KeyError("%s has no option %r" % (args, key))
        setattr(args, key, value)
    return args


def build(module, **overrides):
    """Construct a render callback from a demo module. See options()."""
    return module.build(options(module, **overrides))


# --------------------------------------------------------------------------
# Transitions, for sequencing effects into one show.
# --------------------------------------------------------------------------

def crossfade(a, b, k):
    """Blend two frames. k runs 0 (all a) to 1 (all b)."""
    k = float(np.clip(k, 0.0, 1.0))
    return (a.astype(np.float32) * (1.0 - k)
            + b.astype(np.float32) * k).astype(np.uint8)


def fade_black(a, k):
    """Fade a frame towards black. k runs 0 (unchanged) to 1 (black)."""
    return (a.astype(np.float32) * (1.0 - float(np.clip(k, 0.0, 1.0)))).astype(np.uint8)


def wipe(a, b, k, softness=24):
    """Sweep b over a from the left, with a soft edge.

    A hard edge on a 320 wide panel crosses in a couple of frames and reads as
    a glitch, so the boundary is a ramp rather than a step.
    """
    w = a.shape[1]
    k = float(np.clip(k, 0.0, 1.0))
    # The edge has to travel a full softness past the last column, or at k=1
    # the rightmost pixels are still part way through the ramp and the outgoing
    # frame never fully clears.
    edge = k * (w + softness)
    ramp = np.clip((edge - np.arange(w, dtype=f32)) / max(softness, 1e-6), 0.0, 1.0)
    return (a.astype(np.float32) * (1.0 - ramp)[None, :, None]
            + b.astype(np.float32) * ramp[None, :, None]).astype(np.uint8)


# --------------------------------------------------------------------------
# Colour.
# --------------------------------------------------------------------------

def hsv_to_rgb(h, s, v):
    """Vectorized HSV->RGB. h,s,v are arrays in 0..1. Returns (..., 3) float 0..1."""
    h = (h % 1.0) * 6.0
    i = np.floor(h).astype(int)
    f = h - i
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))
    i = i % 6
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def gradient(stops, size=256, dtype=np.uint8):
    """Build a lookup table by interpolating between colour stops.

    stops: [(position 0..1, (r, g, b)), ...] in increasing position.
    Returns (size, 3), indexable by a 0..size-1 scalar field.

    Pass dtype=f32 if the result will be dithered. The default uint8 has
    already been rounded, and ordered dithering an integer does nothing but
    add noise -- see dither(). Two demos independently lost time to that.
    """
    pos = np.array([p for p, _ in stops], f32)
    cols = np.array([c for _, c in stops], f32)
    x = np.linspace(0.0, 1.0, size, dtype=f32)
    out = np.empty((size, 3), f32)
    for ch in range(3):
        out[:, ch] = np.interp(x, pos, cols[:, ch])
    out = np.clip(out, 0, 255)
    return out if dtype == f32 else out.astype(dtype)


def rainbow(size=256, saturation=0.9, value=1.0, dtype=np.uint8):
    """A full hue sweep as a lookup table. See gradient() on dtype."""
    h = np.linspace(0.0, 1.0, size, endpoint=False, dtype=f32)
    rgb = np.clip(hsv_to_rgb(h, f32(saturation), f32(value)) * 255.0, 0, 255)
    return rgb if dtype == f32 else rgb.astype(dtype)


# 8x8 ordered dither, the classic Bayer sequence scaled to [0, 1).
#
# The range matters and is easy to get wrong: astype(uint8) truncates rather
# than rounds, so an offset of [0, 1) before truncation reproduces correct
# rounding on average and leaves the mean intact. Centring the offset on zero
# instead -- which looks more principled -- biases every pixel half a level
# dark, which on a panel whose whole dark end is already compressed is exactly
# where you would not want to lose brightness.
_BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21],
], f32) / 64.0


def dither(rgb_float):
    """Quantise a float (H, W, 3) image to uint8 with ordered dithering.

    The panel drives 8 PWM bits, so a smooth ramp across many rows lands on
    the same output value for a long stretch and then steps -- visible as
    contour bands, worst in the dark end where the demos spend most of their
    range. Offsetting each pixel by less than half a step before rounding
    turns those hard edges into stipple, which reads as smooth from any normal
    viewing distance.

    The input must be float and must NOT have been through a uint8 palette on
    the way here; dithering an already-rounded value only adds noise. Build
    ramps with gradient(..., dtype=ds.f32).
    """
    h, w = rgb_float.shape[:2]
    # np.tile, not np.resize: resize flattens first and would scramble the
    # matrix, which still dithers but with a pattern that crawls.
    tile = np.tile(_BAYER8, (-(-h // 8), -(-w // 8)))[:h, :w]
    return np.clip(rgb_float + tile[:, :, None], 0, 255).astype(np.uint8)


def shade(rgb, amount):
    """Scale an (H,W,3) uint8 image by an (H,W) float factor, staying uint8."""
    return (rgb.astype(np.float32) * amount[..., None]).clip(0, 255).astype(np.uint8)


# Palettes the demos share. Fire runs black -> red -> orange -> yellow ->
# white, which is the classic heat ramp and reads well on an LED panel because
# the bright end saturates rather than clipping to a flat colour.
FIRE = [(0.00, (0, 0, 0)), (0.15, (60, 0, 0)), (0.35, (200, 30, 0)),
        (0.60, (255, 120, 0)), (0.82, (255, 220, 60)), (1.00, (255, 255, 230))]

ICE = [(0.00, (0, 0, 0)), (0.25, (0, 20, 70)), (0.55, (0, 110, 200)),
       (0.80, (120, 220, 255)), (1.00, (255, 255, 255))]

TOXIC = [(0.00, (0, 0, 0)), (0.30, (10, 60, 0)), (0.60, (80, 220, 20)),
         (0.85, (200, 255, 90)), (1.00, (255, 255, 255))]

MAGMA = [(0.00, (0, 0, 8)), (0.25, (70, 10, 90)), (0.50, (190, 40, 90)),
         (0.75, (250, 130, 60)), (1.00, (255, 245, 190))]

PALETTES = {"fire": FIRE, "ice": ICE, "toxic": TOXIC, "magma": MAGMA}


def named_palette(name, size=256):
    """Look up one of PALETTES, or 'rainbow'."""
    if name == "rainbow":
        return rainbow(size)
    return gradient(PALETTES[name], size)


def palette_argument(ap, default="fire"):
    ap.add_argument("--palette", default=default,
                    choices=sorted(PALETTES) + ["rainbow"],
                    help="colour ramp")
    return ap
