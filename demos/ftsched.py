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
ROTATION = [
    # (name, seconds, {options}, ms_p95, solo)
    ("starfield",  45, {}, 9.9, False),
    ("sunset",     70, {}, 30.0, False),      # 58 s is one full lap of the drive
    ("knit",       45, {}, 4.5, False),
    ("karl",       45, {"fps": 24}, 32.7, False),
    ("cycle",      45, {}, 2.2, False),
    # water is the most expensive effect that still fits a frame on its own,
    # so it is bracketed by the two cheapest things in the show. knit next to
    # it came to 50.3 ms, which is over the 50 ms frame by just enough to drop
    # a frame in the middle of every transition into it.
    ("water",      45, {"drops": 4}, 45.8, False),
    ("boing",      45, {}, 2.0, False),
    ("laser",      45, {}, 11.9, False),
    ("mario",      45, {}, 6.4, False),
    ("goldengate", 45, {}, 19.2, False),
    ("nyancat",    45, {}, 8.2, False),
    ("printer",    45, {}, 16.9, False),
    ("splitflap",  45, {}, 6.2, False),
    ("grove",      45, {}, 18.7, False),
    ("daliclock",  45, {}, 20.1, False),
    ("fire",       45, {}, 18.8, False),
    ("wheel",      45, {}, 13.1, False),
    ("tunnel",     45, {"palette": "ice"}, 13.5, False),
    ("fireworks",  45, {"rate": 3}, 33.7, False),
    ("floor",      45, {}, 10.3, False),
    ("metaballs",  45, {"palette": "magma"}, 29.0, False),
    ("rotozoom",   45, {"palette": "rainbow"}, 12.6, False),
    ("slime",      60, {"agents": 6000, "warmup": 60, "fps": 8}, 81.5, True),
    ("fireflies",  60, {"fps": 12}, 61.3, True),
    ("scroller",   45, {"plasma_frames": 60}, 12.0, False),
]


class Entry(object):
    """One slot in the rotation. Plain data, and the unit the UI operates on."""

    def __init__(self, kind, name, seconds, options=None, ms=0.0, solo=False,
                 argv=None, enabled=True):
        self.kind = kind                     # "py" or "exec"
        self.name = name
        self.seconds = float(seconds)
        self.options = options or {}
        self.ms = float(ms)                  # measured p95, 0 if unknown
        self.solo = bool(solo) or kind == "exec"
        self.argv = list(argv or [])
        self.enabled = bool(enabled)

    def to_json(self):
        d = {"kind": self.kind, "name": self.name, "seconds": self.seconds,
             "enabled": self.enabled, "ms": self.ms, "solo": self.solo}
        if self.options:
            d["options"] = self.options
        if self.argv:
            d["argv"] = self.argv
        return d

    @staticmethod
    def from_json(d):
        return Entry(d.get("kind", "py"), d["name"],
                     d.get("seconds", DEFAULT_SECONDS), d.get("options"),
                     d.get("ms", 0.0), d.get("solo", False), d.get("argv"),
                     d.get("enabled", True))


def default_rotation():
    return [Entry("py", name, secs, dict(opts), ms, solo)
            for name, secs, opts, ms, solo in ROTATION]


def load_rotation(path):
    with open(path) as fh:
        return [Entry.from_json(d) for d in json.load(fh)["rotation"]]


def pair_check(entries, fps, warn):
    """Warn where two neighbours cannot both render inside one frame.

    Advisory only -- a blown pair drops frames during the two second
    transition and nothing else -- but it is the easiest thing to get wrong
    when reordering, and it is invisible until you are in front of the wall.
    """
    live = [e for e in entries if e.enabled and not e.solo and e.ms]
    if len(live) < 2:
        return
    budget = 1000.0 / fps
    for a, b in zip(live, live[1:] + live[:1]):
        if a.ms + b.ms > budget:
            warn("%s (%.1f ms) into %s (%.1f ms) exceeds the %.1f ms frame at "
                 "%d fps; that transition will drop frames"
                 % (a.name, a.ms, b.name, b.ms, budget, fps))


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
            seg = mega.Segment(entry.name, __import__(entry.name), entry.seconds,
                               dict(entry.options), "cut")
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
        self.builder = None
        self.show = None
        # Running totals rather than a list, so uptime does not grow memory.
        self.frames = 0
        self.busy = 0.0
        self.late = 0
        self.worst = 0.0
        self.started = time.monotonic()

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

    def _supervise(self, entry, t):
        """Hand the wall to an external command until it exits or runs out."""
        if self.child_entry is not entry:
            self._reap()
            self.warn("exec %s: %s" % (entry.name, " ".join(entry.argv)))
            try:
                self.child = subprocess.Popen(entry.argv)
                self.child_entry = entry
            except OSError as exc:
                self.warn("exec %s failed: %s" % (entry.name, exc))
                self.show.skip(t)
                return
        if self.child.poll() is not None:    # finished early: end the slot
            self._reap()
            self.show.skip(t)

    def _reap(self):
        if self.child is None:
            return
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
        self.child = None
        self.child_entry = None

    # -- the frame loop ---------------------------------------------------

    def run(self):
        args = self.args
        try:
            import flaschen
        except ImportError:
            sys.path.insert(0, os.path.join(_HERE, "..", "api", "python"))
            import flaschen
        ft = flaschen.Flaschen(args.host, args.port, args.width, args.height,
                               transparent=True)
        offset = (0, 0, args.layer)

        self.builder = Builder(self.rot, args, args.lead, self.warn, self.black)
        self.builder.start(self._build_opening())
        self.show = Show(self.builder, args, self.blend, self.warn)

        # Everything above is startup: big, long-lived, acyclic. Move it out of
        # the generations the collector actually walks.
        gc.collect()
        gc.freeze()

        dt = 1.0 / args.fps
        t0 = time.monotonic()
        i = 0
        self.warn("up at %d fps, %d entries, %.0f s cycle"
                  % (args.fps, len(self.rot.all), self.rot.cycle_seconds()))
        while not self.stop.is_set():
            t = time.monotonic() - t0
            self._drain(t)
            if self.show.paused:
                self.show.hold(dt)           # the effect runs on; the slot never ends
            began = time.monotonic()
            try:
                frame = self._frame(t)
            except Exception as exc:
                self.warn("render failed (%s); skipping the segment" % exc)
                self.show.skip(t)
                frame = None
            if frame is not None:
                ft.send_array_banded(frame, offset, args.band)
            busy = time.monotonic() - began
            self.frames += 1
            self.busy += busy
            self.worst = max(self.worst, busy)
            i += 1
            self._publish(t, ft)
            slack = t0 + i * dt - time.monotonic()
            if slack > 0:
                time.sleep(slack)
            else:
                self.late += 1
                # Never try to catch up. A burst of back-to-back frames after
                # a slow one looks worse than the slow one did, so once we are
                # more than a frame behind, re-base the clock and carry on.
                if -slack > dt:
                    i += 1
                    t0 = time.monotonic() - t - dt
        self._shutdown(ft, offset)

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
        if self.frames > 1 and self.frames % max(1, self.args.fps):
            return
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
                "uptime": round(time.monotonic() - self.started),
                "target_fps": self.args.fps,
                "actual_fps": round(self.frames / max(t, 1e-6), 1),
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
