#!/usr/bin/env python3
"""The front door: the wall's web app on :80, with the panel at the root.

ftsched serves its control panel on 8081, which is fine for a checkout and
wrong for an installation. A port number is something you have to be told, and
someone standing in front of a 320x64 LED wall with a phone types the hostname
and nothing else. So this owns the root: it reverse-proxies ftsched at `/`,
and keeps one page of its own at `/about` -- what the wall is and how to push
your own pixels at it, which the panel has no business explaining.

Everything therefore lives in one origin on one port. That is not just tidier:
it is what lets the panel be reached over TLS at all without the page having
to know which of its links need a different port and scheme, and it means the
same URLs work on the shop wifi and over the tailnet.

It is a separate daemon from ftsched rather than another route inside it. The
scheduler is driving the wall on a frame deadline; the front door is the thing
most likely to be hit by a room full of curious people, and it can be
restarted, reloaded and got wrong without touching the render loop. When
ftsched is down this answers 502 with a page that says so, which is a great
deal better than a connection refused on the one URL anybody knows.

Preview images are the exception: they are served straight off disk here
rather than proxied. They are the overwhelming majority of the bytes -- a cold
page load is three dozen files and a couple of megabytes, against a 5 kB poll
once a second -- and putting that burst through ftsched would run it through
the GIL the render loop is waiting on, which is the whole thing keeping the
front door in its own process was meant to avoid.

What is left to proxy is small, so proxying is deliberately dumb: one upstream
request per request, no connection reuse, no caching. A pool would be more
moving parts than a 1 Hz poll per phone in the room earns.

Binding :80 does not need root: the unit grants CAP_NET_BIND_SERVICE and runs
as pi. See ftindex.service.

TLS is not handled here. `tailscale serve` terminates it with a real ts.net
certificate, renews it, and exposes it to the tailnet only:

  tailscale serve --bg --https=443 http://127.0.0.1:80

Run:  python3 ftindex.py --listen 0.0.0.0:8080
      python3 ftindex.py --listen 0.0.0.0:80 --panel-port 8081
"""

import argparse
import json
import os
import posixpath
import shutil
import socket
import sys
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(_HERE, "ftindex.html")

# How long to wait on ftsched when rendering "now playing" on our own page.
# That page is worth more than the status line on it, so this is short enough
# that a wedged scheduler costs a blink rather than a spinner.
STATE_TIMEOUT = 0.6

# How long to wait when proxying. Longer, because this one is the answer
# rather than a garnish on it, but still bounded: a stuck upstream must not
# park a thread for ever.
PROXY_TIMEOUT = 15

MAX_BODY = 4096                              # ftsched's commands are tiny

# Previews are animated WebP; GIF stays servable so a checkout that has not
# re-baked still shows pictures rather than nothing.
PREVIEW_TYPES = {".webp": "image/webp", ".gif": "image/gif"}

# Headers that describe one hop and must not be forwarded to the next.
HOP_BY_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade"))


def load_state(port):
    """ftsched's snapshot, or None if it is not answering."""
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

    # -- routes -----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.rstrip("/") == "/about":
            self._about()
        elif path == "/healthz":
            # Ours, not ftsched's: this answers whether the front door is up,
            # which is the question a supervisor is asking.
            self._send(200, "ok\n", "text/plain")
        elif path.startswith("/previews/"):
            self._preview(path[len("/previews/"):])
        else:
            self._proxy()

    def _preview(self, name):
        """Served from disk here rather than proxied.

        Previews are the overwhelming majority of the bytes -- a cold page
        load is three dozen files and a couple of megabytes, against a 5 kB
        poll once a second -- and ftsched is the process holding a frame
        deadline. Proxying them would put that whole burst through the GIL the
        render loop is waiting on, which is precisely what keeping the front
        door in its own process was meant to avoid. They are static files
        sitting in the same checkout, so this reads them directly and ftsched
        never hears about it. It also means the wall's pictures still load
        when the scheduler is down.
        """
        name = posixpath.basename(posixpath.normpath("/" + name))
        ctype = PREVIEW_TYPES.get(os.path.splitext(name)[1].lower())
        if not ctype:
            self._send(404, "no\n", "text/plain")
            return
        try:
            with open(os.path.join(self.server.args.previews, name), "rb") as fh:
                data = fh.read()
        except OSError:
            self._send(404, "no preview\n", "text/plain")
            return
        # Immutable in practice: a preview only changes when its demo does,
        # and then the daemon has been restarted anyway.
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        self._proxy()

    # -- the one page of our own -------------------------------------------

    def _about(self):
        args = self.server.args
        state = load_state(args.panel_port)
        if state is None:
            status = ('<span class="dot bad"></span>'
                      "the scheduler is not answering")
        else:
            now = state.get("now") or {}
            health = state.get("health") or {}
            live = sum(1 for r in state.get("rotation") or []
                       if r.get("enabled"))
            status = (
                '<span class="dot ok"></span>now playing '
                '<b>%s</b> <span class="dim">&middot; %d of %d &middot; '
                '%.0f fps</span>' % (
                    esc(now.get("name", "?")),
                    (now.get("position", 0) or 0) + 1, live,
                    health.get("actual_fps") or 0.0))

        try:
            with open(PAGE_FILE, "r", encoding="utf-8") as fh:
                page = fh.read()
        except OSError as exc:
            self._send(500, "no page: %s\n" % exc, "text/plain")
            return

        host = self.headers.get("Host") or socket.gethostname()
        if host.startswith("["):                         # [::1]:80
            host = host[1:host.index("]")] if "]" in host else host
        elif ":" in host:
            host = host.rsplit(":", 1)[0]

        page = (page
                .replace("{{STATUS}}", status)
                .replace("{{HOST}}", esc(host))
                .replace("{{FTPORT}}", str(args.ft_port))
                .replace("{{WIDTH}}", str(args.width))
                .replace("{{HEIGHT}}", str(args.height)))
        self._send(200, page, "text/html; charset=utf-8")

    # -- everything else is ftsched ----------------------------------------

    def _proxy(self):
        args = self.server.args
        body = None
        if self.command == "POST":
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if not 0 < n <= MAX_BODY:
                self._send(400, "bad body length\n", "text/plain")
                return
            body = self.rfile.read(n)

        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (args.panel_port, self.path),
            data=body, method=self.command)
        for name in ("Content-Type", "Accept", "Accept-Encoding",
                     "If-None-Match", "If-Modified-Since"):
            value = self.headers.get(name)
            if value:
                req.add_header(name, value)

        try:
            upstream = urllib.request.urlopen(req, timeout=PROXY_TIMEOUT)
        except urllib.error.HTTPError as exc:
            # 404 for a preview that is not baked, 400 for a bad command:
            # ftsched's answer, not an error in the proxy. Pass it through.
            upstream = exc
        except Exception:
            self._unreachable()
            return

        try:
            self.send_response(upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                # copyfileobj rather than read(): a preview is a couple of
                # hundred kB and there is no reason for it to be resident.
                shutil.copyfileobj(upstream, self.wfile, 64 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass                                         # someone closed a tab
        finally:
            upstream.close()

    def _unreachable(self):
        """ftsched is not there. Say which of the two is broken."""
        self._send(502, PANEL_DOWN, "text/html; charset=utf-8")


PANEL_DOWN = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Flaschen Taschen</title>
<style>
 body { background:#0b0d12; color:#e8ecf4; margin:0; padding:14vh 20px;
        font:16px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif; }
 main { max-width:32rem; margin:0 auto; }
 h1 { font-size:22px; margin:0 0 10px; }
 p { color:#8891a5; margin:0 0 14px; }
 a { color:#ffb03a; }
</style>
<main>
  <h1>The scheduler is not answering</h1>
  <p>The wall's front door is up &mdash; you reached this page &mdash; but the
     process that drives the panels is not responding, so there is nothing to
     show you and probably nothing on the wall.</p>
  <p>It restarts itself on failure, so this may clear on its own in a few
     seconds. <a href="/">Try again</a>, or
     <a href="/about">read about the wall</a> meanwhile.</p>
</main>
"""


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
    ap.add_argument("--previews",
                    default=os.path.join(_HERE, "previews"),
                    help="directory of <name>.webp previews, served from here "
                         "rather than proxied")
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
    sys.stderr.write("ftindex: http://%s:%d/ (panel proxied from :%d)\n"
                     % (host if host not in ("", "0.0.0.0") else
                        socket.gethostname(), port, args.panel_port))
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
