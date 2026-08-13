#!/usr/bin/env python3
"""Checks for tonight.py that a screenshot cannot make.

A calendar panel is unusually easy to get wrong in ways that still look
perfectly good, and every check here exists because one of these would
otherwise have shipped:

  1. **Every event an hour late.** The feed stamps `-08:00` on a July evening
     and California is on -07:00 in July. Honour the offset and the whole panel
     slides one row down, which looks exactly as plausible as the right answer.
     So `_sequoia_cal_epoch` is asserted to put a 19:00 stamp at 19:00 local in
     August *and* in November -- the pair is the point, because a single case
     passes under either reading.
  2. **The urgent block drawn dimmer than the calm ones.** `C_SOON` is a pale
     colour and the body of a block is a fraction of its colour, so a single
     shared fraction made the one block somebody needs to see the faintest
     thing on the panel. That is asserted in pixels, by comparing the actual
     lit values of the imminent block against an ordinary one.
  3. **An event in the wrong column.** Placement goes through calendar dates
     rather than epoch division precisely so a DST weekend cannot shift it, and
     a synthetic calendar with events on known weekdays checks the columns
     land where the dates say.
  4. **A ruler that is not a ruler.** Two-letter labels wherever a cell rounds
     up and one wherever it rounds down reads as damage. The ruler is asserted
     to use one width for the whole span.
  5. **A record that parses, draws beautifully and is last month's.** The
     stale path has to say so on the panel, in words that can be read back off
     the pixels.

Two things about how these are run, both learned the hard way in this tree.
The demo is **not a pure function of `t` in the sense the scheduler cares
about** -- it is, but it takes `now` from the clock once in `build()` -- so
every check that looks at the picture pins `--now`, and the purity check
compares a cold `render(t0)` against the same `t0` driven frame by frame from
zero. And `ftdata.CACHE_DIR` binds at import, so the three data states are each
run in a **separate process** with FT_DATA_CACHE set, at the bottom of this
file; reloading the module in one process does not test what it looks like it
tests.

**Fixture ages are measured from `time.time()`, never from the pinned clock.**
`ftdata.load()` measures a record's age against the real clock whatever the
panel thinks the time is, and round 4's muni suite wrote fixtures whose ages
were relative to a pinned `now` -- they read fresh for half an hour of real
time and then went stale, turning a dozen assertions red in ways that looked
like drawing bugs. `_fixture_guard` fails loudly and once if that ever recurs.

    $ python3 scripts/test-tonight.py
    $ python3 scripts/test-tonight.py --shot ../screenshots/tonight.png

Nothing here needs the network or the live cache; every fixture is built in a
scratch directory. `--live` additionally checks the real cached record if there
is one, which is worth doing after
`python3 ftdata.py --once --only sequoia-calendar`.
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
import tonight                                                # noqa: E402

FAILED = []
PASSED = [0]

PRODUCT = "sequoia-calendar"


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-58s %s" % (name, detail))
    else:
        print("  FAIL %-58s %s" % (name, detail))
        FAILED.append(name)


# --------------------------------------------------------------------------
# Fixtures. A synthetic calendar whose answers cannot be argued with: three
# evenings on named weekdays at named hours, and one already finished.
# --------------------------------------------------------------------------

# A Wednesday. Everything below is relative to it, so the weekday assertions
# hold whenever this file is run.
BASE_DAY = "2026-08-12"


def at(day_offset, hour, minute=0, base=BASE_DAY):
    """Epoch for a local wall-clock moment `day_offset` days after BASE_DAY."""
    y, m, d = (int(v) for v in base.split("-"))
    noon = time.mktime((y, m, d, 12, 0, 0, 0, 0, -1)) + day_offset * 86400
    lt = time.localtime(noon)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                        hour, minute, 0, 0, 0, -1))


def calendar(events):
    return {"site": "SEQUOIA FABRICA", "n": len(events), "n_feed": len(events),
            "skipped": 0, "tz": "America/Los_Angeles", "tz_note": "test",
            "ev": events}


def ev(t, hours, name, all_day=0):
    return {"t": int(t), "d": int(hours * 3600), "a": all_day, "n": name}


# now is Wednesday 18:30 -- inside the evening window, with one event running.
NOW = at(0, 18, 30)

FIXTURE = calendar([
    ev(at(0, 12, 0), 1.0, "LUNCH REPAIR CLINIC"),        # today, already over
    ev(at(0, 18, 0), 3.0, "OPEN SHOP NIGHT"),            # today, running now
    ev(at(1, 19, 0), 2.0, "HAND EMBROIDERY SOCIAL"),     # Thursday 7pm
    ev(at(5, 18, 0), 2.5, "UPMENDING SOCIAL"),           # Monday 6pm
    ev(at(6, 19, 0), 1.0, "MEMBER APPLICANT ORIENTATION"),
])

# The same calendar seen forty minutes before the Thursday social starts.
NOW_SOON = at(1, 18, 20)


def write_record(cache_dir, payload, age_s=60.0):
    """A cache record whose age is measured from the *real* clock. See above."""
    os.makedirs(cache_dir, exist_ok=True)
    rec = {"name": PRODUCT, "fetched_at": time.time() - age_s,
           "source": "test-tonight", "ttl": ftdata.ttl_for(PRODUCT) or 21600,
           "payload": payload}
    with open(os.path.join(cache_dir, PRODUCT + ".json"), "w") as fh:
        json.dump(rec, fh)


_GUARDED = [False]


def _fixture_guard(cache_dir, want_fresh=True):
    """Fail once, loudly, if a fixture is not the freshness it was written as."""
    if _GUARDED[0]:
        return
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return
    fresh = ftdata.is_fresh(PRODUCT, got[1])
    if fresh != want_fresh:
        _GUARDED[0] = True
        check("fixture freshness", False,
              "wrote fresh=%s, load() says fresh=%s (age %.0fs) -- every "
              "picture check below is now testing the wrong path"
              % (want_fresh, fresh, got[1]))


def opts(**kw):
    kw.setdefault("now", "%.0f" % NOW)
    return ds.options(tonight, **kw)


def frames(args, n=60):
    """Render `n` frames in order from a fresh build. Never sample sparsely."""
    r = tonight.build(args)
    out = None
    for i in range(n):
        out = r(i / 20.0, i)
    return r, out.copy()


def settled(args, n=80):
    """A frame from after the reveal has finished, still rendered in order."""
    return frames(args, n)


def contains_text(frame, s, thresh=100, scales=(1, 2)):
    """Is this string drawn anywhere on the frame, at any position or size?

    Reading the words back off the panel is the only way to be sure the honest
    message reached it rather than merely being computed. Strokes-on only, with
    no background test: unlike caiso this panel has no large blocks of lit
    colour -- its brightest regions are event blocks a dozen columns wide, far
    too narrow to contain a whole string by accident.
    """
    lit = frame.max(axis=2) >= thresh
    h, w = lit.shape
    for scale in scales:
        m = tonight.text_mask(s, scale)
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
# The fetcher's arithmetic, which never touches the panel.
# --------------------------------------------------------------------------

def test_fetch_parsing():
    print("\nthe feed, as parsed")

    # The whole timezone argument in two assertions. A fixed -08:00 is correct
    # in November and an hour wrong in August, so one case proves nothing and
    # the pair proves everything: only the wall-clock reading puts a recurring
    # 19:00 orientation at 7 pm on both sides of the DST boundary.
    aug = ftdata._sequoia_cal_epoch("2026-08-18T19:00:00-08:00")
    nov = ftdata._sequoia_cal_epoch("2026-11-03T19:00:00-08:00")
    check("august 19:00 stamp is 19:00 local", aug is not None
          and time.localtime(aug).tm_hour == 19,
          time.strftime("%Y-%m-%d %H:%M", time.localtime(aug)) if aug else "")
    check("november 19:00 stamp is 19:00 local", nov is not None
          and time.localtime(nov).tm_hour == 19,
          time.strftime("%Y-%m-%d %H:%M", time.localtime(nov)) if nov else "")
    check("the recurring 7pm does not move across DST",
          aug is not None and nov is not None
          and time.localtime(aug).tm_hour == time.localtime(nov).tm_hour)
    check("a stamp that is not a stamp is None",
          ftdata._sequoia_cal_epoch("soon") is None
          and ftdata._sequoia_cal_epoch("") is None
          and ftdata._sequoia_cal_epoch(None) is None)

    for raw, want in (
            ("Hand Embroidery Social", "HAND EMBROIDERY SOCIAL"),
            ("Upmending (upcycling + mending) Social", "UPMENDING SOCIAL"),
            ("Crochet & Knitting Social", "CROCHET / KNITTING SOCIAL"),
            ("Let's make BioYarn!", "LET'S MAKE BIOYARN!"),
            ("  spaced   out  ", "SPACED OUT"),
            ("unclosed (paren", "UNCLOSED"),
    ):
        got = ftdata._sequoia_cal_title(raw)
        check("title %r" % raw[:28], got == want, "-> %r" % got)

    long_title = "A " + "VERY " * 20 + "LONG NAME"
    got = ftdata._sequoia_cal_title(long_title)
    check("a long title is capped on a word boundary",
          len(got) <= ftdata.SEQUOIA_CAL_TITLE_MAX and not got.endswith(" ")
          and " " in got, "%d chars" % len(got))

    # What the demo does to a title is font work, and it is the demo's.
    check("the demo drops what its font cannot draw",
          tonight.sanitise("LET'S MAKE BIOYARN!") == "LETS MAKE BIOYARN",
          tonight.sanitise("LET'S MAKE BIOYARN!"))


# --------------------------------------------------------------------------
# Geometry: which column, which rows.
# --------------------------------------------------------------------------

def test_placement():
    print("\nwhere an event lands")
    tmp = tempfile.mkdtemp(prefix="tonight-place")
    try:
        write_record(tmp, FIXTURE)
        _fixture_guard(tmp)
        r, _ = settled(opts(cache_dir=tmp))
        st = r.state
        field = st["field"]

        check("three weeks by default", field.n_days == 21,
              "%d days" % field.n_days)

        # The window follows the events rather than being chosen. This fixture
        # deliberately has a midday clinic in it, so it must open at 11 -- and
        # the evening-only calendar the real feed actually produces must give
        # the tight 17..22 the whole design assumes. Both, because the first
        # alone would pass on a hardcoded window and the second alone would not
        # notice a daytime class being squeezed off the top of the panel.
        check("a midday event widens the window",
              (field.lo_h, field.hi_h) == (11, 22),
              "%d..%d" % (field.lo_h, field.hi_h))
        evenings = calendar([e for e in FIXTURE["ev"]
                             if "LUNCH" not in e["n"]])
        write_record(tmp, evenings)
        r_ev, _ = settled(opts(cache_dir=tmp))
        check("an evenings-only calendar gives the tight window",
              (r_ev.state["field"].lo_h, r_ev.state["field"].hi_h) == (17, 22),
              "%d..%d" % (r_ev.state["field"].lo_h, r_ev.state["field"].hi_h))
        write_record(tmp, FIXTURE)

        by_name = {e["name"]: e for e in st["rec"]["events"]}
        for name, want_day, want_hour in (
                ("OPEN SHOP NIGHT", 0, 18),
                ("HAND EMBROIDERY SOCIAL", 1, 19),
                ("UPMENDING SOCIAL", 5, 18),
                ("MEMBER APPLICANT ORIENTATION", 6, 19),
        ):
            e = by_name[name]
            day, y0, y1, _state = tonight.place(field, e, NOW)
            hour = field.hour_at(y0)
            check("%s in column %d" % (name[:22], want_day), day == want_day,
                  "day %s" % day)
            check("%s starts at %dh" % (name[:22], want_hour),
                  abs(hour - want_hour) < 0.5, "row %d = %.2fh" % (y0, hour))
            check("%s is %d rows for %.1fh" % (name[:18], y1 - y0,
                                               e["dur"] / 3600.0),
                  abs((y1 - y0) / field.rows_per_hour
                      - e["dur"] / 3600.0) < 0.3)

        # A DST-proof placement: the same clock hour on either side of the
        # change must land on the same row, which epoch division would not do.
        nov = calendar([ev(at(0, 19, 0, base="2026-10-28"), 2.0, "BEFORE"),
                        ev(at(0, 19, 0, base="2026-11-04"), 2.0, "AFTER")])
        write_record(tmp, nov)
        r2, _ = settled(opts(cache_dir=tmp, now="%.0f" % at(0, 18, 0,
                                                            base="2026-10-28")))
        f2 = r2.state["field"]
        rows = [tonight.place(f2, e, 0)[1] for e in r2.state["rec"]["events"]]
        check("two 7pm events across the DST change share a row",
              len(rows) == 2 and rows[0] == rows[1], "rows %r" % (rows,))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_state_and_brightness():
    print("\nurgency, in pixels")
    tmp = tempfile.mkdtemp(prefix="tonight-bright")
    try:
        write_record(tmp, FIXTURE)
        _fixture_guard(tmp)

        # Forty minutes before the Thursday social. That block must be the
        # brightest event on the panel, and the check is on the *static* image
        # so the pulse phase cannot make it pass or fail by luck.
        r, _ = settled(opts(cache_dir=tmp, now="%.0f" % NOW_SOON))
        st, field = r.state, r.state["field"]
        check("the headline counts down", st["when"].startswith("IN ")
              and "MIN" in st["when"], st["when"])
        check("and it pulses", st["pulse"] is True)

        by_name = {e["name"]: e for e in st["rec"]["events"]}
        lit = {}
        for name in ("HAND EMBROIDERY SOCIAL", "UPMENDING SOCIAL",
                     "MEMBER APPLICANT ORIENTATION"):
            day, y0, y1, state = tonight.place(field, by_name[name], NOW_SOON)
            x0, x1 = field.cell(day)
            # The body, not the bright start edge: the bug was in the body.
            band = r.static[y0 + 2:y1, x0 + 2:x1 - 1]
            lit[name] = float(band.mean()) if band.size else 0.0
            if name == "HAND EMBROIDERY SOCIAL":
                check("the imminent block is drawn as SOON", state == "soon")
        soon = lit["HAND EMBROIDERY SOCIAL"]
        calm = max(lit["UPMENDING SOCIAL"],
                   lit["MEMBER APPLICANT ORIENTATION"])
        check("the imminent block is brighter than the calm ones",
              soon > calm * 1.5, "soon %.0f vs calm %.0f" % (soon, calm))

        # Yesterday is not on the panel at all: the field starts at today, so
        # an event that ran last night has no column to be drawn in even though
        # the fetcher still carries it. That is the intended answer and it is
        # asserted rather than assumed, because the alternative -- silently
        # binning it into today's column -- is exactly the kind of off-by-one
        # that would look plausible.
        check("yesterday's event has no column",
              tonight.place(field, by_name["OPEN SHOP NIGHT"], NOW_SOON)
              is None)

        # A finished event *today* is dimmer than a calm one, so the ramp runs
        # the whole way in the right direction. Seen from Wednesday evening,
        # where the midday clinic is over and still on the panel.
        r2, _ = settled(opts(cache_dir=tmp))
        f2 = r2.state["field"]
        n2 = {e["name"]: e for e in r2.state["rec"]["events"]}
        day, y0, y1, state = tonight.place(f2, n2["LUNCH REPAIR CLINIC"], NOW)
        x0, x1 = f2.cell(day)
        past = float(r2.static[y0 + 1:y1, x0 + 2:x1 - 1].mean())
        d2, u0, u1, s2 = tonight.place(f2, n2["UPMENDING SOCIAL"], NOW)
        ux0, ux1 = f2.cell(d2)
        calm2 = float(r2.static[u0 + 1:u1, ux0 + 2:ux1 - 1].mean())
        check("a finished event is drawn as PAST", state == "past")
        check("an upcoming one is drawn as NEXT", s2 == "next")
        check("and the finished one is the dimmer", past < calm2,
              "past %.0f vs calm %.0f" % (past, calm2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_now_line():
    print("\nthe present moment")
    tmp = tempfile.mkdtemp(prefix="tonight-now")
    try:
        write_record(tmp, FIXTURE)
        _fixture_guard(tmp)

        # Inside the window: a bright line at the right hour, in today's cell
        # only, with the hours above it darkened.
        r, _ = settled(opts(cache_dir=tmp))
        field = r.state["field"]
        row, x0, x1 = r.state["nowline"]
        check("the now-line is inside the window", r.state["now_inside"])
        check("the now-line is at 18:30",
              abs(field.hour_at(row) - 18.5) < 0.4,
              "%.2fh" % field.hour_at(row))
        check("the now-line is only in today's cell",
              (x0, x1) == field.cell(0), "%d..%d" % (x0, x1))
        check("it is bright", r.static[row, x0 + 3].max() > 180,
              str(tuple(int(v) for v in r.static[row, x0 + 3])))

        # The spent hours of today are darker than the same hours tomorrow.
        band = slice(field.lay.grid_y0 + 1, row - 1)
        today = float(r.static[band, x0 + 4].mean())
        tomorrow = float(r.static[band, field.cell(1)[0] + 4].mean())
        check("today's spent hours are darkened", today < tomorrow * 0.7,
              "%.1f vs %.1f" % (today, tomorrow))

        # Outside the window -- nine in the morning, and this fixture's window
        # opens at eleven because of its midday clinic -- the line clamps to
        # the top edge and stops claiming to be an hour.
        r2, _ = settled(opts(cache_dir=tmp, now="%.0f" % at(0, 9, 0)))
        check("before the evening the line is clamped and plain",
              not r2.state["now_inside"]
              and r2.state["nowline"][0] == r2.layout.grid_y0)

        # The pip moves every frame, which is the whole reason it exists.
        rr = tonight.build(opts(cache_dir=tmp))
        seen = set()
        prev = None
        moved = 0
        for i in range(60, 100):
            f = rr(i / 20.0, i)
            strip = f[row, x0:x1].copy()
            seen.add(strip.tobytes())
            if prev is not None and not np.array_equal(strip, prev):
                moved += 1
            prev = strip
        check("the pip moves on almost every frame", moved >= 30,
              "%d of 39 frames changed, %d distinct" % (moved, len(seen)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_words():
    print("\nthe words, read back off the panel")
    tmp = tempfile.mkdtemp(prefix="tonight-words")
    try:
        write_record(tmp, FIXTURE)
        _fixture_guard(tmp)
        _r, f = settled(opts(cache_dir=tmp))
        check("the site names itself", contains_text(f, "SEQUOIA FABRICA"))
        check("ON NOW is on the panel", contains_text(f, "ON NOW"))
        check("so is what is on", contains_text(f, "OPEN SHOP NIGHT"))
        check("and how packed the span is", contains_text(f, "IN 3 WEEKS"))

        r2, f2 = settled(opts(cache_dir=tmp, now="%.0f" % NOW_SOON))
        check("the countdown reaches the panel",
              contains_text(f2, r2.state["when"]), r2.state["when"])

        # Tomorrow evening, from this afternoon.
        r3, f3 = settled(opts(cache_dir=tmp, now="%.0f" % at(0, 22, 30)))
        check("after tonight it says tomorrow",
              r3.state["when"].startswith("TOMORROW")
              and contains_text(f3, r3.state["when"]), r3.state["when"])

        # An empty calendar is a state, not a fault.
        write_record(tmp, calendar([]))
        r4, f4 = settled(opts(cache_dir=tmp))
        check("an empty calendar says so", contains_text(f4, "NOTHING ON")
              and contains_text(f4, "THE CALENDAR IS CLEAR"))
        check("and still draws three weeks of evenings",
              r4.state["field"].n_days == 21 and f4.max() > 40)
        check("and counts zero", r4.state["count"].startswith("0 IN"),
              r4.state["count"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_span_and_ruler():
    print("\nthe axis")
    tmp = tempfile.mkdtemp(prefix="tonight-span")
    try:
        # Nothing for five weeks: the span has to grow rather than draw an
        # empty panel or silently change scale without saying so.
        far = calendar([ev(at(38, 19, 0), 2.0, "FAR OFF SOCIAL"),
                        ev(at(40, 18, 0), 2.0, "ALSO FAR OFF")])
        write_record(tmp, far)
        _fixture_guard(tmp)
        r, f = settled(opts(cache_dir=tmp))
        check("the span grows past an empty three weeks",
              r.state["field"].n_days > 21 and r.state["field"].n_days % 7 == 0,
              "%d days" % r.state["field"].n_days)
        check("the span reaches the next event",
              r.state["field"].n_days * 86400 > 38 * 86400)
        check("and the header says how many weeks",
              "WEEKS" in r.state["count"], r.state["count"])
        check("the far event is still drawn",
              tonight.place(r.state["field"],
                            r.state["rec"]["events"][0], NOW) is not None)

        # The ruler uses one label width for the whole span, at both spans.
        for cache_payload, want_days in ((FIXTURE, 21), (far, None)):
            write_record(tmp, cache_payload)
            rr, ff = settled(opts(cache_dir=tmp))
            fld = rr.state["field"]
            lay = rr.layout
            widths = set()
            for d in range(fld.n_days):
                x0, x1 = fld.cell(d)
                strip = ff[lay.day_y:lay.day_y + tonight.GLYPH_H, x0:x1]
                cols = int((strip.max(axis=2).max(axis=0) > 30).sum())
                if cols:
                    widths.add(cols)
            check("ruler labels are one width across %d days" % fld.n_days,
                  len(widths) <= 2, "lit column counts %r" % (sorted(widths),))

        # An explicit span is honoured.
        write_record(tmp, FIXTURE)
        r2, _ = settled(opts(cache_dir=tmp, span=14))
        check("--span is honoured", r2.state["field"].n_days == 14,
              "%d" % r2.state["field"].n_days)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_purity():
    print("\npurity")
    tmp = tempfile.mkdtemp(prefix="tonight-pure")
    try:
        write_record(tmp, FIXTURE)
        _fixture_guard(tmp)
        for label, kw in (("calm", {}), ("pulsing", {"now": "%.0f" % NOW_SOON})):
            ok = True
            for t0 in (0.35, 1.5, 2.4, 5.05, 7.15):
                a = tonight.build(opts(cache_dir=tmp, **kw))(t0, int(t0 * 20))
                r = tonight.build(opts(cache_dir=tmp, **kw))
                out = None
                for i in range(int(t0 * 20) + 1):
                    out = r(i / 20.0, i)
                if not np.array_equal(a, out):
                    ok = False
                    break
            check("render(%s) is a pure function of t" % label, ok)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sizes():
    print("\nsizes")
    tmp = tempfile.mkdtemp(prefix="tonight-size")
    try:
        write_record(tmp, FIXTURE)
        _fixture_guard(tmp)
        for w, h in ((320, 64), (256, 64), (512, 128), (192, 48), (320, 32),
                     (320, 16), (128, 32), (64, 32)):
            try:
                r, f = frames(opts(cache_dir=tmp, width=w, height=h), 40)
                lay = r.layout
                ok = (f.shape == (h, w, 3) and f.dtype == np.uint8
                      and f.max() > 0 and lay.grid_y1 > lay.grid_y0)
                detail = "grid %d rows, head %d, headline %d, ruler %d" % (
                    lay.grid_h, lay.head_h, lay.hl_h, lay.day_h)
            except Exception as e:                           # noqa: BLE001
                ok, detail = False, repr(e)[:70]
            check("%dx%d renders" % (w, h), ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_malformed():
    print("\nrecords that are wrong rather than absent")
    tmp = tempfile.mkdtemp(prefix="tonight-bad")
    try:
        for label, payload in (
                ("ev is not a list", {"ev": "nope"}),
                ("no ev at all", {"site": "X"}),
                ("an event with no start", calendar([{"d": 3600, "n": "X"}])),
                ("an event with no name", calendar([ev(at(1, 19), 1.0, "")])),
                ("a negative duration", calendar([ev(at(1, 19), -3.0, "X")])),
        ):
            write_record(tmp, payload)
            try:
                _r, f = frames(opts(cache_dir=tmp), 30)
                # bool() around it deliberately: `f.max() > 0` is a numpy
                # bool_, `True and np.bool_(True)` is a numpy bool_, and
                # `np.bool_(True) is True` is False. An identity test against
                # True on a numpy comparison is always a lie.
                ok = bool(f.shape[2] == 3 and f.max() > 0)
            except Exception as e:                           # noqa: BLE001
                ok = "raised %r" % e
            check("%s draws something" % label, ok is True, "" if ok is True
                  else str(ok))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# The three data states, each in its own process. See the docstring.
# --------------------------------------------------------------------------

def _one_state(state):
    """The child half. `cache_dir=None` on purpose: the point of this process
    is that it exercises the path a real run takes, where the cache comes from
    `ftdata.CACHE_DIR` -- which bound to $FT_DATA_CACHE at import, before this
    function existed. The parent is what put the record there."""
    r, f = settled(opts(cache_dir=None))
    said_stale = contains_text(f, "STALE")
    bits = ["cache=%s" % ftdata.CACHE_DIR, "max=%d" % f.max()]
    if state == "absent":
        ok = (r.state["rec"] is None
              and contains_text(f, "NO CALENDAR YET"))
        bits.append("card=%s" % contains_text(f, "NO CALENDAR YET"))
    elif state == "stale":
        ok = (r.state["stale"] is True and said_stale
              and r.state["field"] is not None
              and contains_text(f, "ON NOW"))
        bits.append("stale=%s says-so=%s draws=%s"
                    % (r.state["stale"], said_stale, r.state["field"] is not None))
    else:
        ok = (r.state["stale"] is False and not said_stale
              and contains_text(f, "ON NOW"))
        bits.append("stale=%s" % r.state["stale"])
    print("RESULT %s %s %s" % (state, "ok" if ok else "FAIL", " ".join(bits)))
    return 0 if ok else 1


def test_states_in_separate_processes():
    """Fresh, stale and absent, each in a process of its own.

    `ftdata.CACHE_DIR` reads $FT_DATA_CACHE **at import**, so setting the
    variable inside an already-running interpreter changes nothing -- the first
    version of this did exactly that, and all three states silently read the
    developer's real cache instead. The variable therefore goes into the
    child's environment before it starts, which is also the only arrangement
    that tests what the wall actually does.
    """
    print("\nthe three data states, one process each")
    ttl = ftdata.ttl_for(PRODUCT) or 21600
    for state in ("fresh", "stale", "absent"):
        tmp = tempfile.mkdtemp(prefix="tonight-state-" + state)
        try:
            if state == "fresh":
                write_record(tmp, FIXTURE, age_s=90.0)
            elif state == "stale":
                write_record(tmp, FIXTURE, age_s=ttl * 2.0)
            # absent: write nothing at all.
            env = dict(os.environ)
            env["FT_DATA_CACHE"] = tmp
            env["FT_DATA_BLOBS"] = tmp
            out = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--state", state],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            line = ""
            for raw in out.stdout.decode("utf-8", "replace").splitlines():
                if raw.startswith("RESULT "):
                    line = raw
            check("%s state" % state, " ok " in line,
                  line[7:] if line else
                  out.stdout.decode("utf-8", "replace")[-160:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_no_network():
    print("\nthe network promise")
    before = set(sys.modules)
    ftdata.load(PRODUCT, tempfile.mkdtemp(prefix="tonight-net"))
    new = set(sys.modules) - before
    bad = [m for m in new
           if m.split(".")[0] in ("urllib", "http", "socket", "ssl",
                                  "requests")]
    check("ftdata.load() imports no network module", not bad, ",".join(bad))
    src = open(os.path.join(HERE, "tonight.py")).read()
    imported = [m for m in ("urllib", "http.client", "socket", "requests",
                            "ssl") if ("import " + m) in src]
    check("tonight.py does not import one either", not imported,
          ",".join(imported))


def test_live(cache_dir):
    print("\nthe real cached record, if there is one")
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        print("  --   no %s in %s; run ftdata.py --once --only %s"
              % (PRODUCT, cache_dir, PRODUCT))
        return
    payload, age = got
    rec, _age, problem = tonight.read_calendar(cache_dir)
    check("the live record parses", rec is not None, problem or "")
    if rec is None:
        return
    n = len(rec["events"])
    check("it has events in it", n > 0, "%d events, %s old"
          % (n, ftdata.describe_age(age)))
    check("they are sorted", all(rec["events"][i]["t"] <= rec["events"][i + 1]["t"]
                                 for i in range(n - 1)))
    check("every one is in the evening or flagged all-day",
          all(e["all"] or 0 <= (e["t"] - tonight.local_midnight(e["t"])) < 86400
              for e in rec["events"]))
    check("no url survived the fetcher",
          all("url" not in raw for raw in payload["ev"]))
    r, f = settled(ds.options(tonight, cache_dir=cache_dir))
    check("and the live panel draws", f.max() > 0,
          "%s | %s" % (r.state["when"], r.state["count"]))


# --------------------------------------------------------------------------
# Screenshot.
# --------------------------------------------------------------------------

def write_shot(path, cache_dir=None, at_t=2.4, **kw):
    """A 3x screenshot, 960x192 from the 320x64 panel.

    t = 2.4 s is deliberate: it is past the reveal and it is a peak of the
    1.35 Hz pulse, so the urgent states are captured at their brightest rather
    than at whatever phase a round number of seconds happened to land on.
    """
    from PIL import Image
    tmp = None
    if cache_dir is None:
        tmp = tempfile.mkdtemp(prefix="tonight-shot")
        write_record(tmp, FIXTURE)
        cache_dir = tmp
        kw.setdefault("now", "%.0f" % NOW_SOON)
    try:
        r = tonight.build(opts(cache_dir=cache_dir, **kw))
        out = None
        for i in range(int(at_t * 20) + 1):
            out = r(i / 20.0, i)
        im = Image.fromarray(np.asarray(out, np.uint8).copy(), "RGB")
        im = im.resize((im.width * 3, im.height * 3), Image.NEAREST)
        im.save(path)
        print("wrote %s (%dx%d)  %s | %s | %s"
              % (path, im.width, im.height, r.state["when"], r.state["name"],
                 r.state["count"]))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", default=ftdata.CACHE_DIR)
    ap.add_argument("--shot", default="", help="write a 3x screenshot")
    ap.add_argument("--shot-live", default="",
                    help="write a 3x screenshot off the real cache")
    ap.add_argument("--at", default="now",
                    help="the moment --shot-live pins, so the screenshot can "
                         "be the real record at a chosen minute rather than a "
                         "simulation of one")
    ap.add_argument("--live", action="store_true",
                    help="also check the real cached record")
    ap.add_argument("--state", default="",
                    choices=("", "fresh", "stale", "absent"),
                    help="internal: run one data state and print RESULT")
    a = ap.parse_args()
    if a.state:
        return _one_state(a.state)

    if a.shot:
        write_shot(a.shot)
    if a.shot_live:
        write_shot(a.shot_live, cache_dir=a.cache_dir, now=a.at)
    if a.shot or a.shot_live:
        return 0

    test_no_network()
    test_fetch_parsing()
    test_placement()
    test_state_and_brightness()
    test_now_line()
    test_words()
    test_span_and_ruler()
    test_purity()
    test_malformed()
    test_sizes()
    test_states_in_separate_processes()
    if a.live:
        test_live(a.cache_dir)

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  - %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
