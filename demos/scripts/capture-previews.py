#!/usr/bin/env python3
"""Bake preview GIFs for the exec entries, by recording what they actually draw.

The numpy demos can be previewed by calling their render function
(make-previews.py). The external ones -- grayscale, life, maze, send-text --
are separate programs whose only output is UDP to a display, so the only way
to see what they look like is to be the display.

So this is a FlaschenTaschen server that records instead of lighting anything:
it binds a port, runs the entry's own argv with the host rewritten to point at
itself, composites the layers the way the real server does, and samples the
result into a GIF. The previews are then the real output of the real binaries
with the real pixel art, not an approximation of them.

Run it on the machine that has the binaries and the artwork, which is the Pi:

  python3 scripts/capture-previews.py --rotation rotation-betelgeuse.json

Nothing needs to be stopped first. The C++ client takes host:port, so this
listens on 1338 and the wall's server keeps 1337 to itself.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMOS = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, _DEMOS)

import preview_gif

# Layers, as the server has them: 0 at the bottom, the topmost non-black pixel
# wins. The C++ tools in the rotation draw on 2 and 5.
LAYERS = 16
MAX_DATAGRAM = 65536


def parse_ft(packet):
    """-> (pixels HxWx3, x, y, z), or None if this is not a frame we grasp.

    The extended PPM the FT clients speak:

        P6\\n<w> <h>\\n#FT: <x> <y> <z>\\n255\\n<w*h*3 bytes>

    The offset comment is optional and may be any of the comment lines, so the
    header is walked line by line rather than matched as a whole.
    """
    if not packet.startswith(b"P6"):
        return None
    pos, fields, offset = 2, [], (0, 0, 0)
    while len(fields) < 3 and pos < len(packet):
        end = packet.find(b"\n", pos)
        if end < 0:
            return None
        line = packet[pos:end].strip()
        pos = end + 1
        if line.startswith(b"#"):
            if line.startswith(b"#FT:"):
                try:
                    offset = tuple(int(v) for v in line[4:].split()[:3])
                except ValueError:
                    return None
            continue
        fields.extend(line.split())
    if len(fields) < 3:
        return None
    try:
        w, h = int(fields[0]), int(fields[1])
    except ValueError:
        return None
    body = packet[pos:pos + w * h * 3]
    if len(body) < w * h * 3 or w <= 0 or h <= 0:
        return None
    pixels = np.frombuffer(body, np.uint8).reshape(h, w, 3)
    x, y, z = (list(offset) + [0, 0, 0])[:3]
    return pixels, x, y, z


class Display(object):
    """A recording FlaschenTaschen server."""

    def __init__(self, width, height, port):
        self.w, self.h = width, height
        self.layers = np.zeros((LAYERS, height, width, 3), np.uint8)
        self.lock = threading.Lock()
        self.packets = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.5)
        self.stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop:
            try:
                packet = self.sock.recv(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                return
            got = parse_ft(packet)
            if got is None:
                continue
            pixels, x, y, z = got
            if not 0 <= z < LAYERS:
                continue
            # Clip rather than reject: send-text draws 320x32 at +5+0, which
            # runs five pixels off the right edge of a 320 wide canvas.
            ph, pw = pixels.shape[:2]
            sx, sy = max(0, -x), max(0, -y)
            dx, dy = max(0, x), max(0, y)
            cw, ch = min(pw - sx, self.w - dx), min(ph - sy, self.h - dy)
            if cw <= 0 or ch <= 0:
                continue
            with self.lock:
                self.layers[z, dy:dy + ch, dx:dx + cw] = \
                    pixels[sy:sy + ch, sx:sx + cw]
                self.packets += 1

    def compose(self):
        """Topmost non-black wins, which is what the real server does."""
        with self.lock:
            stack = self.layers.copy()
        out = np.zeros((self.h, self.w, 3), np.uint8)
        for z in range(LAYERS):
            lit = stack[z].any(axis=2)
            out[lit] = stack[z][lit]
        return out

    def clear(self):
        with self.lock:
            self.layers[:] = 0
            self.packets = 0

    def close(self):
        self.stop = True
        self.sock.close()


def retarget(argv, host_port):
    """Point an entry's command line at us instead of at the wall."""
    argv = list(argv)
    for i, a in enumerate(argv):
        if a == "-h" and i + 1 < len(argv):
            argv[i + 1] = host_port
            return argv
    return argv + ["-h", host_port]


def capture(entry, display, port, frames, fps, warmup, quiet):
    from PIL import Image

    argv = retarget(entry["argv"], "127.0.0.1:%d" % port)
    display.clear()
    child = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    shots = []
    try:
        # Wait for the first packet rather than a fixed delay: `life` and
        # `maze` think before they draw, and a fixed warmup either wastes time
        # or captures a black screen.
        deadline = time.monotonic() + 10.0
        while display.packets == 0 and time.monotonic() < deadline:
            if child.poll() is not None and display.packets == 0:
                break
            time.sleep(0.05)
        time.sleep(warmup)
        step = 1.0 / fps
        due = time.monotonic()
        for _ in range(frames):
            shots.append(Image.fromarray(display.compose(), "RGB"))
            due += step
            slack = due - time.monotonic()
            if slack > 0:
                time.sleep(slack)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    if not shots or max(int(np.asarray(s).max()) for s in shots) <= 8:
        return None, 0
    # A still image -- the send-text jokes set a layer once and stop -- does
    # not need sixteen identical frames in the file.
    arrays = [np.asarray(s) for s in shots]
    if all(np.array_equal(arrays[0], a) for a in arrays[1:]):
        shots = shots[:1]
    return shots, display.packets


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("names", nargs="*", help="entries to capture (default: all exec)")
    ap.add_argument("--rotation", default=os.path.join(_DEMOS, "rotation-betelgeuse.json"))
    ap.add_argument("--out", default=os.path.join(_DEMOS, "previews"))
    ap.add_argument("--port", type=int, default=1338)
    ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--height", type=int, default=64)
    ap.add_argument("--frames", type=int, default=16)
    ap.add_argument("--fps", type=float, default=8.0)
    ap.add_argument("--warmup", type=float, default=1.5,
                    help="seconds to let it run after its first packet")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    with open(args.rotation) as fh:
        rotation = json.load(fh)["rotation"]
    execs = [e for e in rotation if e.get("kind") == "exec" and e.get("argv")]
    if args.names:
        execs = [e for e in execs if e["name"] in set(args.names)]
    if not execs:
        raise SystemExit("capture-previews: nothing to capture")

    os.makedirs(args.out, exist_ok=True)
    display = Display(args.width, args.height, args.port)
    print("listening on :%d, capturing %d entries" % (args.port, len(execs)))
    total = 0
    try:
        for entry in execs:
            path = os.path.join(args.out, entry["name"] + ".gif")
            if os.path.exists(path) and not args.force:
                print("  %-16s have it" % entry["name"])
                continue
            t0 = time.monotonic()
            try:
                shots, packets = capture(entry, display, args.port, args.frames,
                                         args.fps, args.warmup, args.quiet)
            except Exception as exc:
                print("  %-16s FAILED: %s" % (entry["name"], exc))
                continue
            if not shots:
                # Worth saying loudly: a black capture means the command did
                # not draw, not that the demo is dull.
                print("  %-16s *** NOTHING DRAWN (%d packets)"
                      % (entry["name"], packets))
                continue
            size = preview_gif.save(shots, path, args.fps)
            total += size
            print("  %-16s %6.1f kB  %2d frames  %5.1fs  %d packets"
                  % (entry["name"], size / 1024.0, len(shots),
                     time.monotonic() - t0, packets))
    finally:
        display.close()
    if total:
        print("total %.1f kB" % (total / 1024.0))


if __name__ == "__main__":
    main()
