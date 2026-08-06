#!/usr/bin/env python3
"""The control server for ftsched: a small JSON API and the page that drives it.

Deliberately boring. It is stdlib http.server on a daemon thread, and it does
exactly two things to the scheduler: read the snapshot the render loop
published, and push a command onto its queue. It never renders, never touches
the rotation directly, and never holds a lock the frame loop wants -- a client
on a bad phone connection cannot stall the wall, and neither can a hundred of
them.

There is no authentication. This is a wall in a makerspace and the server is
meant to be reachable from anyone's phone on the shop wifi; the worst anyone
can do is change what is on the wall, which is the entire point of it. Do not
expose it to the internet -- bind it to the LAN or to a Tailscale address.

The page is served from ftsched_ui.html next door, read per request so it can
be edited against a running daemon. It is a couple of KB and the request rate
is one page load per person, so caching it would be optimising the wrong side.
"""

import json
import os
import posixpath
import socket
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(_HERE, "ftsched_ui.html")

# Commands the API will forward. An allowlist rather than passing the string
# through, so a typo in a client is a 400 and not a stack trace in the render
# loop's command handler.
OPS = {"jump": ("index",), "toggle": ("name", "on"), "all": ("on",),
       "next": (), "pause": (), "resume": (), "restart": ()}

MAX_BODY = 4096


class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = "ftsched"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *a):
        pass                                 # one line per poll per client, no

    def _send(self, code, body, ctype, cache=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                             # someone closed a tab

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json", "no-store")

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(UI_FILE, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8", "no-store")
            except OSError as exc:
                self._send(500, "no UI: %s" % exc, "text/plain")
        elif path == "/api/state":
            self._json(200, self.server.sched.snapshot())
        elif path.startswith("/previews/"):
            self._preview(path[len("/previews/"):])
        else:
            self._send(404, "no", "text/plain")

    def _preview(self, name):
        # posixpath.basename after normpath: the previews directory is flat,
        # so anything with structure in it is someone probing, not a client.
        name = posixpath.basename(posixpath.normpath("/" + name))
        if not name.endswith(".gif"):
            self._send(404, "no", "text/plain")
            return
        path = os.path.join(self.server.previews, name)
        try:
            with open(path, "rb") as fh:
                # Immutable in practice: a preview only changes when its demo
                # does, and then the daemon has been restarted anyway.
                self._send(200, fh.read(), "image/gif", "max-age=86400")
        except OSError:
            self._send(404, "no preview", "text/plain")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/command":
            self._send(404, "no", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= MAX_BODY:
            self._json(400, {"error": "bad body length"})
            return
        try:
            payload = json.loads(self.rfile.read(n))
            op = payload.pop("op")
        except Exception as exc:
            self._json(400, {"error": "bad JSON: %s" % exc})
            return
        if op not in OPS:
            self._json(400, {"error": "unknown op %r" % op})
            return
        missing = [k for k in OPS[op] if k not in payload]
        if missing:
            self._json(400, {"error": "%s needs %s" % (op, ", ".join(missing))})
            return
        if not self.server.sched.submit(op, payload):
            self._json(503, {"error": "command queue full"})
            return
        # The command runs at the top of the next frame, so the state returned
        # here is still the old one. The page re-polls rather than trusting it.
        self._json(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # A phone that walks out of wifi range leaves a socket behind; without
    # this the handler thread sits in recv until the TCP keepalive gives up.
    timeout = 30

    def __init__(self, addr, sched, previews):
        ThreadingHTTPServer.__init__(self, addr, Handler)
        self.sched = sched
        self.previews = previews


def parse_addr(spec, default_port=8081):
    """host:port, :port, or host. IPv6 needs the brackets: [::]:8081."""
    spec = spec.strip()
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = int(rest.lstrip(":")) if rest.lstrip(":") else default_port
        return host, port
    host, _, port = spec.rpartition(":")
    if not _:
        return spec, default_port
    return (host or "0.0.0.0"), int(port or default_port)


def serve(listen, sched, previews, warn):
    """Start the control server on a daemon thread. Returns it, or None."""
    host, port = parse_addr(listen)
    try:
        server = Server((host, port), sched, previews)
    except OSError as exc:
        # A wall that plays without its control panel is much better than a
        # wall that will not start because the port is taken.
        warn("control server not started on %s:%d (%s)" % (host, port, exc))
        return None
    threading.Thread(target=server.serve_forever, name="ftsched-http",
                     daemon=True).start()
    warn("control on http://%s:%d/" % (host if host not in ("", "0.0.0.0")
                                       else _my_address(), port))
    return server


def _my_address():
    """A plausible address to print, so the log line is clickable.

    Connecting a UDP socket sends nothing; it just asks the routing table
    which source address would be used to reach the outside, which is the one
    worth printing when we are bound to everything.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))          # TEST-NET-1, guaranteed unrouted
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()
