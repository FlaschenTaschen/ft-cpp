#!/usr/bin/env python3
"""Checks for pong.py that a screenshot cannot make.

This demo can draw a perfectly convincing game of Pong that is quietly lying,
and every one of the lies looks exactly like Pong:

  1. **The score can disagree with the picture.** Who wins each point is
     decided by the book, in closed form, before any geometry happens; the
     paddles then have to be talked into agreeing. If a paddle covers the ball
     on the deciding shot, or fails to cover it on any other one, the panel
     shows a point going to the wrong player -- and it looks completely
     normal. Asserted over the whole book: 4096 rallies, every contact.
  2. **The score can stop being a function of the clock.** The whole promise
     is that a power cut costs nothing, so the number at a given absolute
     instant must not depend on when build() ran. Asserted by building at two
     different epochs and comparing pixels.
  3. **It can run away.** A rivalry where one side is 40% ahead is not a
     rivalry, and a score that has outgrown its own numerals is not readable.
     Asserted at one day, one month, one year and ten years.
  4. **The angle can stop coming from the hit.** The outgoing angle is bent
     for reachability, and if the bending wins every time the play stops being
     legible -- the ball just goes wherever it needs to. Asserted as a
     correlation between the hit offset and the angle that came out.

Everything here builds with --epoch pinned, so the checks are deterministic
even though the demo on the wall is not.

    $ python3 scripts/test-pong.py
    $ python3 scripts/test-pong.py --bench      # also time a full loop
"""

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import pong                                                   # noqa: E402

FAILED = []
PASSED = [0]

# Pinned. A round number of seconds after the match epoch, so "day N" and the
# score are both stable whatever day this suite is run on.
EPOCH = pong.MATCH_EPOCH + 11.0 * 86400.0 + 1234.0


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    kw.setdefault("epoch", EPOCH)
    return ds.options(pong, **kw)


def built(**kw):
    r = pong.build(opts(**kw))
    return r, r.match


def ball_at(m, rec, tau):
    """Where the ball is, from the rally's own segments."""
    span = m["y_hi"] - m["y_lo"]
    for (t0, t1, x0, x1, y0, slope) in rec["segs"]:
        if t0 <= tau <= t1:
            bx = x0 + (x1 - x0) * ((tau - t0) / max(t1 - t0, 1e-9))
            by = m["y_lo"] + float(pong.fold(y0 - m["y_lo"]
                                             + slope * abs(bx - x0), span))
            return bx, by
    return None, None


# --------------------------------------------------------------------------
# The type.
# --------------------------------------------------------------------------

def test_type():
    # Measured, not assumed. A hardcoded 5x7 that drifted out of agreement
    # with the table would clip the bottom off every 8.
    heights = set(g.shape for g in pong._DIGIT_GRID.values())
    check("every digit is the same measured size", len(heights) == 1,
          "%dx%d" % (pong.DIGIT_H, pong.DIGIT_W))
    m = pong.digits_mask(12345, 3)
    check("digits_mask agrees with digits_width",
          m.shape == (pong.DIGIT_H * 3, pong.digits_width(5, 3)),
          "%s for 5 digits at scale 3" % (m.shape,))

    # All ten must be distinguishable, or the score is decoration. Compare
    # every pair; two identical grids is the classic copy-paste bug in a
    # hand-drawn font and it is invisible until somebody reads a 6 as an 8.
    keys = sorted(pong._DIGIT_GRID)
    same = [(a, b) for i, a in enumerate(keys) for b in keys[i + 1:]
            if np.array_equal(pong._DIGIT_GRID[a], pong._DIGIT_GRID[b])]
    check("all ten digits are distinct", not same, "%s" % (same,))

    # And the small face is defcon's, measured off its own table.
    check("small face measured from defcon", pong.SMALL_H == 5
          and pong._SMALL["E"].shape == (5, 3),
          "%dx%d" % (pong.SMALL_H, pong.SMALL_W))


def test_score_fits():
    _, m = built()
    W = 320
    for n in range(1, 10):
        s = m["score_scale"](n)
        w = pong.digits_width(n, s)
        left = m["net_x"] - m["net_gap"] - w
        right = m["net_x"] + 2 + m["net_gap"] + w
        check("%d digits fit either side of the net" % n,
              left >= 2 and right <= W - 2,
              "scale %d, %d px a side, left edge %d, right edge %d"
              % (s, w, left, right))


# --------------------------------------------------------------------------
# The book, and the closed form over it.
# --------------------------------------------------------------------------

def test_book():
    _, m = built()
    M, K = m["book"], m["left_per_book"]
    check("the edge is exact, not sampled",
          int(m["cum_left"][M]) == K and int(m["cum_right"][M]) == M - K,
          "%d LEFT / %d RIGHT per book of %d" % (K, M - K, M))
    check("the book is a sensible length",
          3.0 < m["mean_rally"] < 30.0 and 3600 < m["book_seconds"] < 200000,
          "mean rally %.2f s, book %.2f h"
          % (m["mean_rally"], m["book_seconds"] / 3600.0))
    check("every rally has a positive duration", m["duration"].min() > 1.0,
          "shortest %.2f s, longest %.2f s"
          % (m["duration"].min(), m["duration"].max()))

    # Rallies must actually vary, or the panel is a metronome.
    q = np.percentile(m["duration"], [5, 50, 95])
    check("rally lengths vary a lot", q[2] > q[0] * 2.0,
          "p05 %.1f  p50 %.1f  p95 %.1f s" % tuple(q))

    # The lookup against the honest cumulative sum it is standing in for.
    worst = 0
    for u in (0.0, 1.0, 12345.6, m["book_seconds"] * 0.5,
              m["book_seconds"] - 0.001, m["book_seconds"] * 3 + 777.0):
        n, j, tau, sl, sr = m["state_at"](u)
        block = int(u // m["book_seconds"])
        rem = u - block * m["book_seconds"]
        brute = int(np.searchsorted(np.cumsum(m["duration"]), rem,
                                    side="right"))
        worst = max(worst, abs(brute - j))
    check("closed-form rally index matches a brute cumsum", worst == 0,
          "worst slot error %d" % worst)


def test_score_grows_sanely():
    _, m = built()
    rows = []
    for days in (1, 7, 30, 365, 3650):
        _, _, _, sl, sr = m["state_at"](days * 86400.0)
        rows.append((days, sl, sr))
    ok = True
    for days, sl, sr in rows:
        lead = abs(sl - sr) / float(max(1, sl + sr))
        if not (0.0005 < lead < 0.05):
            ok = False
        if len("%d" % max(sl, sr)) > 9:
            ok = False
    check("the score never runs away or overflows", ok,
          "; ".join("d%d %d-%d" % r for r in rows))
    check("the same side wins the long game at every scale",
          all((r[2] - r[1]) > 0 for r in rows),
          "RIGHT ahead by %s" % [r[2] - r[1] for r in rows])

    # Monotonic. Sampled finely enough to catch a searchsorted off-by-one at a
    # rally boundary, which would make the score flicker backwards for a frame.
    prev = (-1, -1)
    bad = 0
    for u in np.arange(0.0, 40000.0, 0.37):
        _, _, _, sl, sr = m["state_at"](u)
        if sl < prev[0] or sr < prev[1]:
            bad += 1
        prev = (sl, sr)
    check("the score is monotonic", bad == 0, "%d regressions" % bad)

    # Every rally must move exactly one of them by exactly one.
    steps = set()
    for u in np.arange(0.0, 3000.0, 0.05):
        steps.add(m["state_at"](u)[3:])
    seq = sorted(steps)
    deltas = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(seq, seq[1:])]
    check("each point moves exactly one score by one",
          all(d in ((1, 0), (0, 1)) for d in deltas),
          "%d transitions, kinds %s" % (len(deltas), sorted(set(deltas))))


def test_clock_not_history():
    """The promise: reboot, redeploy, and the number is the same."""
    a = pong.build(opts(epoch=EPOCH))
    b = pong.build(opts(epoch=EPOCH - 613.5))
    check("same absolute instant, two builds, same pixels",
          np.array_equal(a(0.0, 0).copy(), b(613.5, 0)))
    c = pong.build(opts(epoch=EPOCH - 86400.0))
    check("and a day of 'uptime' apart too",
          np.array_equal(a(0.0, 0).copy(), c(86400.0, 0)))


# --------------------------------------------------------------------------
# The play. This is the check the whole file exists for.
# --------------------------------------------------------------------------

def test_every_contact():
    r, m = built()
    pad_half = m["pad_h"] * 0.5
    span = m["y_hi"] - m["y_lo"]
    bad_cover = bad_miss = stretched = 0
    gaps = []
    offs, slopes = [], []
    for j in range(m["book"]):
        rec = m["make_rally"](j)
        stretched += rec["stretched"]
        R, s0 = rec["returns"], rec["serve_to"]
        for k, (t0, t1, x0, x1, y0, slope) in enumerate(rec["segs"]):
            recv = (s0 + k) & 1
            dx_hit = abs(m["planes"][recv] - x0)
            arrive = m["y_lo"] + float(pong.fold(y0 - m["y_lo"]
                                                 + slope * dx_hit, span))
            kk = rec["keys"][recv]
            t_hit = t0 + (t1 - t0) * (dx_hit / abs(x1 - x0))
            py = float(np.interp(t_hit, kk[:, 0], kk[:, 1]))
            gap = abs(py - arrive)
            if k == R:
                gaps.append(gap)
                if gap <= pad_half:
                    bad_miss += 1
            else:
                if gap > pad_half:
                    bad_cover += 1
                offs.append((arrive - py) / pad_half)
                slopes.append(rec["segs"][k + 1][5])

    check("no rally ends on a shot the book did not award", bad_cover == 0,
          "%d uncovered non-deciding contacts" % bad_cover)
    check("the deciding shot always beats the paddle it was aimed past",
          bad_miss == 0, "%d deciding contacts covered" % bad_miss)
    g = np.array(gaps)
    check("and beats it visibly, not by a pixel", g.min() > 4.0,
          "closest miss %.1f px, mean %.1f px" % (g.min(), g.mean()))
    check("the reach fudge is rare", stretched < 0.01 * len(offs),
          "%d of %d contacts" % (stretched, len(offs)))

    # The 1972 rule, still doing most of the work: hit above the paddle's
    # middle and the ball leaves upward, and the further out the steeper.
    o, s = np.array(offs), np.array(slopes)
    corr = float(np.corrcoef(o, s)[0, 1])
    check("the angle still comes from where it hit", corr > 0.55,
          "corr(hit offset, outgoing slope) = %.3f over %d returns"
          % (corr, len(o)))
    steep = np.abs(s)[np.abs(o) > 0.7].mean()
    flat = np.abs(s)[np.abs(o) < 0.2].mean()
    check("tip shots leave steeper than middle shots", steep > flat * 1.3,
          "|slope| %.3f on the tip vs %.3f in the middle" % (steep, flat))


def test_characters():
    """The two must be visibly different players, not one player twice."""
    r, m = built()
    ys = [[], []]
    for i in range(20 * 900):
        tau_u = (m["epoch"] + i / 20.0) - m["since"]
        _, j, tau, _, _ = m["state_at"](tau_u)
        rec = m["make_rally"](j)
        for side in (0, 1):
            kk = rec["keys"][side]
            ys[side].append(float(np.interp(tau, kk[:, 0], kk[:, 1])))
    left, right = np.array(ys[0]), np.array(ys[1])
    v_left = np.abs(np.diff(left)).mean() * 20.0
    v_right = np.abs(np.diff(right)).mean() * 20.0
    check("LEFT is the busier paddle", v_left > v_right * 1.6,
          "%.1f vs %.1f px/s of travel" % (v_left, v_right))

    mid = (m["p_lo"] + m["p_hi"]) * 0.5
    still_r = float((np.abs(right - mid) < 1.0).mean())
    still_l = float((np.abs(left - mid) < 1.0).mean())
    check("RIGHT parks in the middle and LEFT does not",
          still_r > 0.25 and still_l < still_r * 0.5,
          "at centre %.0f%% vs %.0f%% of the time"
          % (still_r * 100, still_l * 100))

    for name, v in (("LEFT", left), ("RIGHT", right)):
        check("%s stays inside the court" % name,
              v.min() >= m["p_lo"] - 1e-6 and v.max() <= m["p_hi"] + 1e-6,
              "%.1f..%.1f in %.1f..%.1f"
              % (v.min(), v.max(), m["p_lo"], m["p_hi"]))


def test_ball_stays_in():
    r, m = built()
    lo = hi = None
    for i in range(20 * 900):
        u = (m["epoch"] + i / 20.0) - m["since"]
        _, j, tau, _, _ = m["state_at"](u)
        rec = m["make_rally"](j)
        if tau < m["serve_pause"] or tau >= rec["play_end"]:
            continue
        _, by = ball_at(m, rec, tau)
        if by is None:
            continue
        lo = by if lo is None else min(lo, by)
        hi = by if hi is None else max(hi, by)
    check("the ball never leaves the walls",
          lo >= m["y_lo"] - 1e-6 and hi <= m["y_hi"] + 1e-6,
          "%.2f..%.2f in %.2f..%.2f" % (lo, hi, m["y_lo"], m["y_hi"]))

    # And in pixels: nothing white ever lands on the boundary lines.
    onwall = 0
    for i in range(20 * 200):
        f = r(i / 20.0, i)
        if f[0].max() > 200 or f[63].max() > 200:
            onwall += 1
    check("nothing is ever drawn over the boundary lines", onwall == 0,
          "%d frames" % onwall)


# --------------------------------------------------------------------------
# What the panel actually says.
# --------------------------------------------------------------------------

def _quiet_moment(m):
    """A time during a serve pause with the ball blinked off.

    Only the furniture is on screen then, so the score can be read back off
    the pixels without the ball sitting in the middle of a digit.
    """
    for i in range(20 * 400):
        t = i / 20.0
        _, j, tau, sl, sr = m["state_at"]((m["epoch"] + t) - m["since"])
        if 0.30 <= tau < m["serve_pause"] and (tau % 0.42) >= 0.30:
            return t, sl, sr
    raise AssertionError("no quiet moment found")


def test_score_on_the_panel():
    r, m = built()
    t, sl, sr = _quiet_moment(m)
    f = r(t, 0)
    scale = m["score_scale"](max(len("%d" % sl), len("%d" % sr)))
    for value, side in ((sl, "left"), (sr, "right")):
        mask = pong.digits_mask(value, scale)
        w = mask.shape[1]
        x = (m["net_x"] - m["net_gap"] - w if side == "left"
             else m["net_x"] + 2 + m["net_gap"])
        got = f[m["score_y"]:m["score_y"] + mask.shape[0], x:x + w].max(2) > 90
        check("the %s score on the panel is the closed-form number" % side,
              np.array_equal(got, mask), "%d" % value)


def test_celebration():
    r, m = built()
    # Find a point and watch it land.
    for i in range(20 * 400):
        t = i / 20.0
        _, j, tau, _, _ = m["state_at"]((m["epoch"] + t) - m["since"])
        rec = m["make_rally"](j)
        if tau >= rec["play_end"]:
            break
    loser = rec["loser"]
    lit = 0
    for k in range(int(m["celebrate"] * 20)):
        f = r(t + k / 20.0, 0)
        edge = f[1:63, 0:2] if loser == pong.LEFT else f[1:63, 318:320]
        if int(edge.min()) == 255:
            lit += 1
    check("the beaten end of the court lights up", lit >= 2,
          "%d of %d celebration frames" % (lit, int(m["celebrate"] * 20)))

    other = r(t + 0.05, 0)
    far = other[1:63, 318:320] if loser == pong.LEFT else other[1:63, 0:2]
    check("and only that end", int(far.max()) < 200, "%d" % int(far.max()))

    # It must be over before the next serve, or the panel flashes at a rally
    # that has not happened yet.
    after = r(t + m["celebrate"] + 0.6, 0)
    check("the celebration ends", int(after[1:63, 0:2].max()) < 200
          and int(after[1:63, 318:320].max()) < 200)


# --------------------------------------------------------------------------
# The contract.
# --------------------------------------------------------------------------

def test_purity():
    a = pong.build(opts())
    b = pong.build(opts())
    t0 = 9.65
    for i in range(int(t0 * 20) + 1):
        driven = a(i / 20.0, i)
    cold = b(t0, int(t0 * 20))
    check("render is pure in t for one build", np.array_equal(driven, cold),
          "max delta %d"
          % int(np.abs(driven.astype(int) - cold.astype(int)).max()))

    # Two unpinned builds a moment apart differ, because the match is anchored
    # to the clock. That is the documented exception, not a bug.
    live_a = pong.build(ds.options(pong))
    time.sleep(0.35)
    live_b = pong.build(ds.options(pong))
    # Several instants, not one: a serve pause with the ball blinked off is a
    # genuinely static frame, and comparing a single t there would fail about
    # one run in seven for no reason at all.
    differs = sum(0 if np.array_equal(live_a(t, 0).copy(), live_b(t, 0)) else 1
                  for t in (0.0, 1.3, 2.7, 4.1))
    check("unpinned builds follow the wall clock", differs >= 3,
          "%d of 4 sampled instants differ" % differs)


def test_seed_and_flags():
    a, ma = built()
    b, mb = built(seed=7)
    check("--seed changes the book",
          not np.array_equal(ma["returns"], mb["returns"]))
    check("--seed does not change the edge",
          ma["left_per_book"] == mb["left_per_book"],
          "%d both" % ma["left_per_book"])

    _, mc = built(edge=0.75)
    check("--edge sets the long-run winner exactly",
          mc["left_per_book"] == int(round(mc["book"] * 0.75)),
          "%d of %d" % (mc["left_per_book"], mc["book"]))

    off = pong.build(opts(no_readout=True))
    on = pong.build(opts())
    row = 64 - 1 - pong.SMALL_H - 1
    check("--no-readout really drops it",
          int(off(0.4, 0)[row:row + pong.SMALL_H, 20:120].max()) == 0
          and int(on(0.4, 0)[row:row + pong.SMALL_H, 20:120].max()) > 0)


def test_sizes():
    bad = []
    for w, h in ((320, 64), (256, 64), (128, 32), (320, 96)):
        try:
            r = pong.build(opts(width=w, height=h))
            f = r(3.0, 60)
            if f.shape != (h, w, 3) or f.dtype != np.uint8:
                bad.append("%dx%d shape %s" % (w, h, f.shape))
        except Exception as exc:                              # noqa: BLE001
            bad.append("%dx%d %s" % (w, h, exc))
    check("renders at other panel sizes", not bad, "; ".join(bad))


def test_full_loop():
    r, m = built()
    n = 20 * 300
    bad = 0
    for i in range(n):
        f = r(i / 20.0, i)
        if f.shape != (64, 320, 3) or f.dtype != np.uint8:
            bad += 1
    check("five minutes renders clean", bad == 0, "%d frames" % n)


def test_no_network():
    src = open(os.path.join(HERE, "pong.py")).read()
    bad = [mod for mod in ("urllib", "http.client", "socket", "requests",
                           "ssl", "subprocess") if ("import " + mod) in src]
    check("pong.py imports no network module", not bad, ",".join(bad))
    check("pong.py reads no data product", "ftdata" not in src)

    # numpy 1.19 is what the wall has. These are the calls that are easy to
    # reach for by habit and are simply not there.
    modern = [c for c in ("default_rng", "np.random.Generator",
                          "take_along_axis", "removeprefix", "removesuffix")
              if c in src]
    check("no post-1.19 numpy or 3.9+ string calls", not modern,
          ",".join(modern))


def bench():
    r, m = built()
    for i in range(40):
        r(i / 20.0, i)
    n = 20 * 600
    times = np.empty(n)
    for i in range(n):
        t0 = time.perf_counter()
        r(i / 20.0, i)
        times[i] = (time.perf_counter() - t0) * 1000.0
    print("  frame ms over %d frames (600 s of match): mean %.3f  p95 %.3f  "
          "p99 %.3f  max %.3f"
          % (n, times.mean(), np.percentile(times, 95),
             np.percentile(times, 99), times.max()))
    print("  the max is the once-per-rally composite rebuild: %d rallies in "
          "the window" % int(600.0 / m["mean_rally"]))
    t0 = time.perf_counter()
    pong.build(opts(seed=99))
    print("  build(): %.1f ms" % ((time.perf_counter() - t0) * 1000.0))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true", help="time a full loop")
    a = ap.parse_args()

    test_type()
    test_score_fits()
    test_book()
    test_score_grows_sanely()
    test_clock_not_history()
    test_every_contact()
    test_characters()
    test_ball_stays_in()
    test_score_on_the_panel()
    test_celebration()
    test_purity()
    test_seed_and_flags()
    test_sizes()
    test_full_loop()
    test_no_network()
    if a.bench:
        bench()

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
