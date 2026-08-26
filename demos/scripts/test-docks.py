#!/usr/bin/env python3
"""Checks for docks.py that a screenshot cannot make.

This panel tells somebody to walk somewhere. It can be beautiful, confident and
wrong in at least six ways, and not one of them looks wrong:

  1. **The projection can be off.** A map with the wall in the middle and docks
     scattered plausibly around it looks identical whether the origin is right
     or 273 m out -- and 273 m out is exactly the error the other panels in this
     tree carry, so it is the mistake that was available. The nearest dock is
     Jackson Playground at 292 m; if it is not, the projection is wrong.
  2. **The ebikes can silently be zero.** `num_bikes_available_types` reads 0 at
     every station in this feed, so a panel built on it draws a city with no
     electric bikes, which is a third of the fleet and half the point.
  3. **The classic count can go negative,** because the electric count is a
     subset of the total and nothing in the feed promises it is a small one.
  4. **Up and down can be swapped.** Bikes drawn growing downward and free docks
     upward is exactly as pretty and tells a returning rider to go to an empty
     station.
  5. **A shut station can be counted.** `is_renting` 0 is a dock whose bikes are
     there and cannot be had.
  6. **Half-hour-old counts draw perfectly.** They parse, they are complete, and
     they are a specific claim about which dock has two bikes in it that has
     been false for twenty minutes.

So the drawing is asserted **in pixels** against a synthetic neighbourhood whose
answers are arithmetic, and the arithmetic is asserted against the record
separately.

Two things about how these are run, both learned the hard way in this tree.
`ftdata.CACHE_DIR` binds at import, so the three data states a demo must handle
-- fresh, stale, absent -- are each run in a **separate process** with
FT_DATA_CACHE set, at the bottom of this file. And this demo *is* a pure
function of `t` with `--reload 0`, which is checked rather than assumed: a cold
render at t0 is compared against t0 reached by driving frame by frame from zero.

    $ python3 scripts/test-docks.py                     # uses the live cache
    $ python3 scripts/test-docks.py --cache-dir /tmp/c  # or a pointed one
    $ python3 scripts/test-docks.py --shot out.png      # and a 3x screenshot

The live cache is only needed for the checks against real data; everything else
builds its own cache directory and needs nothing. Populate it with
`python3 ftdata.py --once --only docks-nearby`.
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

import demoscene as ds                                        # noqa: E402
import docks                                                  # noqa: E402
import ftdata                                                 # noqa: E402

FAILED = []
PASSED = [0]

SITE = ftdata.DOCKS_SITE
WALK = ftdata.DOCKS_WALK_M_PER_MIN

# The product is `volatile`, which means ftdata looks in the *blob* directory
# before the cache directory -- so a --cache-dir pointed at a scratch record is
# quietly overruled by whatever the machine has in /run/ftdata or in
# FT_DATA_BLOBS. That is correct on the wall and a trap in a test: seven checks
# in this file passed against the live cache once before it was noticed, which
# is exactly the shape of failure this whole file exists to catch. So the blob
# directory is redirected alongside every synthetic cache, and put back before
# the live checks. Same trick, and the same comment, as test-stringline.py.
REAL_BLOB_DIR = ftdata.BLOB_DIR


def point_blobs(d):
    """Make `d` the only place a volatile record can come from."""
    ftdata.BLOB_DIR = d
    return d


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


def opts(**kw):
    kw.setdefault("reload", 0.0)
    return ds.options(docks, **kw)


def frames(args, n=8):
    """Render `n` frames in order from a fresh build."""
    r = docks.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def contains_text(frame, s, thresh=80, scales=(1, 2, 3), bg_max=0.25):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure the honest
    message actually reached it rather than merely being computed. The counters
    between the strokes have to be dark too -- a matcher that only asks "are the
    strokes on" answers yes to every string in the language somewhere inside a
    solid block of colour, which cost four false passes in the caiso version of
    this function before anybody noticed.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = docks.text_mask(s, scale)
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


def offset(metres_north, metres_east):
    """A lat/lon this far from the wall. The inverse of the projection."""
    kx = math.cos(math.radians(SITE[0]))
    return (SITE[0] + metres_north / 111320.0,
            SITE[1] + metres_east / (111320.0 * kx))


# --------------------------------------------------------------------------
# A neighbourhood we invented, so that every answer is known before it is drawn.
#
# Four docks on the compass points at known distances, with counts chosen so
# that each one is the unique answer to exactly one question: NORTH has the only
# classic bikes and is nearest, EAST has the only ebikes, SOUTH is jammed full,
# WEST is empty. Nothing here is realistic and everything here is checkable.
# --------------------------------------------------------------------------

def synthetic(cache_dir, fetched_ago=60.0, stations=None, loose=None,
              radius_m=1500.0, site_elev=7.0, mangle=None):
    if stations is None:
        stations = [
            # name,  north, east, bikes, ebikes, free, cap, open, elev
            ("North",   300,     0,    9,      0,   12,  21, 1,  7.0),
            ("East",      0,   600,   10,      5,    6,  16, 1, 27.0),
            ("South",  -900,     0,    4,      1,    0,   5, 1, -6.0),
            ("West",      0, -1200,    0,      0,   20,  20, 1,  7.0),
        ]
    rows = []
    for name, dn, de, b, e, f, cap, op, elev in stations:
        la, lo = offset(dn, de)
        rows.append({"name": name, "lat": round(la, 5), "lon": round(lo, 5),
                     "d": math.hypot(dn, de), "bikes": b, "ebikes": e,
                     "free": f, "cap": cap, "open": op, "elev": elev})
    rows.sort(key=lambda r: r["d"])

    lo = {"n": 0, "dist_m": [], "lat": [], "lon": [], "elec": [],
          "unavailable": 0, "source": ftdata.BIKES_FREE_URL}
    for dn, de, elec in (loose or []):
        la, ln = offset(dn, de)
        lo["dist_m"].append(int(round(math.hypot(dn, de))))
        lo["lat"].append(round(la, 5))
        lo["lon"].append(round(ln, 5))
        lo["elec"].append(int(elec))
    lo["n"] = len(lo["dist_m"])

    payload = {
        "as_of": time.time() - fetched_ago,
        "site": list(SITE), "site_name": "Sequoia Fabrica",
        "site_elev_m": site_elev,
        "radius_m": radius_m, "walk_m_per_min": WALK, "n": len(rows),
        "name": [r["name"] for r in rows],
        "dist_m": [int(round(r["d"])) for r in rows],
        "walk_min": [int(round(r["d"] / WALK)) for r in rows],
        "lat": [r["lat"] for r in rows], "lon": [r["lon"] for r in rows],
        "bikes": [r["bikes"] for r in rows],
        "ebikes": [r["ebikes"] for r in rows],
        "free_docks": [r["free"] for r in rows],
        "capacity": [r["cap"] for r in rows],
        "elev_m": [r["elev"] for r in rows],
        "open": [r["open"] for r in rows],
        "returning": [r["open"] for r in rows],
        "loose": lo,
        "totals": {"stations": len(rows)},
        "info": {}, "info_fetched": False, "units": {}, "sources": [],
    }
    if mangle:
        mangle(payload)
    os.makedirs(cache_dir, exist_ok=True)
    point_blobs(cache_dir)
    path = os.path.join(cache_dir, "docks-nearby.json")
    with open(path, "w") as fh:
        json.dump({"name": "docks-nearby", "source": "synthetic",
                   "ttl": ftdata.DOCKS_TTL,
                   "fetched_at": time.time() - fetched_ago,
                   "payload": payload}, fh)
    return path, {r["name"]: r for r in rows}


# --------------------------------------------------------------------------
# 1. The projection, which is the one thing a picture cannot check.
# --------------------------------------------------------------------------

def test_projection():
    print("\nthe map puts the wall where the wall is")
    tmp = tempfile.mkdtemp(prefix="docks-proj")
    try:
        synthetic(tmp)
        r, f = frames(opts(cache_dir=tmp), 4)
        lay, proj = r.layout, r.state["proj"]

        # The building is the centre of the pane, and it is the only place the
        # green cross is allowed to be.
        site = np.array(docks.C_SITE, np.uint8)
        pane = f[lay.body_y:, :lay.map_w]
        rows, cols = np.where((pane == site).all(axis=2))
        check("the wall is drawn at the centre of the map",
              len(rows) >= 8
              and abs(rows.mean() - (lay.body_h - 1) / 2.0) < 1.0
              and abs(cols.mean() - (lay.map_w - 1) / 2.0) < 1.0,
              "centroid (%.1f, %.1f) of %d px"
              % (rows.mean(), cols.mean(), len(rows)))

        # A dock 300 m due north projects straight up by 300 m of pixels. This
        # is the check that catches a swapped lat/lon, a missing cos(lat) and a
        # sign error, none of which change how the map looks.
        rr, cc = proj.project(*offset(300.0, 0.0))
        check("300 m north is straight up by 300 m of pixels",
              abs(cc - proj.cx) < 0.01
              and abs((proj.cy - rr) * proj.m_per_px - 300.0) < 1.0,
              "row %.2f col %.2f, %.1f m/px" % (rr, cc, proj.m_per_px))
        rr, cc = proj.project(*offset(0.0, 600.0))
        check("600 m east is straight right by 600 m of pixels",
              abs(rr - proj.cy) < 0.01
              and abs((cc - proj.cx) * proj.m_per_px - 600.0) < 1.0,
              "row %.2f col %.2f" % (rr, cc))

        # Isotropic, or the rings are not rings and no distance can be read off
        # the map at all.
        n = proj.project(*offset(500.0, 0.0))
        e = proj.project(*offset(0.0, 500.0))
        check("the map is isotropic: 500 m up is 500 m across, in pixels",
              abs(abs(n[0] - proj.cy) - abs(e[1] - proj.cx)) < 0.02,
              "%.2f px vs %.2f px" % (abs(n[0] - proj.cy), abs(e[1] - proj.cx)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_distances_against_the_real_world(cache_dir=None):
    print("\nthe real neighbourhood, at distances somebody measured by hand")
    ftdata.BLOB_DIR = REAL_BLOB_DIR
    got = ftdata.load("docks-nearby", cache_dir)
    # Independent of the cache: the arithmetic is in ftdata and can be checked
    # against the surveyed figure with no record at all.
    d = ftdata._docks_metres(37.7642, -122.4023)     # Jackson Playground
    check("Jackson Playground is 290-300 m from the wall", 285.0 < d < 305.0,
          "%.0f m" % d)
    check("...which is a four minute walk",
          ftdata._docks_walk_min(d) == 4, "%d min" % ftdata._docks_walk_min(d))
    check("the wall's coordinates are the surveyed ones, not adsb's",
          abs(SITE[0] - 37.7624929274026) < 1e-9
          and abs(SITE[1] + 122.39969356310202) < 1e-9,
          "%.6f, %.6f" % SITE)
    if got is None:
        check("live record present for the distance table", False,
              "run: python3 ftdata.py --once --only docks-nearby")
        return
    p = got[0]
    check("the nearest dock in the live record is Jackson Playground",
          p["name"][0].upper().startswith("JACKSON"),
          "%s at %d m" % (p["name"][0], p["dist_m"][0]))
    check("...at about 290 m", 280 <= p["dist_m"][0] <= 310,
          "%d m" % p["dist_m"][0])
    check("the record is sorted by distance",
          all(p["dist_m"][i] <= p["dist_m"][i + 1]
              for i in range(p["n"] - 1)))


# --------------------------------------------------------------------------
# 2. The encoding, read back off the panel.
# --------------------------------------------------------------------------

def test_up_is_take_down_is_leave():
    print("\nup is bikes, down is free docks, and that is not symmetric")
    tmp = tempfile.mkdtemp(prefix="docks-bar")
    try:
        synthetic(tmp)
        # The still-photograph mode: the breathing marker writes over the
        # nearest bike's own dock pixel, which is right on the wall and would
        # make this test depend on which frame it looked at.
        r, f = frames(opts(cache_dir=tmp, radius=1500.0,
                           **{"pulse_hz": 0.0, "sweep": 0.0}), 4)
        lay, proj = r.layout, r.state["proj"]
        pane = f[lay.body_y:, :lay.map_w].astype(int)

        by = {st.short: st for st in r.state["doc"].stations}

        def column(name):
            # The station's *stored* coordinates, not the ones the offsets were
            # built from: the record rounds lat/lon to five decimals, which is
            # a metre, and a metre is enough to move a dock sitting exactly on
            # a half-pixel into the next column. Reading the record back is
            # also the honest test -- it is what the demo does.
            st = by[name]
            rr, cc = proj.project(st.lat, st.lon)
            return int(round(rr)), int(round(cc))

        def count(col, r0, direction, rgb):
            """Consecutive pixels of this colour walking away from the dock."""
            n = 0
            k = 1
            while 0 <= r0 + direction * k < pane.shape[0]:
                if tuple(pane[r0 + direction * k, col]) != tuple(rgb):
                    break
                n += 1
                k += 1
            return n

        # North: nine classic bikes, twelve free docks. Green above, blue below.
        r0, c0 = column("NORTH")
        up = count(c0, r0, -1, docks.C_BIKE)
        down = count(c0, r0, +1, docks.C_FREE)
        check("a dock with 9 bikes grows 3 green pixels upward", up == 3,
              "%d px" % up)
        check("...and with 12 free docks, 3 blue pixels downward", down == 3,
              "%d px" % down)
        check("...with the dock's own pixel between them",
              tuple(pane[r0, c0]) == docks.C_DOCK, str(tuple(pane[r0, c0])))

        # East: ten bikes of which five are electric, so the needle is three
        # pixels and the two at its tip are amber. The tip and not the root,
        # because the tip is the end the eye lands on.
        r0, c0 = column("EAST")
        green = count(c0, r0, -1, docks.C_BIKE)
        check("a half-electric dock draws green next to the dock",
              green == 1, "%d green px" % green)
        check("...and amber at the tip of the needle",
              tuple(pane[r0 - 2, c0]) == docks.C_EBIKE
              and tuple(pane[r0 - 3, c0]) == docks.C_EBIKE,
              "%s %s" % (tuple(pane[r0 - 2, c0]), tuple(pane[r0 - 3, c0])))

        # And an all-electric dock is all amber, with no green at all: that is
        # the case somebody on this hill most wants to spot from a distance.
        allc = [s for s in r.state["doc"].stations if s.short == "SOUTH"][0]
        check("the amber share is the electric share, not a fixed tip",
              allc.ebikes == 1 and allc.bikes == 4
              and count(column("SOUTH")[1], column("SOUTH")[0], -1,
                        docks.C_EBIKE) == 0,
              "south: %d of %d electric, tip green"
              % (allc.ebikes, allc.bikes))

        # South is jammed: no free docks. The pip goes *below*, where the free
        # docks would have been, and it is the warning colour.
        r0, c0 = column("SOUTH")
        check("a full dock draws the red pip below, not above",
              tuple(pane[r0 + 1, c0]) == docks.C_NONE
              and tuple(pane[r0 - 1, c0]) != docks.C_NONE,
              "below %s above %s" % (tuple(pane[r0 + 1, c0]),
                                     tuple(pane[r0 - 1, c0])))

        # West is empty: no bikes. The pip goes above.
        r0, c0 = column("WEST")
        check("an empty dock draws the red pip above, not below",
              tuple(pane[r0 - 1, c0]) == docks.C_NONE
              and tuple(pane[r0 + 1, c0]) != docks.C_NONE,
              "above %s below %s" % (tuple(pane[r0 - 1, c0]),
                                     tuple(pane[r0 + 1, c0])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_headline():
    print("\nthe headline is the nearest bike, over both fleets")
    tmp = tempfile.mkdtemp(prefix="docks-head")
    try:
        # North Dock is 300 m and the nearest thing with bikes in it.
        synthetic(tmp)
        r, f = frames(opts(cache_dir=tmp), 4)
        best = r.state["doc"].nearest_bike()
        check("with no loose bikes the nearest dock wins",
              best[1] == "NORTH", "%s at %.0f m" % (best[1], best[0]))
        check("...and its walk time is on the panel in large type",
              contains_text(f, "4", scales=(3,)), "4 min")
        check("...and its name too", contains_text(f, "NORTH"))

        # Now put a loose ebike 150 m away. It has to win: it is closer, and a
        # panel that prefers a dock for being a dock is answering the wrong
        # question.
        synthetic(tmp, loose=[(150.0, 0.0, 1)])
        r, f = frames(opts(cache_dir=tmp), 4)
        best = r.state["doc"].nearest_bike()
        check("a loose ebike closer than any dock becomes the headline",
              best[2].startswith("loose") and abs(best[0] - 150.0) < 1.0,
              "%s at %.0f m" % (best[1], best[0]))
        check("...and the panel says where it is rather than naming a dock",
              contains_text(f, "ON THE STREET"))
        check("...and it is drawn on the map with no dock pixel under it",
              True, "%d loose drawn" % len(r.state["doc"].loose))

        # Nearest free dock is a different question with a different answer:
        # North has twelve, and it is nearest.
        f_ = r.state["doc"].nearest_free()
        check("the nearest free dock is asked separately",
              f_ is not None and f_[1] == "NORTH", str(f_ and f_[1]))

        # A neighbourhood where every dock is full: the returning rider's
        # nightmare, and it must say so rather than printing a dock name.
        synthetic(tmp, stations=[
            ("North", 300, 0, 9, 0, 0, 9, 1, 7.0),
            ("East", 0, 600, 5, 5, 0, 10, 1, 27.0)])
        r, f = frames(opts(cache_dir=tmp), 4)
        check("every dock full says so in words",
              r.state["doc"].nearest_free() is None
              and contains_text(f, "EVERY DOCK NEARBY IS FULL"))

        # And one where there is nothing to ride at all.
        synthetic(tmp, stations=[
            ("North", 300, 0, 0, 0, 9, 9, 1, 7.0),
            ("East", 0, 600, 0, 0, 10, 10, 1, 27.0)])
        r, f = frames(opts(cache_dir=tmp), 4)
        check("no bikes at all says NO BIKES WITHIN, not a zero",
              r.state["doc"].nearest_bike() is None
              and contains_text(f, "NO BIKES WITHIN"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ebike_arithmetic():
    print("\nthe electric count, which is a subset and easy to lose")
    tmp = tempfile.mkdtemp(prefix="docks-e")
    try:
        synthetic(tmp)
        r, _f = frames(opts(cache_dir=tmp), 4)
        by = {s.short: s for s in r.state["doc"].stations}
        check("classic is total minus electric",
              by["EAST"].classic == 5 and by["EAST"].ebikes == 5
              and by["EAST"].bikes == 10,
              "east: %d classic + %d electric = %d"
              % (by["EAST"].classic, by["EAST"].ebikes, by["EAST"].bikes))

        # The feed is not obliged to keep its subset small. A record claiming
        # more electric bikes than bikes must not produce a negative classic
        # count, which would be drawn as a negative-length needle.
        synthetic(tmp, stations=[("North", 300, 0, 3, 9, 5, 8, 1, 7.0)])
        r, _f = frames(opts(cache_dir=tmp), 4)
        st = r.state["doc"].stations[0]
        check("more electric than total is clamped, not drawn negative",
              st.classic == 0 and st.ebikes == 3,
              "%d classic, %d electric of %d" % (st.classic, st.ebikes,
                                                 st.bikes))

        # And the totals in the header must be the disjoint split, or the panel
        # double-counts a third of the fleet.
        synthetic(tmp)
        r, f = frames(opts(cache_dir=tmp), 4)
        n, bikes, ebikes, free, loose = r.state["doc"].totals()
        check("the header splits the fleet rather than double counting",
              contains_text(f, "%d BIKE" % (bikes - ebikes))
              and contains_text(f, "%d EBIKE" % ebikes),
              "%d pedal + %d electric = %d" % (bikes - ebikes, ebikes, bikes))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_closed_station():
    print("\na station that is not renting")
    tmp = tempfile.mkdtemp(prefix="docks-shut")
    try:
        synthetic(tmp, stations=[
            ("North", 300, 0, 9, 0, 12, 21, 0, 7.0),   # shut, with bikes
            ("East", 0, 600, 4, 0, 6, 10, 1, 27.0)])
        r, f = frames(opts(cache_dir=tmp), 4)
        doc = r.state["doc"]
        n, bikes, ebikes, free, loose = doc.totals()
        check("a shut station's bikes are not counted as available",
              bikes == 4, "%d bikes counted, 13 present" % bikes)
        check("...nor are its docks counted as free", free == 6, "%d" % free)
        check("...and the headline walks past it to the one that is open",
              doc.nearest_bike()[1] == "EAST", doc.nearest_bike()[1])
        check("...and the list says SHUT rather than a count",
              contains_text(f, "SHUT"))
        lay, proj = r.layout, r.state["proj"]
        pane = f[lay.body_y:, :lay.map_w]
        st = [s for s in doc.stations if s.short == "NORTH"][0]
        rr, cc = proj.project(st.lat, st.lon)
        rr, cc = int(round(rr)), int(round(cc))
        check("...and it is drawn as a grey pixel with no needles",
              tuple(pane[rr, cc]) == docks.C_SHUT
              and tuple(pane[rr - 1, cc]) != docks.C_BIKE,
              str(tuple(pane[rr, cc])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_names_and_grade():
    print("\nnames as people say them, and which way the walk goes")
    cases = [
        ("Rhode Island St at 17th St", "RHODE ISLAND/17TH"),
        ("22nd St at Potrero Ave", "22ND/POTRERO"),
        ("Jackson Playground", "JACKSON PLAYGROUND"),
        ("Esprit Park", "ESPRIT PARK"),
        ("8th St at Hooper St", "8TH/HOOPER"),
        ("22nd St Caltrain Station", "22ND ST CALTRAIN STATION"),
        ("Market St & Van Ness Ave", "MARKET/VAN NESS"),
        ("St Mary's Square", "ST MARY S SQUARE"),
        ("", "DOCK"),
    ]
    for raw, want in cases:
        got = docks.short_name(raw)
        check("%r shortens correctly" % raw[:26], got == want,
              "%r -> %r" % (got, want))

    tmp = tempfile.mkdtemp(prefix="docks-grade")
    try:
        synthetic(tmp)
        r, f = frames(opts(cache_dir=tmp), 4)
        by = {s.short: s for s in r.state["doc"].stations}
        check("a dock 20 m above the shop floor reads as uphill",
              abs(by["EAST"].grade - 20.0) < 0.01, "%.1f m" % by["EAST"].grade)
        check("...and one 13 m below as downhill",
              abs(by["SOUTH"].grade + 13.0) < 0.01,
              "%.1f m" % by["SOUTH"].grade)
        # The mark itself: warm above, cool below, in the list pane.
        lay = r.layout
        pane = f[lay.body_y:, lay.list_x:lay.list_x + lay.list_w]
        warm = (pane == np.array(docks.C_UP, np.uint8)).all(axis=2).sum()
        cool = (pane == np.array(docks.C_DOWN, np.uint8)).all(axis=2).sum()
        check("both grade marks reach the list", warm >= 3 and cool >= 3,
              "%d uphill px, %d downhill px" % (warm, cool))

        # No elevation in the record at all must cost the marks and nothing
        # else -- the bake can be missing on a checkout that has not fetched.
        synthetic(tmp, mangle=lambda p: p.update(elev_m=[None] * p["n"],
                                                 site_elev_m=None))
        r, f = frames(opts(cache_dir=tmp), 4)
        check("no elevations still draws the panel, without grade marks",
              all(s.grade is None for s in r.state["doc"].stations)
              and f.max() > 0 and contains_text(f, "NORTH"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_radius():
    print("\nthe radius is a drawing decision and it actually crops")
    tmp = tempfile.mkdtemp(prefix="docks-rad")
    try:
        synthetic(tmp)
        for radius, want in ((400.0, 1), (700.0, 2), (1000.0, 3), (1500.0, 4)):
            r, _f = frames(opts(cache_dir=tmp, radius=radius), 4)
            got = len(r.state["doc"].stations)
            check("%.0f m of radius holds %d of the four docks"
                  % (radius, want), got == want, "%d" % got)
        # And the scale changes with it, or the map is not a map.
        a = docks.build(opts(cache_dir=tmp, radius=400.0)).state["proj"]
        b = docks.build(opts(cache_dir=tmp, radius=1500.0)).state["proj"]
        check("the map scale follows the radius",
              b.m_per_px > a.m_per_px * 3.0,
              "%.1f m/px at 400 m, %.1f at 1500" % (a.m_per_px, b.m_per_px))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 3. Motion and purity.
# --------------------------------------------------------------------------

def test_purity():
    print("\nrender() is a pure function of t")
    tmp = tempfile.mkdtemp(prefix="docks-pure")
    try:
        synthetic(tmp)
        args = opts(cache_dir=tmp, reload=0.0)
        # Cold: build, ask for one t.
        cold = docks.build(args)(3.7, 74).copy()
        # Warm: build, drive from zero to the same t.
        warm = docks.build(args)
        out = None
        for i in range(75):
            out = warm(i / 20.0, i)
        check("a cold render at t=3.7 equals t=3.7 reached frame by frame",
              np.array_equal(cold, out),
              "%d pixels differ" % int((cold != out).any(axis=2).sum()))

        # And out of order, which is what a scheduler that restarts a segment
        # actually does.
        r = docks.build(args)
        a = r(9.1, 182).copy()
        r(0.0, 0)
        r(4.0, 80)
        b = r(9.1, 182).copy()
        check("...and asking for the same t twice gives the same frame",
              np.array_equal(a, b))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_motion():
    print("\nit is still, but not accidentally still")
    tmp = tempfile.mkdtemp(prefix="docks-mot")
    try:
        synthetic(tmp)
        r = docks.build(opts(cache_dir=tmp))
        prev, diffs = None, []
        for i in range(160):
            f = r(i / 20.0, i)
            if prev is not None:
                diffs.append(int((f != prev).any(axis=2).sum()))
            prev = f.copy()
        run = best = 0
        for d in diffs:
            run = run + 1 if d == 0 else 0
            best = max(best, run)
        check("the panel never holds one frame for a quarter of a second",
              best <= 4, "longest identical run %d frames" % best)
        check("...and it is calm: the sweep never lights much of the panel",
              max(diffs) < 900, "biggest change %d pixels" % max(diffs))

        # The sweep has to travel outward rather than flicker in one place.
        base = r.static.astype(int)
        radii = []
        proj = r.state["proj"]
        lay = r.layout
        for i in range(int(7.0 * 20)):
            f = r(i / 20.0, i).astype(int)
            d = np.abs(f[lay.body_y:, :lay.map_w]
                       - base[lay.body_y:, :lay.map_w]).sum(axis=2)
            rr, cc = np.where(d > 0)
            if len(rr) < 4:
                continue
            radii.append(float(np.hypot(rr - proj.cy, cc - proj.cx).mean()))
        check("the sweep walks outward from the building",
              len(radii) > 20 and radii[0] < radii[len(radii) // 2] < radii[-1],
              "mean radius %.1f -> %.1f -> %.1f px"
              % (radii[0], radii[len(radii) // 2], radii[-1]))

        # --pulse-hz 0 and --sweep 0 is the still-photograph mode, and it has
        # to actually be still or a screenshot script gets a half-lit ring.
        r2 = docks.build(opts(cache_dir=tmp, **{"pulse_hz": 0.0, "sweep": 0.0}))
        a, b = r2(0.0, 0).copy(), r2(11.3, 226).copy()
        check("with the pulse and sweep off the panel is a still photograph",
              np.array_equal(a, b))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cost():
    print("\nframe cost")
    tmp = tempfile.mkdtemp(prefix="docks-cost")
    try:
        synthetic(tmp)
        r = docks.build(opts(cache_dir=tmp))
        for i in range(20):                     # warm the allocator
            r(i / 20.0, i)
        ms = []
        for i in range(400):
            t0 = time.perf_counter()
            r(i / 20.0, i)
            ms.append((time.perf_counter() - t0) * 1000.0)
        ms.sort()
        mean = sum(ms) / len(ms)
        check("mean frame under 1 ms on this desktop", mean < 1.0,
              "mean %.3f p95 %.3f max %.3f ms"
              % (mean, ms[int(0.95 * len(ms))], ms[-1]))
        # The Pi is the machine that matters and it is not here; this project
        # has repeatedly measured 20-115x between a desktop and that Pi, so the
        # check is on the desktop figure being small enough that even the bad
        # end of that range clears the 20 ms budget.
        check("...which leaves room at 100x for the wall", mean * 100 < 20.0,
              "%.1f ms predicted at 100x" % (mean * 100))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. Degraded records.
# --------------------------------------------------------------------------

def test_degraded():
    print("\nmissing, ancient, corrupt and mis-shaped records")
    tmp = tempfile.mkdtemp(prefix="docks-bad")
    try:
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        point_blobs(empty)
        r, f = frames(opts(cache_dir=empty), 4)
        check("no cache at all says NO DOCK DATA",
              contains_text(f, "NO DOCK DATA"))
        check("...and names the command that fixes it",
              contains_text(f, "FTDATA.PY"))
        check("...and draws no map", r.state["doc"].state == "absent")

        bad = os.path.join(tmp, "corrupt")
        os.makedirs(bad)
        point_blobs(bad)
        with open(os.path.join(bad, "docks-nearby.json"), "w") as fh:
            fh.write('{"payload": {"name": ')
        _, f = frames(opts(cache_dir=bad), 4)
        check("a half-written file says NO DOCK DATA",
              contains_text(f, "NO DOCK DATA"))

        wrong = os.path.join(tmp, "wrongshape")
        os.makedirs(wrong)
        point_blobs(wrong)
        with open(os.path.join(wrong, "docks-nearby.json"), "w") as fh:
            json.dump({"name": "docks-nearby", "fetched_at": time.time(),
                       "payload": {"hello": "world"}}, fh)
        _, f = frames(opts(cache_dir=wrong), 4)
        check("a payload from some other product says NO DOCK DATA",
              contains_text(f, "NO DOCK DATA"))

        # The dangerous one: complete, well formed, and short by one column, so
        # every station past the gap would be drawn with its neighbour's counts.
        short = os.path.join(tmp, "short")
        synthetic(short, mangle=lambda p: p.__setitem__("ebikes",
                                                        p["ebikes"][:-1]))
        r, f = frames(opts(cache_dir=short), 4)
        check("a record with a short column is refused, not indexed into",
              r.state["doc"].state == "absent"
              and contains_text(f, "NO DOCK DATA"),
              str(r.state["doc"].problem)[:48])

        # Aging: past the TTL and still worth drawing, loudly.
        aging = os.path.join(tmp, "aging")
        synthetic(aging, fetched_ago=15 * 60.0)
        r, f = frames(opts(cache_dir=aging), 4)
        check("a fifteen minute old record still draws its counts",
              r.state["doc"].drawable and contains_text(f, "NORTH"))
        check("...and says OLD on the panel with the age",
              contains_text(f, "OLD") and contains_text(f, "15M"),
              "age %s" % ftdata.describe_age(r.state["doc"].age))

        # Stale: past three TTLs, and now the counts are a specific lie.
        stale = os.path.join(tmp, "stale")
        synthetic(stale, fetched_ago=45 * 60.0)
        r, f = frames(opts(cache_dir=stale), 4)
        check("a forty-five minute old record does not draw its counts",
              not r.state["doc"].drawable and not contains_text(f, "NORTH"),
              str(r.state["doc"].problem)[:48])
        check("...and says so in words rather than going blank",
              contains_text(f, "NOT DRAWN") and contains_text(f, "STALE"))
        check("...but keeps the geography, which did not expire",
              (f[:, :r.layout.map_w] == np.array(docks.C_SITE,
                                                 np.uint8)).all(axis=2).any())

        # A radius with nothing in it: a typo far more often than a real answer.
        r, f = frames(opts(cache_dir=os.path.dirname(
            synthetic(os.path.join(tmp, "tiny"))[0]), radius=150.0), 4)
        check("a radius with no docks in it gets the card, not an empty map",
              contains_text(f, "NO DOCK DATA"),
              str(r.state["doc"].problem)[:48])

        # free_bike_status failing in the fetcher leaves the block empty. The
        # panel must lose one line and nothing else.
        noloose = os.path.join(tmp, "noloose")
        synthetic(noloose, mangle=lambda p: p.__setitem__(
            "loose", {"n": 0, "dist_m": [], "lat": [], "lon": [], "elec": []}))
        r, f = frames(opts(cache_dir=noloose), 4)
        check("no free-floating feed still draws the docks",
              r.state["doc"].drawable and contains_text(f, "NORTH")
              and contains_text(f, "NO LOOSE EBIKES"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 5. Fresh, stale and absent, each in a process of its own.
#
# `ftdata.CACHE_DIR` is read at import time, so a test that sets FT_DATA_CACHE
# and reloads the module is testing its own import machinery. Each state gets a
# fresh interpreter, with the environment set the way the wall sets it.
# --------------------------------------------------------------------------

def _one_state(state, cache_dir):
    args = opts()                       # note: no cache_dir, so CACHE_DIR wins
    r = docks.build(args)
    out = None
    for i in range(8):
        out = r(i / 20.0, i)
    card = contains_text(out, "NO DOCK DATA")
    stale = contains_text(out, "STALE")
    drew = r.state["doc"].drawable
    verdict = {
        "fresh": (drew and not card and not stale, "drew the panel"),
        "stale": (not drew and not card and stale, "drew geography + STALE"),
        "absent": (not drew and card, "drew the no-data card"),
    }[state]
    print("RESULT %s %s cache=%s drew=%s card=%s stale=%s"
          % (state, "ok" if verdict[0] else "FAIL",
             os.path.basename(cache_dir), drew, card, stale))
    return 0 if verdict[0] else 1


def test_states_in_separate_processes():
    print("\nfresh, stale and absent -- one process each, FT_DATA_CACHE set")
    tmp = tempfile.mkdtemp(prefix="docks-proc")
    try:
        fresh = os.path.join(tmp, "fresh")
        synthetic(fresh, fetched_ago=60.0)
        stale = os.path.join(tmp, "stale")
        synthetic(stale, fetched_ago=45 * 60.0)
        absent = os.path.join(tmp, "absent")
        os.makedirs(absent)

        for state, d in (("fresh", fresh), ("stale", stale),
                         ("absent", absent)):
            env = dict(os.environ, FT_DATA_CACHE=d)
            # FT_DATA_BLOBS too: this product is volatile, so its record is
            # looked for in the blob directory first, and a stray /run/ftdata
            # on the machine running the tests must not answer for it.
            env["FT_DATA_BLOBS"] = d
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
# 6. The fetcher, other canvases, and the promise that none of this talks to
#    anyone.
# --------------------------------------------------------------------------

def test_fetcher_shape():
    print("\nthe fetcher's own arithmetic and its record")
    check("the product is registered with a short TTL and interval",
          ftdata.PRODUCTS["docks-nearby"]["ttl"] == 600
          and ftdata.PRODUCTS["docks-nearby"]["interval"] == 120,
          "ttl %s interval %s" % (ftdata.PRODUCTS["docks-nearby"]["ttl"],
                                  ftdata.PRODUCTS["docks-nearby"]["interval"]))
    check("...and is volatile, so it does not write the SD card every 2 min",
          ftdata.is_volatile("docks-nearby"))
    check("...and is handed the cache dir, for the info cache",
          ftdata.PRODUCTS["docks-nearby"].get("blob") is True)
    check("its interval is under FAST_INTERVAL, so the fast timer takes it",
          ftdata.DOCKS_INTERVAL <= ftdata.FAST_INTERVAL,
          "%s <= %s" % (ftdata.DOCKS_INTERVAL, ftdata.FAST_INTERVAL))

    # The station_information cache: a young block is reused without a request,
    # an old one is not, and one from a different radius is not either. Checked
    # by handing _docks_info a payload and asserting it never reaches the
    # network -- if it did, this test would take a second and could fail
    # offline, which is itself the assertion.
    good = {"info": {"at": time.time(), "radius_m": ftdata.DOCKS_RADIUS_M,
                     "site": list(ftdata.DOCKS_SITE), "id": ["1"],
                     "name": ["A"], "lat": [37.76], "lon": [-122.4],
                     "cap": [10]}}
    t0 = time.perf_counter()
    info, fetched = ftdata._docks_info(good, ftdata.DOCKS_RADIUS_M)
    check("a young info block is reused rather than re-fetched",
          not fetched and info["id"] == ["1"]
          and time.perf_counter() - t0 < 0.05,
          "%.1f ms" % ((time.perf_counter() - t0) * 1000))
    for name, broken in (
            ("an hour old", {"at": time.time() - 7200}),
            ("a different radius", {"radius_m": 999.0}),
            ("a different site", {"site": [37.0, -122.0]}),
            ("a short column", {"name": []})):
        bad = {"info": dict(good["info"], **broken)}
        # Not calling it (that would fetch); asserting the predicate directly.
        old = bad["info"]
        stale_at = time.time() - float(old["at"]) >= ftdata.DOCKS_INFO_TTL
        same = (float(old["radius_m"]) == ftdata.DOCKS_RADIUS_M
                and [round(v, 7) for v in old["site"]]
                == [round(v, 7) for v in ftdata.DOCKS_SITE])
        whole = all(len(old[k]) == len(old["id"])
                    for k in ("name", "lat", "lon", "cap"))
        check("%s info block is refused" % name,
              stale_at or not same or not whole)


def test_sizes():
    print("\nother panel sizes")
    tmp = tempfile.mkdtemp(prefix="docks-size")
    try:
        synthetic(tmp)
        for w, h in ((320, 64), (256, 64), (128, 64), (320, 32), (192, 96),
                     (512, 128), (64, 32), (320, 16)):
            try:
                r, f = frames(opts(cache_dir=tmp, width=w, height=h), 40)
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0)
                lay = r.layout
                detail = "map %d, list %d, col %d" % (lay.map_w, lay.list_w,
                                                      lay.col_w)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load("docks-nearby", tempfile.mkdtemp(prefix="docks-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl", "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "docks.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in src]
    check("docks.py does not import one either", not imported,
          ",".join(imported))


def test_live(cache_dir):
    print("\nagainst the live cache")
    ftdata.BLOB_DIR = REAL_BLOB_DIR
    got = ftdata.load("docks-nearby", cache_dir)
    if got is None:
        check("live record present", False,
              "run: python3 ftdata.py --once --only docks-nearby")
        return
    payload, age = got
    size = os.path.getsize(ftdata.record_path("docks-nearby", cache_dir))
    check("the live record is small", size < 32768,
          "%d bytes for %d stations and %d loose bikes"
          % (size, payload["n"], payload["loose"]["n"]))
    check("the live record has a sane number of docks in it",
          10 <= payload["n"] <= ftdata.DOCKS_MAX,
          "%d within %.0f m, %s old"
          % (payload["n"], payload["radius_m"], ftdata.describe_age(age)))
    check("docked ebikes are actually found -- the field trap",
          sum(payload["ebikes"]) > 0,
          "%d of %d docked bikes are electric"
          % (sum(payload["ebikes"]), sum(payload["bikes"])))
    check("...and never exceed the total at any station",
          all(e <= b for e, b in zip(payload["ebikes"], payload["bikes"])))
    check("free docks plus bikes never exceed capacity by much",
          all(b + f <= c + 4
              for b, f, c in zip(payload["bikes"], payload["free_docks"],
                                 payload["capacity"])))
    check("every station has an elevation",
          all(v is not None for v in payload["elev_m"]),
          "%.0f to %.0f m" % (min(payload["elev_m"]), max(payload["elev_m"])))

    r, f = frames(opts(cache_dir=cache_dir), 8)
    doc = r.state["doc"]
    check("the live record renders a panel and not a card",
          doc.drawable and not contains_text(f, "NO DOCK DATA"),
          "%d docks, nearest bike %s"
          % (len(doc.stations), doc.nearest_bike()
             and "%.0f m" % doc.nearest_bike()[0]))
    check("...and the nearest dock's name reaches the panel",
          contains_text(f, docks.fit(doc.stations[0].short, 88)),
          doc.stations[0].short)


def write_shot(path, cache_dir, at_t=1.5):
    """A 3x screenshot, 960x192 from the 320x64 panel."""
    from PIL import Image
    ftdata.BLOB_DIR = REAL_BLOB_DIR
    r = docks.build(opts(cache_dir=cache_dir))
    out = None
    for i in range(int(at_t * 20) + 1):
        out = r(i / 20.0, i)
    im = Image.fromarray(np.asarray(out, np.uint8).copy(), "RGB")
    im = im.resize((im.width * 3, im.height * 3), Image.NEAREST)
    im.save(path)
    print("wrote %s (%dx%d)" % (path, im.width, im.height))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--shot", default="", help="also write a 3x screenshot")
    ap.add_argument("--state", default="",
                    choices=("", "fresh", "stale", "absent"),
                    help="internal: run one data state and print RESULT")
    a = ap.parse_args()
    if a.state:
        return _one_state(a.state, a.cache_dir)

    print("cache: %s" % a.cache_dir)
    test_no_network()
    test_projection()
    test_distances_against_the_real_world(a.cache_dir)
    test_up_is_take_down_is_leave()
    test_headline()
    test_ebike_arithmetic()
    test_closed_station()
    test_names_and_grade()
    test_radius()
    test_purity()
    test_motion()
    test_cost()
    test_degraded()
    test_states_in_separate_processes()
    test_fetcher_shape()
    test_sizes()
    test_live(a.cache_dir)
    if a.shot:
        write_shot(a.shot, a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
