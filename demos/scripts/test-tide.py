#!/usr/bin/env python3
"""Checks for tide.py that a screenshot cannot make.

This demo has a failure mode the rest of the demos do not: it can render a
perfectly good-looking panel that is confidently wrong about the tide, and
somebody might plan a swim around it. Looking at it does not catch that. So the
phase is asserted against the fetched NOAA JSON at real timestamps, and the
direction of the flow field is asserted against `meanFloodDir` rather than
against how the arrows look, because an inverted flow field looks completely
plausible and is completely wrong.

    $ python3 scripts/test-tide.py                     # uses the live cache
    $ python3 scripts/test-tide.py --cache-dir /tmp/c  # or a pointed one

Needs a populated cache; run `python3 ftdata.py --once` first. The no-data,
stale and partial-data cases build their own cache directories and need
nothing.
"""

import argparse
import copy
import json
import math
import os
import shutil
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                       # noqa: E402
import ftdata                                                # noqa: E402
import tide                                                  # noqa: E402

FAILED = []
PASSED = [0]


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-56s %s" % (name, detail))
    else:
        print("  FAIL %-56s %s" % (name, detail))
        FAILED.append(name)


def frames(args, n=8):
    r = tide.build(args)
    out = None
    for i in range(n):
        out = r(i / 30.0, i)
    return r, out.copy()


def opts(**kw):
    return ds.options(tide, **kw)


def contains_text(frame, s, thresh=90, scales=(1, 2)):
    """Is this string drawn anywhere on the frame, at any position or size?

    Renders the same glyph mask the demo uses and slides it over the lit
    pixels. Reading the words back off the panel is the only way to be sure the
    honest message actually reached it rather than merely being computed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = tide.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                if np.array_equal(row[:, x:x + gw] & m, m):
                    return True
    return False


# --------------------------------------------------------------------------
# 1. Phase. Asserted against the fetched JSON, not against the picture.
# --------------------------------------------------------------------------

def test_phase(cache_dir, station, cstation):
    print("\nphase against the source data")
    tide_rec, _, err = tide.read_tide(cache_dir, station)
    cur_rec, _, cerr = tide.read_current(cache_dir, cstation)
    if tide_rec is None or cur_rec is None:
        check("cache is populated", False, err or cerr)
        return
    curve, extremes = tide_rec["curve"], tide_rec["extremes"]
    vel, events = cur_rec["vel"], cur_rec["events"]

    # The six-minute curve and the separately fetched hi/lo list are two
    # different products off the same harmonic fit. If our sampler is reading
    # the curve correctly they must agree at every extreme, and the curve must
    # actually turn over there.
    worst_h, worst_slope, n = 0.0, 0, 0
    for t, v, kind in extremes:
        if not curve.covers(t - 5400, t + 5400):
            continue
        n += 1
        worst_h = max(worst_h, abs(float(curve.at(t)) - v))
        before = float(curve.at(t) - curve.at(t - 3600))
        after = float(curve.at(t + 3600) - curve.at(t))
        want_up = kind.upper().startswith("H")
        if not ((before > 0) == want_up and (after < 0) == want_up):
            worst_slope += 1
    check("curve agrees with the hi/lo list at every extreme",
          n >= 4 and worst_h < 0.25, "%d extremes, worst %.3f ft" % (n, worst_h))
    check("curve turns over at every extreme", worst_slope == 0,
          "%d of %d wrong" % (worst_slope, n))

    # Same test for the current: the labelled events and the sampled velocity
    # series have to be the same physical thing.
    bad_slack, bad_peak, ns, npk = 0, 0, 0, 0
    for t, kind, v in events:
        if not vel.covers(t - 5400, t + 5400):
            continue
        got = float(vel.at(t))
        if kind == "slack":
            ns += 1
            bad_slack += abs(got) > 0.35
        elif kind in ("flood", "ebb"):
            npk += 1
            want = 1 if kind == "flood" else -1
            # Sign right, and a local extremum of the series.
            if (got > 0) != (want > 0) or abs(got) < abs(v) * 0.6:
                bad_peak += 1
    check("velocity series is near zero at every labelled slack",
          ns >= 4 and bad_slack == 0, "%d slacks, %d bad" % (ns, bad_slack))
    check("velocity series peaks and signs match the labelled max flood/ebb",
          npk >= 4 and bad_peak == 0, "%d peaks, %d bad" % (npk, bad_peak))

    # Halfway between a slack and the next max flood the water must be
    # flooding, and not yet at full strength. This is the assertion that
    # catches a sign flip or an off-by-one in the interpolation.
    mids, bad = 0, 0
    for i in range(len(events) - 1):
        t0, k0, _ = events[i]
        t1, k1, v1 = events[i + 1]
        if k0 != "slack" or k1 not in ("flood", "ebb"):
            continue
        tm = 0.5 * (t0 + t1)
        if not vel.covers(tm - 60, tm + 60):
            continue
        mids += 1
        got = float(vel.at(tm))
        want_pos = k1 == "flood"
        if (got > 0) != want_pos or not (0.15 < abs(got) < abs(v1)):
            bad += 1
    check("midway from slack to peak reads the right way and part strength",
          mids >= 3 and bad == 0, "%d midpoints, %d bad" % (mids, bad))

    # And the same thing through the demo's own state, at a real timestamp,
    # since that is the path the panel actually takes.
    highs = [e for e in extremes if e[2].upper().startswith("H")]
    if highs:
        t, v, _ = highs[len(highs) // 2]
        r, _ = frames(opts(cache_dir=cache_dir, at=str(t)))
        st = r.state
        h = float(st["tide"]["curve"].at(t))
        left, mid, _, _ = tide.header_text(
            {"tide": st["tide"], "current": st["current"], "h": h,
             "dhdt": float(st["tide"]["curve"].at(t + 900)
                           - st["tide"]["curve"].at(t - 900)),
             "v": float(st["current"]["vel"].at(t)),
             "next_slack": None, "stale": False}, False, False, 320)
        check("panel height at a predicted high water matches the prediction",
              abs(h - v) < 0.25, "%s  (predicted %.2f ft)" % (left, v))
        # High water at the gauge is not slack water at the Gate -- the Bay is
        # not a standing wave and the current runs on for the best part of an
        # hour. So the assertion is that the *nearest slack* is close, not that
        # the current is zero.
        slacks = [e[0] for e in events if e[1] == "slack"]
        lag = min(abs(s - t) for s in slacks) / 60.0 if slacks else 1e9
        check("nearest slack is within 90 min of high water", lag < 90.0,
              "%.0f min after high water, %s" % (lag, mid))


# --------------------------------------------------------------------------
# 2. Which way the water goes. The one that looks fine when it is inverted.
# --------------------------------------------------------------------------

def test_sense(cache_dir, station, cstation):
    print("\nflood/ebb sense against meanFloodDir")
    cur, _, err = tide.read_current(cache_dir, cstation)
    if cur is None:
        check("current record present", False, err)
        return
    flood_dir, ebb_dir = cur["flood_dir"], cur["ebb_dir"]

    r, _ = frames(opts(cache_dir=cache_dir))
    u_m, v_m, sea, bearing, (sr, sc) = r.flow
    check("field has a bearing at the current station", bearing is not None,
          "%.1f deg" % bearing if bearing is not None else "none")
    if bearing is None:
        return

    d = tide.angle_diff(bearing, flood_dir)
    check("positive velocity points along meanFloodDir, not against it",
          d < 90.0, "field %.0f deg vs meanFloodDir %.0f (%.0f apart)"
          % (bearing, flood_dir, d))
    check("field bearing is within 45 deg of meanFloodDir", d < 45.0,
          "%.0f deg apart" % d)
    check("the ebb sense is the opposite one",
          tide.angle_diff((bearing + 180.0) % 360.0, ebb_dir) < 45.0,
          "reversed field %.0f vs meanEbbDir %.0f"
          % ((bearing + 180.0) % 360.0, ebb_dir))

    # Now the same question of the drawn panel, at real flood and ebb moments:
    # do the particles actually move that way? The field could be right and the
    # sign applied to it backwards.
    events = cur["events"]
    for kind, want in (("flood", flood_dir), ("ebb", ebb_dir)):
        peaks = [e for e in events if e[1] == kind]
        if not peaks:
            continue
        t = peaks[len(peaks) // 2][0]
        args = opts(cache_dir=cache_dir, at=str(t), particles=600)
        rr = tide.build(args)
        lay = rr.layout
        # Watch the drifters themselves rather than the lit pixels: the median
        # displacement is immune to the handful that respawn somewhere else
        # mid-measurement, which a centroid is not.
        pos = rr.particles
        rr(0.0, 0)
        start = pos.copy()
        for i in range(1, 25):
            rr(i / 30.0, i)
        dy = float(np.median(pos[0] - start[0]))
        dx = float(np.median(pos[1] - start[1]))
        # Back out of display pixels into metres, undoing the horizontal
        # stretch, before asking for a compass bearing.
        wm, hm = tide.extent_metres(tide.parse_extent(args.extent))
        east = dx * wm / args.width
        north = -dy * hm / lay.map_rows
        got = math.degrees(math.atan2(east, north)) % 360.0
        d = tide.angle_diff(got, want)
        check("drifters run %s within 60 deg of mean%sDir"
              % (kind, kind.capitalize()), d < 60.0,
              "drift %.0f deg vs %.0f (%.0f apart) at %s"
              % (got, want, d, time.strftime("%F %H:%M", time.localtime(t))))


# --------------------------------------------------------------------------
# 3. Nothing, something old, and half of it.
# --------------------------------------------------------------------------

def _write(cache_dir, name, payload, fetched_at):
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, name + ".json"), "w") as fh:
        json.dump({"name": name, "fetched_at": fetched_at,
                   "source": "test", "ttl": 172800, "payload": payload}, fh)


def test_degraded(cache_dir, station, cstation):
    print("\nmissing, stale and partial data")
    tname, cname = "tide-" + station, "currents-" + cstation
    live_t = ftdata.load(tname, cache_dir)
    live_c = ftdata.load(cname, cache_dir)
    if live_t is None or live_c is None:
        check("live records available to derive the cases from", False)
        return
    tmp = tempfile.mkdtemp(prefix="tide-test-")
    try:
        # (a) empty cache
        d = os.path.join(tmp, "empty")
        os.makedirs(d)
        r, f = frames(opts(cache_dir=d))
        check("empty cache renders the no-data panel",
              contains_text(f, "NO TIDE DATA"), "and nothing else claimed")
        check("empty cache names the fix", contains_text(f, "FTDATA"))
        check("empty cache draws no curve", r.state["tide"] is None)

        # (b) ancient: fetched long ago *and* predicting a week that is over
        d = os.path.join(tmp, "ancient")
        old_t = copy.deepcopy(live_t[0])
        old_c = copy.deepcopy(live_c[0])
        shift = 30 * 86400.0
        old_t["curve"]["t0"] -= shift
        old_c["velocity"]["t0"] -= shift
        _write(d, tname, old_t, time.time() - shift)
        _write(d, cname, old_c, time.time() - shift)
        r, f = frames(opts(cache_dir=d))
        check("a month-old prediction is refused, not drawn",
              r.state["tide"] is None and contains_text(f, "NO TIDE DATA"),
              "; ".join(r.state["problems"])[:60])

        # (c) old fetch, span still covering now. A prediction does not rot on
        # the same clock an observation does, so this one must still draw --
        # but it must say so.
        d = os.path.join(tmp, "stale")
        _write(d, tname, copy.deepcopy(live_t[0]), time.time() - 3 * 86400.0)
        _write(d, cname, copy.deepcopy(live_c[0]), time.time() - 3 * 86400.0)
        r, f = frames(opts(cache_dir=d))
        drew = r.state["tide"] is not None
        check("a three-day-old prediction whose span still covers now is drawn",
              drew)
        check("...and is flagged stale on the panel",
              contains_text(f, "STALE") and contains_text(f, "DATA 3D"))

        # (d) partial: the tide arrived, the current did not
        d = os.path.join(tmp, "partial")
        _write(d, tname, copy.deepcopy(live_t[0]), time.time())
        r, f = frames(opts(cache_dir=d))
        check("tide alone still draws the curve", r.state["tide"] is not None)
        check("...and says the current is missing",
              contains_text(f, "NO CURRENT DATA"))

        # (e) fields knocked out of an otherwise valid record
        d = os.path.join(tmp, "holed")
        broken_t = copy.deepcopy(live_t[0])
        del broken_t["curve"]["v"]
        broken_c = copy.deepcopy(live_c[0])
        del broken_c["flood_dir"]
        _write(d, tname, broken_t, time.time())
        _write(d, cname, broken_c, time.time())
        r, f = frames(opts(cache_dir=d))
        check("a record with fields missing renders the no-data panel",
              contains_text(f, "NO TIDE DATA"),
              "; ".join(r.state["problems"])[:60])

        # (f) a current station that is not on this map. The curve can follow
        # any gauge; the flow field cannot, and scaling this bay's channels by
        # another bay's prediction would look entirely convincing.
        d = os.path.join(tmp, "offmap")
        far = copy.deepcopy(live_c[0])
        far["lat"], far["lon"] = 42.33778, -70.95555     # Boston Harbor
        far["station"] = "BOS1111"
        _write(d, tname, copy.deepcopy(live_t[0]), time.time())
        _write(d, "currents-BOS1111", far, time.time())
        r, f = frames(opts(cache_dir=d, current_station="BOS1111"))
        check("a current station off the map draws the curve but no flow",
              r.state["tide"] is not None and not r.state["on_map"])
        check("...and says which station it refused to place",
              contains_text(f, "OFF THIS MAP"))

        # (g) not JSON at all
        d = os.path.join(tmp, "garbage")
        os.makedirs(d)
        with open(os.path.join(d, tname + ".json"), "w") as fh:
            fh.write("{not json at all")
        r, f = frames(opts(cache_dir=d))
        check("a corrupt file renders the no-data panel",
              contains_text(f, "NO TIDE DATA"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. Geometry at other panel sizes.
# --------------------------------------------------------------------------

def test_sizes(cache_dir):
    print("\npanel sizes")
    for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                 (512, 128), (64, 32)):
        try:
            r, f = frames(opts(cache_dir=cache_dir, width=w, height=h), n=4)
            ok = f.shape == (h, w, 3) and f.dtype == np.uint8 and f.max() > 0
            check("%dx%d renders" % (w, h), ok,
                  "map %d rows, curve %d" % (r.layout.map_rows, r.layout.curve_h))
        except Exception as e:                               # noqa: BLE001
            check("%dx%d renders" % (w, h), False, repr(e))


def test_window(cache_dir):
    print("\ntime axis")
    r, _ = frames(opts(cache_dir=cache_dir))
    now = time.time()
    t0, t1 = tide.curve_window(now, 30.0, "day")
    check("day-anchored window contains now", t0 <= now <= t1,
          time.strftime("%F %H:%M", time.localtime(t0)))
    cols = set()
    for k in range(0, 24):
        t = now + k * 3600.0
        a, b = tide.curve_window(t, 30.0, "day")
        cols.add(int((t - a) / (b - a) * 320))
    check("the marker traverses the panel over a day", len(cols) >= 18,
          "%d distinct columns in 24 hourly samples" % len(cols))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--tide-station", default=tide.TIDE_STATION)
    ap.add_argument("--current-station", default=tide.CURRENT_STATION)
    a = ap.parse_args()
    print("cache: %s" % a.cache_dir)
    test_phase(a.cache_dir, a.tide_station, a.current_station)
    test_sense(a.cache_dir, a.tide_station, a.current_station)
    test_degraded(a.cache_dir, a.tide_station, a.current_station)
    test_sizes(a.cache_dir)
    test_window(a.cache_dir)
    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
