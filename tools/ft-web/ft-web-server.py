#!/usr/bin/env python3
"""A FlaschenTaschen server that renders to a browser instead of hardware.

Drop-in replacement for `ft-server` while developing demos: it speaks the same
UDP protocol on port 1337 and composites layers the same way, but pushes the
result to a <canvas> over a WebSocket. Unlike the terminal backend, which
re-emits an ANSI escape sequence per character cell per frame and becomes the
bottleneck well below 60 fps, this stays out of the demo's way and reports what
the demo is actually achieving.

  ./ft-web-server.py -D 320x64
  # then open http://localhost:8080/ and run a demo against 127.0.0.1:1337

Protocol and compositing are mirrored from the C++ server so that what you see
here matches the wall:

  * server/ppm-reader.cc         -- P6 parsing, "#FT: x y z" header comments,
                                    and the trailing-offset extension.
  * server/composite-flaschen-taschen.cc -- 16 layers, black is transparent on
                                    layers above 0, topmost non-black wins.
  * server/main.cc               -- 15 second per-layer inactivity timeout.

Deliberate differences are marked NOTE: below.

Needs numpy. Everything else is standard library.
"""

import argparse
import base64
import errno
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

FT_PORT = 1337
NUM_LAYERS = 16          # CompositeFlaschenTaschen layered_display(display, 16)
LAYER_TIMEOUT = 15       # main.cc: layer_timeout default
MAX_DATAGRAM = 65535     # udp-server.cc: kBufferSize

_HERE = os.path.dirname(os.path.abspath(__file__))
_WS_GUID = b"258EAFA5-E914-47DA-95CA-5AB0DC85B11D"


# --------------------------------------------------------------------------
# PPM parsing. A transcription of ReadImageData() in server/ppm-reader.cc.
# --------------------------------------------------------------------------

class Meta(object):
    """The ImageMetaInfo struct: where and how big the incoming image is."""

    __slots__ = ("width", "height", "range", "offset_x", "offset_y", "layer")

    def __init__(self, width, height):
        self.width = width          # defaults are the display size, per
        self.height = height        # udp-server.cc
        self.range = 0
        self.offset_x = 0
        self.offset_y = 0
        self.layer = 0


def _is_space(b):
    return b in b" \t\n\r\v\f"


def _skip_whitespace(buf, pos, end, meta):
    """Eat whitespace and comments. '#FT:' comments set offsets on `meta`.

    Returns None at end of buffer, matching the C++ NULL return.
    """
    while True:
        while pos < end and _is_space(buf[pos:pos + 1]):
            pos += 1
        if pos >= end:
            return None
        if buf[pos:pos + 1] != b"#":
            return pos
        start = pos
        while pos < end and buf[pos:pos + 1] != b"\n":
            pos += 1
        # parseSpecialComment(): only "#FT:" carries meaning, and offsets
        # inside it are parsed with info=NULL, so no nesting.
        if meta is not None and pos - start >= 4 and buf[start:start + 4] == b"#FT:":
            _parse_offsets(buf, start + 4, pos, meta)


def _read_number(buf, pos, end, meta):
    """strtol() semantics after skipping whitespace/comments.

    Returns (value, new_pos), or (0, None) if no number could be read.
    """
    pos = _skip_whitespace(buf, pos, end, meta)
    if pos is None:
        return 0, None
    start = pos
    if buf[pos:pos + 1] in (b"+", b"-"):
        pos += 1
    digits = pos
    while pos < end and b"0" <= buf[pos:pos + 1] <= b"9":
        pos += 1
    if pos == digits:
        return 0, None
    return int(buf[start:pos]), pos


def _parse_offsets(buf, pos, end, meta):
    """Read up to three numbers into (offset_x, offset_y, layer)."""
    meta.offset_x, pos = _read_number(buf, pos, end, None)
    if pos is None:
        return
    meta.offset_y, pos = _read_number(buf, pos, end, None)
    if pos is None:
        return
    meta.layer, pos = _read_number(buf, pos, end, None)


def read_image_data(buf, meta):
    """Return the offset of the pixel data, filling in `meta`.

    Returns 0 for a buffer with no usable P6 header, which the C++ treats as
    a raw full-display image.
    """
    end = len(buf)
    if end < 3:
        return 0
    if buf[0:2] != b"P6" or not (_is_space(buf[2:3]) or buf[2:3] == b"#"):
        return 0                                  # raw image, no P6 magic

    pos = 2
    width, pos = _read_number(buf, pos, end, meta)
    if pos is None:
        return 0
    height, pos = _read_number(buf, pos, end, meta)
    if pos is None:
        return 0
    range_, pos = _read_number(buf, pos, end, meta)
    if pos is None:
        return 0
    if pos >= end or not _is_space(buf[pos:pos + 1]):
        return 0                                  # last char before the data
    pos += 1

    expected = width * height * 3
    actual = end - pos
    if actual < expected:
        return 0                                  # not enough data
    if actual > expected:
        # The trailing-offset extension: an "x y z" tail after the pixels.
        _parse_offsets(buf, pos + expected, end, meta)

    meta.width = width
    meta.height = height
    meta.range = range_
    return pos


# --------------------------------------------------------------------------
# The layered display.
# --------------------------------------------------------------------------

class Display(object):
    """A CompositeFlaschenTaschen worked in whole rectangles instead of pixels.

    The C++ maintains a z-buffer incrementally as each pixel is set. Compositing
    the whole stack top-down on demand is equivalent -- in both models a pixel
    shows the highest layer that is non-black there, with layer 0 as an opaque
    background -- and it vectorizes, which the per-pixel walk does not.
    """

    def __init__(self, width, height, layers=NUM_LAYERS, timeout=LAYER_TIMEOUT):
        self.width = width
        self.height = height
        self.timeout = timeout
        self.lock = threading.Lock()
        self._layers = np.zeros((layers, height, width, 3), np.uint8)
        self._touched = [False] * layers      # has this layer ever been drawn?
        self._last_use = [0.0] * layers       # monotonic time of last packet
        self.generation = 0                   # bumped by each Send()
        # Counters, read by the stats reporter.
        self.datagrams = 0
        self.frames = 0
        self.bytes_in = 0
        self.bad_packets = 0
        self.last_arrival = 0.0

    def apply(self, buf, now):
        """Ingest one datagram. Mirrors udp_server_run_blocking()'s body."""
        meta = Meta(self.width, self.height)
        start = read_image_data(buf, meta)

        need = meta.width * meta.height * 3
        # NOTE: the C++ reads width*height*3 bytes unconditionally here, so a
        # short raw packet reads past the buffer. We drop it instead.
        if need <= 0 or len(buf) - start < need:
            with self.lock:
                self.bad_packets += 1
                self.datagrams += 1
                self.bytes_in += len(buf)
            return

        img = np.frombuffer(buf, np.uint8, need, start)
        img = img.reshape(meta.height, meta.width, 3)
        layer = min(max(meta.layer, 0), len(self._layers) - 1)   # SetLayer() clamps

        # Clip the blit to the display; the C++ drops out-of-range pixels in
        # CompositeFlaschenTaschen::SetPixel().
        dx0, dy0 = meta.offset_x, meta.offset_y
        sx0 = max(0, -dx0)
        sy0 = max(0, -dy0)
        sx1 = min(meta.width, self.width - dx0)
        sy1 = min(meta.height, self.height - dy0)

        with self.lock:
            self.datagrams += 1
            self.bytes_in += len(buf)
            self.last_arrival = now
            self._last_use[layer] = now         # SetLayer() stamps the layer
            if sx1 > sx0 and sy1 > sy0:
                self._layers[layer,
                             dy0 + sy0:dy0 + sy1,
                             dx0 + sx0:dx0 + sx1] = img[sy0:sy1, sx0:sx1]
                self._touched[layer] = True
                self.frames += 1                # one Send() per datagram
                self.generation += 1

    def expire(self, now):
        """Clear layers above 0 that no packet has addressed recently."""
        with self.lock:
            for z in range(1, len(self._layers)):
                if self._touched[z] and now - self._last_use[z] > self.timeout:
                    self._layers[z] = 0
                    self._touched[z] = False
                    self.generation += 1

    def composite(self):
        """Flatten the stack. Returns (generation, (H,W,3) uint8)."""
        with self.lock:
            gen = self.generation
            out = self._layers[0].copy()
            for z in range(1, len(self._layers)):
                if not self._touched[z]:
                    continue                     # all black, cannot contribute
                layer = self._layers[z]
                visible = layer.any(axis=2)      # black is transparent above 0
                out[visible] = layer[visible]
            return gen, out

    def snapshot_counters(self):
        with self.lock:
            return (self.datagrams, self.frames, self.bytes_in,
                    self.bad_packets, [bool(t) for t in self._touched])


# --------------------------------------------------------------------------
# Minimal RFC 6455 WebSocket, server side only.
# --------------------------------------------------------------------------

def ws_frame(payload, opcode):
    """Wrap a payload in an unmasked server->client frame."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n <= 0xFFFF:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


class Client(object):
    """One connected browser."""

    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()
        self.alive = True
        # Bound how long a stalled browser can hold up the broadcaster.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                        struct.pack("ll", 2, 0))

    def send(self, payload, opcode):
        """Send one frame, or skip it if a previous send is still in flight.

        Dropping a frame for a browser that cannot keep up is the right
        backpressure for a live view: we always want the newest frame, never a
        queue of stale ones, and one slow client must not stall the others.
        """
        if not self.alive:
            return False
        if not self.lock.acquire(False):
            return False                        # still writing, skip this one
        try:
            self.sock.sendall(ws_frame(payload, opcode))
            return True
        except OSError:
            self.alive = False
            return False
        finally:
            self.lock.release()

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class Hub(object):
    """The set of connected browsers."""

    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def add(self, client):
        with self.lock:
            self.clients.add(client)

    def remove(self, client):
        with self.lock:
            self.clients.discard(client)

    def broadcast(self, payload, opcode):
        with self.lock:
            targets = list(self.clients)
        dead = []
        for c in targets:
            c.send(payload, opcode)
            if not c.alive:
                dead.append(c)
        for c in dead:
            self.remove(c)

    def count(self):
        with self.lock:
            return len(self.clients)


def ws_read_loop(client):
    """Read client frames until close, so we notice disconnects promptly.

    Browsers send almost nothing on this direction -- a close frame, and pings
    if configured -- but reading gives us a clean disconnect signal and keeps
    the receive buffer drained.
    """
    sock = client.sock

    def recv_exact(n):
        chunks = []
        while n:
            b = sock.recv(n)
            if not b:
                return None
            chunks.append(b)
            n -= len(b)
        return b"".join(chunks)

    while client.alive:
        try:
            head = recv_exact(2)
            if head is None:
                break
            opcode = head[0] & 0x0F
            masked = head[1] & 0x80
            length = head[1] & 0x7F
            if length == 126:
                ext = recv_exact(2)
                if ext is None:
                    break
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = recv_exact(8)
                if ext is None:
                    break
                length = struct.unpack("!Q", ext)[0]
            key = recv_exact(4) if masked else None
            body = recv_exact(length) if length else b""
            if body is None:
                break
            if key:
                body = bytes(b ^ key[i % 4] for i, b in enumerate(body))
            if opcode == 0x8:                    # close
                break
            if opcode == 0x9:                    # ping
                client.send(body, 0xA)
        except OSError:
            break
    client.alive = False


# --------------------------------------------------------------------------
# HTTP + WebSocket front door.
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ft-web"

    # Injected by make_server().
    hub = None
    display = None

    def log_message(self, fmt, *args):
        pass                                     # too chatty at 60 fps

    def _serve_file(self, name, content_type):
        try:
            with open(os.path.join(_HERE, name), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/ws":
            self._do_websocket()
        elif path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def _do_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if not key or upgrade != "websocket":
            self.send_error(400, "Expected a WebSocket upgrade")
            return
        accept = base64.b64encode(
            hashlib.sha1(key.encode("ascii") + _WS_GUID).digest()).decode("ascii")
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n\r\n")
        self.wfile.flush()

        client = Client(self.connection)
        hello = json.dumps({"type": "hello",
                            "width": self.display.width,
                            "height": self.display.height,
                            "layers": NUM_LAYERS,
                            "timeout": self.display.timeout})
        client.send(hello.encode("utf-8"), 0x1)
        self.hub.add(client)
        try:
            # Hold the handler thread here; returning would close the socket.
            ws_read_loop(client)
        finally:
            self.hub.remove(client)
            client.close()
        self.close_connection = True


def resolve_bind(bind, port, socktype):
    """Pick the address family for a bind address.

    '::' has to become an AF_INET6 socket and '127.0.0.1' an AF_INET one; the
    defaults in the socket module and in HTTPServer disagree about which, so
    neither can be assumed.
    """
    infos = socket.getaddrinfo(bind or None, port, 0, socktype, 0,
                               socket.AI_PASSIVE)
    family, _, _, _, addr = infos[0]
    return family, addr


class DualStackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that can bind IPv6, and accept IPv4 when it does."""

    daemon_threads = True

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        ThreadingHTTPServer.server_bind(self)


def make_server(bind, port, hub, display):
    handler = type("BoundHandler", (Handler,), {"hub": hub, "display": display})
    family, addr = resolve_bind(bind, port, socket.SOCK_STREAM)
    server = type("BoundServer", (DualStackHTTPServer,), {"address_family": family})
    return server(addr[:2], handler)


# --------------------------------------------------------------------------
# Workers.
# --------------------------------------------------------------------------

def udp_loop(display, port, bind, stop):
    family, addr = resolve_bind(bind, port, socket.SOCK_DGRAM)
    sock = socket.socket(family, socket.SOCK_DGRAM)
    if family == socket.AF_INET6:
        try:                                  # accept IPv4 clients too
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # udp-server.cc asks for three full frames' worth, with a floor.
    want = max(3 * (display.width * display.height * 3 + 1024), 3 * 65535)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, want)
    got = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    if got < want:
        print("note: asked for a %d byte receive buffer, kernel gave %d.\n"
              "      Raise it with: sudo sysctl -w net.core.rmem_max=%d"
              % (want, got, want), file=sys.stderr)
    sock.bind(addr)
    sock.settimeout(0.5)
    print("UDP-server: ready to listen on %d" % port, file=sys.stderr)
    while not stop.is_set():
        try:
            data, _ = sock.recvfrom(MAX_DATAGRAM)
        except socket.timeout:
            continue
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise
        display.apply(data, time.monotonic())
    sock.close()


def push_loop(display, hub, fps, stop):
    """Composite and broadcast at a fixed rate, plus periodic stats."""
    interval = 1.0 / fps
    last_gen = -1
    last_expire = 0.0
    last_stats = time.monotonic()
    prev = display.snapshot_counters()
    pushed = 0
    next_tick = time.monotonic()

    while not stop.is_set():
        now = time.monotonic()

        if now - last_expire >= 1.0:             # the layer garbage collector
            display.expire(now)
            last_expire = now

        if hub.count():
            gen, frame = display.composite()
            if gen != last_gen:                  # nothing new, nothing to send
                last_gen = gen
                hub.broadcast(frame.tobytes(), 0x2)
                pushed += 1

        if now - last_stats >= 0.5:
            cur = display.snapshot_counters()
            dt = now - last_stats
            stats = {
                "type": "stats",
                "dgrams_per_s": (cur[0] - prev[0]) / dt,
                "frames_per_s": (cur[1] - prev[1]) / dt,
                "mbit_per_s": (cur[2] - prev[2]) * 8.0 / dt / 1e6,
                "pushed_per_s": pushed / dt,
                "bad": cur[3],
                "layers": cur[4],
                "clients": hub.count(),
            }
            hub.broadcast(json.dumps(stats).encode("utf-8"), 0x1)
            prev = cur
            pushed = 0
            last_stats = now

        next_tick += interval
        slack = next_tick - time.monotonic()
        if slack > 0:
            time.sleep(slack)
        else:
            next_tick = time.monotonic()         # fell behind, resynchronize


def parse_dimension(text):
    try:
        w, h = text.lower().split("x", 1)
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError("expected <width>x<height>, got %r" % text)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("-D", "--dimension", type=parse_dimension, default=(45, 35),
                    metavar="WxH", help="output dimension, like ft-server -D")
    ap.add_argument("--ft-port", type=int, default=FT_PORT,
                    help="UDP port to receive FlaschenTaschen images on")
    ap.add_argument("--http-port", type=int, default=8080,
                    help="TCP port to serve the viewer on")
    ap.add_argument("--bind", default="::",
                    help="address to bind the viewer to; '::1' for local only")
    ap.add_argument("--layer-timeout", type=int, default=LAYER_TIMEOUT,
                    help="clear a layer after this many seconds of inactivity")
    ap.add_argument("--push-fps", type=float, default=60.0,
                    help="cap on frames pushed to the browser")
    args = ap.parse_args()

    width, height = args.dimension

    display = Display(width, height, NUM_LAYERS, args.layer_timeout)
    hub = Hub()
    stop = threading.Event()

    httpd = make_server(args.bind, args.http_port, hub, display)
    threads = [
        threading.Thread(target=udp_loop,
                         args=(display, args.ft_port, args.bind, stop),
                         daemon=True),
        threading.Thread(target=push_loop,
                         args=(display, hub, args.push_fps, stop),
                         daemon=True),
        threading.Thread(target=httpd.serve_forever, daemon=True),
    ]
    for t in threads:
        t.start()

    host = "localhost" if args.bind in ("::", "::1", "", "0.0.0.0") else args.bind
    print("Viewer: http://%s:%d/   (%dx%d, %d layers)"
          % (host, args.http_port, width, height, NUM_LAYERS), file=sys.stderr)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        stop.set()
        httpd.shutdown()


if __name__ == "__main__":
    main()
