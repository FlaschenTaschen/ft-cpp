"""Shared scaffolding for the numpy demos.

Every demo here is the same shape: parse the usual options, precompute what it
can, then hand a `render(t, frame) -> (H, W, 3)` callback to a fixed-rate loop
that pushes frames with api/python's send_array_banded(). This module holds
that common part so each demo file is just its effect.

  import demoscene as ds

  ap = ds.parser("Bouncing dot")
  ap.add_argument("--radius", type=float, default=6.0)
  args = ap.parse_args()

  def render(t, frame):
      ...
  ds.run(render, args)

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


def gradient(stops, size=256):
    """Build a lookup table by interpolating between colour stops.

    stops: [(position 0..1, (r, g, b)), ...] in increasing position.
    Returns (size, 3) uint8, indexable by a 0..size-1 scalar field.
    """
    pos = np.array([p for p, _ in stops], f32)
    cols = np.array([c for _, c in stops], f32)
    x = np.linspace(0.0, 1.0, size, dtype=f32)
    out = np.empty((size, 3), f32)
    for ch in range(3):
        out[:, ch] = np.interp(x, pos, cols[:, ch])
    return np.clip(out, 0, 255).astype(np.uint8)


def rainbow(size=256, saturation=0.9, value=1.0):
    """A full hue sweep as a lookup table."""
    h = np.linspace(0.0, 1.0, size, endpoint=False, dtype=f32)
    rgb = hsv_to_rgb(h, f32(saturation), f32(value))
    return np.clip(rgb * 255.0, 0, 255).astype(np.uint8)


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
