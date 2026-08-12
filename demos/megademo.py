#!/usr/bin/env python3
"""Megademo: play the effects back to back as one continuous show.

This is the sequencer. It owns no effect of its own; it takes a playlist of
(effect, seconds, options) and drives the other demo modules through it,
blending one into the next instead of cutting.

  python3 megademo.py --host 127.0.0.1
  python3 megademo.py --playlist fire:20,tunnel:15:wipe,starfield:12
  python3 megademo.py --duration 60 --transition 1.5 --no-banner

Three things make this work:

  * demoscene.build(module) separates setup from the frame loop, so a render
    callback can be constructed without running anyone's loop. Two effects can
    therefore be alive and rendering at the same time, which is what a real
    transition needs.

  * The effect list is DATA (the PLAYLIST below), not code. Adding an
    effect to the show is adding its name to a list; modules are imported
    lazily by name and any that is missing from the checkout is skipped with a
    warning, so this runs on a tree where half the effects do not exist yet.

  * Builds happen on a background thread, a couple of segments ahead of the
    playhead. scroller.build() bakes for several seconds; doing that inline at
    the moment of the transition would freeze the wall, and doing every build
    up front would mean a slow start and every effect's tables resident at
    once. Building just ahead costs one worker thread and keeps two or three
    effects in memory, which is the trade a Pi 3 wants.

Transitions vary rather than repeating one throughout, because there is no
soundtrack to carry the pacing: a wipe between two textured effects, a fade
through black when the show goes from busy to sparse, a crossfade otherwise.

Needs numpy, Pillow (for the banner only), and the `flaschen` client from
../api/python.
"""

import importlib
import os
import sys
import threading
import time

import numpy as np

import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The show, as data.
# --------------------------------------------------------------------------

# Effects that put a few bright things on mostly black. The transition picker
# uses this: fading through black reads well going to or from one of these,
# where a wipe or crossfade mostly just looks like a smear.
SPARSE = {"starfield", "fireworks", "boing", "daliclock", "sierpinski_rain",
          # Membership is about *readability*, not luminance. These are small
          # type or a few sprites on black, and type blended under a busy
          # effect is unreadable rather than merely dim. gibson is deliberately
          # NOT here: once its towers became filled glass it stopped being a
          # few thin lines and became one of the densest things in the show.
          "wardial", "ansi", "toasters",
          "dither", "dvd", "wiki", "solarwind", "fine"}

# The default running order. Names not present in the checkout are dropped
# with a warning, so effects still being written can already be listed here
# and will join the show the moment their file lands.
#
# The ordering alternates: dense against sparse, warm against cool. A run of
# three busy warm effects reads as one long effect; swapping to a black field
# of stars in between is what makes each of them land.
PLAYLIST = [
    # (effect, seconds, {option: value})
    ("starfield",  16, {}),                       # sparse, cool: open quiet
    ("fire",       18, {}),                       # dense, warm
    ("tunnel",     18, {"palette": "ice"}),       # dense, cool
    ("boing",      14, {}),                       # sparse
    ("metaballs",  18, {"palette": "magma"}),     # dense, warm
    ("water",      16, {}),                       # dense, cool
    ("daliclock",  14, {}),                       # sparse
    ("floor",      16, {}),                       # dense
    ("fireworks",  16, {}),                       # sparse
    ("rotozoom",   18, {"palette": "rainbow"}),   # dense, warm
    ("cycle",      16, {}),                       # dense
    ("scroller",   26, {}),                       # the finale talks to you
]

BANNER_TEXT = ("FLASCHEN TASCHEN MEGADEMO   ***   "
               "ALL EFFECTS IN NUMPY, PUSHED OVER UDP   ***   "
               "GREETINGS TO EVERYONE STILL WRITING DEMOS   ***   ")


# --------------------------------------------------------------------------
# Transitions. Each takes two frames and k in 0..1, exact at both ends.
# --------------------------------------------------------------------------

def _t_cut(a, b, k, args):
    return b if k >= 1.0 else a


def _t_crossfade(a, b, k, args):
    return ds.crossfade(a, b, k)


def _t_wipe(a, b, k, args):
    return ds.wipe(a, b, k, args.wipe_softness)


def _t_black(a, b, k, args):
    """Out through black and back in. Both effects run the whole time, so the
    incoming one is already warmed up (and moving) when it appears."""
    if k < 0.5:
        return ds.fade_black(a, k * 2.0)
    return ds.fade_black(b, 2.0 - k * 2.0)


def _t_flash(a, b, k, args):
    """Out through white. Loud; used sparingly."""
    white = np.full(a.shape, 255, np.uint8)
    if k < 0.5:
        return ds.crossfade(a, white, k * 2.0)
    return ds.crossfade(white, b, k * 2.0 - 1.0)


TRANSITIONS = {
    "cut": _t_cut,
    "crossfade": _t_crossfade,
    "wipe": _t_wipe,
    "black": _t_black,
    "flash": _t_flash,
}


def pick_transition(from_name, to_name, index):
    """Choose a transition when the playlist did not name one.

    Busy to sparse (or back) wants the screen emptied first, or the sparse
    effect arrives invisible under the busy one. Two textured effects have
    enough going on that a wipe reads as a deliberate edge; anything else gets
    a crossfade. Alternating the two keeps a long show from feeling mechanical.
    """
    if from_name in SPARSE or to_name in SPARSE:
        return "black"
    return "wipe" if index % 2 else "crossfade"


# --------------------------------------------------------------------------
# Playlist parsing.
# --------------------------------------------------------------------------

def parse_playlist(spec, default_seconds):
    """`name[:seconds[:transition]][+key=value...]`, comma separated.

        fire:20,tunnel:15:wipe,rotozoom:12+palette=ice+spin=0.3

    Returns [(name, seconds, overrides, transition-or-None)].
    """
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        head, _, tail = item.partition("+")
        parts = head.split(":")
        name = parts[0].strip()
        seconds = float(parts[1]) if len(parts) > 1 and parts[1] else default_seconds
        trans = parts[2].strip() if len(parts) > 2 and parts[2] else None
        if trans and trans not in TRANSITIONS:
            raise SystemExit("megademo: unknown transition %r (have %s)"
                             % (trans, ", ".join(sorted(TRANSITIONS))))
        overrides = {}
        for kv in tail.split("+") if tail else []:
            if not kv:
                continue
            key, _, value = kv.partition("=")
            overrides[key.strip().replace("-", "_")] = value.strip()
        out.append((name, seconds, overrides, trans))
    if not out:
        raise SystemExit("megademo: empty playlist")
    return out


def coerce(value, current):
    """Turn a command line string into whatever type the option already holds."""
    if not isinstance(value, str) or current is None:
        return value
    if isinstance(current, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value


# --------------------------------------------------------------------------
# Segments: an effect module, its options, and its build.
# --------------------------------------------------------------------------

class Segment(object):
    """One entry of the resolved show. `render` is filled in by the builder."""

    def __init__(self, name, module, seconds, overrides, transition):
        self.name = name
        self.module = module
        self.seconds = seconds
        self.overrides = overrides
        self.transition = transition
        self.build_secs = 0.0


def resolve(entries):
    """Import each playlist entry's module, dropping the ones not here yet."""
    segments = []
    for name, seconds, overrides, transition in entries:
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            warn("skipping %s (not in this checkout: %s)" % (name, exc))
            continue
        if not hasattr(module, "build"):
            warn("skipping %s (no build())" % name)
            continue
        segments.append(Segment(name, module, seconds, overrides, transition))
    if not segments:
        raise SystemExit("megademo: no playable effects in the playlist")
    for i, seg in enumerate(segments):
        if seg.transition is None:
            prev = segments[i - 1].name
            seg.transition = pick_transition(prev, seg.name, i)
    return segments


def build_segment(seg, args):
    """Construct one effect's render callback at our canvas size.

    Options come from the module's own parser with an empty argv, so this
    never restates a default that could then drift from the effect's.
    """
    opts = ds.options(seg.module, width=args.width, height=args.height,
                      fps=args.fps, host=args.host, port=args.port)
    for key, value in seg.overrides.items():
        if not hasattr(opts, key):
            warn("%s has no option --%s, ignoring" % (seg.name, key.replace("_", "-")))
            continue
        setattr(opts, key, coerce(value, getattr(opts, key)))
    t0 = time.monotonic()
    render = seg.module.build(opts)
    seg.build_secs = time.monotonic() - t0
    return render


class Builder(object):
    """Builds upcoming segments on a worker thread, `lead` segments ahead.

    The playlist repeats forever, so this works on a global index: index i
    plays segments[i % n], and every pass gets a fresh build. That is on
    purpose — an effect that has been running for ten minutes has drifted
    somewhere odd, and a new instance starts from its own opening state.
    """

    def __init__(self, segments, args, lead):
        self.segments = list(segments)       # the live rotation
        self.args = args
        self.lead = max(1, lead)
        self.ready = {}                      # index -> render callback
        self.specs = {}                      # index -> the Segment it played
        self.want = 0                        # lowest index still needed
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.stop = False
        self.thread = None

    def spec(self, index):
        """Which segment index `index` is (or will be).

        The rotation can shrink — an effect whose build() raises is dropped
        rather than left to punch a hole in the show — so once an index has
        been built its segment is remembered, and only unbuilt indices follow
        the current rotation.
        """
        with self.lock:
            if index in self.specs:
                return self.specs[index]
            return self.segments[index % len(self.segments)]

    def start(self, first):
        """`first` is segments[0], already built inline by the caller."""
        with self.lock:
            self.ready[0] = first
            self.specs[0] = self.segments[0]
            self.want = 0
        self.thread = threading.Thread(target=self._run, name="megademo-build",
                                       daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            with self.lock:
                while not self.stop and self._next_missing() is None:
                    self.wake.wait()
                if self.stop:
                    return
                index = self._next_missing()
                seg = self.segments[index % len(self.segments)]
            try:
                render = build_segment(seg, self.args)
            except Exception as exc:                    # a broken effect
                warn("build of %s failed (%s); dropped from the show" % (seg.name, exc))
                with self.lock:
                    if len(self.segments) > 1 and seg in self.segments:
                        self.segments.remove(seg)
                    else:
                        self.stop = True                # nothing left to play
                        return
                continue                                # same index, next effect
            if not self.args.quiet:
                warn("built %s in %.1fs, %d ahead"
                     % (seg.name, seg.build_secs, index - self.want))
            with self.lock:
                self.ready[index] = render
                self.specs[index] = seg
                self.wake.notify_all()

    def _next_missing(self):
        """Caller holds the lock."""
        for i in range(self.want, self.want + self.lead + 1):
            if i not in self.ready:
                return i
        return None

    def get(self, index):
        """The render for `index`, or None if it is not built yet."""
        with self.lock:
            return self.ready.get(index)

    def retire(self, below):
        """Release everything before `below`; nothing looks back."""
        with self.lock:
            for i in [i for i in self.ready if i < below]:
                del self.ready[i]
                self.specs.pop(i, None)
            self.want = below
            self.wake.notify_all()

    def shutdown(self):
        with self.lock:
            self.stop = True
            self.wake.notify_all()


# --------------------------------------------------------------------------
# The banner: text scrolling across the bottom, over whatever is playing.
# --------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_CANDIDATES = [
    os.path.join(_HERE, "Impact.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
]


def bake_banner(text, width, font_px):
    """Bake the message into one wide RGB strip plus its alpha.

    Compositing text over the effect frame in numpy, rather than pushing it to
    a second FlaschenTaschen layer, keeps the whole show in one stream: no
    layer timeouts to keep alive and no ordering question between two senders.
    The cost is one strip lookup and one blend per frame over a dozen rows.
    """
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, font_px)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    draw0 = ImageDraw.Draw(Image.new("L", (8, 8)))
    left, top, right, bottom = draw0.textbbox((0, 0), text, font=font)
    tw, th = right - left, bottom - top
    band = th + 2
    # A blank run as wide as the panel so the message clears the screen before
    # it comes round again, and the wrap is not visible as a seam.
    strip_w = tw + width
    mask = Image.new("L", (strip_w, band), 0)
    ImageDraw.Draw(mask).text((-left, -top + 1), text, fill=255, font=font)
    alpha = (np.asarray(mask, f32) / 255.0)

    # Hue sweep along the strip: baked once, so it costs nothing per frame and
    # the colour travels with the letters rather than with the screen.
    hue = np.linspace(0.0, 3.0, strip_w, dtype=f32)[None, :] % 1.0
    rgb = ds.hsv_to_rgb(np.broadcast_to(hue, (band, strip_w)),
                        f32(0.55), f32(1.0))
    colour = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
    return colour, alpha


def banner_compositor(args):
    """-> overlay(out, t), drawing the banner into `out` in place, or None."""
    if not args.banner or not args.banner_text:
        return None
    font_px = args.banner_font or max(7, args.height // 5)
    try:
        colour, alpha = bake_banner(args.banner_text, args.width, font_px)
    except Exception as exc:                            # no Pillow, no font
        warn("banner disabled (%s)" % exc)
        return None

    band, strip_w = alpha.shape
    band = min(band, args.height)
    colour, alpha = colour[:band], alpha[:band]
    y0 = args.height - band
    cols = np.arange(args.width)
    # Darken under the text so it stays legible over a bright effect, but
    # taper the darkening at the top of the band so it is not a visible box.
    shadow = np.linspace(0.45, 0.75, band, dtype=f32)[:, None, None]

    def overlay(out, t):
        x = int(t * args.banner_speed) % strip_w
        idx = (x + cols) % strip_w
        a = alpha[:, idx][:, :, None]
        c = colour[:, idx].astype(f32)
        row = out[y0:, :, :].astype(f32) * (1.0 - shadow * a)
        out[y0:, :, :] = (row * (1.0 - a) + c * a).astype(np.uint8)

    return overlay


# --------------------------------------------------------------------------
# The sequencer itself.
# --------------------------------------------------------------------------

class Show(object):
    """Walks the playlist, overlapping neighbours for the transition.

    Timeline, with T the transition length: segment i occupies
    [S_i, S_i + dur_i), and its last T seconds are shared with segment i+1,
    which has already started rendering. So an effect's own clock starts T
    before its segment does, uniformly, and no effect is ever asked for its
    first frame in the same instant it becomes visible.
    """

    def __init__(self, builder, args, overlay=None):
        self.b = builder
        self.args = args
        self.overlay = overlay
        self.verbose = not args.quiet
        self.T = args.transition if len(builder.segments) > 1 else 0.0
        self.index = 0
        self.start = 0.0
        self.clock = {0: -self.T}            # index -> when its clock hit 0
        self.count = {0: 0}                  # index -> frames rendered
        self.out = np.empty((args.height, args.width, 3), np.uint8)
        # Rate accounting, split so the transition (two effects at once, the
        # worst case) can be reported on its own.
        self.busy = {False: 0.0, True: 0.0}
        self.frames = {False: 0, True: 0}
        self.worst = 0.0
        self.in_transition = False
        self.stalled = False

    def _dur(self, index):
        return self.b.spec(index).seconds

    def _one(self, index, t):
        """Render effect `index` at absolute show time t."""
        render = self.b.get(index)
        n = self.count.get(index, 0)
        self.count[index] = n + 1
        return render(t - self.clock[index], n)

    def frame(self, t, _i):
        t0 = time.monotonic()
        out = self._compose(t)
        if self.overlay is not None:
            if out is not self.out:
                np.copyto(self.out, out)     # never scribble on an effect's buffer
                out = self.out
            self.overlay(out, t)
        dt = time.monotonic() - t0
        self.busy[self.in_transition] += dt
        self.frames[self.in_transition] += 1
        if self.in_transition:
            self.worst = max(self.worst, dt)
        return out

    def _compose(self, t):
        dur = self._dur(self.index)
        # Advance the playhead. A segment can only end if the next effect is
        # actually built; otherwise hold, which stretches a segment rather
        # than showing a black gap.
        while t >= self.start + dur:
            if self.b.get(self.index + 1) is None:
                break
            self.index += 1
            self.b.retire(self.index)
            for old in [i for i in self.clock if i < self.index]:
                del self.clock[old]
                self.count.pop(old, None)
            self.start += dur
            dur = self._dur(self.index)
            if self.verbose:
                warn("t=%6.1f now playing %s" % (t, self.b.spec(self.index).name))
        cur = self.b.get(self.index)

        nxt_at = self.start + dur - self.T
        if self.T <= 0 or t < nxt_at:
            self.in_transition = False
            # Fade the very first effect up from black rather than snapping on.
            if self.index == 0 and self.T > 0 and t < self.T:
                return ds.fade_black(self._one(0, t), 1.0 - t / self.T)
            return self._one(self.index, t)

        nxt = self.index + 1
        render = self.b.get(nxt)
        if render is None:                   # not built yet: hold and warn once
            if not self.stalled:
                warn("waiting on %s to finish building" % self.b.spec(nxt).name)
                self.stalled = True
            self.in_transition = False
            return self._one(self.index, t)
        self.stalled = False
        if nxt not in self.clock:
            self.clock[nxt] = nxt_at
            self.count[nxt] = 0
        self.in_transition = True
        k = (t - nxt_at) / self.T
        kind = self.b.spec(nxt).transition
        return TRANSITIONS[kind](self._one(self.index, t), self._one(nxt, t),
                                 min(k, 1.0), self.args)

    def report(self):
        lines = []
        for is_trans, label in ((False, "steady"), (True, "transition")):
            n = self.frames[is_trans]
            if not n:
                continue
            per = self.busy[is_trans] / n
            lines.append("%-10s %5d frames, %6.2f ms/frame, headroom %5.1f fps"
                         % (label, n, per * 1e3, 1.0 / per if per else 0.0))
        if self.worst:
            lines.append("worst single transition frame: %.2f ms" % (self.worst * 1e3))
        return "\n".join(lines)


# --------------------------------------------------------------------------

def warn(message):
    sys.stderr.write("megademo: %s\n" % message)


def add_arguments(ap):
    ap.add_argument("--playlist", default=None,
                    help="name[:secs[:transition]][+opt=val...], comma separated"
                         " (default: the built-in running order)")
    ap.add_argument("--segment", type=float, default=16.0,
                    help="seconds per effect when the playlist does not say")
    ap.add_argument("--transition", type=float, default=2.0,
                    help="seconds of overlap between effects")
    ap.add_argument("--wipe-softness", type=int, default=28,
                    help="wipe edge width in px")
    ap.add_argument("--lead", type=int, default=2,
                    help="segments to build ahead of the playhead")
    ap.add_argument("--banner", dest="banner", action="store_true", default=True)
    ap.add_argument("--no-banner", dest="banner", action="store_false",
                    help="skip the scrolling text along the bottom")
    ap.add_argument("--banner-text", default=BANNER_TEXT)
    ap.add_argument("--banner-font", type=int, default=0,
                    help="banner glyph height px (0 = scale to the canvas)")
    ap.add_argument("--banner-speed", type=float, default=55.0,
                    help="banner scroll px/sec")
    ap.add_argument("--stats", action="store_true",
                    help="print per-phase frame timings on exit")


def main():
    # Effects import each other's neighbours by bare name, so make sure this
    # directory is importable however megademo was invoked.
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

    ap = ds.parser(__doc__.split("\n", 1)[0])
    add_arguments(ap)
    args = ap.parse_args()

    entries = (parse_playlist(args.playlist, args.segment) if args.playlist
               else [(n, s, dict(o), None) for n, s, o in PLAYLIST])
    segments = resolve(entries)
    if not args.quiet:
        print("megademo: %s" % " -> ".join(
            "%s(%gs,%s)" % (s.name, s.seconds, s.transition) for s in segments))

    # The opening effect is the one build nothing can hide, so it is done here
    # and paid for in startup time; everything after it is built ahead on the
    # worker while the show is already running.
    first, opener, dropped = None, None, []
    for seg in segments:
        t0 = time.monotonic()
        try:
            first = build_segment(seg, args)
        except Exception as exc:
            warn("build of %s failed (%s); dropped from the show" % (seg.name, exc))
            dropped.append(seg)
            continue
        opener = seg
        if not args.quiet:
            print("megademo: built %s in %.1fs; the rest build ahead in the "
                  "background" % (seg.name, time.monotonic() - t0))
        break
    if first is None:
        raise SystemExit("megademo: nothing in the playlist would build")
    # Keep the running order, starting from whichever effect actually built.
    live = [s for s in segments if s not in dropped]
    at = live.index(opener)
    live = live[at:] + live[:at]

    builder = Builder(live, args, args.lead)
    builder.start(first)
    show = Show(builder, args, banner_compositor(args))
    try:
        ds.run(show.frame, args)
    finally:
        builder.shutdown()
    if args.stats or not args.quiet:
        print(show.report())


if __name__ == "__main__":
    main()
