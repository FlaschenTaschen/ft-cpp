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
JSON file per product into a cache directory -- and, for the products whose
payload is pixels rather than numbers, one binary sidecar beside it, which is
written to tmpfs instead of a flash card where there is one; see BLOB_DIR.
`load()` reads that directory and **never touches the network** -- it does not
import a HTTP library, and it returns rather than raises when a file is
missing, malformed or ancient.

  $ python3 ftdata.py --list
  $ python3 ftdata.py --once                 # one pass, then exit
  $ python3 ftdata.py --once --due --fast    # only what is quick and overdue
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

# Where the binary sidecars go, which is deliberately not where the records go.
#
# The records are hundreds of bytes to a few kilobytes and they are worth
# keeping across a reboot: a tide prediction fetched yesterday is still true
# this morning, so the panel comes up with a curve on it rather than a no-data
# card. Rewriting them every quarter hour is nothing.
#
# The sidecars are the opposite on both counts. A GOES window is 3.5 MB
# rewritten every pass -- 336 MB a day onto the SD card the Pi boots from, by a
# wide margin the heaviest writer on the machine -- and none of it is worth
# surviving a reboot, because imagery more than half an hour old is stale by
# its own TTL. So the pixels go to tmpfs and the metadata stays on disk. The
# default is /run/ftdata, which on a Pi running the fetcher under systemd is a
# line of unit file (`RuntimeDirectory=ftdata`) and nothing else: /run there is
# a ~180 MB tmpfs and a window costs about two per cent of it. What that costs
# at boot is one honest no-data card until the first fetch lands.
#
# Nothing here requires any of that. A checkout that has no /run/ftdata and
# cannot make one falls back to the cache directory, so running the fetcher by
# hand works with no setup at all -- it just writes the pixels to disk with the
# records. FT_DATA_BLOBS overrides both, for a scratch cache or a machine that
# puts its tmpfs somewhere else.
BLOB_DIR = os.environ.get("FT_DATA_BLOBS", "/run/ftdata")

# Backstops on the sidecar directory; see sweep_blobs(). Generous on purpose --
# these are not the mechanism, prune_blobs() is, and anything these catch is
# already a bug somewhere.
BLOB_MAX_AGE = float(os.environ.get("FT_DATA_BLOBS_MAX_AGE", "86400"))
BLOB_MAX_BYTES = int(os.environ.get("FT_DATA_BLOBS_MAX", str(64 << 20)))

# Where the registry splits for the two timers: `--fast` takes the products
# whose interval is at or under this, ftdata.timer's ordinary pass takes the
# rest. Five minutes rather than sixty seconds so a product can ask for a
# two-minute cadence without needing a third timer to give it one.
FAST_INTERVAL = float(os.environ.get("FT_DATA_FAST_INTERVAL", "300"))

# Products are registered by name. `ttl` is how long a record stays worth
# believing -- not how often it is fetched, which is the timer's business. A
# tide prediction is good for a day; a K index is stale within the hour.
PRODUCTS = {}


def product(name, ttl, description, interval=None, volatile=False):
    """Register a fetch function. It returns the payload; we add the envelope.

    `interval` is the shortest time worth re-fetching in, and it exists because
    the original assumption here -- that one timer cadence suits everything --
    stopped being true the moment a product moved faster than the wall could
    say. A tide prediction is the same file all afternoon; an aircraft crosses
    the Bay in four minutes. So the timer no longer decides: it wakes often and
    asks each product whether it is due, which puts a product's cadence next to
    its TTL where the reasoning about it already is, and means adding a fast
    product does not drag the slow ones along with it. None means "every pass",
    which is what everything did before this existed.

    `volatile` moves the *record* to tmpfs, and it is what makes a one-minute
    product safe on a machine that boots off an SD card. The blob split already
    does this for pixels; a record refetched every minute is the same problem in
    miniature -- 1440 writes a day of something worthless two minutes later and
    not worth having back after a reboot. What it costs is one honest no-data
    card for the first tick after boot, which these demos already draw.
    """
    def wrap(fn):
        PRODUCTS[name] = {"fn": fn, "ttl": ttl, "description": description,
                          "interval": interval, "volatile": bool(volatile)}
        return fn
    return wrap


# --------------------------------------------------------------------------
# Reading. No network here, by construction.
# --------------------------------------------------------------------------

def path_for(name, cache_dir=None):
    return os.path.join(cache_dir or CACHE_DIR, name + ".json")


def is_volatile(name):
    return bool(PRODUCTS.get(name, {}).get("volatile"))


def record_dirs(name, cache_dir=None):
    """Where a record might be. Durable products: the cache, and only that.

    A volatile record lives in the same tmpfs the sidecars use, so the search
    order is tmpfs first and the cache second -- second rather than not at all,
    because a machine that has just been upgraded still has yesterday's record
    on disk under the old rules, and a workstation with no /run/ftdata never
    stopped writing there. Preferring tmpfs is what makes the stale on-disk
    copy harmless: it is only ever read when the fresh one is absent, which is
    exactly the boot-shaped hole `volatile` accepts by design.
    """
    if not is_volatile(name):
        return [cache_dir or CACHE_DIR]
    return blob_dirs(cache_dir)


def record_path(name, cache_dir=None):
    """The record that `load()` would actually read, or None if there is none.

    For anything that wants the file rather than its contents -- the MOTD stats
    it instead of parsing it -- so that a caller does not have to know which of
    the two directories a given product writes to.
    """
    for d in record_dirs(name, cache_dir):
        path = os.path.join(d, name + ".json")
        if os.path.exists(path):
            return path
    return None


def blob_dirs(cache_dir=None):
    """Where a sidecar might be, tmpfs first, cache directory second.

    Two places rather than one because the split is a deployment decision and
    not a data format: the wall's fetcher writes to /run/ftdata, a checkout on
    a workstation writes beside the records, and a Pi that has just been
    upgraded has yesterday's sidecar in the old place and today's in the new
    one. Searching both is safe precisely because a sidecar is named after its
    contents -- the same name never means two different things, so "look here,
    then there" cannot pair a record with the wrong array. Never raises: a
    caller of this is on `load()`'s side of the wall.
    """
    dirs = []
    try:
        if BLOB_DIR and os.path.isdir(BLOB_DIR):
            dirs.append(BLOB_DIR)
    except OSError:
        pass
    cache_dir = cache_dir or CACHE_DIR
    if cache_dir not in dirs:
        dirs.append(cache_dir)
    return dirs


def load(name, cache_dir=None):
    """Return (payload, age_seconds), or None if there is nothing usable.

    Never raises, never blocks, never fetches. A demo calling this in
    `build()` is reading a local file and nothing else. Callers decide what to
    do about age; see `describe_age()`.
    """
    try:
        rec = None
        for d in record_dirs(name, cache_dir):
            try:
                with open(os.path.join(d, name + ".json")) as fh:
                    rec = json.load(fh)
                break
            except FileNotFoundError:
                continue
        if rec is None:
            return None
        return rec["payload"], max(0.0, time.time() - float(rec["fetched_at"]))
    except Exception:
        # Missing, half-written, corrupt, or from a future version. All of
        # those mean the same thing to a demo: draw the no-data state.
        return None


def load_blob(filename, cache_dir=None):
    """Open a binary sidecar written by `store_blob()`. None if unusable.

    JSON is the wrong container for pixels -- a base64'd megabyte of uint8 is
    four times the bytes and a second of parsing -- so a product whose payload
    is an array writes the array beside the record as an `.npz`, and the record
    carries the metadata and the sidecar's name. This is the reading half, and
    it keeps `load()`'s contract exactly: it does not touch the network, it
    does not raise, and a missing, truncated or foreign file is simply None.

    numpy is the one import here, and it is not a concession: every caller of
    this is a demo that has already imported it to draw with.

    Callers pass the filename out of the record rather than composing one, so
    the basename check is not paranoia about the cache directory but about the
    record: a `../` in a fetched file is the one way this could reach outside
    the cache, and it costs a line to make it impossible. It matters more now
    that there are two directories to look in, not less: the check happens once
    and applies to both, because it is the *name* that is being trusted.
    """
    try:
        import numpy as np
        if not filename or os.path.basename(filename) != filename:
            return None
        for d in blob_dirs(cache_dir):
            path = os.path.join(d, filename)
            if not os.path.exists(path):
                continue
            with np.load(path) as z:
                return {k: z[k] for k in z.files}
        return None
    except Exception:
        # Same reasoning as load(): missing, half-written, corrupt or from a
        # version that stored different arrays all mean "draw the no-data
        # state", and none of them should take the wall down.
        return None


def ttl_for(name):
    return PRODUCTS[name]["ttl"] if name in PRODUCTS else None


def is_fresh(name, age):
    ttl = ttl_for(name)
    return ttl is None or age <= ttl


def interval_for(name):
    return PRODUCTS.get(name, {}).get("interval")


def is_due(name, cache_dir=None):
    """Should this product be fetched on this pass?

    Nothing without an interval ever says no, so a fetcher run with --due over
    the old registry behaves exactly as it did. Nor does a product with no
    record: an absent file is the one case where waiting cannot help.
    """
    interval = interval_for(name)
    if not interval:
        return True
    got = load(name, cache_dir)
    if got is None:
        return True
    # A hair under, because the timer's own wakeup jitter would otherwise make
    # a 60 s product miss every other tick: at 59.6 s of age against a 60 s
    # interval it would defer, and the next look is a whole minute later.
    return got[1] >= interval * 0.9


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
    # A volatile record goes wherever the sidecars go, which is tmpfs on the
    # wall and the cache directory anywhere else. Same helper as the blobs use,
    # so the two cannot end up disagreeing about where tmpfs is.
    out_dir = blob_write_dir(cache_dir) if is_volatile(name) else cache_dir
    os.makedirs(out_dir, exist_ok=True)
    rec = {"name": name, "fetched_at": time.time(), "source": source,
           "ttl": PRODUCTS[name]["ttl"], "payload": payload}
    # Write-then-rename: a demo reading the cache while the fetcher writes it
    # must never see half a file. rename(2) within a directory is atomic --
    # which is also why the temporary file has to be made in the directory it
    # will land in, rather than in the cache for everything.
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="." + name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, os.path.join(out_dir, name + ".json"))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def blob_write_dir(cache_dir=None):
    """Where a new sidecar should be written: tmpfs if we can, cache if not.

    Fails soft in both directions. Under systemd the directory is already there
    and owned by this user, so the makedirs is a no-op; run by hand on a
    workstation it fails on /run's permissions and the sidecar lands beside the
    records, which is where it used to live and still works. The one thing that
    must not happen is an exception: a fetcher that cannot write its pixels to
    RAM should write them to disk, not fail the product.
    """
    cache_dir = cache_dir or CACHE_DIR
    if BLOB_DIR and BLOB_DIR != cache_dir:
        try:
            os.makedirs(BLOB_DIR, exist_ok=True)
            if os.access(BLOB_DIR, os.W_OK | os.X_OK):
                return BLOB_DIR
        except OSError:
            pass
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def store_blob(name, arrays, cache_dir, compress=True):
    """Write arrays to a sidecar and return its basename for the record.

    The name carries a fresh random token every time -- `goes-psw-1f3c9a20.npz`
    -- and that is the whole trick. A record and its sidecar are two files, so
    write-then-rename makes each of them atomic but says nothing about the
    pair: a reader landing between the two renames would get the new record
    and the old array, or the reverse, and either is a silent mismatch rather
    than an error. Writing the sidecar under a new name first, then renaming
    the record that points at it, then deleting the sidecars nobody points at,
    means every record ever visible names a file that exists and holds exactly
    the arrays it describes. The cost is one stale file for as long as a slow
    reader holds it open, which is what `prune_blobs()` sweeps up next pass.

    The sidecar goes wherever `blob_write_dir()` says -- tmpfs on the wall, the
    cache directory on a workstation -- and only its basename goes in the
    record. That is what makes moving them a deployment decision rather than a
    format change: nothing written into a record names a directory, so a
    machine that changes its mind about where pixels live is one restart away
    from doing it, and a record written on one side reads on the other.
    """
    import numpy as np
    blob_at = blob_write_dir(cache_dir)
    filename = "%s-%08x.npz" % (name, int.from_bytes(os.urandom(4), "big"))
    fd, tmp = tempfile.mkstemp(dir=blob_at, prefix="." + name, suffix=".npz")
    os.close(fd)
    try:
        # suffix=".npz" on purpose: savez appends the extension itself if the
        # path does not already have it, and would then write beside the temp
        # file rather than into it.
        (np.savez_compressed if compress else np.savez)(tmp, **arrays)
        os.replace(tmp, os.path.join(blob_at, filename))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return filename


def prune_blobs(name, keep, cache_dir):
    """Delete `<name>-*.npz` sidecars other than `keep`. Best effort.

    Both directories, which is what makes the move to tmpfs self-installing: the
    first pass after the change writes the window to /run and deletes the 3.5 MB
    that has been sitting in ~/.cache/ftdata since before it, with no migration
    step to remember and nothing left behind if the change is reverted.
    """
    prefix, suffix = name + "-", ".npz"
    for d in blob_dirs(cache_dir):
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for fn in entries:
            if fn == keep or not (fn.startswith(prefix) and fn.endswith(suffix)):
                continue
            try:
                os.unlink(os.path.join(d, fn))
            except OSError:
                pass
    sweep_blobs(keep, cache_dir)


def sweep_blobs(keep=None, cache_dir=None,
                max_age=None, max_bytes=None):
    """Bound the tmpfs sidecar directory's age and size. Best effort.

    `prune_blobs()` is the mechanism and this is the backstop, for the files it
    cannot see: a fetcher killed between the sidecar's rename and the record's,
    a product that has been renamed or removed, somebody's prune that did not
    run. On an SD card those would only waste space. In tmpfs they hold RAM that
    the rest of the machine shares -- /run is 182 MB and the wall's other units
    keep things in it -- so unreferenced pixels get a second, blunter sweep that
    knows nothing about products.

    Only ever the tmpfs directory, and only ever files ending `.npz`: pointed at
    a cache directory this would be a thing that deletes records by age, which
    is exactly the fault it exists to prevent. A day is generous by two orders
    of magnitude -- every product here rewrites its record inside a quarter hour
    -- so a sidecar this touches has not been named by anything for ninety-six
    fetch passes, and any record still pointing at it went stale long before.
    """
    d = BLOB_DIR
    cache_dir = cache_dir or CACHE_DIR
    if not d or d == cache_dir:
        return
    max_age = BLOB_MAX_AGE if max_age is None else max_age
    max_bytes = BLOB_MAX_BYTES if max_bytes is None else max_bytes
    try:
        entries = os.listdir(d)
    except OSError:
        return

    now = time.time()
    live = []
    for fn in entries:
        if not fn.endswith(".npz"):
            continue
        path = os.path.join(d, fn)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if fn != keep and now - st.st_mtime > max_age:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        live.append((st.st_mtime, st.st_size, path, fn))

    total = sum(sz for _, sz, _, _ in live)
    if total <= max_bytes:
        return
    # Oldest first, and never the file the record being written names: a full
    # tmpfs is somebody else's bug, and the fix for it must not be to break the
    # product that noticed.
    for mtime, size, path, fn in sorted(live):
        if total <= max_bytes:
            break
        if fn == keep:
            continue
        try:
            os.unlink(path)
            total -= size
        except OSError:
            pass


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
        # A blob product writes its own sidecar, so it is the one kind of
        # fetch function that has to be told where the cache is.
        payload, source = (spec["fn"](cache_dir) if spec.get("blob")
                           else spec["fn"]())
    except Exception as e:                                   # noqa: BLE001
        print("ftdata: %s failed: %r" % (name, e), file=sys.stderr)
        return False
    _store(name, payload, source, cache_dir)
    return True


def fetch_all(cache_dir=None, only=None, due_only=False, max_interval=None):
    """Fetch products into the cache; return (fetched, considered).

    Two counts rather than one because with --due most passes fetch nothing and
    that is the healthy case, not a failure -- "0/1" in the journal every minute
    would read like something is broken. `max_interval` selects the fast half of
    the registry for the fast timer, by the product's own declared cadence
    rather than by a list of names in a unit file that would go stale the first
    time somebody added a product.
    """
    ok = considered = 0
    for name in sorted(PRODUCTS):
        if only and name not in only:
            continue
        if max_interval is not None:
            interval = interval_for(name)
            if not interval or interval > max_interval:
                continue
        considered += 1
        if due_only and not is_due(name, cache_dir):
            continue
        if fetch(name, cache_dir):
            ok += 1
    return ok, considered


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


# --------------------------------------------------------------------------
# Tides and tidal currents, from NOAA CO-OPS. tide.py draws these.
#
# Two things make this different from the solar feed above. The first is that
# a prediction is not an observation: the file is a *forecast* spanning days
# either side of the fetch, so a record several hours old is still telling the
# truth, and the question a demo has to ask is not "how old is this?" but "does
# it still cover now?". Both are answerable -- the age comes from `load()`, the
# span is in the payload -- and tide.py checks both.
#
# The second is the station. These are per-station products, so the name
# carries the station id and `register_*` can be called again for another one;
# FT_TIDE_STATIONS and FT_CURRENT_STATIONS add stations without editing this
# file. San Francisco is only the default because that is where the wall is.
#
# Times are stored as epoch seconds, always. The API can hand back local
# station time, but that is a naive wall-clock string with no offset attached
# and it steps backwards an hour every autumn, so everything here is fetched in
# GMT and converted once. Turning an epoch back into something a human reads is
# the demo's business, and it does it in the *display's* local time -- which is
# the right answer for a wall in the same city as the station, and an honest
# one anywhere else.
# --------------------------------------------------------------------------

COOPS_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

TIDE_STATION = "9414290"            # San Francisco, Fort Point, inside the Gate
CURRENT_STATION = "SFB1201"         # Golden Gate, mid-channel

# How far either side of the fetch to ask for. Two days each way is enough that
# a demo showing a day-wide window never runs off the end of the curve even if
# the fetcher has been down since yesterday.
COOPS_SPAN_DAYS = 2

# Predictions, so this is generous on purpose: the payload is still true long
# after it was fetched. It is the *span* that expires, not the fetch. Two days
# is where the record stops covering a window centred on now.
COOPS_TTL = 172800

_COOPS_EPOCH_FMT = "%Y-%m-%d %H:%M"


def _coops_url(**params):
    from urllib.parse import urlencode
    return COOPS_URL + "?" + urlencode(params)


def _coops_epoch(s):
    """'2026-08-07 18:55' in GMT -> epoch seconds."""
    import calendar
    return float(calendar.timegm(time.strptime(s, _COOPS_EPOCH_FMT)))


def _coops_dates(days=COOPS_SPAN_DAYS):
    now = time.time()
    return (time.strftime("%Y%m%d", time.gmtime(now - days * 86400)),
            time.strftime("%Y%m%d", time.gmtime(now + days * 86400)))


def _uniform_series(times, values, step):
    """Compress an evenly sampled series to (t0, step, values), or None.

    The six-minute tide curve is a thousand-odd samples on an exact grid, and
    storing a timestamp beside each one would treble the file for no
    information. If the grid ever has a gap this returns None and the caller
    keeps the explicit times instead of quietly drawing a curve with a hole
    smoothed over.
    """
    if len(times) < 2:
        return None
    for i in range(1, len(times)):
        if abs((times[i] - times[i - 1]) - step) > 1.0:
            return None
    return {"t0": times[0], "step": float(step), "v": values}


MDAPI_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/"


def _coops_meta(station, kind=None):
    """(name, lat, lon) for a station, or (id, None, None) if the lookup fails.

    Best effort on the name -- a wall that says 'SAN FRANCISCO' rather than
    '9414290' is nicer, but not nice enough to lose a whole tide record over.
    The position is not decoration: tide.py normalises its flow field at the
    current station's own coordinates, and refuses to draw a flow map at all
    for a station that is not on the map.
    """
    try:
        url = MDAPI_URL + station + ".json"
        if kind:
            url += "?type=" + kind
        rec = get_json(url, timeout=10)["stations"][0]
        lat = rec.get("lat")
        lon = rec.get("lng", rec.get("lon"))
        return ((rec.get("name") or station).upper(),
                float(lat) if lat is not None else None,
                float(lon) if lon is not None else None)
    except Exception:                                        # noqa: BLE001
        return station, None, None


def _tide_payload(station):
    begin, end = _coops_dates()
    common = dict(product="predictions", application="ft", datum="MLLW",
                  station=station, time_zone="gmt", units="english",
                  format="json", begin_date=begin, end_date=end)

    raw = get_json(_coops_url(**common)).get("predictions") or []
    times = [_coops_epoch(r["t"]) for r in raw]
    heights = [round(float(r["v"]), 2) for r in raw]
    if not times:
        raise ValueError("no predictions for station %s" % station)

    # The hi/lo call is a second request rather than a peak-find on the curve
    # because NOAA's extremes come off the harmonic fit itself: they land
    # between six-minute samples and at a height the sampled curve never quite
    # reaches. Labelling a drawn maximum with a computed one would be off by a
    # few minutes and a few hundredths, every time.
    hilo = get_json(_coops_url(interval="hilo", **common)).get("predictions") or []
    extremes = [{"t": _coops_epoch(r["t"]), "v": round(float(r["v"]), 2),
                 "type": r.get("type", "")} for r in hilo]

    name, lat, lon = _coops_meta(station)
    payload = {"station": station, "name": name, "lat": lat, "lon": lon,
               "datum": "MLLW", "units": "ft",
               "span": [times[0], times[-1]], "extremes": extremes}
    packed = _uniform_series(times, heights, 360.0)
    if packed:
        payload["curve"] = packed
    else:
        payload["curve"] = {"t": times, "v": heights}
    return payload, _coops_url(**common)


def _current_payload(station, interval=30):
    begin, end = _coops_dates()
    common = dict(product="currents_predictions", application="ft",
                  station=station, time_zone="gmt", units="english",
                  format="json", begin_date=begin, end_date=end)

    def cp(**extra):
        d = get_json(_coops_url(**dict(common, **extra)))
        return (d.get("current_predictions") or {}).get("cp") or []

    series = cp(interval=str(interval))
    if not series:
        raise ValueError("no current predictions for station %s" % station)
    times = [_coops_epoch(r["Time"]) for r in series]
    vel = [round(float(r["Velocity_Major"]), 2) for r in series]

    # meanFloodDir / meanEbbDir are repeated on every record and are the whole
    # point of this product: the signed velocity says how hard and which way
    # along the channel, and these two say what "along the channel" means in
    # compass degrees. Without them a sign is just a sign.
    head = series[0]
    events = [{"t": _coops_epoch(r["Time"]),
               "type": (r.get("Type") or "").lower(),
               "v": round(float(r["Velocity_Major"]), 2)}
              for r in cp(interval="MAX_SLACK")]

    name, lat, lon = _coops_meta(station, "currentpredictions")
    payload = {"station": station, "units": "kn",
               "name": name, "lat": lat, "lon": lon,
               "flood_dir": float(head.get("meanFloodDir", 0.0)),
               "ebb_dir": float(head.get("meanEbbDir", 180.0)),
               "bin": head.get("Bin"), "depth": head.get("Depth"),
               "span": [times[0], times[-1]], "events": events}
    packed = _uniform_series(times, vel, interval * 60.0)
    payload["velocity"] = packed if packed else {"t": times, "v": vel}
    return payload, _coops_url(interval=str(interval), **common)


def register_tide_station(station):
    """Register a `tide-<station>` product. Returns the product name."""
    name = "tide-" + station

    def fetch_tide(station=station):
        return _tide_payload(station)

    fetch_tide.__name__ = "_tide_" + station
    product(name, ttl=COOPS_TTL,
            description="NOAA tide predictions, station %s" % station)(fetch_tide)
    return name


def register_current_station(station):
    """Register a `currents-<station>` product. Returns the product name."""
    name = "currents-" + station

    def fetch_current(station=station):
        return _current_payload(station)

    fetch_current.__name__ = "_currents_" + station
    product(name, ttl=COOPS_TTL,
            description="NOAA current predictions, station %s" % station)(fetch_current)
    return name


for _st in [TIDE_STATION] + [s for s in
                             os.environ.get("FT_TIDE_STATIONS", "").split(",") if s]:
    register_tide_station(_st.strip())
for _st in [CURRENT_STATION] + [s for s in
                                os.environ.get("FT_CURRENT_STATIONS", "").split(",") if s]:
    register_current_station(_st.strip())


# --------------------------------------------------------------------------
# GOES GeoColor imagery, from NESDIS STAR. goes.py plays these as a time lapse.
#
# This is the first product whose payload is not numbers, and it changes what
# the cache has to do. The other records here are a few kilobytes of JSON and
# the fetcher rewrites them wholesale every pass. A frame series cannot work
# that way: the source is a 240 kB JPEG every five minutes, a window of them is
# megabytes, and re-fetching the window each pass would put 70 MB an hour
# through the shop wifi to change three frames.
#
# So two things are different. The fetch is **incremental** -- the record lists
# the frame timestamps it already holds, and a pass downloads only the slots
# that are new and drops the ones that have aged out. And the pixels are
# **cooked before they are stored**: each JPEG is decoded, cropped and resized
# to the panel's exact geometry and only the 61 kB result is kept, so a window
# costs a twentieth of what the JPEGs would and, more to the point, the demo
# never decodes anything. Pillow lives on this side of the wall, in the fetcher
# process, next to the sockets. `goes.py` imports numpy and nothing else.
#
# The frames go in a sidecar (see store_blob) as one (N, H, W, 3) uint8 array;
# the JSON record carries the timestamps, the crop, the geometry and the name
# of the sidecar.
#
# The third difference is where that sidecar lands. 3.5 MB rewritten every pass
# is 336 MB a day at the wall's timer, and on a Pi that is SD card wear for
# pixels whose own TTL calls them stale in half an hour, so the sidecar goes to
# tmpfs and only the record goes to the card. See BLOB_DIR at the top of this
# file; nothing in this section has to know about it, because a record names a
# basename and never a directory.
# --------------------------------------------------------------------------

GOES_CDN = "https://cdn.star.nesdis.noaa.gov"
GOES_SAT = os.environ.get("FT_GOES_SAT", "GOES18")      # West; PSW is its sector
GOES_SECTOR = os.environ.get("FT_GOES_SECTOR", "psw")   # Pacific Southwest
GOES_SIZE = os.environ.get("FT_GOES_SIZE", "600x600")

# The scan cadence, and its phase. GOES-18's mesoscale-and-sector schedule puts
# every psw scan start on a minute ending 1, 6, 11 ... -- ten days of the
# directory listing, 2888 files, and not one exception -- so the fetcher can
# name tomorrow's files without asking. That matters more than it sounds: the
# alternative is the HTML index for the directory, which is 3.1 MB (349 kB
# gzipped) of every frame since last week, downloaded to learn three names.
GOES_CADENCE = 300
GOES_PHASE = 60

# How much of the window to keep. 72 frames at five minutes is six hours, which
# is long enough to watch a front arrive and, around dawn or dusk, to carry the
# terminator across the panel. Stored at 320x64 that is 3.5 MB compressed.
GOES_FRAMES = int(os.environ.get("FT_GOES_FRAMES", "72"))
# A cold start is the whole window at 240 kB a frame, so it is capped: a pass
# that only manages part of it leaves a shorter window, which the demo draws,
# and the next pass fills in more.
GOES_MAX_FETCH = int(os.environ.get("FT_GOES_MAX_FETCH", "96"))

# The crop, in source pixels of the 600x600 sector image, and the panel it is
# resized to. 500 x 100 is exactly 5:1, so this is a *crop* and not a squash --
# nothing in the picture is stretched. See goes.py on why this band.
GOES_CROP = (0, 236, 500, 336)
GOES_PANEL = (320, 64)

# The corners of that crop on the ground, north-west round to south-west. The
# sector image is the ABI fixed grid -- a geostationary projection from
# 137.0 W, 56.1 urad a pixel, which is the instrument's own 2 km grid -- and
# these came from fitting that projection to the state borders NESDIS draws on
# the imagery: the 42 N line, the 120 W line, and the corners of Nevada, which
# are surveyed to the metre. Three landmarks fit to under a pixel and the ones
# held back -- Lake Tahoe, the Great Salt Lake, the Salton Sea, San Francisco
# Bay -- land within three. A geostationary grid is not north-up, so the band
# is slightly skewed: its centre line runs from 37.7 N on the left edge to
# 38.1 N on the right. On the ground it is 1127 km by 301 km, which is 3.5 km
# a panel pixel across and 4.7 km down -- the north-south foreshortening of
# looking at 37 N from over the equator at 137 W, not anything done here.
GOES_EXTENT = {"nw": [39.02, -126.23], "ne": [39.48, -113.05],
               "se": [36.80, -114.11], "sw": [36.40, -126.66],
               "km_per_px": [3.52, 4.70], "km": [1127, 301]}

GOES_PRODUCT = "goes-" + GOES_SECTOR
# Imagery arrives every five minutes; a record whose newest frame is half an
# hour old means the fetcher or the CDN has stopped, and the demo says so.
GOES_TTL = 1800


def goes_slots(now=None, count=GOES_FRAMES, cadence=GOES_CADENCE,
               phase=GOES_PHASE):
    """The `count` most recent scan-start epochs at or before `now`."""
    now = time.time() if now is None else now
    newest = ((now - phase) // cadence) * cadence + phase
    return [newest - i * cadence for i in range(count - 1, -1, -1)]


def goes_stamp(epoch):
    """A scan-start epoch as NESDIS names it: YYYYDDDHHMM, UTC."""
    return time.strftime("%Y%j%H%M", time.gmtime(epoch))


def goes_url(epoch, sat=None, sector=None, size=None):
    sat = sat or GOES_SAT
    sector = sector or GOES_SECTOR
    size = size or GOES_SIZE
    # CONUS and FD sit at the top of the tree; everything else is under SECTOR,
    # and the token in the filename is the directory's name in the case NESDIS
    # writes it -- upper for CONUS, as given for a sector.
    if sector.upper() in ("CONUS", "FD"):
        path = "%s/ABI/%s/GEOCOLOR" % (sat, sector.upper())
        token = sector.upper()
    else:
        path = "%s/ABI/SECTOR/%s/GEOCOLOR" % (sat, sector)
        token = sector
    return "%s/%s/%s_%s-ABI-%s-GEOCOLOR-%s.jpg" % (
        GOES_CDN, path, goes_stamp(epoch), sat, token, size)


def _goes_tile(data, crop, panel):
    """One JPEG's bytes -> a (h, w, 3) uint8 tile, cropped and resized.

    Pillow is imported here and only here. It is a fetcher-side dependency in
    the same sense urllib is: the demo must not need it, must not pay for
    importing it, and must not be the thing that discovers it is missing.
    """
    import io
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    im = im.convert("RGB")
    x0, y0, x1, y1 = crop
    if im.size != (600, 600):
        # A different --size was asked for. Scale the crop with it rather than
        # cropping the same pixels out of a different picture, which would
        # quietly be a different piece of California.
        sx, sy = im.size[0] / 600.0, im.size[1] / 600.0
        x0, x1 = int(round(x0 * sx)), int(round(x1 * sx))
        y0, y1 = int(round(y0 * sy)), int(round(y1 * sy))
    tile = im.crop((x0, y0, x1, y1)).resize(panel, Image.LANCZOS)
    return np.asarray(tile, dtype=np.uint8)


def _goes_payload(cache_dir):
    """Top the window up and rewrite the sidecar. Returns (payload, source)."""
    import numpy as np

    wanted = goes_slots()
    want_set = set(int(t) for t in wanted)

    # What survives from last pass. The sidecar is read rather than the JPEGs
    # re-fetched, which is the entire point of storing cooked pixels.
    have = {}
    got = load(GOES_PRODUCT, cache_dir)
    if got is not None:
        blob = load_blob((got[0] or {}).get("blob"), cache_dir)
        if blob is not None and "frames" in blob and "stamps" in blob:
            frames, stamps = blob["frames"], blob["stamps"]
            if (len(frames) == len(stamps)
                    and frames.shape[1:] == (GOES_PANEL[1], GOES_PANEL[0], 3)):
                for t, f in zip(stamps, frames):
                    if int(t) in want_set:
                        have[int(t)] = f

    # Newest first: a pass that runs out of time or wifi should have left the
    # most recent weather on the wall, not the oldest.
    todo = [t for t in reversed(wanted) if int(t) not in have][:GOES_MAX_FETCH]
    fetched = failed = 0
    source = goes_url(wanted[-1])
    for t in todo:
        try:
            have[int(t)] = _goes_tile(get(goes_url(t), timeout=30),
                                      GOES_CROP, GOES_PANEL)
            fetched += 1
        except Exception:                                    # noqa: BLE001
            # A slot can be missing for the ordinary reasons -- the newest one
            # is not posted yet, the scan was pre-empted by a mesoscale
            # request -- and a hole in a time lapse is not an error worth
            # failing the whole product over.
            failed += 1

    stamps = sorted(have)
    if not stamps:
        raise ValueError("no GOES frames could be fetched")
    frames = np.stack([have[t] for t in stamps])
    stamps = np.asarray(stamps, np.float64)

    filename = store_blob(GOES_PRODUCT,
                          {"frames": frames, "stamps": stamps}, cache_dir)
    payload = {
        "blob": filename, "count": int(len(stamps)),
        "stamps": [float(t) for t in stamps],
        "oldest": float(stamps[0]), "newest": float(stamps[-1]),
        "cadence": GOES_CADENCE, "want": len(wanted),
        "sat": GOES_SAT, "sector": GOES_SECTOR, "size": GOES_SIZE,
        "product": "GEOCOLOR", "crop": list(GOES_CROP),
        "panel": list(GOES_PANEL), "extent": GOES_EXTENT,
        "fetched": fetched, "missing": failed,
    }
    prune_blobs(GOES_PRODUCT, filename, cache_dir)
    return payload, source


product(GOES_PRODUCT, ttl=GOES_TTL,
        description="GOES GeoColor time lapse, %d frames at %s"
                    % (GOES_FRAMES, GOES_SECTOR))(_goes_payload)
# Not a flag on product(): marking the spec afterwards keeps the registration
# helper exactly as the other twelve products use it.
PRODUCTS[GOES_PRODUCT]["blob"] = True

# The Bay Area wind field, from Open-Meteo. winds.py draws this.
#
# Everything above fetches a *point*: one gauge, one satellite, one index.
# This one has to fetch a **field**, because the thing worth looking at here
# is a gradient -- the Pacific marine layer accelerating through the Golden
# Gate and losing half its speed by the time it is over Oakland. One station
# cannot say that. Happily Open-Meteo takes comma-separated coordinate lists
# and answers with a JSON *list* of location objects, so a grid is one request
# rather than seventy-seven, which is the difference between a polite client
# and an abusive one.
#
# It is free and keyless, so the arithmetic of not abusing it is worth writing
# down. Open-Meteo counts a multi-location request as one call per location:
# 7x11 points, four times an hour, is 7 392 location-calls a day against a
# 10 000/day fair-use budget and 308/hour against a 5 000/hour one. That is
# the whole reason the grid is 77 points and not 200.
#
# Resolution is chosen to match the model rather than to look impressive.
# Requesting 37.81,-122.48 comes back stamped 37.8268,-122.5061 -- the API
# snaps to the model cell and *tells you where it landed* -- and the distinct
# cells in a request like this one sit about 3 km apart, which is NOAA's HRRR
# CONUS grid. Asking for points closer together than that just returns the
# same cell twice, so the payload stores the snapped coordinates, deduplicated:
# the honest statement of where these numbers actually live.
#
# Direction is stored exactly as the API gives it -- **the compass bearing the
# wind is coming FROM**, which is the meteorological convention and the
# opposite of the direction anything drawn on a map should move. Converting it
# is the demo's job and it is the one bug in this whole demo that would look
# entirely plausible on the wall.
# --------------------------------------------------------------------------

OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"

# lat0, lat1, lon0, lon1. Deliberately a little outside winds.py's map crop
# (37.74-37.90 N, 122.28-122.68 W) so that every pixel of the panel is
# surrounded by data and the interpolation is never an extrapolation.
WIND_EXTENT = (37.70, 37.94, -122.72, -122.24)

# Rows x columns of requested points. See the budget arithmetic above.
WIND_GRID = (7, 11)

# Hours ahead, plus the hour just gone. The extra past hour is what makes
# "now" an interpolation between two model hours rather than an extrapolation
# off the front of the array in the fifty-nine minutes after the top of one.
WIND_FORECAST_HOURS = 30
WIND_PAST_HOURS = 1

# Two hours. This is a forecast, so like the tides the payload keeps telling
# the truth for a while after it was fetched -- but unlike the tides it is a
# *forecast of the weather*, which is a different kind of promise: the run it
# came from is superseded hourly. Past two hours the panel says STALE, and
# when the span itself stops covering now it says nothing at all.
WIND_TTL = 7200


def _wind_grid_env():
    """(nlat, nlon) from FT_WIND_GRID='7x11', or the default."""
    s = os.environ.get("FT_WIND_GRID", "").lower().replace(",", "x")
    try:
        a, b = s.split("x")
        return max(2, int(a)), max(2, int(b))
    except Exception:                                        # noqa: BLE001
        return WIND_GRID


def _linspace(a, b, n):
    return [a + (b - a) * i / (n - 1.0) for i in range(n)]


def _wind_url(extent=WIND_EXTENT, grid=None, hours=WIND_FORECAST_HOURS):
    la0, la1, lo0, lo1 = extent
    nlat, nlon = grid or _wind_grid_env()
    lats, lons = [], []
    for y in _linspace(la0, la1, nlat):
        for x in _linspace(lo0, lo1, nlon):
            lats.append("%.4f" % y)
            lons.append("%.4f" % x)
    from urllib.parse import urlencode
    return OPENMETEO_URL + "?" + urlencode({
        "latitude": ",".join(lats), "longitude": ",".join(lons),
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "wind_speed_unit": "kn", "timezone": "UTC",
        "forecast_hours": hours, "past_hours": WIND_PAST_HOURS,
        "cell_selection": "nearest",
    })


def _iso_hour_epoch(s):
    """'2026-08-09T14:00' in UTC -> epoch seconds."""
    import calendar
    return float(calendar.timegm(time.strptime(s[:16], "%Y-%m-%dT%H:%M")))


def _num(x):
    return None if x is None else float(x)


@product("wind-bay", ttl=WIND_TTL,
         description="Open-Meteo 10 m wind over a grid of the SF Bay Area")
def _wind_bay():
    """A grid of hourly wind, deduplicated onto the model's own cells.

    Gusts ride along because they are the one extra number that changes what
    somebody does about the answer: eighteen knots steady and eighteen gusting
    thirty are different afternoons on the water, and a mean wind speed cannot
    tell them apart. They cost a third of the payload and are drawn as a
    number rather than as a picture, which is about the right weight for them.

    Anything the model declines to answer for comes back as null and is stored
    as null. Dropping the station instead would silently shrink the grid and
    move the interpolation without saying so; the demo can see a hole and
    weight around it, which is the honest version of the same thing.
    """
    rows = get_json(_wind_url(), timeout=40)
    if not isinstance(rows, list):
        # A single-location request answers with an object, not a list. That
        # only happens if someone shrinks the grid to one point, and the rest
        # of this function would silently read it as a dict of hours.
        rows = [rows]

    times = None
    cells = {}
    for r in rows:
        h = r.get("hourly") or {}
        ts = h.get("time") or []
        if not ts:
            continue
        if times is None:
            times = ts
        elif ts != times:
            # Every location in one request shares a time axis. If that ever
            # stops being true, the grid is not a grid.
            raise ValueError("locations disagree about the hourly time axis")
        key = (round(float(r["latitude"]), 4), round(float(r["longitude"]), 4))
        if key in cells:
            continue                    # two requested points, one model cell
        cells[key] = {
            "lat": key[0], "lon": key[1],
            "elev": _num(r.get("elevation")),
            "speed": [None if v is None else round(float(v), 1)
                      for v in h.get("wind_speed_10m", [])],
            "dir": [None if v is None else round(float(v)) % 360
                    for v in h.get("wind_direction_10m", [])],
            "gust": [None if v is None else round(float(v), 1)
                     for v in h.get("wind_gusts_10m", [])],
        }
    if not times or not cells:
        raise ValueError("no usable wind locations in the response")

    grid = [cells[k] for k in sorted(cells)]
    t0 = _iso_hour_epoch(times[0])
    nlat, nlon = _wind_grid_env()
    return {
        "model": "open-meteo best_match (NOAA HRRR over CONUS, ~3 km)",
        "units": {"speed": "kn", "dir": "deg true, FROM", "gust": "kn"},
        "extent": list(WIND_EXTENT), "requested": [nlat, nlon],
        "t0": t0, "step": 3600.0, "n": len(times),
        "span": [t0, t0 + 3600.0 * (len(times) - 1)],
        "grid": grid,
    }, OPENMETEO_URL

# Hyper-local weather for the wall's own address. wx.py draws these.
#
# Three products, from three services, because no single keyless service knows
# what a panel at one street address needs to say. That is not a shortcoming
# to be papered over -- it is the fact the demo is built around, and it is why
# each product records *what kind of number it is* as well as its value:
#
#   wx-obs-<station>   a real observation, from a real instrument, 2.8 km away
#   wx-model-<site>    a numerical forecast evaluated at the exact address
#   wx-air-<site>      a chemistry model's grid cell, likewise
#
# The station is the only measurement anywhere near the building. Unioning the
# station lists of a 7x7 block of NWS gridpoints around the address turns up 52
# stations and exactly one inside San Francisco: SFOC1, "San Francisco
# Downtown", 2.8 km away. The next nearest is Oakland Museum at 12.3 km, on the
# far side of the Bay and in a different climate; KSFO is 16 km south and in
# another one again. Every dedicated PWS network -- Weather Underground,
# PurpleAir, Synoptic, AirNow -- now answers 401 or 403 without a key.
#
# **Assume no field is present.** SFOC1 reports temperature, dewpoint and
# humidity and reports `null` for wind, pressure, gust and visibility, with a
# `Z` quality flag: the fields exist in the JSON and carry nothing. Other
# stations drop other fields, and the same station drops different ones on
# different hours. So every value goes through _nws_value(), which returns None
# unless there is a number *and* the unit code is the one being converted from.
# A missing field must reach the panel as absent, never as zero.
#
# **met.no's terms are honoured here, not in the demo.** They ask for an
# identifying User-Agent with a contact address, and for the Expires header to
# be respected rather than polled through. So: FT_CONTACT (or the default
# below) goes into the UA; the response's Expires and Last-Modified are kept in
# the payload; and a fetch before Expires makes **no request at all**, while a
# fetch after it is conditional on If-Modified-Since and takes the 304 when
# offered. A --loop 900 fetcher therefore touches api.met.no about twice an
# hour, which is roughly how often the model actually changes.
#
# One consequence needs saying: when a fetch is skipped or 304s, the record is
# rewritten with a new `fetched_at` and unchanged contents, so the *fetch* age
# understates the *data* age. Every payload here therefore carries `t`, the
# epoch the numbers describe, and wx.py ages them by that instead. Age is part
# of the data, and the part that matters is the data's, not the socket's.
# --------------------------------------------------------------------------

# Defaults, in the same spirit as tide.py's: somewhere real, so a checkout
# draws a real panel, and overridable so it can be somewhere else. Set
# FT_WX_SITES and FT_WX_STATIONS for your own address and nearest station.
WX_LAT, WX_LON = 37.7627, -122.3966     # the Mission, San Francisco
WX_STATION = "SFOC1"                    # San Francisco Downtown, 2.8 km away

# met.no and NWS both want to know who is calling and how to reach them. This
# is a genuine address, not a decorative one; if this code is run somewhere
# else, set FT_CONTACT so the complaint reaches whoever is actually fetching.
WX_CONTACT = os.environ.get("FT_CONTACT", "jof@thejof.com")
WX_UA = ("flaschen-taschen-wx/1 "
         "(+https://github.com/hzeller/flaschen-taschen; %s)" % WX_CONTACT)

NWS_STATION_URL = "https://api.weather.gov/stations/"
METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
OPENMETEO_AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# An observation is worth believing for about as long as it takes the next one
# to arrive, plus a missed hour. The model products are hourly too, but a
# forecast instant describes a whole hour and does not curdle at the boundary.
WX_OBS_TTL = 5400
WX_MODEL_TTL = 7200


def _wx_http(url, headers=None, timeout=20):
    """(status, headers, body) for a GET, with 304 as a result and not an error.

    ftdata.get() is the right thing for a feed that is simply fetched. This
    exists because met.no's terms are about *how* it is fetched: it needs a
    request header on the way out and two response headers on the way back, and
    a 304 with no body is a success. Imported lazily, like get(), so that
    load() stays free of any network module.
    """
    import urllib.error
    import urllib.request
    hdrs = {"User-Agent": WX_UA}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, dict(e.headers or {}), b""
        raise


def _wx_epoch(iso):
    """An ISO 8601 stamp -> epoch seconds, or None. Accepts 'Z' and offsets."""
    if not iso:
        return None
    try:
        import datetime
        s = iso.strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:                                        # noqa: BLE001
        return None


def _wx_http_date(s):
    """An HTTP-date header -> epoch seconds, or None."""
    if not s:
        return None
    try:
        import email.utils
        return email.utils.mktime_tz(email.utils.parsedate_tz(s))
    except Exception:                                        # noqa: BLE001
        return None


def _wx_site(lat, lon):
    """The product-name suffix for a site: '37.7627_-122.3966'."""
    return "%.4f_%.4f" % (float(lat), float(lon))


def _nws_value(props, key, unit, scale=1.0, offset=0.0):
    """A converted NWS field, or None if it is absent, null or in another unit.

    The unit check is not pedantry. windSpeed arrives as km_h-1 from most
    stations and m_s-1 from a few, and silently applying one conversion to the
    other is how a 5 m/s breeze becomes an 18 m/s gale on a wall in a workshop.
    Unknown unit means unknown number means None.
    """
    field = props.get(key)
    if not isinstance(field, dict):
        return None
    value = field.get("value")
    if value is None or not isinstance(value, (int, float)):
        return None
    if unit and not str(field.get("unitCode", "")).endswith(unit):
        return None
    return float(value) * scale + offset


def _nws_station_meta(station):
    """(name, lat, lon) for a station; (id, None, None) if the lookup fails.

    Best effort, exactly as _coops_meta is: the position is what lets the panel
    print how far away the thermometer is, which is the single most important
    caption on it, but not important enough to lose an observation over.
    """
    try:
        rec = json.loads(_wx_http(
            NWS_STATION_URL + station,
            {"Accept": "application/geo+json"}, timeout=10)[2])
        coords = (rec.get("geometry") or {}).get("coordinates") or []
        props = rec.get("properties") or {}
        return ((props.get("name") or station).upper(),
                float(coords[1]) if len(coords) > 1 else None,
                float(coords[0]) if len(coords) > 1 else None)
    except Exception:                                        # noqa: BLE001
        return station, None, None


def _wx_obs_payload(station):
    """The latest observation from one NWS station, per-field.

    Everything is stored in one unit system -- degrees C, m/s, hPa, percent --
    so the demo never has to know what the station happened to report in.
    """
    url = NWS_STATION_URL + station + "/observations/latest"
    body = _wx_http(url, {"Accept": "application/geo+json"})[2]
    props = json.loads(body).get("properties") or {}

    name, lat, lon = _nws_station_meta(station)
    payload = {
        "station": station, "name": name, "lat": lat, "lon": lon,
        "t": _wx_epoch(props.get("timestamp")),
        "iso": props.get("timestamp"),
        "temp_c": _nws_value(props, "temperature", "degC"),
        "dewpoint_c": _nws_value(props, "dewpoint", "degC"),
        "rh_pct": _nws_value(props, "relativeHumidity", "percent"),
        # Present in the JSON, null at SFOC1, and quite possibly a number at
        # whatever station somebody points this at next.
        "wind_ms": _nws_value(props, "windSpeed", "km_h-1", 1000.0 / 3600.0),
        "gust_ms": _nws_value(props, "windGust", "km_h-1", 1000.0 / 3600.0),
        "wind_dir": _nws_value(props, "windDirection", "degree_(angle)"),
        "pressure_hpa": _nws_value(props, "barometricPressure", "Pa", 0.01),
        "text": (props.get("textDescription") or "").strip() or None,
    }
    if payload["wind_ms"] is None:
        payload["wind_ms"] = _nws_value(props, "windSpeed", "m_s-1")
    return payload, url


# met.no is fetched at most once per Expires, and the state that makes that
# possible lives here rather than in the record, because fetch() hands the
# product function no cache directory. The disk record is consulted once per
# process so a fetcher that has just started does not spend its first pass
# re-requesting something it already has.
_METNO_STATE = {}


def _metno_previous(name):
    if name in _METNO_STATE:
        return _METNO_STATE[name]
    got = load(name)
    prev = None
    if got is not None and isinstance(got[0], dict):
        prev = got[0]
    _METNO_STATE[name] = prev
    return prev


def _wx_model_payload(name, lat, lon):
    """The instant nearest now out of met.no's locationforecast, and nothing else.

    44 kB of hourly forecast arrives and about 300 bytes are kept. A 320x64
    panel has no room for a forecast strip, and storing one so that it could
    have a row later would put a day of numbers on the Pi's flash every quarter
    hour for a row that does not exist.

    See the block comment above on the Expires handling; it is the part of this
    function that matters most, and it is the part that is easiest to delete by
    accident while making some unrelated change.
    """
    from urllib.parse import urlencode
    url = METNO_URL + "?" + urlencode({"lat": round(float(lat), 4),
                                       "lon": round(float(lon), 4)})
    now = time.time()
    prev = _metno_previous(name)

    # Still inside the Expires window the server gave us: there is nothing new
    # behind that URL and asking would be rude. Not an error, not a failure --
    # simply the same numbers again, with their own `t` unchanged.
    if prev and prev.get("expires") and now < float(prev["expires"]):
        return dict(prev), url

    headers = {"Accept": "application/json"}
    if prev and prev.get("last_modified"):
        headers["If-Modified-Since"] = prev["last_modified"]
    status, resp_headers, body = _wx_http(url, headers)

    expires = _wx_http_date(resp_headers.get("Expires"))
    last_modified = resp_headers.get("Last-Modified")
    if status == 304 and prev:
        payload = dict(prev)
        payload["expires"] = expires or (now + 1800.0)
        if last_modified:
            payload["last_modified"] = last_modified
        _METNO_STATE[name] = payload
        return payload, url

    doc = json.loads(body)
    props = doc.get("properties") or {}
    series = props.get("timeseries") or []
    if not series:
        raise ValueError("no timeseries from met.no for %s,%s" % (lat, lon))

    # The entry whose hour contains now, which is the last one at or before it;
    # the first entry if the whole series is somehow in the future.
    chosen, chosen_t = series[0], _wx_epoch(series[0].get("time"))
    for entry in series:
        t = _wx_epoch(entry.get("time"))
        if t is None or t > now:
            break
        chosen, chosen_t = entry, t

    data = chosen.get("data") or {}
    inst = ((data.get("instant") or {}).get("details")) or {}
    next1 = data.get("next_1_hours") or {}

    def val(key):
        v = inst.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    payload = {
        "lat": float(lat), "lon": float(lon),
        "t": chosen_t, "iso": chosen.get("time"),
        "updated_at": _wx_epoch((props.get("meta") or {}).get("updated_at")),
        "temp_c": val("air_temperature"),
        "rh_pct": val("relative_humidity"),
        "pressure_hpa": val("air_pressure_at_sea_level"),
        "cloud_pct": val("cloud_area_fraction"),
        "wind_ms": val("wind_speed"),
        # Meteorological convention: the direction the wind is coming FROM.
        "wind_dir": val("wind_from_direction"),
        "precip_1h": ((next1.get("details") or {}).get("precipitation_amount")),
        "symbol_1h": (next1.get("summary") or {}).get("symbol_code"),
        "label": "MET.NO",
        "expires": expires or (now + 1800.0),
        "last_modified": last_modified,
    }
    _METNO_STATE[name] = payload
    return payload, url


def _wx_air_payload(lat, lon):
    """Open-Meteo's CAMS air quality at a point. Model output, not a sensor.

    The response says which grid cell it actually answered for, and it is not
    the point asked for -- a few kilometres off, typically. That is stored as
    `grid_lat`/`grid_lon` rather than quietly discarded, because "modelled for
    an 11 km cell that contains the Mission" is the honest description of this
    number and the panel is entitled to say so.
    """
    from urllib.parse import urlencode
    url = OPENMETEO_AQ_URL + "?" + urlencode({
        "latitude": round(float(lat), 4), "longitude": round(float(lon), 4),
        "current": "pm2_5,pm10,us_aqi,ozone,nitrogen_dioxide",
        "timezone": "UTC"})
    doc = json.loads(_wx_http(url)[2])
    cur = doc.get("current") or {}
    if not cur:
        raise ValueError("no current air quality for %s,%s" % (lat, lon))

    def val(key):
        v = cur.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    aqi = val("us_aqi")
    return {
        "lat": float(lat), "lon": float(lon),
        "grid_lat": doc.get("latitude"), "grid_lon": doc.get("longitude"),
        "t": _wx_epoch(cur.get("time")), "iso": cur.get("time"),
        "us_aqi": int(round(aqi)) if aqi is not None else None,
        "pm2_5": val("pm2_5"), "pm10": val("pm10"),
        "ozone": val("ozone"), "no2": val("nitrogen_dioxide"),
        "model": "CAMS via Open-Meteo", "label": "OPEN-METEO",
    }, url


def register_wx_station(station):
    """Register a `wx-obs-<station>` product. Returns the product name."""
    name = "wx-obs-" + station

    def fetch_obs(station=station):
        return _wx_obs_payload(station)

    fetch_obs.__name__ = "_wx_obs_" + station
    product(name, ttl=WX_OBS_TTL,
            description="NWS observation, station %s (measured)" % station)(fetch_obs)
    return name


def register_wx_site(lat, lon):
    """Register `wx-model-<site>` and `wx-air-<site>`. Returns both names."""
    site = _wx_site(lat, lon)
    model, air = "wx-model-" + site, "wx-air-" + site

    def fetch_model(name=model, lat=lat, lon=lon):
        return _wx_model_payload(name, lat, lon)

    def fetch_air(lat=lat, lon=lon):
        return _wx_air_payload(lat, lon)

    fetch_model.__name__ = "_wx_model_" + site
    fetch_air.__name__ = "_wx_air_" + site
    product(model, ttl=WX_MODEL_TTL,
            description="met.no forecast at %s (modelled)" % site)(fetch_model)
    product(air, ttl=WX_MODEL_TTL,
            description="Open-Meteo CAMS air quality at %s (modelled)" % site)(fetch_air)
    return model, air


for _st in [WX_STATION] + [s for s in
                           os.environ.get("FT_WX_STATIONS", "").split(",") if s]:
    register_wx_station(_st.strip())
# FT_WX_SITES is 'lat,lon;lat,lon'. The wall's own address is the default
# because that is the address the wall is at.
_wx_sites = [(WX_LAT, WX_LON)]
for _pair in os.environ.get("FT_WX_SITES", "").split(";"):
    if "," in _pair:
        try:
            _a, _b = _pair.split(",", 1)
            _wx_sites.append((float(_a), float(_b)))
        except ValueError:
            print("ftdata: bad FT_WX_SITES entry %r" % _pair, file=sys.stderr)
for _lat, _lon in _wx_sites:
    register_wx_site(_lat, _lon)


# --------------------------------------------------------------------------
# Aircraft over the Bay. adsb.py draws these.
#
# **Which feed, and why not the obvious ones.** Three keyless aggregators
# publish the same shape of JSON, all descended from readsb's `aircraft.json`,
# and they were all tried against this exact query before one was picked:
#
#   api.adsb.lol/v2/point/...        200 OK, `{"ac": [], "total": 0}`. It
#                                    answers, it answers quickly, and it answers
#                                    with nothing. An empty list is not an
#                                    error, so a demo built on this would have
#                                    drawn an honest, permanently empty sky.
#   opendata.adsb.fi/api/v2/...      works; 63 aircraft, 240 ms.
#   api.airplanes.live/v2/point/...  works; 65 aircraft, 250 ms.
#
# The last one is what is used, and adsb.fi is the drop-in second source if it
# ever stops -- the response shapes differ only in that adsb.fi calls the list
# `aircraft` and airplanes.live calls it `ac`. Neither wants a key. Both ask for
# civility rather than credentials: airplanes.live documents roughly one request
# a second, and this asks once a minute.
#
# **Ground traffic is dropped, and counted.** Half of what comes back is parked
# or taxiing -- 36 of 70 on a Sunday morning -- reported as the *string*
# "ground" in `alt_baro` rather than a number. None of it can be dead-reckoned,
# because a pushback tug does not hold a groundspeed and a track, and a heap of
# static dots on the SFO apron is the brightest thing on the panel for the worst
# possible reason. So the record keeps the airborne ones and stores the ground
# count as a number, which is the honest version of throwing them away: the
# panel can say "34 airborne, 36 on the ground" and mean it.
#
# **The payload is columnar**, one list per field rather than one dict per
# aircraft, and that is worth about 40% of the bytes at this size -- 120
# aircraft do not need the string "alt" repeated 120 times. It also happens to
# be exactly what the demo wants, since every one of these columns becomes a
# numpy array in build() and nothing has to be transposed on a 600 MHz Pi.
#
# **Every aircraft carries its own position age.** `seen_pos` is how long ago
# that aircraft's position was last heard, and it is not the same as the age of
# the fetch: a jet over the Gate updates twice a second and something in the
# hills behind Livermore may not have been heard for half a minute. The demo
# dead-reckons from `t - pa` per aircraft rather than from one timestamp for the
# whole record, which costs one float a plane and is the difference between a
# picture that is a minute old and one that is a minute old *and knows it*.
#
# One minute is the interval and five is the TTL, and the gap between them is
# deliberate: at 500 knots a minute of extrapolation is 8 nm, which the dead
# reckoning covers, and five minutes is 40 nm, which nothing covers. Past the
# TTL the demo stops drawing aircraft rather than drawing fiction. The record is
# `volatile` because it is rewritten 1440 times a day and is worthless two
# minutes later; none of that belongs on the flash card the Pi boots from.
# --------------------------------------------------------------------------

ADSB_URL = "https://api.airplanes.live/v2/point/%.4f/%.4f/%d"

# The wall's own address, in the Mission. Everything on the panel is measured
# from here, so this is the one number to change for another installation.
ADSB_LAT, ADSB_LON = 37.7627, -122.3966

# Nautical miles. Comfortably outside adsb.py's map crop, which reaches about
# 32 nm at its far corner, so the panel is never showing the edge of the query
# rather than the edge of the sky.
ADSB_RADIUS_NM = 50

# The nearest this many are kept. Two hundred-odd arrive at a busy hour, a
# 320x64 panel is a mess above about fifty, and the ones that get cut are by
# construction the furthest away and the least likely to be on the map at all.
ADSB_MAX = 120

ADSB_TTL = 300
ADSB_INTERVAL = 60

# A truthful User-Agent, with an address that reaches whoever is fetching.
# Deliberately not ftdata.get()'s generic one: this is a volunteer-run feed
# being asked for something 1440 times a day, and it is entitled to know who is
# asking. Set FT_CONTACT if that is not the person below.
ADSB_UA = ("flaschen-taschen-adsb/1 (+https://github.com/hzeller/flaschen-taschen; %s)"
           % os.environ.get("FT_CONTACT", "jof@thejof.com"))


def _adsb_num(x):
    """A finite number, or None. Rejects the string 'ground' and every null."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if x == x and abs(x) != float("inf") else None


@product("adsb-bay", ttl=ADSB_TTL, interval=ADSB_INTERVAL, volatile=True,
         description="airborne ADS-B within %d nm of the wall, from "
                     "airplanes.live" % ADSB_RADIUS_NM)
def _adsb_bay():
    """The airborne traffic around the wall, trimmed to what a panel can draw.

    Rounding is chosen against what a pixel is worth rather than against what
    looks tidy. One panel column is about 300 m, so four decimal places of
    latitude (11 m) is already three hundred times finer than anything that can
    be seen, and whole degrees of track put a 500 kt aircraft 0.7 km off after
    five whole minutes of extrapolation -- which is a fifth of the error the
    minute-old fix itself carries. Everything is stored as int where an int can
    say it, because JSON writes `12725` in five bytes and `12725.0` in seven.

    An aircraft with no track is dropped rather than drawn stationary. Every
    airborne aircraft in a day of samples had one; the ones that do not are
    TIS-B and MLAT shadows whose position is a guess in the first place, and a
    mark that sits still on a map where everything else is moving reads as a
    bug rather than as an aircraft.
    """
    import urllib.request
    url = ADSB_URL % (ADSB_LAT, ADSB_LON, ADSB_RADIUS_NM)
    req = urllib.request.Request(url, headers={"User-Agent": ADSB_UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        doc = json.loads(resp.read())

    seen = doc.get("ac")
    if not isinstance(seen, list):
        # adsb.fi calls the same list `aircraft`. Accepting both costs a line
        # and makes swapping the source a one-line change to ADSB_URL.
        seen = doc.get("aircraft")
    if not isinstance(seen, list):
        raise ValueError("no aircraft list in the response from %s" % url)

    # readsb reports `now` in milliseconds since the epoch. Falling back to the
    # local clock rather than failing: the positions are still good, and a demo
    # that dead-reckons from a clock a second out is not measurably wrong.
    served = _adsb_num(doc.get("now"))
    t = served / 1000.0 if served and served > 1e11 else time.time()

    ground = 0
    rows = []
    for a in seen:
        if a.get("alt_baro") == "ground":
            ground += 1
            continue
        lat, lon = _adsb_num(a.get("lat")), _adsb_num(a.get("lon"))
        alt, gs = _adsb_num(a.get("alt_baro")), _adsb_num(a.get("gs"))
        trk = _adsb_num(a.get("track"))
        if None in (lat, lon, alt, gs, trk):
            continue
        # The callsign is what a person reads; the registration is the fallback
        # for the ones flying without one, and the ICAO address is the fallback
        # for that. Something is always printable, and none of it is invented.
        call = str(a.get("flight") or "").strip() or str(a.get("r") or "").strip()
        rows.append((_adsb_num(a.get("dst")) or 0.0, {
            "hex": str(a.get("hex") or "")[:6],
            "call": call[:8] or None,
            "type": (str(a.get("t")).strip()[:4] if a.get("t") else None),
            "cat": (str(a.get("category")).strip()[:2] if a.get("category") else None),
            "lat": round(lat, 4), "lon": round(lon, 4),
            "alt": int(round(alt)), "gs": int(round(gs)),
            "trk": int(round(trk)) % 360,
            "dst": round(_adsb_num(a.get("dst")) or 0.0, 1),
            "pa": round(max(0.0, _adsb_num(a.get("seen_pos")) or 0.0), 1),
        }))

    rows.sort(key=lambda r: r[0])
    kept = [r[1] for r in rows[:ADSB_MAX]]
    cols = ("hex", "call", "type", "cat", "lat", "lon", "alt", "gs", "trk",
            "dst", "pa")
    payload = {
        "origin": [ADSB_LAT, ADSB_LON], "radius_nm": ADSB_RADIUS_NM,
        "t": t, "n": len(kept), "n_air": len(rows), "n_ground": ground,
        "n_seen": len(seen), "capped": len(rows) > len(kept),
        "units": {"alt": "ft baro", "gs": "kn", "trk": "deg true",
                  "dst": "nm", "pa": "s since position last heard"},
        "source": "airplanes.live",
    }
    payload.update({c: [r[c] for r in kept] for c in cols})
    return payload, url


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
    ap.add_argument("--due", action="store_true",
                    help="skip products fetched within their own interval")
    ap.add_argument("--fast", action="store_true",
                    help="only products with an interval of --fast-under or less")
    ap.add_argument("--fast-under", type=float, default=FAST_INTERVAL,
                    help="what --fast means, in seconds")
    args = ap.parse_args()

    if args.list:
        for name in sorted(PRODUCTS):
            got = load(name, args.cache_dir)
            age = "absent" if got is None else describe_age(got[1]) + " old"
            every = interval_for(name)
            print("  %-22s ttl %-7s every %-7s %-9s %s%s"
                  % (name, "%ds" % PRODUCTS[name]["ttl"],
                     describe_age(every) if every else "pass", age,
                     PRODUCTS[name]["description"],
                     " [tmpfs]" if is_volatile(name) else ""))
        return

    only = set(x for x in args.only.split(",") if x)
    max_interval = args.fast_under if args.fast else None
    if not args.loop:
        n, seen = fetch_all(args.cache_dir, only, args.due, max_interval)
        print("ftdata: %d/%d products refreshed" % (n, seen))
        return
    while True:
        started = time.time()
        n, seen = fetch_all(args.cache_dir, only, args.due, max_interval)
        print("ftdata: %d/%d refreshed" % (n, seen), flush=True)
        time.sleep(max(5.0, args.loop - (time.time() - started)))


if __name__ == "__main__":
    main()
