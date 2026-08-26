#!/usr/bin/env python3
"""Tests for ft-web-server: PPM parsing, layer compositing, and the socket path.

Run with `python3 test_ft_web.py` or under pytest. The end-to-end tests bind
real sockets on the loopback interface using ephemeral ports.
"""

import base64
import importlib.util
import os
import socket
import struct
import sys
import threading
import time
import unittest

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))

# The module has a dash in its name, so it cannot be imported normally.
_spec = importlib.util.spec_from_file_location(
    "ft_web_server", os.path.join(_HERE, "ft-web-server.py"))
ftweb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ftweb)


def ppm(width, height, pixels, comment=None, trailer=None):
    """Build a datagram the way the FlaschenTaschen clients do."""
    head = b"P6\n"
    if comment is not None:
        head += comment + b"\n"
    head += b"%d %d\n255\n" % (width, height)
    body = head + bytes(pixels)
    if trailer is not None:
        body += trailer
    return body


class TestParsing(unittest.TestCase):
    """read_image_data() against server/ppm-reader.cc."""

    def parse(self, buf, dw=320, dh=64):
        meta = ftweb.Meta(dw, dh)
        return ftweb.read_image_data(buf, meta), meta

    def test_plain_header(self):
        buf = ppm(2, 1, [1, 2, 3, 4, 5, 6])
        start, meta = self.parse(buf)
        self.assertEqual(buf[start:], bytes([1, 2, 3, 4, 5, 6]))
        self.assertEqual((meta.width, meta.height), (2, 1))
        self.assertEqual((meta.offset_x, meta.offset_y, meta.layer), (0, 0, 0))

    def test_ft_comment_offsets(self):
        buf = ppm(2, 1, [0] * 6, comment=b"#FT: 7 9 3")
        _, meta = self.parse(buf)
        self.assertEqual((meta.offset_x, meta.offset_y, meta.layer), (7, 9, 3))

    def test_plain_comment_is_ignored(self):
        buf = ppm(2, 1, [0] * 6, comment=b"# just a comment")
        start, meta = self.parse(buf)
        self.assertEqual((meta.width, meta.height), (2, 1))
        self.assertEqual((meta.offset_x, meta.offset_y, meta.layer), (0, 0, 0))
        self.assertEqual(len(buf) - start, 6)

    def test_trailing_offsets(self):
        """The extension used by api/python: 'x y z' after the pixel data."""
        buf = ppm(2, 1, [0] * 6, trailer=b"0 32 5\n")
        _, meta = self.parse(buf)
        self.assertEqual((meta.offset_x, meta.offset_y, meta.layer), (0, 32, 5))

    def test_trailing_offsets_newline_separated(self):
        """The form flaschen_np.py emits: each number on its own line."""
        buf = ppm(2, 1, [0] * 6, trailer=b"0\n0\n11\n")
        _, meta = self.parse(buf)
        self.assertEqual((meta.offset_x, meta.offset_y, meta.layer), (0, 0, 11))

    def test_partial_trailer(self):
        """Fewer than three trailing numbers leaves the rest at their default."""
        buf = ppm(2, 1, [0] * 6, trailer=b"4\n")
        _, meta = self.parse(buf)
        self.assertEqual((meta.offset_x, meta.offset_y, meta.layer), (4, 0, 0))

    def test_raw_without_magic(self):
        """No P6 magic means a raw image at the display's default size."""
        start, meta = self.parse(b"\x01\x02\x03" * 4, dw=2, dh=2)
        self.assertEqual(start, 0)
        self.assertEqual((meta.width, meta.height), (2, 2))

    def test_short_data_falls_back_to_raw(self):
        buf = b"P6\n4 4\n255\n" + b"\x00" * 5      # needs 48 bytes, has 5
        start, _ = self.parse(buf)
        self.assertEqual(start, 0)

    def test_p6_must_be_followed_by_space_or_comment(self):
        start, _ = self.parse(b"P6x1 1\n255\n\x00\x00\x00")
        self.assertEqual(start, 0)


class TestCompositing(unittest.TestCase):
    """Display against server/composite-flaschen-taschen.cc."""

    def setUp(self):
        self.d = ftweb.Display(4, 2, layers=16, timeout=15)

    def send(self, buf):
        self.d.apply(buf, time.monotonic())

    def frame(self):
        return self.d.composite()[1]

    def fill(self, w, h, color):
        return list(color) * (w * h)

    def test_layer_zero_background(self):
        self.send(ppm(4, 2, self.fill(4, 2, (10, 20, 30))))
        self.assertTrue((self.frame() == [10, 20, 30]).all())

    def test_higher_layer_wins(self):
        self.send(ppm(4, 2, self.fill(4, 2, (10, 20, 30))))
        self.send(ppm(1, 1, [1, 2, 3], comment=b"#FT: 2 1 4"))
        f = self.frame()
        self.assertEqual(list(f[1, 2]), [1, 2, 3])
        self.assertEqual(list(f[0, 0]), [10, 20, 30])

    def test_black_is_transparent_above_layer_zero(self):
        self.send(ppm(4, 2, self.fill(4, 2, (10, 20, 30))))
        self.send(ppm(1, 1, [1, 2, 3], comment=b"#FT: 2 1 4"))
        self.send(ppm(1, 1, [0, 0, 0], comment=b"#FT: 2 1 4"))
        self.assertEqual(list(self.frame()[1, 2]), [10, 20, 30],
                         "black on a high layer must reveal the layer below")

    def test_black_is_opaque_on_layer_zero(self):
        self.send(ppm(4, 2, self.fill(4, 2, (10, 20, 30))))
        self.send(ppm(4, 2, self.fill(4, 2, (0, 0, 0))))
        self.assertTrue((self.frame() == 0).all())

    def test_lower_layer_does_not_show_through(self):
        self.send(ppm(1, 1, [9, 9, 9], comment=b"#FT: 0 0 5"))
        self.send(ppm(1, 1, [7, 7, 7], comment=b"#FT: 0 0 2"))
        self.assertEqual(list(self.frame()[0, 0]), [9, 9, 9])

    def test_offset_placement(self):
        self.send(ppm(2, 1, [1, 1, 1, 2, 2, 2], trailer=b"1 1 0\n"))
        f = self.frame()
        self.assertEqual(list(f[1, 1]), [1, 1, 1])
        self.assertEqual(list(f[1, 2]), [2, 2, 2])
        self.assertEqual(list(f[0, 0]), [0, 0, 0])

    def test_blit_is_clipped_to_the_display(self):
        """An image hanging off the edge must not error or wrap."""
        self.send(ppm(4, 2, self.fill(4, 2, (5, 5, 5)), trailer=b"3 1 0\n"))
        f = self.frame()
        self.assertEqual(list(f[1, 3]), [5, 5, 5])
        self.assertEqual(list(f[0, 0]), [0, 0, 0])

    def test_negative_offset_is_clipped(self):
        self.send(ppm(4, 2, self.fill(4, 2, (5, 5, 5)), trailer=b"-2 0 0\n"))
        f = self.frame()
        self.assertEqual(list(f[0, 0]), [5, 5, 5])
        self.assertEqual(list(f[0, 3]), [0, 0, 0])

    def test_layer_clamped_to_range(self):
        """SetLayer() clamps rather than indexing out of bounds."""
        self.send(ppm(1, 1, [3, 3, 3], comment=b"#FT: 0 0 99"))
        self.assertEqual(list(self.frame()[0, 0]), [3, 3, 3])

    def test_layer_expiry_clears_upper_layers_only(self):
        self.send(ppm(4, 2, self.fill(4, 2, (10, 20, 30))))
        self.send(ppm(1, 1, [1, 2, 3], comment=b"#FT: 0 0 4"))
        self.d.expire(time.monotonic() + 16)
        f = self.frame()
        self.assertEqual(list(f[0, 0]), [10, 20, 30], "layer 4 should have aged out")
        self.assertTrue((f == [10, 20, 30]).all(), "layer 0 must never expire")

    def test_generation_advances_only_on_real_writes(self):
        before = self.d.generation
        self.send(ppm(1, 1, [1, 1, 1]))
        self.assertGreater(self.d.generation, before)
        mid = self.d.generation
        self.send(b"P6\n4 4\n255\n" + b"\x00" * 5)      # truncated, dropped
        self.assertEqual(self.d.generation, mid)
        self.assertEqual(self.d.snapshot_counters()[3], 1, "should count as bad")


class TestHandshake(unittest.TestCase):
    """The accept value must be checked against an external reference.

    Everything else in this file validates the server against itself, which
    cannot catch a wrong protocol constant: a bad GUID produces a stable,
    self-consistent accept value that only a real client rejects. Chrome
    reported "Incorrect 'Sec-WebSocket-Accept' header value" against a GUID
    that had one hex digit moved from one end of the last group to the other.
    """

    def accept_for(self, key):
        return base64.b64encode(
            __import__("hashlib").sha1(key.encode("ascii") + ftweb._WS_GUID)
            .digest()).decode("ascii")

    def test_rfc6455_example(self):
        # RFC 6455 section 1.3.
        self.assertEqual(self.accept_for("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_guid_is_the_rfc_constant(self):
        self.assertEqual(ftweb._WS_GUID, b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11")


class TestWebSocketFraming(unittest.TestCase):

    def test_short_frame(self):
        f = ftweb.ws_frame(b"hi", 0x1)
        self.assertEqual(f, b"\x81\x02hi")

    def test_medium_frame_uses_16_bit_length(self):
        f = ftweb.ws_frame(b"x" * 1000, 0x2)
        self.assertEqual(f[:4], b"\x82\x7e" + struct.pack("!H", 1000))

    def test_large_frame_uses_64_bit_length(self):
        f = ftweb.ws_frame(b"x" * 70000, 0x2)
        self.assertEqual(f[:10], b"\x82\x7f" + struct.pack("!Q", 70000))


class TestEndToEnd(unittest.TestCase):
    """Push a real datagram in one side and read the frame out the other."""

    WIDTH, HEIGHT = 8, 4

    def setUp(self):
        self.display = ftweb.Display(self.WIDTH, self.HEIGHT, 16, 15)
        self.hub = ftweb.Hub()
        self.stop = threading.Event()

        self.httpd = ftweb.make_server("127.0.0.1", 0, self.hub, self.display)
        self.http_port = self.httpd.server_address[1]

        # Bind the UDP socket ourselves to claim an ephemeral port, then let
        # the server's loop take the same port.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        self.ft_port = probe.getsockname()[1]
        probe.close()

        self.threads = [
            threading.Thread(target=ftweb.udp_loop,
                             args=(self.display, self.ft_port, "127.0.0.1", self.stop),
                             daemon=True),
            threading.Thread(target=ftweb.push_loop,
                             args=(self.display, self.hub, 120.0, self.stop),
                             daemon=True),
            threading.Thread(target=self.httpd.serve_forever, daemon=True),
        ]
        for t in self.threads:
            t.start()
        time.sleep(0.3)

    def tearDown(self):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def ws_connect(self):
        s = socket.create_connection(("127.0.0.1", self.http_port), timeout=5)
        key = base64.b64encode(b"0123456789abcdef").decode()
        s.sendall(("GET /ws HTTP/1.1\r\n"
                   "Host: 127.0.0.1\r\n"
                   "Upgrade: websocket\r\n"
                   "Connection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n" % key).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            self.assertTrue(chunk, "server closed during handshake")
            buf += chunk
        self.assertIn(b"101 Switching Protocols", buf)
        # Computed here from the RFC constant, not from the server's own, so a
        # wrong GUID in the server fails this instead of agreeing with itself.
        import hashlib
        want = base64.b64encode(hashlib.sha1(
            key.encode("ascii") + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        ).digest()).decode("ascii")
        self.assertIn(("Sec-WebSocket-Accept: " + want).encode("ascii"), buf)
        self.rest = buf.split(b"\r\n\r\n", 1)[1]
        return s

    def ws_recv(self, s):
        """Read one unmasked server frame. Returns (opcode, payload)."""
        def need(n):
            while len(self.rest) < n:
                chunk = s.recv(65536)
                if not chunk:
                    raise AssertionError("connection closed")
                self.rest += chunk
            out, self.rest = self.rest[:n], self.rest[n:]
            return out

        head = need(2)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        self.assertFalse(head[1] & 0x80, "server frames must not be masked")
        if length == 126:
            length = struct.unpack("!H", need(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", need(8))[0]
        return opcode, need(length) if length else b""

    def send_udp(self, payload):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.sendto(payload, ("127.0.0.1", self.ft_port))
        u.close()

    def next_binary(self, s, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            opcode, payload = self.ws_recv(s)
            if opcode == 0x2:
                return payload
        raise AssertionError("no binary frame arrived")

    def test_hello_then_frame_roundtrip(self):
        s = self.ws_connect()
        try:
            opcode, payload = self.ws_recv(s)
            self.assertEqual(opcode, 0x1, "first message should be the JSON hello")
            import json
            hello = json.loads(payload)
            self.assertEqual(hello["type"], "hello")
            self.assertEqual((hello["width"], hello["height"]),
                             (self.WIDTH, self.HEIGHT))

            n = self.WIDTH * self.HEIGHT
            self.send_udp(ppm(self.WIDTH, self.HEIGHT, [40, 50, 60] * n))
            frame = self.next_binary(s)

            self.assertEqual(len(frame), n * 3)
            arr = np.frombuffer(frame, np.uint8).reshape(self.HEIGHT, self.WIDTH, 3)
            self.assertTrue((arr == [40, 50, 60]).all())
        finally:
            s.close()

    def test_layered_frame_roundtrip(self):
        s = self.ws_connect()
        try:
            self.ws_recv(s)                                   # hello
            n = self.WIDTH * self.HEIGHT
            self.send_udp(ppm(self.WIDTH, self.HEIGHT, [10, 10, 10] * n))
            self.next_binary(s)
            self.send_udp(ppm(2, 2, [200, 100, 50] * 4, comment=b"#FT: 3 1 6"))

            deadline = time.time() + 5
            while time.time() < deadline:
                arr = np.frombuffer(self.next_binary(s), np.uint8)
                arr = arr.reshape(self.HEIGHT, self.WIDTH, 3)
                if (arr[1, 3] == [200, 100, 50]).all():
                    break
            else:
                self.fail("overlay never appeared")

            self.assertTrue((arr[1:3, 3:5] == [200, 100, 50]).all())
            self.assertTrue((arr[0, 0] == [10, 10, 10]).all())
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
