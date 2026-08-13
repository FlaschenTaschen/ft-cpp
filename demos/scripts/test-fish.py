#!/usr/bin/env python3
"""Checks for fish.py that a screenshot cannot make.

A tank is the hardest kind of demo to test, because there is no right answer to
compare against -- it is not showing a number, and any frame full of fish looks
like a success. But it can be broken in ways that a still frame hides
completely, and every one of these has either happened here or came within a
line of happening:

  1. **The peduncle can break.** The body wave displaces the centre line, and
     the tail joins the body through a two-pixel-thick stalk. Measured
     vertically along a steep diagonal, two pixels is less than one, and the
     tail visibly detaches -- but only at the extremes of the beat, so a
     screenshot at the wrong phase is perfect. Asserted by flood filling every
     baked sprite and requiring exactly one connected component.
  2. **The turn can stop turning.** Squash level comes from dx/dt over a
     reference speed. Get the reference wrong and a fish either never narrows
     (it reverses like a sprite in a 1982 game) or sits permanently head-on (it
     becomes a floating sliver). Asserted by sweeping a whole swim period and
     requiring each fish to reach both full-profile extremes *and* the narrow
     middle, with the direction flipping exactly twice.
  3. **The tail can stall.** The beat is a base rate plus a `sin(2*theta)`
     term, and if that term ever exceeds the base the phase runs backwards and
     the fish swims with a stuttering tail. Asserted as strict monotonicity of
     the beat, plus full coverage of the six baked phases.
  4. **The chase can quietly never happen.** The jerk is the only story in the
     panel and it is one blended term; if the lock envelope never reaches 1 the
     demo is just eleven fish minding their business, and it looks completely
     fine. Asserted on the actual distance to the victim, locked versus not.
  5. **The visitor can never arrive**, or never leave. Asserted against the
     schedule, including that it is off screen -- and therefore free -- for the
     large majority of the cycle.
  6. **It can stop being a pure function of t.** The scheduler builds segments
     ahead on a worker thread and the preview baker steps at its own rate, so a
     single accumulated variable anywhere desyncs the wall from the preview.
     Asserted by comparing a cold render against the same t reached by driving
     frame by frame from zero.
  7. **The water can go black.** Fish on true black read as fish in space. One
     changed constant does that and nothing else complains.

Everything here is seeded and uses fish.py's own defaults, so the checks are
deterministic.

    $ python3 scripts/test-fish.py
    $ python3 scripts/test-fish.py --bench      # also time a long run
"""

import argparse
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import fish                                                   # noqa: E402

FAILED = []
PASSED = [0]

FPS = 20


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    return ds.options(fish, **kw)


# --------------------------------------------------------------------------

def components(mask):
    """Number of 4-connected components in a boolean array. Small arrays only."""
    h, w = mask.shape
    seen = np.zeros_like(mask)
    n = 0
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or seen[sy, sx]:
                continue
            n += 1
            stack = [(sy, sx)]
            seen[sy, sx] = True
            while stack:
                y, x = stack.pop()
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
    return n


def test_sprites_are_one_piece():
    """Every fish, at every tail phase and every squash, is a single blob.

    This is the peduncle test. The threshold is deliberately generous -- 0.35
    coverage, not 0.5 -- because the failure mode is not a hairline gap, it is
    the tail sitting several pixels clear of the body.
    """
    water = np.asarray(fish.WATER_MID, np.float32)
    worst = None
    bad = 0
    total = 0
    for length, amp in ((46.0, 6.6), (30.0, 4.4), (18.0, 2.8), (9.0, 1.5),
                        (84.0, 6.4)):
        for k in range(7):
            c = -1.0 + 2.0 * k / 6.0
            squash = 0.22 + 0.78 * abs(c)      # same spacing bake_fish uses
            for j in range(fish.PH):
                _, a = fish.fish_sprite(length, amp, fish.STYLES[0],
                                        j / float(fish.PH), squash, 0.1, water)
                n = components(a > 0.35)
                total += 1
                if n != 1:
                    bad += 1
                    if worst is None:
                        worst = (length, amp, round(squash, 2), j, n)
    check("every baked sprite is one connected piece", bad == 0,
          "%d/%d bad%s" % (bad, total, "" if worst is None else " first=%r" % (worst,)))


def test_body_wave_actually_moves():
    """The tail tip must swing, and swing much further than the head."""
    water = np.asarray(fish.WATER_MID, np.float32)
    tips, heads = [], []
    for j in range(fish.PH):
        _, a = fish.fish_sprite(40.0, 6.0, fish.STYLES[0], j / float(fish.PH),
                                1.0, 0.0, water)
        rows = np.arange(a.shape[0])
        col = a[:, 1]                                  # near the tail tip
        tips.append(float((rows * col).sum() / max(col.sum(), 1e-6)))
        col = a[:, -3]                                 # the nose
        heads.append(float((rows * col).sum() / max(col.sum(), 1e-6)))
    tail_swing = max(tips) - min(tips)
    head_swing = max(heads) - min(heads)
    check("tail beats", tail_swing > 2.0, "swing %.2f px" % tail_swing)
    check("head is much steadier than the tail", head_swing < 0.35 * tail_swing,
          "head %.2f px vs tail %.2f px" % (head_swing, tail_swing))


def test_fish_turn():
    """Over one swim period every fish reaches both profiles and the sliver."""
    r = fish.build(opts())
    tank = r.tank
    bad_ends, bad_mid, bad_flips = [], [], []
    for i, f in enumerate(tank["fishes"]):
        period = 2.0 * math.pi / f["w"]
        ks = []
        for n in range(400):
            t = period * n / 400.0
            if f["jerk"]:
                x, _ = fish._pursuit(f, f["victim"], t)
                x2, _ = fish._pursuit(f, f["victim"], t + 0.05)
            else:
                x, _ = fish._path(f, t)
                x2, _ = fish._path(f, t + 0.05)
            c = (x2 - x) * 20.0 / f["vref"]
            c = max(-1.0, min(1.0, c))
            # Same warp render() applies: the table is uniform in width, so the
            # foreshortening curve lives in the lookup. Kept in step with
            # fish.py deliberately -- modelling the old mapping here would let
            # a change in render() pass unnoticed.
            m = math.copysign(abs(c) ** 0.85, c)
            ks.append(int(round((m + 1.0) * 0.5 * (f["nsq"] - 1))))
        ks = np.asarray(ks)
        mid = (f["nsq"] - 1) // 2
        if not (ks.min() == 0 and ks.max() == f["nsq"] - 1):
            bad_ends.append(i)
        # It must pass *through* the narrow middle, not jump the sign.
        if not (ks <= mid).any() or not (ks >= mid).any() or (ks == mid).sum() < 2:
            bad_mid.append(i)
        # Counted *cyclically*: a linear diff over exactly one period reports
        # one flip whenever the window happens to open on a turn, which is a
        # property of where sampling started and not of the fish.
        side = np.sign(ks - mid)
        side = side[side != 0]
        side = np.concatenate([side, side[:1]])
        flips = int((np.diff(side) != 0).sum())
        # The jerk is exempt: its path is a blend towards another fish, so a
        # dart back the other way mid-crossing is the behaviour, not a bug.
        if flips != 2 and not f["jerk"]:
            bad_flips.append((i, flips))
    check("every fish reaches both full profiles", not bad_ends, str(bad_ends))
    check("every fish passes through the head-on sliver", not bad_mid, str(bad_mid))
    check("every fish turns exactly twice per period", not bad_flips,
          str(bad_flips))


def test_tail_phase_advances():
    """The beat modulation must never run the phase backwards."""
    r = fish.build(opts())
    bad = []
    for i, f in enumerate(r.tank["fishes"]):
        period = 2.0 * math.pi / f["w"]
        ts = np.arange(0.0, period, period / 3000.0)
        th = f["w"] * ts + f["ph"]
        turns = f["beat"] * ts + 0.14 * np.sin(2.0 * th)
        d = np.diff(turns)
        seen = set(int(x * fish.PH) % fish.PH for x in turns)
        if d.min() <= 0 or len(seen) != fish.PH:
            bad.append((i, round(float(d.min()), 6), len(seen)))
    check("tail phase is strictly increasing and covers all %d" % fish.PH,
          not bad, str(bad))


def test_jerk_chases():
    """Locked on, the jerk is far closer to its victim than when it is not."""
    r = fish.build(opts())
    jerks = [f for f in r.tank["fishes"] if f["jerk"]]
    check("there is exactly one jerk", len(jerks) == 1, "%d" % len(jerks))
    if not jerks:
        return
    j = jerks[0]
    near, far = [], []
    for n in range(4000):
        t = n * 0.05
        g = math.sin(2 * math.pi * t / j["lock_p"] + j["lock_ph"])
        lock = min(1.0, max(0.0, (g - 0.25) / 0.45))
        x, y = fish._pursuit(j, j["victim"], t)
        vx, vy = fish._path(j["victim"], t)
        d = math.hypot(x - vx, y - vy)
        if lock > 0.95:
            near.append(d)
        elif lock == 0.0:
            far.append(d)
    check("the jerk does lock on", len(near) > 100, "%d locked samples" % len(near))
    check("locked, it is much closer to its victim",
          np.median(near) < 0.45 * np.median(far),
          "median %.1f px locked vs %.1f px free" % (np.median(near), np.median(far)))


def test_visitor_schedule():
    """It arrives, it crosses the whole panel, it leaves, and it is mostly gone."""
    r = fish.build(opts())
    v = r.tank["visitor"]
    W = r.tank["W"]
    check("there is a visitor", v is not None)
    if v is None:
        return
    xs = []
    present = 0
    N = 6000
    for n in range(N):
        t = v["period"] * n / N
        p = fish._visitor_path(v, t, W)
        if p is not None:
            present += 1
            xs.append(p[0])
    frac = present / float(N)
    check("visitor is off screen most of the cycle", frac < 0.55,
          "present %.0f%% of %.0f s" % (100 * frac, v["period"]))
    check("visitor crosses the whole panel",
          min(xs) < -0.5 * v["len"] and max(xs) > W + 0.5 * v["len"] - v["len"],
          "x %.0f .. %.0f over W=%d" % (min(xs), max(xs), W))


def test_fish_stay_in_the_water():
    """No fish is drawn into the sand or out of the top of the tank."""
    r = fish.build(opts())
    tank = r.tank
    H = tank["H"]
    floor = H - tank["sand_rows"]
    bad = []
    for i, f in enumerate(tank["fishes"]):
        h = f["tab"][f["nsq"] - 1][0][1].shape[1]
        lo, hi = 1e9, -1e9
        for n in range(600):
            t = n * 0.37
            if f["jerk"]:
                _, y = fish._pursuit(f, f["victim"], t)
            else:
                _, y = fish._path(f, t)
            lo = min(lo, y - h * 0.5)
            hi = max(hi, y + h * 0.5)
        if lo < -2.0 or hi > floor + 3.0:
            bad.append((i, round(lo, 1), round(hi, 1)))
    check("fish stay between the surface and the sand", not bad,
          "floor=%d %s" % (floor, bad))


def test_water_is_not_black():
    """Fish on true black read as fish in space; the tank must have a ground."""
    r = fish.build(opts())
    f = r(31.0, 620)
    # Sample a band of open water high in the frame, away from the sand.
    band = f[4:20].reshape(-1, 3)
    lum = band.astype(np.int32).sum(1)
    check("open water is lit, not black", np.percentile(lum, 5) >= 12,
          "5th pct sum(rgb) = %d" % np.percentile(lum, 5))
    check("water is blue-green, not grey",
          float(band[:, 2].mean()) > 1.5 * float(band[:, 0].mean()),
          "R %.1f B %.1f" % (band[:, 0].mean(), band[:, 2].mean()))


def test_purity():
    """A cold render(t) equals the same t reached by driving from zero."""
    cold = fish.build(opts())
    warm = fish.build(opts())
    for i in range(1500):
        warm(i / float(FPS), i)
    bad = []
    for t in (0.0, 7.35, 41.0, 74.95, 200.0):
        a = cold(t, int(t * FPS)).copy()
        b = warm(t, int(t * FPS)).copy()
        if not np.array_equal(a, b):
            bad.append((t, int(np.abs(a.astype(int) - b.astype(int)).max())))
    check("render is a pure function of t", not bad, str(bad))


def test_seed_determinism():
    """Same seed, same tank; different seed, different tank."""
    a = fish.build(opts(seed=7))(19.0, 380).copy()
    b = fish.build(opts(seed=7))(19.0, 380).copy()
    c = fish.build(opts(seed=8))(19.0, 380).copy()
    check("same seed reproduces the frame exactly", np.array_equal(a, b))
    check("a different seed is a different tank", not np.array_equal(a, c))


def test_shapes_and_options():
    """Every combination still returns a well formed frame."""
    bad = []
    for kw in ({}, {"shoal_on": False}, {"weed_on": False}, {"crab_on": False},
               {"visitor": 0.0}, {"fish": 0}, {"bubbles": 0}, {"dither": False},
               {"fish": 1, "shoal": 1, "weed": 1}, {"caustics": 0.0},
               {"fish": 20, "shoal": 90, "speed": 2.0}):
        try:
            r = fish.build(opts(**kw))
            for t in (0.0, 13.7, 96.0):
                f = r(t, int(t * FPS))
                if f.shape != (64, 320, 3) or f.dtype != np.uint8:
                    bad.append((kw, f.shape, f.dtype))
        except Exception as exc:                             # noqa: BLE001
            bad.append((kw, repr(exc)))
    check("all option combinations render (64,320,3) uint8", not bad, str(bad))


def test_full_run():
    """Ten minutes of frames, including a whole visitor cycle, without error."""
    r = fish.build(opts())
    n = 0
    for i in range(FPS * 600):
        f = r(i / float(FPS), i)
        n += 1
    check("600 s renders clean", n == FPS * 600, "%d frames" % n)


def bench():
    r = fish.build(opts())
    for i in range(50):
        r(i / float(FPS), i)
    ts = []
    for i in range(FPS * 300):
        t0 = time.perf_counter()
        r(i / float(FPS), i)
        ts.append((time.perf_counter() - t0) * 1000.0)
    ts = np.asarray(ts)
    print("\n  %d frames: mean %.3f ms  p95 %.3f  p99 %.3f  max %.3f"
          % (len(ts), ts.mean(), np.percentile(ts, 95), np.percentile(ts, 99),
             ts.max()))
    t0 = time.perf_counter()
    fish.build(opts())
    print("  build(): %.0f ms" % ((time.perf_counter() - t0) * 1000.0))


def _facing(alpha, thresh=0.35):
    """Which way a baked sprite points, from its silhouette alone.

    A fish tapers to a point at the nose, swells through the body, necks down
    to a thin peduncle and then flares out again into the caudal fin. So the
    two ends are told apart by the waist: the tail end dips to a local minimum
    before its final flare, and the nose end just runs out. Returns "left",
    "right", or None when the shape is too squashed to call.
    """
    h = (alpha > thresh).sum(axis=0).astype(float)
    nz = np.nonzero(h)[0]
    if len(nz) < 6:
        return None
    body = h[nz[0]:nz[-1] + 1]
    mid = int(np.argmax(body))

    def waist(seg):
        if len(seg) < 4:
            return False
        m = int(np.argmin(seg))
        return 0 < m < len(seg) - 1 and seg[m] < 0.65 * max(seg[0], seg[-1])

    tail_left = waist(body[:mid + 1][::-1])
    tail_right = waist(body[mid:])
    if tail_left == tail_right:
        return None
    return "right" if tail_left else "left"


def test_fish_face_the_way_they_swim():
    """A fish must point where it is going.

    bake_fish promises index 0 is full profile swimming left and the last
    index is full profile swimming right, and fish_sprite bakes a right-facing
    fish -- so exactly the *left* half of the table may be mirrored. That
    condition was inverted, and every fish in the tank swam tail-first.

    No still frame catches this and none of the other checks can: a mirrored
    fish is a perfectly good fish, one-piece, correctly shaded, beating its
    tail. It only reads as wrong once it is moving, which is why the test has
    to compare the silhouette against the direction the table index means.
    """
    nsq = 9
    tab = fish.bake_fish(26, 6.0, fish.VISITOR_STYLE, 0.0, (10, 40, 60), nsq)
    bad, checked = [], 0
    for k, want in ((0, "left"), (nsq - 1, "right")):
        for j in range(fish.PH):
            _, ia = tab[k][j]
            got = _facing(1.0 - ia[0, ..., 0])
            if got is None:
                bad.append("k=%d j=%d silhouette unreadable" % (k, j))
                continue
            checked += 1
            if got != want:
                bad.append("k=%d j=%d faces %s, swims %s" % (k, j, got, want))
    # Every full-profile sprite must be readable and right: at full profile
    # there is no squash to blur the shape, so a None here is itself a failure.
    check("full-profile fish face their direction of travel",
          checked == 2 * fish.PH and not bad,
          "%d/%d sprites checked, %d wrong%s"
          % (checked, 2 * fish.PH, len(bad), ("; " + bad[0]) if bad else ""))


def test_turn_is_smooth():
    """No single step of the squash table may jump more than a couple of pixels.

    The turn is quantised to the table, so one step is one visible jump in the
    fish's width. Sizing the table by size class gave the *biggest* fish the
    fewest levels -- a 37 px fish crossed in five steps, changing width sixteen
    pixels at a time, which reads as a snap rather than a turn. Two things fix
    it and both are asserted here: enough levels for the fish's length, and
    levels spaced uniformly in width rather than in velocity (spaced the other
    way the widest jump lands right where the turn is fastest).

    Measured off the rendered silhouettes, not recomputed from the formula, so
    it fails if the spacing changes for any reason.
    """
    bad, checked = [], 0
    for L in (7, 13, 27, 37, 44):
        per_side = int(math.ceil(0.78 * L / fish.MAX_SQUASH_STEP_PX))
        nsq = max(7, 2 * per_side + 1)
        tab = fish.bake_fish(L, max(1.2, 0.14 * L), fish.VISITOR_STYLE, 0.0,
                             (10, 40, 60), nsq)
        widths = []
        for row in tab:
            _, ia = row[0]
            a = 1.0 - ia[0, ..., 0]
            widths.append(int(((a > 0.35).sum(axis=0) > 0).sum()))
        step = max(abs(widths[i + 1] - widths[i]) for i in range(len(widths) - 1))
        checked += 1
        if step > fish.MAX_SQUASH_STEP_PX:
            bad.append("len=%d nsq=%d steps %d px" % (L, nsq, step))
    check("the turn never jumps more than %d px of width"
          % fish.MAX_SQUASH_STEP_PX,
          checked == 5 and not bad,
          "%d sizes checked%s" % (checked, ("; " + bad[0]) if bad else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--bench", action="store_true", help="also time a long run")
    args = ap.parse_args()

    print("fish.py")
    test_sprites_are_one_piece()
    test_body_wave_actually_moves()
    test_fish_turn()
    test_fish_face_the_way_they_swim()
    test_turn_is_smooth()
    test_tail_phase_advances()
    test_jerk_chases()
    test_visitor_schedule()
    test_fish_stay_in_the_water()
    test_water_is_not_black()
    test_purity()
    test_seed_determinism()
    test_shapes_and_options()
    test_full_run()
    if args.bench:
        bench()

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  FAILED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
