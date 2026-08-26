#!/usr/bin/env python3
"""Checks for opencircuit.py that a screenshot cannot make.

Three kinds of thing are asserted here, and only the first is about pixels.

**That it is one tube.** The whole conceit of this panel is that every pixel on
it is the same phosphor at a different intensity. That is easy to state and
easy to break with a single stray blend, and it is invisible in a screenshot
because a wrong colour among five hundred right ones looks like a wrong colour
among five hundred right ones only if you are looking for it. Since the ramp is
uint8 and the dither is applied to the index rather than to the colour, every
pixel the panel can emit is *exactly* one of the 256 ramp entries -- so the
assertion is set membership, with no tolerance, in every frame of a whole loop.

**That it is a pure function of t.** ftsched builds a segment on a worker
thread and starts it at t=0, the preview baker steps it at a fixed rate, and
the wall's loop drifts. So two independently built callbacks asked for the same
t must produce the same bytes, and the same callback asked for a t it has
already passed must reproduce it. A phosphor buffer would quietly break this,
which is exactly why there is not one.

**That a bad record cannot put a date on the wall.** This is the assertion that
matters. The failure worth designing against is not a crash -- it is the panel
confidently advertising a class from a record that is a fortnight stale, which
looks completely normal. The pages are checked as *words*: with an absent,
empty or stale cache, no page may carry the workshop's title or its date, and
the page that replaces it must name the fetcher. `ftdata.CACHE_DIR` binds at
import, so each cache state is set up as its own directory and passed through
`--cache-dir` rather than by reimporting the module.

    $ python3 scripts/test-opencircuit.py
"""

import json
import os
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import demoscene as ds                                        # noqa: E402
import ftdata                                                 # noqa: E402
import opencircuit as oc                                      # noqa: E402

FAILED = []
PASSED = [0]

# A moment when the fixture's first workshop is nine days out, so the
# countdown, the day name and the "then" page all have something to say.
NOW = 1787716000.0

TITLE = "INTRODUCTION TO SOLDERING"

FIXTURE = {
    "org": "OPEN CIRCUIT SF",
    "host": "OPENCIRCUITSF.COM",
    "n": 2, "n_feed": 2, "skipped": 0,
    "ev": [
        {"t": 1788485400, "d": 9000, "n": TITLE, "v": "SEQUOIA FABRICA",
         "a": "1736 18TH STREET", "w": "FOOT OF POTRERO HILL",
         "cap": 8, "topic": "SOLDERING / ASSEMBLY"},
        {"t": 1790904600, "d": 9000, "n": TITLE, "v": "SEQUOIA FABRICA",
         "a": "1736 18TH STREET", "w": "FOOT OF POTRERO HILL",
         "cap": 8, "topic": "SOLDERING / ASSEMBLY"},
    ],
}


def check(name, ok, detail=""):
    if ok:
        PASSED[0] += 1
    else:
        FAILED.append("%s%s" % (name, (": " + detail) if detail else ""))
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name,
                         ("" if ok else "  -- " + detail)))


def cache_with(payload, age=0.0, root=None):
    """A cache directory holding one opencircuit record of the given age."""
    d = tempfile.mkdtemp(prefix="octest-", dir=root)
    if payload is not None:
        with open(os.path.join(d, "opencircuit.json"), "w") as fh:
            json.dump({"name": "opencircuit", "fetched_at": time.time() - age,
                       "source": "test", "ttl": oc.OPENCIRCUIT_TTL_FOR_TEST,
                       "payload": payload}, fh)
    return d


# ftdata owns the TTL; the fixture must not invent a second one that could
# drift away from it.
oc.OPENCIRCUIT_TTL_FOR_TEST = ftdata.ttl_for("opencircuit") or 21600


def words(render):
    """Every string the built plan will put on the panel, in order."""
    out = []
    for _start, _end, page in render.pages:
        for ln in list(page.header or []) + list(page.lines):
            out.append(ln.text)
    return out


# --------------------------------------------------------------------------

def test_one_phosphor():
    """Every pixel in every frame is exactly one entry of the ramp."""
    for name in sorted(oc.PHOSPHORS):
        cache = cache_with(FIXTURE)
        render = ds.build(oc, at=NOW, cache_dir=cache, phosphor=name)
        ramp = ds.gradient(oc.PHOSPHORS[name], 256, dtype=np.uint8)
        allowed = set(map(tuple, ramp.tolist()))
        bad = None
        # A whole loop at 15 fps: enough to cross both power transitions, both
        # workshop pages, every typed line and a couple of pulse cycles.
        for i in range(int(render.total * 15)):
            frame = render(i / 15.0, i)
            seen = set(map(tuple, np.unique(
                frame.reshape(-1, 3), axis=0).tolist()))
            off = seen - allowed
            if off:
                bad = "t=%.2f %r" % (i / 15.0, sorted(off)[:3])
                break
        check("one phosphor (%s)" % name, bad is None, bad or "")


def test_pure_in_t():
    """Two independent builds agree, and one callback repeats itself."""
    cache = cache_with(FIXTURE)
    a = ds.build(oc, at=NOW, cache_dir=cache)
    b = ds.build(oc, at=NOW, cache_dir=cache)
    probes = [0.05, 0.9, 2.1, 5.0, 11.3, 19.7, 28.4, 36.0, 41.9, 43.6]
    diff = [t for t in probes
            if not np.array_equal(a(t, 0), b(t, 12345))]
    check("independent builds agree", not diff, "differ at %r" % (diff,))

    # Walk forward, then ask for the same instants again out of order.
    for t in probes:
        a(t, 0)
    again = [t for t in reversed(probes)
             if not np.array_equal(a(t, 0), b(t, 0))]
    check("seekable after playing", not again, "differ at %r" % (again,))

    # And at a different frame rate, which is what the wall's drift amounts to.
    c = ds.build(oc, at=NOW, cache_dir=cache)
    for i in range(200):
        c(i / 7.0, i)
    odd = [t for t in probes if not np.array_equal(c(t, 0), b(t, 0))]
    check("same at another frame rate", not odd, "differ at %r" % (odd,))


def test_shape_and_range():
    cache = cache_with(FIXTURE)
    r = ds.build(oc, at=NOW, cache_dir=cache)
    ok = True
    for t in (0.3, 4.0, 15.0, 30.0, 43.0):
        f = r(t, 0)
        ok = ok and f.shape == (64, 320, 3) and f.dtype == np.uint8
    check("frames are (64, 320, 3) uint8", ok)

    # The panel is never entirely dark while the raster is up: an all-black
    # frame and a crashed render loop look identical on a wall.
    dark = [t for t in (4.0, 15.0, 30.0, 40.0)
            if int(r(t, 0).max()) < 40]
    check("the tube is never blank mid-loop", not dark, "dark at %r" % (dark,))


def test_fresh_says_the_date():
    cache = cache_with(FIXTURE)
    r = ds.build(oc, at=NOW, cache_dir=cache)
    said = words(r)
    check("fresh record draws the title", TITLE in said)
    check("fresh record draws the date",
          any(w.startswith("THU SEP 3 ") for w in said),
          "got %r" % (said,))
    check("fresh record counts down",
          any("IN 9 DAYS" in w for w in said), "got %r" % (said,))
    check("fresh record draws the seats",
          any("8 SEATS" in w for w in said), "got %r" % (said,))
    check("both workshops get a page",
          sum(1 for w in said if w == TITLE) == 2, "got %r" % (said,))


def test_bad_records_say_nothing():
    """Absent, empty and stale must not put a workshop on the wall."""
    stale_age = oc.OPENCIRCUIT_TTL_FOR_TEST * (oc.STALE_MULTIPLE + 1.0)
    cases = [
        ("absent", cache_with(None), "NO WORKSHOP DATA"),
        ("empty", cache_with(dict(FIXTURE, ev=[], n=0)),
         "NO WORKSHOPS SCHEDULED"),
        ("stale", cache_with(FIXTURE, age=stale_age), "NO WORKSHOP DATA"),
        # A record whose only workshops have already finished is the same
        # problem wearing the fresh label: the fetcher keeps the running one on
        # purpose, and once it has ended the panel has nothing upcoming.
        ("all past", cache_with(dict(
            FIXTURE, ev=[dict(FIXTURE["ev"][0], t=int(NOW - 90000))])),
         "NO WORKSHOPS SCHEDULED"),
    ]
    for label, cache, expect in cases:
        r = ds.build(oc, at=NOW, cache_dir=cache)
        said = words(r)
        check("%s: no title on the panel" % label,
              not any(TITLE in w for w in said), "got %r" % (said,))
        check("%s: no date on the panel" % label,
              not any(w.startswith(("MON ", "TUE ", "WED ", "THU ", "FRI ",
                                    "SAT ", "SUN ")) for w in said),
              "got %r" % (said,))
        check("%s: says so in words" % label,
              any(expect in w for w in said), "got %r" % (said,))
        # The identity card survives every one of these, because the group's
        # name is not a fact with a TTL.
        check("%s: still says who it is" % label,
              any("OPEN CIRCUIT SF" in w for w in said), "got %r" % (said,))

    # Absent and stale both draw the fetcher's name, since in both cases the
    # thing a person can do about it is run the fetcher.
    for label, cache in (("absent", cache_with(None)),
                         ("stale", cache_with(FIXTURE, age=stale_age))):
        said = words(ds.build(oc, at=NOW, cache_dir=cache))
        check("%s: names the fetcher" % label,
              any("FTDATA.PY" in w for w in said), "got %r" % (said,))


def test_aging_still_draws():
    """A record a little past its TTL is still worth quoting. See propagation."""
    age = oc.OPENCIRCUIT_TTL_FOR_TEST * 1.5
    r = ds.build(oc, at=NOW, cache_dir=cache_with(FIXTURE, age=age))
    check("aging record still draws the title", TITLE in words(r))


def test_font_covers_the_record():
    """Every character the fetcher can store has a glyph.

    The record is upper-cased and its punctuation is folded by the fetcher, so
    the set of characters that can reach `bake()` is small and knowable. A
    missing one is drawn as a hollow box on purpose -- this asserts that the
    box never actually appears for the text this panel composes.
    """
    cache = cache_with(FIXTURE)
    r = ds.build(oc, at=NOW, cache_dir=cache)
    missing = sorted({ch for w in words(r) for ch in w
                      if ch != " " and ch not in oc.BANK})
    check("every glyph the pages need exists", not missing,
          "no glyph for %r" % (missing,))


def test_every_panel_size():
    """It is laid out for 320x64, but it may not crash on anything else.

    This panel has more fixed pixel geometry than most -- a header rule on a
    known row, a circuit with four components at hand-placed x positions, a
    bloom that works on 2x2 blocks -- and every one of those was an out-of-
    bounds write or a broadcast error on some panel when first written. The
    ones that mattered were not exotic: `-D 45x35` is *ft-server's own
    default*, so anybody starting the emulator without arguments and pointing
    this at it hit an IndexError rather than a small ugly picture.

    Looking wrong on a 16-row panel is fine and expected. Raising is not.
    """
    cache = cache_with(FIXTURE)
    bad = []
    for w in (1, 2, 3, 7, 8, 16, 45, 64, 128, 160, 240, 319, 320, 321, 640):
        for h in (1, 3, 8, 16, 32, 35, 48, 63, 64, 65, 128):
            try:
                r = ds.build(oc, at=NOW, cache_dir=cache, width=w, height=h)
                for t in (0.3, 1.0, 4.0, 15.0, 30.0, 43.0, 43.9):
                    f = r(t, 0)
                    if f.shape != (h, w, 3) or f.dtype != np.uint8:
                        raise AssertionError("shape %r" % (f.shape,))
            except Exception as exc:                          # noqa: BLE001
                bad.append((w, h, repr(exc)[:60]))
    check("survives every panel size (%d tried)" % (15 * 11), not bad,
          "%r" % (bad[:4],))


def test_countdown_units():
    """The countdown says the coarsest thing that is still true.

    Written against local midnight rather than against fixed offsets from NOW,
    because that is what the function is *for*: the answer has to be a property
    of the calendar, so a test that adds hours to NOW would pass or fail
    depending on what time of day the suite happens to run.
    """
    day = 86400.0
    bad = []

    def want(when, expect):
        got = oc.countdown(when, 7200.0, NOW)
        if got != expect:
            bad.append((when, expect, got))

    # Midnight of the day NOW falls on, and one 18:30 class on each of the
    # next several days. Built by walking whole days through localtime so the
    # arithmetic survives a DST boundary landing inside the window.
    def evening(n):
        base = time.mktime(time.localtime(NOW)[:3] + (0, 0, 0, 0, 0, -1))
        lt = time.localtime(base + n * day + day / 2.0)
        return time.mktime(lt[:3] + (18, 30, 0, 0, 0, -1))

    want(evening(1), "TOMORROW")
    want(evening(2), "IN 2 DAYS")
    want(evening(9), "IN 9 DAYS")
    want(evening(21), "IN 3 WEEKS")
    # Already begun, and long finished.
    want(NOW - 60, "ON NOW")
    want(NOW - 40000, "STARTED")
    # Inside the last hour, elapsed time wins over the calendar.
    want(NOW + 1800, "IN 30 MIN")
    # Later the same day, but more than an hour off: TODAY, never "IN N HOURS".
    later = NOW + 5 * 3600
    if oc.days_between(NOW, later) == 0:
        want(later, "TODAY")
    check("countdown picks its unit", not bad, "%r" % (bad,))

    # The DST-safe day count: every day of a year is exactly one day from the
    # one before it, including the two that are not 24 hours long.
    walk, t0 = [], time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))
    for n in range(365):
        a = t0 + n * day
        lt = time.localtime(a + day / 2.0)
        b = time.mktime(lt[:3] + (12, 0, 0, 0, 0, -1))
        if oc.days_between(a, b) != 1:
            walk.append(time.strftime("%Y-%m-%d", time.localtime(a)))
    check("days_between survives the clock change", not walk,
          "wrong on %r" % (walk[:4],))


def test_fetcher_parsing():
    """The two pieces of the fetcher that turn a feed into a record."""
    check("Z stamps are read as UTC",
          oc_epoch("2026-09-04T01:30:00Z") == 1788485400.0,
          "got %r" % (oc_epoch("2026-09-04T01:30:00Z"),))
    for bad in ("", "2026-09-04", "2026-09-04T01:30:00+00:00", "nonsense"):
        check("rejects %r" % bad, oc_epoch(bad) is None)
    check("street is the part before the comma",
          ftdata._oc_street("1736 18th Street, San Francisco CA 94107")
          == "1736 18TH STREET")
    check("titles fold case and ampersands",
          ftdata._oc_text("Soldering & Assembly", 40) == "SOLDERING / ASSEMBLY")


def oc_epoch(s):
    return ftdata._oc_epoch(s)


def main():
    root = tempfile.mkdtemp(prefix="opencircuit-test-")
    os.environ.setdefault("FT_DATA_CACHE", os.path.join(root, "empty"))
    test_fetcher_parsing()
    test_countdown_units()
    test_shape_and_range()
    test_every_panel_size()
    test_fresh_says_the_date()
    test_bad_records_say_nothing()
    test_aging_still_draws()
    test_font_covers_the_record()
    test_pure_in_t()
    test_one_phosphor()
    print("\n%d passed, %d failed" % (PASSED[0], len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  FAIL " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
