#!/usr/bin/env python3
"""The wall's status as a login banner, rendered ahead of time.

The obvious way to do this is a script in /etc/update-motd.d/ that asks the wall
how it is doing. On this Pi that costs two to three seconds -- interpreter
startup, an HTTP call, `systemctl is-active`, `vcgencmd` -- on a machine that is
already CPU-starved and, right now, voltage-throttled. Three seconds of staring
at nothing before every prompt is not a banner, it is a penalty.

So nothing is computed at login. ftctl renders this to a file whenever the state
it already polls actually changes, and the login banner is a `cat`. That reuses
the change detection written for MQTT: a repaint happens when the effect changes
or somebody moves the brightness, a few times a minute, and never on a timer for
its own sake.

The expensive facts -- systemctl, vcgencmd -- are refreshed at most once a minute
and cached, so a burst of effect changes does not drag a subprocess along behind
each one.

The picture is the *preview* of what is playing, half-block rendered at the
wall's own 5:1 aspect. It is not a capture of the panel: only ft_server knows the
composite and it does not hand it out. The caption says so.

It reaches a login through /etc/motd, which is a symlink to the rendered file.
That is the one path pam_motd takes on every interactive session
(`session optional pam_motd.so noupdate`). The /etc/update-motd.d/ route looks
like the obvious home for this and is a trap here: pam_motd on this Debian does
not actually run `run-parts` for sshd sessions, so a script there is never
executed. The symlink also fails well -- ftctl's RuntimeDirectory goes away with
the daemon, so a wall with no ftctl prints nothing rather than something wrong.

Run by hand to see it, or to check it after editing:

  python3 ftmotd.py                 # to stdout
  python3 ftmotd.py --out /run/ft-motd/banner.ansi
"""

import argparse
import json
import os
import subprocess
import sys
import time

# The panel's own palette, so the terminal and the web UI look like one system.
AMBER = (255, 176, 58)
GREEN = (69, 209, 122)
GREY = (136, 145, 165)
RED = (255, 92, 92)
WHITE = (232, 236, 244)

ART_WIDTH = 62                    # columns
ART_ROWS = ART_WIDTH // 10        # two pixels per row, so W/(2*ROWS) == 5:1
EXTRAS_TTL = 60.0                 # seconds to cache systemctl/vcgencmd
SERVICES = ("ft_server", "ftsched", "ftctl", "nginx")


def rgb(colour, text, bold=False):
    return "\x1b[%s38;2;%d;%d;%dm%s\x1b[0m" % (
        "1;" if bold else "", colour[0], colour[1], colour[2], text)


def bar(fraction, width, colour):
    """Rounded down, so a full bar means full and nothing else does."""
    filled = int(max(0.0, min(1.0, fraction)) * width)
    return rgb(colour, "█" * filled) + rgb(GREY, "░" * (width - filled))


# -- the expensive facts ---------------------------------------------------

class Extras(object):
    """systemctl and vcgencmd, at most once a minute."""

    def __init__(self, ttl=EXTRAS_TTL):
        self.ttl = ttl
        self._at = 0.0
        self.services = []
        self.throttled_now = False
        self.throttled_ever = False
        self.throttled_raw = None

    def stale(self, now=None):
        return (now or time.time()) - self._at >= self.ttl

    def refresh(self):
        self._at = time.time()
        try:
            out = subprocess.run(["systemctl", "is-active"] + list(SERVICES),
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL,
                                 timeout=5).stdout.decode().split()
        except Exception:
            out = []
        out += ["?"] * (len(SERVICES) - len(out))
        self.services = list(zip(SERVICES, out))

        try:
            raw = subprocess.run(["vcgencmd", "get_throttled"],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL,
                                 timeout=5).stdout.decode()
            value = int(raw.strip().split("=")[1], 16)
        except Exception:
            return
        # Low bits are live; the 0x10000 range is "has happened since boot",
        # which deserves a quieter mention than something happening now.
        self.throttled_raw = value
        self.throttled_now = bool(value & 0x7)
        self.throttled_ever = bool(value & 0x70000)


# -- the picture -----------------------------------------------------------

def art(demo, previews, cache_dir):
    """Half-block render of an effect's preview, cached on disk by name."""
    if not demo:
        return None
    safe = "".join(c for c in demo if c.isalnum() or c in "-_")
    if not safe:
        return None
    cached = os.path.join(cache_dir, safe + ".ansi") if cache_dir else None
    if cached:
        try:
            with open(cached) as fh:
                return fh.read()
        except OSError:
            pass

    path = os.path.join(previews, safe + ".webp")
    try:
        # Imported here rather than at module scope: Pillow costs the better
        # part of a second to import on this Pi, and a caller that never needs
        # a picture should not pay for one.
        from PIL import Image
        with Image.open(path) as img:
            img.seek(0)                      # first frame of the animation
            frame = img.convert("RGB").resize((ART_WIDTH, ART_ROWS * 2),
                                              Image.BILINEAR)
        px = frame.load()
    except Exception:
        return None

    rows = []
    for row in range(ART_ROWS):
        cells = []
        for col in range(ART_WIDTH):
            top = px[col, row * 2]
            low = px[col, row * 2 + 1]
            # An upper half-block with the foreground painting the top pixel and
            # the background the bottom: one cell, two pixels, square aspect.
            cells.append("\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀"
                         % (top[0], top[1], top[2], low[0], low[1], low[2]))
        rows.append("".join(cells) + "\x1b[0m")
    rendered = "\n".join(rows)

    if cached:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cached + ".tmp", "w") as fh:
                fh.write(rendered)
            os.replace(cached + ".tmp", cached)
        except OSError:
            pass                             # an unwritable cache still renders
    return rendered


# -- the banner ------------------------------------------------------------

def render(display, sched, extras, previews, cache_dir, urls):
    now = (sched or {}).get("now") or {}
    health = (sched or {}).get("health") or {}
    demo = now.get("name")
    out = []
    pad = "  "

    title = "BETELGEUSE"
    letters = []
    for i, ch in enumerate(title):
        # Amber into a warmer red across the word: roughly what the wall looks
        # like with sunset on it.
        f = i / float(len(title) - 1)
        letters.append(rgb((255, int(176 - 90 * f), int(58 + 20 * f)), ch, True))
    size = "%d×%d" % (display["width"], display["height"]) \
        if display and display.get("width") else "320×64"
    out.append("")
    out.append(pad + " ".join(letters) + "   " +
               rgb(GREY, size + " · flaschen taschen"))
    out.append("")

    picture = art(demo, previews, cache_dir)
    if picture:
        for line in picture.split("\n"):
            out.append(pad + line)
        out.append(pad + rgb(GREY, "↑ preview of the effect playing, not a "
                                   "capture of the panel"))
        out.append("")

    if display is None:
        out.append(pad + rgb(RED, "●") + " " + rgb(WHITE, "wall     ") +
                   rgb(RED, "the display server is not answering"))
    else:
        pct = display["brightness"]
        lit = rgb(GREY, "blanked") if display["blanked"] else rgb(GREEN, "on")
        dot = rgb(GREY if display["blanked"] else GREEN, "●")
        out.append(pad + dot + " " + rgb(WHITE, "wall     ") + lit + "  " +
                   bar(pct / 100.0, 12, AMBER) + rgb(GREY, " %d%%" % pct) +
                   ("" if display.get("dimmer") else rgb(GREY, "  no dimmer")))

    if sched is None:
        out.append(pad + rgb(RED, "●") + " " + rgb(WHITE, "playing  ") +
                   rgb(RED, "the scheduler is not answering"))
    else:
        rotation = sched.get("rotation") or []
        live = sum(1 for e in rotation if e.get("enabled", True))
        actual = health.get("actual_fps") or 0.0
        target = health.get("target_fps") or 0.0
        dropped = health.get("dropped_packets") or 0
        paused = sched.get("paused")
        fps_colour = GREEN if target and actual >= target * 0.9 else AMBER
        out.append(
            pad + rgb(GREY if paused else GREEN, "●") + " " +
            rgb(WHITE, "playing  ") + rgb(AMBER, demo or "—", True) +
            rgb(GREY, "  %d/%d  " % ((now.get("position") or 0) + 1, live)) +
            bar((now.get("elapsed") or 0.0) / (now.get("duration") or 1.0),
                12, WHITE) +
            "  " + rgb(fps_colour, "%.0f/%.0f fps" % (actual, target)) +
            (rgb(GREY, " · paused") if paused else "") +
            (rgb(RED, " · %d dropped" % dropped) if dropped else ""))

    if extras.services:
        cells = [rgb(GREEN if state == "active" else RED, name)
                 for name, state in extras.services]
        out.append(pad + rgb(GREY, "●") + " " + rgb(WHITE, "services ") +
                   rgb(GREY, " ").join(cells))

    if extras.throttled_now:
        out.append(pad + rgb(RED, "⚠", True) + " " +
                   rgb(WHITE, "power    ") +
                   rgb(RED, "under-voltage and throttling right now", True) +
                   rgb(GREY, "  0x%x · ~/docs/hardware.md"
                       % extras.throttled_raw))
    elif extras.throttled_ever:
        out.append(pad + rgb(AMBER, "⚠") + " " + rgb(WHITE, "power    ") +
                   rgb(AMBER, "under-voltage has occurred since boot") +
                   rgb(GREY, "  0x%x" % extras.throttled_raw))

    out.append("")
    if urls.get("lan"):
        line = pad + rgb(GREY, "  reach  ") + rgb(AMBER, urls["lan"])
        if urls.get("tailnet"):
            line += rgb(GREY, "   tailnet ") + rgb(GREY, urls["tailnet"])
        out.append(line)
    out.append(pad + rgb(GREY, "  docs   ") + rgb(WHITE, "~/docs/README.md") +
               rgb(GREY, "   control  ") +
               rgb(WHITE, "python3 ~/ft-cpp/tools/ftc.py get"))
    # When this was painted. It is only repainted on a change, so a couple of
    # minutes old is normal and healthy -- but if ftctl has died, /etc/motd
    # dangles and nothing prints at all, which is the louder signal.
    out.append(pad + rgb(GREY, "  as of  " + time.strftime("%H:%M:%S")))
    out.append("")
    return "\n".join(out) + "\n"


def write(path, text):
    """Atomic, so a login can never read half a banner."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)                  # pam_motd reads this as root
    os.replace(tmp, path)


class Writer(object):
    """An ftctl listener: repaint when what a person would see has changed."""

    def __init__(self, path, previews, cache_dir, urls):
        self.path = path
        self.previews = previews
        self.cache_dir = cache_dir
        self.urls = urls
        self.extras = Extras()
        self._key = None

    def update(self, display, sched):
        now = (sched or {}).get("now") or {}
        key = (display and display.get("brightness"),
               display and display.get("blanked"),
               now.get("name"), (sched or {}).get("paused"),
               display is None, sched is None)
        refresh_extras = self.extras.stale()
        if key == self._key and not refresh_extras:
            return False
        if refresh_extras:
            self.extras.refresh()
        self._key = key
        write(self.path, render(display, sched, self.extras, self.previews,
                                self.cache_dir, self.urls))
        return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--ftctl", default="http://127.0.0.1:8082/api/display")
    ap.add_argument("--previews", default="/home/pi/ft-cpp/demos/previews")
    ap.add_argument("--cache", default="/run/ft-motd")
    ap.add_argument("--lan", default="http://betelgeuse.local/")
    ap.add_argument("--tailnet", default="")
    ap.add_argument("--out", default=None, help="write here instead of stdout")
    args = ap.parse_args()

    import urllib.request
    try:
        with urllib.request.urlopen(args.ftctl, timeout=2.0) as fh:
            state = json.loads(fh.read().decode("utf-8"))
    except Exception:
        state = {"display": None, "scheduler": None}

    extras = Extras()
    extras.refresh()
    text = render(state.get("display"), state.get("scheduler"), extras,
                  args.previews, args.cache,
                  {"lan": args.lan, "tailnet": args.tailnet})
    if args.out:
        write(args.out, text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
