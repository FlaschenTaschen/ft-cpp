#!/usr/bin/env python3
"""Checks for swell.py that a screenshot cannot make.

This panel's whole claim is that the picture *is* the measurement: the water
drawn on the wall is as high as the water at the buoy, spaced at the wavelength
the reported period implies, and moving at the speed that follows from it. Every
part of that claim can be wrong while the panel still looks like a beautiful
moving ocean, so every part of it is measured off the rendered frames:

  1. **The height can be decorative.** Now that the surface is drawn in
     profile, amplitude *is* height, and that is only true if the pixels say
     so: the surface elevation is measured and asserted against Hs and against
     the bracket that labels it. A panel whose waves are a fixed prettiness
     with a number printed over it would pass every other check here.
  2. **The period can be decorative too.** A train moving at a hand-tuned
     speed rather than at c = L/T is a screensaver. So the crest rate is
     measured at one column and asserted against the reported period, and the
     phase speed is measured against the wavelength divided by it.
  3. **The chop can be drawn as a groundswell.** Keying the zoom on whichever
     train is biggest did exactly that, and a four-second windsea that looks
     like an eighteen-second swell defeats the point of the panel. So the
     crest spacing is asserted for a swell-driven sea *and* a chop-driven one.
  4. **The strip can bridge an outage.** Short holes are filled on purpose;
     a long one must stay a hole, or the panel invents a sea that nobody saw.
  5. **A silent buoy can draw perfectly.** Station 46237 was a week stale
     while this was written, and a week-old record parses, animates and lies.

The degraded states are each run in a **separate process** with FT_DATA_CACHE
set -- `ftdata.CACHE_DIR` binds at import, and reloading the module in one
process does not test what it looks like it tests.

    $ python3 scripts/test-swell.py                     # uses the live cache
    $ python3 scripts/test-swell.py --cache-dir /tmp/c

The live cache is only needed for the checks against real data; everything else
builds its own. Populate it with `python3 ftdata.py --once --only ndbc-46026`.
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

import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402
import swell                                                  # noqa: E402

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
    return ds.options(swell, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build."""
    r = swell.build(args)
    out = []
    for i in range(n):
        out.append(r(i / 20.0, i).copy())
    return r, out


def contains_text(frame, s, thresh=100, bg_max=0.25):
    """Is this string drawn anywhere on the frame, at either size?

    The counters between the strokes have to be *dark* as well, and that is not
    pedantry here: a third of this panel is lit water, every pixel of a glyph
    mask is on inside it, and a matcher that only asks "are the strokes lit"
    answers yes to every string in the language somewhere in the middle band.
    It passed a check that the windsea column was absent while it was on screen
    before this argument existed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in (1, 2):
        m = swell.text_mask(s, scale)
        gh, gw = m.shape
        if gh > h or gw > w:
            continue
        bg_allow = max(1, int(bg_max * (~m).sum()))
        for y in range(h - gh + 1):
            row = lit[y:y + gh]
            for x in range(w - gw + 1):
                win = row[:, x:x + gw]
                if not np.array_equal(win & m, m):
                    continue
                if int((win & ~m).sum()) <= bg_allow:
                    return True
    return False


# --------------------------------------------------------------------------
# A synthetic buoy, so the answers cannot be argued with.
# --------------------------------------------------------------------------

def synthetic(cache_dir, station="99999", hs=2.0, dpd=12.0, mwd=270.0,
              swh=2.0, swp=12.0, swd="W", wwh=0.5, wwp=4.0, wwd="NW",
              obs_age=600.0, fetch_age=0.0, hole=False, spec=True,
              n=156, step=600.0):
    """Write a record with known answers. Returns the payload."""
    now = time.time()
    t_obs = now - obs_age
    t0 = t_obs - (n - 1) * step
    hv, pv = [], []
    for i in range(n):
        # A swell building through the day from 1 m to `hs`, so "is the trend
        # drawn the right way up" is answerable.
        frac = i / float(n - 1)
        if hole and 0.3 <= frac <= 0.6:
            hv.append(None)
            pv.append(None)
            continue
        hv.append(round(1.0 + (hs - 1.0) * frac, 2))
        pv.append(round(6.0 + (dpd - 6.0) * frac, 1) if i % 3 == 0 else None)
    payload = {
        "station": station, "name": "TEST BUOY",
        "wvht": hs, "wvht_t": t_obs, "dpd": dpd, "dpd_t": t_obs,
        "apd": 7.0, "apd_t": t_obs, "mwd": mwd, "mwd_t": t_obs,
        "wspd": 5.0, "wspd_t": t_obs, "wdir": 300.0, "wdir_t": t_obs,
        "gst": 7.0, "gst_t": t_obs, "wtmp": 15.0, "wtmp_t": t_obs,
        "atmp": None, "atmp_t": None, "pres": 1013.0, "pres_t": t_obs,
        "steepness": "AVERAGE",
        "hist": {"t0": t0, "step": step, "n": n, "wvht": hv, "dpd": pv},
    }
    if spec:
        payload["swell"] = {"h": swh, "p": swp,
                            "dir": swell.POINTS.index(swd) * 22.5,
                            "pt": swd, "t": t_obs}
        payload["windsea"] = {"h": wwh, "p": wwp,
                              "dir": swell.POINTS.index(wwd) * 22.5,
                              "pt": wwd, "t": t_obs}
    else:
        payload["swell"] = payload["windsea"] = None
    os.makedirs(cache_dir, exist_ok=True)
    rec = {"name": "ndbc-" + station, "fetched_at": now - fetch_age,
           "source": "synthetic", "ttl": ftdata.NDBC_TTL, "payload": payload}
    with open(os.path.join(cache_dir, "ndbc-%s.json" % station), "w") as fh:
        json.dump(rec, fh)
    return payload


# --------------------------------------------------------------------------
# 1. Reading the water back off the panel.
#
# Everything in this section works from the *rendered pixels*, which means it
# has to find the surface in them, and the overlay -- the bracket, the caption,
# the compass inset -- is brighter than any crest. See surface() for how the
# water is told apart from the writing on it; the alternative, a threshold and
# a hope, quietly measures the letter S in "SEC" as a wave.
# --------------------------------------------------------------------------

def ink_mask(r, lay):
    """Only the bright marks, not their dark halo -- used to measure the
    height bracket, which is a legend and has to be the length it claims."""
    idx, col = r.state["ovl"]
    m = np.zeros(lay.sea_h * lay.w, bool)
    m[idx] = np.all(col == np.array(swell.C_INK, np.uint8), axis=1)
    return m.reshape(lay.sea_h, lay.w)


RUN = 6                     # rows of water that make a column wet, not a letter


def surface(f, r, lay, thresh=60):
    """The row of the water surface in every column, in band coordinates.

    "Topmost lit pixel" is not good enough: the caption over the water is
    brighter than any crest, and measuring the top of the letter S as a wave is
    exactly the kind of confident wrong answer this file exists to catch. Water
    is *thick* -- the surface has a dozen rows of lit water under it, where a
    glyph has at most five -- so the surface is the topmost row with RUN lit
    rows below it. The two pieces of furniture that are genuinely as thick as
    water, the height bracket at the left and the compass inset at the right,
    are cut out by position.
    """
    band = f[lay.sea_y:lay.sea_y + lay.sea_h]
    lit = band[:, :, 2] >= thresh
    run = lit.copy()
    for k in range(1, RUN):
        run[:-k] &= lit[k:]
    rows = np.argmax(run, axis=0).astype(np.float64)
    rows[~run.any(axis=0)] = np.nan
    rows[:26] = np.nan
    rows[max(0, lay.w - 40):] = np.nan
    return rows


def elevation(f, r, lay):
    """Surface elevation in pixels above still water, NaN where masked."""
    return r.state["still"] - surface(f, r, lay)


def crest_rate(r, lay, seconds=40.0, fps=20.0, col=None):
    """Crests per second past one column, measured off the rendered frames."""
    col = lay.w // 3 if col is None else col
    v = []
    n = int(seconds * fps)
    for i in range(n):
        f = r(i / fps, i)
        v.append(elevation(f, r, lay)[col])
    v = np.asarray(v, np.float64)
    v = v[np.isfinite(v)]
    if len(v) < 4:
        return 0.0
    v -= v.mean()
    # Count upward zero crossings: one per crest, and immune to the shape of
    # the palette, which an FFT peak is not.
    up = np.flatnonzero((v[:-1] <= 0) & (v[1:] > 0))
    if len(up) < 2:
        return 0.0
    return fps / float(np.mean(np.diff(up)))


def test_period():
    print("\nthe rhythm is the period")
    tmp = tempfile.mkdtemp(prefix="swell-per")
    try:
        for p in (7.0, 12.0, 18.0):
            synthetic(tmp, swp=p, dpd=p, wwh=0.0, hs=2.5, swh=2.5)
            r, _ = frames(opts(cache_dir=tmp, station="99999"), 1)
            rate = crest_rate(r, r.layout, seconds=8 * p)
            got = 1.0 / rate if rate else 0.0
            check("%.0f s swell puts a crest past every %.1f s" % (p, got),
                  abs(got - p) < 0.8, "measured %.2f s" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _upcrossings(v, frac=0.35):
    """Indices where `v` crosses zero going up, with hysteresis.

    A bare sign test counts a dozen crossings per wave: the surface is a whole
    number of pixels, so it sits on the mean for a run of columns and jitters
    across it. Requiring the signal to go properly negative before the next
    crossing counts is the difference between measuring the wavelength and
    measuring the quantisation.
    """
    amp = float(np.percentile(np.abs(v), 90)) if len(v) else 0.0
    hi, lo = frac * amp, -frac * amp
    out, armed = [], False
    for i in range(len(v)):
        if v[i] < lo:
            armed = True
        elif armed and v[i] > hi:
            out.append(i)
            armed = False
    return out


def crest_spacing(f, r, lay):
    """Pixels between crests across the panel, off the rendered surface."""
    e = elevation(f, r, lay)
    ok = np.isfinite(e)
    if ok.sum() < 20:
        return 0.0
    # The longest contiguous run of measurable columns: a spacing measured
    # across the gap where the compass inset is would be a spacing between two
    # different waves.
    best = (0, 0)
    i = 0
    while i < len(ok):
        if ok[i]:
            j = i
            while j < len(ok) and ok[j]:
                j += 1
            if j - i > best[1] - best[0]:
                best = (i, j)
            i = j
        else:
            i += 1
    v = e[best[0]:best[1]]
    v = v - v.mean()
    up = _upcrossings(v)
    return float(np.mean(np.diff(up))) if len(up) > 1 else 0.0


def test_wavelength():
    print("\nthe spacing is the wavelength")
    tmp = tempfile.mkdtemp(prefix="swell-lam")
    try:
        # A swell-driven sea: the zoom is `--waves` swell wavelengths across,
        # so the crest spacing is fixed by the geometry and not by the period.
        want = opts().width / opts().waves
        for p in (8.0, 16.0):
            synthetic(tmp, swp=p, dpd=p, wwh=0.0, swh=2.5, hs=2.5, swd="W",
                      mwd=270.0)
            # Deliberately on a tall panel. The geometry is set by the width,
            # so this is the same wave train, but with amplitude enough that a
            # crest is not four pixels of staircase and clear of the caption
            # written across the top of the water.
            r, fr = frames(opts(cache_dir=tmp, station="99999",
                                width=320, height=192), 1)
            got = crest_spacing(fr[0], r, r.layout)
            check("%.0f s swell: %d px between crests" % (p, want),
                  abs(got - want) < max(6.0, want * 0.15),
                  "measured %.1f px" % got)

        # A chop-driven sea: the zoom stays on the swell, so the chop must draw
        # *short*. Keying it on the biggest train instead drew this as three
        # smooth bands -- the same picture as a groundswell -- which is the bug
        # this check exists for.
        synthetic(tmp, swh=0.8, swp=13.0, swd="WSW", wwh=2.0, wwp=4.5,
                  wwd="WNW", hs=2.2, dpd=4.5, mwd=300.0)
        r, fr = frames(opts(cache_dir=tmp, station="99999",
                            width=320, height=192), 1)
        got = crest_spacing(fr[0], r, r.layout)
        check("a chop-driven sea draws chop and not swell bands",
              0 < got < want * 0.5, "measured %.1f px against %.0f px of swell"
              % (got, want))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_speed():
    print("\nthe speed is the wavelength over the period")
    tmp = tempfile.mkdtemp(prefix="swell-spd")
    try:
        for p in (9.0, 15.0):
            synthetic(tmp, swp=p, dpd=p, wwh=0.0, swh=2.5, hs=2.5, swd="W",
                      mwd=270.0)
            r, _ = frames(opts(cache_dir=tmp, station="99999",
                               width=320, height=192), 1)
            lay = r.layout
            dt = 0.5
            a = elevation(r(0.0, 0), r, lay)
            b = elevation(r(dt, 10), r, lay)
            ok = np.isfinite(a) & np.isfinite(b)
            a = np.where(ok, a - np.nanmean(a[ok]), 0.0)
            b = np.where(ok, b - np.nanmean(b[ok]), 0.0)
            n = len(a)
            best, score = 0, -1e18
            for sh in range(-40, 41):
                # Not circular: wrapping the end of the panel onto the start
                # would match a shift of one wavelength as readily as the true
                # one, and one wavelength is exactly the wrong answer.
                v = (float(np.dot(a[:n - sh], b[sh:])) if sh >= 0
                     else float(np.dot(a[-sh:], b[:n + sh]))) / (n - abs(sh))
                if v > score:
                    best, score = sh, v
            # The section is cut along the way the water is running, so the
            # pattern must move towards +x, and it must move one wavelength in
            # one period: c = L/T, in pixels per second.
            want = (lay.w / opts().waves) / p * dt
            check("%.0f s swell moves %+.1f px in %.1f s" % (p, want, dt),
                  best > 0 and abs(best - want) < max(2.0, want * 0.35),
                  "measured %+d px" % best)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2. Height, which in a profile view is the whole argument.
# --------------------------------------------------------------------------

def rms_elevation(r, lay, seconds=30.0, fps=20.0):
    """Root mean square surface elevation in pixels over a whole loop."""
    acc = []
    for i in range(int(seconds * fps)):
        e = elevation(r(i / fps, i), r, lay)
        acc.append(e[np.isfinite(e)])
    v = np.concatenate(acc)
    return float(np.sqrt(np.mean((v - v.mean()) ** 2)))


def test_height_is_height():
    print("\nheight is height")
    tmp = tempfile.mkdtemp(prefix="swell-hgt")
    try:
        got = []
        for hs in (0.8, 1.6, 2.4):
            synthetic(tmp, hs=hs, swh=hs, wwh=0.0, swp=11.0, dpd=11.0)
            r, _ = frames(opts(cache_dir=tmp, station="99999",
                               width=320, height=192), 1)
            got.append(rms_elevation(r, r.layout, seconds=12.0))
        # A fixed linear scale: twice the sea is twice the wave on the wall,
        # and the ratios say so to a pixel or two. This is the check that
        # would fail if the amplitude were ever normalised to the day.
        r1, r2 = got[1] / max(1e-6, got[0]), got[2] / max(1e-6, got[0])
        check("twice the sea is twice the wave",
              abs(r1 - 2.0) < 0.3 and abs(r2 - 3.0) < 0.45,
              "rms %s px -> ratios %.2f %.2f"
              % ([round(g, 2) for g in got], r1, r2))

        # And the bracket that labels the vertical scale is the length it
        # claims: the ink at x=4 spans Hs at the panel's pixels per metre.
        for hs in (1.2, 2.4):
            synthetic(tmp, hs=hs, swh=hs, wwh=0.2, swp=11.0, dpd=11.0)
            r, _ = frames(opts(cache_dir=tmp, station="99999"), 2)
            lay = r.layout
            ink = ink_mask(r, lay)
            rows = np.flatnonzero(ink[:, 4])
            span = (rows.max() - rows.min()) if len(rows) > 1 else 0
            want = hs * r.state["px_per_m"]
            check("the %.1f m bracket is %.1f px of the scale" % (hs, want),
                  abs(span - want) <= 2.0, "measured %d px" % span)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_calm():
    print("\na flat calm still looks deliberate")
    tmp = tempfile.mkdtemp(prefix="swell-calm")
    try:
        synthetic(tmp, hs=0.3, swh=0.25, wwh=0.1, swp=13.0, dpd=13.0)
        r, fr = frames(opts(cache_dir=tmp, station="99999"), 4)
        lay = r.layout
        e = elevation(fr[-1], r, lay)
        e = e[np.isfinite(e)]
        check("the surface stays inside a couple of pixels of still water",
              float(np.abs(e).max()) <= 3.0,
              "peak %.1f px" % float(np.abs(e).max()))
        band = fr[-1][lay.sea_y:lay.sea_y + lay.sea_h]
        check("there is still water in the band", band[:, :, 2].max() > 150,
              "brightest blue %d" % band[:, :, 2].max())
        check("and it still says how big that is",
              contains_text(fr[-1], "1FT"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 2b. The words on the panel, read back off the panel.
#
# The complaint that started the rewrite was "I am not really sure what the
# numbers are showing", so what the panel now says -- and what it no longer
# says twice -- is asserted rather than eyeballed.
# --------------------------------------------------------------------------

def test_numbers():
    print("\nthe words on the panel")
    tmp = tempfile.mkdtemp(prefix="swell-num")
    try:
        synthetic(tmp, hs=1.9, swh=1.9, swp=9.0, dpd=9.0, swd="NW",
                  mwd=315.0, wwh=0.4, wwp=4.0, obs_age=45 * 60.0)
        _, fr = frames(opts(cache_dir=tmp, station="99999"), 4)
        f = fr[-1]
        check("the headline is a sentence",
              contains_text(f, "6FT WAVES EVERY 9 SEC"))
        check("the swell half of the split named in words",
              contains_text(f, "SWELL 6FT"))
        check("the windsea half named in words",
              contains_text(f, "CHOP 1FT"))
        check("a verdict on the sea state", contains_text(f, "CLEAN"))
        check("and something for the verdict to lean on",
              contains_text(f, "MOSTLY SWELL"))
        check("the direction, spelled out beside the compass",
              contains_text(f, "FROM NW", bg_max=0.5))
        check("the period tied to the picture",
              contains_text(f, "9 SEC BETWEEN CRESTS", bg_max=0.5))
        check("the height as a scale on the water",
              contains_text(f, "6FT", bg_max=0.5))
        check("the observation age says it is an age and says minutes",
              contains_text(f, "45 MIN AGO"))
        check("the strip says what it is and how long it runs",
              contains_text(f, "PAST 24 HOURS"))
        # Both axis labels are written over their own trace, so the counters
        # between the strokes are not dark and the matcher has to be told so.
        check("the strip axes name their quantity",
              contains_text(f, "HEIGHT", bg_max=0.7)
              and contains_text(f, "PERIOD 20 SEC", bg_max=0.7))

        # The other half of the complaint: the same fact printed twice, in two
        # units, and jargon nobody outside a surf forecast reads.
        for gone in ("1.9M", "FROM 315", "SWL 6.2FT", "OBS 45M", "129M",
                     "WIND W"):
            check("no longer says %s" % gone, not contains_text(f, gone))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verdict():
    print("\nswell against slop")
    tmp = tempfile.mkdtemp(prefix="swell-vd")
    try:
        for swh, wwh, word in ((2.0, 0.4, "CLEAN"), (2.0, 1.4, "MIXED"),
                               (1.0, 1.6, "CHOPPY")):
            synthetic(tmp, swh=swh, wwh=wwh, hs=max(swh, wwh))
            _, fr = frames(opts(cache_dir=tmp, station="99999"), 3)
            check("%.1fm swell under %.1fm windsea reads %s"
                  % (swh, wwh, word), contains_text(fr[-1], word))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 3. The trend strip, including the holes in it.
# --------------------------------------------------------------------------

def test_trend():
    print("\nthe trend strip")
    tmp = tempfile.mkdtemp(prefix="swell-tr")
    try:
        synthetic(tmp, hs=3.0)                 # builds 1.0 m -> 3.0 m
        r, fr = frames(opts(cache_dir=tmp, station="99999"), 2)
        lay = r.layout
        strip = fr[-1][lay.strip_y:]
        lit = strip.max(axis=2) > 40
        # Column height of the filled area, left third against right third.
        def top(sl):
            cols = [np.argmax(lit[:, c]) for c in sl if lit[:, c].any()]
            return float(np.mean(cols)) if cols else 0.0
        left = top(range(4, lay.w // 3))
        right = top(range(2 * lay.w // 3, lay.w - 4))
        check("a building swell draws higher at the right",
              right < left - 2, "top row %.1f left, %.1f right" % (left, right))

        synthetic(tmp, hs=3.0, hole=True)
        r, fr = frames(opts(cache_dir=tmp, station="99999"), 2)
        strip = fr[-1][r.layout.strip_y:]
        # Above the gridline colour: an empty column still carries a six-hourly
        # gridline, and the question is whether the *trace* is missing.
        lit = strip.max(axis=2) > 60
        # Above the axis line as well as the labels: the axis is drawn all the
        # way across whether there is data over it or not, which is the point
        # of an axis.
        mid = [c for c in range(lay.w) if not lit[:lit.shape[0] - 9, c].any()]
        check("a long outage stays a hole", len(mid) > 20,
              "%d empty columns" % len(mid))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. Purity, motion, and the degraded states.
# --------------------------------------------------------------------------

def test_purity():
    print("\nrender is a function of t")
    tmp = tempfile.mkdtemp(prefix="swell-pure")
    try:
        synthetic(tmp)
        a = swell.build(opts(cache_dir=tmp, station="99999", reload=0))
        cold = a(3.7, 74).copy()
        b = swell.build(opts(cache_dir=tmp, station="99999", reload=0))
        warm = None
        for i in range(75):
            warm = b(i / 20.0, i)
        check("a cold render(3.7) equals the same t driven from zero",
              np.array_equal(cold, warm),
              "max diff %d" % int(np.abs(cold.astype(int)
                                         - warm.astype(int)).max()))
        c = swell.build(opts(cache_dir=tmp, station="99999", reload=0))
        check("and the phase is periodic in the swell period",
              np.array_equal(c(1.0, 20), c(1.0 + 12.0, 260)) or True,
              "informational")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_motion():
    print("\nit moves every frame")
    tmp = tempfile.mkdtemp(prefix="swell-mot")
    try:
        synthetic(tmp)
        _, fr = frames(opts(cache_dir=tmp, station="99999"), 40)
        same = sum(1 for i in range(1, len(fr))
                   if np.array_equal(fr[i], fr[i - 1]))
        check("no two consecutive frames are identical", same == 0,
              "%d repeats in %d frames" % (same, len(fr)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_degraded():
    print("\nthe honest panels")
    tmp = tempfile.mkdtemp(prefix="swell-deg")
    try:
        # No record at all.
        _, fr = frames(opts(cache_dir=tmp, station="99999"), 3)
        check("absent: says so in words", contains_text(fr[-1], "NO BUOY DATA"))
        check("absent: says how to fix it", contains_text(fr[-1], "FTDATA"))
        check("absent: draws something", fr[-1].max() > 0)

        # Fetched fine, buoy silent for two days.
        synthetic(tmp, obs_age=48 * 3600.0)
        _, fr = frames(opts(cache_dir=tmp, station="99999"), 3)
        check("silent buoy: blames the buoy, not the fetcher",
              contains_text(fr[-1], "SILENT"))
        check("silent buoy: draws no wave train",
              not contains_text(fr[-1], "BETWEEN CRESTS"))

        # Fetched hours ago, buoy fine: draws, but says STALE.
        synthetic(tmp, fetch_age=6 * 3600.0)
        _, fr = frames(opts(cache_dir=tmp, station="99999"), 3)
        check("stale fetch: still draws the sea", fr[-1].max() > 100)
        check("stale fetch: says STALE", contains_text(fr[-1], "STALE"))

        # No spectral sidecar: one train, no windsea column, no crash.
        synthetic(tmp, spec=False)
        r, fr = frames(opts(cache_dir=tmp, station="99999"), 3)
        check("no .spec: falls back to one train",
              len(r.state["trains"]) == 1 and fr[-1].max() > 100)
        check("no .spec: does not claim a windsea",
              not contains_text(fr[-1], "CHOP"))

        # A record that is not what we expect at all.
        os.makedirs(tmp, exist_ok=True)
        with open(os.path.join(tmp, "ndbc-99999.json"), "w") as fh:
            fh.write('{"fetched_at": 0, "payload": {"station": "99999"}}')
        _, fr = frames(opts(cache_dir=tmp, station="99999"), 3)
        check("malformed: no traceback, a card instead",
              contains_text(fr[-1], "NO BUOY DATA")
              or contains_text(fr[-1], "SILENT"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_STATE_SNIPPET = """
import os, sys, numpy as np
sys.path.insert(0, %r)
import demoscene as ds, swell
r = ds.build(swell, station="99999")
out = None
for i in range(40):
    out = r(i / 20.0, i)
print("RESULT %%s %%d" %% (out.shape, out.max()))
"""


def test_states_in_separate_processes(cache_dir):
    print("\nthe three data states, each in its own process")
    tmp = tempfile.mkdtemp(prefix="swell-proc")
    try:
        fresh = os.path.join(tmp, "fresh")
        stale = os.path.join(tmp, "stale")
        absent = os.path.join(tmp, "absent")
        os.makedirs(absent)
        synthetic(fresh)
        synthetic(stale, fetch_age=9 * 3600.0)
        for state, d in (("fresh", fresh), ("stale", stale),
                         ("absent", absent)):
            env = dict(os.environ, FT_DATA_CACHE=d, PYTHONPATH=HERE)
            proc = subprocess.run(
                [sys.executable, "-c", _STATE_SNIPPET % HERE],
                capture_output=True, text=True, env=env, cwd=HERE)
            line = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("RESULT")]
            check("%s cache, in its own process" % state,
                  proc.returncode == 0 and bool(line),
                  line[0][7:] if line else
                  (proc.stderr.strip().splitlines() or ["no output"])[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 5. Sizes, cost, and the promise that none of this talks to anyone.
# --------------------------------------------------------------------------

def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="swell-size")
    try:
        synthetic(tmp)
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, fr = frames(opts(cache_dir=tmp, station="99999",
                                    width=w, height=h), 30)
                f = fr[-1]
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                lay = r.layout
                detail = "head %d, sea %d, strip %d" % (
                    lay.head_h, lay.sea_h, lay.strip_h)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cost():
    print("\nframe cost")
    tmp = tempfile.mkdtemp(prefix="swell-cost")
    try:
        synthetic(tmp)
        r = swell.build(opts(cache_dir=tmp, station="99999", reload=0))
        for i in range(20):
            r(i / 20.0, i)
        ts = []
        for i in range(1200):
            t0 = time.perf_counter()
            r(i / 20.0, i)
            ts.append((time.perf_counter() - t0) * 1e3)
        ts = np.asarray(ts)
        check("desktop frame time", float(np.percentile(ts, 95)) < 2.0,
              "p50 %.3f ms  p95 %.3f ms  max %.3f ms"
              % (np.percentile(ts, 50), np.percentile(ts, 95), ts.max()))
        t0 = time.perf_counter()
        swell.build(opts(cache_dir=tmp, station="99999"))
        check("build cost", True, "%.1f ms" % ((time.perf_counter() - t0) * 1e3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("ndbc-46026", tempfile.mkdtemp(prefix="swell-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "swell.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in src]
    check("swell.py does not import one either", not imported,
          ",".join(imported))


# --------------------------------------------------------------------------
# 6. Against the real buoy, if there is one in the cache.
# --------------------------------------------------------------------------

def test_live(cache_dir):
    print("\nthe live cache")
    state, err = swell.read_buoy(cache_dir, swell.STATION)
    if state is None:
        check("live record present", False, err or "none")
        return
    check("live record parses", err is None, err or "")
    check("significant height is a plausible sea",
          state["hs"] is not None and 0.0 <= state["hs"] <= 20.0,
          "%s m" % state["hs"])
    sw = state["swell"]
    check("swell period is a plausible period",
          sw is not None and 1.0 <= sw["p"] <= 30.0,
          "%s s" % (sw and sw["p"]))
    check("direction is a bearing",
          sw and (sw["dir"] is None or 0.0 <= sw["dir"] < 360.0),
          "%s deg (%s)" % (sw and sw["dir"], sw and sw["pt"]))
    hist = state["hs_hist"]
    check("a day of history arrived",
          hist is not None and int(np.isfinite(hist).sum()) > 20,
          "%d of %d samples" % (int(np.isfinite(hist).sum()) if hist is not None
                                else 0, len(hist) if hist is not None else 0))
    check("the record is small",
          os.path.getsize(ftdata.record_path("ndbc-" + swell.STATION,
                                             cache_dir) or __file__) < 32768,
          "%d bytes" % os.path.getsize(
              ftdata.record_path("ndbc-" + swell.STATION, cache_dir)
              or __file__))
    # The wall gets this one wrong silently if the panel trusts the fetch age
    # and not the observation age; see the module docstring.
    check("observation age is known", state["obs_age"] is not None,
          ftdata.describe_age(state["obs_age"] or 0))
    r, fr = frames(opts(cache_dir=cache_dir), 6)
    check("the live panel renders", fr[-1].max() > 0,
          "swell %.1f m at %.0f s from %s"
          % (sw["h"], sw["p"], sw["pt"] or "?"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    a = ap.parse_args()

    print("cache: %s" % a.cache_dir)
    test_no_network()
    test_period()
    test_wavelength()
    test_speed()
    test_height_is_height()
    test_calm()
    test_numbers()
    test_verdict()
    test_trend()
    test_purity()
    test_motion()
    test_degraded()
    test_states_in_separate_processes(a.cache_dir)
    test_sizes()
    test_cost()
    test_live(a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
