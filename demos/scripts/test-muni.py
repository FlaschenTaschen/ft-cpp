#!/usr/bin/env python3
"""Checks for muni.py that a screenshot cannot make.

This panel can draw a confident, pretty, wrong picture in several ways, and
none of them look wrong on the wall:

  1. **The 22 can silently vanish.** Its stop is 413 m away; the 19's and the
     55's are 140-245 m. Any stop search that takes the nearest few stops
     overall, or uses a quarter-mile radius, finds four stops of the 19 and 55
     and never reaches the 22 -- and then draws a beautiful panel that claims
     three routes and shows two. A cron job on the Pi had exactly this bug for
     212 consecutive runs; `test_cron_coverage` writes that comparison down.
     Now that rows are stops rather than routes there are only four rows for
     four places, so "which rows fit" is a second way to lose the 22, and it is
     asserted in the rendered layout and not only in the record.
  2. **The catchable/missed boundary can be off by a row.** It is the whole
     panel: a bus one pixel outside the post is one you can run for, one pixel
     inside it is one you have lost. Drawn the wrong way round it is exactly
     as pretty and says the opposite thing, so it is asserted in pixels either
     side of the post, on both sides of the stop, rather than in arithmetic.
  3. **The posts can end up in the wrong place.** If the walk is not drawn to
     the same scale as the buses, the panel is decorative rather than true.
     The posts are asserted against walk time * pixels-per-minute directly,
     and separately asserted *not* to be equidistant -- the three real
     distances being different is the panel's content.
  4. **The two directions can end up on the same side.** The layout's whole
     claim is that inbound comes in from one edge and outbound from the other
     and they converge on the stop between them. Asserted in pixels.
  5. **A shared stop can leak.** 16th & Wisconsin is the 22's stop *and* a 55
     stop. The 55 has its own stop 200 m closer, so a Wisconsin 55 drawn on the
     55's row would sit at the wrong distance. Asserted by line and direction.
  6. **A scheduled bus can wear a tracked bus's clothes.** `Monitored: false`
     must draw hollow. This is the panel's honesty and it is one array away
     from being lost.

Two things about how these run, both learned the hard way in this tree. The
demo is a **wall-clock** panel, so every check pins `--now`; without that the
answers change between one run and the next. Pinning `--now` is not enough on
its own, though: a record's *age* is measured against the real clock inside
`ftdata.load()`, so the fixture's `fetched_at` is written from `time.time()`
and not from the pinned moment -- see `synthetic()`. `--time-offset` exists to
prove that: it moves the real clock out from under the suite and everything
must still pass. And `ftdata.CACHE_DIR` binds at import, so the three data
states -- fresh, stale, absent -- are each run in a **separate process** with
FT_DATA_CACHE set, at the bottom of this file. Reloading the module in one
process does not test what it looks like it tests.

    $ python3 scripts/test-muni.py                     # synthetic, needs nothing
    $ python3 scripts/test-muni.py --cache-dir /tmp/c  # also check a real record
    $ python3 scripts/test-muni.py --time-offset 86400 # the clock, a day on

Populate a real cache with:

    $ python3 ftdata.py --once --only muni-18th
    $ FT_511_KEY=... python3 ftdata.py --once --only muni-live
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
import muni                                                   # noqa: E402

FAILED = []
PASSED = [0]

# A fixed weekday moment, so every expected number below is a constant.
NOW = 1786555946.0 + 90.0


def check(name, ok, detail=""):
    if ok:
        PASSED[0] += 1
    else:
        FAILED.append("%s%s" % (name, (": " + detail) if detail else ""))
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name,
                         ("  -- " + detail) if detail and not ok else ""))


# --------------------------------------------------------------------------
# A synthetic pair of records whose answers cannot be argued with.
#
# It mirrors the real geometry rather than inventing one, because the layout
# now depends on the *shape* of that geometry and not just its numbers: six
# (route, direction) flows collapse into four places, because the 55 and the 22
# each have both directions at one corner and the 19 does not -- its two
# directions stop a block apart. A fixture with six distinct stop names would
# have six rows and would never exercise the pairing, and a fixture with three
# would never exercise the lopsided row.
#
# Walks are exact round numbers of minutes so that "catchable" and "missed" are
# decided by arithmetic the test controls rather than by whatever the live feed
# happened to be doing.
# --------------------------------------------------------------------------

# (route, direction) -> the stop it calls at.
NAMES = {("19", 1): "De Haro St & 18th St",
         ("19", 0): "Rhode Island St & 18th St",
         ("22", 1): "16th Street & Wisconsin St",
         ("22", 0): "16th St & Wisconsin St",
         ("55", 1): "Connecticut St & 18th St",
         ("55", 0): "Connecticut St & 18th St"}
CODES = {("19", 1): "14352", ("19", 0): "16192",
         ("22", 1): "17769", ("22", 0): "17762",
         ("55", 1): "14126", ("55", 0): "14125"}
LABELS = {("19", 1): "BEACH", ("19", 0): "SHIPYARD",
          ("22", 1): "BAY", ("22", 0): "UCSF",
          ("55", 1): "16TH/MSN", ("55", 0): "20TH/3RD"}
# Minutes of walk, per flow. Deliberately unequal *within* a pair as well as
# between them: the two sides of one corner are not the same walk either.
WALK_MIN = {("19", 1): 2.0, ("19", 0): 4.0,
            ("22", 1): 7.0, ("22", 0): 6.5,
            ("55", 1): 3.5, ("55", 0): 3.0}
METRES = {("19", 1): 140, ("19", 0): 245,
          ("22", 1): 423, ("22", 0): 413,
          ("55", 1): 201, ("55", 0): 187}
FLOWS = [("19", 1), ("19", 0), ("22", 1), ("22", 0), ("55", 1), ("55", 0)]

# The rows the fixture produces, nearest first, and the names as drawn.
ROWS = ["DE HARO", "CONNECTICUT", "RHODE ISLAND", "16TH/WISCONSIN"]

# The three stops the disabled cron job on the Pi covered. It called
# find_stops_within_radius("Sequoia Fabrica", radius_miles=0.25) and took
# stops[:3], which is exactly these -- De Haro & 18th at 0.152 km and the two
# sides of Connecticut & 18th at 0.175 and 0.189 km. Every 22 stop on 16th St
# is 0.41 km away, outside that radius, so the cron never showed a 22 at all.
# This panel must be a strict superset of it or it is a regression dressed up
# as a redesign.
CRON_STOPS = set(("14352", "14125", "14126"))


def synthetic(cache_dir, now=NOW, live=True, fetched_ago=60.0,
              live_ago=60.0, expired=False):
    """Write a muni-18th (and optionally muni-live) record into `cache_dir`.

    Two clocks are in play here and they are not the same clock.

    The *panel* clock is pinned: `now` decides where the buses are drawn, and
    every expected column below is a constant derived from it. But the
    *freshness* clock is not ours to pin -- `ftdata.load()` reports a record's
    age as `time.time() - fetched_at`, against the real wall clock, and
    `muni.build()` drops a live record past its TTL and falls back to the
    timetable. So `fetched_ago` is measured from the real clock, never from
    `now`. Writing `fetched_at = now - 60` instead gives a fixture whose
    freshness decays as the real clock walks away from the pinned `NOW`: the
    suite passed for the first half hour after `NOW` and then, silently,
    started rendering the schedule fallback and failing fifteen position and
    colour checks that had nothing to do with position or colour.
    """
    os.makedirs(cache_dir, exist_ok=True)
    real = time.time()
    today = time.strftime("%Y%m%d", time.localtime(now))
    dates = [time.strftime("%Y%m%d", time.localtime(now + d * 86400.0))
             for d in (-1, 0, 1)]
    services = {} if expired else dict((d, ["W"]) for d in dates)

    stops = []
    for flow in FLOWS:
        route, direction = flow
        # Timetabled departures every ten minutes from local midnight, so the
        # schedule fallback always has something on the street.
        stops.append({
            "route": route, "dir": direction, "code": CODES[flow],
            "name": NAMES[flow], "label": LABELS[flow],
            "metres": METRES[flow], "walk_s": int(WALK_MIN[flow] * 60.0),
            "headsign": LABELS[flow], "lat": 37.76, "lon": -122.40,
            "times": {"W": list(range(0, 1440, 10))},
        })
    _write(cache_dir, "muni-18th", {
        "schema": 1, "feed_version": "TEST", "feed_start": dates[0],
        "feed_end": dates[-1], "services": services, "stops": stops,
        "routes": ["19", "22", "55"], "walk_mps": 1.25, "walk_detour": 1.3,
    }, real - fetched_ago, ftdata.ttl_for("muni-18th"))
    del today

    if not live:
        try:
            os.unlink(os.path.join(cache_dir, "muni-live.json"))
        except OSError:
            pass
        return

    # Buses at exact offsets either side of each flow's walk time, plus one
    # far out, plus a lateness pair and one unmonitored bus.
    live_stops = {}
    for flow in FLOWS:
        route, direction = flow
        walk_m = WALK_MIN[flow]
        offsets = (walk_m - 0.5, walk_m + 0.5, walk_m + 4.0)
        visits = []
        for i, off in enumerate(offsets):
            exp = now + off * 60.0
            visits.append({
                "line": route, "dir": "IB" if direction else "OB",
                "dest": LABELS[flow], "exp": int(round(exp)),
                # The *far* bus of every flow is three minutes late. It has to
                # be the far one: make the near one late and its aimed time
                # falls past the stop, on the other side of the centre, where
                # there is nothing to assert against.
                "aim": int(round(exp - 180.0)) if i == 2 else int(round(exp)),
                "mon": not (route == "55" and direction == 1 and i == 1),
            })
        # The poison pill: 16th & Wisconsin really does carry the 55 as well
        # as the 22, at a completely different walk distance.
        if route == "22":
            visits.append({
                "line": "55", "dir": "IB" if direction else "OB",
                "dest": "WRONG", "exp": int(round(now + 60.0)),
                "aim": int(round(now + 60.0)), "mon": True})
        live_stops[CODES[flow]] = visits
    _write(cache_dir, "muni-live", {
        "schema": 1, "t": int(now), "agency": "SF", "stops": live_stops,
        "n_stops": 6, "n_asked": 6, "errors": [], "rate_limited": None,
    }, real - live_ago, ftdata.ttl_for("muni-live"))


def _write(cache_dir, name, payload, fetched_at, ttl):
    path = os.path.join(cache_dir, name + ".json")
    with open(path, "w") as fh:
        json.dump({"name": name, "fetched_at": fetched_at, "source": "test",
                   "ttl": ttl, "payload": payload}, fh)


def opts(**kw):
    kw.setdefault("now", NOW)
    return ds.options(muni, **kw)


def rendered(cache, **kw):
    """(frame at t=0, the laid-out rows behind it)."""
    render = muni.build(opts(cache_dir=cache, **kw))
    return render(0.0, 0), getattr(render, "lanes", [])


def by_name(lanes, name):
    for lane in lanes:
        if lane.name == name:
            return lane
    return None


def ppm_for(horizon=None):
    return muni.HALF_W / (muni.HORIZON_MIN if horizon is None else horizon)


def bus_x(flow, mins, ppm):
    """The column a bus `mins` out sits at, on its own side of the stop."""
    return int(round(muni.CX + flow.side * mins * ppm))


def side_band(frame, lane, side):
    """The rows a direction's buses are drawn in, and only those."""
    _name_y, _li, bus_in, road, bus_out, late_out = muni.lane_rows(lane)
    if side == muni.LEFT:
        return frame[bus_in:road]
    return frame[bus_out:late_out]


def lit_columns(band, thresh=24):
    """Columns of a band with any pixel above `thresh`."""
    return set(np.nonzero(band.max(axis=(0, 2)) > thresh)[0].tolist())


def bus_columns(band, side, thresh=24):
    """Lit columns of a band, minus the stop's own column.

    The stop is drawn as a bright vertical at the centre through both bus
    bands, and it never moves. Left in, it pins the extreme of every band to
    the centre column and a check that buses close on the stop reads
    160 -> 160 for ever, which is true and says nothing.
    """
    cut = muni.CX - 2 if side == muni.LEFT else muni.CX + 2
    lit = lit_columns(band, thresh)
    return set(c for c in lit if (c < cut if side == muni.LEFT else c > cut))


def is_missed(band, x):
    """Is the sprite centred near column x drawn in the missed grey?"""
    win = band[:, max(0, x - 5):x + 6].reshape(-1, 3).astype(int)
    win = win[win.max(axis=1) > 24]
    if not len(win):
        return None
    # Grey means the three channels sit close together; a route colour does not.
    spread = (win.max(axis=1) - win.min(axis=1)).mean()
    # bool(), not the numpy scalar: `np.bool_(True) is True` is False, and a
    # test that asserts identity against one silently never passes.
    return bool(spread < 24)


def post_column(frame, lane, side):
    """Where the post actually got drawn, found in pixels, not arithmetic.

    The post is the brightest thing on the road row of its own half. The stop's
    own column at the centre is brighter still and would win an unqualified
    argmax on every row, so the centre and its immediate neighbours are cut out
    of the search rather than reasoned about.
    """
    road = muni.lane_rows(lane)[3]
    row = frame[road].astype(int).sum(axis=1)
    if side == muni.LEFT:
        lo, hi = 0, muni.CX - 1
    else:
        lo, hi = muni.CX + 2, muni.W
    return lo + int(np.argmax(row[lo:hi]))


# --------------------------------------------------------------------------

def test_fixture_is_live():
    """The fixture must read as fresh, or every check below it means nothing.

    Almost everything this file asserts -- bus columns, the greying either side
    of the post, hollowness, the lateness mark, the margin text -- is asserted
    against the *live* record. `build()` drops a live record past its TTL and
    draws the timetable instead, which is correct behaviour and a disaster for
    a test: the panel still renders, still looks right, and fifteen checks fail
    describing pixels without once mentioning freshness. This check exists so
    that that failure has a name.
    """
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        got = ftdata.load("muni-live", cache)
        fresh = got is not None and ftdata.is_fresh("muni-live", got[1])
        check("the synthetic live record reads as fresh", fresh,
              "age %ds vs ttl %s" % (int(got[1]) if got else -1,
                                     ftdata.ttl_for("muni-live")))
        # And in pixels: `auto` must land on the same frame as forcing `live`,
        # and a different one from the timetable. Forcing `live` uses the
        # record whether or not it is believed, so the two agreeing is exactly
        # the statement that the record was believed.
        auto = muni.build(opts(cache_dir=cache))(0.0, 0)
        forced = muni.build(opts(cache_dir=cache, source="live"))(0.0, 0)
        sched = muni.build(opts(cache_dir=cache, source="schedule"))(0.0, 0)
        check("auto draws the live record, not the timetable",
              np.array_equal(auto, forced) and not np.array_equal(auto, sched))


def test_routes_present():
    """All three routes, both directions. The 22 is the one that goes missing."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        payload = ftdata.load("muni-18th", cache)[0]
        got = set((s["route"], s["dir"]) for s in payload["stops"])
        for route in ("19", "22", "55"):
            for direction in (0, 1):
                check("route %s dir %d present" % (route, direction),
                      (route, direction) in got)
        # And, separately, present *in the drawn layout*. Rows are places now
        # and there is room for four of them, so a route can survive the fetch
        # and still be dropped by the layout, which the old per-route panel
        # could not do.
        _frame, lanes = rendered(cache)
        drawn = set()
        for lane in lanes:
            for flow in lane.flows:
                drawn.add((flow.route, flow.dir))
        for route in ("19", "22", "55"):
            for direction in (0, 1):
                check("route %s dir %d is drawn" % (route, direction),
                      (route, direction) in drawn,
                      "drawn %s" % sorted(drawn))


def test_cron_coverage():
    """A strict superset of the cron job this panel replaced.

    The cron called find_stops_within_radius(site, 0.25 miles) and took the
    first three, which were 14352 (De Haro & 18th, 0.152 km) and 14125/14126
    (Connecticut & 18th, 0.189/0.175 km). Every 22 stop on 16th St is 0.41 km
    or further, outside that radius, so in 212 consecutive runs the cron never
    once showed the 22. Losing any of its three while claiming to replace it
    would be a silent regression, and gaining the 22 is the point.
    """
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        payload = ftdata.load("muni-18th", cache)[0]
        codes = set(str(s.get("code")) for s in payload["stops"])
        check("the record covers every stop the cron covered",
              CRON_STOPS <= codes,
              "missing %s" % sorted(CRON_STOPS - codes))
        check("the record covers strictly more than the cron did",
              codes > CRON_STOPS, "codes %s" % sorted(codes))

        _frame, lanes = rendered(cache)
        drawn = set(f.code for lane in lanes for f in lane.flows)
        check("every cron stop survives the layout", CRON_STOPS <= drawn,
              "missing %s" % sorted(CRON_STOPS - drawn))
        routes = set(f.route for lane in lanes for f in lane.flows)
        check("and the 22 the cron could never see is drawn", "22" in routes,
              "routes %s" % sorted(routes))
        check("the cron's own radius would still exclude the 22",
              min(f.metres for lane in lanes for f in lane.flows
                  if f.route == "22") > 402.0)


def test_grouping():
    """Six flows, four places, and the 19's two directions are not one place."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        _frame, lanes = rendered(cache)
        check("six flows collapse into four rows", len(lanes) == 4,
              "%d rows: %s" % (len(lanes), [l.name for l in lanes]))
        check("rows are ordered by walk, nearest first",
              [l.name for l in lanes] == ROWS,
              "%s" % [l.name for l in lanes])

        conn = by_name(lanes, "CONNECTICUT")
        check("both 55s share one row",
              conn is not None and len(conn.flows) == 2
              and set(f.dir for f in conn.flows) == set((0, 1)))
        wis = by_name(lanes, "16TH/WISCONSIN")
        check("both 22s share one row",
              wis is not None and len(wis.flows) == 2)
        # SFMTA spells the two sides of that corner differently -- "16th
        # Street & Wisconsin St" against "16th St & Wisconsin St" -- and they
        # still have to land in one row.
        check("the two spellings of one corner group together",
              NAMES[("22", 1)] != NAMES[("22", 0)] and wis is not None
              and len(wis.flows) == 2)
        for name in ("DE HARO", "RHODE ISLAND"):
            lane = by_name(lanes, name)
            check("%s carries one direction only" % name,
                  lane is not None and len(lane.flows) == 1)
        check("the 19's two directions are two different rows",
              by_name(lanes, "DE HARO").flows[0].dir
              != by_name(lanes, "RHODE ISLAND").flows[0].dir)


def test_names():
    """The abbreviation is derived, drawable, and never silently lossy."""
    check("street types are dropped",
          muni.street_parts("De Haro St & 18th St") == ["DE HARO", "18TH"],
          "%s" % muni.street_parts("De Haro St & 18th St"))
    check("the two spellings of one corner normalise alike",
          muni.street_parts("16th Street & Wisconsin St")
          == muni.street_parts("16th St & Wisconsin St"))
    parts = [muni.street_parts(NAMES[f]) for f in FLOWS]
    home = muni.home_street(parts)
    check("our own street is derived, not hardcoded", home == "18TH", home)
    check("our own street is elided from a name",
          muni.abbreviate(["DE HARO", "18TH"], "18TH") == "DE HARO")
    check("a stop somewhere else keeps both streets",
          muni.abbreviate(["16TH", "WISCONSIN"], "18TH") == "16TH/WISCONSIN")
    check("eliding never leaves a nameless row",
          muni.abbreviate(["18TH"], "18TH") == "18TH")
    # One street shared by one stop is a coincidence, not our address.
    check("a street needs two stops before it counts as ours",
          muni.home_street([["A", "B"], ["C", "D"]]) is None)
    # The font has no ampersand, so nothing may reach the panel carrying one.
    for name in ROWS:
        check("%s is drawable in this font" % name,
              all(c in muni.CHARSET for c in name))
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        _frame, lanes = rendered(cache)
        check("every drawn name is drawable",
              all(all(c in muni.CHARSET for c in l.name) for l in lanes))
        check("no drawn name overflows the panel",
              all(muni.text_width(l.name) < muni.W - 2 * 40 for l in lanes),
              "%s" % [(l.name, muni.text_width(l.name)) for l in lanes])


def test_row_heights():
    """The nearest stop gets more rows than the far ones, and nothing spills."""
    check("the budget is spent exactly",
          sum(muni._allocate(4)) == muni.LANE_BUDGET,
          "%s" % muni._allocate(4))
    for n in range(1, muni.LANE_MAX_LANES + 1):
        heights = muni._allocate(n)
        check("%d rows spend the budget exactly" % n,
              sum(heights) == muni.LANE_BUDGET, "%s" % heights)
        check("%d rows are all big enough to draw" % n,
              min(heights) >= muni.LANE_MIN_H, "%s" % heights)
        check("%d rows never grow towards the far end" % n,
              all(heights[i] >= heights[i + 1] for i in range(n - 1)),
              "%s" % heights)
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        _frame, lanes = rendered(cache)
        check("the nearest stop's row is the tallest",
              lanes[0].h > lanes[-1].h,
              "%s" % [(l.name, l.h) for l in lanes])
        check("the nearest stop gets the taller bus",
              lanes[0].bus_h > lanes[-1].bus_h,
              "%s" % [(l.name, l.bus_h) for l in lanes])
        for lane in lanes:
            rows = muni.lane_rows(lane)
            check("%s fits inside the panel" % lane.name,
                  rows[0] >= muni.LANE_TOP and rows[-1] < muni.H,
                  "%s in %d..%d" % (rows, lane.y0, lane.y0 + lane.h))


def test_posts_to_scale():
    """The posts are the walk, at the same pixels-per-minute as the buses."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        ppm = ppm_for()
        frame, lanes = rendered(cache)
        for lane in lanes:
            for flow in lane.flows:
                want = int(round(muni.CX + flow.side * flow.walk_min * ppm))
                got = post_column(frame, lane, flow.side)
                check("%s %s post at the walk distance"
                      % (lane.name, muni.DIR_OF[flow.dir]),
                      abs(got - want) <= 1, "want %d got %d" % (want, got))

        # And, the point of the whole panel: they are *not* equidistant. The
        # 22's gate has to be several times the 19's, because 413 m is several
        # times 140 m, and a layout that centred every name in an equal box
        # would pass every check above and say nothing true.
        gates = dict((l.name, abs(l.flows[0].post_x - muni.CX)) for l in lanes)
        check("the walks are drawn at different widths",
              len(set(gates.values())) == len(gates), "%s" % gates)
        check("the far stop's gate dwarfs the near one's",
              gates["16TH/WISCONSIN"] > 3 * gates["DE HARO"], "%s" % gates)
        check("gates widen with walk time",
              gates["DE HARO"] < gates["CONNECTICUT"]
              < gates["RHODE ISLAND"] < gates["16TH/WISCONSIN"], "%s" % gates)
        # The two sides of one corner are not the same walk either, and the
        # panel must not average them.
        conn = by_name(lanes, "CONNECTICUT")
        left = abs(conn.by_side[muni.LEFT].post_x - muni.CX)
        right = abs(conn.by_side[muni.RIGHT].post_x - muni.CX)
        check("the two sides of one corner keep their own walks", left != right,
              "left %d right %d" % (left, right))


def test_directions_converge():
    """Inbound from one edge, outbound from the other, meeting at the name."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        ppm = ppm_for()
        frame, lanes = rendered(cache)
        lane = by_name(lanes, "16TH/WISCONSIN")
        for flow in lane.flows:
            x = bus_x(flow, flow.walk_min + 0.5, ppm)
            if flow.side == muni.LEFT:
                check("inbound approaches from the left", x < muni.CX,
                      "x=%d" % x)
            else:
                check("outbound approaches from the right", x > muni.CX,
                      "x=%d" % x)
            band = side_band(frame, lane, flow.side)
            check("%s is drawn in its own band" % muni.DIR_OF[flow.dir],
                  x in lit_columns(band), "x=%d" % x)
            # ...and nowhere near the other one. The two bands are either side
            # of the road, so a direction leaking into the wrong band is the
            # failure that would make the panel read backwards.
            other = side_band(frame, lane, -flow.side)
            wrong = [c for c in lit_columns(other) if abs(c - x) <= 2]
            check("%s does not leak into the other band"
                  % muni.DIR_OF[flow.dir], not wrong, "%s" % wrong)

        # A stop with only one direction leaves the other half of its road
        # visibly dark, rather than drawing a street nothing runs on.
        solo = by_name(lanes, "DE HARO")
        road = muni.lane_rows(solo)[3]
        served = solo.flows[0].side
        live_px = int(frame[road, muni.CX - 30 if served == muni.LEFT
                            else muni.CX + 30].sum())
        dead_px = int(frame[road, muni.CX + 30 if served == muni.LEFT
                            else muni.CX - 30].sum())
        check("an unserved half of road is drawn darker", dead_px < live_px,
              "dead %d vs live %d" % (dead_px, live_px))


def test_catchable_boundary():
    """A bus outside the walk is its route's colour; inside it, grey."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        ppm = ppm_for()
        frame, lanes = rendered(cache)
        for lane in lanes:
            for flow in lane.flows:
                band = side_band(frame, lane, flow.side)
                inside = bus_x(flow, flow.walk_min - 0.5, ppm)
                outside = bus_x(flow, flow.walk_min + 0.5, ppm)
                tag = "%s %s" % (lane.name, muni.DIR_OF[flow.dir])
                check("%s bus inside the walk is greyed" % tag,
                      is_missed(band, inside) is True, "x=%d" % inside)
                check("%s bus outside the walk keeps its colour" % tag,
                      is_missed(band, outside) is False, "x=%d" % outside)


def test_shared_stop_does_not_leak():
    """A 55 sitting at the 22's stop must not appear on the 22's row."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        live = ftdata.load("muni-live", cache)[0]
        stop = {"code": CODES[("22", 1)], "route": "22", "dir": 1}
        got = muni.live_visits(live, stop)
        check("22 row takes only 22s from a shared stop", len(got) == 3,
              "got %d visits" % len(got))
        # ...and the 55's own row reads its own, closer, stop.
        stop55 = {"code": CODES[("55", 1)], "route": "55", "dir": 1}
        check("55 row reads its own stop",
              len(muni.live_visits(live, stop55)) == 3)
        # Direction must filter too: asking the same stop the other way round
        # must not return the inbound buses.
        wrong = muni.live_visits(live, {"code": CODES[("22", 1)],
                                        "route": "22", "dir": 0})
        check("direction filters as well as line", len(wrong) == 0)


def test_unmonitored_is_hollow():
    """Monitored:false draws the outline, not the solid."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        ppm = ppm_for()
        frame, lanes = rendered(cache)
        lane = by_name(lanes, "CONNECTICUT")
        flow = lane.by_side[muni.LEFT]          # the inbound 55
        check("the unmonitored bus is on the row under test", flow.dir == 1)
        band = side_band(frame, lane, flow.side)
        x = bus_x(flow, flow.walk_min + 0.5, ppm)         # unmonitored
        solid_x = bus_x(flow, flow.walk_min + 4.0, ppm)   # monitored
        b = lane.bus_h
        hollow = band[1:b - 1, x - 2:x + 3].max()
        solid = band[1:b - 1, solid_x - 2:solid_x + 3].max()
        check("unmonitored bus is drawn hollow", int(hollow) < int(solid),
              "hollow centre %d vs solid centre %d" % (hollow, solid))
        # The roof still has to be there, or it is not a bus, it is a gap.
        check("the hollow bus still has a silhouette",
              int(band[0, x - 2:x + 3].max()) > 20)


def test_lateness_bar():
    """Lateness is a mark beside the road, on the same axis, signed."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        ppm = ppm_for()
        frame, lanes = rendered(cache)
        lane = by_name(lanes, "DE HARO")
        flow = lane.flows[0]
        late_y = muni.lane_rows(lane)[1 if flow.side == muni.LEFT else 5]
        row = frame[late_y].astype(int)
        lit = np.nonzero(row.sum(axis=1) > 24)[0]
        check("a lateness mark is drawn at all", len(lit) > 0)
        if len(lit):
            # The far bus is three minutes late, so its aimed cap sits three
            # minutes further out along its own side.
            bus = bus_x(flow, flow.walk_min + 4.0, ppm)
            aim = bus_x(flow, flow.walk_min + 4.0 - 3.0, ppm)
            lo, hi = sorted((bus, aim))
            check("the lateness mark reaches back to the aimed time",
                  lit.min() <= lo + 2 and lit.max() >= hi - 3,
                  "lit %d..%d, want ~%d..%d" % (lit.min(), lit.max(), lo, hi))
            # Late is warm: red channel must dominate blue.
            px = frame[late_y, aim].astype(int)
            check("late reads warm", px[0] > px[2], "rgb %s" % px.tolist())
            # A missed bus gets no mark: how late a bus you cannot catch is
            # running is not information. The near bus is inside the walk.
            missed = bus_x(flow, flow.walk_min - 0.5, ppm)
            near = row[max(0, missed - 3):missed + 4].sum(axis=1)
            # Against the threshold, not against zero: the background is
            # (6, 7, 10), so "nothing drawn here" sums to 161 a pixel and an
            # equality test would fail describing a mark that is not there.
            check("a missed bus carries no lateness mark",
                  int(near.max()) <= 24, "max %d" % near.max())


def test_margin_numbers():
    """The two numbers flanking a name are arrival minus walk, per direction."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        frame, lanes = rendered(cache)
        lane = by_name(lanes, "CONNECTICUT")
        # Every flow's first catchable bus is 0.5 min past its walk time, so
        # "leave in" is 0.5 min everywhere: under a minute, so NOW.
        want = muni.text_mask("NOW")
        for side in (muni.LEFT, muni.RIGHT):
            x0, x1 = lane.slot_left if side == muni.LEFT else lane.slot_right
            x = x1 - want.shape[1] if side == muni.LEFT else x0
            band = frame[lane.name_y:lane.name_y + muni.GLYPH_H]
            got = band[:, x:x + want.shape[1]].max(axis=2) > 40
            check("a half-minute margin prints NOW on the %s"
                  % ("left" if side == muni.LEFT else "right"),
                  np.array_equal(got, want))
        # The two numbers sit on opposite sides of the name, which is the only
        # thing that says which direction each is about.
        check("the two margins straddle the name",
              lane.slot_left[1] <= lane.slot_right[0])

        # Drive the clock back six minutes. The inbound 55's first bus was half
        # a minute inside a 3.5-minute walk; six minutes earlier it is nine
        # minutes out, which is 5.5 minutes of margin, and that prints as 5 --
        # the panel floors, it does not round, because rounding 5.5 up to 6
        # would tell somebody they had longer than they do.
        frame, lanes = rendered(cache, now=NOW - 360.0)
        lane = by_name(lanes, "CONNECTICUT")
        want = muni.text_mask("5")
        x0, x1 = lane.slot_left
        band = frame[lane.name_y:lane.name_y + muni.GLYPH_H]
        got = band[:, x1 - want.shape[1]:x1].max(axis=2) > 40
        check("margin is floored, not rounded", np.array_equal(got, want))

        # A direction with nothing catchable inside the horizon says when the
        # next one is rather than going blank, and the slot was sized for it.
        check("the margin slot is wide enough for a clock time",
              muni.MARGIN_W >= muni.text_width("00:00"))


def test_horizon_flag():
    """--horizon rescales the approach, the posts and which buses are on it."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        frame, lanes = rendered(cache, horizon=40.0)
        lane = by_name(lanes, "16TH/WISCONSIN")
        for flow in lane.flows:
            want = int(round(muni.CX
                             + flow.side * flow.walk_min * ppm_for(40.0)))
            got = post_column(frame, lane, flow.side)
            check("--horizon moves the %s post" % muni.DIR_OF[flow.dir],
                  abs(got - want) <= 1, "want %d got %d" % (want, got))


def test_motion():
    """Buses close on the stop as t advances, and the panel is pure in t."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        render = muni.build(opts(cache_dir=cache))
        lane = by_name(render.lanes, "16TH/WISCONSIN")
        for side in (muni.LEFT, muni.RIGHT):
            a = bus_columns(side_band(render(0.0, 0), lane, side), side)
            b = bus_columns(side_band(render(60.0, 1200), lane, side), side)
            check("the %s side moves"
                  % ("left" if side == muni.LEFT else "right"), a != b)
            # Converging: the left half moves right, the right half moves left.
            if side == muni.LEFT:
                check("the left side closes on the stop", max(b) > max(a),
                      "%d -> %d" % (max(a), max(b)))
            else:
                check("the right side closes on the stop", min(b) < min(a),
                      "%d -> %d" % (min(a), min(b)))

        r1 = muni.build(opts(cache_dir=cache))
        r2 = muni.build(opts(cache_dir=cache))
        cold = r1(9.15, 183)
        for i in range(184):
            warm = r2(i / 20.0, i)
        check("render is a pure function of t", np.array_equal(cold, warm))


def test_sizes():
    """Every frame is the shape and dtype the wall expects."""
    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache)
        for kw in ({}, {"source": "schedule"}):
            frame = muni.build(opts(cache_dir=cache, **kw))(3.0, 60)
            check("frame shape %s" % (kw or "live"),
                  frame.shape == (64, 320, 3) and frame.dtype == np.uint8,
                  "%s %s" % (frame.shape, frame.dtype))


def test_no_network():
    """The demo module must not have pulled a network library in with it."""
    for mod in ("urllib.request", "http.client", "socket", "requests",
                "ssl"):
        check("muni.py does not import %s" % mod,
              mod not in sys.modules or mod == "socket",
              "in sys.modules")


def test_schedule_fallback():
    """No live record: the timetable, and it must not look tracked."""
    # First the mechanism, which is one array per size: the hollow sprite must
    # actually be hollow and the solid one solid, at the same place. Both bus
    # heights are checked, because the near rows and the far rows use different
    # silhouettes and only one of them would have been noticed by eye.
    sprites = muni._bus_sprites()
    for h in (3, 4):
        for route in ("19", "22", "55"):
            for side in (muni.LEFT, muni.RIGHT):
                solid = sprites[(h, route, muni.NEXT, True, side)][0]
                hollow = sprites[(h, route, muni.NEXT, False, side)][0]
                check("%s h%d %s solid sprite has a filled body"
                      % (route, h, side),
                      int(solid[1:h - 1, 1:8].max()) > 40)
                check("%s h%d %s hollow sprite has an empty body"
                      % (route, h, side),
                      int(hollow[1:h - 1, 1:8].max()) == 0)
                check("%s h%d %s hollow sprite still has a roof"
                      % (route, h, side),
                      int(hollow[0, 1:8].max()) > 20)
        # The mirror has to be a mirror, or a bus reads as reversing into its
        # stop: the bright leading edge must be at the front on each side.
        left = sprites[(h, "19", muni.NEXT, True, muni.LEFT)][0]
        right = sprites[(h, "19", muni.NEXT, True, muni.RIGHT)][0]
        check("h%d sprites are mirrors of each other" % h,
              np.array_equal(left, right[:, ::-1]))
        check("h%d leading edge faces the way it travels" % h,
              int(left[1, -1].max()) > int(left[1, 0].max())
              and int(right[1, 0].max()) > int(right[1, -1].max()))

    with tempfile.TemporaryDirectory() as cache:
        synthetic(cache, live=False)
        # Everything the timetable produces is unmonitored by construction.
        geom = ftdata.load("muni-18th", cache)[0]
        rows = muni.scheduled(geom, geom["stops"][0], NOW)
        check("timetable produces buses at all", len(rows) > 0)
        check("no timetabled bus claims to be monitored",
              all(not mon for _when, _aim, mon in rows))
        check("no timetabled bus claims a lateness",
              all(aim is None for _when, aim, _mon in rows))

        frame, lanes = rendered(cache)
        check("timetable still draws a street", frame.max() > 60)

        # And in pixels: every drawn bus must have a dark middle. The roof row
        # is lit for both sprites, so runs are found there and then probed one
        # row down, at the centre, where only a solid bus has anything.
        checked = filled = 0
        for lane in lanes:
            for flow in lane.flows:
                band = side_band(frame, lane, flow.side)
                lit = band[0].max(axis=1) > 24
                x = 0
                while x < len(lit):
                    if not lit[x]:
                        x += 1
                        continue
                    end = x
                    while end < len(lit) and lit[end]:
                        end += 1
                    if end - x >= 5:
                        mid = (x + end) // 2
                        checked += 1
                        if int(band[1:lane.bus_h - 1, mid].max()) > 24:
                            filled += 1
                    x = end
        check("some timetable buses were actually found", checked >= 6,
              "found %d" % checked)
        check("no timetable bus is drawn solid", filled == 0,
              "%d of %d had filled bodies" % (filled, checked))


def _one_state(state, cache_dir):
    """Render one data state in this process; called by the child below."""
    args = opts(cache_dir=cache_dir)
    frame = muni.build(args)(1.0, 20)
    assert frame.shape == (64, 320, 3) and frame.dtype == np.uint8
    return "%s ok max=%d" % (state, frame.max())


def test_states_in_separate_processes():
    """Fresh, stale and absent, each in its own interpreter.

    ftdata.CACHE_DIR binds at import, and muni.py reads two products. Testing
    these by mutating a cache in one process would pass while the deployed
    thing failed.
    """
    root = tempfile.mkdtemp()
    try:
        cases = {}
        fresh = os.path.join(root, "fresh")
        synthetic(fresh)
        cases["fresh"] = fresh

        stale = os.path.join(root, "stale")
        synthetic(stale, fetched_ago=6 * 86400.0, live_ago=6 * 86400.0)
        cases["stale"] = stale

        absent = os.path.join(root, "absent")
        os.makedirs(absent)
        cases["absent"] = absent

        expired = os.path.join(root, "expired")
        synthetic(expired, live=False, expired=True)
        cases["expired"] = expired

        for state, cache in sorted(cases.items()):
            # Re-running this file with a flag, rather than exec'ing a slice of
            # its own source in a child. The slicing version worked until the
            # file grew a string containing the word it split on, and then
            # failed with a SyntaxError that said nothing about the real cause.
            env = dict(os.environ, FT_DATA_CACHE=cache, FT_DATA_BLOBS=cache)
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--render-once", cache, "--state", state],
                env=env, capture_output=True, text=True)
            check("state %s renders in its own process" % state,
                  proc.returncode == 0,
                  (proc.stderr or "").strip().split("\n")[-1][:160])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_fetcher_helpers():
    """The reductions in ftdata.py, which no rendering check would catch."""
    check("headsign shortening", ftdata._muni_short("Beach Street") == "BEACH",
          ftdata._muni_short("Beach Street"))
    check("ampersand headsign",
          ftdata._muni_short("16th St & Mission") == "16TH/MSN",
          ftdata._muni_short("16th St & Mission"))
    check("UCSF is named, not truncated",
          ftdata._muni_short("UCSF Mission Bay") == "UCSF")
    check("a label always fits the gutter",
          all(len(ftdata._muni_short(s)) <= 8
              for s in ("Beach Street", "Shipyard", "UCSF Mission Bay",
                        "16th St & Mission", "20th St & 3rd St", "",
                        "Somewhere Extremely Long Indeed")))
    # GTFS times past 24:00 are a different service day, not a modulo.
    check("30:09 is 1809 minutes, not 9", ftdata._muni_hhmmss("30:09:00") == 1809)
    check("midnight is zero", ftdata._muni_hhmmss("00:00:00") == 0)
    check("junk time is None", ftdata._muni_hhmmss("nonsense") is None)
    # 511 timestamps are UTC with a Z, and must not be read as local.
    epoch = ftdata._muni_epoch("2026-08-12T17:34:54Z")
    check("511 timestamps parse as UTC",
          epoch is not None
          and time.strftime("%H:%M", time.gmtime(epoch)) == "17:34")
    check("absent timestamp is None", ftdata._muni_epoch(None) is None)
    # The key is read from the environment and never defaulted.
    saved = os.environ.pop("FT_511_KEY", None)
    try:
        check("no key means no key", ftdata.muni_live_key() is None)
        os.environ["FT_511_KEY"] = "  x  "
        check("a key is stripped", ftdata.muni_live_key() == "x")
    finally:
        os.environ.pop("FT_511_KEY", None)
        if saved is not None:
            os.environ["FT_511_KEY"] = saved
    # No secret may reach a record.
    check("the product description does not carry a key",
          "FT_511_KEY" in ftdata.PRODUCTS["muni-live"]["description"]
          or "$FT_511_KEY" in ftdata.PRODUCTS["muni-live"]["description"])


def test_request_budget():
    """Six stops on this interval must stay inside 511's sixty an hour.

    `is_due()` fires at nine tenths of the interval on purpose, so the worst
    case is what has to fit, not the nominal. Note that the six *stops* did not
    become four when the six rows did: the panel groups two stops onto one row,
    it does not stop asking 511 about them.
    """
    stops = len(ftdata.MUNI_LIVE_STOPS_FALLBACK)
    worst = stops * 3600.0 / (ftdata.MUNI_LIVE_INTERVAL * 0.9)
    check("worst-case request rate is inside the cap", worst <= 60.0,
          "%.1f/hour" % worst)
    check("worst-case request rate leaves headroom", worst <= 30.0,
          "%.1f/hour" % worst)
    check("the TTL outlasts the interval",
          ftdata.MUNI_LIVE_TTL > ftdata.MUNI_LIVE_INTERVAL)
    check("the fetcher still covers every stop the cron did",
          CRON_STOPS <= set(ftdata.MUNI_LIVE_STOPS_FALLBACK),
          "%s" % sorted(ftdata.MUNI_LIVE_STOPS_FALLBACK))


def test_live_record_routes(cache_dir):
    """The same assertions against a real fetched record, which is the point."""
    got = ftdata.load("muni-18th", cache_dir)
    if got is None:
        print("     (no live muni-18th record; skipping)")
        return
    stops = got[0].get("stops") or []
    pairs = set((s.get("route"), s.get("dir")) for s in stops)
    for route in ("19", "22", "55"):
        check("real record has route %s both ways" % route,
              (route, 0) in pairs and (route, 1) in pairs,
              "got %s" % sorted(str(p) for p in pairs))
    codes = set(str(s.get("code")) for s in stops)
    check("real record is a strict superset of the cron's stops",
          CRON_STOPS < codes, "missing %s" % sorted(CRON_STOPS - codes))
    # The 22 must be genuinely further than the other two, or the stop search
    # has quietly snapped it to something on 18th St that is not the 22.
    by_route = {}
    for s in stops:
        by_route[s["route"]] = min(by_route.get(s["route"], 1e9), s["metres"])
    if set(by_route) >= set(("19", "22", "55")):
        check("the 22's stop really is the far one",
              by_route["22"] > by_route["19"] and by_route["22"] > by_route["55"],
              "metres %s" % by_route)
    # And the layout the real record produces must still carry all three.
    render = muni.build(ds.options(muni, now=NOW, cache_dir=cache_dir))
    lanes = getattr(render, "lanes", [])
    if lanes:
        routes = set(f.route for l in lanes for f in l.flows)
        check("the real record draws all three routes",
              set(("19", "22", "55")) <= routes, "got %s" % sorted(routes))
        drawn = set(f.code for l in lanes for f in l.flows)
        check("the real layout keeps every cron stop", CRON_STOPS <= drawn,
              "missing %s" % sorted(CRON_STOPS - drawn))


def test_live_cache(cache_dir):
    """Against a real fetched pair, if there is one."""
    got = ftdata.load("muni-live", cache_dir)
    if got is None:
        print("     (no live muni-live record; skipping)")
        return
    payload, age = got
    check("real live record has stops", bool(payload.get("stops")))
    check("real live record has visits", payload.get("n_visits", 0) > 0)
    lines = set()
    for visits in (payload.get("stops") or {}).values():
        for v in visits:
            lines.add(v.get("line"))
            check("visit %s has an absolute expected time" % v.get("line"),
                  isinstance(v.get("exp"), (int, float)))
    check("real live record covers 19, 22 and 55",
          set(("19", "22", "55")) <= lines, "got %s" % sorted(lines))
    check("no vehicle identifier was cached",
          not any("VehicleRef" in v or "vehicle" in v
                  for visits in payload["stops"].values() for v in visits))
    args = opts(cache_dir=cache_dir, now=float(payload.get("t") or NOW) + 60.0)
    frame = muni.build(args)(0.0, 0)
    check("the real record draws", frame.shape == (64, 320, 3)
          and frame.max() > 60)
    del age


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--render-once", default=None,
                    help="internal: build and render one frame, then exit")
    ap.add_argument("--state", default="?", help="internal: label for the above")
    ap.add_argument("--time-offset", type=float, default=0.0,
                    help="drive the real clock forward by this many seconds")
    args = ap.parse_args()

    # Hermeticity, made runnable. The fixture writes `fetched_at` relative to
    # `time.time()` while the panel's own clock is pinned to NOW; if that ever
    # regresses to being relative to NOW, the record's apparent age grows with
    # the real clock and the live checks quietly become schedule checks. Moving
    # `time.time()` by an hour or a day and requiring the same result is the
    # cheapest possible proof that the two clocks are still separate.
    if args.time_offset:
        real = time.time
        offset = args.time_offset
        time.time = lambda: real() + offset
        print("(real clock moved on by %+.0f s)" % offset)

    if args.render_once:
        print(_one_state(args.state, args.render_once))
        return 0

    test_fetcher_helpers()
    test_request_budget()
    test_fixture_is_live()
    test_routes_present()
    test_cron_coverage()
    test_grouping()
    test_names()
    test_row_heights()
    test_posts_to_scale()
    test_directions_converge()
    test_catchable_boundary()
    test_shared_stop_does_not_leak()
    test_unmonitored_is_hollow()
    test_lateness_bar()
    test_margin_numbers()
    test_horizon_flag()
    test_motion()
    test_sizes()
    test_schedule_fallback()
    test_states_in_separate_processes()
    test_no_network()
    test_live_record_routes(args.cache_dir)
    test_live_cache(args.cache_dir)

    print("\n%d passed, %d failed" % (PASSED[0], len(FAILED)))
    for line in FAILED:
        print("  FAIL %s" % line)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
