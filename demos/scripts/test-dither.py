#!/usr/bin/env python3
"""Checks for dither.py that a screenshot cannot make.

This demo's whole claim is that four named algorithms are being run correctly,
and every way of getting one of them wrong still produces a picture that looks
like a dithered picture:

  1. **A kernel weight can be wrong** and Floyd-Steinberg still stipples --
     just with a bias, so the panel is a shade darker or lighter than the
     photograph beside it. So the diffusions are checked *numerically*: FS
     reproduces the source's mean brightness to within a level, on a ramp and
     on flat tones at both ends of the range, and Atkinson does not, because
     Atkinson deliberately throws a quarter of the error away. If those two
     ever agree, one of them is not what its label says.
  2. **Atkinson could quietly be Floyd-Steinberg.** They are two rows apart in
     the source and produce visually similar fields. They are checked to be
     *different* fields, and different in the specific direction Atkinson is
     known for: crushed shadows and blown highlights on flat tone.
  3. **Bayer could be tiling wrong.** A scrambled matrix still dithers; it
     just crawls. The ordered field is asserted to have exact period 8 in both
     axes on a flat input, and to hit the right density on a grey ramp.
  4. **The wipe could composite the wrong side.** Left and right of the
     boundary are two different quantisers, and swapping them is invisible in
     a still. The boundary is checked to move monotonically and to have the
     right panel on the right side of it.
  5. **A panel is beautiful and mislabelled.** The algorithm names are read
     back off the rendered pixels, not off the argument list.

Two things about how these are run, both learned the hard way in this tree.
`render` is asserted *pure* -- a cold call at t must equal the same t reached
by stepping from zero -- because everything it draws was baked in `build()`
and that is the demo's main performance claim as well as its correctness one.
And `ftdata.CACHE_DIR` binds at import, so the three data states -- fresh,
stale, absent -- are each exercised in a **separate process** with
FT_DATA_CACHE pointed at a temporary directory, at the bottom of this file.
Reloading the module in one process does not test what it looks like it tests.

    $ python3 scripts/test-dither.py                     # uses the live cache
    $ python3 scripts/test-dither.py --cache-dir /tmp/c  # or a pointed one

The live cache is only needed for the one check against real GOES imagery;
everything else builds its own cache directory or uses the generated test
image and needs nothing. Populate it with
`python3 ftdata.py --once --only goes-psw`.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                         # noqa: E402
import dither                                                  # noqa: E402
import ftdata                                                  # noqa: E402

FAILED = []
PASSED = [0]


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-56s %s" % (name, detail))
    else:
        print("  FAIL %-56s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(dither, **kw)


def contains_text(frame, s, thresh=150):
    """Is this string drawn anywhere on the frame, in the panel's own font?

    Reading the words back off the pixels is the only way to be sure the label
    on a region is the label for the algorithm that actually drew it.

    The negative space has to be dark as well, and that is not a nicety: a
    dithered picture contains large solid-white regions, and "every pixel the
    mask asks for is lit" is true of *any* mask inside one of them. Checking
    that the counters between the strokes are unlit is what stops this
    reporting that the panel says STALE when what it actually has is a cloud.
    """
    m = dither.text_mask(s)
    gh, gw = m.shape
    lit = frame.max(axis=2) > thresh
    H, W = lit.shape
    off = ~m
    n_off = max(1, int(off.sum()))
    for y in range(0, H - gh + 1):
        for x in range(0, W - gw + 1):
            box = lit[y:y + gh, x:x + gw]
            if np.all(box[m]) and box[off].sum() <= 0.2 * n_off:
                return True
    return False


# --------------------------------------------------------------------------
# The quantisers, against inputs whose answers cannot be argued with.
# --------------------------------------------------------------------------

def ramp(w=320, h=64):
    """A horizontal 0..255 ramp: the one input every method must get right."""
    x = np.arange(w, dtype=np.float32) * (255.0 / max(1, w - 1))
    return np.repeat(x[None, :], h, axis=0)


def test_quantisers():
    print("quantisers")
    src = ramp()

    fs = dither.diffuse(src, *dither.FS_KERNEL)
    atk = dither.diffuse(src, *dither.ATK_KERNEL)
    bay = dither.bayer_dither(src)
    thr = dither.threshold_dither(src)

    for nm, f in (("fs", fs), ("atkinson", atk), ("bayer", bay),
                  ("threshold", thr)):
        check("%s field is 0/1 uint8" % nm,
              f.dtype == np.uint8 and set(np.unique(f)) <= {0, 1},
              str(f.dtype))

    # Floyd-Steinberg conserves error: every sixteenth goes somewhere, so the
    # density of the output is the mean of the input. One level is generous;
    # in practice this comes out around a hundredth.
    want = src.mean() / 255.0
    check("floyd-steinberg preserves mean brightness",
          abs(fs.mean() - want) * 255.0 < 1.0,
          "%+.3f levels" % ((fs.mean() - want) * 255.0))

    check("atkinson and floyd-steinberg are different fields",
          not np.array_equal(atk, fs),
          "%.1f%% of pixels differ" % (100.0 * (atk != fs).mean()))

    # Atkinson's signature, and the thing that proves the discarded quarter is
    # really being discarded: on a large *flat* field it loses the ends of the
    # range. A ramp is too kind to show this -- there is always a brighter
    # neighbour along to carry the residue -- so it is asked on flat tones,
    # where the two eighths that go nowhere accumulate into a visible bias.
    for v, word, worse in ((18.0, "shadow", "fewer"), (238.0, "highlight",
                                                       "more")):
        flat_v = np.full((64, 320), v, np.float32)
        f_fs = dither.diffuse(flat_v, *dither.FS_KERNEL).mean()
        f_at = dither.diffuse(flat_v, *dither.ATK_KERNEL).mean()
        ok = (f_at < f_fs) if worse == "fewer" else (f_at > f_fs)
        check("atkinson crushes the %s end (%s dots than fs)" % (word, worse),
              ok, "atk %.4f vs fs %.4f, source %.4f" % (f_at, f_fs, v / 255.0))
        check("floyd-steinberg holds the %s end" % word,
              abs(f_fs - v / 255.0) * 255.0 < 1.5,
              "%+.2f levels" % ((f_fs - v / 255.0) * 255.0))

    # Ordered dither on a flat field must be exactly periodic. This is what
    # catches a scrambled or mis-tiled matrix, which still looks like a dither.
    flat = np.full((64, 320), 100.0, np.float32)
    fb = dither.bayer_dither(flat)
    check("bayer tiles with period 8 in x",
          np.array_equal(fb[:, :8], fb[:, 8:16]) and
          np.array_equal(fb[:, :8], fb[:, 312:320]))
    check("bayer tiles with period 8 in y",
          np.array_equal(fb[:8], fb[8:16]))
    check("bayer matrix is a permutation of 0..63",
          sorted(dither.BAYER8.reshape(-1).tolist()) == list(range(64)))
    # A flat value v should light round(v/255*64) of every 64 cells.
    for v in (32.0, 96.0, 160.0, 224.0):
        f = dither.bayer_dither(np.full((64, 320), v, np.float32))
        lit = f[:8, :8].sum()
        want_lit = int(round(v / 255.0 * 64.0))
        check("bayer density at %d is %d/64" % (v, want_lit),
              abs(int(lit) - want_lit) <= 1, "got %d" % lit)

    check("threshold is exactly src >= 128",
          np.array_equal(thr, (src >= 128.0).astype(np.uint8)))
    # And the control really is the worst of the four at the job.
    err = {}
    for nm, f in (("fs", fs), ("atkinson", atk), ("bayer", bay),
                  ("threshold", thr)):
        err[nm] = abs(f.mean() - want) * 255.0
    check("threshold has the worst mean error of the four",
          err["threshold"] == max(err.values()),
          " ".join("%s %.2f" % kv for kv in sorted(err.items())))

    # The kernels themselves, since a typo in a weight is the single most
    # likely way for this file to be quietly wrong.
    check("fs kernel sums to its divisor",
          sum(w for _, _, w in dither.FS_KERNEL[0]) == dither.FS_KERNEL[1])
    check("atkinson kernel sums to 6 of 8",
          sum(w for _, _, w in dither.ATK_KERNEL[0]) == 6.0
          and dither.ATK_KERNEL[1] == 8.0)
    for nm, (kern, _) in (("fs", dither.FS_KERNEL),
                          ("atkinson", dither.ATK_KERNEL)):
        ok = all(dy > 0 or dx > 0 for dx, dy, _ in kern)
        check("%s kernel only pushes error forward" % nm, ok)

    # The edge path and the interior fast path must agree. They are two
    # separate pieces of code in diffuse() and only one of them is exercised
    # by most pixels.
    small = ramp(9, 5)
    ref = dither.diffuse(small, *dither.FS_KERNEL)
    check("diffusion works on an image smaller than the kernel reach",
          ref.shape == (5, 9))


# --------------------------------------------------------------------------
# The panel: geometry, the wipe, and the labels.
# --------------------------------------------------------------------------

def test_panel():
    print("panel")
    r = dither.build(opts(source="test"))
    f = r(0.0, 0)
    check("frame is (64, 320, 3) uint8", f.shape == (64, 320, 3)
          and f.dtype == np.uint8, str(f.shape))
    check("cycle is a sane length", 15.0 < r.cycle < 60.0,
          "%.1f s" % r.cycle)

    # Every baked panel is a whole frame, ready to copy. If one of these were
    # ever built at the wrong size the wipe would raise on the wall.
    ok = all(p.shape == (64, 320, 3) for p in r.panels)
    ok = ok and all(p.shape == (64, 320, 3) for p in r.zoom.values())
    check("all baked panels are full frames", ok,
          "%d at 1:1, %d magnified" % (len(r.panels), len(r.zoom)))

    # The first wipe: continuous tone giving way to Floyd-Steinberg, left to
    # right. Sample it across the crossing and check the boundary only ever
    # moves one way and that the two sides are the two panels they claim.
    edge = np.array(dither.C_EDGE, np.uint8)
    last = -1
    moved = True
    matched = 0
    for i in range(1, 63):
        t = i * 0.05
        fr = r(t, i)
        cols = np.where(np.all(fr[32] == edge, axis=-1))[0]
        if not len(cols):
            continue
        b = int(cols[0])
        if b <= last:
            moved = False
        last = b
        if 8 < b < 312:
            left_ok = np.array_equal(fr[10:50, :b], r.panels[dither.FS][10:50, :b])
            right_ok = np.array_equal(fr[10:50, b + 1:],
                                      r.panels[dither.CONT][10:50, b + 1:])
            if left_ok and right_ok:
                matched += 1
    check("the wipe boundary only ever moves forward", moved,
          "last at column %d" % last)
    check("left of the boundary is the incoming quantiser, right the outgoing",
          matched > 10, "%d sampled frames agreed" % matched)

    # And the names are on the pixels, not just in the argument list.
    for stage, name in enumerate(dither.STAGE_NAMES):
        check("%r is drawn on its own panel" % name,
              contains_text(r.panels[stage], name))
    # The source caption is deliberately dimmer than the algorithm names --
    # it is furniture -- so it is read back at a lower threshold.
    check("the source is named on the panel",
          contains_text(r.panels[0], "TEST IMAGE", thresh=100))


def test_purity():
    print("purity")
    a = dither.build(opts(source="test"))
    for t in (0.0, 2.35, 7.9, 16.4, 21.05, 27.7):
        cold = a(t, 0).copy()
        b = dither.build(opts(source="test"))
        n = int(t * 20)
        for i in range(n + 1):
            b(i / 20.0, i)
        warm = b(t, n).copy()
        check("render(%.2f) is the same cold as driven" % t,
              np.array_equal(cold, warm))
    # Two independent builds must agree completely: there is no randomness in
    # this demo at all, and if one ever appears it has to be seeded.
    c = dither.build(opts(source="test"))
    d = dither.build(opts(source="test"))
    check("two builds produce identical panels",
          all(np.array_equal(p, q) for p, q in zip(c.panels, d.panels)))


def test_timing():
    print("timing")
    r = dither.build(opts(source="test"))
    n = int(r.cycle * 20)
    ts = []
    for i in range(n):
        t0 = time.perf_counter()
        r(i / 20.0, i)
        ts.append(time.perf_counter() - t0)
    ts = np.sort(np.array(ts)) * 1000.0
    mean, p95, mx = ts.mean(), ts[int(0.95 * len(ts))], ts[-1]
    print("       %d frames  mean %.3f ms  p95 %.3f ms  max %.3f ms"
          % (n, mean, p95, mx))
    # A very slack bound: this is a desktop and the wall is twenty times
    # slower, but a regression that put arithmetic back into render() would
    # blow past this by orders of magnitude.
    check("render stays under 1 ms a frame on a desktop", mean < 1.0,
          "%.3f ms" % mean)
    print("       build took %.0f ms" % r.build_ms)


def test_source_choice():
    """The two selection rules that decide what actually gets dithered."""
    print("subject selection")
    # A bimodal image -- pure black and pure white, nothing between -- is the
    # trap pick_frame used to fall into. midtone_score must rank it below a
    # genuine gradient.
    bimodal = np.where(np.arange(320)[None, :] < 160, 0.0, 255.0)
    bimodal = np.repeat(bimodal, 64, 0).astype(np.float32)
    check("midtone score prefers a gradient to a silhouette",
          dither.midtone_score(ramp()) > dither.midtone_score(bimodal),
          "%.3f vs %.3f" % (dither.midtone_score(ramp()),
                            dither.midtone_score(bimodal)))
    # And the detail window must land somewhere that has midtone in it, not on
    # the flat black half of a picture that is half flat black.
    half = np.zeros((64, 320), np.float32)
    half[:, 160:] = ramp()[:, :160]
    x0, y0 = dither.detail_window(half, 320, 64, 80, 16)
    check("detail window avoids the empty half of a picture", x0 >= 120,
          "picked x=%d y=%d" % (x0, y0))


# --------------------------------------------------------------------------
# The three data states, each in its own process. ftdata.CACHE_DIR binds at
# import, so this cannot be done by reassigning it.
# --------------------------------------------------------------------------

def write_cache(cache_dir, age_seconds, frames=6):
    """Fabricate a goes-psw record and sidecar of a known age."""
    os.makedirs(cache_dir, exist_ok=True)
    now = time.time()
    # A window of ramps with a moving bright wedge, so pick_frame has
    # something to prefer and the panel has midtone everywhere.
    w, h = 320, 64
    x = np.arange(w, dtype=np.float32)[None, :]
    y = np.arange(h, dtype=np.float32)[:, None]
    stack = np.empty((frames, h, w, 3), np.uint8)
    for i in range(frames):
        g = (128.0 + 100.0 * np.sin((x / 40.0) + i) * np.cos(y / 22.0))
        g = np.clip(g, 0, 255).astype(np.uint8)
        stack[i] = g[:, :, None]
    stamps = [now - age_seconds - 300.0 * (frames - 1 - i)
              for i in range(frames)]
    blob = ftdata.store_blob("goes-psw", {"frames": stack,
                                          "stamps": np.array(stamps)},
                             cache_dir)
    payload = {"blob": blob, "count": frames, "stamps": stamps,
               "cadence": 300, "want": frames, "sat": "GOES18",
               "sector": "psw"}
    with open(os.path.join(cache_dir, "goes-psw.json"), "w") as fh:
        json.dump({"payload": payload, "fetched_at": now - age_seconds}, fh)


def state_child(mode, cache_dir):
    """Run inside the subprocess: build the panel and report what it says."""
    r = dither.build(opts(cache_dir=cache_dir))
    frame = r(1.6, 32)
    out = {
        "source": r.source_name,
        "problem": r.problem,
        "caption": r.caption_line,
        "stale_on_panel": contains_text(frame, "STALE"),
        "test_on_panel": contains_text(frame, "TEST IMAGE", thresh=100),
        "nonblank": int(frame.max()),
        "shape": list(frame.shape),
    }
    # And a whole loop, headless, to prove no state raises partway through.
    for i in range(int(r.cycle * 20) + 3):
        r(i / 20.0, i)
    print("RESULT " + json.dumps(out))


def run_state(mode):
    tmp = tempfile.mkdtemp(prefix="dither-%s-" % mode)
    try:
        cache = os.path.join(tmp, "cache")
        os.makedirs(cache)
        if mode == "fresh":
            write_cache(cache, age_seconds=120.0)
        elif mode == "stale":
            write_cache(cache, age_seconds=3.0 * 86400.0)
        env = dict(os.environ)
        env["FT_DATA_CACHE"] = cache
        env["FT_DATA_BLOBS"] = cache
        out = subprocess.check_output(
            [sys.executable, os.path.abspath(__file__), "--state", mode,
             "--cache-dir", cache], env=env, stderr=subprocess.STDOUT)
        for line in out.decode().splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[7:])
        raise RuntimeError("child produced no result:\n" + out.decode())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_states():
    print("data states (each in its own process)")
    fresh = run_state("fresh")
    check("fresh: names the satellite", fresh["source"] == "GOES18 PSW",
          fresh["caption"])
    check("fresh: does not cry stale", not fresh["stale_on_panel"])
    check("fresh: draws something", fresh["nonblank"] > 200)

    stale = run_state("stale")
    check("stale: still draws the imagery", stale["source"] == "GOES18 PSW",
          stale["caption"])
    check("stale: says STALE on the panel in pixels",
          stale["stale_on_panel"], stale["caption"])

    absent = run_state("absent")
    check("absent: falls back to the generated image",
          absent["source"] == "TEST IMAGE", absent["caption"])
    check("absent: says so on the panel in pixels", absent["test_on_panel"])
    check("absent: is not a blank or a card", absent["nonblank"] > 200)
    check("absent: reports why the cache was no good",
          bool(absent["problem"]), str(absent["problem"]))


def test_live(cache_dir):
    """The one check that needs a real cache: it works on real imagery."""
    print("live cache")
    got = ftdata.load(ftdata.GOES_PRODUCT, cache_dir)
    if got is None:
        print("  skip   no goes-psw in the cache "
              "(python3 ftdata.py --once --only goes-psw)")
        return
    r = dither.build(opts(cache_dir=cache_dir))
    check("real imagery is what got dithered", r.problem is None
          and r.source_name != "TEST IMAGE", r.source_name)
    check("the dithered field is not all one value",
          0.05 < r.fields[dither.FS].mean() < 0.95,
          "%.3f white" % r.fields[dither.FS].mean())
    check("the punch-in window has midtone in it",
          dither.midtone_score(
              r.source[r.detail[1]:r.detail[1] + r.detail[3],
                       r.detail[0]:r.detail[0] + r.detail[2]]) > 0.3,
          "at %s" % (r.detail,))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--state", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.state:
        state_child(args.state, args.cache_dir)
        return 0

    test_quantisers()
    test_panel()
    test_purity()
    test_source_choice()
    test_timing()
    test_states()
    test_live(args.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  FAILED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
