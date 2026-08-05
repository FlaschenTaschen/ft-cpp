#!/usr/bin/env python3
"""ftsched: a long-running scheduler and supervisor for the wall.

This replaces sf-demos.sh. That script ran the rotation by launching one
`python3 demo.py --duration 45` per segment in a shell loop, which works and
ran for months, but it pays for every segment three times over:

  * A fresh interpreter and a fresh `import numpy` per segment. On the Pi 3
    that is about 1.4 s of black wall, every 45 s, forever -- roughly 3% of
    the show is the rotation booting.
  * No overlap. Two effects can never be alive at once across a process
    boundary, so every segment change is a hard cut. megademo.py already
    solved this *inside* one process; the rotation around it could not.
  * No handle on it. Once bash is in the middle of a sleep there is nothing to
    ask what is playing, nothing to skip, and nothing to switch off short of
    editing the file and restarting the unit -- which drops the wall.

So: one process that stays up, holds the demo modules imported, builds the
next effect on a worker thread while the current one plays, blends between
them, and exposes the whole thing over HTTP so it can be steered from a phone
in the shop.

Structure
---------
Three threads and no more:

  render   the frame loop. Owns the wall. Never blocks on anything else: it
           drains a command queue under a short lock at the top of each frame
           and is otherwise alone.
  build    the Builder, constructing effects a couple of segments ahead of
           the playhead. Inherited from megademo, which already had to solve
           this -- scroller.build() bakes for seconds and cannot be done
           inline at the moment of a transition.
  http     the control server. Only ever reads a published snapshot and pushes
           commands onto the queue, so it cannot stall the wall no matter how
           slow a client is or how many of them there are.

Allocation
----------
The steady state allocates nothing per frame that this file controls: one
output buffer, one Blender's worth of transition scratch, one preformatted UDP
header in the client, and a state snapshot rebuilt once a second rather than
once a frame. What the effects allocate internally is theirs. gc.freeze()
after startup moves the demo tables -- large, long-lived, not cyclic -- into
the permanent generation so collections stop walking them.

Segment kinds
-------------
  py     a demoscene module, rendered in-process. The normal case.
  exec   an external command that draws on the wall itself, for the C++ tools
         in the rotation (send-image and friends). The scheduler stops sending
         for the duration and supervises the child: killed if it outruns its
         slot, and the slot ends early if it exits first. A transition needs
         both effects rendering at once, which cannot cross a process
         boundary, so these always cut.

Run:  python3 ftsched.py --host localhost --listen 0.0.0.0:8081
      python3 ftsched.py --rotation rotation.json --state-file /var/lib/ft.json
      python3 ftsched.py --dump-rotation > rotation.json
"""

import gc
import json
import os
import signal
import subprocess
import sys
import threading
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import demoscene as ds
import megademo as mega
import ftsched_web

f32 = ds.f32

DEFAULT_SECONDS = 45.0

# Fraction of the frame a transition is allowed to spend rendering its two
# effects. The rest is the send, the blend and the scheduler's own overhead;
# at 0.8 a pair measured at 40 ms is paced to 20 fps rather than 25.
TRANSITION_HEADROOM = 0.8


# --------------------------------------------------------------------------
# The rotation, as data.
# --------------------------------------------------------------------------

# The betelgeuse running order, carried over from sf-demos.sh along with its
# measurements. `ms` is p95 render time on the Pi 3 at 320x64 -- measured, not
# estimated -- and is here so the UI can show it and pair_check() can complain
# before the wall does.
#
# A transition renders BOTH neighbours in the same frame, so what has to fit
# the frame budget is the sum of a pair, not any single effect. That is also
# why slime (81.5 ms) and fireflies (61.3 ms) are marked solo: each exceeds a
# whole frame alone, so they get a cut on either side rather than a blend, and
# their own lower --fps keeps them inside their slot.
#
# The order alternates expensive against cheap for the same reason.
# `fps` is per entry, not global. The shell rotation this replaces ran each
# demo as its own process at its own frame rate, so the cheap ones ran at 60
# and only the expensive ones crawled; a single flat rate for the whole show
# would make boing and cycle visibly choppier than they are today for no gain.
# Each is set to leave roughly 40% headroom over the measured cost, which is
# what absorbs a transition without the pacing slipping.
ROTATION = [
    # (name, seconds, fps, {options}, ms_p95, solo)
    ("starfield",  45, 50, {}, 9.9, False),
    ("sunset",     70, 24, {}, 30.0, False),   # 58 s is one full lap of the drive
    ("knit",       45, 60, {}, 4.5, False),
    ("karl",       45, 24, {}, 32.7, False),
    ("cycle",      45, 60, {}, 2.2, False),
    # water is the most expensive effect that still fits a frame on its own,
    # so it is bracketed by the two cheapest things in the show. knit next to
    # it summed to 50.3 ms, over the 50 ms frame by just enough to drop a frame
    # in the middle of every transition into it.
    ("water",      45, 20, {"drops": 4}, 45.8, False),
    ("boing",      45, 60, {}, 2.0, False),
    ("laser",      45, 40, {}, 11.9, False),
    ("mario",      45, 60, {}, 6.4, False),
    ("goldengate", 45, 30, {}, 19.2, False),
    ("nyancat",    45, 60, {}, 8.2, False),
    ("printer",    45, 30, {}, 16.9, False),
    ("splitflap",  45, 30, {}, 6.2, False),    # a flap board has nothing to gain
    ("grove",      45, 30, {}, 18.7, False),
    ("daliclock",  45, 30, {}, 20.1, False),
    ("fire",       45, 30, {}, 18.8, False),
    ("wheel",      45, 40, {}, 13.1, False),
    ("tunnel",     45, 40, {"palette": "ice"}, 13.5, False),
    ("fireworks",  45, 24, {"rate": 3}, 33.7, False),
    ("floor",      45, 50, {}, 10.3, False),
    ("metaballs",  45, 24, {"palette": "magma"}, 29.0, False),
    ("rotozoom",   45, 40, {"palette": "rainbow"}, 12.6, False),
    ("slime",      60,  8, {"agents": 6000, "warmup": 60}, 81.5, True),
    ("fireflies",  60, 12, {}, 61.3, True),
    ("scroller",   45, 40, {"plasma_frames": 60}, 12.0, False),
]


class Entry(object):
    """One slot in the rotation. Plain data, and the unit the UI operates on."""

    def __init__(self, kind, name, seconds, fps=0, options=None, ms=0.0,
                 solo=False, argv=None, enabled=True, clears=None, wait=True):
        self.kind = kind                     # "py" or "exec"
        self.name = name
        self.seconds = float(seconds)
        self.fps = int(fps or 0)             # 0 = take the daemon's --fps
        self.options = options or {}
        # exec only. `clears` are the layers the child draws on, blanked when
        # its slot ends -- the C++ tools set a layer with a timeout of their
        # own and it would otherwise sit on top of the next effect for the
        # remainder of that timeout. `wait` is whether the child exiting ends
        # the slot: true for the ones that run for their whole -t duration,
        # false for send-text, which sets a layer and returns immediately.
        self.clears = list(clears or [])
        self.wait = bool(wait)
        self.ms = float(ms)                  # measured p95, 0 if unknown
        self.solo = bool(solo) or kind == "exec"
        self.argv = list(argv or [])
        self.enabled = bool(enabled)

    def to_json(self):
        d = {"kind": self.kind, "name": self.name, "seconds": self.seconds,
             "fps": self.fps, "enabled": self.enabled, "ms": self.ms,
             "solo": self.solo}
        if self.options:
            d["options"] = self.options
        if self.argv:
            d["argv"] = self.argv
        if self.clears:
            d["clears"] = self.clears
        if not self.wait:
            d["wait"] = False
        return d

    @staticmethod
    def from_json(d):
        return Entry(d.get("kind", "py"), d["name"],
                     d.get("seconds", DEFAULT_SECONDS), d.get("fps", 0),
                     d.get("options"), d.get("ms", 0.0), d.get("solo", False),
                     d.get("argv"), d.get("enabled", True), d.get("clears"),
                     d.get("wait", True))


def default_rotation():
    return [Entry("py", name, secs, fps, dict(opts), ms, solo)
            for name, secs, fps, opts, ms, solo in ROTATION]


def load_rotation(path):
    with open(path) as fh:
        return [Entry.from_json(d) for d in json.load(fh)["rotation"]]


def pair_check(entries, fps, warn):
    """Report neighbours whose transition has to be paced down to fit.

    Not a problem -- _rate() handles it, and two seconds at a lower rate does
    not read -- but a pair that drags the transition a long way below both of
    its neighbours usually means the running order could be better, and that
    is invisible until you are standing in front of the wall.
    """
    live = [e for e in entries if e.enabled and not e.solo and e.ms]
    if len(live) < 2:
        return
    for a, b in zip(live, live[1:] + live[:1]):
        own = min(a.fps or fps, b.fps or fps)
        paced = min(own, int(TRANSITION_HEADROOM * 1000.0 / (a.ms + b.ms)))
        # Only worth mentioning if it is a real drop, not a rounding step.
        if paced < own * 0.75:
            warn("%s (%.1f ms) into %s (%.1f ms): transition paced at %d fps, "
                 "down from %d" % (a.name, a.ms, b.name, b.ms, paced, own))


# --------------------------------------------------------------------------
# The steerable running order.
# --------------------------------------------------------------------------

class Rotation(object):
    """The live running order and the index mapping the Builder walks.

    The Builder maps a global, forever-increasing index onto the order by
    `entries[i % n]`. Everything the UI wants -- switch an effect off, jump
    straight to one -- is a change to that mapping for indices not yet built,
    while indices already built stay pinned to what they were (the Builder
    remembers those in `specs`, which it already had to do for the case of an
    effect dropping out of the show mid-run).

    So this adds exactly one number: `offset`, where index i plays entry
    (i + offset) % n. Jumping to position p at index i is offset = p - i.
    """

    def __init__(self, entries):
        self.lock = threading.RLock()
        self.all = list(entries)             # every entry, enabled or not
        self.offset = 0

    def _live(self):
        """Caller holds the lock."""
        return [e for e in self.all if e.enabled]

    def at(self, index):
        with self.lock:
            live = self._live()
            return live[(index + self.offset) % len(live)] if live else None

    def set_enabled(self, name, on):
        with self.lock:
            hit = [e for e in self.all if e.name == name]
            if not hit:
                return False
            if not on and len(self._live()) <= len([e for e in hit if e.enabled]):
                return False                 # never empty the rotation
            for e in hit:
                e.enabled = on
            return True

    def jump(self, position, at_index):
        """Make global index `at_index` play live position `position`."""
        with self.lock:
            live = self._live()
            if not live or not 0 <= position < len(live):
                return False
            self.offset = (position - at_index) % len(live)
            return True

    def position_of(self, entry):
        with self.lock:
            live = self._live()
            return live.index(entry) if entry in live else -1

    def cycle_seconds(self):
        with self.lock:
            return sum(e.seconds for e in self._live())

    def snapshot(self):
        with self.lock:
            live = self._live()
            return [dict(e.to_json(),
                         position=live.index(e) if e in live else -1)
                    for e in self.all]


class Builder(mega.Builder):
    """megademo's builder, asking a Rotation what each index is.

    The base class owns the worker thread, the lookahead window, retiring
    finished builds, and dropping an effect whose build() raises. What changes
    is where the mapping comes from and that it can move underneath us.
    """

    def __init__(self, rotation, args, lead, warn, black):
        self.rot = rotation
        self.warn_fn = warn
        self.black = black
        self._segcache = {}                  # entry -> Segment
        # Bumped every time the index->entry mapping moves. A build takes
        # seconds (scroller bakes for fourteen), so a jump or a toggle can
        # easily land while one is in flight; without this the finished render
        # would be filed against an index that now means something else, and
        # the wall would play the effect you just jumped away from.
        self.gen = 0
        # The base class keeps a fixed list; ours is in the Rotation. It is
        # still read for len() in a couple of places, so keep it plausible.
        mega.Builder.__init__(self, rotation.all, args, lead)

    def entry_at(self, index):
        return self.rot.at(index)

    def spec(self, index):
        with self.lock:
            if index in self.specs:
                return self.specs[index]
        return self.entry_at(index)

    def segment_for(self, entry):
        seg = self._segcache.get(entry)
        if seg is None:
            overrides = dict(entry.options)
            # The demo has to be built for the rate it will actually be driven
            # at: several of them scale motion per frame off args.fps, so
            # building at 20 and running at 60 plays them three times too fast.
            if entry.fps:
                overrides["fps"] = entry.fps
            seg = mega.Segment(entry.name, __import__(entry.name), entry.seconds,
                               overrides, "cut")
            self._segcache[entry] = seg
        seg.seconds = entry.seconds
        return seg

    def render_for(self, entry):
        """An exec entry has nothing to build, but it still occupies an index
        and the playhead still has to walk through it, so it gets a render
        that draws nothing. The frame loop recognises it and stops sending."""
        if entry.kind == "exec":
            black = self.black
            return lambda t, n: black
        return mega.build_segment(self.segment_for(entry), self.args)

    def invalidate_from(self, index):
        """Drop builds for `index` and later: the mapping moved under them."""
        with self.lock:
            for i in [i for i in self.ready if i >= index]:
                del self.ready[i]
                self.specs.pop(i, None)
            self.gen += 1
            self.wake.notify_all()

    def _run(self):
        while True:
            with self.lock:
                while not self.stop and self._next_missing() is None:
                    self.wake.wait()
                if self.stop:
                    return
                index = self._next_missing()
                gen = self.gen
            entry = self.entry_at(index)
            if entry is None:                # rotation emptied under us
                time.sleep(0.2)
                continue
            t0 = time.monotonic()
            try:
                render = self.render_for(entry)
            except Exception as exc:
                self.warn_fn("build of %s failed (%s); switching it off"
                             % (entry.name, exc))
                entry.enabled = False        # this moves the mapping too
                with self.lock:
                    self.gen += 1
                continue
            with self.lock:
                if self.gen != gen:
                    # Somebody jumped or toggled while this was building. Throw
                    # it away; the loop picks up whatever the index means now.
                    continue
                self.ready[index] = render
                self.specs[index] = entry
                self.wake.notify_all()
            if not self.args.quiet and entry.kind == "py":
                self.warn_fn("built %s in %.1fs" % (entry.name,
                                                    time.monotonic() - t0))


class Show(mega.Show):
    """The sequencer, with a playhead that can be steered.

    Two changes from megademo. Durations and transitions come from the
    Rotation per index rather than from a fixed list, and skip() rewrites the
    current segment's end time so a jump rides the ordinary transition
    machinery -- a jump is a segment ending early, not a special case. If the
    target is not built yet the inherited logic already holds the outgoing
    effect rather than showing black, so jumping to a slow builder stretches.
    """

    def __init__(self, builder, args, blender, warn):
        mega.Show.__init__(self, builder, args, None)
        self.blend = blender
        self.warn_fn = warn
        self.rot = builder.rot
        self.T = args.transition
        self.paused = False

    def _dur(self, index):
        entry = self.b.entry_at(index)
        return entry.seconds if entry else DEFAULT_SECONDS

    def transition_for(self, index):
        """Cut into or out of anything solo; otherwise megademo's picker."""
        cur, nxt = self.b.entry_at(index - 1), self.b.entry_at(index)
        if cur is None or nxt is None or cur.solo or nxt.solo:
            return "cut"
        return mega.pick_transition(cur.name, nxt.name, index)

    def blended(self, kind, a, b, k):
        """Dispatch onto the preallocated Blender."""
        if kind == "cut":
            return b if k >= 1.0 else a
        if kind == "wipe":
            return self.blend.wipe(a, b, k, self.args.wipe_softness)
        if kind == "black":
            return self.blend.through_black(a, b, k)
        if kind == "flash":
            return self.blend.flash(a, b, k)
        return self.blend.crossfade(a, b, k)

    def skip(self, t, gap=0.0):
        """End the current segment `gap` seconds from now."""
        self.start = min(self.start, t + gap - self._dur(self.index))

    def hold(self, seconds):
        """Push the segment's end back; used to implement pause."""
        self.start += seconds

    def _compose(self, t):
        dur = self._dur(self.index)
        # Advance the playhead. A segment can only end once the next effect is
        # actually built; otherwise hold, which stretches a segment rather
        # than punching a black hole in the show.
        while t >= self.start + dur:
            if self.index + 1 not in self.b.ready:
                break
            self.index += 1
            self.b.retire(self.index)
            for old in [i for i in self.clock if i < self.index]:
                del self.clock[old]
                self.count.pop(old, None)
            self.start += dur
            dur = self._dur(self.index)
            # An effect's clock is normally started by the transition that
            # brings it in, a couple of seconds before its segment does. A
            # segment entered by a *cut* never had that transition -- which is
            # every solo effect, and every jump into one -- so its clock has to
            # be started here or the first frame asks self.clock for a key that
            # was never set and the segment is skipped.
            if self.index not in self.clock:
                self.clock[self.index] = self.start
                self.count[self.index] = 0
            entry = self.b.entry_at(self.index)
            self.warn_fn("now playing %s" % (entry.name if entry else "?"))

        kind = self.transition_for(self.index + 1)
        T = 0.0 if kind == "cut" else self.T
        nxt_at = self.start + dur - T

        if T <= 0 or t < nxt_at:
            self.in_transition = False
            # Fade the very first effect up from black rather than snapping on.
            if self.index == 0 and self.T > 0 and t < self.T:
                return self.blend.fade_black(self._one(0, t), 1.0 - t / self.T)
            return self._one(self.index, t)

        nxt = self.index + 1
        if nxt not in self.b.ready:
            if not self.stalled:
                self.warn_fn("waiting on the next effect to build")
                self.stalled = True
            self.in_transition = False
            return self._one(self.index, t)
        self.stalled = False
        if nxt not in self.clock:
            self.clock[nxt] = nxt_at
            self.count[nxt] = 0
        self.in_transition = True
        k = min((t - nxt_at) / T, 1.0)
        return self.blended(kind, self._one(self.index, t), self._one(nxt, t), k)


# --------------------------------------------------------------------------
# The daemon.
# --------------------------------------------------------------------------

class Scheduler(object):

    def __init__(self, args, entries):
        self.args = args
        self.rot = Rotation(entries)
        self.commands = []                   # (op, payload), drained per frame
        self.cmd_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.state = {}                      # what the HTTP thread serves
        self.stop = threading.Event()
        self.dirty = threading.Event()       # enable/disable needs persisting
        self.blend = ds.Blender(args.height, args.width)
        self.out = np.empty((args.height, args.width, 3), np.uint8)
        self.black = np.zeros((args.height, args.width, 3), np.uint8)
        self.child = None                    # a running exec segment
        self.child_entry = None
        self.ft = None                       # the client, once run() has it
        self.builder = None
        self.show = None
        # Running totals rather than a list, so uptime does not grow memory.
        self.frames = 0
        self.busy = 0.0
        self.late = 0
        self.worst = 0.0
        self.started = time.monotonic()
        self._pub_at = self.started          # last snapshot, for the rate window
        self._pub_frames = 0

    # -- commands, from the HTTP thread -----------------------------------

    def submit(self, op, payload=None):
        with self.cmd_lock:
            if len(self.commands) > 64:      # a wedged render loop, not a user
                return False
            self.commands.append((op, payload or {}))
        return True

    def _drain(self, t):
        with self.cmd_lock:
            if not self.commands:
                return
            pending, self.commands = self.commands, []
        for op, payload in pending:
            try:
                self._apply(op, payload, t)
            except Exception as exc:
                self.warn("command %s failed: %s" % (op, exc))

    def _apply(self, op, payload, t):
        if op == "pause":
            self.show.paused = True
        elif op == "resume":
            self.show.paused = False
        elif op == "toggle":
            if self.rot.set_enabled(payload["name"], bool(payload["on"])):
                # Only indices past the playhead move. The current effect keeps
                # playing even if it is the one just switched off, which is
                # what you want when you switch it off while looking at it.
                self.builder.invalidate_from(self.show.index + 1)
                self.dirty.set()
        elif op == "jump":
            if self.rot.jump(int(payload["index"]), self.show.index + 1):
                self.builder.invalidate_from(self.show.index + 1)
                self.show.skip(t, self.args.transition)
        elif op == "next":
            self.show.skip(t, self.args.transition)
        elif op == "restart":
            self.builder.invalidate_from(self.show.index + 1)
            self.show.skip(t, self.args.transition)
        else:
            raise ValueError("unknown op %r" % op)

    # -- exec segments ----------------------------------------------------

    def _blank(self, layer):
        """Push black onto a layer, repeated so a lost datagram cannot leave a
        stale frame sitting there for the rest of its timeout."""
        for _ in range(3):
            self.ft.send_array_banded(self.black, (0, 0, layer), self.args.band)
            time.sleep(0.01)

    def _supervise(self, entry, t):
        """Hand the wall to an external command until it exits or runs out."""
        if self.child_entry is not entry:
            self._reap()
            # Our own layer has to go black before the child draws, or the
            # last frame of the outgoing effect shows through wherever the
            # child's layer is black -- which for pixel art is most of it.
            self._blank(self.args.layer)
            self.warn("exec %s: %s" % (entry.name, " ".join(entry.argv)))
            self.child_entry = entry
            try:
                self.child = subprocess.Popen(entry.argv)
            except OSError as exc:
                self.warn("exec %s failed: %s" % (entry.name, exc))
                self.show.skip(t)
            return
        if self.child is not None and self.child.poll() is not None:
            self.child = None
            # send-text and friends set a layer and return at once; their slot
            # is meant to hold that image for its full length, so only the
            # long-running children end their own slot by exiting.
            if entry.wait:
                self.show.skip(t)

    def _reap(self):
        """End the current exec slot: stop the child, clear what it drew."""
        entry, self.child_entry = self.child_entry, None
        if self.child is not None:
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait()
            self.child = None
        for layer in (entry.clears if entry else ()):
            self._blank(layer)

    # -- the frame loop ---------------------------------------------------

    def run(self):
        args = self.args
        try:
            import flaschen
        except ImportError:
            sys.path.insert(0, os.path.join(_HERE, "..", "api", "python"))
            import flaschen
        ft = self.ft = flaschen.Flaschen(args.host, args.port, args.width,
                                         args.height, transparent=True)
        offset = (0, 0, args.layer)

        self.builder = Builder(self.rot, args, args.lead, self.warn, self.black)
        self.builder.start(self._build_opening())
        self.show = Show(self.builder, args, self.blend, self.warn)

        # Everything above is startup: big, long-lived, acyclic. Move it out of
        # the generations the collector actually walks.
        gc.collect()
        gc.freeze()

        t0 = time.monotonic()
        due = t0
        self.warn("up, %d entries, %.0f s cycle"
                  % (len(self.rot.all), self.rot.cycle_seconds()))
        while not self.stop.is_set():
            t = time.monotonic() - t0
            self._drain(t)
            dt = 1.0 / self._rate()
            if self.show.paused:
                self.show.hold(dt)           # the effect runs on; the slot never ends
            began = time.monotonic()
            try:
                frame = self._frame(t)
            except Exception as exc:
                self.warn("render failed (%s: %s); skipping the segment"
                          % (type(exc).__name__, exc))
                self.show.skip(t)
                frame = None
            if frame is not None:
                ft.send_array_banded(frame, offset, args.band)
            busy = time.monotonic() - began
            self.frames += 1
            self.busy += busy
            self.worst = max(self.worst, busy)
            self._publish(t, ft)
            # A deadline that accumulates, rather than t0 + i*dt: the interval
            # changes from segment to segment, so there is no single i*dt to
            # count in.
            due += dt
            slack = due - time.monotonic()
            if slack > 0:
                time.sleep(slack)
            else:
                self.late += 1
                # Never try to catch up. A burst of back-to-back frames after a
                # slow one looks worse than the slow one did, so once we are
                # more than a frame behind, give up the lost time and re-base.
                if -slack > dt:
                    due = time.monotonic()
        self._shutdown(ft, offset)

    def _rate(self):
        """Frames per second for the frame about to be rendered.

        Per segment, because the effects differ by an order of magnitude in
        cost and the cheap ones should not be held down to the pace of water.
        Through a transition both neighbours render into the same frame, so it
        runs at the slower of the two and steps up (or down) once the outgoing
        effect is gone.
        """
        show, args = self.show, self.args
        cur = self.builder.entry_at(show.index)
        rate = (cur.fps or args.fps) if cur else args.fps
        if show.in_transition:
            nxt = self.builder.entry_at(show.index + 1)
            if nxt is not None:
                rate = min(rate, nxt.fps or args.fps)
                # Both effects render into the same frame here, so the pair can
                # cost more than either one's own rate allows -- grove into
                # daliclock is 38.8 ms against a 33.3 ms frame at 30 fps. Pace
                # the transition to what the pair actually costs rather than
                # letting it run late for two seconds. It is two seconds, both
                # effects are moving, and a dip during a crossfade does not
                # read; frames arriving late do.
                if cur is not None and cur.ms and nxt.ms:
                    rate = min(rate, int(TRANSITION_HEADROOM * 1000.0
                                         / (cur.ms + nxt.ms)))
        return max(1, rate)

    def _build_opening(self):
        """Build the first effect inline. This is the one build nothing can
        hide, so it is paid for in startup time; the rest build ahead."""
        for _ in range(len(self.rot.all)):
            entry = self.rot.at(0)
            if entry is None:
                raise SystemExit("ftsched: rotation is empty")
            try:
                t0 = time.monotonic()
                render = self.builder.render_for(entry)
                self.warn("built %s in %.1fs; the rest build in the background"
                          % (entry.name, time.monotonic() - t0))
                return render
            except Exception as exc:
                self.warn("build of %s failed (%s); switching it off"
                          % (entry.name, exc))
                entry.enabled = False
        raise SystemExit("ftsched: nothing in the rotation would build")

    def _frame(self, t):
        frame = self.show.frame(t, self.frames)
        entry = self.builder.entry_at(self.show.index)
        if entry is not None and entry.kind == "exec":
            self._supervise(entry, t)
            return None                      # the child owns the wall
        if self.child is not None:           # walked off the end of an exec slot
            self._reap()
        if frame is not self.out:
            np.copyto(self.out, frame)
        return self.out

    # -- what the web UI reads --------------------------------------------

    def _publish(self, t, ft):
        """Rebuild the served snapshot once a second, not once a frame."""
        now = time.monotonic()
        if self.frames > 1 and now - self._pub_at < 1.0:
            return
        # Frames per second over the last interval, not over all of uptime:
        # the target rate changes per segment, so a lifetime average would
        # only ever report the mixture and never whether we are keeping up.
        window = now - self._pub_at
        recent = (self.frames - self._pub_frames) / window if window > 0 else 0.0
        self._pub_at, self._pub_frames = now, self.frames
        show = self.show
        entry = self.builder.entry_at(show.index)
        nxt = self.builder.entry_at(show.index + 1)
        dur = show._dur(show.index)
        per = self.busy / self.frames if self.frames else 0.0
        state = {
            "now": {
                "name": entry.name if entry else None,
                "kind": entry.kind if entry else None,
                "position": self.rot.position_of(entry) if entry else -1,
                "elapsed": round(max(0.0, t - show.start), 1),
                "duration": round(dur, 1),
                "in_transition": show.in_transition,
            },
            "next": {
                "name": nxt.name if nxt else None,
                "position": self.rot.position_of(nxt) if nxt else -1,
                "transition": show.transition_for(show.index + 1),
                "ready": (show.index + 1) in self.builder.ready,
            },
            "paused": show.paused,
            "cycle_seconds": round(self.rot.cycle_seconds()),
            "rotation": self.rot.snapshot(),
            "health": {
                "uptime": round(now - self.started),
                "target_fps": self._rate(),
                "actual_fps": round(recent, 1),
                "ms_per_frame": round(per * 1e3, 2),
                "worst_ms": round(self.worst * 1e3, 2),
                "late_frames": self.late,
                "dropped_packets": getattr(ft, "dropped", 0),
            },
        }
        with self.state_lock:
            self.state = state

    def snapshot(self):
        with self.state_lock:
            return self.state

    def _shutdown(self, ft, offset):
        self._reap()
        self.builder.shutdown()
        for _ in range(4):                   # a lost packet must not freeze a frame
            ft.send_array_banded(self.black, offset, self.args.band)
            time.sleep(0.02)
        self.warn("down; %d frames, %.2f ms/frame, %d late"
                  % (self.frames, 1e3 * self.busy / max(self.frames, 1), self.late))

    def warn(self, message):
        sys.stderr.write("ftsched: %s\n" % message)
        sys.stderr.flush()


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--listen", default="0.0.0.0:8081",
                    help="control server address (empty to disable)")
    ap.add_argument("--rotation", default=None,
                    help="JSON rotation file (default: the built-in order)")
    ap.add_argument("--dump-rotation", action="store_true",
                    help="print the built-in rotation as JSON and exit")
    ap.add_argument("--state-file", default=None,
                    help="persist which effects are switched off, across restarts")
    ap.add_argument("--transition", type=float, default=2.0,
                    help="seconds of overlap between effects")
    ap.add_argument("--wipe-softness", type=int, default=28)
    ap.add_argument("--lead", type=int, default=2,
                    help="segments to build ahead of the playhead")
    ap.add_argument("--previews", default=os.path.join(_HERE, "previews"),
                    help="directory of <name>.gif previews for the web UI")


def load_state(path, entries, warn):
    """Which effects are switched off survives a restart; nothing else does.

    The running order lives in the rotation file, which goes through review.
    What somebody switched off from their phone in the shop does not, so it is
    kept apart and can only ever toggle entries the rotation already lists.
    """
    if not path or not os.path.exists(path):
        return
    try:
        with open(path) as fh:
            off = set(json.load(fh).get("disabled", []))
    except Exception as exc:
        warn("ignoring unreadable state file %s (%s)" % (path, exc))
        return
    for e in entries:
        if e.name in off:
            e.enabled = False
    if off:
        warn("restored %d switched-off entries from %s" % (len(off), path))


def save_state(path, rot):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"disabled": [e.name for e in rot.all if not e.enabled]}, fh)
    os.replace(tmp, path)                    # never a half-written state file


def main():
    ap = ds.parser(__doc__.split("\n", 1)[0], fps=20)
    add_arguments(ap)
    args = ap.parse_args()

    if args.dump_rotation:
        print(json.dumps({"rotation": [e.to_json() for e in default_rotation()]},
                         indent=2))
        return

    def warn(m):
        sys.stderr.write("ftsched: %s\n" % m)
        sys.stderr.flush()

    entries = load_rotation(args.rotation) if args.rotation else default_rotation()
    load_state(args.state_file, entries, warn)
    pair_check(entries, args.fps, warn)

    sched = Scheduler(args, entries)
    server = ftsched_web.serve(args.listen, sched, args.previews, warn) \
        if args.listen else None

    def bye(signum, _frame):
        warn("signal %d" % signum)
        sched.stop.set()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    try:
        sched.run()
    finally:
        save_state(args.state_file, sched.rot)
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()
