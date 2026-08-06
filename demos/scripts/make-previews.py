#!/usr/bin/env python3
"""Bake a short looping GIF of each demo, for the ftsched web UI.

A still does not tell you what most of these are. printer and knit are only
legible as motion; splitflap is *entirely* motion; slime and fireflies look
like noise until you see them move. But a full video of two dozen demos is
megabytes over the shop wifi to render a control panel, so this makes the
smallest thing that still reads: two seconds at 8 fps, at the wall's native
320x64, palettised.

These are built on a desktop and committed, NOT generated on the Pi. Building
one costs a demo's whole build() plus a few seconds of rendering, and doing
that for two dozen demos on the Pi 3 while the wall is playing would steal the
CPU the render loop needs. They only change when a demo does.

  python3 scripts/make-previews.py                  # everything missing
  python3 scripts/make-previews.py --force sunset knit
"""

import argparse
import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMOS = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, _DEMOS)

import demoscene as ds
import ftsched
import preview_gif

# Demos whose opening is not representative: a warmup is rendered and thrown
# away so the preview shows the effect in its steady state rather than its
# first second. A fire that has not caught yet is a black rectangle.
WARMUP = {
    "fire": 3.0, "water": 4.0, "slime": 8.0, "fireflies": 6.0, "grove": 3.0,
    "karl": 6.0, "metaballs": 2.0, "printer": 6.0, "knit": 4.0, "laser": 5.0,
    "sunset": 12.0, "splitflap": 1.5, "daliclock": 1.0, "goldengate": 3.0,
    "wheel": 2.0, "scroller": 2.0,
}

# The demos are driven at their own frame rate through the warmup and between
# captured frames, so anything with internal state (a physics step, a scroll
# position, a print head) advances the way it would on the wall instead of
# being teleported.
RENDER_FPS = 20


def bake(name, options, frames, fps, warmup, width, height):
    from PIL import Image

    module = __import__(name)
    opts = ds.options(module, width=width, height=height, fps=RENDER_FPS)
    for key, value in (options or {}).items():
        if hasattr(opts, key):
            setattr(opts, key, value)
    render = module.build(opts)

    dt = 1.0 / RENDER_FPS
    step = max(1, int(round(RENDER_FPS / fps)))
    t, i = 0.0, 0
    while t < warmup:                        # advance, discard
        render(t, i)
        t += dt
        i += 1

    shots = []
    for _ in range(frames * step):
        frame = render(t, i)
        if i % step == 0:
            # render() may hand back a buffer it reuses, so copy before the
            # next call rather than after the loop.
            shots.append(Image.fromarray(np.asarray(frame, np.uint8).copy(), "RGB"))
        t += dt
        i += 1
    return shots[:frames]


def save(shots, path, fps):
    """See preview_gif: one palette across the clip, written atomically."""
    return preview_gif.save(shots, path, fps)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("names", nargs="*", help="demos to bake (default: all)")
    ap.add_argument("--out", default=os.path.join(_DEMOS, "previews"))
    ap.add_argument("--frames", type=int, default=16)
    ap.add_argument("--fps", type=float, default=8.0)
    ap.add_argument("--width", type=int, default=ds.WIDTH)
    ap.add_argument("--height", type=int, default=ds.HEIGHT)
    ap.add_argument("--force", action="store_true", help="rebuild existing")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    wanted = {e.name: e for e in ftsched.default_rotation() if e.kind == "py"}
    names = args.names or sorted(wanted)

    total = 0
    for name in names:
        path = os.path.join(args.out, name + ".gif")
        if os.path.exists(path) and not args.force:
            print("%-12s have it" % name)
            continue
        entry = wanted.get(name)
        try:
            t0 = time.monotonic()
            shots = bake(name, entry.options if entry else {}, args.frames,
                         args.fps, WARMUP.get(name, 0.5), args.width, args.height)
            save(shots, path, args.fps)
        except Exception as exc:
            print("%-12s FAILED: %s" % (name, exc))
            continue
        size = os.path.getsize(path)
        total += size
        # A preview that is entirely black means the warmup is wrong, not that
        # the demo is broken, and it is worth saying so at build time.
        lit = max(float(np.asarray(s).max()) for s in shots)
        print("%-12s %6.1f kB  %2d frames  %4.1fs  %s"
              % (name, size / 1024.0, len(shots), time.monotonic() - t0,
                 "peak %d" % lit if lit > 8 else "*** ALL BLACK, check WARMUP"))
    if total:
        print("total %.1f kB" % (total / 1024.0))


if __name__ == "__main__":
    main()
