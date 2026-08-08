#!/usr/bin/env python3
"""Fingerprint every demo under one interpreter+numpy, so upgrades can be diffed.

The wall's Pi runs whatever numpy Debian bullseye shipped, and moving off it is
only safe if we know what changes. This walks every demoscene module, builds it
with its own defaults, renders a fixed strip of frames, and records a hash of
each frame plus how long a frame took. Run it under two different numpys and
`--compare` the two result files: anything that stops importing, stops building,
stops rendering, or renders *different pixels* falls out of the diff.

The hard part is not the hashing, it is knowing which differences mean anything.
Several demos are legitimately nondeterministic -- unseeded RNG, a clock read --
and those will differ from themselves, never mind from another numpy. So every
demo is run twice under the *same* interpreter first, in two separate processes,
and marked self-stable or not. A demo that cannot reproduce itself can only be
checked for crash-freedom; reporting it as "changed by numpy" would be a lie.
Demos that take --seed get one, which pulls most of them into the stable set.

Each demo runs in its own subprocess. A demo that segfaults an old numpy, or
wedges, then costs us that demo rather than the whole run.

  scripts/numpy-compat.py --out RESULTS.json                 # this interpreter
  scripts/numpy-compat.py --only fire slime --out one.json
  scripts/numpy-compat.py --compare base.json new.json       # the matrix
  scripts/numpy-compat.py --dump-frames /tmp/png --only fire # look at pixels

No server, no display, no network: build() and render() are called directly.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMOS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_DEMOS)

# Frames are stepped at a fixed rate rather than jumped to, because a good half
# of these demos carry state between calls -- a physics step, a scroll position,
# a print head. Teleporting t forward would render a demo that never existed.
STEP_FPS = 20
HASH_EVERY = 20          # hash one frame a second
FRAMES = 10              # ... for ten seconds, which covers most demos' cycle
SEED = 12345

# Not demos: the shared scaffolding, the sequencer, the client libraries, and
# the four upstream scripts that predate demoscene.py and drive their own loop.
NOT_DEMOS = {"demoscene", "megademo", "flaschen_np", "fsa",
             "grid", "ripple", "sierpinski_rain"}


def discover(demos_dir=_DEMOS):
    """Every demoscene module: a .py with a build() that takes an options object."""
    names = []
    for fn in sorted(os.listdir(demos_dir)):
        if not fn.endswith(".py"):
            continue
        name = fn[:-3]
        if name in NOT_DEMOS:
            continue
        with open(os.path.join(demos_dir, fn)) as fh:
            src = fh.read()
        if "def build(" in src and "demoscene" in src:
            names.append(name)
    return names


# --------------------------------------------------------------------------
# The worker: one demo, in its own process.
# --------------------------------------------------------------------------

FROZEN_EPOCH = 1700000000.0      # 2023-11-14 22:13:20 UTC, an arbitrary instant


def freeze_clock(epoch=FROZEN_EPOCH):
    """Pin wall-clock reads, so clock-driven demos become comparable.

    daliclock and splitflap put the time of day on the wall. Left alone they
    differ between any two runs that straddle a second boundary, which would
    make them permanently "changed by numpy" -- the exact false positive this
    harness exists to avoid. Patching before the demo is imported catches
    `from time import time` as well as `time.time()`.

    The clock is pinned but not stopped: it runs from a fixed epoch at exactly
    the rate the harness advances t. Stopping it would be simpler and would be
    useless, because a clock demo driven by a stopped clock renders the same
    image sixty times and the comparison would cover one frame. Monotonic is
    left alone -- it drives pacing, not pixels.

    Returns the list whose [0] the caller advances to the current t.
    """
    import datetime
    import time as _t
    offset = [0.0]
    _t.time = lambda: epoch + offset[0]
    _t.localtime = lambda s=None: _t.gmtime(epoch + offset[0] if s is None else s)

    class _Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(epoch + offset[0], tz)

        @classmethod
        def today(cls):
            return cls.fromtimestamp(epoch + offset[0])
    datetime.datetime = _Frozen
    return offset


def fingerprint(name, frames=FRAMES, dump_dir=None, frozen=True):
    """Build one demo and hash a strip of its frames. Returns a result dict."""
    clock = freeze_clock() if frozen else [0.0]
    sys.path.insert(0, _DEMOS)
    sys.path.insert(0, _HERE)
    sys.path.insert(0, os.path.join(_ROOT, "api", "python"))
    os.chdir(_DEMOS)                      # demos load assets by relative path

    import numpy as np

    out = {"demo": name, "status": None, "error": None,
           "hashes": [], "build_s": None, "render_ms": None, "frames": frames}

    t0 = time.perf_counter()
    try:
        module = __import__(name)
    except BaseException:
        out["status"] = "IMPORT_FAIL"
        out["error"] = _last_line()
        return out, None

    import demoscene as ds
    try:
        opts = ds.options(module, fps=STEP_FPS)
        # A fixed seed where the demo offers one is the cheapest way to turn a
        # "nondeterministic, cannot compare" into a real comparison.
        if hasattr(opts, "seed"):
            setattr(opts, "seed", SEED)
        render = module.build(opts)
    except BaseException:
        out["status"] = "BUILD_FAIL"
        out["error"] = _last_line()
        return out, None
    out["build_s"] = round(time.perf_counter() - t0, 3)

    dt = 1.0 / STEP_FPS
    steps = frames * HASH_EVERY
    kept = []
    spent = 0.0
    try:
        for i in range(steps):
            t = i * dt
            clock[0] = t
            c0 = time.perf_counter()
            frame = render(t, i)
            spent += time.perf_counter() - c0
            if i % HASH_EVERY == 0:
                # render() may hand back a buffer it reuses; copy before the
                # next call, not after the loop.
                kept.append(np.asarray(frame, np.uint8).copy())
    except BaseException:
        out["status"] = "RENDER_FAIL"
        out["error"] = _last_line()
        return out, None

    out["render_ms"] = round(1000.0 * spent / steps, 3)
    out["status"] = "OK"
    out["hashes"] = [hashlib.sha256(f.tobytes()).hexdigest()[:16] for f in kept]
    stack = np.stack(kept) if kept else np.zeros((0, 1, 1, 3), np.uint8)

    # For the demos that cannot be compared frame-for-frame -- the ones that are
    # not reproducible even under a fixed seed -- this is all the check there
    # is, so it has to catch the two ways a demo dies quietly on a new numpy:
    # rendering nothing, and rendering the same nothing forever. A demo that
    # throws is easy; a demo that returns a black rectangle at 60 fps looks
    # perfectly healthy from the outside.
    out["peak"] = int(stack.max()) if stack.size else 0
    out["distinct"] = len(set(out["hashes"]))

    if dump_dir:
        _dump(stack, dump_dir, name)
    return out, stack


def _last_line():
    return traceback.format_exc().strip().splitlines()[-1]


def _dump(stack, dump_dir, name):
    from PIL import Image
    os.makedirs(dump_dir, exist_ok=True)
    for i, f in enumerate(stack):
        Image.fromarray(f, "RGB").save(os.path.join(dump_dir, "%s-%02d.png" % (name, i)))


# --------------------------------------------------------------------------
# The driver.
# --------------------------------------------------------------------------

def run_one(python, name, frames, npz_dir, dump_dir, timeout, live_clock=False):
    """Spawn a worker for one demo and read back its result."""
    tmp = os.path.join(npz_dir, name + ".json")
    cmd = [python, os.path.abspath(__file__), "--worker", name,
           "--frames", str(frames), "--worker-out", tmp, "--npz-dir", npz_dir]
    if dump_dir:
        cmd += ["--dump-frames", dump_dir]
    if live_clock:
        cmd += ["--live-clock"]
    # A fixed hash seed removes the one source of run-to-run variation that is
    # neither the demo's fault nor numpy's: dict/set iteration order. Without
    # it a demo that iterates a set of sprites is nondeterministic for a reason
    # that has nothing to do with the question being asked here.
    # Thread counts are pinned to one as well. Not because these demos use BLAS
    # -- they are ufuncs and gathers, no linear algebra in sight -- but because
    # a threaded numpy on a 24-thread desktop would make the timings a function
    # of what else is running, and the Pi has four slow cores anyway.
    env = dict(os.environ, PYTHONHASHSEED="0", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
               NUMEXPR_NUM_THREADS="1")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"demo": name, "status": "TIMEOUT", "hashes": [],
                "error": "no result after %gs" % timeout}
    if os.path.exists(tmp):
        with open(tmp) as fh:
            res = json.load(fh)
        os.unlink(tmp)
        return res
    # No result file at all: the interpreter died under the demo (segfault,
    # OOM, os._exit). That is a result too, and a loud one.
    tail = (proc.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
    return {"demo": name, "status": "CRASH", "hashes": [],
            "error": "exit %d: %s" % (proc.returncode, tail[:200])}


def environment(python):
    code = ("import sys, numpy, platform; print(sys.version.split()[0]); "
            "print(numpy.__version__); print(platform.machine())")
    outp = subprocess.run([python, "-c", code], stdout=subprocess.PIPE).stdout
    py, np_v, mach = outp.decode().split()
    return {"python": py, "numpy": np_v, "machine": mach}


def survey(python, names, frames, npz_dir, dump_dir, timeout, repeats,
           live_clock=False):
    env = environment(python)
    label = "py%s/np%s" % (env["python"], env["numpy"])
    results = {}
    for n, name in enumerate(names, 1):
        runs = []
        for r in range(repeats):
            sub = os.path.join(npz_dir, "run%d" % r)
            os.makedirs(sub, exist_ok=True)
            runs.append(run_one(python, name, frames, sub,
                                dump_dir if r == 0 else None, timeout,
                                live_clock))
        first = runs[0]
        # Self-stability is decided here, under one numpy, before anything is
        # compared across versions. Two runs, two processes, same everything.
        if all(r["status"] == "OK" for r in runs) and repeats > 1:
            first["self_stable"] = all(r["hashes"] == first["hashes"] for r in runs[1:])
        elif repeats > 1:
            first["self_stable"] = None
        else:
            first["self_stable"] = None
        results[name] = first
        flags = []
        if first.get("self_stable") is False:
            flags.append("NONDETERMINISTIC (smoke test only)")
        if first["status"] == "OK" and first.get("peak", 255) <= 8:
            flags.append("*** ALL BLACK")
        if first["status"] == "OK" and first.get("distinct", 2) <= 1:
            flags.append("*** FROZEN")
        print("[%2d/%d] %-12s %-11s %s  %s"
              % (n, len(names), name, first["status"],
                 ("%7.2f ms/f" % first["render_ms"]) if first.get("render_ms") else "         --",
                 "  ".join(flags)),
              flush=True)
    return {"label": label, "env": env, "demos": results}


# --------------------------------------------------------------------------
# Comparison.
# --------------------------------------------------------------------------

def pixel_diff(base_npz, new_npz, name):
    """Frames that differ, and the worst per-channel delta. None if unavailable."""
    import numpy as np
    a = os.path.join(base_npz, "run0", name + ".npy")
    b = os.path.join(new_npz, "run0", name + ".npy")
    if not (os.path.exists(a) and os.path.exists(b)):
        return None
    fa, fb = np.load(a).astype(np.int16), np.load(b).astype(np.int16)
    if fa.shape != fb.shape:
        return {"n_differ": len(fa), "max_abs": 255, "note": "shape changed"}
    d = np.abs(fa - fb)
    per_frame = d.reshape(len(d), -1).max(axis=1)
    return {"n_differ": int((per_frame > 0).sum()), "max_abs": int(d.max()),
            "n_pixels": int((d.max(axis=-1) > 0).sum())}


def compare(base, new, base_npz=None, new_npz=None):
    """Classify each demo in `new` against `base`. Returns rows for printing."""
    rows = []
    for name in sorted(set(base["demos"]) | set(new["demos"])):
        b = base["demos"].get(name)
        n = new["demos"].get(name)
        if n is None:
            rows.append((name, "MISSING", "", None))
            continue
        if n["status"] != "OK":
            rows.append((name, n["status"], n.get("error") or "", n.get("render_ms")))
            continue
        if b is None or b["status"] != "OK":
            rows.append((name, "NEW_OK", "baseline did not run", n.get("render_ms")))
            continue
        if b.get("self_stable") is False or n.get("self_stable") is False:
            # Not reproducible, so the frames say nothing. Smoke test instead:
            # it ran, it lit pixels, and it did not freeze on one image.
            peak, distinct = n.get("peak", 0), n.get("distinct", 0)
            bad = ("ALL BLACK" if peak <= 8 else
                   "FROZEN, one image for the whole run" if distinct <= 1 else "")
            rows.append((name, "SMOKE_FAIL" if bad else "RUNS_ONLY",
                         bad or "nondeterministic; smoke test only, peak %d, %d "
                         "distinct frames" % (peak, distinct), n.get("render_ms")))
            continue
        if b["hashes"] == n["hashes"]:
            rows.append((name, "IDENTICAL", "", n.get("render_ms")))
            continue
        note = "%d/%d frames differ" % (
            sum(1 for x, y in zip(b["hashes"], n["hashes"]) if x != y), len(b["hashes"]))
        if base_npz and new_npz:
            pd = pixel_diff(base_npz, new_npz, name)
            if pd:
                note = ("%d/%d frames differ, max |delta| %d over %d px"
                        % (pd["n_differ"], len(b["hashes"]), pd["max_abs"],
                           pd.get("n_pixels", -1)))
        rows.append((name, "DIFFERS", note, n.get("render_ms")))
    return rows


def print_matrix(base, new, rows):
    bt = base["demos"]
    print("\n%s  ->  %s" % (base["label"], new["label"]))
    print("%-12s %-10s %9s %9s %7s  %s"
          % ("demo", "status", "base ms", "new ms", "speed", "note"))
    for name, status, note, ms in rows:
        b = bt.get(name, {})
        bms = b.get("render_ms")
        ratio = ("%.2fx" % (bms / ms)) if (bms and ms) else "--"
        print("%-12s %-10s %9s %9s %7s  %s"
              % (name, status,
                 "%.2f" % bms if bms else "--", "%.2f" % ms if ms else "--",
                 ratio, note[:70]))
    ok = [r for r in rows if r[1] in ("IDENTICAL", "RUNS_ONLY", "DIFFERS")]
    tot_b = sum(bt[n].get("render_ms") or 0 for n, _, _, m in ok if n in bt)
    tot_n = sum(m or 0 for _, _, _, m in ok)
    if tot_b and tot_n:
        print("\ntotal render time over %d demos: %.1f ms -> %.1f ms  (%.2fx)"
              % (len(ok), tot_b, tot_n, tot_b / tot_n))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter to survey (its venv's numpy is what is tested)")
    ap.add_argument("--only", nargs="*", help="just these demos")
    ap.add_argument("--skip", nargs="*", default=[], help="demos to leave out")
    ap.add_argument("--frames", type=int, default=FRAMES)
    ap.add_argument("--repeats", type=int, default=2,
                    help="runs per demo, to decide self-stability (1 disables)")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out", help="write results JSON here")
    ap.add_argument("--npz-dir", help="where per-demo frame arrays live")
    ap.add_argument("--dump-frames", help="also write PNGs here (first run only)")
    ap.add_argument("--compare", nargs=2, metavar=("BASE", "NEW"),
                    help="print the matrix for two result files")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--live-clock", action="store_true",
                    help="do not freeze the wall clock (clock demos then differ)")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.list:
        print("\n".join(discover()))
        return 0

    if args.compare:
        with open(args.compare[0]) as fh:
            base = json.load(fh)
        with open(args.compare[1]) as fh:
            new = json.load(fh)
        print_matrix(base, new,
                     compare(base, new, base.get("npz_dir"), new.get("npz_dir")))
        return 0

    if args.worker:
        res, stack = fingerprint(args.worker, args.frames, args.dump_frames,
                                 frozen=not args.live_clock)
        if stack is not None and args.npz_dir:
            import numpy as np
            os.makedirs(args.npz_dir, exist_ok=True)
            np.save(os.path.join(args.npz_dir, args.worker + ".npy"), stack)
        with open(args.worker_out, "w") as fh:
            json.dump(res, fh)
        return 0

    names = args.only or discover()
    names = [n for n in names if n not in args.skip]
    npz_dir = args.npz_dir or os.path.join("/tmp/ft-numpy-compat",
                                           os.path.basename(os.path.dirname(
                                               os.path.dirname(args.python))))
    os.makedirs(npz_dir, exist_ok=True)
    res = survey(args.python, names, args.frames, npz_dir, args.dump_frames,
                 args.timeout, args.repeats, args.live_clock)
    res["npz_dir"] = npz_dir
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(res, fh, indent=1)
        print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
