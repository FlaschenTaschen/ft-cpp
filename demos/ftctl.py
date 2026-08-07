#!/usr/bin/env python3
"""The wall's global state: on/off, brightness, wipe -- and its way into MQTT.

ft_server owns the panel and composites every client onto it, so how bright the
wall is and whether it is showing anything belong to it rather than to any one
client. It exposes those on a unix socket as lines of text. This is the only
thing that speaks that protocol; everything else comes through here.

Why a daemon of its own rather than a few routes inside ftsched:

  The whole point of a global off switch is that it works when the rest is not.
  ftsched drives the rotation on a frame deadline and is BindsTo=ft_server, so
  it restarts whenever the server does; if "turn the wall off" lived in it, the
  switch would be missing exactly when somebody is trying to deal with a
  misbehaving wall. This has no dependency on ftsched at all: it reads it when
  it is there, reports it as absent when it is not, and controls the display
  either way.

  It is also the thing that remembers. ft_server deliberately does not persist
  brightness -- after it drops privileges it can write nowhere, and a dragged
  slider is dozens of commands a second, which is not something to point at an
  SD card. So desired state lives here, and gets re-applied when the generation
  in `get` changes, which is how a server restart is noticed.

Reads are served from a cache refreshed on a timer, never by doing a socket
round trip inside a request. ftsched's web API made the same promise for the
same reason: a phone on bad wifi must not be able to stall anything by holding
a request open. Writes do go straight through, with a timeout, and update the
cache optimistically so a slider does not snap back under the finger.

MQTT is optional. Without paho-mqtt installed this is still a working HTTP
control API; with it, the wall appears in Home Assistant as one device -- a
light for the display itself, and separate entities for the rotation, so that
"off" and "paused" stay different things. See --mqtt-host.

Run:
  python3 ftctl.py --socket /run/ft/control.sock --listen 127.0.0.1:8082
  python3 ftctl.py --mqtt-host mqtt.lan --mqtt-user ha --mqtt-pass ...
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))

# One command per connection, so these are per-command and not per-session.
SOCKET_TIMEOUT = 2.0
SCHED_TIMEOUT = 2.0
POLL_SECONDS = 1.0

# Brightness the wall reports in percent, 1..100. Home Assistant lights want
# 0..255. Converting in one place, and rounding so that a round trip through
# HA does not drift a percent at a time.
HA_BRIGHTNESS_SCALE = 255


def pct_to_ha(percent):
    return max(1, min(HA_BRIGHTNESS_SCALE,
                      int(round(percent * HA_BRIGHTNESS_SCALE / 100.0))))


def ha_to_pct(value):
    return max(1, min(100, int(round(value * 100.0 / HA_BRIGHTNESS_SCALE))))


class Control(object):
    """The ft_server control socket. One connection per command."""

    def __init__(self, path):
        self.path = path

    def _round_trip(self, line):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        try:
            sock.connect(self.path)
            sock.sendall((line + "\n").encode("ascii"))
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("ascii", "replace")
        finally:
            sock.close()

    def get(self):
        """Current display state as a dict, or None if the server is not there.

        Absent is a normal condition -- the server restarts, or was started
        without --control-socket -- so it is reported rather than raised.
        """
        try:
            reply = self._round_trip("get")
        except (socket.error, OSError):
            return None
        state = {}
        for row in reply.splitlines():
            if not row.strip():
                continue
            key, _, value = row.partition(" ")
            try:
                state[key] = int(value)
            except ValueError:
                state[key] = value
        if "brightness" not in state:
            return None            # not a reply we understand
        return {
            "brightness": state["brightness"],
            "blanked": bool(state.get("blanked")),
            "dimmer": bool(state.get("dimmer")),
            "width": state.get("width"),
            "height": state.get("height"),
            "generation": state.get("generation"),
        }

    def command(self, line):
        """(ok, message). ok is False both for a refusal and for no server."""
        try:
            reply = self._round_trip(line).strip()
        except (socket.error, OSError) as exc:
            return False, "display server unreachable: %s" % exc
        if reply == "ok":
            return True, ""
        return False, reply[4:].strip() if reply.startswith("err ") else reply


class Scheduler(object):
    """ftsched's HTTP API, as far as we need it. Absence is expected."""

    def __init__(self, base):
        self.base = base.rstrip("/")

    def _fetch(self, path, payload=None):
        url = self.base + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=SCHED_TIMEOUT) as fh:
            return json.loads(fh.read().decode("utf-8"))

    def state(self):
        try:
            return self._fetch("/api/state")
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def command(self, op, **fields):
        payload = {"op": op}
        payload.update(fields)
        try:
            self._fetch("/api/command", payload)
            return True, ""
        except urllib.error.HTTPError as exc:
            try:
                return False, json.loads(exc.read().decode("utf-8")).get(
                    "error", str(exc))
            except Exception:
                return False, str(exc)
        except (urllib.error.URLError, OSError) as exc:
            return False, "scheduler unreachable: %s" % exc


class Desired(object):
    """What we want the display to be, across restarts of anything.

    Small enough that it is written whole, and only when it changes -- this
    sits on an SD card that a wall in a workshop will outlive only if nothing
    writes to it every time a slider moves.
    """

    def __init__(self, path):
        self.path = path
        self.brightness = None      # None until something has set one
        self.blanked = False
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(saved.get("brightness"), int):
            self.brightness = saved["brightness"]
        self.blanked = bool(saved.get("blanked"))

    def save(self):
        if not self.path:
            return
        blob = {"brightness": self.brightness, "blanked": self.blanked}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(blob, fh)
            os.replace(tmp, self.path)      # atomic; a torn file is unreadable
        except OSError as exc:
            sys.stderr.write("ftctl: cannot write %s (%s)\n" % (self.path, exc))


class Bridge(object):
    """Cache, reconciler, and the single place either backend is written to."""

    def __init__(self, control, scheduler, desired, pause_when_off):
        self.control = control
        self.scheduler = scheduler
        self.desired = desired
        self.pause_when_off = pause_when_off
        self.lock = threading.Lock()
        self.display = None            # last good control state, or None
        self.sched = None              # last good ftsched state, or None
        self._generation = None
        self._listeners = []           # called after every refresh
        self._stop = threading.Event()

    # -- reads ------------------------------------------------------------

    def snapshot(self):
        with self.lock:
            return {"display": self.display, "scheduler": self.sched}

    def add_listener(self, fn):
        self._listeners.append(fn)

    # -- writes -----------------------------------------------------------
    #
    # Straight through to the socket rather than into ftsched's command queue:
    # that one is bounded and drains at the render loop's pace, so a dragged
    # slider would push rotation commands out of it.

    def set_brightness(self, percent):
        percent = max(1, min(100, int(percent)))
        ok, err = self.control.command("brightness %d" % percent)
        if ok:
            self.desired.brightness = percent
            self.desired.save()
            self._patch(brightness=percent)
        return ok, err

    def set_blanked(self, blanked):
        ok, err = self.control.command("blank %s" % ("on" if blanked else "off"))
        if not ok:
            return ok, err
        self.desired.blanked = bool(blanked)
        self.desired.save()
        self._patch(blanked=bool(blanked))
        # The policy that "off" also stops the rotation lives here and nowhere
        # else. ft_server has never heard of ftsched, and having ftsched watch
        # the display state would point the dependency the wrong way. Failing
        # to reach the scheduler is not a failure of turning the wall off.
        if self.pause_when_off:
            self.scheduler.command("pause" if blanked else "resume")
        return True, ""

    def wipe(self):
        return self.control.command("wipe")

    def _patch(self, **fields):
        """Fold an accepted write into the cache, so reads do not lag a poll."""
        with self.lock:
            if self.display is not None:
                self.display = dict(self.display, **fields)

    # -- the refresh loop -------------------------------------------------

    def run(self):
        while not self._stop.is_set():
            display = self.control.get()
            sched = self.scheduler.state()
            with self.lock:
                self.display = display
                self.sched = sched
            if display is not None:
                self._reconcile(display)
            for fn in self._listeners:
                try:
                    fn()
                except Exception as exc:            # a listener is not the boss
                    sys.stderr.write("ftctl: listener failed (%s)\n" % exc)
            self._stop.wait(POLL_SECONDS)

    def stop(self):
        self._stop.set()

    def _reconcile(self, display):
        """Re-apply what we want after the server has been restarted.

        `generation` is stamped once per run of ft_server, so a change in it is
        a restart -- which is cheaper and more certain than inferring one from
        the brightness having gone back to whatever --led-brightness said.
        """
        generation = display.get("generation")
        first_sight = self._generation is None
        if generation == self._generation:
            return
        self._generation = generation
        if first_sight and self.desired.brightness is None:
            # Nothing to restore and nothing remembered: adopt what the server
            # booted with, so the first slider position is honest.
            self.desired.brightness = display.get("brightness")
            return
        if self.desired.brightness is not None and \
                display.get("brightness") != self.desired.brightness:
            self.control.command("brightness %d" % self.desired.brightness)
        if self.desired.blanked != display.get("blanked"):
            self.control.command(
                "blank %s" % ("on" if self.desired.blanked else "off"))


# -- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = "ftctl"

    def log_message(self, fmt, *a):
        pass                        # one line per poll per client, no thank you

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, blob):
        self._send(code, json.dumps(blob), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/api/display", "/api/display/"):
            self._json(200, self.server.bridge.snapshot())
        elif path == "/api/thumbnail.png":
            self._thumbnail()
        elif path == "/healthz":
            # Deliberately 200 even with the display unreachable: this process
            # being alive and the wall being reachable are different questions,
            # and conflating them makes a restart loop out of a blank wall.
            self._send(200, "ok\n", "text/plain")
        else:
            self._send(404, "no\n", "text/plain")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] not in ("/api/display", "/api/display/"):
            self._send(404, "no\n", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 4096:
            self._json(400, {"error": "bad body length"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError as exc:
            self._json(400, {"error": "bad JSON: %s" % exc})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "expected an object"})
            return

        bridge = self.server.bridge
        problems = []
        if "brightness" in payload:
            try:
                percent = int(payload["brightness"])
            except (TypeError, ValueError):
                problems.append("brightness must be a number")
            else:
                ok, err = bridge.set_brightness(percent)
                if not ok:
                    problems.append(err)
        if "blanked" in payload:
            ok, err = bridge.set_blanked(bool(payload["blanked"]))
            if not ok:
                problems.append(err)
        if payload.get("wipe"):
            ok, err = bridge.wipe()
            if not ok:
                problems.append(err)
        if not problems and not payload:
            problems.append("nothing to do")

        if problems:
            self._json(502 if "unreachable" in problems[0] else 400,
                       {"error": "; ".join(problems)})
            return
        self._json(200, bridge.snapshot())

    def _thumbnail(self):
        """A PNG of the demo currently playing.

        This is the preview of what is on rather than a capture of the panel:
        only ft_server knows the composite, and it does not hand it out yet. It
        is labelled accordingly in Home Assistant. Cached by name, because
        Home Assistant will ask for it about once a second and converting the
        same file each time on a Pi 3 is a waste.
        """
        sched = self.server.bridge.snapshot()["scheduler"]
        name = (sched or {}).get("now", {}).get("name")
        if not name:
            self._send(404, "nothing playing\n", "text/plain")
            return
        png = self.server.thumbnail(name)
        if png is None:
            self._send(404, "no preview for %s\n" % name, "text/plain")
            return
        self._send(200, png, "image/png")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    timeout = 30

    def __init__(self, addr, bridge, previews):
        ThreadingHTTPServer.__init__(self, addr, Handler)
        self.bridge = bridge
        self.previews = previews
        self._thumb_name = None
        self._thumb_png = None

    def thumbnail(self, name):
        if name == self._thumb_name:
            return self._thumb_png
        png = None
        path = os.path.join(self.previews, name + ".webp")
        try:
            from PIL import Image
            import io
            with Image.open(path) as img:
                img.seek(0)                     # first frame of the animation
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "PNG")
                png = buf.getvalue()
        except Exception:
            png = None                          # missing preview, or no Pillow
        self._thumb_name, self._thumb_png = name, png
        return png


def parse_addr(spec, default_port=8082):
    spec = spec.strip()
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = int(rest.lstrip(":")) if rest.lstrip(":") else default_port
        return host, port
    host, sep, port = spec.rpartition(":")
    if not sep:
        return spec, default_port
    return host, int(port)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--socket", default="/run/ft/control.sock",
                    help="ft_server's control socket")
    ap.add_argument("--listen", default="127.0.0.1:8082",
                    help="HTTP control API; nginx is the way in from outside")
    ap.add_argument("--scheduler", default="http://127.0.0.1:8081",
                    help="ftsched's base URL; absence is handled, not fatal")
    ap.add_argument("--previews", default=os.path.join(_HERE, "previews"),
                    help="where the baked demo previews live")
    ap.add_argument("--state-file", default="/var/lib/ftctl/desired.json",
                    help="remembered brightness and on/off; '' to not persist")
    ap.add_argument("--no-pause-when-off", dest="pause_when_off",
                    action="store_false",
                    help="leave the rotation running while the wall is dark")
    # -- MQTT / Home Assistant
    ap.add_argument("--mqtt-host", default=None,
                    help="broker; without this, MQTT is simply not started")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)
    ap.add_argument("--mqtt-prefix", default="ft/betelgeuse",
                    help="topic prefix for this wall")
    ap.add_argument("--mqtt-discovery-prefix", default="homeassistant")
    ap.add_argument("--node-id", default="betelgeuse",
                    help="stable id; it ends up in the HA entity ids")
    ap.add_argument("--friendly-name", default="Betelgeuse")
    ap.add_argument("--public-url", default=None,
                    help="how Home Assistant can reach this wall's web page, "
                         "e.g. http://betelgeuse.local/ -- used for the "
                         "thumbnail and the device's configuration link")
    args = ap.parse_args()

    if args.state_file:
        directory = os.path.dirname(args.state_file)
        if directory and not os.path.isdir(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                sys.stderr.write("ftctl: no state directory (%s)\n" % exc)

    control = Control(args.socket)
    scheduler = Scheduler(args.scheduler)
    desired = Desired(args.state_file)
    bridge = Bridge(control, scheduler, desired, args.pause_when_off)

    host, port = parse_addr(args.listen)
    httpd = Server((host, port), bridge, args.previews)

    mqtt_bridge = None
    if args.mqtt_host:
        import ftctl_mqtt
        mqtt_bridge = ftctl_mqtt.start(bridge, args)
        if mqtt_bridge is None:
            sys.stderr.write("ftctl: MQTT not started; HTTP still serving\n")
    else:
        # Say so. Silence here is indistinguishable from a broker that is simply
        # quiet, and the way this goes wrong in practice is a systemd drop-in
        # whose Environment= lines sit above the [Service] header -- systemd
        # discards them with a warning nobody is looking for, --mqtt-host arrives
        # empty, and the wall never appears in Home Assistant with nothing at all
        # to suggest why.
        sys.stderr.write("ftctl: no --mqtt-host, so no Home Assistant; "
                         "HTTP control API only\n")

    poller = threading.Thread(target=bridge.run)
    poller.daemon = True
    poller.start()

    sys.stderr.write("ftctl: %s -> http://%s:%d, display socket %s\n"
                     % (args.friendly_name, host, port, args.socket))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        if mqtt_bridge is not None:
            mqtt_bridge.stop()


if __name__ == "__main__":
    main()
