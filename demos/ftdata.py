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
