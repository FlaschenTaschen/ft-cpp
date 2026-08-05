#!/usr/bin/env python3
"""Demoscene scroller: rainbow glow text bouncing over a plasma field.

Built to run ON the Pi (over loopback) where the ARM core is slow, so the
expensive stuff is precomputed and each frame is cheap:

  * PLASMA background  -> precomputed as a seamless loop of frames, replayed.
  * TEXT + rainbow + glow -> baked once into one wide strip (straight text).
  * BOUNCE -> applied at runtime in SCREEN space, so letters ride up and down
    as they cross the display (a text-space wave would just slide rigidly).

The bake costs a few seconds of black at startup and scales with
--plasma-frames and the canvas size; the steady state after that is a couple
of numpy slices per frame.

Needs numpy, Pillow, and the `flaschen` client from ../api/python
(`pip install ./api/python`).

Run:  python3 scroller.py --host 127.0.0.1                 # on the Pi
      python3 scroller.py --text "GREETZ  " --amp 16 --no-plasma
"""

import argparse
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import flaschen

WIDTH = 320
HEIGHT = 64
HOST = "localhost"
PORT = 1337

_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_CANDIDATES = [
    os.path.join(_HERE, "Impact.ttf"),          # bundled alongside the script, if you have it
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Linux fallback
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/Library/Fonts/Impact.ttf",
]
f32 = np.float32


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


def load_font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def text_mask(text, font_size, height, pad):
    """Render the message to a tall (height x N) grayscale mask, padded left+
    right by `pad` px so it scrolls fully on and off screen."""
    font = load_font(font_size)
    l, t, r, b = ImageDraw.Draw(Image.new("L", (10, 10))).textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    strip = Image.new("L", (tw + pad * 2, height), 0)
    ImageDraw.Draw(strip).text((pad - l, (height - th) // 2 - t), text, fill=255, font=font)
    return np.asarray(strip, dtype=np.float32) / f32(255.0)


def bake_text(text, font_size, H, W):
    """Bake straight (un-bounced) rainbow + glow text into one wide uint8 strip,
    plus a wrap-around copy so any [off:off+W] window is a contiguous slice."""
    mask = text_mask(text, font_size, H, pad=W)                      # (H, SW)
    SW = mask.shape[1]
    glow = np.asarray(Image.fromarray((mask * 255).astype(np.uint8))
                      .filter(ImageFilter.GaussianBlur(3.5)), dtype=f32) / f32(255.0)
    hue = (np.arange(SW, dtype=f32) * f32(1.0 / 90.0)) % f32(1.0)    # rainbow along the text
    tint = hsv_to_rgb(hue, f32(0.95), f32(1.0)).astype(f32)          # (SW,3)
    inten = np.minimum(mask + f32(0.6) * glow, f32(1.0))             # (H,SW)
    rgb = inten[..., None] * tint[None, :, :] + mask[..., None] * f32(0.5)
    strip8 = np.clip(rgb * f32(255.0), 0, 255).astype(np.uint8)      # (H,SW,3)
    return np.concatenate([strip8, strip8[:, :W]], axis=1), SW       # (H, SW+W, 3)


def bake_plasma_loop(H, W, n_frames, brightness):
    """Precompute a seamless plasma loop (all temporal phases complete whole
    cycles over n_frames, so frame N wraps cleanly to frame 0)."""
    yy, xx = np.mgrid[0:H, 0:W].astype(f32)
    cx, cy = W / 2.0, H / 2.0
    radial = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2).astype(f32)
    frames = np.empty((n_frames, H, W, 3), np.uint8)
    for fidx in range(n_frames):
        ph = f32(2.0 * np.pi * fidx / n_frames)
        p = (np.sin(xx / 18.0 + ph)
             + np.sin(yy / 11.0 - ph)
             + np.sin((xx + yy) / 22.0 + 2.0 * ph)
             + np.sin(radial / 9.0 - ph))
        p = (p + f32(4.0)) / f32(8.0)                                # -> 0..1
        hue = (p + f32(fidx / n_frames)) % f32(1.0)                  # one hue cycle over the loop
        val = f32(brightness) * (f32(0.35) + f32(0.65) * p)
        rgb = hsv_to_rgb(hue, f32(0.9), val.astype(f32))
        frames[fidx] = np.clip(rgb * f32(255.0), 0, 255).astype(np.uint8)
    return frames


def run(render, ft, shape, offset, band_rows=0, fps=30, duration=0, stats=True):
    """Drive `render(t, frame_idx) -> (H,W,3)` at a steady frame rate.

    duration>0 auto-stops after that many seconds — a safety valve so a
    launched stream can never become a runaway flooder. Either exit path
    blanks the display, repeated a few times so a lost packet cannot leave a
    stale frame frozen on the wall.
    """
    def clear():
        black = np.zeros(shape, np.uint8)
        for _ in range(4):
            ft.send_array_banded(black, offset, band_rows)
            time.sleep(0.02)

    dt = 1.0 / fps
    start = time.monotonic()
    i = 0
    last_report = start
    try:
        while True:
            t = time.monotonic() - start
            if duration and t >= duration:
                clear()
                print(f"\ndone — {duration}s elapsed, cleared display")
                return
            ft.send_array_banded(render(t, i), offset, band_rows)
            i += 1
            if stats and time.monotonic() - last_report >= 2.0:
                print(f"\r{i / t:5.1f} fps   ", end="", flush=True)
                last_report = time.monotonic()
            # Steady pacing without drift.
            slack = start + i * dt - time.monotonic()
            if slack > 0:
                time.sleep(slack)
    except KeyboardInterrupt:
        clear()
        print("\nbye — cleared display")


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--text", default=(
        "FLASCHENTASCHEN LIVES  ✦  GREETZ TO ALL PIXEL PUSHERS  ✦     "),
        help="message to scroll; trailing spaces set the gap before it repeats")
    ap.add_argument("--speed", type=float, default=80.0, help="scroll px/sec")
    ap.add_argument("--font", type=int, default=36, help="glyph height px (smaller = more room to bounce)")
    ap.add_argument("--amp", type=float, default=15.0, help="vertical bounce amplitude px")
    ap.add_argument("--freq", type=float, default=0.09, help="bounce waves across the screen (rad/px)")
    ap.add_argument("--bob-hz", type=float, default=0.6, help="how fast the bounce wave travels (Hz)")
    ap.add_argument("--plasma", dest="plasma", action="store_true", default=True)
    ap.add_argument("--no-plasma", dest="plasma", action="store_false")
    ap.add_argument("--plasma-frames", type=int, default=150,
                    help="length of the precomputed plasma loop; fewer = faster startup")
    ap.add_argument("--brightness", type=float, default=0.5, help="plasma brightness 0..1")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--width", type=int, default=WIDTH, help="canvas width")
    ap.add_argument("--height", type=int, default=HEIGHT, help="canvas height")
    ap.add_argument("--layer", "-l", type=int, default=0, help="canvas layer (0-15)")
    ap.add_argument("--band", type=int, default=0,
                    help="rows per UDP datagram (0 = whole frame in one)")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--duration", type=float, default=0, help="auto-stop after N sec (0 = forever)")
    args = ap.parse_args()

    W, H = args.width, args.height
    ext, SW = bake_text(args.text, args.font, H, W)
    plasma = bake_plasma_loop(H, W, args.plasma_frames, args.brightness) if args.plasma else None
    P = plasma.shape[0] if args.plasma else 0

    rows_i = np.arange(H)[:, None]                 # (H,1)
    xf = np.arange(W, dtype=f32)[None, :]          # (1,W)
    two_pi = f32(2.0 * np.pi)

    def render(t, i):
        # Straight text window for this scroll position.
        off = int(t * args.speed) % SW
        txt = ext[:, off:off + W]                                   # (H,W,3) uint8

        # SCREEN-SPACE vertical bounce: each column shifted by a sine of its
        # x position + time, so letters visibly ride up/down as they scroll.
        shift = (f32(args.amp) * np.sin(xf * f32(args.freq)
                 + f32(args.bob_hz) * two_pi * f32(t))).astype(np.int32)  # (1,W)
        delta = rows_i - shift                                      # (H,W)
        idx = np.broadcast_to(np.clip(delta, 0, H - 1)[..., None], (H, W, 3))
        bounced = np.take_along_axis(txt, idx, axis=0).copy()       # (H,W,3)
        bounced[(delta < 0) | (delta > H - 1)] = 0                  # no vertical wrap

        if plasma is None:
            return bounced
        return np.maximum(plasma[i % P], bounced)                   # text over plasma

    # transparent=True: this demo owns its layer and wants literal black, and
    # it skips the per-frame black->(1,1,1) rewrite that would otherwise scan
    # every pixel of every frame.
    ft = flaschen.Flaschen(args.host, args.port, W, H, transparent=True)
    run(render, ft, shape=(H, W, 3), offset=(0, 0, args.layer),
        band_rows=args.band, fps=args.fps, duration=args.duration)


if __name__ == "__main__":
    main()
