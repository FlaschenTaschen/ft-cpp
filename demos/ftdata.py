#!/usr/bin/env python3
"""Fetch outside data on a timer; hand it to the demos off a disk cache.

The demos that show live data -- propagation, tides, weather -- cannot fetch
it themselves, and the reason is the scheduler. `ftsched` builds the next
segment on a worker thread while the current one is on the wall, and Python
threads share the GIL, so a `build()` that blocks on a socket does not merely
wait: it stops the render loop from getting the interpreter back. A slow
server would drop frames on the wall, and a hung one would mean the segment
never builds at all. Nobody standing in the workshop should be able to tell
that a NOAA endpoint is having a bad afternoon.

So the network lives here, in a process of its own, on a timer. It writes one
JSON file per product into a cache directory. `load()` reads that directory
and **never touches the network** -- it does not import a HTTP library, and it
returns rather than raises when a file is missing, malformed or ancient.

  $ python3 ftdata.py --list
  $ python3 ftdata.py --once                 # one pass, then exit
  $ python3 ftdata.py --loop 900             # every fifteen minutes

Every record carries `fetched_at`, and `load()` hands back the age alongside
the payload, because **age is part of the data**. A tide clock showing
yesterday's phase, or a propagation panel showing last week's K index, is
worse than one showing nothing: it is confidently wrong, and the wall gives no
hint which it is. Demos are expected to say how old their data is, and to say
so loudly once it stops being worth believing. `describe_age()` is here so
they all phrase it the same way.
"""

import argparse
import json
import os
import sys
import tempfile
import time

CACHE_DIR = os.environ.get(
    "FT_DATA_CACHE", os.path.expanduser("~/.cache/ftdata"))

# Products are registered by name. `ttl` is how long a record stays worth
# believing -- not how often it is fetched, which is the timer's business. A
# tide prediction is good for a day; a K index is stale within the hour.
PRODUCTS = {}


def product(name, ttl, description):
    """Register a fetch function. It returns the payload; we add the envelope."""
    def wrap(fn):
        PRODUCTS[name] = {"fn": fn, "ttl": ttl, "description": description}
        return fn
    return wrap


# --------------------------------------------------------------------------
# Reading. No network here, by construction.
# --------------------------------------------------------------------------

def path_for(name, cache_dir=None):
    return os.path.join(cache_dir or CACHE_DIR, name + ".json")


def load(name, cache_dir=None):
    """Return (payload, age_seconds), or None if there is nothing usable.

    Never raises, never blocks, never fetches. A demo calling this in
    `build()` is reading a local file and nothing else. Callers decide what to
    do about age; see `describe_age()`.
    """
    try:
        with open(path_for(name, cache_dir)) as fh:
            rec = json.load(fh)
        return rec["payload"], max(0.0, time.time() - float(rec["fetched_at"]))
    except Exception:
        # Missing, half-written, corrupt, or from a future version. All of
        # those mean the same thing to a demo: draw the no-data state.
        return None


def ttl_for(name):
    return PRODUCTS[name]["ttl"] if name in PRODUCTS else None


def is_fresh(name, age):
    ttl = ttl_for(name)
    return ttl is None or age <= ttl


def describe_age(age):
    """A short human phrase for an age in seconds: '4m', '2h', '3d'."""
    if age < 90:
        return "%ds" % int(age)
    if age < 5400:
        return "%dm" % int(age / 60)
    if age < 172800:
        return "%dh" % int(age / 3600)
    return "%dd" % int(age / 86400)


# --------------------------------------------------------------------------
# Writing. Only the fetcher process runs this.
# --------------------------------------------------------------------------

def _store(name, payload, source, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    rec = {"name": name, "fetched_at": time.time(), "source": source,
           "ttl": PRODUCTS[name]["ttl"], "payload": payload}
    # Write-then-rename: a demo reading the cache while the fetcher writes it
    # must never see half a file. rename(2) within a directory is atomic.
    fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix="." + name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, path_for(name, cache_dir))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(url, timeout=20):
    """Fetch a URL as bytes. Imported lazily so `load()` stays network-free."""
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": "flaschen-taschen-ftdata/1 (+wall display)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url, timeout=20):
    return json.loads(get(url, timeout))


def fetch(name, cache_dir=None):
    """Fetch one product into the cache. Returns True on success.

    A failure leaves the previous record in place on purpose: yesterday's
    tides with an honest age on them beat an empty panel, and the demo is
    already required to show that age.
    """
    cache_dir = cache_dir or CACHE_DIR
    spec = PRODUCTS[name]
    try:
        payload, source = spec["fn"]()
    except Exception as e:                                   # noqa: BLE001
        print("ftdata: %s failed: %r" % (name, e), file=sys.stderr)
        return False
    _store(name, payload, source, cache_dir)
    return True


def fetch_all(cache_dir=None, only=None):
    ok = 0
    for name in sorted(PRODUCTS):
        if only and name not in only:
            continue
        if fetch(name, cache_dir):
            ok += 1
    return ok


# --------------------------------------------------------------------------
# Products. One worked example lives here; the rest are added alongside the
# demo that needs them, so a demo and its data arrive together.
# --------------------------------------------------------------------------

HAMQSL_URL = "https://www.hamqsl.com/solarxml.php"


@product("hamqsl", ttl=7200,
         description="N0NBH solar indices and per-band HF conditions")
def _hamqsl():
    """Solar indices plus the band-by-band call every ham display makes.

    1.6 kB of XML, which is why this is the source rather than assembling the
    same picture out of four SWPC endpoints: it already carries `good`/`fair`/
    `poor` per band for day and night, which is somebody's judgement rather
    than a measurement, and reproducing that judgement badly would be worse
    than quoting it.
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(get(HAMQSL_URL).decode("utf-8", "replace"))
    data = root.find("solardata")

    def txt(tag):
        el = data.find(tag)
        return el.text.strip() if el is not None and el.text else None

    bands = {}
    for band in data.iterfind("calculatedconditions/band"):
        # name="80m-40m" time="day" -> {"80m-40m": {"day": "Good"}}
        bands.setdefault(band.get("name"), {})[band.get("time")] = \
            (band.text or "").strip()
    vhf = {}
    for ph in data.iterfind("calculatedvhfconditions/phenomenon"):
        vhf[ph.get("name")] = (ph.text or "").strip()

    return {
        "updated": txt("updated"),
        "solarflux": txt("solarflux"), "sunspots": txt("sunspots"),
        "aindex": txt("aindex"), "kindex": txt("kindex"),
        "xray": txt("xray"), "solarwind": txt("solarwind"),
        "magneticfield": txt("magneticfield"), "aurora": txt("aurora"),
        "signalnoise": txt("signalnoise"), "muf": txt("muf"),
        "bands": bands, "vhf": vhf,
    }, HAMQSL_URL


# -- Space weather, from SWPC directly. -------------------------------------
#
# hamqsl above is one small file carrying somebody's summary. These are the
# measurements it summarises, and they are here because a single number cannot
# say what a series can: "K is 5" and "K has been 5 for twelve hours" are
# different afternoons. All three are trimmed hard before they are stored,
# because this cache lives on a Pi on shop wifi and the demo needs a few
# hundred numbers out of files that run to half a megabyte.

KP_3H_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
KP_1M_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"


@product("swpc_kp", ttl=5400,
         description="planetary Kp: 3-hourly for three days, plus the estimate now")
def _swpc_kp():
    """The definitive 3-hourly Kp, and the running estimate between bulletins.

    Two files because they answer two questions. The 3-hourly series is the
    published index -- what an archive will still say next year -- and it is
    what a history strip should plot, since each bar is a real interval rather
    than a moment. The 1-minute file is the estimate SWPC runs continuously,
    which is the only thing that knows a storm started twenty minutes ago; it
    covers about six hours, so it can *only* be the "now" figure.

    They disagree by design near the right-hand edge, and the panel shows the
    estimate as the headline with the published bars behind it rather than
    trying to reconcile them.

    `a_running` rides along in the 3-hourly file, which is where the A index on
    the panel comes from -- same source as the K bars, so the two cannot drift
    apart the way they would if A were quoted from somewhere else.
    """
    rows = get_json(KP_3H_URL)
    # 4.8 kB of 7 days; three days is as much as a 320 px strip can show with
    # bars wide enough to read, and 24 records is under a kilobyte.
    tail = rows[-24:]
    series = [{"t": r["time_tag"], "kp": round(float(r["Kp"]), 2),
               "a": int(r["a_running"])} for r in tail]

    now = None
    try:
        live = get_json(KP_1M_URL)
        if live:
            last = live[-1]
            now = {"t": last["time_tag"],
                   "kp": round(float(last["estimated_kp"]), 2)}
    except Exception:                                        # noqa: BLE001
        # The estimate is a nicety; the published series is the product. A
        # panel with bars and no headline is still a useful panel.
        pass

    return {"series": series, "now": now}, KP_3H_URL


XRAY_URL = "https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json"

# GOES long-channel thresholds, W/m^2. The letter is the decade and the digit
# is the mantissa: 4.7e-6 is C4.7. This is the readable form of the number and
# the only form anyone says out loud.
XRAY_CLASSES = ((1e-4, "X"), (1e-5, "M"), (1e-6, "C"), (1e-7, "B"))


def xray_class(flux):
    """Format a W/m^2 long-channel flux as 'C4.7'. None if there is no flux."""
    if flux is None or flux <= 0:
        return None
    for scale, letter in XRAY_CLASSES:
        if flux >= scale:
            return "%s%.1f" % (letter, flux / scale)
    return "A%.1f" % (flux / 1e-8)


@product("swpc_xray", ttl=3600,
         description="GOES 1-8A X-ray flux, 24h at 15-minute peaks")
def _swpc_xray():
    """656 kB of two channels a minute, kept as 96 numbers.

    Only the long channel (0.1-0.8 nm) is kept, because that is the one the
    flare classes are defined on; the short channel is a hardness ratio nobody
    reads off a wall. It is bucketed to 15 minutes and each bucket keeps its
    **maximum**, not its mean: a flare is a spike a few minutes wide, and
    averaging one into a quarter hour of quiet sun is how you end up with a
    panel that missed an M-class event entirely.

    The peak of the whole day comes back alongside, since "biggest flare
    today" is a thing people ask and recomputing it from a downsampled series
    would give a slightly different -- and always smaller -- answer.
    """
    rows = get_json(XRAY_URL)
    long_chan = [r for r in rows if r.get("energy") == "0.1-0.8nm"]
    if not long_chan:
        raise ValueError("no 0.1-0.8nm samples in GOES feed")

    def stamp(r):
        return r["time_tag"]

    long_chan.sort(key=stamp)
    buckets = 96                                    # 24 h at a quarter hour
    n = len(long_chan)
    series = []
    for i in range(buckets):
        lo = (i * n) // buckets
        hi = max(lo + 1, ((i + 1) * n) // buckets)
        vals = [r["flux"] for r in long_chan[lo:hi]
                if isinstance(r.get("flux"), (int, float)) and r["flux"] > 0]
        # Two significant figures is a tenth of a flare class; storing the
        # float64 repr would triple the file for precision nobody can see.
        series.append(float("%.2e" % max(vals)) if vals else None)

    latest = None
    for r in reversed(long_chan):
        if isinstance(r.get("flux"), (int, float)) and r["flux"] > 0:
            latest = r
            break
    peak = max((r["flux"] for r in long_chan
                if isinstance(r.get("flux"), (int, float))), default=None)

    return {
        "series": series, "minutes_per_bucket": 15,
        "start": stamp(long_chan[0]), "end": stamp(long_chan[-1]),
        "current": latest["flux"] if latest else None,
        "current_class": xray_class(latest["flux"]) if latest else None,
        "current_t": stamp(latest) if latest else None,
        "peak": peak, "peak_class": xray_class(peak),
        "satellite": long_chan[-1].get("satellite"),
    }, XRAY_URL


WIND_SPEED_URL = "https://services.swpc.noaa.gov/products/summary/solar-wind-speed.json"
WIND_MAG_URL = "https://services.swpc.noaa.gov/products/summary/solar-wind-mag-field.json"


@product("swpc_solarwind", ttl=3600,
         description="solar wind speed and IMF Bt/Bz at L1")
def _swpc_solarwind():
    """Sixty bytes each, which is the whole reason these are the endpoints.

    The `/products/solar-wind/mag-1-day.json` path that every older script
    uses is gone -- it 404s now -- and the day-long series it served would
    have been trimmed to these two numbers anyway. Bz is the one worth the
    space: southward Bz is what opens the magnetosphere, so a negative number
    here is the reason tomorrow's K will be bad, hours before K knows it.
    """
    payload = {"speed": None, "bt": None, "bz": None, "t": None}
    rows = get_json(WIND_SPEED_URL)
    if rows:
        payload["speed"] = rows[0].get("proton_speed")
        payload["t"] = rows[0].get("time_tag")
    rows = get_json(WIND_MAG_URL)
    if rows:
        payload["bt"] = rows[0].get("bt")
        payload["bz"] = rows[0].get("bz_gsm")
        payload["t"] = rows[0].get("time_tag") or payload["t"]
    return payload, WIND_SPEED_URL


def main():
    ap = argparse.ArgumentParser(
        description="fetch outside data into a cache the demos read",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--cache-dir", default=CACHE_DIR)
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--loop", type=float, default=0,
                    help="seconds between passes (0 = use --once)")
    ap.add_argument("--only", default="", help="comma-separated product names")
    ap.add_argument("--list", action="store_true", help="show products and exit")
    args = ap.parse_args()

    if args.list:
        for name in sorted(PRODUCTS):
            got = load(name, args.cache_dir)
            age = "absent" if got is None else describe_age(got[1]) + " old"
            print("  %-22s ttl %-7s %-9s %s"
                  % (name, "%ds" % PRODUCTS[name]["ttl"], age,
                     PRODUCTS[name]["description"]))
        return

    only = set(x for x in args.only.split(",") if x)
    if not args.loop:
        n = fetch_all(args.cache_dir, only)
        print("ftdata: %d/%d products refreshed" % (n, len(only or PRODUCTS)))
        return
    while True:
        started = time.time()
        n = fetch_all(args.cache_dir, only)
        print("ftdata: %d/%d refreshed" % (n, len(only or PRODUCTS)), flush=True)
        time.sleep(max(5.0, args.loop - (time.time() - started)))


if __name__ == "__main__":
    main()
