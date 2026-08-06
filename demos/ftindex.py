#!/usr/bin/env python3
"""The front door: a landing page on :80 so people can find the wall.

The control panel lives on port 8081, which is fine if you already know it and
useless if you do not. Someone standing in front of a 320x64 LED wall with a
phone will type the hostname and nothing else, get a connection refused, and
give up. This serves that bare hostname: what the wall is, what is on it right
now, a button to the panel, and how to push your own pixels at it.

It is deliberately a separate daemon from ftsched rather than another route
inside it. ftsched drives the wall on a frame deadline, and the thing most
likely to be hit by a room full of curious people should not share a process
-- let alone a GIL -- with the render loop. This one holds no state, can be
restarted at any time, and if ftsched is down it says so instead of vanishing.

Binding :80 does not need root: the unit grants CAP_NET_BIND_SERVICE and runs
as pi. See ftindex.service.

TLS is not handled here either. `tailscale serve` terminates it with a real
ts.net certificate, renews it, and exposes it only to the tailnet, which is
three things this would otherwise have to get right on its own:

  tailscale serve --bg --https=443  http://127.0.0.1:80     # this page
  tailscale serve --bg --https=8443 http://127.0.0.1:8081   # the panel

That needs HTTPS Certificates enabled for the tailnet (admin console -> DNS).
Until it is, `tailscale cert` returns "your Tailscale account does not support
getting TLS certs" and the plain HTTP side is unaffected.

Run:  python3 ftindex.py --listen 0.0.0.0:8080
      python3 ftindex.py --listen 0.0.0.0:80 --panel-port 8081
"""

import argparse
import json
import os
import socket
import sys

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(_HERE, "ftindex.html")

# How long to wait on ftsched when rendering "now playing". The page is worth
# more than the status line on it, so this is short enough that a wedged
# scheduler costs a blink rather than a spinner.
STATE_TIMEOUT = 0.6


def load_state(port):
    """ftsched's snapshot, or None if it is not answering."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port,
                                    timeout=STATE_TIMEOUT) as fh:
            return json.loads(fh.read().decode("utf-8"))
    except Exception:
        return None                          # down, starting, wedged: same page


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = "ftindex"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # -- where the reader actually is -------------------------------------

    def scheme(self):
        """http, or https when tailscale serve is terminating TLS for us.

        Trusting X-Forwarded-Proto is only safe because nothing else can set
        it: the only proxy in front of this is tailscaled on the loopback, and
        the header decides link text rather than anything security-relevant.
        """
        return "https" if self.headers.get(
            "X-Forwarded-Proto", "").lower() == "https" else "http"

    def host(self):
        """The name the reader typed, without any port."""
        host = self.headers.get("Host") or self.server.server_address[0]
        if host.startswith("["):                         # [::1]:80
            return host[1:host.index("]")] if "]" in host else host
        return host.rsplit(":", 1)[0] if ":" in host else host

    def panel_url(self):
        """The panel, reachable the same way this page was.

        A link from an https page to an http one is not blocked the way a
        subresource would be, but it drops the reader out of TLS without
        saying so. If we were reached over the tailnet, point at the panel's
        tailnet port instead.
        """
        args = self.server.args
        if self.scheme() == "https":
            return "https://%s:%d/" % (self.host(), args.panel_tls_port)
        return "http://%s:%d/" % (self.host(), args.panel_port)

    # -- routes -----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._index()
        elif path == "/panel":
            # A short, typeable way to get to the panel, and what the button
            # on the page uses -- so the redirect is the single place that
            # knows which port the panel is on.
            self._send(302, b"", "text/plain",
                       {"Location": self.panel_url()})
        elif path == "/healthz":
            self._send(200, "ok\n", "text/plain")
        else:
            self._send(404, "no\n", "text/plain")

    def _index(self):
        args = self.server.args
        state = load_state(args.panel_port)

        if state is None:
            status = ('<span class="dot bad"></span>'
                      "the scheduler is not answering")
        else:
            now = state.get("now") or {}
            health = state.get("health") or {}
            live = state.get("rotation") and sum(
                1 for r in state["rotation"] if r.get("enabled"))
            status = (
                '<span class="dot ok"></span>now playing '
                '<b>%s</b> <span class="dim">&middot; %d of %d &middot; '
                '%.0f fps</span>' % (
                    esc(now.get("name", "?")),
                    (now.get("position", 0) or 0) + 1, live or 0,
                    health.get("actual_fps") or 0.0))

        try:
            with open(PAGE_FILE, "r", encoding="utf-8") as fh:
                page = fh.read()
        except OSError as exc:
            self._send(500, "no page: %s\n" % exc, "text/plain")
            return

        page = (page
                .replace("{{PANEL}}", esc(self.panel_url()))
                .replace("{{STATUS}}", status)
                .replace("{{HOST}}", esc(self.host()))
                .replace("{{FTPORT}}", str(args.ft_port))
                .replace("{{WIDTH}}", str(args.width))
                .replace("{{HEIGHT}}", str(args.height)))
        self._send(200, page, "text/html; charset=utf-8")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    timeout = 30

    def __init__(self, addr, args):
        ThreadingHTTPServer.__init__(self, addr, Handler)
        self.args = args


def parse_addr(spec, default_port=80):
    """host:port, :port, or host. IPv6 needs the brackets: [::]:80."""
    spec = spec.strip()
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = int(rest.lstrip(":")) if rest.lstrip(":") else default_port
        return host, port
    host, sep, port = spec.rpartition(":")
    if not sep:
        return spec, default_port
    return (host or "0.0.0.0"), int(port or default_port)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0:80")
    ap.add_argument("--panel-port", type=int, default=8081,
                    help="where ftsched's control panel is")
    ap.add_argument("--panel-tls-port", type=int, default=8443,
                    help="where tailscale serve exposes the panel over TLS")
    ap.add_argument("--ft-port", type=int, default=1337,
                    help="the wall's own UDP port, for the instructions")
    ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--height", type=int, default=64)
    args = ap.parse_args()

    host, port = parse_addr(args.listen)
    try:
        server = Server((host, port), args)
    except OSError as exc:
        sys.stderr.write("ftindex: cannot listen on %s:%d (%s)\n"
                         % (host, port, exc))
        raise SystemExit(1)
    sys.stderr.write("ftindex: http://%s:%d/\n"
                     % (host if host not in ("", "0.0.0.0") else
                        socket.gethostname(), port))
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
