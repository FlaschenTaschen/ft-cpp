#!/usr/bin/env python3
"""Checks for sfmix.py that a screenshot cannot make.

A weathermap is the easiest kind of panel to get confidently, invisibly wrong,
because every wrong version of it is a perfectly attractive picture of coloured
lines over a bay. The specific ways this one can lie:

  1. **The colour can be off the ramp.** A trunk at 24% of capacity and one at
     2.5% have to be different colours, in the right order, at the right
     saturation. Swap the ramp end for end and the panel says the busy trunk is
     the idle one, and looks exactly as good.
  2. **The two directions can be the same strand.** The whole reason a link is
     drawn twice is that in and out differ; colouring both halves from
     `max(in, out)` would draw the same pretty double line and quietly assert
     something false about every asymmetric trunk on the map.
  3. **The comets can run the wrong way.** Flow direction is the one thing on
     the panel with no textual backup at all.
  4. **A planned link can be drawn as a healthy one.** Zero traffic on an unlit
     fibre is blue on any ramp that starts at zero, and blue means fine.
  5. **The projection can be mirrored.** A 45 degree rotation has four
     plausible-looking variants and three of them put San Jose where San
     Francisco is. The bay looks like a bay in all four.

So the drawing is asserted **in pixels** against a synthetic exchange whose
answers cannot be argued with -- known utilisations, a known asymmetry, a known
planned link -- and the arithmetic is asserted against the fetched JSON
separately.

Two things about how these are run, both learned in this tree. `render` here
*is* a pure function of `t` (see test_purity), which is unusual for a data
panel and is checked rather than assumed. And `ftdata.CACHE_DIR` binds at
import, so the three data states a demo must handle -- fresh, stale, absent --
are each run in a **separate process** with FT_DATA_CACHE set, at the bottom of
this file. Reloading the module in one process does not test what it looks like
it tests.

    $ python3 scripts/test-sfmix.py                     # uses the live cache
    $ python3 scripts/test-sfmix.py --cache-dir /tmp/c  # or a pointed one

The live cache is only needed for the checks against real data; everything else
builds its own cache directory and needs nothing. Populate it with
`python3 ftdata.py --once --only sfmix-ix`.
"""

import argparse
import json
import math
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
import ftdata                                                  # noqa: E402
import sfmix                                                   # noqa: E402

FAILED = []
PASSED = [0]


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    kw.setdefault("reload", 0.0)
    return ds.options(sfmix, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build."""
    r = sfmix.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def contains_text(frame, s, thresh=90, scales=(1, 2), bg_max=0.25):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure the honest
    message reached it rather than merely being computed. The counters between
    the strokes have to be dark too: this panel has solid blocks of lit colour
    on it (the legend ramp, the chart fill), every pixel of a glyph mask is lit
    inside one, and a matcher that only asks "are the strokes on" answers yes
    to every string in the language somewhere inside the ramp.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = sfmix.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                win = row[:, x:x + gw]
                if not np.array_equal(win & m, m):
                    continue
                if (win & ~m).mean() <= bg_max:
                    return True
    return False


def count_colour(frame, rgb, tol=2):
    """How many pixels are (near enough) exactly this colour."""
    d = np.abs(frame.astype(int) - np.array(rgb, int))
    return int((d.max(axis=2) <= tol).sum())


# --------------------------------------------------------------------------
# A synthetic exchange. Two metros in a straight line and one trunk between
# them, so every pixel of the answer can be worked out by hand.
# --------------------------------------------------------------------------

def synthetic(cache_dir, trunks=None, fetched_ago=60.0, points=97,
              peak_mbps=400000, now_mbps=300000):
    os.makedirs(cache_dir, exist_ok=True)
    if trunks is None:
        trunks = [dict(a="North", z="South", cap_mbps=400000,
                       out_mbps=96000, in_mbps=24000,        # 24% and 6%
                       util_pct=24.0, status="up", members=1, reporting=1)]
    # Two metros a long way apart on the panel's own long axis. The rotation is
    # 45 degrees, so a due-southeast pair lands horizontally.
    metros = {"North": {"lat": 37.80, "lon": -122.45, "code": "NOR", "sites": 2},
              "South": {"lat": 37.30, "lon": -121.95, "code": "SOU", "sites": 1}}
    for t in trunks:
        if "path" not in t:
            a, z = metros[t["a"]], metros[t["z"]]
            t["path"] = [[a["lon"], a["lat"]], [z["lon"], z["lat"]]]

    now = time.time()
    step = 900
    t0 = now - step * (points - 1)
    stamps = [int(t0 + i * step) for i in range(points)]
    # A single clean bump so the peak is unambiguous and its index is known.
    curve = [int(now_mbps + (peak_mbps - now_mbps)
                 * math.exp(-((i - points // 3) / 6.0) ** 2))
             for i in range(points)]
    peak_i = int(np.argmax(curve))
    payload = {"generation": "g-test", "generated_at": "test",
               "metros": metros, "trunks": trunks,
               "backbone_links": len(trunks), "sites": 3,
               "total": {"now_mbps": curve[-1], "now_at": stamps[-1],
                         "peak_mbps": curve[peak_i], "peak_at": stamps[peak_i],
                         "step_s": step, "t": stamps, "mbps": curve},
               "note": "synthetic"}
    rec = {"name": sfmix.PRODUCT, "fetched_at": now - fetched_ago,
           "source": "synthetic", "ttl": ftdata.ttl_for(sfmix.PRODUCT),
           "payload": payload}
    with open(os.path.join(cache_dir, sfmix.PRODUCT + ".json"), "w") as fh:
        json.dump(rec, fh)
    return payload


# --------------------------------------------------------------------------
# 1. The ramp, which is the whole claim the map makes.
# --------------------------------------------------------------------------

def test_ramp():
    print("\nthe utilisation ramp")
    stops = [(0.0, "blue"), (0.25, "green"), (0.5, "yellow"),
             (0.75, "orange"), (1.0, "red")]
    for x, name in stops:
        got = tuple(int(round(v)) for v in sfmix.ramp_colour(x))
        want = dict(sfmix.RAMP)[x]
        check("ramp at %.2f is the portal's %s" % (x, name), got == want,
              "%s vs %s" % (got, want))
    # The ramp must sweep hue one way, cool to warm, all the way along. This is
    # the invariant that catches a ramp assembled end for end, which no single
    # stop check does -- and it has to be hue and not "more red, less blue",
    # because Grafana's red (#F2495C) is *less* red and *more* blue than its
    # orange and a channel-wise test fails on the real ramp.
    def hue(rgb):
        r, g, b = [v / 255.0 for v in rgb]
        mx, mn = max(r, g, b), min(r, g, b)
        if mx == mn:
            return 0.0
        d = mx - mn
        if mx == r:
            h = ((g - b) / d) % 6
        elif mx == g:
            h = (b - r) / d + 2
        else:
            h = (r - g) / d + 4
        return h * 60.0

    xs = np.linspace(0, 1, 41)
    hues, last = [], None
    for x in xs:
        hv = hue(sfmix.ramp_colour(x))
        if last is not None:                # unwrap onto a continuous branch
            while hv - last > 180:
                hv -= 360
            while last - hv > 180:
                hv += 360
        hues.append(hv)
        last = hv
    check("the ramp sweeps hue one way, cool to warm",
          bool((np.diff(hues) <= 1e-6).all()),
          "%.0f deg to %.0f deg" % (hues[0], hues[-1]))
    check("beyond full scale clamps to red, it does not wrap",
          tuple(int(v) for v in sfmix.ramp_colour(4.0)) == sfmix.RAMP[-1][1])


def test_strand_colours():
    print("\ntwo strands, two directions, two colours")
    tmp = tempfile.mkdtemp(prefix="sfmix-strand")
    try:
        synthetic(tmp)
        r, f = frames(opts(cache_dir=tmp, flow=0.0), 4)
        rec = r.state["rec"]
        t = rec["trunks"][0]
        u_out = sfmix.strand_util(t, True)
        u_in = sfmix.strand_util(t, False)
        check("out is 24% of capacity", abs(u_out - 0.24) < 1e-6, "%.4f" % u_out)
        check("in is 6% of capacity", abs(u_in - 0.06) < 1e-6, "%.4f" % u_in)

        c_out = tuple(int(round(v)) for v in sfmix.ramp_colour(0.24 / 0.30))
        c_in = tuple(int(round(v)) for v in sfmix.ramp_colour(0.06 / 0.30))
        check("the two directions get different colours", c_out != c_in,
              "%s vs %s" % (c_out, c_in))
        n_out, n_in = count_colour(f, c_out), count_colour(f, c_in)
        check("the busier direction is on the panel", n_out > 20, "%d px" % n_out)
        check("the quieter direction is on the panel too", n_in > 20,
              "%d px" % n_in)
        check("neither direction was drawn with the other's colour",
              abs(n_out - n_in) < max(n_out, n_in) * 0.5,
              "%d vs %d px" % (n_out, n_in))

        # And the specific failure: colouring both halves from max(in, out).
        both_max = count_colour(f, c_out) and not count_colour(f, c_in)
        check("both halves were NOT coloured from max(in, out)", not both_max)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_planned_is_not_idle():
    print("\na planned link is not a healthy one")
    tmp = tempfile.mkdtemp(prefix="sfmix-planned")
    try:
        synthetic(tmp, trunks=[
            dict(a="North", z="South", cap_mbps=100000, out_mbps=0, in_mbps=0,
                 util_pct=0.0, status="planned", members=1, reporting=1)])
        r, f = frames(opts(cache_dir=tmp, flow=0.0), 4)
        n_plan = count_colour(f, sfmix.C_PLANNED)
        n_blue = count_colour(f, sfmix.RAMP[0][1])
        check("drawn in the planned colour", n_plan > 20, "%d px" % n_plan)
        # The legend ramp legitimately contains one column of ramp-zero blue,
        # so the test is "not a trunk's worth", not "none".
        check("not drawn in ramp-zero blue", n_blue < 12, "%d px" % n_blue)
        check("no light runs along it", r.state["n_flow"] == 0,
              "%d flow px" % r.state["n_flow"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_traffic_is_not_zero_traffic():
    print("\na trunk nothing was reported for is not a trunk at zero")
    tmp = tempfile.mkdtemp(prefix="sfmix-noreport")
    try:
        synthetic(tmp, trunks=[
            dict(a="North", z="South", cap_mbps=100000, out_mbps=None,
                 in_mbps=None, util_pct=0.0, status="up", members=2,
                 reporting=0)])
        r, f = frames(opts(cache_dir=tmp, flow=0.0), 4)
        check("drawn in the no-data grey",
              count_colour(f, sfmix.C_NOTRAFFIC) > 20,
              "%d px" % count_colour(f, sfmix.C_NOTRAFFIC))
        check("and carries no comets", r.state["n_flow"] == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2. The projection, which has four plausible variants and one right one.
# --------------------------------------------------------------------------

def test_projection():
    print("\nthe rotated projection")
    tmp = tempfile.mkdtemp(prefix="sfmix-proj")
    try:
        synthetic(tmp)
        r, _f = frames(opts(cache_dir=tmp, flow=0.0), 2)
        proj = r.state["proj"]
        # North west should be up and to the LEFT, and further north west
        # should be further left. This is the check that fails on all three
        # mirrored variants of the same rotation.
        nw = proj([(-122.45, 37.80)])[0]
        se = proj([(-121.95, 37.30)])[0]
        check("north west is left of south east", nw[0] < se[0],
              "%.1f < %.1f" % (nw[0], se[0]))
        check("north west is above south east", nw[1] < se[1],
              "%.1f < %.1f" % (nw[1], se[1]))
        dx, dy = proj.north()
        check("the north arrow points up", dy < -0.5, "dy %.2f" % dy)
        check("the north arrow points left", dx < -0.5, "dx %.2f" % dx)

        # Isometric: a kilometre east must be the same number of pixels as a
        # kilometre north. Anisotropic scaling would fill the box better and
        # would be a lie about distance.
        o = proj([(-122.20, 37.55)])[0]
        east = proj([(-122.20 + 0.01 / math.cos(math.radians(37.55)), 37.55)])[0]
        north = proj([(-122.20, 37.55 + 0.01)])[0]
        de = math.hypot(*(east - o))
        dn = math.hypot(*(north - o))
        check("the projection is isometric", abs(de - dn) / max(de, 1e-9) < 0.02,
              "%.3f vs %.3f px" % (de, dn))

        # And the inverse really is the inverse; the coastline sampling stands
        # on it and a sign error there draws a plausible bay in the wrong place.
        for lon, lat in ((-122.4, 37.75), (-121.9, 37.35)):
            p = proj([(lon, lat)])[0]
            blon, blat = proj.inverse(np.array([p[0]]), np.array([p[1]]))
            check("inverse round-trips %.2f,%.2f" % (lon, lat),
                  abs(blon[0] - lon) < 1e-6 and abs(blat[0] - lat) < 1e-6)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_coastline():
    print("\nthe baked coastline")
    got = sfmix.load_sea()
    check("sfmix-map.npz loads", got is not None)
    if got is None:
        return
    sea, bbox = got
    check("covers the south bay", bbox[1] <= 37.25, "lat0 %.2f" % bbox[1])
    check("covers San Francisco", bbox[3] >= 37.80, "lat1 %.2f" % bbox[3])
    frac = float(sea.mean())
    check("is roughly a third water", 0.2 < frac < 0.5, "%.3f" % frac)

    def at(lat, lon):
        rows, cols = sea.shape
        r = int((bbox[3] - lat) / (bbox[3] - bbox[1]) * rows)
        c = int((lon - bbox[0]) / (bbox[2] - bbox[0]) * cols)
        return bool(sea[r, c])

    for name, lat, lon, want in (
            ("mid-bay off Hayward", 37.60, -122.25, True),
            ("the Pacific off Pacifica", 37.55, -122.65, True),
            ("downtown San Jose", 37.335, -121.890, False),
            ("CoreSite SV4, Santa Clara", 37.3763, -121.9706, False),
            ("Mission Bay, San Francisco", 37.770, -122.390, False)):
        check("%s is %s" % (name, "water" if want else "land"),
              at(lat, lon) == want)


# --------------------------------------------------------------------------
# 3. Motion, purity and the frame budget.
# --------------------------------------------------------------------------

def test_purity():
    print("\nrender is a pure function of t")
    tmp = tempfile.mkdtemp(prefix="sfmix-pure")
    try:
        synthetic(tmp)
        # Cold: build and jump straight to t. Driven: build and step there one
        # frame at a time. A demo that accumulates anything between calls
        # differs, and would desync from the scheduler, which starts every
        # segment at t=0 after building it minutes earlier on another thread.
        cold = sfmix.build(opts(cache_dir=tmp))(4.35, 87)
        r = sfmix.build(opts(cache_dir=tmp))
        driven = None
        for i in range(88):
            driven = r(i / 20.0, i)
        check("cold render(4.35) == the same t driven from zero",
              np.array_equal(cold, driven),
              "%d px differ" % int((cold != driven).any(axis=2).sum()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_motion():
    print("\nsomething moves in every single frame")
    tmp = tempfile.mkdtemp(prefix="sfmix-motion")
    try:
        synthetic(tmp)
        r = sfmix.build(opts(cache_dir=tmp))
        prev, still = None, 0
        for i in range(120):
            f = r(i / 20.0, i)
            if prev is not None and np.array_equal(prev, f):
                still += 1
            prev = f.copy()
        check("no two consecutive frames are identical", still == 0,
              "%d still frames of 119" % still)

        # And the comets run the right way: light started at the a end of the
        # forward strand has to be further along it a moment later. Measured on
        # the comet's centre of mass along the trunk's own axis.
        r2 = sfmix.build(opts(cache_dir=tmp, flow=1.0))
        idx = r2.state
        n = idx["n_flow"]
        check("there are comets to check", n > 0, "%d px" % n)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_flow_direction():
    """The comets have to run the way the bits do, and faster when there are
    more of them. This is the only claim on the panel with no text backing it
    up, so it is measured rather than eyeballed.

    Measured on a synthetic trunk carrying traffic in ONE direction, which is
    what makes it unambiguous: the comet pixels are exactly the pixels where
    the rendered frame differs from the baked one, no colour discrimination is
    needed, and there is no second strand to be confused with. Position is the
    circular mean of those pixels modulo the comet period rather than a plain
    centroid -- a dozen comets are in flight at once and the leading one leaves
    the end while the next enters, so a plain centroid jitters backwards at
    random and a test built on it passes or fails by luck.
    """
    print("\nthe comets run the way the bits do")
    tmp = tempfile.mkdtemp(prefix="sfmix-flow")
    try:
        def one_way(name, out_mbps, in_mbps, dt=0.12):
            sub = os.path.join(tmp, name)
            synthetic(sub, trunks=[
                dict(a="North", z="South", cap_mbps=400000,
                     out_mbps=out_mbps, in_mbps=in_mbps,
                     util_pct=100.0 * max(out_mbps, in_mbps) / 400000.0,
                     status="up", members=1, reporting=1)])
            r = sfmix.build(opts(cache_dir=sub))
            proj, rec = r.state["proj"], r.state["rec"]
            a = proj([(rec["metros"]["North"]["lon"],
                       rec["metros"]["North"]["lat"])])[0]
            z = proj([(rec["metros"]["South"]["lon"],
                       rec["metros"]["South"]["lat"])])[0]
            axis = np.array([z[0] - a[0], z[1] - a[1]], float)
            axis /= np.hypot(axis[0], axis[1])
            mw = r.layout.map_w

            def phase(t):
                f = r(t, int(t * 20))[:, :mw].astype(float)
                base = r.static[:, :mw].astype(float)
                d = f - base
                m = np.abs(d).max(axis=2) > 8
                ys, xs = np.nonzero(m)
                if len(xs) < 6:
                    return None
                sv = (xs - a[0]) * axis[0] + (ys - a[1]) * axis[1]
                ang = 2 * math.pi * (sv % sfmix.FLOW_PERIOD) / sfmix.FLOW_PERIOD
                wgt = np.abs(d).max(axis=2)[m]
                zc = (wgt * np.exp(1j * ang)).sum()
                return float((np.angle(zc) % (2 * math.pi))
                             / (2 * math.pi) * sfmix.FLOW_PERIOD)

            p0, p1 = phase(0.0), phase(dt)
            if p0 is None or p1 is None:
                return None
            step = (p1 - p0) % sfmix.FLOW_PERIOD
            return step - sfmix.FLOW_PERIOD if step > sfmix.FLOW_PERIOD / 2 \
                else step

        fwd = one_way("fwd", 96000, 0)          # 24% a->z, nothing coming back
        rev = one_way("rev", 0, 96000)          # 24% z->a
        slow = one_way("slow", 24000, 0)        # 6% a->z
        check("a->z traffic sends its light towards z",
              fwd is not None and fwd > 0.3, "%s px in 0.12 s" % fwd)
        check("z->a traffic sends its light towards a",
              rev is not None and rev < -0.3, "%s px in 0.12 s" % rev)
        check("the two directions run against each other",
              fwd is not None and rev is not None and fwd * rev < 0,
              "%.2f vs %.2f px" % (fwd or 0, rev or 0))
        # Speed proportional to load. This is what catches a constant-speed
        # animation, which is indistinguishable from this one in a screenshot.
        check("a busier direction's light moves faster",
              fwd is not None and slow is not None and fwd > slow * 1.5,
              "24%%: %.2f px, 6%%: %.2f px" % (fwd or 0, slow or 0))

        # And a direction measured at zero carries no light at all.
        sub = os.path.join(tmp, "idle")
        synthetic(sub, trunks=[
            dict(a="North", z="South", cap_mbps=400000, out_mbps=0,
                 in_mbps=0, util_pct=0.0, status="up", members=1,
                 reporting=1)])
        r = sfmix.build(opts(cache_dir=sub))
        check("a trunk measured at zero carries no comets",
              r.state["n_flow"] == 0, "%d flow px" % r.state["n_flow"])
        check("but it is still drawn, at the bottom of the ramp",
              count_colour(r(0.0, 0), sfmix.RAMP[0][1]) > 40,
              "%d px" % count_colour(r(0.0, 0), sfmix.RAMP[0][1]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_budget():
    print("\nthe frame budget")
    tmp = tempfile.mkdtemp(prefix="sfmix-perf")
    try:
        synthetic(tmp)
        r = sfmix.build(opts(cache_dir=tmp))
        for i in range(50):
            r(i / 20.0, i)
        ts = []
        for i in range(600):
            t0 = time.perf_counter()
            r(i / 20.0, i)
            ts.append((time.perf_counter() - t0) * 1e3)
        ts = np.array(ts)
        p50, p95 = np.percentile(ts, 50), np.percentile(ts, 95)
        # Desktop timings lie by well over an order of magnitude, so this is a
        # regression tripwire and not a claim about the wall. A tenth of a
        # millisecond here is a couple of milliseconds there.
        check("p50 under 0.30 ms here", p50 < 0.30, "p50 %.3f p95 %.3f max %.3f"
              % (p50, p95, ts.max()))
        check("p95 is near p50, not a fat tail", p95 < max(4 * p50, 0.4),
              "%.3f vs %.3f" % (p95, p50))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. The aggregate, and the words on the panel.
# --------------------------------------------------------------------------

def test_numbers():
    print("\nthe numbers on the panel")
    check("300000 Mb/s reads as 300G", sfmix.bits(300000) == "300G",
          sfmix.bits(300000))
    check("1500 Mb/s reads as 2G at zero places", sfmix.bits(1500) == "2G",
          sfmix.bits(1500))
    check("870 Mb/s reads as 870M", sfmix.bits(870) == "870M", sfmix.bits(870))
    check("2.5e6 Mb/s reads in terabits, with a decimal",
          sfmix.bits(2500000) == "2.5T", sfmix.bits(2500000))
    check("missing reads as --", sfmix.bits(None) == "--")

    tmp = tempfile.mkdtemp(prefix="sfmix-num")
    try:
        p = synthetic(tmp, now_mbps=302000, peak_mbps=411000)
        _r, f = frames(opts(cache_dir=tmp), 8)
        want_now = sfmix.bits(p["total"]["now_mbps"])
        want_peak = sfmix.bits(p["total"]["peak_mbps"])
        check("the headline is the record's current total",
              contains_text(f, want_now), want_now)
        check("the peak is on the panel", contains_text(f, want_peak),
              want_peak)
        check("the peak's clock time is on the panel",
              contains_text(f, sfmix.hhmm(p["total"]["peak_at"])),
              sfmix.hhmm(p["total"]["peak_at"]))
        check("the unit is on the panel", contains_text(f, "BIT/S"))
        check("the legend says what the ramp measures",
              contains_text(f, "LINK LOAD"))
        check("the legend carries its top number",
              contains_text(f, "30%"))
        check("the time axis is labelled at both ends",
              contains_text(f, "-24H") and contains_text(f, "NOW"))
        check("both metro codes are on the map",
              contains_text(f, "NOR") and contains_text(f, "SOU"))
        check("a fresh record does not say STALE", not contains_text(f, "STALE"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_chart_zero_based():
    print("\nthe curve is not zoomed onto its own top")
    tmp = tempfile.mkdtemp(prefix="sfmix-chart")
    try:
        # A flat day with a 3% wobble. Zoomed onto its own range it looks like
        # an event; drawn from zero it looks like the flat day it is.
        synthetic(tmp, now_mbps=300000, peak_mbps=309000)
        r, f = frames(opts(cache_dir=tmp, flow=0.0), 4)
        lay = r.layout
        x0, cw = lay.pane_x, lay.pane_w - 2
        reg = f[lay.chart_y:lay.chart_y + lay.chart_h, x0:x0 + cw]
        lit = (reg.max(axis=2) > 30)
        heights = lit.sum(axis=0)
        # Every column is filled from the bottom to the curve, so a 3% wobble
        # must move the top by at most a row or two out of twenty-three.
        spread = int(heights.max() - heights.min())
        check("a 3% day is drawn as a flat day", spread <= 3,
              "%d rows of variation" % spread)
        check("the fill reaches the bottom of the chart",
              bool(lit[-1].all()), "%d/%d columns" % (int(lit[-1].sum()), cw))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_degraded():
    print("\nrecords that must not draw a map")
    tmp = tempfile.mkdtemp(prefix="sfmix-bad")
    try:
        for name, mangle in (
                ("empty file", lambda d: ""),
                ("not json", lambda d: "{{{"),
                ("no trunks", lambda d: _edit(d, trunks=[])),
                ("no metros", lambda d: _edit(d, metros={})),
                ("a trunk with no route", lambda d: _edit(
                    d, trunks=[dict(d["payload"]["trunks"][0], path=[])])),
                ("a curve that does not line up", lambda d: _edit(
                    d, total=dict(d["payload"]["total"], mbps=[1, 2, 3]))),
        ):
            sub = os.path.join(tmp, name.replace(" ", "-"))
            synthetic(sub)
            path = os.path.join(sub, sfmix.PRODUCT + ".json")
            with open(path) as fh:
                doc = json.load(fh)
            out = mangle(doc)
            with open(path, "w") as fh:
                fh.write(out if isinstance(out, str) else json.dumps(out))
            r, f = frames(opts(cache_dir=sub), 4)
            check("%s draws the no-data card" % name,
                  r.state["rec"] is None and contains_text(f, "NO SFMIX DATA"),
                  str(r.state["problem"])[:44])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _edit(doc, **kw):
    doc["payload"].update(kw)
    return doc


# --------------------------------------------------------------------------
# 5. Fresh, stale and absent, each in a process of its own.
#
# `ftdata.CACHE_DIR` is read at import time, so a test that sets FT_DATA_CACHE
# and reloads the module is testing the state of its own import machinery.
# --------------------------------------------------------------------------

def _one_state(state, cache_dir):
    args = opts()                       # note: no cache_dir, so CACHE_DIR wins
    r = sfmix.build(args)
    out = None
    for i in range(8):
        out = r(i / 20.0, i)
    card = contains_text(out, "NO SFMIX DATA")
    stale = contains_text(out, "STALE")
    drew = r.state["rec"] is not None
    verdict = {
        "fresh": (drew and not card and not stale, "drew a map, no flags"),
        "stale": (drew and not card and stale, "drew a map with STALE on it"),
        "absent": (not drew and card, "drew the no-data card"),
    }[state]
    print("RESULT %s %s cache=%s drew=%s card=%s stale=%s"
          % (state, "ok" if verdict[0] else "FAIL",
             os.path.basename(cache_dir), drew, card, stale))
    return 0 if verdict[0] else 1


def test_states_in_separate_processes():
    print("\nfresh, stale and absent -- one process each, FT_DATA_CACHE set")
    tmp = tempfile.mkdtemp(prefix="sfmix-proc")
    try:
        fresh = os.path.join(tmp, "fresh")
        synthetic(fresh, fetched_ago=120.0)
        stale = os.path.join(tmp, "stale")
        synthetic(stale, fetched_ago=4 * 3600.0)
        absent = os.path.join(tmp, "absent")
        os.makedirs(absent)

        for state, d in (("fresh", fresh), ("stale", stale),
                         ("absent", absent)):
            env = dict(os.environ, FT_DATA_CACHE=d, FT_DATA_BLOBS=d)
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--state", state, "--cache-dir", d],
                env=env, capture_output=True, text=True, timeout=180)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT")]
            check("%s cache, in its own process" % state,
                  proc.returncode == 0 and bool(line),
                  (line[0][7:] if line
                   else (proc.stderr.strip().splitlines() or ["no output"])[-1]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 6. Other panels, and the promise that none of this talks to anyone.
# --------------------------------------------------------------------------

def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="sfmix-size")
    try:
        synthetic(tmp)
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, f = frames(opts(cache_dir=tmp, width=w, height=h), 40)
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                lay = r.layout
                detail = "map %dx%d, pane %d" % (lay.map_w, lay.map_h,
                                                 lay.pane_w)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load(sfmix.PRODUCT, tempfile.mkdtemp(prefix="sfmix-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "sfmix.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in src]
    check("sfmix.py does not import one either", not imported,
          ",".join(imported))


# --------------------------------------------------------------------------
# 7. The live record, if there is one.
# --------------------------------------------------------------------------

def test_live(cache_dir):
    print("\nthe live cache")
    got = ftdata.load(sfmix.PRODUCT, cache_dir)
    if got is None:
        print("  --   no live record; run ftdata.py --once --only " +
              sfmix.PRODUCT)
        return
    payload, age = got
    rec, _age, problem = sfmix.read_ix(cache_dir)
    check("the live record parses", rec is not None, str(problem))
    if rec is None:
        return
    check("it carries a generation", bool(payload.get("generation")),
          str(payload.get("generation")))
    check("it is under 20 kB",
          len(json.dumps(payload)) < 20000,
          "%d bytes" % len(json.dumps(payload)))

    for t in rec["trunks"]:
        cap = t.get("cap_mbps") or 0
        if not cap or not t.get("reporting"):
            continue
        want = 100.0 * max(t["in_mbps"], t["out_mbps"]) / cap
        check("%s-%s util matches max(in,out)/cap"
              % (t["a"][:3], t["z"][:3]),
              abs(want - t["util_pct"]) < 0.05,
              "%.2f vs %.2f" % (want, t["util_pct"]))
        check("%s-%s is under capacity in both directions"
              % (t["a"][:3], t["z"][:3]),
              max(t["in_mbps"], t["out_mbps"]) <= cap * 1.02,
              "%d/%d Mb/s" % (max(t["in_mbps"], t["out_mbps"]), cap))

    total = rec["total"]
    check("the peak is at least the current value",
          total["peak_mbps"] >= total["now_mbps"],
          "%s vs %s" % (sfmix.bits(total["peak_mbps"]),
                        sfmix.bits(total["now_mbps"])))
    curve = [v for v in rec["curve"] if v is not None]
    check("the peak is at least the curve's own maximum",
          total["peak_mbps"] >= max(curve) * 0.999,
          "%s vs %s" % (sfmix.bits(total["peak_mbps"]),
                        sfmix.bits(max(curve))))
    check("the curve covers about a day",
          20 * 3600 <= rec["stamps"][-1] - rec["stamps"][0] <= 25 * 3600,
          "%.1f h" % ((rec["stamps"][-1] - rec["stamps"][0]) / 3600.0))
    check("the record is fresh enough to draw without apology",
          ftdata.is_fresh(sfmix.PRODUCT, age),
          "%s old" % ftdata.describe_age(age))

    r, f = frames(opts(cache_dir=cache_dir), 8)
    check("the live record renders", f.shape[2] == 3 and f.max() > 0)
    check("every metro's code is on the map",
          all(contains_text(f, m["code"]) for m in rec["metros"].values()),
          ",".join(m["code"] for m in rec["metros"].values()))
    lit = float((f.max(axis=2) > 24).mean())
    check("the panel is mostly dark, so it blends", lit < 0.45,
          "%.1f%% lit" % (lit * 100))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--state", default="",
                    choices=("", "fresh", "stale", "absent"),
                    help="internal: run one data state and print RESULT")
    a = ap.parse_args()
    if a.state:
        return _one_state(a.state, a.cache_dir)

    print("cache: %s" % a.cache_dir)
    test_no_network()
    test_ramp()
    test_strand_colours()
    test_planned_is_not_idle()
    test_no_traffic_is_not_zero_traffic()
    test_projection()
    test_coastline()
    test_purity()
    test_motion()
    test_flow_direction()
    test_budget()
    test_numbers()
    test_chart_zero_based()
    test_degraded()
    test_states_in_separate_processes()
    test_sizes()
    test_live(a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
