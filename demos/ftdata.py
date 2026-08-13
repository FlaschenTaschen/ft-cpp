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

# The installation's own address, which three products here fetch for. Kept in
# demos/site.json rather than written out once per product; see ftsite.py. It
# imports nothing but the standard library, so this stays a cheap import and
# load() still touches no network module.
import ftsite

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


# --------------------------------------------------------------------------
# An hour of solar wind, measured at L1 and propagated to the bow shock, plus
# the aurora oval's hemispheric power. solarwind.py draws this as a picture;
# propagation.py reads the latest speed and Bz off it for its readout.
#
# **This product used to be two.** `swpc_solarwind` fetched the latest scalar
# speed and Bz off SWPC's summary endpoints for propagation's instrument
# panel, and a second product fetched this propagated series for solarwind's
# picture -- two requests to the same agency for overlapping data. They are
# one product now, because the series already contains the scalars: its last
# row is the most recent minute at L1, which is exactly what "latest" means on
# the readout. One fetch, one cache entry, and the two panels can no longer
# disagree with each other about what the wind is doing.
#
# The consequence for consumers is that `speed` and `bz` here are *arrays*,
# not numbers; the scalars live in `latest`. propagation.py reads them through
# a helper that accepts either shape, so a wall still holding an old
# scalar-shaped record keeps drawing until the next fetch replaces it.
# --------------------------------------------------------------------------

L1_WIND_URL = ("https://services.swpc.noaa.gov/products/geospace/"
               "propagated-solar-wind-1-hour.json")
AURORA_POWER_URL = ("https://services.swpc.noaa.gov/text/"
                    "aurora-nowcast-hemi-power.txt")

# One column of a 320 px panel is worth about three minutes of this; sixty
# samples is one a minute, which is finer than anything downstream can show and
# still only about a kilobyte and a half of JSON.
L1_WIND_SAMPLES = 60


def _l1_num(value, places):
    """A rounded float, or None for null, empty, '(n/a)' and NaN alike."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    f = round(f, places)
    return int(f) if places <= 0 else f


def _aurora_hemispheric_power():
    """The last row of the Ovation hemispheric power table, in gigawatts.

    Fixed-width text with a comment header, five-minute cadence, one file a
    day -- so shortly after 00:00 UTC the file has a header and one row in it,
    and the last data line is the only line worth having. It is read by taking
    the last line that parses rather than by counting from the end, because the
    file sometimes ends in a blank line and sometimes does not.
    """
    text = get(AURORA_POWER_URL).decode("ascii", "replace")
    out = {"north_gw": None, "south_gw": None, "t": None}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        north, south = _l1_num(parts[2], 0), _l1_num(parts[3], 0)
        if north is None or south is None:
            continue
        out = {"north_gw": north, "south_gw": south, "t": parts[0]}
    return out


@product("swpc_solarwind", ttl=3600, interval=600,
         description="an hour of propagated L1 solar wind, plus aurora power")
def _swpc_solarwind():
    """Sixty minutes of wind in flight, as four parallel arrays.

    The file is a header row followed by data rows, which is SWPC's usual
    `/products/` shape and the reason for the column-name lookup: the column
    order has moved before and reading `row[6]` for bz would fail silently and
    draw a beautiful wrong picture rather than raise.

    Parallel arrays rather than a list of records, because at sixty samples the
    repeated keys are two thirds of the file. Speed to the nearest km/s,
    density and field to a tenth: that is finer than a 320 px panel can
    resolve, and the float64 reprs would triple the record for nothing.

    Arrays are oldest first, which is the order the file is in and also the
    order of *distance from the Sun*: the oldest sample is the one nearest the
    Earth. Anything drawing this wants it that way round; anything printing
    'now' wants the last element, which is what `latest` is for.
    """
    rows = get_json(L1_WIND_URL)
    if not rows or not isinstance(rows[0], list):
        raise ValueError("propagated solar wind: no header row")
    col = {}
    for i, name in enumerate(rows[0]):
        col[str(name)] = i
    for key in ("time_tag", "speed", "density", "bz", "bt",
                "propagated_time_tag"):
        if key not in col:
            raise ValueError("propagated solar wind: no %r column" % key)
    data = [r for r in rows[1:] if isinstance(r, list) and len(r) >= len(col)]
    if not data:
        raise ValueError("propagated solar wind: no data rows")

    # Thin from the newest end, so the most recent minute is always kept: it is
    # the one the panel prints as the current wind speed.
    step = max(1, -(-len(data) // L1_WIND_SAMPLES))
    keep = list(reversed(data[::-step]))[-L1_WIND_SAMPLES:]

    def column(key, places):
        return [_l1_num(r[col[key]], places) for r in keep]

    speed = column("speed", 0)
    density = column("density", 1)
    bz = column("bz", 1)
    bt = column("bt", 1)

    latest = {"t": None, "speed": None, "density": None, "bz": None,
              "bt": None, "arrival": None}
    for i in range(len(keep) - 1, -1, -1):
        if speed[i] is not None and bz[i] is not None:
            latest = {"t": keep[i][col["time_tag"]],
                      "speed": speed[i], "density": density[i],
                      "bz": bz[i], "bt": bt[i],
                      "arrival": keep[i][col["propagated_time_tag"]]}
            break

    try:
        aurora = _aurora_hemispheric_power()
    except Exception:                                        # noqa: BLE001
        # The oval's power is a garnish -- it sets how brightly the poles glow
        # and nothing else. A panel with a stream, a field and a magnetopause
        # is still the panel; one that failed to draw because a text file was
        # briefly a 500 is not.
        aurora = {"north_gw": None, "south_gw": None, "t": None}

    return {
        "speed": speed, "density": density, "bz": bz, "bt": bt,
        "t_first": keep[0][col["time_tag"]],
        "t_last": keep[-1][col["time_tag"]],
        "arrival_first": keep[0][col["propagated_time_tag"]],
        "arrival_last": keep[-1][col["propagated_time_tag"]],
        "samples": len(keep), "minutes_per_sample": step,
        "latest": latest, "aurora": aurora,
        "units": {"speed": "km/s", "density": "protons/cm^3",
                  "bz": "nT GSM", "bt": "nT", "aurora": "GW",
                  "t": "UTC, measured at L1",
                  "arrival": "UTC, propagated to the bow shock"},
    }, L1_WIND_URL


# --------------------------------------------------------------------------
# The global routing table, churning, as San Francisco hears it. bgp.py draws
# it as a per-second chart of the last quarter hour with a ticker of the actual
# prefixes underneath.
#
# **Why RouteViews at SFMIX and not the obvious live feed.** RIPE's RIS Live
# streams the whole default-free zone over plain HTTP as newline-delimited
# JSON, and it was the first thing tried here. It works, and it is the wrong
# tool for this panel for two reasons. Unfiltered it delivered 78 MB in 25
# seconds -- that is not going anywhere near a Pi on shop wifi -- and even
# filtered to one collector it can only ever be *sampled*: the fetcher opens
# the socket, reads for twenty seconds, and closes it, so the other hundred and
# sixty seconds of every three minutes are simply not observed. A burst that
# lasted a minute would be missed entirely, and a chart that silently omits the
# interesting parts is a worse chart than a coarser one that does not.
#
# The RouteViews archive has the opposite shape. Every collector writes a
# complete MRT dump of every update it saw in each fifteen-minute window and
# publishes it about a minute after the window closes, bzip2'd. One 1.2 MB file
# gets **the entire window**, 75,000 messages, with per-second resolution and
# nothing sampled away. It is a quarter of an hour behind, and that is a trade
# worth making: this panel is about rate and texture, not about the last
# second, and the age is on the screen anyway.
#
# And the collector is `route-views.sfmix` -- RouteViews' vantage point inside
# the San Francisco Metropolitan Internet Exchange, which is a couple of miles
# from the wall and is where the makerspace's own ISP hands off its traffic.
# The routes this panel draws are the ones the room's packets are actually
# steered by, which is not a claim any of the other collectors could make.
# Eight networks peer with it -- Cloudflare and Amazon among them -- so the
# feed is a genuinely local view of a global table rather than a global average
# of one.
#
# **MRT is parsed here, by hand, and that needs justifying.** The usual answer
# is libbgpstream or mrtparse, and neither is going on a Pi for this: the first
# is a C library with a build, the second pulls in a dependency tree to do
# something this file already does for PDFs. The wire format is RFC 6396 and
# RFC 4271 and the part of it a churn counter needs is small -- walk the record
# frames, find the BGP UPDATEs, count the prefixes in the withdrawn block, the
# NLRI block and the two multiprotocol attributes, and read the AS_PATH. What
# is deliberately *not* implemented is everything else: communities, MED,
# aggregators, the legacy two-byte-ASN subtypes nobody has emitted this decade.
# An attribute this does not understand is skipped by its own length field,
# which is why an unknown one cannot desynchronise the parse.
#
# **Where the cost is.** 12.5 MB of MRT and 75,000 records is 0.5 s of pure
# Python on a desktop, so call it ten on the wall, once every fifteen minutes,
# in the fetcher process and never in a demo. Both ends are capped anyway --
# see BGP_MAX_BZ2, BGP_MAX_MRT and BGP_MAX_RECORDS -- and a capped parse says
# so in the record rather than quietly reporting a low rate.


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
# NDBC buoy observations -- what the ocean is actually doing, as opposed to
# what the harmonic fit above says it will do. swell.py draws these.
#
# Every product above this line is a *prediction* served as JSON. This one is a
# measurement served as fixed-width text by a machine that has been publishing
# the same format since before JSON, and both of those differences matter.
#
# The file is newest row first, which is the good luck this section is built
# on: the last day of a buoy's life is the first sixteen kilobytes of a file
# that runs to six hundred, so a ranged GET takes the part we want and leaves
# the rest on the server. NDBC serve these off CloudFront and honour
# `Range:` with a 206; a server that ignored it would answer 200 with the whole
# file and the parse below would still be right, just dearer. That is the only
# reason this is safe to do at a ten-minute cadence over shop wifi.
#
# `MM` means missing and it is *common*: a buoy with a dead wave sensor keeps
# reporting wind and water temperature for months, and the sample this was
# written against had WVHT on one row, WTMP on the next and neither on the one
# after. So nothing here assumes a row is complete. Each headline value is
# taken from the newest row that actually has it, and it carries the time of
# *that* row, because "1.9 m" and "1.9 m, six hours ago" are different claims
# and only one of them is worth animating.
#
# Two files, because they answer different questions. `.txt` is the ten-minute
# standard meteorological record -- one significant wave height, one dominant
# period, one mean direction, which is the sea summarised as though it were a
# single wave. `.spec` is the directional spectral summary, and it splits that
# into the swell and the windsea with a height, a period and a direction each.
# That split is the single most useful thing the data says: 1.9 m at 9 s out of
# the northwest with a 0.4 m windsea on top is a clean groundswell, and the
# same 1.9 m as 1.3 m of swell under 1.4 m of 4-second slop is a completely
# different afternoon in a boat. Both files are parsed the same way, off their
# own header line rather than off a hardcoded column order, so a column added
# at the end of either does not silently shift everything after it.
# --------------------------------------------------------------------------

NDBC_REALTIME = "https://www.ndbc.noaa.gov/data/realtime2/"

# An hour. The buoy reports every ten minutes, so a record that has not been
# refreshed in six cycles means the fetcher or the network is down rather than
# that the sea has stopped, and the panel should say so. Note this is the age of
# the *fetch*; the age of the newest observation inside it is a separate number
# and swell.py shows that one too -- see NDBC_HOURS.
NDBC_TTL = 3600

# The buoy's own cadence, and no faster. NDBC ask for a descriptive User-Agent
# and for restraint; `get()` already sends the former and this is the latter.
NDBC_INTERVAL = 600

# How much history to keep. A day is what the demo plots; the extra two hours
# are so a 24-hour window still fills after the fetcher has missed a pass or
# two, and so the newest-row search has somewhere to look when a sensor has
# been quiet for a while.
NDBC_HOURS = 26
NDBC_STEP = 600.0

# Ranged-GET sizes. A standard-meteorological row is about a hundred bytes and
# a spectral row about seventy, so 64 kB is four days of the first and 8 kB is
# a day and a half of the second -- generous by a factor of three either way,
# and still a fortieth of what fetching the whole file would cost.
NDBC_BYTES = 65536
NDBC_SPEC_BYTES = 8192

# Station names. NDBC publish these in a 400 kB table of every buoy on the
# planet, which is not worth a request to put four words on a wall, so the
# handful anybody here would point this at are written down and everything else
# falls back to its number.
NDBC_NAMES = {
    "46026": "SF 18NM W", "46237": "SF BAR", "46013": "BODEGA BAY",
    "46012": "HALF MOON BAY", "46042": "MONTEREY", "46059": "W CALIFORNIA",
    "46022": "EEL RIVER", "46028": "CAPE SAN MARTIN", "46214": "POINT REYES",
}

# The compass points the spectral file uses for direction, in the order that
# makes the index the bearing. `.spec` gives a point and not a number -- the
# directional estimate is not worth a degree -- so this is the whole conversion.
NDBC_POINTS = ("N NNE NE ENE E ESE SE SSE "
               "S SSW SW WSW W WNW NW NNW").split()


def _ndbc_get(station, ext, nbytes):
    """The first `nbytes` of a realtime2 file, as text.

    Ranged, because these files are newest-first and we want the top of one.
    Falls back to whatever the server sends: a 200 with the lot is a slow
    success, not a failure, and the parser stops at the cutoff either way.
    """
    import urllib.request
    url = NDBC_REALTIME + station + ext
    req = urllib.request.Request(url, headers={
        "User-Agent": "flaschen-taschen-ftdata/1 (+wall display)",
        "Range": "bytes=0-%d" % (nbytes - 1)})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(nbytes)
    return raw.decode("ascii", "replace"), url


def _ndbc_num(s):
    """A column as a float, or None for NDBC's `MM` and anything unparseable."""
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _ndbc_rows(text, cutoff):
    """Parse a realtime2 file into (epoch, {column: text}) newest first.

    Stops at the first row older than `cutoff`, which is what keeps this cheap:
    the rows are in descending time order, so the loop touches a day of them and
    returns. A truncated last line -- the guaranteed consequence of a ranged GET
    landing mid-row -- has the wrong field count and is dropped by the same test
    that drops a corrupt one.

    Column names come from the file's own `#YY MM DD ...` header rather than
    from a list here. The two files this parses have different columns, the
    order has changed once in NDBC's history, and reading the header costs one
    line.
    """
    import calendar
    names, out = None, []
    for line in text.splitlines():
        if line.startswith("#"):
            if names is None:
                names = line[1:].split()
            continue
        f = line.split()
        if names is None or len(f) != len(names) or len(f) < 6:
            continue
        try:
            t = float(calendar.timegm(
                (int(f[0]), int(f[1]), int(f[2]), int(f[3]), int(f[4]), 0,
                 0, 1, -1)))
        except ValueError:
            continue
        if t < cutoff:
            break
        out.append((t, dict(zip(names[5:], f[5:]))))
    return out


def _ndbc_latest(rows, key):
    """The newest row that actually has `key`, as (value, time), or (None, None).

    The reason this is a search and not `rows[0][key]`: a buoy whose wave sensor
    has failed still reports wind and pressure every ten minutes, and the newest
    row is then a row with `MM` where the interesting number goes. Taking the
    newest *present* value and carrying its own timestamp is the only way to
    show one without implying the other is that fresh.
    """
    for t, r in rows:
        v = _ndbc_num(r.get(key))
        if v is not None:
            return v, t
    return None, None


def _ndbc_train(rows, hkey, pkey, dkey):
    """One wave train out of the spectral file: height, period, direction.

    All three from the *same* row, deliberately. Mixing this hour's swell height
    with last hour's swell direction would draw a wave train that never existed,
    and the whole point of the panel is that the picture is the measurement.
    """
    for t, r in rows:
        h = _ndbc_num(r.get(hkey))
        p = _ndbc_num(r.get(pkey))
        if h is None or p is None or p <= 0:
            continue
        pt = (r.get(dkey) or "").upper()
        deg = NDBC_POINTS.index(pt) * 22.5 if pt in NDBC_POINTS else None
        return {"h": round(h, 2), "p": round(p, 1), "dir": deg, "pt": pt or "",
                "t": t}
    return None


def _ndbc_history(rows, keys, hours=NDBC_HOURS, step=NDBC_STEP):
    """The last `hours` of a few columns, resampled onto an exact grid.

    Explicit timestamps beside every sample would double the record for no
    information, and the buoy is already on a ten-minute grid; what it is not is
    *gapless*, so a hole stays a hole. `null` in these arrays means the buoy did
    not report, and swell.py draws a gap there rather than interpolating across
    it -- a trend line that closes over a six-hour outage is a claim nobody
    measured.
    """
    if not rows:
        return None
    n = int(hours * 3600.0 / step)
    t1 = round(rows[0][0] / step) * step
    t0 = t1 - (n - 1) * step
    out = {"t0": t0, "step": step, "n": n}
    for spec in keys:
        out[spec[0]] = [None] * n
    for t, r in rows:
        i = int(round((t - t0) / step))
        if not (0 <= i < n):
            continue
        for name, key, nd in keys:
            v = _ndbc_num(r.get(key))
            if v is not None and out[name][i] is None:
                out[name][i] = round(v, nd)
    return out


def _ndbc_payload(station):
    """One buoy: the present sea state, the spectral split, and a day of trend."""
    cutoff = time.time() - NDBC_HOURS * 3600.0
    text, url = _ndbc_get(station, ".txt", NDBC_BYTES)
    rows = _ndbc_rows(text, cutoff)
    if not rows:
        raise ValueError("no recent rows for NDBC station %s" % station)

    payload = {"station": station,
               "name": NDBC_NAMES.get(station, station).upper(),
               "units": {"h": "m", "p": "s", "dir": "degT", "spd": "m/s",
                         "temp": "degC"}}
    # Each headline value with the time of the row it came from. See
    # _ndbc_latest() on why they are not all the same time.
    for name, key, nd in (("wvht", "WVHT", 2), ("dpd", "DPD", 1),
                          ("apd", "APD", 1), ("mwd", "MWD", 0),
                          ("wspd", "WSPD", 1), ("wdir", "WDIR", 0),
                          ("gst", "GST", 1), ("wtmp", "WTMP", 1),
                          ("atmp", "ATMP", 1), ("pres", "PRES", 1)):
        v, t = _ndbc_latest(rows, key)
        payload[name] = None if v is None else round(v, nd)
        payload[name + "_t"] = t
    payload["hist"] = _ndbc_history(
        rows, (("wvht", "WVHT", 2), ("dpd", "DPD", 1)))

    # The spectral summary is a nice-to-have and is allowed to fail on its own:
    # not every station publishes one, and a panel that can draw a single wave
    # train from the standard file is much better than a panel that draws
    # nothing because the second request timed out.
    try:
        stext, surl = _ndbc_get(station, ".spec", NDBC_SPEC_BYTES)
        srows = _ndbc_rows(stext, cutoff)
        payload["swell"] = _ndbc_train(srows, "SwH", "SwP", "SwD")
        payload["windsea"] = _ndbc_train(srows, "WWH", "WWP", "WWD")
        if srows:
            payload["steepness"] = (srows[0][1].get("STEEPNESS") or "").upper()
            payload["spec_t"] = srows[0][0]
        payload["spec_url"] = surl
    except Exception as e:                                   # noqa: BLE001
        print("ftdata: %s spectral summary unavailable: %r" % (station, e),
              file=sys.stderr)
        payload["swell"] = payload["windsea"] = None
    return payload, url


def register_buoy(station):
    """Register an `ndbc-<station>` product. Returns the product name."""
    name = "ndbc-" + station

    def fetch_buoy(station=station):
        return _ndbc_payload(station)

    fetch_buoy.__name__ = "_ndbc_" + station
    product(name, ttl=NDBC_TTL, interval=NDBC_INTERVAL,
            description="NDBC buoy %s: waves, wind and a day of trend"
                        % station)(fetch_buoy)
    return name


SWELL_BUOY = "46026"                    # 18 nm west of the Golden Gate

for _bu in [SWELL_BUOY] + [s for s in
                           os.environ.get("FT_BUOYS", "").split(",") if s]:
    register_buoy(_bu.strip())


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
WX_LAT, WX_LON = ftsite.LAT, ftsite.LON  # Dogpatch/Potrero, San Francisco
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
    """The product-name suffix for a site: '37.7625_-122.3997'."""
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
# The Wikipedia edit firehose. wiki.py draws this.
#
# Wikimedia publishes every change to every one of its nine hundred-odd wikis
# on one keyless public SSE stream, worldwide, in real time. It is the only
# source in this file that is a stream of *events* rather than a snapshot of a
# state, and that difference is the whole product: a snapshot can be fetched,
# but a firehose can only be sampled. So this fetcher opens the socket, listens
# for forty seconds, aggregates what went past, and hangs up. It is a periodic
# process like everything else here, not a daemon -- ftsched builds segments on
# a worker thread and a fetcher holding a socket open forever would be a second
# long-lived thing on the Pi for no benefit, since the panel replays a window
# rather than tracking a live cursor.
#
# **What forty seconds of it looks like**, measured rather than assumed:
# roughly 1500 messages, 36 a second, from about 40 distinct wikis, 61% of them
# flagged as bot edits. Slightly over half are `categorize` -- MediaWiki
# emitting one message per category as a page's categories change, which is
# machine bookkeeping about an edit that already has its own message -- so those
# are dropped, along with `log` (account creation, blocks, deletions: the
# identity-adjacent half of the stream and nothing this panel wants). What is
# kept is `edit` and `new`: an actual revision of an actual page. That is about
# 15 a second, and it is the number the panel puts on the wall.
#
# The traffic is the real cost. Forty seconds is about 1.7 MB off the wire,
# because every message carries the full rendered HTML of the edit summary
# whether you want it or not and there is no server-side filter to ask for
# less. At a fifteen-minute interval that is ~7 MB an hour on shop wifi, which
# is the largest number in this file and is why the interval is not shorter.
# The record it becomes is about 12 kB.
#
# **PRIVACY -- what is thrown away, in this function, before anything is
# stored.** Every message carries `user`, which is a username for a registered
# editor and a bare IP address for an anonymous one. It is never read into any
# structure here: there is no field for it in the payload, no hash of it, no
# count keyed on it. Nor is there any handle that could be turned back into it:
#
#   * `user`                 -- dropped outright. Identity, and for anonymous
#                               editors a home or workplace IP address.
#   * `comment`/`parsedcomment` -- dropped. Edit summaries are free text written
#                               by a person and routinely name people.
#   * `revision.old/new`, `id`, `meta.id`, `notify_url` -- dropped. A revision
#                               id is a one-call lookup back to its author, so
#                               keeping one would be keeping `user` in a costume.
#   * titles outside namespace 0 -- dropped. `User:Someone/sandbox` and its Talk
#                               page are a person's name in the title field, and
#                               main-space article titles are the only ones this
#                               panel wanted anyway.
#
# What is kept is the public shape of the encyclopedia and nothing about who
# wrote it: article titles, the wiki's own database name, the byte length before
# and after as a single delta, the bot flag, whether the page was new, and the
# millisecond the edit happened. None of it is reversible to a person by any
# means this record provides.
#
# **The titles need a Latin alphabet and most of the stream does not have one.**
# The demo draws in a 3x5 bitmap font -- A-Z, digits and a handful of
# punctuation -- and about 44% of main-space titles in any given window are
# Cyrillic, CJK, Devanagari, Arabic or Hebrew. A panel of tofu boxes is a
# failure, so the split is: **colour carries every project including the ones
# whose script cannot be drawn, and type carries only the ones that can.** The
# counts, the rate, the bot share and the coloured strokes are the whole
# firehose; the ticker is the subset this font can spell, and the panel says so.
# Accented Latin is folded rather than rejected (NFD, drop the combining marks,
# plus a short table for the letters that do not decompose -- ss, ae, o, th),
# which is what keeps es, fr, de, pt, pl and no in the ticker instead of only
# en. `n_titles_seen` records how many main-space titles went past so the demo
# could say what fraction survived if it ever wanted to.
#
# **The events are stored columnar and in relative time.** Three parallel lists
# -- millisecond offset from the window start, byte delta, project index -- plus
# a flags int, rather than a dict per edit; at 600 events that is worth about
# half the bytes. Relative rather than absolute time is deliberate on both
# counts: it is four digits instead of thirteen, and it is what lets the demo
# replay the window at its true internal pacing without also publishing the
# exact wall-clock second any particular article was edited.
# --------------------------------------------------------------------------

SEQUOIA_CAL_URL = "https://sequoia.garden/api/calendar.json"

# Six hours: see above, this is a dead-fetcher detector and not a shelf life.
SEQUOIA_CAL_TTL = 21600

# Hourly. 2.7 kB on the wire against our own solar-powered server, which is why
# this is not five minutes: `solar-garden` in this same file explains what a
# request costs that machine.
SEQUOIA_CAL_INTERVAL = 3600

# How far past its end an event stays on the record. The feed appears to publish
# only future events, so in practice this keeps whatever is running *right now*
# -- the single most valuable state the panel has -- plus anything that finished
# earlier today, so the panel can draw this evening honestly rather than
# claiming an empty one.
SEQUOIA_CAL_KEEP_PAST = 86400

# Enough for a season at this cadence, and a hard stop if the feed ever grows a
# recurrence expander and starts returning a thousand.
SEQUOIA_CAL_MAX = 40

# Longer than any title observed (the longest was 28 characters once its
# parenthetical was removed) and short enough that a pathological one cannot
# blow the record up. The panel draws the nearest title at double size and 28
# characters is 223 of its 320 columns, so this cap is also the panel's.
SEQUOIA_CAL_TITLE_MAX = 44

# An all-day entry has no useful clock time. It is given this much length so it
# is a visible block rather than a zero-height nothing, and the record flags it
# so the panel can draw it as the different kind of thing it is.
SEQUOIA_CAL_ALLDAY_S = 43200

def _sequoia_cal_epoch(s):
    """A feed timestamp as epoch seconds, taking the wall clock and not the offset.

    See the long note above for why the trailing `-08:00` is discarded rather
    than honoured. zoneinfo first so that a fetcher running anywhere resolves
    this the way the makerspace experiences it, with `mktime` behind it for a
    Python that has no tzdata -- which on the Pi this ships to is set to Pacific
    anyway, so the fallback agrees.

    Parsed by slicing rather than by regex, and not only because `re` is
    imported further down this file than this section sits: the slice *is* the
    documentation of which eight characters of `2026-08-13T19:00:00-08:00` are
    believed and which nine are thrown away. Anything that does not have digits
    where ISO 8601 puts them is None, which the caller treats as an unusable
    entry rather than as a broken feed.
    """
    s = str(s or "")
    if len(s) < 16 or s[4] != "-" or s[7] != "-" or s[13] != ":":
        return None
    try:
        year, mon, day = int(s[0:4]), int(s[5:7]), int(s[8:10])
        hour, minute = int(s[11:13]), int(s[14:16])
        sec = int(s[17:19]) if len(s) >= 19 and s[16] == ":" else 0
    except ValueError:
        return None
    try:
        import datetime
        from zoneinfo import ZoneInfo
        return float(datetime.datetime(
            year, mon, day, hour, minute, sec,
            tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())
    except Exception:                                        # noqa: BLE001
        # tm_isdst=-1: let the C library decide, which is right on any machine
        # whose zone is the makerspace's and no worse than guessing anywhere
        # else.
        return float(time.mktime((year, mon, day, hour, minute, sec, 0, 0, -1)))


def _sequoia_cal_title(s):
    """The title as the wall should carry it. Editorial, not typographic.

    Case is folded up here rather than in the demo because upper case is what
    the record is *for*: every panel that draws it draws capitals, and storing
    both forms would be storing a string nobody reads.
    """
    s = str(s or "")
    # Parentheticals, dropped by scan rather than by regex; see the note in
    # _sequoia_cal_epoch. An unclosed "(" takes the rest of the title with it,
    # which is the right answer: whatever follows it was never going to be
    # readable prose.
    while "(" in s:
        head, _, rest = s.partition("(")
        s = head + rest.partition(")")[2]
    s = " ".join(s.replace("&", "/").split()).upper()
    if len(s) <= SEQUOIA_CAL_TITLE_MAX:
        return s
    cut = s[:SEQUOIA_CAL_TITLE_MAX]
    # On a word boundary if there is one in the last third, so the cap reads as
    # a shorter name rather than as a truncation.
    sp = cut.rfind(" ")
    if sp >= SEQUOIA_CAL_TITLE_MAX * 2 // 3:
        cut = cut[:sp]
    return cut.rstrip()


@product("sequoia-calendar", ttl=SEQUOIA_CAL_TTL,
         interval=SEQUOIA_CAL_INTERVAL,
         description="upcoming classes and socials at the makerspace")
def _sequoia_calendar():
    """The makerspace's own public calendar, trimmed to what a panel draws.

    An empty array is returned as an empty record rather than raised as an
    error. A quiet month is a real state of a small workshop's calendar and the
    panel draws it as one; raising here would instead leave last week's record
    in place -- see `fetch()` -- and the wall would keep advertising a social
    that already happened, which is the one genuinely bad outcome available.
    """
    raw = get_json(SEQUOIA_CAL_URL, timeout=20)
    if not isinstance(raw, list):
        raise ValueError("sequoia calendar is not a JSON array")

    now = time.time()
    events, skipped = [], 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        start = _sequoia_cal_epoch(item.get("start"))
        if start is None:
            skipped += 1
            continue
        all_day = bool(item.get("allDay"))
        end = _sequoia_cal_epoch(item.get("end"))
        if end is None or end <= start:
            end = start + (SEQUOIA_CAL_ALLDAY_S if all_day else 3600.0)
        title = _sequoia_cal_title(item.get("title"))
        if not title:
            skipped += 1
            continue
        if end < now - SEQUOIA_CAL_KEEP_PAST:
            continue
        events.append({
            "t": int(round(start)),
            # A duration rather than an end: it is three or four digits instead
            # of ten, and it is what the panel measures a block's height with.
            "d": int(round(min(end - start, 86400.0))),
            "a": 1 if all_day else 0,
            "n": title,
        })

    events.sort(key=lambda e: (e["t"], e["n"]))
    # Bookwhen can list the same session twice around a reschedule.
    uniq, seen = [], set()
    for e in events:
        key = (e["t"], e["n"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)

    return {
        "site": ftsite.NAME.upper(),
        "n": len(uniq[:SEQUOIA_CAL_MAX]),
        "n_feed": len(raw),
        "skipped": skipped,
        "tz": "America/Los_Angeles",
        "tz_note": "feed stamps a fixed -08:00 year round; wall-clock taken as local",
        "ev": uniq[:SEQUOIA_CAL_MAX],
    }, SEQUOIA_CAL_URL


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
#   api.airplanes.live/v2/point/...  worked; 65 aircraft, 250 ms.
#
# airplanes.live was what shipped until 2026-08-12, when it began answering
# every endpoint with `403 {"error": "please contact us at
# contact@airplanes.live"}` -- the same 403 for the project User-Agent, a bare
# curl one and a browser one, while airplanes.live itself still served 200 from
# the same host. Their guide documents one request a second and this asked once
# a minute, sixty times under, so this was not the rate. It reads as a block on
# the wall's address, and an address is not something a fetcher can argue with.
#
# So adsb.fi is what ships, which is what the second source was written to be.
# The response shapes differ in two places and no more: adsb.fi calls the list
# `aircraft` where airplanes.live called it `ac`, which the parser below has
# always accepted both of, and adsb.fi reports `now` in seconds where readsb
# reports milliseconds, which the timestamp line now accepts both of. Neither
# wants a key. Both ask for civility rather than credentials.
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

WIKI_STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"

# Seconds of firehose per fetch. Long enough that a burst is inside the window
# and short enough that the panel is not a two-minute loop of the same titles.
WIKI_WINDOW = float(os.environ.get("FT_WIKI_WINDOW", "40"))

# Backstops, so a stream that suddenly runs hot cannot fill memory or the cache.
# Neither has ever fired; both are cheaper than finding out.
WIKI_MAX_BYTES = 24 << 20
WIKI_MAX_EVENTS = 1400

# Title reservoir. The demo lays these out along a scrolling strip and fits
# fifteen or so; sixty candidates is enough slack for it to choose ones that do
# not collide, and 44 characters is a wide panel's worth of 3x5 type.
WIKI_MAX_TITLES = 60
WIKI_TITLE_CHARS = 44

# Two hours to live, a quarter of an hour between fetches. Wikipedia at four in
# the afternoon looks like Wikipedia at five -- the rate and the bot share barely
# move -- so a stale record is still a true picture of the encyclopedia and the
# TTL is generous. What goes stale is the *titles*, which are the delight, and
# that is what the interval is for.
WIKI_TTL = 7200
WIKI_INTERVAL = 900

# Wikimedia asks for a User-Agent that identifies the client and reaches a
# human; their policy is explicit about it and they will block a generic one.
WIKI_UA = ("flaschen-taschen-wiki/1 (+https://github.com/hzeller/flaschen-taschen; %s)"
           % os.environ.get("FT_CONTACT", "jof@thejof.com"))

# Exactly the glyphs wiki.py can draw. Kept here rather than imported from the
# demo because the fetcher must not import a demo module, and duplicated with
# that stated: if the demo's font grows, this string grows with it.
WIKI_CHARSET = frozenset(" -.,:;/'\"!?()&+*=%_0123456789"
                         "ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# The Latin letters that NFD will not take apart, because their diacritic is
# welded on rather than combining. Without these, half of Scandinavian, Polish,
# Icelandic and German goes in the bin for the sake of one letter.
WIKI_FOLD = {
    u"ß": "ss", u"æ": "ae", u"Æ": "AE", u"ø": "o",
    u"Ø": "O", u"đ": "d", u"Đ": "D", u"ł": "l",
    u"Ł": "L", u"þ": "th", u"Þ": "TH", u"ð": "d",
    u"Ð": "D", u"œ": "oe", u"Œ": "OE", u"å": "a",
    u"Å": "A", u"–": "-", u"—": "-", u"‘": "'",
    u"’": "'", u"“": '"', u"”": '"', u" ": " ",
}


def _wiki_latin(title):
    """A title in wiki.py's font, or None if it cannot be spelled in it.

    Fold first, reject second. "Zaragoza" and "Malmo" and "Strasse" all survive
    a NFD-and-drop-the-marks pass; a Cyrillic or CJK title survives nothing and
    is meant not to, because the alternative is a row of empty boxes claiming to
    be an article name.
    """
    import unicodedata
    s = u"".join(WIKI_FOLD.get(ch, ch) for ch in title)
    s = unicodedata.normalize("NFD", s)
    s = u"".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("_", " ").strip().upper()
    if not s or not all(ch in WIKI_CHARSET for ch in s):
        return None
    if len(s) > WIKI_TITLE_CHARS:
        # Say it was cut rather than stopping mid-word, which reads as though
        # the article really is called "ST. MICHAEL'S ABBEY (ORANGE COUNTY".
        s = s[:WIKI_TITLE_CHARS - 3].rstrip() + "..."
    return s


def _wiki_dt(meta):
    """`meta.dt` as an epoch float. Milliseconds, which is the point.

    `timestamp` on the message is whole seconds, and whole seconds cannot show
    that eleven of the last fifteen edits arrived inside 200 ms of each other --
    which is the burstiness the panel is drawing. This parses the ISO string by
    hand rather than reaching for a dependency: the format is fixed by the
    schema and it is always UTC with a Z on the end.
    """
    import calendar
    s = str((meta or {}).get("dt") or "")
    if len(s) < 20 or s[-1] != "Z":
        return None
    try:
        base = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        frac = float(s[19:-1]) if len(s) > 20 else 0.0
        return base + frac
    except (ValueError, OverflowError):
        return None


@product("wiki-stream", ttl=WIKI_TTL, interval=WIKI_INTERVAL,
         description="a %ds window of the Wikimedia recentchange firehose"
                     % int(WIKI_WINDOW))
def _wiki_stream():
    """Listen to the firehose for a bounded window, aggregate, and hang up.

    The SSE framing is done by hand -- `data:` lines accumulated until a blank
    line closes the event -- because the whole of what a client library would
    add here is reconnection, and reconnecting is precisely what this must not
    do. One connection, one window, then close.

    The window is bounded three ways and each bound has a different failure in
    mind: elapsed time is the normal exit, a byte count catches a stream that
    starts shouting, and an event count catches the same thing one layer up. The
    socket also has its own read timeout, because a firehose that has gone quiet
    looks exactly like a firehose that has gone away.
    """
    import urllib.request

    req = urllib.request.Request(
        WIKI_STREAM_URL,
        headers={"User-Agent": WIKI_UA, "Accept": "text/event-stream"})

    t_start = time.time()
    n_all = n_kept = n_bot = n_new = 0
    n_titles_seen = 0
    add_bytes = del_bytes = 0
    per_wiki = {}
    events = []                       # (t, delta, wiki, bot, new)
    titles = []                       # (t, wiki, delta, latin)
    t_first = t_last = None
    read = 0
    data = []

    def take(doc):
        """One recentchange message. Nothing about `user` is read, ever."""
        nonlocal n_all, n_kept, n_bot, n_new, n_titles_seen
        nonlocal add_bytes, del_bytes, t_first, t_last
        n_all += 1
        kind = doc.get("type")
        if kind not in ("edit", "new"):
            return                    # categorize is bookkeeping; log is people
        length = doc.get("length")
        if not isinstance(length, dict):
            return
        new_len = length.get("new")
        if not isinstance(new_len, (int, float)) or isinstance(new_len, bool):
            return
        old_len = length.get("old")
        if not isinstance(old_len, (int, float)) or isinstance(old_len, bool):
            old_len = 0               # a new page has no old length, by design
        delta = int(max(-999999, min(999999, int(new_len) - int(old_len))))

        when = _wiki_dt(doc.get("meta")) or time.time()
        if t_first is None:
            t_first = when
        t_last = when

        wiki = str(doc.get("wiki") or doc.get("server_name") or "?")[:32]
        is_bot = bool(doc.get("bot"))
        is_new = kind == "new"
        per_wiki[wiki] = per_wiki.get(wiki, 0) + 1
        n_kept += 1
        n_bot += 1 if is_bot else 0
        n_new += 1 if is_new else 0
        if delta >= 0:
            add_bytes += delta
        else:
            del_bytes -= delta
        if len(events) < WIKI_MAX_EVENTS:
            events.append((when, delta, wiki, is_bot, is_new))

        # Titles: main namespace only, which is both the privacy rule and the
        # quality one. Wikidata's Q-numbers and Wiktionary's single words are
        # main-space too but are not what anybody means by an article title, so
        # the bare-identifier shapes go as well.
        if doc.get("namespace") != 0:
            return
        raw = str(doc.get("title") or "")
        if not raw or (raw[0] in "QPL" and raw[1:].isdigit()):
            return
        n_titles_seen += 1
        latin = _wiki_latin(raw)
        if latin and len(latin) >= 3:
            titles.append((when, wiki, delta, latin))

    with urllib.request.urlopen(req, timeout=25) as resp:
        for raw in resp:
            read += len(raw)
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line:
                continue              # event:, id:, and the :ok keepalive
            if data:
                try:
                    take(json.loads("".join(data)))
                except (ValueError, TypeError, KeyError):
                    pass
                data = []
            if (time.time() - t_start >= WIKI_WINDOW
                    or read >= WIKI_MAX_BYTES or n_kept >= WIKI_MAX_EVENTS):
                break

    if n_kept < 8 or t_first is None or t_last is None or t_last <= t_first:
        raise ValueError("only %d usable events in %.0fs of stream"
                         % (n_kept, time.time() - t_start))

    secs = t_last - t_first
    # Project index. The wikis are ordered by how much they contributed, so the
    # low indices are the ones the legend names and the demo can slice off the
    # front of the list without sorting it again.
    order = sorted(per_wiki, key=lambda k: (-per_wiki[k], k))
    idx = {name: i for i, name in enumerate(order)}

    # Titles thinned to a reservoir spread across the window rather than the
    # first sixty, which would be the first eight seconds of it.
    if len(titles) > WIKI_MAX_TITLES:
        step = len(titles) / float(WIKI_MAX_TITLES)
        titles = [titles[int(i * step)] for i in range(WIKI_MAX_TITLES)]

    payload = {
        "secs": round(secs, 2),
        "n": n_kept, "n_all": n_all,
        "per_s": round(n_kept / secs, 2),
        "all_per_s": round(n_all / max(1e-6, time.time() - t_start), 2),
        "bot_pct": round(100.0 * n_bot / n_kept, 1),
        "n_new": n_new,
        "add_bytes": add_bytes, "del_bytes": del_bytes,
        "n_projects": len(per_wiki),
        "projects": [[name, per_wiki[name]] for name in order[:12]],
        "pnames": order,
        # Columnar, relative, and integer everywhere an integer will say it.
        "ms": [int(round((e[0] - t_first) * 1000.0)) for e in events],
        "d": [e[1] for e in events],
        "pi": [idx[e[2]] for e in events],
        "f": [(1 if e[3] else 0) | (2 if e[4] else 0) for e in events],
        "titles": [[int(round((e[0] - t_first) * 1000.0)), idx[e[1]], e[2],
                    e[3]] for e in titles],
        "n_titles_seen": n_titles_seen,
        "title_note": "namespace 0, Latin-script only",
        "bytes_read": read,
        "source": "Wikimedia EventStreams (mediawiki.recentchange)",
    }
    return payload, WIKI_STREAM_URL


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

ADSB_URL = "https://opendata.adsb.fi/api/v2/lat/%.4f/lon/%.4f/dist/%d"

# The wall's own address, in Dogpatch. Everything on the panel is measured from
# here; it lives in demos/site.json now, which is the one place to change it.
ADSB_LAT, ADSB_LON = ftsite.LAT, ftsite.LON

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
                     "adsb.fi" % ADSB_RADIUS_NM)
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

    # readsb reports `now` in milliseconds since the epoch and adsb.fi reports
    # it in seconds, and the magnitude is what tells them apart: a seconds
    # count does not reach 1e11 until the year 5138, so anything past it is
    # milliseconds and anything past 1e9 is a plausible seconds count. Falling
    # back to the local clock rather than failing on either shape: the
    # positions are still good, and a demo that dead-reckons from a clock a
    # second out is not measurably wrong.
    served = _adsb_num(doc.get("now")) or 0.0
    if served > 1e11:
        t = served / 1000.0
    elif served > 1e9:
        t = served
    else:
        t = time.time()

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
        "source": "adsb.fi",
    }
    payload.update({c: [r[c] for r in kept] for c in cols})
    return payload, url


# --------------------------------------------------------------------------
# The 19, 22 and 55 at our own front door, as a timetable. muni.py draws these.
#
# **This is the schedule, not a prediction, and that is not a design choice.**
# Muni has no real-time feed this wall may honestly use. 511.org's SIRI and
# GTFS-RT endpoints are the documented public source of Muni vehicle positions
# and both answer 401 without a free-but-registered key -- a key nobody had
# when this was written. The legacy NextBus feed at retro.umoiq.com is still
# keyless but its agency list is down to eighteen agencies and SF Muni is not
# among them; gtfs.sfmta.com does not answer at all; api.sfmta.com does not
# resolve; sfmta.com's own /status/gtfs-rt/*.pb are 404 Drupal pages; and
# Swiftly's api.goswift.ly wants credentials. The BART block further down this
# file reaches the same conclusion from the other end -- it exists because BART
# publishes GTFS-RT for free and Muni does not.
#
# There is one more door, and it is deliberately left shut. SFMTA's own route
# pages embed a live Umo IQ vehicle API -- `webservices.umoiq.com/api/pub/v1`,
# under the *unlisted* agency id `sfmta-cis` -- and it does answer 200 with a
# thousand buses and their GPS times. It answers because the page ships a
# `key=` query parameter in the clear, and that key is SFMTA's: issued to
# SFMTA, metered against SFMTA, and rotatable by either party the moment
# somebody notices a light display in Potrero Hill polling it. Lifting a
# credential out of somebody else's front end is not the same thing as a
# keyless feed, and a panel that dies the week the token rotates is not one
# anybody wants to own. The clean route to live Muni predictions is a 511.org
# token -- free, a minute to request, rate-limited to sixty calls an hour. If
# one ever lands in this tree, this product grows a sibling and `muni.py`
# grows a live mode. Until then the panel shows what the timetable says, it
# says "SCHEDULE" on the wall in as many words, and nobody is invited to
# believe a drawn bus is a tracked one.
#
# **Where the schedule comes from.** San Francisco's open data portal carries
# SFMTA's static GTFS as a Socrata blob asset, dataset `dni7-qpv3`, and it is
# genuinely keyless: 10.4 MB of zip, HTTP 200, no token, no click-through
# despite the licence text inside the archive. It is the same file SFMTA hands
# Google. The portal's metadata endpoint is 2 KB and carries `blobFilename`,
# which is the published schedule's own name -- `SFMTA_GTFS_20260723_20260828v5
# .zip`, service period baked into it. That name is used as a version: the
# daily fetch pulls the metadata, and only downloads the archive when the name
# has changed. On the days it has not -- which is most days, since SFMTA signs
# up a new schedule roughly quarterly -- the fetch costs two kilobytes and the
# previous payload is re-stored under a fresh timestamp. That is honest: the
# payload really is still the currently published schedule, and the record's
# age really is how long ago we last confirmed it.
#
# **The reduction is severe and it has to be.** `stop_times.txt` alone is 97 MB
# uncompressed and about three million rows -- a hundred times the whole rest
# of this cache put together. What the panel needs out of it is six stops: the
# nearest stop for each of routes 19, 22 and 55 in each of its two directions,
# and the departure times at those six. That is about 1900 integers. So the
# archive is never unpacked to disk and `stop_times.txt` is streamed through a
# csv reader straight out of the zip, keeping only rows whose stop_id is one of
# the few dozen within 800 m of the installation. The pass takes half a minute
# and happens on the days the schedule changes, which is a handful a year.
#
# **The stops are found, not hardcoded.** The makerspace wiki names the 19, 22
# and 55 as its buses and those three route ids are hardcoded here, with the
# wiki as the citation -- but *which* stop each one uses is derived, by taking
# every stop within 800 m, asking `stop_times` which route and direction
# actually serves it, and keeping the nearest per (route, direction). Today
# that resolves to De Haro & 18th and Rhode Island & 18th for the 19, both
# Connecticut & 18th platforms for the 55, and the two 16th & Wisconsin
# platforms for the 22 -- 140 m, 187 m and 413 m away respectively, which is
# the whole point of the panel, because that spread is a spread in walking
# time. A stop moving in a future sign-up fixes itself.
#
# **Calendars, because GTFS service ids are a trap here.** SFMTA's
# `calendar.txt` declares the obvious weekday/Saturday/Sunday triple, and then
# `calendar_dates.txt` removes service 1 on every single weekday of the period
# and adds `M11` or `M21` in its place. Nothing in `trips.txt` references
# service 1 at all. A fetcher that read only calendar.txt would produce a
# perfectly well-formed record with no weekday service in it. So the calendar
# is resolved here, date by date across the feed's whole period, into a plain
# map of "20260812" -> the service ids running that day, and the demo does no
# GTFS reasoning whatsoever -- it looks today up in a dict.
#
# Times are kept as minutes after midnight and are allowed past 1440, which is
# how GTFS spells "this is still Tuesday's service": the 22 runs to 30:09, and
# a naive modulo would file its 06:09 owl trips under the wrong day.
# --------------------------------------------------------------------------

MUNI_PRODUCT = "muni-18th"

# Three days, because that is how long a static schedule stays true enough to
# draw -- and a fetcher that has been down that long is a bigger problem than
# a bus time being from Monday. The panel prints the age either way.
MUNI_TTL = 259200
MUNI_INTERVAL = 86400

# Socrata: the metadata view (cheap, gives the version) and the blob itself.
MUNI_DATASET = "dni7-qpv3"
MUNI_META_URL = "https://data.sfgov.org/api/views/%s.json" % MUNI_DATASET
MUNI_ZIP_URL = ("https://data.sfgov.org/download/%s/application/x-zip-compressed"
                % MUNI_DATASET)

# The routes the space's own wiki names as serving 1736 18th St. Route *ids*,
# which for SFMTA happen to equal the numbers on the front of the bus.
MUNI_ROUTES = ("19", "22", "55")

# How far to look for a stop. 800 m is comfortably past the 16th St pair the 22
# uses at 413 m and comfortably short of the Potrero Ave stops at 671 m, which
# are a different corridor and not this panel's business.
MUNI_SEARCH_M = 800.0

# Walking. 1.25 m/s is an unhurried adult; 1.30 is the detour factor for the
# fact that Potrero Hill is a street grid and not a field, and that the 22's
# stop is two blocks over and one block down rather than 413 m of straight
# line. Both are here rather than in the demo because the demo must not be in
# the business of deciding how fast you walk -- it draws what it is given.
MUNI_WALK_MPS = 1.25
MUNI_WALK_DETOUR = 1.30

# Refuse a zip that has grown implausible rather than unpacking it on a Pi.
MUNI_MAX_ZIP = 64 << 20

# Payload shape version. Bumping it makes the version-skip below re-download
# rather than re-store a payload the demo can no longer read.
MUNI_SCHEMA = 1


# Headsigns that will not survive being trimmed by rule. 'UCSF Mission Bay'
# reduced mechanically lands on 'UCSF MSNBAY', which is eleven characters of
# nothing anybody says out loud; the 22 goes to UCSF and that is what it should
# read. Everything not named here falls through to the generic trimming, so a
# sign-up that renames a terminal degrades to something ugly but truthful
# rather than to a KeyError.
MUNI_TERMINALS = {
    "UCSF MISSION BAY": "UCSF",
    "BEACH STREET": "BEACH",
    "BAY STREET": "BAY",
    "SUTTER FILLMORE": "SUTTER",
}


def _muni_short(name):
    """A headsign trimmed to the eight characters the panel's gutter holds.

    'Beach Street' -> 'BEACH', '16th St & Mission' -> '16TH/MSN'. Purely
    cosmetic, and done here rather than in the demo so that the demo is handed
    a label instead of a parser.
    """
    s = " ".join(str(name or "").upper().split())
    if s in MUNI_TERMINALS:
        return MUNI_TERMINALS[s]
    for long, short in ((" STREET", ""), (" ST", ""), (" AVENUE", ""),
                        (" AVE", ""), ("MISSION", "MSN")):
        s = s.replace(long, short)
    s = s.replace(" & ", "/").replace("&", "/")
    return " ".join(s.split())[:8] or "?"


def _muni_metres(lat, lon, lat0, lon0):
    """Flat-earth metres between two nearby points. Good to centimetres here."""
    import math
    return math.hypot((lat - lat0) * 111320.0,
                      (lon - lon0) * 111320.0 * math.cos(math.radians(lat0)))


def _muni_hhmmss(s):
    """'25:09:00' -> 1509 minutes after midnight. None if unparseable."""
    try:
        h, m, _sec = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _muni_rows(zf, member):
    """Stream one GTFS table out of the zip as dicts, never touching disk."""
    import csv
    import io
    with zf.open(member) as fh:
        for row in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")):
            yield row


def _muni_calendar(zf, routes_services):
    """Resolve calendar.txt + calendar_dates.txt into date -> [service_id].

    Only service ids that some trip we care about actually references are kept,
    which is what stops the map carrying the phantom service 1 that
    calendar.txt declares and calendar_dates.txt then removes from every day of
    the period.
    """
    weekly = []
    lo = hi = None
    for row in _muni_rows(zf, "calendar.txt"):
        sid = row.get("service_id")
        start, end = row.get("start_date"), row.get("end_date")
        if not (sid and start and end):
            continue
        days = [row.get(d) == "1" for d in
                ("monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday")]
        weekly.append((sid, days, start, end))
        lo = start if lo is None or start < lo else lo
        hi = end if hi is None or end > hi else hi

    exceptions = {}
    try:
        for row in _muni_rows(zf, "calendar_dates.txt"):
            sid, date, kind = (row.get("service_id"), row.get("date"),
                               row.get("exception_type"))
            if not (sid and date and kind):
                continue
            exceptions.setdefault(date, []).append((sid, kind))
            lo = date if lo is None or date < lo else lo
            hi = date if hi is None or date > hi else hi
    except KeyError:
        pass                            # calendar_dates.txt is optional in GTFS

    if lo is None or hi is None:
        raise ValueError("GTFS has neither calendar.txt nor calendar_dates.txt")

    out = {}
    day = time.mktime(time.strptime(lo + " 12:00:00", "%Y%m%d %H:%M:%S"))
    last = time.mktime(time.strptime(hi + " 12:00:00", "%Y%m%d %H:%M:%S"))
    # Noon steps so a DST boundary cannot skip or repeat a date.
    while day <= last + 1.0:
        stamp = time.localtime(day)
        date = time.strftime("%Y%m%d", stamp)
        wd = int(time.strftime("%w", stamp))        # 0 = Sunday
        wd = 6 if wd == 0 else wd - 1               # 0 = Monday, as GTFS orders
        active = set()
        for sid, days, start, end in weekly:
            if start <= date <= end and days[wd]:
                active.add(sid)
        for sid, kind in exceptions.get(date, ()):
            if kind == "1":
                active.add(sid)
            elif kind == "2":
                active.discard(sid)
        active &= routes_services
        if active:
            out[date] = sorted(active)
        day += 86400.0
    return out, lo, hi


@product(MUNI_PRODUCT, ttl=MUNI_TTL, interval=MUNI_INTERVAL,
         description="SFMTA timetable for the 19/22/55 stops nearest the "
                     "installation, from the keyless GTFS on data.sfgov.org")
def _muni_18th():
    """The six nearest 19/22/55 stops and every departure the timetable gives.

    Two requests at most: the Socrata metadata, and -- only when the published
    schedule's filename has changed since last time -- the 10.4 MB archive.
    """
    import io
    import zipfile

    meta = get_json(MUNI_META_URL, timeout=30)
    version = str(meta.get("blobFilename") or "").strip()
    if not version:
        raise ValueError("%s published no blobFilename to version by"
                         % MUNI_DATASET)

    # The cheap path: same published schedule as last time, so re-store what we
    # already parsed rather than pulling ten megabytes to reach the same answer.
    got = load(MUNI_PRODUCT)
    if got is not None:
        old = got[0]
        if (isinstance(old, dict) and old.get("feed_version") == version
                and old.get("schema") == MUNI_SCHEMA
                and old.get("stops")):
            payload = dict(old)
            payload["revalidated"] = True
            return payload, MUNI_META_URL

    raw = get(MUNI_ZIP_URL, timeout=180)
    if len(raw) > MUNI_MAX_ZIP:
        raise RuntimeError("SFMTA GTFS is %d bytes, over the %d cap"
                           % (len(raw), MUNI_MAX_ZIP))
    zf = zipfile.ZipFile(io.BytesIO(raw))

    lat0, lon0 = ftsite.LAT, ftsite.LON

    # Every stop within walking-ish distance, by id. A few dozen of the 3500.
    near = {}
    for row in _muni_rows(zf, "stops.txt"):
        try:
            lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        metres = _muni_metres(lat, lon, lat0, lon0)
        if metres <= MUNI_SEARCH_M:
            near[row["stop_id"]] = {
                "stop_id": row["stop_id"],
                "code": (row.get("stop_code") or "").strip(),
                "name": " ".join((row.get("stop_name") or "").split()),
                "lat": round(lat, 6), "lon": round(lon, 6),
                "metres": int(round(metres)),
            }
    if not near:
        raise ValueError("no GTFS stop within %g m of the site" % MUNI_SEARCH_M)

    # trip_id -> (route, direction, headsign, service) for the three routes.
    wanted = {}
    for row in _muni_rows(zf, "trips.txt"):
        if row.get("route_id") in MUNI_ROUTES:
            wanted[row["trip_id"]] = (row["route_id"],
                                      str(row.get("direction_id") or "0"),
                                      row.get("trip_headsign") or "",
                                      row.get("service_id") or "")
    if not wanted:
        raise ValueError("GTFS has no trips for routes %s"
                         % ", ".join(MUNI_ROUTES))

    # The one expensive pass. Three million rows in, a few thousand out.
    # (stop_id, route, direction) -> {service_id: [minute, ...]}, plus a
    # headsign tally so the gutter label is the usual destination rather than
    # whichever variant happened to come first.
    times = {}
    signs = {}
    for row in _muni_rows(zf, "stop_times.txt"):
        stop_id = row.get("stop_id")
        if stop_id not in near:
            continue
        trip = wanted.get(row.get("trip_id"))
        if trip is None:
            continue
        route, direction, headsign, service = trip
        minute = _muni_hhmmss(row.get("departure_time")
                              or row.get("arrival_time"))
        if minute is None:
            continue
        key = (stop_id, route, direction)
        times.setdefault(key, {}).setdefault(service, []).append(minute)
        tally = signs.setdefault(key, {})
        tally[headsign] = tally.get(headsign, 0) + 1

    if not times:
        raise ValueError("none of the %d nearby stops is served by %s"
                         % (len(near), ", ".join(MUNI_ROUTES)))

    # Nearest stop per (route, direction). Ties cannot happen -- two stops at
    # exactly the same distance would be the same stop -- but sorting on the
    # id as well keeps a re-fetch from ever reordering the panel.
    best = {}
    for key in times:
        stop_id, route, direction = key
        metres = near[stop_id]["metres"]
        rank = (metres, stop_id)
        if (route, direction) not in best or rank < best[(route, direction)][0]:
            best[(route, direction)] = (rank, key)

    stops = []
    services_used = set()
    for route in MUNI_ROUTES:
        for direction in ("1", "0"):        # inbound first: it reads as "up"
            picked = best.get((route, direction))
            if picked is None:
                continue
            key = picked[1]
            stop = dict(near[key[0]])
            walk_s = (stop["metres"] * MUNI_WALK_DETOUR) / MUNI_WALK_MPS
            tally = signs.get(key, {})
            headsign = max(tally, key=lambda h: (tally[h], h)) if tally else ""
            by_service = {}
            for service, mins in times[key].items():
                by_service[service] = sorted(set(mins))
                services_used.add(service)
            stop.update({
                "route": route,
                "dir": int(direction),
                "headsign": " ".join(headsign.split()),
                "label": _muni_short(headsign),
                "walk_s": int(round(walk_s)),
                "times": by_service,
            })
            stops.append(stop)

    if not stops:
        raise ValueError("resolved no (route, direction) stops at all")

    services, feed_lo, feed_hi = _muni_calendar(zf, services_used)
    if not services:
        raise ValueError("calendar resolved to no service days for %s"
                         % ", ".join(sorted(services_used)))

    return {
        "schema": MUNI_SCHEMA,
        "feed_version": version,
        "feed_start": feed_lo,
        "feed_end": feed_hi,
        "site": [round(lat0, 6), round(lon0, 6)],
        "tz": "America/Los_Angeles",
        "routes": list(MUNI_ROUTES),
        "walk_mps": MUNI_WALK_MPS,
        "walk_detour": MUNI_WALK_DETOUR,
        "search_m": MUNI_SEARCH_M,
        "n_near": len(near),
        "services": services,
        "stops": stops,
        "source": "SFMTA GTFS, data.sfgov.org/%s" % MUNI_DATASET,
    }, MUNI_ZIP_URL


# --------------------------------------------------------------------------
# Live Muni arrivals at those same six stops. muni.py draws these over the
# timetable above.
#
# **The key.** 511.org is the Bay Area's transit clearing house and the only
# public source of Muni vehicle predictions. It needs a token, free, from
# https://511.org/open-data/token -- an email address and about a minute. This
# fetcher reads it from the environment as `FT_511_KEY`, and there is no
# default and no fallback: an unset key is not an error, it is simply this
# product not existing, and `muni.py` falls back to the timetable product above
# and says on the wall that it has done so. That is deliberate. The token
# belongs to the installation, not to the source tree, and a checkout that has
# never heard of it must still draw a real panel. It is never written into a
# record either -- only into the query string of a request.
#
# **The rate limit is the whole design, and it is not ours alone to spend.**
# The response headers say `RateLimit-Limit: 60`, and watching
# `RateLimit-Remaining` recover shows a rolling hour: sixty in hand, about one
# back per minute. StopMonitoring filters by exactly one `stopCode` -- a
# comma-separated pair was tried and returns zero visits rather than two stops
# -- so six stops is six requests a pass, and the arithmetic is fixed:
#
#     requests/hour = stops * 3600 / (interval * 0.9)
#
# The 0.9 is not decoration. `is_due()` deliberately fires a hair early so a
# product does not miss every other timer tick, so the *worst case* cadence is
# nine tenths of the interval and that is what a budget has to be built on.
#
# The wall does not start with sixty. It has a pre-existing cron job
# (`/home/pi/muni.sh`, every five minutes from 10:00 to 22:00) polling the same
# API for three stops, which is **36 requests an hour** through the whole
# operating day and leaves roughly **24** for this. Six stops at 1080 s comes
# to 22.2 an hour, which fits under that with real margin rather than sitting
# on the line. Eighteen minutes is a long time for a bus prediction and it is
# the honest consequence of sharing a sixty-an-hour key with an existing job.
#
# **The single knob, for whoever integrates this.** That cron job is polling
# three of these six stops to print text in a terminal, and this product
# supersedes it. Retiring it returns 36 requests an hour, at which point
# `MUNI_LIVE_INTERVAL` drops to 360 and the panel refreshes every six minutes
# -- 40 an hour, still inside sixty on its own. Nothing else has to change.
#
# **Eighteen minutes is survivable because of what is stored.** What goes in
# the record is **absolute arrival timestamps, not countdowns**. "17:34:54" is
# still 17:34:54 a quarter of an hour later; the panel subtracts the wall clock
# every frame and counts down smoothly between fetches. What ages is the
# *revision* -- a bus that got stuck after the last fetch is still drawn on its
# old promise -- and the age on screen is what that is for. The far end of the
# panel's horizon is barely affected, because a bus twenty minutes out gets
# refreshed before it is the one you are running for.
#
# **429 is expected, not exceptional.** Sharing a key means eventually losing a
# race for it. On a 429 the pass stops dead -- the remaining stops are not
# asked, because hammering a rate limiter is how a soft limit becomes a hard
# one -- `Retry-After` is recorded, and whatever stops did answer are stored.
# If none did, the fetch fails and the previous record stays put with an honest
# age on it, which is exactly what `fetch()` is designed to do.
#
# **Unfiltered, this endpoint is a trap.** `StopMonitoring?agency=SF` with no
# stopCode returns every monitored visit in the system: 35,573 of them, 2.6 MB
# gzipped and 32 MB decoded, sixty times this entire cache, onto a Raspberry Pi
# over shop wifi. Filtered to one stop it is 630 bytes on the wire. The filter
# is not an optimisation, it is the difference between a product and an outage,
# which is why an oversized body is treated as a failed filter and refused.
#
# **Two things about the response that each cost an afternoon if you meet them
# cold.** It is gzipped whether or not you asked -- `urlopen` does not
# transparently decode -- and it carries a UTF-8 byte order mark, so
# `json.loads` on the decompressed bytes dies on byte one and `utf-8-sig` is
# required. Neither is documented.
#
# **What this gets that no other panel on the wall has.** Every visit carries
# both `AimedArrivalTime` -- what the timetable promised -- and
# `ExpectedArrivalTime`, what is actually going to happen. The difference is
# lateness, signed, per bus, and because muni.py's one axis is minutes-to-
# arrival, that difference is a *distance on screen*: the ghost of where the
# bus should have been sits behind it, or ahead of it when it is running early,
# which on the 19 it frequently is. And `Monitored` is false when 511 is
# quoting the timetable back rather than watching a vehicle; the panel draws
# that differently, because a panel that cannot tell you which of the two it is
# showing is not worth the wall.
#
# `VehicleRef`, `VehicleLocation`, `Bearing` and `Occupancy` are all in the
# response and none of them are stored. The panel's axis is time, not space, so
# a coordinate is dead weight in a record; and the standing rule since `bikes`
# is that an identifier which is not drawn does not get cached.
# --------------------------------------------------------------------------

MUNI_LIVE_PRODUCT = "muni-live"

# Half an hour before the countdown stops being worth believing, and eighteen
# minutes between fetches. The TTL has to sit comfortably above the interval or
# the panel would spend the tail of every cycle calling itself stale; see the
# rate-limit note above for where the interval comes from. It is a budget, not
# a preference, and the comment above says what frees it.
MUNI_LIVE_TTL = 1800
MUNI_LIVE_INTERVAL = 900

MUNI_511_URL = "https://api.511.org/transit/StopMonitoring"
MUNI_511_AGENCY = "SF"
MUNI_511_KEY_ENV = "FT_511_KEY"

# Never keep more than this per stop; 511 returns a handful and the panel has
# room for fewer.
MUNI_LIVE_MAX_VISITS = 6

# Refuse a response that is not the filtered one. A stopCode that 511 stopped
# recognising would otherwise quietly return the whole 32 MB agency.
MUNI_LIVE_MAX_BYTES = 512 << 10

# The six stops, as `stop_code`s -- what 511 calls `stopCode` and GTFS calls
# `stop_code`, the same numbers, verified against both.
#
# Normally these are read straight out of the `muni-18th` record, where they
# were *derived* from ftsite.LAT/LON: the nearest stop to the installation for
# each of the 19, 22 and 55, **in each direction, per route**. Per route
# matters. Taking the nearest few stops overall instead gives four stops of the
# 19 and 55 within 250 m and never reaches the 22 at all, whose nearest stop is
# 413 m away on 16th St -- a panel that names three routes and shows two. The
# GTFS fetcher's 800 m search radius exists to clear that.
#
# This list is only the fallback for a machine whose GTFS record has not landed
# yet. It is a snapshot of that derivation, run 2026-08-12 against the
# SFMTA_GTFS_20260723_20260828v5 schedule:
#
#   14352  De Haro St & 18th St        19 inbound   (Beach St)      140 m
#   16192  Rhode Island St & 18th St   19 outbound  (Shipyard)      245 m
#   17769  16th Street & Wisconsin St  22 inbound   (Bay St)        423 m
#   17762  16th St & Wisconsin St      22 outbound  (UCSF)          413 m
#   14126  Connecticut St & 18th St    55 inbound   (16th/Mission)  201 m
#   14125  Connecticut St & 18th St    55 outbound  (20th/3rd)      187 m
#
# 16th & Wisconsin serves the 55 as well as the 22, and folding the 55 onto it
# would have saved two requests a pass. It is not done, because the 55's own
# stop is 187 m away and Wisconsin is 413 m: the merge would overstate the 55's
# walk by two hundred metres, and the walk is the one thing this panel exists
# to get right.
#
# Hardcoding the *stops* is fine where hardcoding the *address* is not. These
# are an output of the site, recomputed by the GTFS fetcher every time SFMTA
# signs up a new schedule; a wall that moves gets new ones without an edit here.
MUNI_LIVE_STOPS_FALLBACK = ("14352", "16192", "17769", "17762", "14126",
                            "14125")


class MuniRateLimited(Exception):
    """511 said 429. Carries the Retry-After it asked for, in seconds."""

    def __init__(self, retry_after):
        Exception.__init__(self, "rate limited, retry after %ss" % retry_after)
        self.retry_after = retry_after


def muni_live_key():
    """The 511 token, or None. Never logged, never stored, never defaulted."""
    return (os.environ.get(MUNI_511_KEY_ENV) or "").strip() or None


def _muni_epoch(stamp):
    """'2026-08-12T17:34:54Z' -> epoch seconds. None if absent or malformed."""
    import calendar
    if not stamp:
        return None
    try:
        return float(calendar.timegm(
            time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def _muni_live_stops():
    """The stop codes to poll: out of the GTFS record if it is there.

    Reading them from the sibling product keeps the derivation in one place --
    the GTFS fetcher is the thing that knows where the installation is and
    which stop is nearest -- without making this product fail when that one has
    not run yet.
    """
    got = load(MUNI_PRODUCT)
    if got is not None:
        payload = got[0]
        if isinstance(payload, dict):
            codes = [str(s.get("code") or "").strip()
                     for s in (payload.get("stops") or ())]
            codes = [c for c in codes if c]
            if codes:
                return codes
    return list(MUNI_LIVE_STOPS_FALLBACK)


def _muni_live_one(key, code):
    """One stop's monitored visits, as (response epoch, [visit, ...])."""
    import gzip
    import urllib.error
    import urllib.request
    from urllib.parse import urlencode

    query = urlencode({"api_key": key, "agency": MUNI_511_AGENCY,
                       "format": "json", "stopCode": code})
    req = urllib.request.Request(
        MUNI_511_URL + "?" + query,
        headers={"User-Agent": "flaschen-taschen-ftdata/1 (+wall display)",
                 "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(MUNI_LIVE_MAX_BYTES + 1)
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise MuniRateLimited(exc.headers.get("Retry-After") or "?")
        raise
    if len(raw) > MUNI_LIVE_MAX_BYTES:
        raise RuntimeError("stop %s returned over %d bytes, so the stopCode "
                           "filter did not apply" % (code, MUNI_LIVE_MAX_BYTES))
    if "gzip" in encoding:
        raw = gzip.decompress(raw)
    # utf-8-sig, not utf-8: 511 prefixes a byte order mark it does not mention.
    doc = json.loads(raw.decode("utf-8-sig"))

    delivery = doc.get("ServiceDelivery") or {}
    stamp = _muni_epoch(delivery.get("ResponseTimestamp"))
    monitoring = delivery.get("StopMonitoringDelivery") or {}
    if isinstance(monitoring, list):            # SIRI permits either shape
        monitoring = monitoring[0] if monitoring else {}

    visits = []
    for visit in monitoring.get("MonitoredStopVisit") or ():
        journey = visit.get("MonitoredVehicleJourney") or {}
        call = journey.get("MonitoredCall") or {}
        expected = _muni_epoch(call.get("ExpectedArrivalTime")
                               or call.get("ExpectedDepartureTime"))
        aimed = _muni_epoch(call.get("AimedArrivalTime")
                            or call.get("AimedDepartureTime"))
        if expected is None:
            # No prediction at all is not a bus this panel can place on its
            # axis, so it is dropped rather than guessed at from the aim.
            continue
        visits.append({
            "line": str(journey.get("LineRef") or "").strip(),
            "dir": str(journey.get("DirectionRef") or "").strip().upper(),
            "dest": " ".join(str(call.get("DestinationDisplay")
                                 or journey.get("DestinationName")
                                 or "").split())[:24],
            "exp": int(round(expected)),
            "aim": int(round(aimed)) if aimed is not None else None,
            # Monitored false means 511 is reading the timetable back to us
            # rather than watching a vehicle. The panel draws the difference.
            "mon": bool(journey.get("Monitored")),
        })
    visits.sort(key=lambda v: v["exp"])
    return stamp, visits[:MUNI_LIVE_MAX_VISITS]


@product(MUNI_LIVE_PRODUCT, ttl=MUNI_LIVE_TTL, interval=MUNI_LIVE_INTERVAL,
         volatile=True,
         description="live 511.org arrival predictions for the six 19/22/55 "
                     "stops nearest the installation (needs $FT_511_KEY)")
def _muni_live():
    """Six filtered StopMonitoring calls, one per stop, about 4 KB in total.

    Volatile: a prediction is worthless across a reboot, and rewriting six of
    them onto the Pi's SD card every eighteen minutes would buy nothing.
    """
    key = muni_live_key()
    if not key:
        raise RuntimeError(
            "$%s is not set; get a free token at https://511.org/open-data/"
            "token and export it for the fetcher. muni.py falls back to the "
            "%s timetable without it." % (MUNI_511_KEY_ENV, MUNI_PRODUCT))

    codes = _muni_live_stops()
    stops = {}
    errors = []
    newest = None
    limited = None
    for code in codes:
        try:
            stamp, visits = _muni_live_one(key, code)
        except MuniRateLimited as exc:
            # Stop the pass dead. Asking the remaining five is how a soft limit
            # becomes a hard one, and they would all get the same answer.
            limited = exc.retry_after
            errors.append("%s: 429, retry after %s" % (code, limited))
            break
        except Exception as exc:
            # One stop failing is not the product failing: five lanes of real
            # predictions and one honest gap beats no panel at all.
            errors.append("%s: %s" % (code, type(exc).__name__))
            continue
        stops[code] = visits
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp

    if not stops:
        raise RuntimeError("no stop answered (%s)"
                           % ("; ".join(errors) or "no stops configured"))

    return {
        "schema": 1,
        "t": int(newest if newest is not None else time.time()),
        "agency": MUNI_511_AGENCY,
        "stops": stops,
        "n_stops": len(stops),
        "n_asked": len(codes),
        "n_visits": sum(len(v) for v in stops.values()),
        "rate_limited": limited,
        "errors": errors,
        "source": "511.org SIRI StopMonitoring",
    }, MUNI_511_URL


# --------------------------------------------------------------------------
# Every BART train, as a path through time. stringline.py draws these.
#
# **Why BART.** It publishes GTFS-Realtime with no API key, no signup and no
# terms beyond politeness -- https://api.bart.gov/gtfsrt/tripupdate.aspx answers
# 200 with about 39 KB of protobuf to anybody who asks. The obvious alternative
# for a wall in the Mission is Muni, and 511.org's GTFS-RT returns 401 without a
# free-but-registered key, which was verified before this was written. So BART.
#
# **The protobuf is decoded by hand, in about sixty lines.** The proper answer
# is `gtfs-realtime-bindings`, which pulls in `protobuf`, which is a C extension
# and a wheel and a version skew on a Raspberry Pi that already has enough of
# those. What is actually needed here is five field numbers deep in a message
# whose wire format is self-describing: every field is a tag varint carrying a
# number and a type, and the four types that appear are varint, length-delimited
# and two fixed widths that can be skipped. Nothing is validated against a
# schema, which is the honest trade -- a field that changes meaning would be
# read as the old meaning -- and against that, the reader cannot be broken by a
# field being *added*, which is the thing that actually happens to these feeds.
#
# **A TripUpdate is only the future.** The feed carries, per running train, the
# stops it has not reached yet; a stop drops out behind the train as it passes.
# That is exactly half a stringline. So the fetcher keeps the other half: each
# pass merges the new predictions into what it already had for that trip, and a
# stop that has fallen out of the feed keeps the last time it was given. Since
# the fetch runs every minute, that last time was published within sixty seconds
# of the train actually being there, which makes it an observation in every
# sense that matters to a panel. The record therefore *accumulates* -- it is the
# only product here that is a function of its own previous value -- and the
# ninety minutes it holds is what the past half of the diagram is drawn from.
#
# **Trips have to be matched to a line, and the feed does not say.** BART's
# TripDescriptor carries a trip_id and nothing else: no route_id, no direction,
# no headsign. Two things recover it, in order:
#
#   1. a trip_id -> line table baked into `stringline-lines.npz` from the static
#      schedule. Trip ids are regenerated every time BART publishes a new
#      schedule, so this table goes stale a few times a year -- and is therefore
#      only ever a *hint*, accepted when the live stop list is consistent with
#      it and ignored otherwise.
#   2. failing that, the set of stations the trip calls at. A trip whose stops
#      all lie on exactly one line is on that line. Checked against the whole
#      static schedule, this is right for 2384 trips, wrong for none, and
#      undecidable for 346 -- the SFO-Millbrae and Warm Springs-Berryessa
#      shuttles, which really are on two lines at once.
#
# A trip that neither method resolves is counted and dropped, never guessed at.
# Guessing would put a Red train on the Yellow diagram, which on a stringline is
# not a small error: it invents a headway that does not exist.
#
# One minute is the interval and five the TTL, matching the ADS-B reasoning: a
# minute of drift is a train a kilometre out of place, which the diagram absorbs
# because it is drawn at two kilometres a row, and five minutes is the point at
# which the panel should say so rather than keep drawing. `volatile` because it
# is rewritten 1440 times a day and is worthless the next morning.
# --------------------------------------------------------------------------

BART_URL = "https://api.bart.gov/gtfsrt/tripupdate.aspx"

BART_PRODUCT = "bart-stringline"
BART_TTL = 300
BART_INTERVAL = 60

# How much history the record carries. The panel's default window is forty
# minutes of past, and a train takes ninety to cross the Yellow line end to end,
# so ninety keeps a whole run visible and bounds the record at the same time.
BART_KEEP = 5400.0

# Backstops, so a feed having a strange day cannot grow the record without
# limit. Both are far above anything BART has ever put in it: 83 trips and 1098
# stop times was a Monday teatime.
BART_MAX_TRIPS = 300
BART_MAX_POINTS = 60

# The baked line geometry, which is stringline.py's asset and is read here for
# one thing only: turning platform stop ids into stations and stations into
# lines. Cached in a one-slot dict because --loop keeps this process alive for
# weeks and the file does not change under it.
BART_ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "stringline-lines.npz")
_BART_GEOM = {}


def _pb_varint(buf, i):
    """A base-128 varint at `i`. Returns (value, next index)."""
    r = s = 0
    while True:
        c = buf[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7


def _pb_fields(buf):
    """Decode one protobuf message into [(field number, wire type, value)].

    Values are ints for varints and `bytes` for length-delimited fields; the
    two fixed-width types are handed back as bytes too and are never used here.
    Unknown field numbers cost nothing, which is the property that makes this
    safe to point at a feed that gains fields later.
    """
    out = []
    i, n = 0, len(buf)
    while i < n:
        key, i = _pb_varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _pb_varint(buf, i)
        elif wt == 2:
            ln, i = _pb_varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        else:
            raise ValueError("protobuf wire type %d at %d" % (wt, i))
        out.append((fn, wt, v))
    return out


def _pb_get(fields, num):
    return [v for f, _, v in fields if f == num]


def _pb_signed(v):
    """Protobuf writes a negative int32 as a 64-bit two's complement varint."""
    return v - (1 << 64) if v >= (1 << 63) else v


def _bart_geometry():
    """(stop id -> station code, [ {code: index} per line ], hints, keys).

    Loaded once. numpy is imported inside the function, like everything else
    optional in this file, so `load()` stays free of it.
    """
    if "g" in _BART_GEOM:
        return _BART_GEOM["g"]
    import numpy as np
    with np.load(BART_ASSET, allow_pickle=False) as z:
        keys = [str(k) for k in z["line_key"]]
        off = [int(v) for v in z["line_off"]]
        code = [str(c) for c in z["st_code"]]
        stop_of = dict(zip((str(s) for s in z["sid"]),
                           (str(c) for c in z["sid_code"])))
        index = [{c: i for i, c in enumerate(code[off[k]:off[k + 1]])}
                 for k in range(len(keys))]
        ends = [(0, off[k + 1] - off[k] - 1) for k in range(len(keys))]
        hints = {}
        for tid, li, dr, last in zip(z["trip_id"], z["trip_line"],
                                     z["trip_dir"], z["trip_last"]):
            hints[str(tid)] = (int(li), int(dr), int(last))
    g = (stop_of, index, ends, hints, keys)
    _BART_GEOM["g"] = g
    return g


def _bart_line_of(tid, codes, index, ends, hints):
    """(line, direction) for a trip, or (None, None) if it cannot be told.

    `codes` are the stations the trip still calls at, in order. Direction 0 is
    towards the line's last station, which is the direction the panel draws
    downwards.
    """
    hint = hints.get(tid)
    if hint is not None:
        li, dr, last = hint
        ix = index[li]
        if all(c in ix for c in codes):
            # The live stop list has to be a *suffix* of the scheduled trip:
            # every station on the line, running the scheduled way round, and
            # ending at or before the scheduled terminal. Not "ending exactly
            # at it", because BART's feed drops a trip's final stop a station
            # early -- a Millbrae train's last update is SFO -- and requiring
            # the terminal threw away a quarter of the feed. Three conditions
            # together are still a strong enough check that a trip id reused by
            # a later schedule for a different line will fail them.
            seq = [ix[c] for c in codes]
            step = -1 if dr else 1
            fwd = all((seq[i + 1] - seq[i]) * step > 0
                      for i in range(len(seq) - 1))
            ends_ok = (seq[-1] <= last) if dr == 0 else (seq[-1] >= last)
            if fwd and ends_ok:
                return li, dr
    cand = [li for li, ix in enumerate(index) if all(c in ix for c in codes)]
    if len(cand) > 1:
        # Several lines share the Market Street trunk, so a train seen only
        # inside it is on all of them as far as its stop list can say. Its
        # terminal breaks most of those ties; what it does not break stays
        # undecided rather than being guessed.
        cand = [li for li in cand if index[li][codes[-1]] in ends[li]]
    if len(cand) != 1:
        return None, None
    li = cand[0]
    a, b = index[li][codes[0]], index[li][codes[-1]]
    if a == b:
        return None, None
    return li, (0 if b > a else 1)


def _bart_parse(blob, stop_of):
    """The feed as (feed timestamp, [(trip id, [(stop code, time)], delay)]).

    Arrival is preferred over departure because a stringline is about when the
    train *reaches* a place. Stops the feed marks SKIPPED are dropped, and so is
    anything at a stop id the baked geometry does not know -- which is how the
    OAK Airport shuttle, which is not one of the five lines, leaves quietly.
    """
    top = _pb_fields(blob)
    feed_t = 0.0
    head = _pb_get(top, 1)
    if head:
        ts = _pb_get(_pb_fields(head[0]), 3)
        if ts:
            feed_t = float(ts[0])
    trips = []
    for ent in _pb_get(top, 2):
        tu = _pb_get(_pb_fields(ent), 3)
        if not tu:
            continue
        tuf = _pb_fields(tu[0])
        desc = _pb_get(tuf, 1)
        if not desc:
            continue
        tid = _pb_get(_pb_fields(desc[0]), 1)
        if not tid:
            continue
        delay = _pb_get(tuf, 5)
        delay = _pb_signed(delay[0]) if delay else None
        stops = []
        for stu in _pb_get(tuf, 2):
            sf = _pb_fields(stu)
            rel = _pb_get(sf, 5)
            if rel and rel[0] == 1:                          # SKIPPED
                continue
            sid = _pb_get(sf, 4)
            if not sid:
                continue
            code = stop_of.get(sid[0].decode("ascii", "replace"))
            if code is None:
                continue
            ev = _pb_get(sf, 2) or _pb_get(sf, 3)            # arrival, else dep
            if not ev:
                continue
            evf = _pb_fields(ev[0])
            when = _pb_get(evf, 2)
            if not when:
                continue
            if delay is None:
                d = _pb_get(evf, 1)
                if d:
                    delay = _pb_signed(d[0])
            stops.append((code, float(when[0])))
        if len(stops) >= 1:
            trips.append((tid[0].decode("ascii", "replace"), stops,
                          0.0 if delay is None else float(delay)))
    return feed_t, trips


def _bart_previous(cache_dir, index):
    """Last pass's record, back as {trip id: [line, dir, {station: time}, delay]}.

    Reading the product's own last output is what makes the past half of the
    diagram exist. It is deliberately forgiving: a record from an older format,
    or one naming a line that no longer exists, simply contributes nothing and
    the history rebuilds itself over the next hour and a half.
    """
    out = {}
    got = load(BART_PRODUCT, cache_dir)
    if got is None:
        return out
    payload = got[0] or {}
    for tr in payload.get("trips", []):
        try:
            li = int(tr["l"])
            if not 0 <= li < len(index):
                continue
            t0 = float(tr["t0"])
            pts = {int(s): t0 + float(a) for s, a in zip(tr["s"], tr["a"])}
            if pts:
                out[str(tr["i"])] = [li, int(tr.get("d", 0)), pts,
                                     float(tr.get("y", 0.0))]
        except Exception:                                    # noqa: BLE001
            continue
    return out


@product(BART_PRODUCT, ttl=BART_TTL, interval=BART_INTERVAL, volatile=True,
         description="BART trains as time-distance paths, from the keyless "
                     "GTFS-Realtime TripUpdate feed")
def _bart_stringline(cache_dir):
    """A rolling ninety minutes of every BART train's path along its line."""
    stop_of, index, ends, hints, keys = _bart_geometry()
    feed_t, seen = _bart_parse(get(BART_URL, timeout=25), stop_of)
    now = time.time()
    if not 1e9 < feed_t < now + 3600:
        # A feed with no timestamp, or one whose clock is wrong, is still full
        # of usable predictions; only the "how old is this" line suffers.
        feed_t = now

    state = _bart_previous(cache_dir, index)
    unknown = 0
    for tid, stops, delay in seen:
        codes = [c for c, _ in stops]
        prev = state.get(tid)
        if prev is not None:
            li, dr = prev[0], prev[1]
        else:
            li, dr = _bart_line_of(tid, codes, index, ends, hints)
            if li is None:
                unknown += 1
                continue
            prev = state[tid] = [li, dr, {}, 0.0]
        ix = index[li]
        for code, when in stops:
            i = ix.get(code)
            if i is not None:
                prev[2][i] = when
        prev[3] = delay

    cut = now - BART_KEEP
    trips = []
    for tid, (li, dr, pts, delay) in state.items():
        pts = {s: t for s, t in pts.items() if t >= cut}
        if len(pts) < 2:
            continue
        # Station order *is* travel order -- a train calls at them in sequence,
        # which is the only ordering a Marey diagram can be drawn from.
        order = sorted(pts, reverse=bool(dr))
        if len(order) > BART_MAX_POINTS:
            order = order[-BART_MAX_POINTS:]
        times = [pts[s] for s in order]
        # Forced non-decreasing. A prediction that goes backwards between two
        # stops happens, rarely, when an estimate is revised across a fetch, and
        # the demo interpolates against these as an x axis: numpy's interp on a
        # non-increasing x is not an error, it is silently wrong.
        for i in range(1, len(times)):
            if times[i] < times[i - 1]:
                times[i] = times[i - 1]
        t0 = times[0]
        trips.append((times[-1], {
            "i": tid, "l": li, "d": dr, "t0": int(round(t0)),
            "s": order, "a": [int(round(x - t0)) for x in times],
            "y": int(round(delay)),
        }))

    # Newest last-known position first if anything has to go: a train that
    # finished an hour ago is the least interesting thing in the record.
    trips.sort(key=lambda r: -r[0])
    kept = [r[1] for r in trips[:BART_MAX_TRIPS]]
    payload = {
        "t": now, "feed_t": feed_t, "lines": keys,
        "keep": BART_KEEP,
        "n_feed": len(seen), "n_trips": len(kept), "n_unknown": unknown,
        "n_points": sum(len(tr["s"]) for tr in kept),
        "units": {"t0": "epoch seconds", "a": "seconds after t0",
                  "s": "station index within the line", "y": "delay seconds",
                  "d": "0 towards the line's last station, 1 towards its first"},
        "source": "BART GTFS-Realtime TripUpdates",
        "trips": kept,
    }
    return payload, BART_URL


# Not a flag on product(): marking the spec afterwards keeps the registration
# helper exactly as the other products use it. This one wants the cache
# directory because, uniquely here, its new record is a function of its old one.
PRODUCTS[BART_PRODUCT]["blob"] = True


# --------------------------------------------------------------------------
# What California is running on. caiso.py draws this.
#
# CAISO's "Today's Outlook" page is backed by three keyless CSVs that are
# rewritten every five minutes, and they are the whole product: no key, no
# registration, no terms beyond ordinary politeness. The alternative is
# EIA-930, which is the same picture an hour later and **needs an API key**,
# and OASIS, which needs a client certificate and speaks zipped XML. So this
# is the source, and it is fetched at a tenth of the rate it changes.
#
# **The paths have moved and will move again.** Every script older than about
# a year fetches `/outlook/SP/fuelsource.csv`; that 404s now. What answers
# today is `/outlook/current/<name>.csv`, with `/outlook/history/<YYYYMMDD>/`
# alongside it for finished days. Both were checked by hand before this was
# written, and CAISO_BASE is one string so the next move is one line.
#
# Three files rather than one because they are three different measurements
# and only the first is a mix:
#
#   fuelsource.csv  thirteen fuels in MW, 5-minute, midnight to now
#   demand.csv      day-ahead and hour-ahead forecasts for the *whole* day,
#                   plus actual demand up to now and nulls after it
#   co2.csv         emissions by source in metric tons an hour, to now
#
# The forecast columns are why demand.csv is worth a request of its own: they
# are the only thing in any of this that knows what the evening looks like, so
# the panel has something honest to draw to the right of the now-line instead
# of dead space.
#
# **The Time column is CAISO's own local wall clock**, "HH:MM" with no date and
# no offset, which is the Pacific zone whatever the machine fetching it thinks
# it is in. So the timestamps are resolved here, once, against
# America/Los_Angeles explicitly rather than against `localtime` -- a fetcher
# run from a laptop in another zone would otherwise write a record whose
# midnight is somebody else's midnight, and the panel would draw the whole day
# shifted with nothing to say it had. Epoch seconds from there on, like the
# tides. The two DST days are handled by resolving each row separately instead
# of assuming 288 rows times 300 seconds spans a day: in March one of those
# days is 276 rows long and in November one is 300, and a uniform grid laid
# over either puts the evening peak an hour out.
#
# Everything is stored as published, ungrouped: thirteen fuels, not five bands.
# How to group them so that sixty-four rows of LED can be read from across a
# room is a *drawing* decision and it belongs in the demo, where it can be
# argued with, rather than baked irreversibly into the cache.
# --------------------------------------------------------------------------

CAISO_BASE = os.environ.get("FT_CAISO_BASE", "https://www.caiso.com/outlook")
CAISO_TZ = "America/Los_Angeles"

# An hour. The numbers themselves arrive every five minutes, so a record this
# old has missed eleven of them and the leading edge of the curve is visibly
# behind the clock -- which is exactly when the panel should start saying so.
# The rest of the day's curve is still perfectly true, so this is a warning
# threshold and not a delete: caiso.py keeps drawing and flags it.
CAISO_TTL = 3600

# Ten minutes, against a five-minute source. Half the available resolution,
# deliberately: nobody reads a 24-hour area chart closely enough to see one
# missing sample, and this is a public server with no key on it.
CAISO_INTERVAL = 600


def _caiso_csv(name):
    """One outlook CSV as (header, rows of strings). Raises if it is not one."""
    url = "%s/current/%s.csv" % (CAISO_BASE, name)
    text = get(url).decode("utf-8", "replace")
    import csv
    import io
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2 or not rows[0] or rows[0][0].strip().lower() != "time":
        # A 404 from this host is a styled HTML page with a 200-shaped body in
        # front of it, so "did it parse as CSV" is not the question; "is the
        # first column called Time" is.
        raise ValueError("%s is not a Today's Outlook CSV" % url)
    return [h.strip() for h in rows[0]], [r for r in rows[1:] if r], url


def _caiso_key(header):
    """'Small hydro' -> 'small_hydro'. The published name, mechanically."""
    return "".join(c if c.isalnum() else "_" for c in header.strip().lower())


def _caiso_epochs(datestr, stamps):
    """['00:00', ...] on a given Pacific date -> epoch seconds.

    Row by row rather than t0 + i*step, because two days a year are not 24
    hours long and a uniform grid over either of them is an hour wrong by the
    evening -- which is the half of the day this panel is about. A stamp that
    does not advance means the file has walked into the next day, which is what
    demand.csv's trailing 00:00 is.
    """
    import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(CAISO_TZ)
    except Exception:                                        # noqa: BLE001
        tz = None
    day = datetime.date.fromisoformat(datestr)
    out, prev, extra = [], None, 0
    for s in stamps:
        hh, mm = int(s[:2]), int(s[3:5])
        minute = hh * 60 + mm
        if prev is not None and minute <= prev:
            extra += 1
        prev = minute
        when = datetime.datetime.combine(
            day + datetime.timedelta(days=extra), datetime.time(hh, mm))
        # No tzdata on the machine is a real possibility on a minimal image, and
        # the fallback is right where it matters: the wall is in the same zone
        # as the ISO. It is wrong elsewhere, which is why it is not the default.
        out.append((when.replace(tzinfo=tz) if tz else when).timestamp())
    return out


def _caiso_today():
    """Today's date in CAISO's zone, as the CSVs mean it."""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(CAISO_TZ)).date().isoformat()
    except Exception:                                        # noqa: BLE001
        return datetime.date.today().isoformat()


def _caiso_num(s):
    """A cell as a float, or None. Blank means 'not yet', never zero.

    The distinction is the whole reason this is not `float(s or 0)`: demand.csv
    carries the rest of the day as empty cells, and a zero there would draw a
    grid that had switched itself off at teatime.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _caiso_table(name, datestr):
    """(t, {column_key: [values]}, order, url) for one outlook CSV.

    Trailing rows in which every column is blank are dropped -- the mix and
    emissions files are written for the whole day and filled in as it happens,
    so the tail is not missing data, it is data that has not happened yet.
    """
    header, rows, url = _caiso_csv(name)
    keys = [_caiso_key(h) for h in header[1:]]
    cols = {k: [] for k in keys}
    stamps = []
    for r in rows:
        vals = [_caiso_num(v) for v in r[1:len(header)]]
        vals += [None] * (len(keys) - len(vals))
        stamps.append(r[0].strip())
        for k, v in zip(keys, vals):
            cols[k].append(v)
    while stamps and all(cols[k][-1] is None for k in keys):
        stamps.pop()
        for k in keys:
            cols[k].pop()
    if not stamps:
        raise ValueError("%s has no populated rows yet" % url)
    return _caiso_epochs(datestr, stamps), cols, keys, url


def _caiso_round(values, places=0):
    """Store MW as integers. A tenth of a megawatt is not a thing anyone sees."""
    if places:
        return [None if v is None else round(v, places) for v in values]
    return [None if v is None else int(round(v)) for v in values]


@product("caiso-mix", ttl=CAISO_TTL, interval=CAISO_INTERVAL,
         description="CAISO fuel mix, demand and CO2 for today, 5-minute")
def _caiso_mix():
    """Today's California grid: what generated it, how much of it, and its CO2.

    Three requests and about 30 kB of record by the end of a day, which is the
    largest thing in this cache that is not pixels. It is worth it and it is
    deliberately not `volatile`: the payload is the day *so far*, so a record
    that survives a reboot is the difference between coming back up with the
    whole morning's duck curve and coming back up with a blank chart and one
    sample on it.

    Only the fuel mix is required. Demand and emissions are fetched separately
    and each is allowed to fail on its own, because a panel that can say what
    the state is burning is still worth having when the forecast endpoint is
    having an afternoon -- and losing all three because one of them moved is
    exactly the failure this file exists to avoid.
    """
    date = _caiso_today()
    t, fuels, order, url = _caiso_table("fuelsource", date)
    for k in order:
        fuels[k] = _caiso_round(fuels[k])

    payload = {
        "date": date, "tz": CAISO_TZ,
        "t": t, "n": len(t),
        "span": [t[0], t[-1]],
        # Midnight to midnight in CAISO's zone: the axis the day is drawn on,
        # and not derivable from `t` once the record is only half a day long.
        "day": [_caiso_epochs(date, ["00:00"])[0],
                _caiso_epochs(date, ["00:00", "00:00"])[1]],
        "fuels": fuels, "fuel_order": order,
        "units": {"generation": "MW", "demand": "MW",
                  "co2": "metric tons per hour"},
        "demand": None, "co2": None,
    }

    try:
        dt, dem, dorder, _ = _caiso_table("demand", date)
        payload["demand"] = {"t": dt, "n": len(dt), "order": dorder,
                             "series": {k: _caiso_round(dem[k]) for k in dorder}}
    except Exception as e:                                   # noqa: BLE001
        print("ftdata: caiso-mix demand unavailable: %r" % e, file=sys.stderr)

    try:
        ct, co2, corder, _ = _caiso_table("co2", date)
        payload["co2"] = {"t": ct, "n": len(ct), "order": corder,
                          "series": {k: _caiso_round(co2[k]) for k in corder}}
    except Exception as e:                                   # noqa: BLE001
        print("ftdata: caiso-mix co2 unavailable: %r" % e, file=sys.stderr)

    return payload, url


# --------------------------------------------------------------------------
# Which way San Francisco's shared bikes are moving. bikes.py draws this.
#
# **GBFS publishes no trips, and this product does not pretend otherwise.** Bay
# Wheels speaks GBFS -- the open standard every bikeshare system speaks -- and
# every feed in it is a *snapshot*: how many bikes are in each dock at this
# instant, and where the undocked ebikes are lying. There is no origin in it, no
# destination, no journey and no rider. Anything anybody says about bikes moving
# from A to B is inferred from how the counts changed between two snapshots, and
# the whole design of this record is about making that inference small, checkable
# and impossible to mistake for observation.
#
# So the fetcher takes a snapshot every ten minutes, differences it against the
# one before, and stores two derived quantities per interval and nothing else:
#
#   mov    the sum of |change| over every station, matched by station. One bike
#          ridden from one dock to another contributes 2 to it -- minus one at
#          the dock it left, plus one at the dock it reached -- so `mov / 2` is a
#          count of docked bikes that changed place. It is a **floor** and not a
#          trip count: two riders swapping a dock inside one ten-minute window
#          cancel and are invisible, a bike that leaves a dock and is left loose
#          at the kerb is a departure with no matching arrival, and an operator's
#          van moving fifteen bikes at once looks exactly like fifteen riders.
#
#   flow   the *net* change in docked bikes for each of forty bands of distance
#          from the Ferry Building. This is the only thing here that is a field:
#          its running sum along the axis is the net number of bikes that had to
#          cross each distance, which is a conserved quantity and is what a swarm
#          can honestly be drawn from. Churn inside a band cancels out of it by
#          construction, which is the point -- what survives is structure.
#
# **The axis is distance from downtown, and that is a choice with a reason.**
# San Francisco's 383 docks are a blob 11.9 km by 12.4 km, so a map of them on a
# panel five times wider than it is tall would spend three hundred columns saying
# that the city is square. Distance from the Ferry Building is instead the one
# spatial variable the data actually varies along: the commute is in and out of
# it, and it happens to run downhill, which is why the ground elevation is kept
# alongside. `bikes.py` draws the pair as a cross-section -- how far out, and how
# high up -- and the flow as a swarm over it.
#
# **It attaches an altitude to every station.** GBFS carries lat/lon and no
# elevation. The heights come from a committed bake, `demos/bikes-terrain.npz`,
# so nothing on the wall ever asks a terrain service anything -- see
# BIKES_TERRAIN below for its provenance and for what happens to a station the
# bake has never heard of.
#
# **It reduces 790 kB to about twenty.** Three feeds, 634 stations and 600-odd
# loose bikes go in. What comes out is five short arrays over the stations inside
# the city sorted by distance, a rolling twelve hours of forty-number flow
# vectors, and a couple of dozen scalars. The arrays stay per-station rather than
# being binned into panel columns here, for the same reason caiso-mix stores
# thirteen fuels and not five bands: how to bin them is a drawing decision and it
# belongs where it can be argued with. The *flow* is binned here, because a
# rolling day of 383 numbers per interval would not be small and forty is enough
# resolution for a field whose shortest real feature is a neighbourhood.
#
# **It remembers, and it has to.** A snapshot feed cannot answer "what happened
# in the last hour" at all, so each pass appends one entry to a rolling series
# kept in the record. Ten-minute buckets keyed on absolute epoch rather than a
# growing list: a bucket fetched twice is overwritten, a bucket that is missed is
# simply absent, the series is trimmed to twelve hours and 80 entries on every
# write, and a record restored from before a reboot picks up where it left off.
# That is why this product is deliberately *not* volatile -- the accumulated half
# day is the only thing here that cannot be re-fetched. On a cold cache there is
# no flow at all until the second pass lands, which is a state bikes.py draws in
# words rather than hiding.
#
# **Cost.** Three requests, about 790 kB, every ten minutes: 1.3 kB/s averaged,
# which is half what quake-week costs. station_information is nearly static and
# could be fetched far more rarely, but re-fetching it is what makes a
# newly-installed station appear correctly rather than being quietly dropped, and
# 348 kB per ten minutes is not worth a staleness bug.
# --------------------------------------------------------------------------

BIKES_PRODUCT = "baywheels"

# gbfs.baywheels.com 301-redirects here; using the destination directly saves a
# round trip and makes the host visible in the record's `source`.
BIKES_GBFS = os.environ.get("FT_GBFS_BASE", "https://gbfs.lyftbikes.com/gbfs/en")
BIKES_STATUS_URL = BIKES_GBFS + "/station_status.json"
BIKES_INFO_URL = BIKES_GBFS + "/station_information.json"
BIKES_FREE_URL = BIKES_GBFS + "/free_bike_status.json"

# The crop, as (lat0, lat1, lon0, lon1). Bay Wheels is one system covering four
# separated cities -- San Francisco, Oakland/Emeryville/Berkeley across the bay,
# and San Jose fifty miles south -- and they do not share a commute, a terrain or
# a downtown. Mixing them would put a San Jose station 4 km from "downtown" on
# the same axis as a Mission station and mean nothing by it. This box is the city
# and county of San Francisco plus the few Daly City docks on its south edge:
# 383 of the system's 634 stations, and the ones whose commute is the story.
BIKES_BBOX = (37.700, 37.840, -122.530, -122.350)

# The origin of the distance axis: the Ferry Building at the foot of Market
# Street. Not the geometric centre of the city and not the centre of the dock
# network -- the place the morning peak is pointed at. Every `dist_m` in the
# record is a great-circle distance from here.
BIKES_DOWNTOWN = (37.7955, -122.3937)

# Half an hour. station_status is regenerated every minute and we take it every
# ten, so a record this old has missed twenty updates -- which on a Friday
# evening is enough for the picture to be a lie about which stations are dry.
# It still draws, with the age on it; see bikes.py.
BIKES_TTL = 1800

# Ten minutes, matching the history bucket exactly so that one pass fills one
# bucket and one bucket is one difference. A minute-cadence feed sampled every
# ten minutes is a deliberate loss, and it is the loss that sets what `mov` can
# mean: anything that happens and un-happens inside ten minutes is invisible to
# this record. Sampling faster would raise the floor and cost the public server
# more; ten minutes is where the two arguments met.
BIKES_INTERVAL = 600

# The rolling series. Ten-minute buckets, twelve hours, and a hard cap on the
# entry count a little over twelve hours' worth -- belt and braces, because the
# thing that must never happen to a record that appends to itself is unbounded
# growth, and a clock that jumps is a real event on a Pi with no RTC.
#
# Twelve hours rather than the twenty-four this used to keep: the panel replays
# the window as a swarm and half a day is already one whole commute plus both
# of its shoulders, while the flow vector is forty numbers a bucket and 144 of
# them would double the record to buy a second night nobody watches.
BIKES_HIST_BUCKET = 600.0
BIKES_HIST_HOURS = 12.0
BIKES_HIST_MAX = 80

# --------------------------------------------------------------------------
# The calendar day, kept separately from the rolling twelve hours.
#
# `hist` is a *window*: twelve hours of forty-number flow vectors, capped at
# eighty entries, which is what the swarm replay needs and is deliberately not
# longer. A panel that draws today against a typical day needs something else
# entirely -- every ten minutes since local midnight, whether that is one hour
# ago or twenty-three -- and it needs almost nothing per slot: how much moved,
# and over how many seconds that was measured.
#
# So this is 144 ten-minute slots from local midnight carrying two scalars
# each, which is about 1.5 kB of JSON against the 20 kB the record already is,
# and it is reset when the local date rolls over rather than rolling
# continuously. Local midnight and not UTC: the thing being drawn is a *day* as
# somebody who rides a bike experiences one, and the axis on the panel is
# labelled in the time on their watch. `time.mktime` with `tm_isdst = -1`
# resolves the two ambiguous hours a year the way the system zone says to.
#
# A slot is null until a pass lands in it. That is the whole cold-start story:
# a wall that booted at three in the afternoon has 89 nulls in front of it and
# the panel says where the trace starts instead of drawing a line from zero.
BIKES_DAY_SLOTS = 144

# Bands of distance from downtown, for the flow field and for the loose bikes.
# Forty over twelve kilometres is a band every 300 m, which is eight columns of
# a 320-wide panel and about ten docks. Finer would be storing noise: the median
# ten-minute interval moves a couple of dozen docked bikes across the whole city.
BIKES_FLOW_BINS = 40
BIKES_FLOW_KM = 12.0

# How far apart two snapshots have to be before their difference is worth
# calling a flow, and how far apart before it stops being one.
#
# The floor exists because the timer can fire twice in quick succession -- a
# manual `--once` next to a running loop -- and a four-minute difference scaled
# up to an hourly rate is mostly quantisation. The ceiling is four missed passes:
# past that the two snapshots straddle enough of a commute that "net change"
# stops describing a flow and starts describing a different time of day.
BIKES_FLOW_MIN_DT = 240.0
BIKES_FLOW_MAX_DT = 2400.0

# --------------------------------------------------------------------------
# Observed journeys, for the free-floating ebikes only, and the privacy design
# that goes with them.
#
# **What was measured.** free_bike_status carries two identifiers per undocked
# ebike: `bike_id`, an opaque 32-hex token, and `name`, the number printed on
# the physical bike ("190-591"). GBFS rotates `bike_id` between rentals
# specifically so that trips cannot be reconstructed, and it does: across two
# snapshots 36 minutes apart, 0 of 634 tokens survived, and across two four
# minutes apart 590 of 624 survived but *not one of the survivors had moved* --
# the token is stable exactly while the bike sits still. `name` is not rotated:
# 585 of 620 survived 36 minutes, with a median displacement of 4.5 m and a
# 90th percentile of 11 m, which is GPS jitter, and nine bikes that had plainly
# been ridden somewhere.
#
# So for this subset -- and only this subset -- a journey is *observable*: the
# same physical bike seen at two places at two times. That is a materially
# better thing to draw than an inference, and it is what the panel now leads
# with. What it is not is the whole system: about 620 free-floating ebikes
# against 383 docks and some 2 700 docked bikes, and a bike that docks simply
# vanishes from this feed. The panel says which fleet it is drawing.
#
# **The privacy design, which is deliberate and not boilerplate.** The spec
# rotates `bike_id` to stop exactly what `name` makes possible again, and this
# record is going to live on a wall in a public makerspace. So:
#
#   * The printed number is hashed the moment it is read and the raw string
#     never reaches a variable that is stored. Nothing anywhere in the payload,
#     and nothing on the panel, is a bike number.
#   * The tokens live in `loose_base` only, which holds *one* snapshot and is
#     overwritten on every pass. No identifier of any kind enters `hist`. So
#     the record cannot link a bike across more than a single ten-minute
#     interval, however long the fetcher has been running -- there is no
#     accumulating trip history in it to obtain.
#   * What survives into the history is a pair of positions on the panel's own
#     distance axis, rounded to 100 m, with no way back to which bike made it.
#
# The hash is not itself the control and is not claimed as one: six-digit bike
# numbers are a small enough space to enumerate, so an attacker holding a
# record could recover the current snapshot's numbers -- which the public feed
# already gives them. The control is that history carries no identifier at all.
#
# **The threshold.** 120 m, against a measured 90th-percentile jitter of 11 m.
# Below it a bike has not moved; above it, it has been somewhere.
BIKES_TRACK_MIN_M = 120.0

# Most journeys kept per bucket. Two movers in four minutes at ten on a Monday
# night; the morning peak is far busier, and the cap is what stops one
# extraordinary interval from doubling the record. `seen`, `moved`, `gone` and
# `came` are counted before the cap, so the panel's number is the true one even
# when the drawn tracks are a sample of it.
BIKES_TRACK_MAX = 48

# The two ends of a journey are stored in hundreds of metres along the distance
# axis, 0..119 over the twelve kilometre crop. That is three characters a
# number in JSON against the panel's own 37 m per column, and the tracks are
# drawn as motion between two neighbourhoods rather than as a GPS trace, which
# is also the resolution the panel should be claiming.
BIKES_TRACK_UNIT_M = 100.0

# Ground elevation per station, in metres, baked once and committed.
#
#   source     opentopodata.org public API, dataset `ned10m` -- the USGS 3D
#              Elevation Program 1/3 arc-second seamless DEM, ~10 m posting,
#              public domain, no key and no signup
#   stations   gbfs.lyftbikes.com/gbfs/en/station_information.json
#   retrieved  2026-08-10, all 634 system stations, 100 locations a request
#   arrays     ids, elev (m), lat, lon, meta (the recipe, as text)
#
# Baked rather than fetched because a terrain service is a second thing that can
# be down, and the ground does not move. A station the bake has never heard of --
# one installed since -- takes the elevation of the nearest baked station, which
# in a city with a dock every few blocks is a good deal better than dropping it,
# and is recorded in the payload as `interpolated` so the number is checkable.
BIKES_TERRAIN = "bikes-terrain.npz"


def _bikes_terrain():
    """The baked elevation table. numpy only, no network, raises if absent."""
    import numpy as np
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        BIKES_TERRAIN)
    with np.load(path) as z:
        return ({str(k): float(v) for k, v in zip(z["ids"], z["elev"])},
                z["lat"].astype("float64"), z["lon"].astype("float64"),
                z["elev"].astype("float64"))


def _bikes_elevation(ids, lats, lons):
    """Metres for each station, and how many had to be guessed.

    Guessed means "installed since the bake": nearest baked station in the
    plane, with longitude scaled by cos(latitude) so that a degree east and a
    degree north are the same distance. Exact for every station in the table,
    which today is all but none of them.
    """
    import numpy as np
    table, blat, blon, belev = _bikes_terrain()
    out = np.empty(len(ids), np.float64)
    missing = []
    for i, sid in enumerate(ids):
        got = table.get(sid)
        if got is None:
            missing.append(i)
        else:
            out[i] = got
    if missing:
        kx = float(np.cos(np.radians(0.5 * (BIKES_BBOX[0] + BIKES_BBOX[1]))))
        for i in missing:
            dy = blat - lats[i]
            dx = (blon - lons[i]) * kx
            out[i] = belev[int(np.argmin(dy * dy + dx * dx))]
    return out, len(missing)


def _bikes_distance(lats, lons):
    """Metres from BIKES_DOWNTOWN, as a float array.

    Equirectangular and not haversine, deliberately: the longest distance in
    this box is twelve kilometres and the flat-earth error over that is under a
    metre, against a 300 m band width. quake.py uses haversine because its
    radius reaches Cape Mendocino, which is a different argument.
    """
    import numpy as np
    kx = float(np.cos(np.radians(BIKES_DOWNTOWN[0])))
    dy = (lats - BIKES_DOWNTOWN[0]) * 111320.0
    dx = (lons - BIKES_DOWNTOWN[1]) * 111320.0 * kx
    return np.hypot(dx, dy)


def _bikes_bin(dist_m):
    """Which distance band each station falls in. Clipped, never dropped."""
    import numpy as np
    edge = BIKES_FLOW_KM * 1000.0
    idx = (dist_m / edge * BIKES_FLOW_BINS).astype(np.int64)
    return np.clip(idx, 0, BIKES_FLOW_BINS - 1)


def _bikes_sid(ids):
    """Six hex characters per station id, concatenated into one string.

    The record has to carry enough of each station's identity to difference the
    next snapshot against this one, and it must not carry 383 UUIDs to do it --
    that is fourteen kilobytes of the same text every ten minutes forever. Six
    hex digits is sixteen million buckets for 383 stations, so a collision is a
    one-in-thirty-thousand event, and a collision merely misattributes one
    station's change to another rather than corrupting anything.

    Matching on a hash rather than on array position is what makes a station
    being installed, removed or renumbered cost nothing: the stations that are
    in both snapshots are differenced and the rest are simply not, whereas
    position matching would silently shift every station past the new one and
    invent a citywide flow out of an insertion.
    """
    import hashlib
    return "".join(hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]
                   for s in ids)


def _bikes_unsid(blob):
    """The inverse of _bikes_sid: a string back into a list of six-hex keys."""
    if not isinstance(blob, str) or len(blob) % 6:
        return []
    return [blob[i:i + 6] for i in range(0, len(blob), 6)]


def _bikes_flow(previous, sid, bikes, bins, as_of):
    """Difference this snapshot against the last one in the record.

    Returns (flow, mov, dt, base), where `flow` is the net change in docked
    bikes per distance band, `mov` is the sum of |change| over every station
    matched by identity, `dt` is the seconds the difference covers, and `base`
    is what the *next* pass should difference against.

    The three ways this can decline to answer are all real and all benign:

      no baseline      first pass after a cold start or a version change. One
                       bucket with no flow in it, and bikes.py says so.
      too close        the timer fired twice inside four minutes. The old
                       baseline is *kept* rather than replaced, so the next
                       ordinary pass still gets a full-length difference
                       instead of inheriting a ten-second one.
      too far or back  four missed passes, or a clock that jumped. The baseline
                       is reset to now and the next pass starts clean; joining
                       across the gap would draw an hour of commute as if it
                       had happened in ten minutes.
    """
    import numpy as np
    here = {"at": float(as_of), "sid": sid, "bikes": [int(v) for v in bikes]}
    old = (previous or {}).get("base")
    if not isinstance(old, dict):
        return None, None, None, here
    keys = _bikes_unsid(old.get("sid"))
    counts = old.get("bikes")
    try:
        at = float(old["at"])
    except (KeyError, TypeError, ValueError):
        return None, None, None, here
    if not keys or not isinstance(counts, list) or len(counts) != len(keys):
        return None, None, None, here

    dt = float(as_of) - at
    if dt < BIKES_FLOW_MIN_DT:
        # Too close together to mean anything -- and, crucially, keep the old
        # baseline. Replacing it here is the bug that makes a doubled pass
        # erase a good interval; see the docstring.
        return None, None, None, (old if dt >= 0.0 else here)
    if dt > BIKES_FLOW_MAX_DT:
        return None, None, None, here

    was = dict(zip(keys, counts))
    now_keys = _bikes_unsid(sid)
    flow = np.zeros(BIKES_FLOW_BINS, np.int64)
    mov = 0
    for i, k in enumerate(now_keys):
        before = was.get(k)
        if before is None:
            continue
        d = int(bikes[i]) - int(before)
        if d:
            mov += abs(d)
            flow[bins[i]] += d
    # `mov` counts both ends of a move, so it is even for anything that stayed
    # inside the city and odd only where a bike joined or left the docked fleet.
    # Halving happens in the demo, where the caveat can be printed next to it.
    #
    # This used to return `int(mov) * 2`, which was a plain bug: `mov` is
    # already the sum of |change| over the stations and so already counts both
    # ends, the record's own `units` block says "/2 is bikes moved", and the
    # demo dutifully halved it -- so every bike-movement figure this product
    # has ever produced was exactly twice the truth. Fixed here rather than
    # compensated for in the demo, because the unit the record documents is the
    # unit it should carry. Buckets written by the old code are twice as tall
    # as they should be and age out of `hist` within twelve hours.
    return [int(v) for v in flow], int(mov), round(dt, 1), here


def _bikes_anon(name):
    """A printed bike number as an opaque token, or None.

    Called at the moment the feed is parsed, so that the number itself lives
    only inside the parsing loop. See the BIKES_TRACK_* block for why this is
    not the privacy control on its own and what is.
    """
    import hashlib
    if not name:
        return None
    return hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:8]


def _bikes_tracks(previous, keys, lats, lons, as_of):
    """Journeys observed for the free-floating ebikes since the last snapshot.

    Returns (tracks, seen, gone, came, base):

      tracks  a flat list [from, to, from, to, ...] in hundreds of metres
              along the distance axis, one pair per bike that moved further
              than BIKES_TRACK_MIN_M, capped at BIKES_TRACK_MAX by displacement
              so that what is dropped is the shortest hops and not a slice of
              the city. None if no comparison could be made.
      seen    bikes present in both snapshots -- the denominator.
      gone    bikes in the old snapshot and not the new one. Almost always a
              bike that was docked or picked up by a van; occasionally one
              rented and in flight. A journey with one end unobservable.
      came    the reverse: undocked, released, or a rental ending.

    `gone` and `came` are counted and never drawn. They are real events and
    they are half-observations, and drawing a half-observation as a journey is
    the one thing this whole product is arranged to avoid; a dot appearing out
    of nothing on a map is read as a bike arriving from somewhere, which is
    exactly the claim that cannot be made.
    """
    import numpy as np
    n = len(keys)
    here = {"at": float(as_of), "k": [], "lat": [], "lon": []}
    if n:
        here["k"] = [k for k in keys]
        here["lat"] = [int(round(v * 1e4)) for v in lats]
        here["lon"] = [int(round(v * 1e4)) for v in lons]
    old = (previous or {}).get("loose_base")
    if not isinstance(old, dict) or not n:
        return None, 0, 0, 0, here
    try:
        at = float(old["at"])
        ok, olat, olon = old["k"], old["lat"], old["lon"]
    except (KeyError, TypeError, ValueError):
        return None, 0, 0, 0, here
    if not (isinstance(ok, list) and len(ok) == len(olat) == len(olon)):
        return None, 0, 0, 0, here
    dt = float(as_of) - at
    if not (BIKES_FLOW_MIN_DT <= dt <= BIKES_FLOW_MAX_DT):
        # The same three windows the docked flow uses, and for the same
        # reasons; a doubled pass keeps the older baseline so the next
        # ordinary one still sees a full-length interval.
        return None, 0, 0, 0, (old if 0.0 <= dt < BIKES_FLOW_MIN_DT else here)

    was = {}
    for i, k in enumerate(ok):
        if k is not None:
            was[k] = (olat[i] * 1e-4, olon[i] * 1e-4)
    now_keys = set(k for k in keys if k is not None)
    seen = gone = 0
    a_lat, a_lon, b_lat, b_lon = [], [], [], []
    for i, k in enumerate(keys):
        if k is None:
            continue
        before = was.get(k)
        if before is None:
            continue
        seen += 1
        a_lat.append(before[0])
        a_lon.append(before[1])
        b_lat.append(float(lats[i]))
        b_lon.append(float(lons[i]))
    gone = sum(1 for k in was if k not in now_keys)
    came = len(now_keys) - seen
    if not seen:
        return [], 0, gone, came, here

    a_lat = np.asarray(a_lat, np.float64)
    a_lon = np.asarray(a_lon, np.float64)
    b_lat = np.asarray(b_lat, np.float64)
    b_lon = np.asarray(b_lon, np.float64)
    kx = float(np.cos(np.radians(BIKES_DOWNTOWN[0])))
    step = np.hypot((b_lat - a_lat) * 111320.0,
                    (b_lon - a_lon) * 111320.0 * kx)
    moved = np.flatnonzero(step >= BIKES_TRACK_MIN_M)
    if len(moved) > BIKES_TRACK_MAX:
        moved = moved[np.argsort(step[moved])[::-1][:BIKES_TRACK_MAX]]
    da = _bikes_distance(a_lat[moved], a_lon[moved])
    db = _bikes_distance(b_lat[moved], b_lon[moved])
    cap = int(BIKES_FLOW_KM * 1000.0 / BIKES_TRACK_UNIT_M) - 1
    qa = np.clip(np.round(da / BIKES_TRACK_UNIT_M), 0, cap).astype(int)
    qb = np.clip(np.round(db / BIKES_TRACK_UNIT_M), 0, cap).astype(int)
    tracks = []
    for x0, x1 in zip(qa, qb):
        tracks.append(int(x0))
        tracks.append(int(x1))
    return tracks, int(seen), int(gone), int(came), here


def _bikes_history(previous, sample, now):
    """Append one sample to the rolling series and bound it. Pure arithmetic.

    Keyed on the absolute ten-minute bucket, which is what makes every failure
    mode benign. A pass that runs twice inside one bucket overwrites rather than
    lengthening the series. A pass that is missed leaves a hole, and the hole is
    visible because the epochs are stored rather than assumed to be regular. A
    clock that jumps backwards -- a Pi with no battery-backed RTC getting NTP
    for the first time after boot -- would otherwise leave the series in an
    order the demo would draw as a scribble, so anything at or after the new
    bucket is dropped before appending.

    `flow` is a list per entry rather than a scalar, which the length check
    below handles without knowing that: every column is required to be a list as
    long as `t`, and a record written by a version that stored different columns
    simply fails that and starts a fresh series rather than being extended into
    a shape the demo would misread.
    """
    keys = ("fleet_m", "docks_m", "bikes", "empty", "loose", "mov", "dt",
            "flow", "trk", "seen", "gone", "came")
    bucket = float(int(now // BIKES_HIST_BUCKET) * int(BIKES_HIST_BUCKET))
    hist = {"t": []}
    for k in keys:
        hist[k] = []

    old = (previous or {}).get("hist")
    if isinstance(old, dict) and isinstance(old.get("t"), list):
        n = len(old["t"])
        # Every column has to be the same length as `t` or the record is from a
        # version that stored different things and cannot be extended safely.
        if all(isinstance(old.get(k), list) and len(old[k]) == n for k in keys):
            keep = [i for i, t in enumerate(old["t"])
                    if isinstance(t, (int, float)) and t < bucket
                    and t > bucket - BIKES_HIST_HOURS * 3600.0]
            keep = keep[-(BIKES_HIST_MAX - 1):]
            hist["t"] = [float(old["t"][i]) for i in keep]
            for k in keys:
                hist[k] = [old[k][i] for i in keep]

    hist["t"].append(bucket)
    for k in keys:
        hist[k].append(sample.get(k))
    hist["bucket"] = BIKES_HIST_BUCKET
    hist["hours"] = BIKES_HIST_HOURS
    hist["bins"] = BIKES_FLOW_BINS
    hist["n"] = len(hist["t"])
    return hist


def _bikes_day0(now):
    """The epoch of the local midnight that starts the day containing `now`."""
    lt = time.localtime(now)
    return float(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                              0, 0, 0, 0, 0, -1)))


def _bikes_today(previous, sample, now):
    """The day so far, in 144 ten-minute slots from local midnight.

    Two columns and no more: `mov`, the sum of |change| over the stations for
    the difference that landed in this slot, and `dt`, the seconds that
    difference actually covers. The second one is not redundant. A missed pass
    makes the next difference forty minutes long instead of ten, and a rate
    computed against the nominal slot would then draw a spike followed by three
    holes; with `dt` in the record the demo can spread that one measurement
    across the four slots it really describes, which is what it does.

    Reset by date rather than rolled: if the stored `day0` is not the local
    midnight of `now`, the arrays start empty. A record carried across a
    reboot, a clock that jumps, and the ordinary passage of midnight are then
    all the same code path. See BIKES_DAY_SLOTS.
    """
    n = BIKES_DAY_SLOTS
    day0 = _bikes_day0(now)
    mov = [None] * n
    dt = [None] * n
    old = (previous or {}).get("today")
    if isinstance(old, dict):
        try:
            same = abs(float(old.get("day0")) - day0) < 1.0
        except (TypeError, ValueError):
            same = False
        if same:
            for key, dst in (("mov", mov), ("dt", dt)):
                col = old.get(key)
                if isinstance(col, list) and len(col) == n:
                    dst[:] = [None if v is None else v for v in col]
    slot = int((now - day0) // BIKES_HIST_BUCKET)
    if 0 <= slot < n and sample.get("mov") is not None:
        mov[slot] = int(sample["mov"])
        dt[slot] = None if sample.get("dt") is None else float(sample["dt"])
    return {"day0": day0, "bucket": BIKES_HIST_BUCKET, "n": n,
            "mov": mov, "dt": dt}


def _bikes_round(values, places=None):
    """A list of numbers as ints (or `places` decimals), passing None through."""
    if places is None:
        return [None if v is None else int(round(float(v))) for v in values]
    return [None if v is None else round(float(v), places) for v in values]


@product(BIKES_PRODUCT, ttl=BIKES_TTL, interval=BIKES_INTERVAL,
         description="Bay Wheels in SF: net bike flow by distance from downtown")
def _baywheels(cache_dir):
    """Bay Wheels reduced to a flow field: net docked-bike change by distance.

    station_status and station_information are both required -- without the
    second there are no coordinates, and so neither an altitude nor a distance
    from downtown, which are the two axes. free_bike_status is allowed to fail
    on its own, because the docked fleet is the entire flow field and losing it
    to a hiccup in a feed about a different population would be the failure this
    file exists to avoid.
    """
    import numpy as np

    info_doc = get_json(BIKES_INFO_URL, timeout=30)
    status_doc = get_json(BIKES_STATUS_URL, timeout=30)

    lat0, lat1, lon0, lon1 = BIKES_BBOX
    inside = {}
    for s in info_doc["data"]["stations"]:
        try:
            la, lo = float(s["lat"]), float(s["lon"])
            sid = str(s["station_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if lat0 <= la <= lat1 and lon0 <= lo <= lon1:
            inside[sid] = (la, lo, int(s.get("capacity") or 0))

    ids, lats, lons = [], [], []
    caps, bikes, docks, renting, ebikes = [], [], [], [], []
    for s in status_doc["data"]["stations"]:
        got = inside.get(str(s.get("station_id")))
        if got is None:
            continue
        la, lo, cap = got
        # `is_installed` 0 is a dock that has been pulled out of the ground for
        # the season. It is not an empty station and it is not a station at all.
        if not int(s.get("is_installed") or 0):
            continue
        b = int(s.get("num_bikes_available") or 0)
        d = int(s.get("num_docks_available") or 0)
        # Capacity as published, falling back to what the four dock counts add
        # up to. They agree everywhere today; the fallback is for the station
        # whose capacity field is missing or zero, where the counts are still a
        # true denominator and a dropped station is not.
        c = cap or (b + d + int(s.get("num_bikes_disabled") or 0)
                    + int(s.get("num_docks_disabled") or 0))
        if c <= 0:
            continue
        ids.append(str(s["station_id"]))
        lats.append(la)
        lons.append(lo)
        caps.append(c)
        bikes.append(min(b, c))
        docks.append(d)
        ebikes.append(int(s.get("num_ebikes_available") or 0))
        # `is_renting` 0 with the dock still installed is a station taken out of
        # service -- construction, a street fair, a broken kiosk. Its bikes are
        # real and its emptiness is not somebody's commute, so it is kept and
        # flagged rather than either counted or dropped.
        renting.append(1 if int(s.get("is_renting") or 0) else 0)

    if len(ids) < 20:
        raise ValueError("only %d Bay Wheels stations inside the SF box"
                         % len(ids))

    lats = np.asarray(lats, np.float64)
    lons = np.asarray(lons, np.float64)
    elev, interpolated = _bikes_elevation(ids, lats, lons)
    dist = _bikes_distance(lats, lons)

    # Sorted by distance from downtown once, here, so that every array in the
    # record and every band index computed from it agree by construction. That
    # is a change of axis from the version of this product that sorted by
    # altitude, and it is the axis the panel now draws along. `kind="stable"` so
    # two stations at the same metre keep a fixed order between passes, which is
    # what stops the panel shimmering where the docks are dense.
    order = np.argsort(dist, kind="stable")
    ids = [ids[i] for i in order]
    dist = dist[order]
    elev = elev[order]
    lats, lons = lats[order], lons[order]
    caps = np.asarray(caps, np.float64)[order]
    bikes = np.asarray(bikes, np.float64)[order]
    docks = np.asarray(docks, np.float64)[order]
    ebikes = np.asarray(ebikes, np.float64)[order]
    renting = np.asarray(renting, np.int64)[order]
    n = len(elev)
    bins = _bikes_bin(dist)

    total_bikes = float(bikes.sum())
    total_caps = float(caps.sum())
    # The two altitudes the old version of this panel was built on, kept because
    # they cost four numbers a bucket and they are the one sentence about
    # gravity that survives without a hillside to draw it on. `fleet_m` is the
    # mean height of a bike you could go and unlock; `docks_m` is the mean height
    # of a *parking space*, which is where the fleet would sit if it were spread
    # evenly. The difference goes negative when the fleet has run downhill.
    fleet_m = float((elev * bikes).sum() / total_bikes) if total_bikes else None
    docks_m = float((elev * caps).sum() / total_caps) if total_caps else None

    open_ = renting == 1
    empty = int(((bikes == 0) & open_).sum())
    jammed = int(((docks == 0) & open_).sum())

    loose_bins = [0] * BIKES_FLOW_BINS
    loose_n = loose_off = 0
    free_url = None
    free_key, free_lat, free_lon = [], [], []
    try:
        free_doc = get_json(BIKES_FREE_URL, timeout=30)
        free_url = BIKES_FREE_URL
        fl, fo = [], []
        for b in free_doc["data"]["bikes"]:
            try:
                la, lo = float(b["lat"]), float(b["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (lat0 <= la <= lat1 and lon0 <= lo <= lon1):
                continue
            if int(b.get("is_disabled") or 0) or int(b.get("is_reserved") or 0):
                loose_off += 1
                continue
            fl.append(la)
            fo.append(lo)
            # See _bikes_anon() and the BIKES_TRACK_* block: the printed bike
            # number is read here and turned into an opaque token immediately.
            # It is never put in a variable that reaches the payload.
            free_key.append(_bikes_anon(b.get("name")))
        if fl:
            # Binned on the same distance axis as everything else, which for a
            # loose bike is exact rather than approximate: it has a position of
            # its own and the axis is a function of position. The old version
            # had to borrow the altitude of the nearest dock; nothing is
            # borrowed here.
            free_lat = np.asarray(fl, np.float64)
            free_lon = np.asarray(fo, np.float64)
            fd = _bikes_distance(free_lat, free_lon)
            loose_n = int(len(fd))
            loose_bins = np.bincount(_bikes_bin(fd),
                                     minlength=BIKES_FLOW_BINS).tolist()
    except Exception as e:                                   # noqa: BLE001
        print("ftdata: baywheels free_bike_status unavailable: %r" % e,
              file=sys.stderr)

    now = time.time()
    # The feed's own timestamp, not ours: `dt` is the interval the difference
    # actually covers, and the fetch can sit behind a slow request or a retry.
    # Falling back to the wall clock keeps a feed that omits it working.
    as_of = float(status_doc.get("last_updated") or now)
    previous = load(BIKES_PRODUCT, cache_dir)
    prev_payload = previous[0] if previous else None
    sid = _bikes_sid(ids)
    flow, mov, dt, base = _bikes_flow(prev_payload, sid, bikes, bins, as_of)
    tracks, seen, gone, came, loose_base = _bikes_tracks(
        prev_payload, free_key, free_lat, free_lon, as_of)

    sample = {"fleet_m": None if fleet_m is None else round(fleet_m, 2),
              "docks_m": None if docks_m is None else round(docks_m, 2),
              "bikes": int(total_bikes), "empty": empty, "loose": loose_n,
              "mov": mov, "dt": dt, "flow": flow,
              "trk": tracks, "seen": seen, "gone": gone, "came": came}

    payload = {
        "as_of": as_of,
        "region": "San Francisco",
        "bbox": list(BIKES_BBOX),
        "downtown": list(BIKES_DOWNTOWN),
        "n": n,
        # Five arrays over the stations, ascending by distance from downtown.
        # Everything the panel draws about *now* comes out of these.
        "dist_m": _bikes_round(dist),
        "elev_m": _bikes_round(elev),
        "fill_pct": _bikes_round(bikes / np.maximum(caps, 1.0) * 100.0),
        "free_docks": _bikes_round(docks),
        "open": [int(v) for v in renting],
        "loose_bins": loose_bins,
        "totals": {
            "stations": n,
            "closed": int(n - open_.sum()),
            "capacity": int(total_caps),
            "bikes": int(total_bikes),
            "ebikes": int(ebikes.sum()),
            "free_docks": int(docks.sum()),
            "empty": empty,
            "jammed": jammed,
            "loose": loose_n,
            "loose_unavailable": loose_off,
        },
        "altitude_m": {"fleet": sample["fleet_m"], "docks": sample["docks_m"],
                       "low": round(float(elev.min()), 1),
                       "high": round(float(elev.max()), 1)},
        "flow": {"bins": BIKES_FLOW_BINS, "km": BIKES_FLOW_KM,
                 "min_dt": BIKES_FLOW_MIN_DT, "max_dt": BIKES_FLOW_MAX_DT,
                 "track_m": BIKES_TRACK_MIN_M, "track_max": BIKES_TRACK_MAX,
                 "track_unit_m": 100.0},
        "interpolated": int(interpolated),
        "hist": _bikes_history(prev_payload, sample, now),
        # The calendar day so far, which `hist` cannot answer: see
        # BIKES_DAY_SLOTS. Two columns of 144 slots, reset at local midnight.
        "today": _bikes_today(prev_payload, sample, now),
        # Not for drawing. These two are the snapshot the *next* pass
        # differences against, and they are the only things in this payload
        # that are state rather than observation. Kept in the record and not in
        # a sidecar because a sidecar lives in tmpfs and a reboot would cost a
        # bucket every time. `loose_base` is overwritten on every pass and
        # never enters `hist`, which is the whole of the privacy design; see
        # the BIKES_TRACK_* block above.
        "base": base,
        "loose_base": loose_base,
        "units": {"dist_m": "metres from the Ferry Building",
                  "elev_m": "metres above NAVD88", "fill_pct": "percent",
                  "hist.t": "epoch seconds, start of a 10 minute bucket",
                  "hist.dt": "seconds the flow difference covers",
                  "hist.mov": "sum of |change| over stations; /2 is bikes moved",
                  "hist.flow": "net docked-bike change per distance band",
                  "hist.trk": "observed free-ebike journeys, [from, to, ...] "
                              "in hundreds of metres from downtown",
                  "hist.seen": "free ebikes present in both snapshots",
                  "hist.gone": "free ebikes that vanished: docked or taken",
                  "hist.came": "free ebikes that appeared: undocked or freed",
                  "today.day0": "epoch of the local midnight the slots start at",
                  "today.mov": "as hist.mov, in 10 minute slots from midnight",
                  "today.dt": "seconds the slot's difference covers"},
        "sources": [BIKES_INFO_URL, BIKES_STATUS_URL, free_url],
    }
    return payload, BIKES_STATUS_URL


# Not a flag on product(), for the same reason goes-psw does it this way: the
# helper stays exactly as the other products use it. This product is not pixels
# and writes no sidecar -- it needs the cache directory for the other reason,
# which is that it reads its own previous record both to extend the rolling
# series and to difference this snapshot against the last one, and doing that
# against ftdata's default cache while the fetcher was pointed at another one
# would silently graft two machines' histories together.
PRODUCTS[BIKES_PRODUCT]["blob"] = True

# --------------------------------------------------------------------------
# The bike docks within a walk of the front door. docks.py draws this.
#
# **A different question from `baywheels`, off the same three feeds.** That
# product is about the city: a flow field over twelve kilometres, differenced
# between snapshots, replayed over half a day. This one is about the next sixty
# seconds for somebody standing in the workshop -- is there a bike within a few
# minutes' walk, is it an ebike, and coming the other way, is there a free dock
# to put one in. Nothing here is differenced against anything and nothing here
# accumulates: it is a snapshot of about forty docks, and when it is stale it is
# simply wrong rather than incomplete, which is why the TTL is short and the
# panel prints the age.
#
# It is a separate product rather than more fields on `baywheels` because the
# two want opposite things from the fetcher. `baywheels` must not be sampled
# faster than its ten-minute history bucket and must never be volatile, because
# the accumulated half day is the only thing in it that cannot be re-fetched.
# This one wants two minutes and is worth nothing after a reboot.
#
# **The docked ebike count is `num_ebikes_available`, and this is the one field
# it is easy to get wrong.** GBFS 2.x also defines
# `num_bikes_available_types`, which is the obvious place to look and which
# Lyft's SF feed does not publish at all -- the key is absent from every one of
# the 634 stations, so code that reads it gets zero everywhere and quietly
# reports that San Francisco has no docked ebikes. It has plenty: 36 of the 100
# docked bikes within a kilometre of the wall on the evening this was written.
# `num_ebikes_available` is a *subset* of `num_bikes_available`, so the classic
# count is the difference and the two add up to the total, which is how the
# panel columns it.
#
# **The crop is a circle and not a bounding box.** `baywheels` takes a lat/lon
# box because it wants a city; this wants "how far do I have to walk", so the
# radius is a real distance from one point and the payload is sorted by it.
# 1.5 km stored against the panel's default 1.0 km of drawing, so `--radius` can
# be turned up on the wall without the fetcher having to agree.
#
# **Cost, and the one piece of caching in this file.** Three feeds are 795 kB:
# station_information 348, station_status 243, free_bike_status 204. Taking all
# three every two minutes would be 6.6 kB/s sustained, which is five times what
# `baywheels` costs and more than this panel is worth. But station_information
# is *near-static* -- names, coordinates and dock capacities, changing when a
# station is installed or moved -- so the trimmed version of it (the forty-odd
# stations inside the radius, about a kilobyte) is kept in the record and reused
# for an hour. Steady state is then status plus free bikes, 447 kB every two
# minutes, 3.7 kB/s, with one 348 kB request an hour on top. What that costs is
# that a station installed inside the radius can take up to an hour to appear,
# which for a thing that happens a few times a year is the right trade. Set
# DOCKS_INFO_TTL to 0 to turn the cache off.
# --------------------------------------------------------------------------

DOCKS_PRODUCT = "docks-nearby"

# The wall's own address, from the site config -- Sequoia Fabrica, Dogpatch,
# San Francisco, surveyed to the building rather than to the block.
#
# This product was written while `adsb.py`, `quake.py` and QUAKE_LAT/QUAKE_LON
# still carried (37.7627, -122.3966), 273 m north-east, and it kept a private
# constant rather than inherit an address it knew to be wrong. That conflict is
# now resolved the other way: every one of them reads `ftsite`, so this reads it
# too. Worth keeping the reason on record, because this is the panel where it
# mattered -- at 39 m to the pixel, 273 m is seven pixels and the difference
# between the nearest dock being Jackson Playground and being Rhode Island St,
# where on a 50 nautical mile radar picture it is a fifth of one.
DOCKS_SITE = (ftsite.LAT, ftsite.LON)

# How far out to collect, in metres of straight line. 1.5 km is 45 docks, 235
# docked bikes and 851 free docks on a Monday evening -- comfortably more than
# the panel draws, so `--radius` is a drawing decision and not a fetch one.
DOCKS_RADIUS_M = 1500.0

# Hard caps, so a feed that suddenly reports every station in the Bay at the
# same coordinates cannot turn a 6 kB record into a 300 kB one. Both are well
# clear of the live numbers (45 stations, 27 loose bikes inside 1.5 km).
DOCKS_MAX = 64
DOCKS_LOOSE_MAX = 48

# Metres a minute on foot, for the walk times that are the panel's units.
#
# 75 m/min is 4.5 km/h, an ordinary adult pace, and it is applied to the
# *straight-line* distance -- so these are optimistic by whatever the street
# grid costs, which in Dogpatch is not much because the grid is a grid. The
# number is here rather than in the demo because it is what makes `dist_m` mean
# something to a person, and because the panel and this file must not be able to
# disagree about it.
DOCKS_WALK_M_PER_MIN = 75.0

# Ten minutes. station_status is regenerated every minute; a record this old has
# missed nine updates, which on a Friday evening is enough for a dock the panel
# says has two bikes in it to have none. It still draws, with the age on it.
DOCKS_TTL = 600

# Two minutes. Fast enough to be worth calling a "right now" panel, slow enough
# not to hammer a public feed: 30 requests an hour against a file regenerated 60
# times an hour. Under FAST_INTERVAL, so the fast timer takes it.
DOCKS_INTERVAL = 120

# How long the trimmed station_information block is reused. See the cost note.
DOCKS_INFO_TTL = 3600.0


def _docks_metres(lat, lon, site=None):
    """Straight-line metres from the wall. Equirectangular, plain Python.

    Flat-earth over 1.5 km is wrong by well under a centimetre, and there are a
    few hundred stations to test rather than a few hundred thousand, so this
    stays out of numpy entirely -- the whole loop costs less than the import.
    """
    import math
    la0, lo0 = site or DOCKS_SITE
    kx = math.cos(math.radians(la0))
    dy = (float(lat) - la0) * 111320.0
    dx = (float(lon) - lo0) * 111320.0 * kx
    return math.hypot(dx, dy)


def _docks_walk_min(dist_m):
    """Straight-line metres as whole minutes on foot. See DOCKS_WALK_M_PER_MIN."""
    return int(round(float(dist_m) / DOCKS_WALK_M_PER_MIN))


def _docks_site_elevation():
    """Ground height at the wall, in metres, off the committed terrain bake.

    There is no DEM in this tree, only `bikes-terrain.npz`, which is elevations
    at the 634 dock locations -- so this is the height of the nearest *dock*,
    which today is Jackson Playground 290 m away and 4 m lower than the shop
    floor's true figure. That is fine for what it is used for, which is the sign
    and rough size of the climb to each dock, and it is why the payload calls it
    `approx`. Returns None rather than raising if the bake is missing: an
    elevation is a nice-to-have and the dock counts are not.
    """
    try:
        elev, _missing = _bikes_elevation(
            ["__wall__"], [DOCKS_SITE[0]], [DOCKS_SITE[1]])
        return float(elev[0])
    except Exception:                                        # noqa: BLE001
        return None


def _docks_info(previous, radius_m):
    """The trimmed station_information block: reused if young enough, else fetched.

    Returns (info, fetched), where `info` is a dict of parallel lists over the
    stations inside the radius and `fetched` says whether the 348 kB request
    actually happened on this pass. Anything at all wrong with the cached block
    -- missing, short, from a run with a different radius or a different site --
    is treated as absent, because the alternative is pairing this pass's counts
    with last hour's coordinates and there is no way to notice that on a wall.
    """
    keys = ("id", "name", "lat", "lon", "cap")
    old = (previous or {}).get("info")
    if DOCKS_INFO_TTL > 0 and isinstance(old, dict):
        try:
            fresh = time.time() - float(old["at"]) < DOCKS_INFO_TTL
            same = (float(old["radius_m"]) == float(radius_m)
                    and [round(v, 7) for v in old["site"]]
                    == [round(v, 7) for v in DOCKS_SITE])
            n = len(old["id"])
            whole = all(isinstance(old.get(k), list) and len(old[k]) == n
                        for k in keys)
            if fresh and same and whole and n:
                return old, False
        except (KeyError, TypeError, ValueError):
            pass

    doc = get_json(BIKES_INFO_URL, timeout=30)
    info = {"at": time.time(), "radius_m": float(radius_m),
            "site": list(DOCKS_SITE)}
    for k in keys:
        info[k] = []
    for s in doc["data"]["stations"]:
        try:
            la, lo = float(s["lat"]), float(s["lon"])
            sid = str(s["station_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if _docks_metres(la, lo) > radius_m:
            continue
        info["id"].append(sid)
        # The published name, verbatim. Shortening "Rhode Island St at 17th St"
        # into something that fits 22 characters of 3x5 type is a drawing
        # decision and it lives in the demo, where it can be argued with.
        info["name"].append(str(s.get("name") or "")[:48])
        info["lat"].append(round(la, 5))
        info["lon"].append(round(lo, 5))
        info["cap"].append(int(s.get("capacity") or 0))
    return info, True


@product(DOCKS_PRODUCT, ttl=DOCKS_TTL, interval=DOCKS_INTERVAL, volatile=True,
         description="Bay Wheels docks within a walk of the wall: "
                     "bikes, ebikes and free docks, nearest first")
def _docks_nearby(cache_dir):
    """The docks inside DOCKS_RADIUS_M, sorted by how far they are to walk.

    station_status is required. station_information is required the first time
    and once an hour after that; in between its trimmed form comes out of the
    previous record. free_bike_status is allowed to fail on its own -- a
    free-floating ebike parked on the kerb is worth drawing and is not what the
    panel is for, so losing that feed costs one line of the panel and not the
    panel.
    """
    previous = load(DOCKS_PRODUCT, cache_dir)
    prev_payload = previous[0] if previous else None
    info, info_fetched = _docks_info(prev_payload, DOCKS_RADIUS_M)
    status_doc = get_json(BIKES_STATUS_URL, timeout=30)

    near = {}
    for i, sid in enumerate(info["id"]):
        near[sid] = i

    rows = []
    for s in status_doc["data"]["stations"]:
        i = near.get(str(s.get("station_id")))
        if i is None:
            continue
        # `is_installed` 0 is a dock pulled out of the ground for the season.
        # It is not an empty station; there is nothing there to walk to.
        if not int(s.get("is_installed") or 0):
            continue
        la, lo = info["lat"][i], info["lon"][i]
        bikes = int(s.get("num_bikes_available") or 0)
        # See the block comment: this field and not num_bikes_available_types,
        # which Lyft's feed does not publish. Clamped because a subset that
        # exceeds its superset would come out of the arithmetic as a negative
        # number of classic bikes, and the panel would draw it.
        ebikes = min(bikes, int(s.get("num_ebikes_available") or 0))
        free = int(s.get("num_docks_available") or 0)
        cap = info["cap"][i] or (bikes + free
                                 + int(s.get("num_bikes_disabled") or 0)
                                 + int(s.get("num_docks_disabled") or 0))
        dist = _docks_metres(la, lo)
        rows.append({
            "d": dist, "sid": info["id"][i], "name": info["name"][i],
            "lat": la, "lon": lo,
            "bikes": bikes, "ebikes": ebikes, "free": free, "cap": int(cap),
            # `is_renting` 0 with the dock installed is a station out of service
            # -- construction, a street fair, a dead kiosk. Its bikes are real
            # and you cannot have them, so it is kept and flagged rather than
            # either counted or dropped. Same call baywheels makes.
            "open": 1 if int(s.get("is_renting") or 0) else 0,
            "ret": 1 if int(s.get("is_returning") or 0) else 0,
        })

    rows.sort(key=lambda r: r["d"])
    del rows[DOCKS_MAX:]
    if not rows:
        raise ValueError("no Bay Wheels stations within %.0f m of the wall"
                         % DOCKS_RADIUS_M)

    site_elev = _docks_site_elevation()
    try:
        # Exact for every station in the bake, which is all of them today; one
        # installed since takes the nearest baked station's height. Same helper
        # and the same caveat as baywheels, which is the point of sharing it.
        elev, _missing = _bikes_elevation(
            [r["sid"] for r in rows],
            [r["lat"] for r in rows], [r["lon"] for r in rows])
        elev = [round(float(v), 1) for v in elev]
    except Exception as e:                                   # noqa: BLE001
        print("ftdata: docks-nearby elevation unavailable: %r" % e,
              file=sys.stderr)
        elev = [None] * len(rows)

    loose = {"n": 0, "dist_m": [], "lat": [], "lon": [], "elec": [],
             "unavailable": 0, "source": None}
    try:
        free_doc = get_json(BIKES_FREE_URL, timeout=30)
        loose["source"] = BIKES_FREE_URL
        found = []
        for b in free_doc["data"]["bikes"]:
            try:
                la, lo = float(b["lat"]), float(b["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            d = _docks_metres(la, lo)
            if d > DOCKS_RADIUS_M:
                continue
            if int(b.get("is_disabled") or 0) or int(b.get("is_reserved") or 0):
                loose["unavailable"] += 1
                continue
            found.append((d, la, lo,
                          1 if str(b.get("type")) == "electric_bike" else 0))
        found.sort()
        # No identifier of any kind is read out of this feed, let alone stored.
        # `baywheels` hashes the printed bike number because it needs to match
        # one snapshot to the next; this product never compares two snapshots,
        # so it has no use for identity and takes none. See the BIKES_TRACK_*
        # block above for why that distinction is worth being explicit about.
        del found[DOCKS_LOOSE_MAX:]
        loose["n"] = len(found)
        loose["dist_m"] = [int(round(d)) for d, _la, _lo, _e in found]
        loose["lat"] = [round(la, 5) for _d, la, _lo, _e in found]
        loose["lon"] = [round(lo, 5) for _d, _la, lo, _e in found]
        loose["elec"] = [e for _d, _la, _lo, e in found]
    except Exception as e:                                   # noqa: BLE001
        print("ftdata: docks-nearby free_bike_status unavailable: %r" % e,
              file=sys.stderr)

    open_rows = [r for r in rows if r["open"]]
    payload = {
        # The feed's own stamp rather than ours: the panel's honesty about how
        # old its counts are should not include our request latency, and should
        # include the feed's own.
        "as_of": float(status_doc.get("last_updated") or time.time()),
        "site": list(DOCKS_SITE),
        "site_name": "Sequoia Fabrica",
        "site_elev_m": None if site_elev is None else round(site_elev, 1),
        "radius_m": DOCKS_RADIUS_M,
        "walk_m_per_min": DOCKS_WALK_M_PER_MIN,
        "n": len(rows),
        # Parallel arrays over the stations, ascending by distance. Everything
        # the panel draws comes out of these; nothing is pre-binned, because how
        # to bin them is a drawing decision.
        "name": [r["name"] for r in rows],
        "dist_m": [int(round(r["d"])) for r in rows],
        "walk_min": [_docks_walk_min(r["d"]) for r in rows],
        "lat": [r["lat"] for r in rows],
        "lon": [r["lon"] for r in rows],
        "bikes": [r["bikes"] for r in rows],
        "ebikes": [r["ebikes"] for r in rows],
        "free_docks": [r["free"] for r in rows],
        "capacity": [r["cap"] for r in rows],
        "elev_m": elev,
        "open": [r["open"] for r in rows],
        "returning": [r["ret"] for r in rows],
        "loose": loose,
        "totals": {
            "stations": len(rows),
            "closed": len(rows) - len(open_rows),
            "bikes": sum(r["bikes"] for r in open_rows),
            "ebikes": sum(r["ebikes"] for r in open_rows),
            "free_docks": sum(r["free"] for r in open_rows),
            "capacity": sum(r["cap"] for r in open_rows),
            "empty": sum(1 for r in open_rows if r["bikes"] == 0),
            "jammed": sum(1 for r in open_rows if r["free"] == 0),
            "loose": loose["n"],
        },
        # Kept so the next pass can skip the 348 kB request. Not for drawing:
        # `name`, `lat`, `lon` and `capacity` above are the same values in the
        # order the panel wants them.
        "info": info,
        "info_fetched": bool(info_fetched),
        "units": {"dist_m": "straight-line metres from the wall",
                  "walk_min": "whole minutes at %.0f m/min, straight line"
                              % DOCKS_WALK_M_PER_MIN,
                  "elev_m": "metres above NAVD88, from bikes-terrain.npz",
                  "site_elev_m": "approx: the nearest baked dock's elevation",
                  "bikes": "docked bikes available, ebikes included",
                  "ebikes": "of those, electric; classic is bikes - ebikes",
                  "free_docks": "empty docks, i.e. places to leave one",
                  "loose": "free-floating bikes, mostly electric, no docks"},
        "sources": [BIKES_INFO_URL, BIKES_STATUS_URL, loose["source"]],
    }
    return payload, BIKES_STATUS_URL


# Same reason as baywheels above, for half of it: this product reads its own
# previous record, to reuse the trimmed station_information rather than fetch
# 348 kB of it every two minutes. It writes no sidecar.
PRODUCTS[DOCKS_PRODUCT]["blob"] = True

# --------------------------------------------------------------------------
# The ground under the building. quake.py draws this.
#
# **One feed, two scales, and a third request that is not a feed.** USGS
# publishes a fixed set of summary GeoJSON files, regenerated every minute and
# served off a CDN, and `all_week.geojson` alone answers both halves of what the
# panel wants: everything the ANSS network located anywhere on Earth in the last
# seven days, which contains both every M0.4 under Berkeley and every M4.5+ from
# Tonga. Taking one file rather than composing `all_day` with `2.5_week` and
# `4.5_week` avoids the whole class of bug where two feeds disagree about the
# same event -- USGS revises magnitudes for hours after an origin, and two files
# fetched a second apart can hold two versions of one earthquake. It is 1.4 MB,
# which at a ten-minute cadence is 2.4 kB/s averaged, and the record we keep
# from it is about forty times smaller.
#
# What is stored is trimmed to what quake.py draws:
#
#   local    every event within 300 km of the wall, no magnitude floor at all,
#            with distance and bearing precomputed here so the demo never does
#            trigonometry per frame
#   world    the M4.5+ of the week as (time, magnitude) pairs only -- that is a
#            sparkline and nothing else -- plus the single largest in full
#   baseline the last M4.0+ within 100 km, whenever it was
#
# **The baseline is the one thing the feeds cannot answer**, and the panel's
# headline number depends on it. A local M4 happens a few times a year, so on
# almost every day of the year the answer lies outside every summary window that
# exists -- `significant_month` is global and a Bay Area M4.2 does not qualify.
# So that one number comes from the FDSN event service instead, which is the
# same catalogue and equally keyless: one radius query, `limit=1`, ordered by
# time, about 1 kB and under a second. It is fetched inside its own try/except
# because a failure there must not cost us the week's events too; when it fails
# the payload carries `baseline: null` and quake.py prints `--` rather than a
# number it does not have.
#
# **Quarry blasts are dropped.** The feed's `type` field distinguishes
# `earthquake` from `quarry blast`, `explosion` and `ice quake`, and the East Bay
# quarries put several a week into a 300 km radius. A demo about the ground
# moving on its own should not count somebody's morning shot, so non-earthquakes
# are filtered and the count of what was dropped is kept, because a filter you
# cannot see is a filter you cannot check.
# --------------------------------------------------------------------------

QUAKE_FEED = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
              "all_week.geojson")
QUAKE_FDSN = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# The wall's own address, the same one wx.py uses: Sequoia Fabrica, 1736 18th
# Street, from demos/site.json. Every distance and bearing here is from it.
QUAKE_LAT, QUAKE_LON = ftsite.LAT, ftsite.LON

QUAKE_LOCAL_KM = 300.0          # "near here", generously drawn
QUAKE_WORLD_MAG = 4.5           # the planet's week, the conventional threshold
QUAKE_BASELINE_KM = 100.0       # "close enough that the room felt it"
QUAKE_BASELINE_MAG = 4.0

# An hour. The catalogue is revised continuously -- magnitudes move, events are
# deleted -- but nothing in this picture curdles quickly, and the honest failure
# is a panel that says it is looking at hour-old data rather than one that goes
# blank. Past the TTL quake.py flags it; past three times it stops drawing.
QUAKE_TTL = 3600


def _quake_km_bearing(lat, lon):
    """Great-circle distance in km and compass bearing from the wall."""
    import math
    la0, lo0 = math.radians(QUAKE_LAT), math.radians(QUAKE_LON)
    la1, lo1 = math.radians(float(lat)), math.radians(float(lon))
    dlo = lo1 - lo0
    # Haversine rather than the equirectangular approximation the demo could
    # get away with: this radius reaches Cape Mendocino and the southern San
    # Joaquin, and the flat-earth error at 300 km is a couple of kilometres --
    # small, but this is the number the panel prints next to a place name.
    h = (math.sin((la1 - la0) / 2) ** 2
         + math.cos(la0) * math.cos(la1) * math.sin(dlo / 2) ** 2)
    km = 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))
    y = math.sin(dlo) * math.cos(la1)
    x = math.cos(la0) * math.sin(la1) - math.sin(la0) * math.cos(la1) * math.cos(dlo)
    return km, math.degrees(math.atan2(y, x)) % 360.0


def _quake_event(feature, want_place=True):
    """One GeoJSON feature reduced to the fields quake.py actually draws."""
    p = feature.get("properties") or {}
    lon, lat, dep = (list(feature.get("geometry", {}).get("coordinates") or [])
                     + [None, None, None])[:3]
    mag = p.get("mag")
    if mag is None or lat is None or lon is None:
        return None
    km, bearing = _quake_km_bearing(lat, lon)
    out = {"id": feature.get("id"), "t": float(p["time"]) / 1000.0,
           "mag": round(float(mag), 2), "magtype": p.get("magType"),
           "lat": round(float(lat), 4), "lon": round(float(lon), 4),
           "dep": None if dep is None else round(float(dep), 1),
           "km": round(km, 1), "bearing": round(bearing)}
    if want_place:
        # The feed's place strings run to "16km SSE of Cobb, California" and
        # occasionally much longer. The panel has room for about twenty
        # characters, and the leading distance is one we recomputed ourselves.
        out["place"] = str(p.get("place") or "")[:48]
    return out


def _quake_baseline():
    """The last M4+ within 100 km, from FDSN. None if the service says no."""
    from urllib.parse import urlencode
    url = QUAKE_FDSN + "?" + urlencode({
        "format": "geojson", "latitude": "%.4f" % QUAKE_LAT,
        "longitude": "%.4f" % QUAKE_LON,
        "maxradiuskm": "%g" % QUAKE_BASELINE_KM,
        "minmagnitude": "%g" % QUAKE_BASELINE_MAG,
        # 1900 rather than an open start: ANSS has nothing instrumental before
        # then anyway, and a bounded query is the polite kind to send.
        "starttime": "1900-01-01", "orderby": "time", "limit": "1",
    })
    doc = get_json(url, timeout=30)
    feats = doc.get("features") or []
    if not feats:
        return None
    ev = _quake_event(feats[0])
    if ev is not None:
        ev["radius_km"] = QUAKE_BASELINE_KM
        ev["min_mag"] = QUAKE_BASELINE_MAG
    return ev


@product("quake-usgs", ttl=QUAKE_TTL, interval=600,
         description="USGS ANSS: everything within 300 km, the world's M4.5+")
def _quake_usgs():
    """A week of earthquakes, trimmed to two scales and one long baseline."""
    doc = get_json(QUAKE_FEED, timeout=60)
    feats = doc.get("features")
    if not isinstance(feats, list) or not feats:
        raise ValueError("no features in the USGS week feed")

    local, world, dropped = [], [], 0
    biggest = None
    for f in feats:
        p = f.get("properties") or {}
        mag = p.get("mag")
        if mag is None:
            continue
        geom = (f.get("geometry") or {}).get("coordinates") or []
        if len(geom) < 2 or geom[0] is None or geom[1] is None:
            continue
        if p.get("type") not in (None, "earthquake"):
            dropped += 1
            continue
        km, _ = _quake_km_bearing(geom[1], geom[0])
        if km <= QUAKE_LOCAL_KM:
            ev = _quake_event(f)
            if ev is not None:
                local.append(ev)
        if float(mag) >= QUAKE_WORLD_MAG:
            world.append([round(float(p["time"]) / 1000.0, 1),
                          round(float(mag), 2)])
            if biggest is None or float(mag) > biggest["mag"]:
                biggest = _quake_event(f)

    # Newest first. The demo wants "the latest" far more often than it wants a
    # scan, and sorting once here is free.
    local.sort(key=lambda e: e["t"], reverse=True)
    world.sort()

    try:
        baseline = _quake_baseline()
    except Exception as e:                                   # noqa: BLE001
        # Losing the headline scalar must not lose the map with it.
        print("ftdata: quake-usgs baseline query failed: %r" % e,
              file=sys.stderr)
        baseline = None

    gen = doc.get("metadata", {}).get("generated")
    return {
        "site": [QUAKE_LAT, QUAKE_LON],
        "generated": None if gen is None else float(gen) / 1000.0,
        "feed": "all_week.geojson",
        "span_h": 168.0,
        "local": {"radius_km": QUAKE_LOCAL_KM, "n": len(local),
                  "non_earthquakes_dropped": dropped, "events": local},
        "world": {"min_mag": QUAKE_WORLD_MAG, "n": len(world),
                  "biggest": biggest, "events": world},
        "baseline": baseline,
    }, QUAKE_FEED


# --------------------------------------------------------------------------
# Raw ground motion, from the seismometer ten miles from the wall.
# helicorder.py draws these as a drum recorder.
#
# quake-usgs above is the *processed* end of this pipeline: events, located,
# with a magnitude on them, after somebody's algorithm has decided they are
# events at all. This is the other end -- the ground going up and down at one
# station, which is the measurement everything else is derived from, and which
# is mostly a flat line with a microseism wobble in it. That is the point of
# drawing it: the quiet is the data too.
#
# **The station is BK.BRK, Byerly Vault on the UC Berkeley campus**, an STS-2
# broadband seismometer 17 km from the wall -- close enough that anything the
# room would feel is emphatic on this trace, and it is a real instrument that a
# person could walk to. BHZ is the 40 sps vertical channel. Berkeley's own
# network is served by NCEDC's FDSN endpoints, keyless, no signup, no quota
# published; the same query shape works for BK.BRIB and BK.BKS if this vault
# ever goes off the air.
#
# **The data is miniSEED with Steim2 compression and there is no ASCII option
# for this network**, which is the one real cost in this product. EarthScope's
# `irisws/timeseries` will hand out ASCII but it does not archive BK, only the
# global networks, and a panel captioned "ground motion near you" showing a
# station in New Mexico would be a lie told to avoid a hundred lines of code.
# So `_steim_decode()` below is that hundred lines. It is checked on every
# record against the reverse integration constant -- Steim2 stores the last
# sample of each record redundantly in the header frame precisely so that a
# decoder can prove it walked the differences correctly -- and a mismatch
# raises rather than storing a plausible wrong wiggle.
#
# **What is stored is an envelope, not a waveform.** Six hours at 40 sps is
# 864,000 samples and 1.7 MB of miniSEED; what the panel can draw is 1800
# columns one pixel wide. Each column is 12 seconds reduced to its minimum and
# its maximum, which is exactly what a helicorder pen does and is the one
# decimation that does not lie about amplitude -- a mean would flatten every
# burst, and a subsample would hit or miss one at random. 3600 numbers, about
# 22 kB of JSON: the whole six hours at the finest resolution the panel can
# actually show.
#
# **It tops up rather than refetching.** The grid is anchored to absolute
# 12-second bins, so a fetch five minutes after the last one asks NCEDC for
# five minutes (23 kB) and slides the previous columns along. A cold start, or
# a gap longer than the window, fetches the whole six hours once. That is the
# difference between 7 MB an hour off the shop wifi and 300 kB.
#
# The baseline is removed before storing: an STS-2 wanders a couple of thousand
# counts over six hours with the temperature and the tide, which at this scale
# is half a trace lane of slow drift that has nothing to do with anything. A
# two-minute box smoothing of each column's midpoint is subtracted, which is
# the modern equivalent of the pen's zero adjustment. Everything above about a
# minute of period goes with it, and nothing a local earthquake does is that
# slow.
#
# FT_HELICORDER_END pins the end of the window to a fixed time, which is how
# the screenshot of a real earthquake in the README was made and how the tests
# get a known six hours. Unset -- always, on the wall -- it means now.
# --------------------------------------------------------------------------

HELI_DATASELECT = "https://service.ncedc.org/fdsnws/dataselect/1/query"
HELI_STATION_URL = "https://service.ncedc.org/fdsnws/station/1/query"

HELI_NET, HELI_STA, HELI_CHA = "BK", "BRK", "BHZ"

# Six lanes of one hour. One hour a lane is the classic drum format and the
# unit a person actually thinks in; six of them is what fits in 54 rows with
# nine rows a lane, which is the least a trace can be and still have a shape.
HELI_SPAN_H = 6
HELI_TRACE_COLS = 300                  # columns per hour, one panel pixel each
HELI_BIN_S = 3600.0 / HELI_TRACE_COLS  # 12 s a column
HELI_COLS = HELI_SPAN_H * HELI_TRACE_COLS

# Half an hour. Past that the panel says STALE and keeps drawing: a drum with
# an honest gap at the right-hand end is still six hours of ground motion, and
# is exactly what the paper would look like if the pen had run dry.
HELI_TTL = 1800
HELI_INTERVAL = 300

# The zero adjustment: the baseline subtracted from each column is a box
# smoothing of the column midpoints this many seconds wide. Two minutes is
# well outside anything a local earthquake does and well inside the thermal
# and tidal wander of a broadband vault.
HELI_BASE_S = 120.0

# The response -- counts per m/s -- changes when somebody recalibrates the
# vault, which is a thing that has happened eight times since 1996 and never
# twice in a day. Refetched daily; carried in the record in between.
HELI_META_MAX_AGE = 86400

HELI_PRODUCT = "helicorder-bk"

# Steim2's seven packings. (nibble, dnib) -> (differences per word, bits each).
# The fields are packed right-aligned against bit 0, which is the one thing in
# the format that is easy to get backwards: for c=1 four 8-bit differences fill
# all 32 bits, but for c=2 and c=3 the top two bits are the dnib and the
# differences live in what is left, so seven 4-bit differences occupy bits 0-27
# and bits 28-29 are simply unused. Shifting down from bit 31 instead decodes
# every quiet record into a plausible-looking wrong number.
_STEIM2_PACK = {
    (1, 0): (4, 8), (1, 1): (4, 8), (1, 2): (4, 8), (1, 3): (4, 8),
    (2, 1): (1, 30), (2, 2): (2, 15), (2, 3): (3, 10),
    (3, 0): (5, 6), (3, 1): (6, 5), (3, 2): (7, 4),
}

# Steim1: same frame structure, three packings, no dnib.
_STEIM1_PACK = dict(((1, d), (4, 8)) for d in range(4))
_STEIM1_PACK.update(dict(((2, d), (2, 16)) for d in range(4)))
_STEIM1_PACK.update(dict(((3, d), (1, 32)) for d in range(4)))


def _steim_decode(payload, nsamples, order=2):
    """Steim1/2 -> samples. Returns (samples, x0, xn); caller checks xn.

    Vectorised rather than looped because this also runs on the wall's Pi, and
    six hours is 864,000 samples: a per-sample Python loop is a minute there
    and about eighty milliseconds like this.

    The shape of the format: 64-byte frames of sixteen big-endian 32-bit words,
    word 0 a map of two bits per following word saying how many differences
    that word holds, and the samples are the running sum of those differences.
    Frame 0's words 1 and 2 are not differences -- they are X0, the first
    sample, and Xn, the last -- and their map nibbles are 0, so the mask
    arithmetic drops them without a special case. The first *difference* in the
    stream is the step from the previous record's last sample and is therefore
    meaningless here, which is why the cumulative sum starts at d[1].
    """
    import numpy as np

    w = np.frombuffer(payload, dtype=">u4")
    nframes = len(w) // 16
    if nframes < 1 or nsamples <= 0:
        return np.zeros(0, np.int64), 0, 0
    w = w[:nframes * 16].reshape(nframes, 16)

    nib = (w[:, :1] >> (30 - 2 * np.arange(1, 16, dtype=np.uint32))) & 3
    body = w[:, 1:]
    dnib = (body >> 30) & 3

    # Every word decodes into at most seven differences; `count` says how many
    # of the seven slots are real, and the boolean take at the end flattens
    # them back into stream order (frame, then word, then position).
    diffs = np.zeros((nframes, 15, 7), np.int32)
    count = np.zeros((nframes, 15), np.int8)
    for key, spec in (_STEIM2_PACK if order == 2 else _STEIM1_PACK).items():
        n, bits = spec
        m = (nib == key[0]) & (dnib == key[1])
        if not m.any():
            continue
        sel = body[m]
        for k in range(n):
            if bits == 32:
                v = sel.astype(np.int64)
            else:
                shift = np.uint32(bits * (n - 1 - k))
                v = ((sel >> shift) & np.uint32((1 << bits) - 1)).astype(np.int64)
            v -= (v >= (1 << (bits - 1))) * (1 << bits)
            diffs[m, k] = v.astype(np.int32)
        count[m] = n

    d = diffs[np.arange(7)[None, None, :] < count[:, :, None]]
    x0 = int(w[0, 1]) - (int(w[0, 1]) >> 31) * (1 << 32)
    xn = int(w[0, 2]) - (int(w[0, 2]) >> 31) * (1 << 32)

    out = np.empty(nsamples, np.int64)
    out[0] = x0
    if nsamples > 1:
        if len(d) < nsamples:
            raise ValueError("steim%d: %d differences for %d samples"
                             % (order, len(d), nsamples))
        np.cumsum(d[1:nsamples].astype(np.int64), out=out[1:])
        out[1:] += x0
    return out, x0, xn


def _mseed_series(data):
    """miniSEED bytes -> ([(start_epoch, samples), ...], sample_rate).

    Fixed 48-byte header, then a chain of blockettes of which the only one that
    matters here is 1000: it carries the encoding and the record length, and
    without it there is no way to know how far the next record is. Records with
    an encoding this cannot read raise rather than being skipped -- a drum with
    silently missing hours is worse than no drum.
    """
    import datetime
    import struct

    import numpy as np

    segs, rate, off, n = [], None, 0, len(data)
    while off + 64 <= n:
        hdr = data[off:off + 48]
        nsamp, = struct.unpack(">H", hdr[30:32])
        factor, mult = struct.unpack(">hh", hdr[32:36])
        nblk = hdr[39]
        data_off, = struct.unpack(">H", hdr[44:46])
        blk_off, = struct.unpack(">H", hdr[46:48])

        reclen, enc, p = None, None, blk_off
        for _ in range(nblk):
            if not p or off + p + 8 > n:
                break
            btype, nxt = struct.unpack(">HH", data[off + p:off + p + 4])
            if btype == 1000:
                enc = data[off + p + 4]
                reclen = 1 << data[off + p + 6]
                break
            p = nxt
        if reclen is None:
            raise ValueError("miniSEED record with no blockette 1000")
        if enc not in (10, 11):
            raise ValueError("miniSEED encoding %r is not Steim1 or Steim2" % enc)

        year, doy, hh, mm, ss, _u, ticks = struct.unpack(">HHBBBBH", hdr[20:30])
        jan1 = datetime.datetime(year, 1, 1,
                                 tzinfo=datetime.timezone.utc).timestamp()
        start = (jan1 + (doy - 1) * 86400.0 + hh * 3600.0 + mm * 60.0
                 + ss + ticks * 1e-4)

        # SEED's two-field sample rate: a positive factor is samples per
        # second, a negative one is seconds per sample, and the multiplier
        # does the same trick again.
        if factor > 0:
            r = float(factor * mult) if mult > 0 else -float(factor) / mult
        elif factor < 0:
            r = -float(mult) / factor if mult > 0 else 1.0 / (factor * mult)
        else:
            r = 0.0
        if r > 0:
            rate = r

        if nsamp:
            s, _x0, xn = _steim_decode(data[off + data_off:off + reclen],
                                       nsamp, 2 if enc == 11 else 1)
            # The whole reason the reverse integration constant exists.
            if int(s[-1]) != xn:
                raise ValueError("steim: record at %d ends %d, header says %d"
                                 % (off, int(s[-1]), xn))
            segs.append((start, np.asarray(s, np.int32)))
        off += reclen
    if not segs or not rate:
        raise ValueError("no decodable miniSEED records")
    return segs, rate


def _heli_iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))


def _heli_window_url(t0, t1):
    from urllib.parse import urlencode
    return HELI_DATASELECT + "?" + urlencode({
        "net": HELI_NET, "sta": HELI_STA, "cha": HELI_CHA,
        "starttime": _heli_iso(t0), "endtime": _heli_iso(t1)})


def _heli_bins(segs, rate, t0, t1):
    """Reduce samples to per-column (min, max). Returns (lo, hi, have).

    One expanded array at the sample rate and then a reshape-and-min, rather
    than np.minimum.at over scattered indices: ufunc.at is roughly a
    microsecond an element, which is a second here on a desktop and half a
    minute on the wall. The expansion costs 7 MB for six hours and is thrown
    away immediately.
    """
    import numpy as np

    ncols = int(round((t1 - t0) / HELI_BIN_S))
    per = int(round(HELI_BIN_S * rate))
    if ncols <= 0 or per <= 0:
        return None
    big = np.iinfo(np.int32).max
    lo_s = np.full(ncols * per, big, np.int32)
    hi_s = np.full(ncols * per, -big, np.int32)
    for start, s in segs:
        i = int(round((start - t0) * rate))
        a, b = max(0, i), min(ncols * per, i + len(s))
        if b <= a:
            continue
        chunk = s[a - i:b - i]
        lo_s[a:b] = chunk
        hi_s[a:b] = chunk
    lo = lo_s.reshape(ncols, per).min(1)
    hi = hi_s.reshape(ncols, per).max(1)
    have = lo != big
    return lo, hi, have


def _heli_centre(lo, hi, have):
    """Subtract a two-minute smoothing of the column midpoints, in place-ish.

    The pen's zero adjustment. Gaps are filled with the median before smoothing
    so that a missing minute does not drag the baseline through the hole and
    bend the trace either side of it.
    """
    import numpy as np

    mid = np.where(have, (lo.astype(np.float64) + hi) * 0.5, np.nan)
    if not have.any():
        return lo.astype(np.int32), hi.astype(np.int32)
    mid = np.where(have, mid, np.nanmedian(mid))
    k = int(HELI_BASE_S / HELI_BIN_S) | 1
    k = min(k, len(mid) if len(mid) % 2 else len(mid) - 1)
    if k >= 3:
        pad = k // 2
        # Edge-padded so the first and last columns get a full window rather
        # than a baseline that tapers towards zero and tips the trace up.
        ext = np.concatenate([np.full(pad, mid[0]), mid, np.full(pad, mid[-1])])
        base = np.convolve(ext, np.full(k, 1.0 / k), mode="valid")
    else:
        base = mid
    return (np.round(lo - base).astype(np.int32),
            np.round(hi - base).astype(np.int32))


def _heli_station_meta(when):
    """Latitude, longitude and counts-per-m/s for the epoch covering `when`.

    The text format is one line per response epoch and the vault has had eight
    of them; picking the current one matters because the sensitivity changed by
    a factor of four in 2010 and every micron on the panel is scaled by it.
    """
    import calendar

    url = (HELI_STATION_URL + "?net=%s&sta=%s&cha=%s&level=channel&format=text"
           % (HELI_NET, HELI_STA, HELI_CHA))
    text = get(url, timeout=30).decode("utf-8", "replace")

    def _epoch(s):
        s = s.strip().split(".")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return calendar.timegm(time.strptime(s, fmt))
            except ValueError:
                continue
        return None

    best = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        f = [c.strip() for c in line.split("|")]
        if len(f) < 17:
            continue
        t0, t1 = _epoch(f[15]), _epoch(f[16])
        if t0 is None or t0 > when or (t1 is not None and t1 <= when):
            continue
        best = {"net": f[0], "sta": f[1], "loc": f[2], "cha": f[3],
                "lat": float(f[4]), "lon": float(f[5]), "elev": float(f[6]),
                "instrument": f[10][:40], "scale": float(f[11]),
                "scale_units": f[13], "rate": float(f[14]),
                "meta_at": time.time()}
    if best is None:
        raise ValueError("no %s.%s.%s response epoch covering %s"
                         % (HELI_NET, HELI_STA, HELI_CHA, _heli_iso(when)))
    return best


def _heli_end():
    """The end of the window. Now, unless FT_HELICORDER_END pins it."""
    s = os.environ.get("FT_HELICORDER_END", "").strip()
    if not s:
        return time.time()
    try:
        return float(s)
    except ValueError:
        pass
    import calendar
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(time.strptime(s, fmt)))
        except ValueError:
            continue
    raise ValueError("cannot read a UTC time out of FT_HELICORDER_END=%r" % s)


@product(HELI_PRODUCT, ttl=HELI_TTL, interval=HELI_INTERVAL,
         description="NCEDC BK.BRK BHZ: six hours of raw vertical ground "
                     "motion, 24 s min/max envelope")
def _helicorder(cache_dir):
    """Top up the six-hour envelope and rewrite it.

    Takes `cache_dir` -- and is therefore flagged as a blob product below --
    for one reason: it reads its own previous record so that a five-minute
    fetch is a five-minute request. It writes no sidecar. The flag is the only
    hook `fetch()` has for "this fetcher needs to know where the cache is".
    """
    import math

    import numpy as np

    end = _heli_end()
    t1 = math.floor(end / HELI_BIN_S) * HELI_BIN_S
    t0 = math.floor(t1 / 3600.0) * 3600.0 - (HELI_SPAN_H - 1) * 3600.0

    lo = np.zeros(HELI_COLS, np.int32)
    hi = np.zeros(HELI_COLS, np.int32)
    have = np.zeros(HELI_COLS, bool)

    prev = load(HELI_PRODUCT, cache_dir)
    meta, filled_to = None, t0
    if prev is not None:
        p = prev[0] or {}
        try:
            shift = int(round((t0 - float(p["t0"])) / HELI_BIN_S))
            if 0 <= shift < HELI_COLS and int(p.get("cols", 0)) == HELI_COLS:
                keep = HELI_COLS - shift
                plo = np.array([0 if v is None else v for v in p["lo"]], np.int32)
                phi = np.array([0 if v is None else v for v in p["hi"]], np.int32)
                phave = np.array([v is not None for v in p["lo"]], bool)
                lo[:keep], hi[:keep] = plo[shift:], phi[shift:]
                have[:keep] = phave[shift:]
                filled_to = max(t0, min(t1, float(p.get("filled_to", t0))))
            m = p.get("station") or {}
            if m.get("scale") and time.time() - float(
                    m.get("meta_at", 0)) < HELI_META_MAX_AGE:
                meta = m
        except Exception:                                    # noqa: BLE001
            # A record from an older layout is not an error, it is a cold
            # start: fall through and fetch the whole window.
            lo[:], hi[:], have[:] = 0, 0, False
            filled_to = t0

    if meta is None:
        meta = _heli_station_meta(t1)

    # The source recorded is what was asked for on this pass, which on a
    # top-up is a few minutes and not the six hours on the panel. When nothing
    # was due the whole window is named instead, because "we asked for zero
    # seconds of data" is a true statement that tells a reader nothing.
    source = (_heli_window_url(filled_to, t1) if t1 - filled_to >= HELI_BIN_S
              else _heli_window_url(t0, t1))
    if t1 - filled_to >= HELI_BIN_S:
        data = get(source, timeout=180 if t1 - filled_to > 3600 else 60)
        segs, rate = _mseed_series(data)
        got = _heli_bins(segs, rate, filled_to, t1)
        if got is not None:
            nlo, nhi, nhave = got
            clo, chi = _heli_centre(nlo, nhi, nhave)
            a = int(round((filled_to - t0) / HELI_BIN_S))
            b = min(HELI_COLS, a + len(clo))
            if b > a:
                lo[a:b], hi[a:b] = clo[:b - a], chi[:b - a]
                have[a:b] = nhave[:b - a]
        filled_to = t1
        meta["rate"] = float(rate)

    p2p = (hi - lo)[have]
    noise = float(np.median(p2p)) if len(p2p) else 0.0
    peak, peak_t = 0.0, None
    if have.any():
        amp = np.maximum(np.abs(lo), np.abs(hi))
        amp = np.where(have, amp, 0)
        i = int(np.argmax(amp))
        peak = float(amp[i])
        peak_t = t0 + (i + 0.5) * HELI_BIN_S

    km, bearing = _quake_km_bearing(meta["lat"], meta["lon"])
    return {
        "station": meta,
        "site": [QUAKE_LAT, QUAKE_LON], "km": round(km, 1),
        "bearing": round(bearing),
        "t0": t0, "t1": t0 + HELI_COLS * HELI_BIN_S, "filled_to": filled_to,
        "bin_s": HELI_BIN_S, "cols": HELI_COLS,
        "trace_cols": HELI_TRACE_COLS, "span_h": HELI_SPAN_H,
        "lo": [None if not h else int(v) for v, h in zip(lo, have)],
        "hi": [None if not h else int(v) for v, h in zip(hi, have)],
        "n_have": int(have.sum()),
        "noise": round(noise, 1), "peak": round(peak, 1), "peak_t": peak_t,
    }, source


# The one thing this flag does in fetch() is pass cache_dir to the fetch
# function; see _helicorder's docstring. No sidecar is written, so there is
# nothing for prune_blobs() to sweep.
PRODUCTS[HELI_PRODUCT]["blob"] = True


# --------------------------------------------------------------------------
# Orbital elements, from CelesTrak's GP service. sats.py propagates these.
#
# This is the slowest-moving product in the file and the fastest-moving demo,
# which is the whole point of it. Everything else here fetches a *number that
# changes* -- a tide height, a K index, a wind field -- and the panel is only as
# alive as the fetcher. These are elements: they describe an orbit rather than a
# position, they are revised about once a day, and the demo turns them into a
# position by knowing what time it is. So `sats.py` moves continuously, forever,
# on a cache record that is three days old and still perfectly good.
#
# Hence ttl=3 days and interval=86400. Fetching this every quarter hour would be
# 96 requests a day at CelesTrak to receive the same file 95 times; the service
# is free, keyless and asks politely for exactly this restraint. Not volatile:
# a record worth three days is emphatically worth surviving a reboot, and one
# write a day is nothing on any flash card.
#
# **Three group queries, not fifteen object queries.** `gp.php?CATNR=25544`
# works and would fetch precisely what is wanted, but fifteen of them is fifteen
# requests for 8 kB of data that three requests already contain. GROUP=stations
# is 9 kB, GROUP=amateur 40 kB and GROUP=weather 30 kB; the union is parsed,
# fifteen objects are picked out of it by NORAD number and the other 180 are
# dropped. What is stored is 2 kB.
#
# **GROUP=noaa no longer exists.** The obvious pick for a ham-adjacent wall is
# NOAA 15/18/19, the APT birds a $20 dongle can hear -- and CelesTrak answers
# `GROUP=noaa not found` now, with those three gone from GROUP=weather too,
# because NOAA ended POES operations in 2025 and the group went with them. The
# polar weather birds here are their successors: NOAA-20 and NOAA-21 (JPSS,
# HRD not APT), MetOp-B and Meteor-M2 3, which is the one still transmitting
# LRPT that anybody in the shop could actually receive.
#
# **The payload is the seven mean elements and nothing else**, because that is
# what the propagator in sats.py consumes. BSTAR is dropped: it is the SGP4 drag
# term, sats.py does not implement SGP4, and storing a number the demo cannot
# honour would invite somebody to assume it does. MEAN_MOTION_DOT is kept and is
# used -- it is the TLE's n-dot/2 in rev/day^2, and the quadratic term it feeds
# into the mean anomaly is the one piece of drag a Kepler propagator can carry.
#
# Times are epoch seconds, as everywhere else here. The EPOCH field is an ISO
# stamp in UTC with no offset on it and microseconds that matter -- a second of
# epoch error is 7 km along track for the ISS -- so it is parsed rather than
# truncated.
# --------------------------------------------------------------------------

CELESTRAK_GP = "https://celestrak.org/NORAD/elements/gp.php"

# The groups worth one request each, and what a satellite drawn from each is
# called on the panel. The kind rides into the payload because the demo colours
# by it: stations white, amateur green, weather amber.
SAT_GROUPS = (("stations", "station"), ("amateur", "amateur"),
              ("weather", "weather"))

# The roster. NORAD number, the short label the panel has room for, and a note
# on why it earns one of fifteen places on a 320 px map. Deliberately modest:
# the amateur group alone is 97 objects and forty Russian cubesats in one
# sun-synchronous plane draw as a single smear.
SAT_ROSTER = (
    (25544, "ISS",     "the one everybody looks for; 51.6 deg, 90 min"),
    (48274, "CSS",     "Tiangong, the other crewed station, 41.5 deg"),
    (7530,  "AO-7",    "launched 1974 and still worked today, the oldest"),
    (22825, "AO-27",   "FM, still up after thirty years"),
    (24278, "FO-29",   "JAS-2, linear transponder, a classic"),
    (27607, "SO-50",   "the FM bird most first contacts are made on"),
    (39444, "AO-73",   "FUNcube-1, linear plus a telemetry beacon"),
    (40967, "AO-85",   "Fox-1A, 64.8 deg so it fills in the mid latitudes"),
    (44909, "RS-44",   "linear, high and slow, long passes"),
    (53109, "IO-117",  "GreenCube: a digipeater at 5900 km, MEO not LEO"),
    (43700, "QO-100",  "Es'hail-2: geostationary, so it never moves at all"),
    (43013, "NOAA-20", "JPSS-1, sun-synchronous polar"),
    (54234, "NOAA-21", "JPSS-2, the same plane half an orbit apart"),
    (38771, "METOP-B", "EUMETSAT polar, the European half of the pair"),
    (57166, "METEOR",  "Meteor-M2 3, still sending LRPT you can receive"),
)

SATS_TTL = 3 * 86400
SATS_INTERVAL = 86400


def _gp_epoch(s):
    """'2026-08-08T22:57:12.255840' in UTC -> epoch seconds.

    The fractional part is kept. It looks like noise next to a three-day TTL,
    but epoch is the origin the whole propagation hangs off: a second of error
    puts the ISS 7.7 km along its track, which is two pixels on this map and
    rather more than the propagator's own accuracy budget.
    """
    import calendar
    head, _, frac = str(s).partition(".")
    base = float(calendar.timegm(time.strptime(head, "%Y-%m-%dT%H:%M:%S")))
    return base + (float("0." + frac) if frac.isdigit() else 0.0)


def _gp_url(group):
    from urllib.parse import urlencode
    return CELESTRAK_GP + "?" + urlencode({"GROUP": group, "FORMAT": "json"})


@product("sats", ttl=SATS_TTL, interval=SATS_INTERVAL,
         description="CelesTrak GP elements for %d satellites" % len(SAT_ROSTER))
def _sats():
    """Mean elements for the roster, out of three CelesTrak group queries.

    A group that fails is skipped rather than fatal: the amateur file being
    unreachable should cost the panel its amateur birds for a day, not the ISS.
    The product only fails outright if nothing at all was found, since an empty
    roster would leave sats.py drawing an empty map with no explanation.
    """
    wanted = dict((cat, (label, note)) for cat, label, note in SAT_ROSTER)
    found = {}
    kinds = {}
    sources = []
    errors = []
    for group, kind in SAT_GROUPS:
        url = _gp_url(group)
        try:
            rows = get_json(url, timeout=30)
        except Exception as e:                                # noqa: BLE001
            errors.append("%s: %r" % (group, e))
            continue
        sources.append(url)
        for rec in rows if isinstance(rows, list) else [rows]:
            cat = rec.get("NORAD_CAT_ID")
            # Keep the first group a satellite turns up in: the ISS is in both
            # stations and amateur, and it is a station with a ham radio on it
            # rather than an amateur satellite, which is also how it is coloured.
            if cat in wanted and cat not in found:
                found[cat] = rec
                kinds[cat] = kind

    if not found:
        raise ValueError("no roster satellites in any CelesTrak group (%s)"
                         % "; ".join(errors) if errors else "empty response")

    sats = []
    for cat, label, _note in SAT_ROSTER:
        rec = found.get(cat)
        if rec is None:
            continue
        sats.append({
            "id": int(cat), "label": label, "kind": kinds[cat],
            "name": str(rec.get("OBJECT_NAME") or label),
            "epoch": _gp_epoch(rec["EPOCH"]),
            # rev/day, and rev/day^2 for the TLE's n-dot/2 field.
            "n": float(rec["MEAN_MOTION"]),
            "ndot2": float(rec.get("MEAN_MOTION_DOT") or 0.0),
            "e": float(rec["ECCENTRICITY"]),
            # Degrees, as the GP set gives them; sats.py converts once.
            "i": float(rec["INCLINATION"]),
            "raan": float(rec["RA_OF_ASC_NODE"]),
            "argp": float(rec["ARG_OF_PERICENTER"]),
            "ma": float(rec["MEAN_ANOMALY"]),
        })

    epochs = [s["epoch"] for s in sats]
    return {
        "sats": sats, "count": len(sats), "wanted": len(SAT_ROSTER),
        "missing": [label for cat, label, _ in SAT_ROSTER if cat not in found],
        # The oldest element set in the record, which is the age that actually
        # bounds the propagation -- not the age of the fetch, which only says
        # when we last asked. A group that 404s for a week leaves fresh-looking
        # records full of week-old elements, and this is how the panel notices.
        "epoch_oldest": min(epochs), "epoch_newest": max(epochs),
        "errors": errors,
        "units": {"n": "rev/day", "ndot2": "rev/day^2", "angles": "deg",
                  "epoch": "epoch seconds UTC"},
    }, sources[0] if sources else CELESTRAK_GP


# --------------------------------------------------------------------------
# An hour of solar wind in flight between L1 and the Earth. solarwind.py draws
# it as a picture rather than a readout: the stream across the panel, the
# interplanetary field carried in it, and the magnetosphere it runs into.
#
# **Why the geospace file and not the plasma/mag pair.** The obvious endpoints
# are `/products/solar-wind/plasma-*.json` and `mag-*.json`, and every tutorial
# on the internet still names them. They 404. So does the whole
# `/products/solar-wind/` directory -- the same disappearance that made
# swpc_solarwind above fall back to the two sixty-byte summary files. What is
# still served, and is far better than either, is
# `/products/geospace/propagated-solar-wind-1-hour.json`: one 6.6 kB table,
# one row a minute for the last hour, with speed, density, temperature, the
# full field vector in GSM, |B|, and -- the part that matters here -- a
# `propagated_time_tag` saying when each sample will actually reach the bow
# shock. It is the input to SWPC's own geospace model, so it is the tidiest
# and least gap-ridden solar wind series they publish.
#
# **The window is the transit time, which is a gift.** A sample measured at L1
# reaches the Earth roughly forty-five to fifty minutes later at typical wind
# speeds, and the file holds sixty minutes. So the file is, almost exactly, the
# plasma currently in flight: its oldest row is arriving at the magnetosphere
# about now and its newest row has just been measured a million and a half
# kilometres upstream. A panel that lays the rows out left to right is
# therefore not a chart with a time axis pretending to be a picture -- it is a
# picture, of where the plasma is. That coincidence is the whole reason
# solarwind.py exists in the shape it does, and it is why the arrival stamps
# are kept in the record rather than trimmed away: the demo prints how far
# ahead the leading edge is.
#
# The rows carry nulls when an instrument drops out, and they are stored as
# nulls rather than filled here. The gap is real information -- three minutes
# of missing density during a shock arrival is exactly when it happens -- and
# the demo can draw a hole more honestly than the fetcher can invent a value.
#
# The aurora power rides along because it is 14 kB of text and the alternative
# is not. `ovation_aurora_latest.json` is the gridded oval, and it is 900 kB of
# JSON, every fetch, to be reduced to two numbers by the time it reaches a
# panel where the Earth is five pixels across. `aurora-nowcast-hemi-power.txt`
# is the same model's hemispheric integral, in gigawatts, at five-minute
# cadence, and two numbers is all this panel can draw. Ten quiet gigawatts, a
# hundred in a storm.
# --------------------------------------------------------------------------

BGP_ARCHIVE = "https://archive.routeviews.org"

# The collector, and the words for where it is. Overridable because somebody
# forking this wall for another city should not have to edit code to move the
# vantage point, and every RouteViews collector publishes the identical layout.
BGP_COLLECTOR = os.environ.get("FT_BGP_COLLECTOR", "route-views.sfmix")
BGP_SITE = os.environ.get("FT_BGP_SITE", "SFMIX SAN FRANCISCO")

# RouteViews rolls an updates file every fifteen minutes and publishes it a
# minute or so after the window closes. Asking on the same cadence is exactly
# right; asking faster only re-downloads a file we already have.
BGP_INTERVAL = 900

# Three quarters of an hour. Two missed windows and the panel should start
# saying so: churn is the one thing here that genuinely does not keep, and a
# fifteen-minute picture of the routing table from two hours ago is a picture
# of an event that is over.
BGP_TTL = 2700

# How many fifteen-minute slots back to look for the newest published file.
# Six is an hour and a half, which covers the publisher having a bad afternoon
# without turning a fetch into a crawl of the archive.
BGP_LOOKBACK = 6

# Caps, in the order they bite. The compressed file is normally 1.0-1.6 MB and
# the decompressed MRT 10-16 MB; these are roughly five times that, so they
# never fire in normal operation and do fire on the day some collector emits a
# pathological window. A capped fetch is still a usable fetch -- the record
# carries the span actually parsed and the rates are computed against it, not
# against the fifteen minutes it was supposed to be.
BGP_MAX_BZ2 = 8 << 20
BGP_MAX_MRT = 64 << 20
BGP_MAX_RECORDS = 250000

# Time resolution kept in the record. Two seconds over a nine-hundred second
# window is 450 numbers a series, which is more columns than the panel has and
# small enough to sit in a JSON record without apology. Per-second would be
# 900 and would let the demo redraw at a resolution no 320-pixel panel can
# show; the binning is done here so the demo never has to know it happened.
BGP_BIN_SECS = 2

# Lines the ticker can draw. Reservoir-sampled across the whole window rather
# than taken from the front of it, because the front of a fifteen-minute window
# is frequently one router dumping its table and forty lines of the same peer
# is not what the routing table looks like.
BGP_SAMPLES = 48

# Origin ASNs kept, by how many prefixes each announced. The tail is thousands
# long and the head is the story.
BGP_ORIGINS = 12


def _bgp_slot(epoch):
    """(url, filename) for the fifteen-minute window starting at `epoch`."""
    lt = time.gmtime(epoch)
    name = "updates.%s.bz2" % time.strftime("%Y%m%d.%H%M", lt)
    return ("%s/%s/bgpdata/%s/UPDATES/%s"
            % (BGP_ARCHIVE, BGP_COLLECTOR, time.strftime("%Y.%m", lt), name),
            name)


def _bgp_newest(now=None, lookback=BGP_LOOKBACK):
    """Find the newest published updates file. Returns (url, name, size).

    Walks back a slot at a time from the present and HEADs each candidate. The
    filenames are derived from the clock rather than from the directory
    listing, which is a month of two thousand eight hundred links and would be
    a bigger download than half the products in this file.
    """
    import urllib.error
    import urllib.request
    now = time.time() if now is None else now
    slot = int(now // BGP_INTERVAL) * BGP_INTERVAL
    last = None
    for k in range(1, lookback + 1):
        url, name = _bgp_slot(slot - k * BGP_INTERVAL)
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "flaschen-taschen-ftdata/1 (+wall display)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                size = int(resp.headers.get("Content-Length") or 0)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue                 # not published yet; try the one before
            raise
        if size > BGP_MAX_BZ2:
            # Refusing is better than truncating here: a bz2 stream cut mid
            # block does not decompress, so a partial download of an oversized
            # file buys nothing at all.
            last = "%s is %d bytes, over the %d cap" % (name, size, BGP_MAX_BZ2)
            continue
        return url, name, size
    raise RuntimeError(last or "no updates file published in the last %d slots"
                       % lookback)


def _bgp_fetch_mrt(url):
    """Download and decompress one updates file, both ends capped."""
    import bz2
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "flaschen-taschen-ftdata/1 (+wall display)"})
    dec = bz2.BZ2Decompressor()
    chunks = []
    raw = out = 0
    with urllib.request.urlopen(req, timeout=60) as resp:
        while True:
            block = resp.read(1 << 16)
            if not block:
                break
            raw += len(block)
            if raw > BGP_MAX_BZ2:
                raise RuntimeError("compressed stream ran past the %d cap"
                                   % BGP_MAX_BZ2)
            piece = dec.decompress(block)
            if piece:
                out += len(piece)
                if out > BGP_MAX_MRT:
                    # Stop cleanly rather than raise: whole MRT records already
                    # decoded are perfectly good data, and the parse below finds
                    # the end of the last complete one by itself.
                    chunks.append(piece)
                    break
                chunks.append(piece)
    return b"".join(chunks), raw


def _bgp_prefixes(buf, i, end, v6=False, want_first=False):
    """Walk an RFC 4271 NLRI block; return (count, first_prefix_or_None).

    One length-in-bits byte then that many bits rounded up to whole octets,
    with the trailing zero octets of the prefix left off the wire entirely --
    which is why the bytes are padded back out before being formatted. The
    same encoding carries v4 and v6; only the width of the padding differs.
    """
    n = 0
    first = None
    width = 16 if v6 else 4
    while i < end:
        bits = buf[i]
        i += 1
        nb = (bits + 7) >> 3
        if nb > width or i + nb > end:
            # Malformed, or a SAFI whose NLRI is not a plain prefix (labelled
            # unicast and VPN routes both prepend fields here). Either way the
            # rest of this block cannot be walked, and guessing would corrupt
            # the count far more than stopping does.
            break
        if want_first and first is None:
            pad = buf[i:i + nb] + b"\0" * (width - nb)
            if v6:
                import socket
                first = "%s/%d" % (socket.inet_ntop(socket.AF_INET6, pad), bits)
            else:
                first = "%d.%d.%d.%d/%d" % (pad[0], pad[1], pad[2], pad[3], bits)
        i += nb
        n += 1
    return n, first


def _bgp_as_path(buf, i, end):
    """Decode an AS_PATH attribute body into a list of four-byte ASNs.

    Segments are (type, count, ASNs); AS_SET and AS_SEQUENCE are flattened
    together because the distinction does not survive being drawn four pixels
    high, and the four-byte width is safe because every RouteViews collector
    has spoken RFC 6793 since long before it started writing these files.
    """
    out = []
    from struct import unpack_from
    while i + 2 <= end:
        count = buf[i + 1]
        i += 2
        for k in range(count):
            if i + 4 > end:
                return out
            out.append(unpack_from(">I", buf, i)[0])
            i += 4
    return out


def _bgp_parse(data):
    """Count churn out of a raw MRT stream. Everything happens in one pass.

    Returns the counters bgp.py needs and nothing else -- twelve megabytes in,
    about nine kilobytes out. The expensive part is deliberately not done for
    every record: the AS_PATH's byte range is noted while walking the
    attributes, which is free, and it is only decoded into a list of integers
    for the few dozen records the ticker reservoir keeps.
    """
    import random
    from struct import Struct
    hdr = Struct(">IHHI").unpack_from
    u16 = Struct(">H").unpack_from
    u32 = Struct(">I").unpack_from

    # MRT types and subtypes, RFC 6396 s4.4. Only BGP4MP is ever in an updates
    # file; the _ET flavour is identical but for four bytes of microseconds
    # ahead of the body, which is why it is a `+= 4` and not a second parser.
    BGP4MP, BGP4MP_ET = 16, 17
    AS4_SUBTYPES = (4, 7)               # MESSAGE_AS4, MESSAGE_AS4_LOCAL
    AS2_SUBTYPES = (1, 6)               # MESSAGE, MESSAGE_LOCAL

    n = len(data)
    i = 0
    records = 0
    truncated = False
    ann = wdr = 0
    t_first = t_last = None
    ann_sec = {}
    wdr_sec = {}
    peers = {}
    origins = {}
    reservoir = []
    seen_lines = 0
    rng = random.Random()

    while i + 12 <= n:
        if records >= BGP_MAX_RECORDS:
            truncated = True
            break
        ts, mtype, sub, length = hdr(data, i)
        i += 12
        end = i + length
        if end > n:
            # A short final record, which is what the decompression cap leaves
            # behind. Everything before it is intact.
            truncated = True
            break
        j = i
        i = end
        if mtype == BGP4MP_ET:
            j += 4
        elif mtype != BGP4MP:
            continue
        if sub in AS4_SUBTYPES:
            if j + 8 > end:
                continue
            peer_as = u32(data, j)[0]
            j += 8
        elif sub in AS2_SUBTYPES:
            if j + 4 > end:
                continue
            peer_as = u16(data, j)[0]
            j += 4
        else:
            continue                     # a state change, not a message
        j += 2                           # interface index
        if j + 2 > end:
            continue
        afi = u16(data, j)[0]
        j += 2
        j += 2 * (4 if afi == 1 else 16)         # peer and local addresses

        # The BGP message itself: sixteen marker bytes, a length and a type.
        if j + 19 > end:
            continue
        if data[j + 18] != 2:            # not an UPDATE (OPEN, KEEPALIVE, ...)
            continue
        msg_end = min(j + u16(data, j + 16)[0], end)
        k = j + 19
        if k + 2 > msg_end:
            continue
        wlen = u16(data, k)[0]
        k += 2
        nw, w_first = _bgp_prefixes(data, k, min(k + wlen, msg_end),
                                    want_first=True)
        k += wlen
        if k + 2 > msg_end:
            continue
        alen = u16(data, k)[0]
        k += 2
        attr_end = min(k + alen, msg_end)

        path_span = None
        v6_ann = v6_wdr = 0
        v6_first = None
        p = k
        while p + 3 <= attr_end:
            flags = data[p]
            atype = data[p + 1]
            if flags & 0x10:             # extended length
                if p + 4 > attr_end:
                    break
                blen = u16(data, p + 2)[0]
                p += 4
            else:
                blen = data[p + 2]
                p += 3
            aend = min(p + blen, attr_end)
            if atype == 2:                                   # AS_PATH
                path_span = (p, aend)
            elif atype == 14 and p + 4 <= aend:              # MP_REACH_NLRI
                mafi = u16(data, p)[0]
                nh = data[p + 3]
                q = p + 4 + nh + 1       # next hop, then one reserved octet
                c, f = _bgp_prefixes(data, q, aend, v6=(mafi == 2),
                                     want_first=(mafi == 2))
                v6_ann += c
                if v6_first is None:
                    v6_first = f
            elif atype == 15 and p + 3 <= aend:              # MP_UNREACH_NLRI
                mafi = u16(data, p)[0]
                c, f = _bgp_prefixes(data, p + 3, aend, v6=(mafi == 2),
                                     want_first=(mafi == 2))
                v6_wdr += c
                if w_first is None:
                    w_first = f
            p = aend

        na, a_first = _bgp_prefixes(data, attr_end, msg_end, want_first=True)
        if a_first is None:
            a_first = v6_first
        na += v6_ann
        nw += v6_wdr
        if not (na or nw):
            continue                     # a pure keepalive-ish UPDATE

        records += 1
        ann += na
        wdr += nw
        if t_first is None:
            t_first = ts
        t_last = ts
        if na:
            ann_sec[ts] = ann_sec.get(ts, 0) + na
        if nw:
            wdr_sec[ts] = wdr_sec.get(ts, 0) + nw
        peers[peer_as] = peers.get(peer_as, 0) + 1

        origin = None
        if path_span is not None and na:
            # The origin only needs the last ASN of the last segment, so this
            # walks the segment headers rather than decoding every hop -- the
            # difference over seventy-five thousand records is most of the
            # parse. The full path is decoded below, for the ticker only.
            q, qe = path_span
            while q + 2 <= qe:
                count = data[q + 1]
                q += 2
                if count and q + 4 * count <= qe:
                    origin = u32(data, q + 4 * (count - 1))[0]
                q += 4 * count
            if origin is not None:
                origins[origin] = origins.get(origin, 0) + na

        # Reservoir sampling, so the ticker is a fair draw from the whole
        # window instead of the first forty-eight lines of it. Both kinds of
        # line go in the same reservoir on purpose: withdrawals are two per
        # cent of the traffic and they should be two per cent of the ticker,
        # because the panel's whole claim is that these numbers are real.
        for kind, pfx, npfx in (("A", a_first, na), ("W", w_first, nw)):
            if not pfx:
                continue
            seen_lines += 1
            if len(reservoir) < BGP_SAMPLES:
                slot = len(reservoir)
                reservoir.append(None)
            else:
                slot = rng.randrange(seen_lines)
                if slot >= BGP_SAMPLES:
                    continue
            path = []
            if path_span is not None:
                path = _bgp_as_path(data, path_span[0], path_span[1])
            reservoir[slot] = {
                "k": kind, "p": pfx, "n": npfx, "peer": peer_as,
                "o": (path[-1] if path else None), "path": path[-6:],
                "t": ts,
            }

    if t_first is None:
        raise RuntimeError("no BGP UPDATEs in %d bytes of MRT" % n)

    # Bin to a fixed grid anchored on the window's first second, so the demo
    # can index it with arithmetic rather than searching timestamps.
    span = max(1, t_last - t_first + 1)
    nbins = -(-span // BGP_BIN_SECS)
    ann_bins = [0] * nbins
    wdr_bins = [0] * nbins
    for src, dst in ((ann_sec, ann_bins), (wdr_sec, wdr_bins)):
        for ts, v in src.items():
            dst[(ts - t_first) // BGP_BIN_SECS] += v

    peak_at, peak = t_first, 0
    for ts in set(ann_sec) | set(wdr_sec):
        v = ann_sec.get(ts, 0) + wdr_sec.get(ts, 0)
        if v > peak:
            peak, peak_at = v, ts

    top = sorted(origins.items(), key=lambda kv: -kv[1])[:BGP_ORIGINS]
    reservoir = [s for s in reservoir if s]
    reservoir.sort(key=lambda s: s["t"])
    return {
        "t0": t_first, "t1": t_last, "secs": span,
        "records": records, "truncated": truncated,
        "ann": ann, "wdr": wdr,
        "ann_s": round(ann / float(span), 2), "wdr_s": round(wdr / float(span), 2),
        "peak": peak, "peak_at": peak_at,
        "bin_secs": BGP_BIN_SECS, "ann_bins": ann_bins, "wdr_bins": wdr_bins,
        "peers": sorted(peers.items(), key=lambda kv: -kv[1]),
        "n_peers": len(peers),
        "origins": [[a, c] for a, c in top], "n_origins": len(origins),
        "samples": reservoir,
    }


@product("bgp-sfmix", ttl=BGP_TTL, interval=BGP_INTERVAL,
         description="BGP churn at %s (RouteViews %s)"
                     % (BGP_SITE, BGP_COLLECTOR))
def _bgp_sfmix():
    """One complete fifteen-minute MRT window from the SFMIX collector."""
    url, name, size = _bgp_newest()
    data, raw = _bgp_fetch_mrt(url)
    payload = _bgp_parse(data)
    payload.update({
        "collector": BGP_COLLECTOR, "site": BGP_SITE,
        "file": name, "bytes": raw, "mrt_bytes": len(data),
        "units": {"t0": "epoch seconds UTC", "ann_s": "prefixes/second",
                  "bins": "prefixes per bin_secs seconds",
                  "ann": "prefixes announced in the window",
                  "wdr": "prefixes withdrawn in the window"},
    })
    return payload, url


# --------------------------------------------------------------------------
# Ship movements at the Port of San Francisco, from the Port's own cruise
# terminal schedule. ships.py draws them against the Golden Gate tide.
#
# **Why a schedule and not AIS.** The obvious source for "what is moving in the
# Bay" is AIS, and every AIS feed within reach of this project wants a key:
# aisstream.io, MarineTraffic and VesselFinder all register you first, and
# AISHub's price is a receiver of your own feeding the pool. The Marine
# Exchange of the San Francisco Bay Region does publish exactly the report this
# demo would want -- due to arrive, due to depart, vessels in port, updated
# around the clock -- and sells it to members; sfmx.org has the sample PDFs up
# and the live ones behind the membership. So there is no keyless live-position
# feed for this bay, and a wall in a workshop is not going to invent one.
#
# What *is* public, free and authoritative is the Port's own cruise terminal
# schedule: a PDF calendar of every cruise call at Piers 27 and 35 for the year,
# with the vessel, the berth, the line, the ETA and ETD to the minute and the
# port either side. Cruise ships are the largest vessels that come through the
# Gate on a published timetable, which makes them the ones this panel can say
# something true about.
#
# **A caveat that belongs in the record and not just in the demo.** These are
# *berth* times at Pier 27 or 35, not Golden Gate transit times. A ship
# alongside at 07:00 passed under the bridge the better part of an hour
# earlier. Nothing here converts between the two, because the conversion
# depends on the pilot, the ship and the day, and a made-up offset drawn to the
# minute would look exactly as authoritative as the published number beside it.
#
# **Scraping, defensively.** The current PDF's URL carries the revision date, so
# it changes every few weeks and cannot be hardcoded; the fetch reads the
# Port's cruise page and takes the links off it. The PDF itself is parsed here
# rather than by a library, because there is no PDF module in this project's
# dependencies and adding one to a Pi for eleven columns of a table is a poor
# trade. The parse is positional: inflate the content streams, recover the
# (x, y) of every text run, group runs into rows by y, and assign each cell to
# the nearest column of the *header row it found in the document*. That last
# part is what makes it survive a layout edit -- the columns are read from the
# page, not from a table of offsets in this file. When it does eventually break
# it raises, and `fetch()` leaves the previous record alone.
# --------------------------------------------------------------------------

# Both are standard library and neither opens anything, so they sit at module
# level with the regexes that need them rather than being imported per call --
# unlike urllib above, which is deferred to keep `load()` provably offline.
import re                                                    # noqa: E402
import zlib                                                  # noqa: E402

SFPORT_CRUISE_PAGE = "https://www.sfport.com/maritime/cruise"

# A week. The payload is a year-long calendar, so like the tide predictions it
# keeps telling the truth long after it was fetched -- what expires is not the
# data but our confidence that we are looking at the current revision. The Port
# reissues the sheet every few weeks, so a week of failed fetches is where
# "probably still right" stops being good enough to draw without a warning.
SFPORT_CRUISE_TTL = 604800

# Six hours. There is nothing to gain from asking more often: the file changes
# a handful of times a quarter, and it is a quarter megabyte a time. Four
# passes a day still puts a revision on the wall the same day it is published,
# and keeps this off the fifteen-minute timer where it would be pure waste.
SFPORT_CRUISE_INTERVAL = 21600

# How much of the calendar to keep. A whole year of calls is only about twenty
# kilobytes of JSON, but there is no reason to carry last January around, and
# the demo never looks further ahead than the tide predictions reach anyway.
SFPORT_KEEP_PAST = 7 * 86400
SFPORT_KEEP_AHEAD = 200 * 86400

_PDF_OBJ = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.S)
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_PDF_CONTENTS = re.compile(rb"/Contents\s*(?:(\d+)\s+\d+\s+R|\[([^\]]*)\])")
_PDF_REF = re.compile(rb"(\d+)\s+\d+\s+R")

# The text operators, and the three ways the current point moves between them.
# Tm sets it outright, Td/TD shift it, T* drops a line; this document uses only
# the first, but a reissue made by a different tool will use the others and
# tracking all three costs one regex alternation.
_PDF_TOKEN = re.compile(
    rb"(?P<tm>(?:[-+0-9.]+\s+){6})Tm"
    rb"|(?P<td>(?:[-+0-9.]+\s+){2})T[dD]"
    rb"|(?P<star>T\*)"
    rb"|(?P<arr>\[(?:[^\[\]\\]|\\.)*\])\s*TJ"
    rb"|(?P<lit>\((?:[^()\\]|\\.)*\))\s*Tj"
    rb"|(?P<bt>BT)")

_PDF_ESCAPE = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
               b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}


def _pdf_unescape(b):
    out = bytearray()
    i = 0
    while i < len(b):
        if b[i:i + 1] != b"\\":
            out += b[i:i + 1]
            i += 1
            continue
        nxt = b[i + 1:i + 2]
        if nxt in _PDF_ESCAPE:
            out += _PDF_ESCAPE[nxt]
            i += 2
        elif nxt.isdigit():
            j = i + 1
            while j < len(b) and j < i + 4 and b[j:j + 1].isdigit():
                j += 1
            out += bytes([int(b[i + 1:j], 8) & 0xFF])
            i = j
        else:
            i += 2
    return out


def _pdf_show(operand):
    """The visible characters of a Tj operand or a TJ array.

    A TJ array is strings interleaved with kerning numbers -- `[(Ru)11(by)]` --
    and the numbers are what make a word arrive in four pieces. Concatenating
    the literals and dropping the kerning is exactly right for reading a table:
    the pieces of one word are always in one array.
    """
    out = bytearray()
    depth = start = 0
    i = 0
    while i < len(operand):
        c = operand[i:i + 1]
        if c == b"\\" and depth:
            i += 2
            continue
        if c == b"(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                out += _pdf_unescape(operand[start:i])
        i += 1
    return out.decode("latin-1")


def _pdf_pages(raw):
    """Decoded content bytes, one entry per page of the document.

    Per *page* and not per stream, which matters more than it sounds: a page's
    content is often split across several streams, and one table row can land
    either side of the split. Grouping text by stream tears those rows in half
    -- the first half of this parser did exactly that and quietly lost a column
    off five calls -- so the page tree is walked and each page's streams are
    concatenated before anything looks at coordinates.
    """
    objs = {}
    for m in _PDF_OBJ.finditer(raw):
        objs[int(m.group(1))] = m.group(2)

    def inflate(body):
        m = _PDF_STREAM.search(body)
        if not m:
            return None
        try:
            return zlib.decompress(m.group(1))
        except zlib.error:
            return m.group(1)                # an uncompressed content stream

    out = []
    for num in sorted(objs):
        head = objs[num].split(b"stream", 1)[0]
        if not re.search(rb"/Type\s*/Page\b", head):
            continue
        m = _PDF_CONTENTS.search(head)
        if not m:
            continue
        refs = ([int(m.group(1))] if m.group(1)
                else [int(r) for r in _PDF_REF.findall(m.group(2))])
        chunks = [c for c in (inflate(objs[r]) for r in refs if r in objs) if c]
        if chunks:
            out.append(b"\n".join(chunks))
    if not out:
        raise ValueError("no page content streams in PDF")
    return out


def _pdf_rows(raw, tol=3.0):
    """[(page, y, [(x, text), ...])] with the cells of each row left to right.

    `tol` is in PDF units against a row pitch of about 19, so it is loose
    enough for the half-point baseline wobble a word processor leaves behind
    and nowhere near loose enough to merge two rows.
    """
    rows = []
    for page, content in enumerate(_pdf_pages(raw)):
        x = y = 0.0
        here = []
        for m in _PDF_TOKEN.finditer(content):
            if m.group("tm"):
                n = m.group("tm").split()
                x, y = float(n[4]), float(n[5])
            elif m.group("td"):
                n = m.group("td").split()
                x += float(n[0])
                y += float(n[1])
            elif m.group("star"):
                y -= 11.0
            elif m.group("bt"):
                x = y = 0.0
            else:
                s = _pdf_show(m.group("arr") or m.group("lit")).strip()
                if s:
                    for row in here:
                        if abs(row[0] - y) <= tol:
                            row[1].append((x, s))
                            break
                    else:
                        here.append((y, [(x, s)]))
        here.sort(key=lambda r: -r[0])       # PDF y grows upwards; reading order
        for y_, cells in here:
            cells.sort()
            rows.append((page, y_, cells))
    return rows


# The columns worth having. "ETA Day"/"ETD Day" are the weekday spelled out,
# which the date already says, and "No."/"Port Agent" are the Port's own
# bookkeeping.
_SFPORT_COLUMNS = ("Vessel", "ETA Date", "Arrival Time", "Last Port",
                   "ETD Date", "Departure Time", "Next Port", "Berth",
                   "Cruise Line", "Type")

_SFPORT_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}

_SFPORT_DATE = re.compile(r"([A-Za-z]{3})-(\d{1,2})-(\d{4})")
_SFPORT_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([AP])", re.I)
_SFPORT_REVISED = re.compile(r"updated\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)
_SFPORT_LINK = re.compile(
    rb'href="([^"]*cruise[_%20\-]*schedule[^"]*\.pdf)"', re.I)


def _sfport_epoch(date_s, time_s):
    """A published date and clock time -> epoch seconds, or None.

    The sheet is written in San Francisco for ships arriving in San Francisco,
    so the times on it are Pacific wall clock with no offset attached -- which
    is fine until the fetcher runs somewhere else, and a container or a cloud
    box is UTC by default. So the zone is named rather than assumed. If the
    system has no tz database to name it with, local time is the fallback,
    which is right on the Pi this ships to and wrong by hours nowhere that
    matters; either way it is one conversion, here, and everything downstream
    is epoch seconds like the rest of this file.
    """
    md = _SFPORT_DATE.search(date_s or "")
    if not md:
        return None
    mon = _SFPORT_MONTHS.get(md.group(1).upper())
    if not mon:
        return None
    day, year = int(md.group(2)), int(md.group(3))
    hour = minute = 0
    mt = _SFPORT_TIME.search(time_s or "")
    if mt:
        hour = int(mt.group(1)) % 12
        minute = int(mt.group(2))
        if mt.group(3).upper() == "P":
            hour += 12
    try:
        import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.datetime(year, mon, day, hour, minute,
                               tzinfo=ZoneInfo("America/Los_Angeles"))
        return float(dt.timestamp())
    except Exception:                                        # noqa: BLE001
        return float(time.mktime((year, mon, day, hour, minute, 0, 0, 0, -1)))


def _sfport_parse(raw):
    """(calls, revised_epoch) out of one cruise schedule PDF."""
    rows = _pdf_rows(raw)

    # The header row, wherever it is. This sheet runs the table across two
    # pages and only prints the header on one of them, and which one is not the
    # first: found by content, then applied to every row in the document.
    cols = None
    revised = None
    for _page, _y, cells in rows:
        names = [s for _, s in cells]
        if cols is None and all(h in names for h in _SFPORT_COLUMNS[:3]):
            cols = {s: x for x, s in cells if s in _SFPORT_COLUMNS}
        for _x, s in cells:
            m = _SFPORT_REVISED.search(s)
            if m and revised is None:
                mo, dy, yr = (int(g) for g in m.groups())
                revised = _sfport_epoch("%s-%d-%d" % (
                    list(_SFPORT_MONTHS)[mo - 1] if 1 <= mo <= 12 else "",
                    dy, yr + 2000 if yr < 100 else yr), "")
    if not cols or len(cols) < 6:
        raise ValueError("cruise schedule header row not found")

    calls = []
    for _page, _y, cells in rows:
        if any(s in ("Vessel", "ETA Date") for _, s in cells):
            continue
        rec = {}
        for x, s in cells:
            near = min(cols, key=lambda h: abs(x - cols[h]))
            # Half a column's width. Anything further from every header than
            # that is not a cell of this table -- a footnote, a legend, the
            # page furniture -- and guessing a home for it would put junk in
            # the payload.
            if abs(x - cols[near]) < 90.0:
                rec.setdefault(near, []).append(s)

        def cell(h):
            return " ".join(rec.get(h, [])).strip()

        eta = _sfport_epoch(cell("ETA Date"), cell("Arrival Time"))
        etd = _sfport_epoch(cell("ETD Date"), cell("Departure Time"))
        # The sheet interleaves "Pier 27 Event" rows -- a concert, a private
        # hire -- among the calls. They have dates and nothing else, and they
        # are not ships, so the test is a berth and a clock time rather than a
        # blacklist of words that would go stale the first time somebody typed
        # a different one.
        if not cell("Berth") or not (cell("Arrival Time") or cell("Departure Time")):
            continue
        if eta is None and etd is None:
            continue
        calls.append({"vessel": cell("Vessel").upper(),
                      "line": cell("Cruise Line").upper(),
                      "berth": cell("Berth").upper(),
                      "type": cell("Type").upper(),
                      "eta": eta, "etd": etd,
                      "from": cell("Last Port").upper(),
                      "to": cell("Next Port").upper()})
    if not calls:
        raise ValueError("cruise schedule parsed to no calls")
    return calls, revised


@product("sfport-cruise", ttl=SFPORT_CRUISE_TTL,
         interval=SFPORT_CRUISE_INTERVAL,
         description="Port of SF cruise terminal schedule (scheduled calls)")
def _sfport_cruise():
    """Every scheduled call at Piers 27 and 35, out of the Port's own PDFs.

    Two files rather than one, because the Port publishes a sheet per calendar
    year and the interesting window in December is on next year's. Whichever of
    them fails to parse is skipped rather than fatal -- the 2027 sheet being
    reissued in a shape this cannot read is no reason to lose 2026 -- but all
    of them failing raises, which is what keeps the last good record in place.
    """
    page = get(SFPORT_CRUISE_PAGE, timeout=20)
    from urllib.parse import urljoin
    seen, urls = set(), []
    for href in _SFPORT_LINK.findall(page):
        url = urljoin(SFPORT_CRUISE_PAGE, href.decode("latin-1"))
        base = url.rsplit("/", 1)[-1]
        year = base[:4]
        if not year.isdigit() or int(year) < time.gmtime().tm_year:
            continue
        if year in seen:                     # the newest revision of each year
            continue
        seen.add(year)
        urls.append((year, url))
    if not urls:
        raise ValueError("no cruise schedule PDFs linked from %s"
                         % SFPORT_CRUISE_PAGE)

    now = time.time()
    calls, revised, used, failures = [], None, [], []
    for _year, url in sorted(urls)[:2]:
        try:
            got, rev = _sfport_parse(get(url, timeout=45))
        except Exception as e:                               # noqa: BLE001
            failures.append("%s: %r" % (url, e))
            continue
        used.append(url)
        calls.extend(got)
        if rev is not None:
            revised = rev if revised is None else max(revised, rev)
    if not used:
        raise ValueError("no cruise schedule parsed; " + "; ".join(failures))

    def when(c):
        return c["eta"] if c["eta"] is not None else c["etd"]

    keep = [c for c in calls
            if -SFPORT_KEEP_PAST <= (when(c) - now) <= SFPORT_KEEP_AHEAD]
    keep.sort(key=when)
    # Both sheets can carry the same call in the week either side of new year.
    uniq, prev = [], set()
    for c in keep:
        key = (c["vessel"], c["eta"], c["etd"])
        if key not in prev:
            prev.add(key)
            uniq.append(c)

    return {"port": "SAN FRANCISCO", "berths": "PIERS 27 AND 35",
            "revised": revised, "calls": uniq,
            "note": "berth times as published, not Golden Gate transit times",
            "sources": used}, used[0]


# --------------------------------------------------------------------------

SOLAR_URL = "https://sequoia.garden/api/stats.json"

# Half an hour. The picture is a 24 hour landscape and it is still very nearly
# the right landscape at 29 minutes old; what goes stale faster than that is the
# present-tense claim -- "the site is up and this is its battery right now" --
# and thirty minutes is where the demo has to stop making it.
SOLAR_TTL = 1800

# See the note above on politeness. Also: the site publishes on a five-minute
# cadence, so anything under 300 s would return the identical document.
SOLAR_INTERVAL = 900

# It is on a battery at the end of a domestic link and it has been observed
# taking several seconds to answer. Longer than the house default of 20 s,
# because a timeout here throws away a whole fetch interval.
SOLAR_TIMEOUT = 30

# A hard cap on what is stored, independent of what arrives. 288 five-minute
# buckets is 24 hours and is what the endpoint publishes today; if it ever
# starts publishing a week at the same resolution, this record stays the size
# it is and the panel keeps drawing the last day rather than quietly growing a
# 60 kB record on the Pi's SD card.
SOLAR_MAX_BUCKETS = 288


def _solar_days(uptime):
    """Whole days out of an uptime string like '190d 20h 5m'. None if absent.

    The string is carried through verbatim as well -- it is what gets printed --
    but the number is wanted separately so the demo can decide whether 190 days
    is worth a line of its own without parsing anything at build time.
    """
    import re
    m = re.match(r"\s*(\d+)\s*d", str(uptime or ""))
    return int(m.group(1)) if m else None


@product("solar-garden", ttl=SOLAR_TTL, interval=SOLAR_INTERVAL,
         description="sequoia.garden's solar battery: 24h of volts, charge "
                     "current and state of charge at 5 minute resolution")
def _solar_garden():
    """Last 24 hours of the makerspace website's own power supply.

    Three of the seven published series, rounded to the precision that survives
    being drawn on a 64 row panel, plus the dozen scalars the readout needs.
    """
    d = get_json(SOLAR_URL, timeout=SOLAR_TIMEOUT)
    sl = d.get("sparklines") or {}
    meta = d.get("sparkline_meta") or {}

    stamps = list(sl.get("timestamps") or [])
    if len(stamps) < 2:
        raise ValueError("sequoia.garden returned %d sparkline buckets"
                         % len(stamps))

    # Keep the newest buckets if there are somehow more than a day of them.
    if len(stamps) > SOLAR_MAX_BUCKETS:
        stamps = stamps[-SOLAR_MAX_BUCKETS:]
    keep = len(stamps)

    def series(key, digits):
        """The last `keep` values of one array, rounded. Missing -> None.

        None rather than a substitute: a gap in the record is a gap in the day,
        and the demo draws it as one. Interpolating here would invent a battery
        voltage for a five minute window the sensor did not report, which is
        precisely the kind of quiet fiction the cache is not allowed to hold.
        """
        vals = list(sl.get(key) or [])[-keep:]
        vals += [None] * (keep - len(vals))
        out = []
        for v in vals:
            try:
                out.append(round(float(v), digits))
            except (TypeError, ValueError):
                out.append(None)
        return out

    # The bucket step, in seconds, from the source rather than assumed -- the
    # whole one-column-per-bucket identity rests on it being 300.
    step = float(meta.get("bucket_ms") or 300000) / 1000.0
    t0 = float(stamps[0]) / 1000.0

    def num(key):
        try:
            return float(d[key])
        except (KeyError, TypeError, ValueError):
            return None

    avg = d.get("range_averages") or {}

    def avg_of(key, digits):
        try:
            return round(float(avg[key]), digits)
        except (KeyError, TypeError, ValueError):
            return None

    return {
        # The series, chronological, one entry per `step` seconds from `t0`.
        "t0": t0,
        "step": step,
        "n": keep,
        "volt": series("voltage", 3),
        "cur_ma": series("currentDraw", 1),
        "soc": series("mainBattery", 2),

        # The present moment, as the site itself reports it.
        "soc_pct": num("soc_pct"),
        "status": str(d.get("status") or ""),
        "v": num("esp32_v_V"),
        "i_ma": num("esp32_i_mA"),
        "load_w": num("load_W"),
        "p_in_w": num("p_in_W"),
        "cpu_c": num("cpu_temp_c"),
        "cpu_load": num("cpu_load_15min"),

        # The site's own staleness flag: the web server answered, but the
        # battery monitor behind it has not spoken in `sensor_age_s`. That is a
        # different outage from the one where this fetch fails outright, and
        # the panel says so differently.
        "sensor_stale": bool(d.get("data_stale")),
        "sensor_age_s": num("data_age_s"),

        "uptime": str(d.get("uptime") or ""),
        "up_days": _solar_days(d.get("uptime")),
        "local_time": str(d.get("local_time") or ""),

        "avg_volt": avg_of("voltage", 3),
        "avg_cur_ma": avg_of("currentDraw", 1),
        "avg_soc": avg_of("mainBattery", 2),

        "site": "sequoia.garden",
    }, SOLAR_URL


# --------------------------------------------------------------------------
# The San Francisco Metropolitan Internet Exchange: what is on its backbone
# right now, and how much of it there is. sfmix.py draws the weathermap.
#
# **Three endpoints, all keyless, all the exchange's own.**
#
#   /statistics/map/map.json   the public structure -- sites, metros, and the
#                              inter-metro trunks with their real fibre routes
#                              as coarse lon/lat polylines.
#   /statistics/map/traffic    live bits per second per opaque cable id, with a
#                              24 hour series and a per-member breakdown.
#   /statistics/metrics/?panel=ix_total&range=24h
#                              the aggregate everybody quotes: total ingress
#                              and egress across every member port, 300 s step.
#
# **The generation field is a safety interlock and it is used as one.** Both
# map.json and the traffic feed carry the same `generation` string, and the
# cable ids are opaque *per generation* -- they are rebuilt from scratch every
# time the portal re-runs its NetBox build. Joining traffic from one generation
# onto geometry from another does not fail loudly: it silently drops some links
# and, worse, could colour a trunk with a number belonging to a different one.
# So the two are fetched, compared, and refetched once if they disagree (which
# is the ordinary race -- the builder republished between our two GETs). A
# second disagreement raises, and `fetch()` keeps the last good record.
#
# **What is thrown away here, and why.** The three responses are about 135 KB
# together and almost none of it survives:
#
#   * The twelve *sites* collapse to five *metros*. At the scale this panel
#     draws -- eighty kilometres across two hundred columns, so a third of a
#     kilometre a pixel -- the six Santa Clara facilities are the same pixel,
#     and the six intra-metro cables between them are zero pixels long. The
#     portal's own zoomed-out tier already solved this: `metro_cables` are
#     pre-aggregated inter-metro trunks with their own routes, and traffic is
#     summed onto them exactly the way the portal's frontend does it.
#   * The 24 hour per-link series and the per-member breakdowns go entirely.
#     The panel shows *now* per trunk; the history it shows is the exchange
#     total, which is a different and much smaller series.
#   * The routes are Douglas-Peucker simplified to about a tenth of their
#     vertices. 846 points down to under a hundred, at a tolerance well under
#     one panel pixel, so the drawn line is unchanged.
#   * The ix_total series is bucketed down from 289 points to 97 and carried in
#     megabits rather than bits. Bucket *maximum* rather than mean, and the true
#     peak and its timestamp are carried separately, because the peak is the
#     number an exchange is judged by and a decimation that shaved it would be
#     the one lie this record could tell.
#
# What lands in the cache is about 6 KB.
# --------------------------------------------------------------------------

SFMIX_MAP_URL = "https://portal.sfmix.org/statistics/map/map.json"
SFMIX_TRAFFIC_URL = "https://portal.sfmix.org/statistics/map/traffic"
SFMIX_METRICS_URL = ("https://portal.sfmix.org/statistics/metrics/"
                     "?panel=%s&range=24h")

# Half an hour. The traffic feed is a five-minute Prometheus rate behind a
# thirty-second server-side cache, so a record older than this is showing a
# backbone load that has had time to move; the structure underneath it is good
# for days. Thirty minutes is where "this is what the exchange is doing" stops
# being true and the panel has to say so.
SFMIX_TTL = 1800

# Five minutes, which is the resolution of the underlying counters. Asking
# faster returns the same numbers and costs the portal a Prometheus burst.
SFMIX_INTERVAL = 300

# How many points of the 24 hour total curve to keep. The chart it draws is
# about ninety columns wide, so ninety-seven buckets of fifteen minutes each is
# a hair over one sample a column and nothing is thrown away that could be seen.
SFMIX_SERIES_POINTS = 97

# Douglas-Peucker tolerance for the trunk routes, in degrees. 0.0012 deg is
# about 110 m, which at the panel's ~330 m a pixel is a third of a pixel: the
# simplification is invisible by construction, and it takes 846 vertices to 90.
SFMIX_PATH_TOL = 0.0012


def _sfmix_simplify(path, tol):
    """Douglas-Peucker on a [[lon, lat], ...] polyline. Iterative, no recursion.

    Plain arithmetic in degrees rather than metres: the tolerance is a panel
    pixel and the aspect error over half a degree of latitude is smaller than
    the rounding that follows, so converting would be precision theatre.
    """
    if len(path) < 3:
        return [list(p) for p in path]
    keep = [False] * len(path)
    keep[0] = keep[-1] = True
    stack = [(0, len(path) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        x0, y0 = path[i0]
        x1, y1 = path[i1]
        dx, dy = x1 - x0, y1 - y0
        norm = (dx * dx + dy * dy) ** 0.5
        worst, at = -1.0, -1
        for i in range(i0 + 1, i1):
            x, y = path[i]
            if norm == 0.0:
                d = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
            else:
                d = abs(dy * (x - x0) - dx * (y - y0)) / norm
            if d > worst:
                worst, at = d, i
        if worst > tol:
            keep[at] = True
            stack.append((i0, at))
            stack.append((at, i1))
    return [[round(path[i][0], 5), round(path[i][1], 5)]
            for i in range(len(path)) if keep[i]]


def _sfmix_metro_code(codes):
    """The three-letter airport-ish prefix a metro's site codes agree on.

    'sfo01', 'sfo02' -> 'SFO'; the Santa Clara metro's six codes are five scl
    and one snv, and the majority wins. Derived rather than hardcoded because a
    table of pretty names in this file would be the thing that went stale the
    first time a site was added.
    """
    counts = {}
    for code in codes:
        head = str(code)[:3].upper()
        if len(head) == 3 and head.isalpha():
            counts[head] = counts.get(head, 0) + 1
    if not counts:
        return "?"
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _sfmix_buckets(values, stamps, n):
    """Bucket a series down to `n` points, keeping the maximum of each bucket.

    Maximum and not mean: this is a traffic curve whose whole shape question is
    "how high did it get", and averaging four five-minute samples into a
    quarter hour shaves the top off every spike it touches.
    """
    if not values:
        return [], []
    n = min(n, len(values))
    out_v, out_t = [], []
    for k in range(n):
        lo = k * len(values) // n
        hi = max(lo + 1, (k + 1) * len(values) // n)
        chunk = [v for v in values[lo:hi] if v is not None]
        out_v.append(max(chunk) if chunk else None)
        out_t.append(int(stamps[hi - 1]))
    return out_v, out_t


def _sfmix_mbps(v):
    return None if v is None else int(round(float(v) / 1e6))


@product("sfmix-ix", ttl=SFMIX_TTL, interval=SFMIX_INTERVAL,
         description="SFMIX metro trunks, utilisation and 24h exchange total")
def _sfmix_ix():
    """The exchange's backbone as five trunks, plus the total curve for today.

    Traffic is summed onto a trunk from its member cables the way the portal's
    own frontend does it -- utilisation is `max(in, out) / capacity`, the
    convention every weathermap has used since MRTG, because a link is as busy
    as its busier direction and averaging the two hides a saturated one.

    Direction is resolved per member rather than assumed. A trunk's `out` is
    a_metro to z_metro, and each member cable carries its own a_site; where a
    member happens to be cabled the other way round its two figures are swapped
    before they are added, so the arrows on the panel point at the direction
    the bits are actually going.
    """
    struct = get_json(SFMIX_MAP_URL, timeout=30)
    traffic = get_json(SFMIX_TRAFFIC_URL, timeout=30)
    gen = struct.get("generation")
    if gen != traffic.get("generation"):
        # The ordinary case is a rebuild landing between our two GETs. Refetch
        # the structure once, which is the half that just changed.
        struct = get_json(SFMIX_MAP_URL, timeout=30)
        gen = struct.get("generation")
    if not gen or gen != traffic.get("generation"):
        raise ValueError("map generation %r != traffic generation %r"
                         % (gen, traffic.get("generation")))

    metros_in = struct.get("metros") or {}
    links = traffic.get("links") or {}
    site_metro = {code: site.get("metro")
                  for code, site in (struct.get("sites") or {}).items()}
    cable_a = {c["id"]: c.get("a_site") for c in (struct.get("cables") or [])}

    metros = {}
    for name, m in metros_in.items():
        codes = m.get("codes") or []
        metros[name] = {"lat": round(float(m["lat"]), 5),
                        "lon": round(float(m["lon"]), 5),
                        "code": _sfmix_metro_code(codes),
                        "sites": len(codes)}

    trunks = []
    for g in struct.get("metro_cables") or []:
        a_metro, z_metro = g.get("a_metro"), g.get("z_metro")
        cap = float(g.get("capacity_bps") or 0.0)
        in_bps = out_bps = 0.0
        seen = 0
        for cid in g.get("member_ids") or []:
            tr = links.get(cid)
            if not tr:
                continue
            seen += 1
            # `out` on a cable leaves its own a_site. If that site is in the
            # trunk's z metro the cable is cabled the other way round and its
            # two directions belong to the trunk's other arrow.
            flip = site_metro.get(cable_a.get(cid)) == z_metro
            in_bps += float(tr.get("out_bps") or 0.0) if flip \
                else float(tr.get("in_bps") or 0.0)
            out_bps += float(tr.get("in_bps") or 0.0) if flip \
                else float(tr.get("out_bps") or 0.0)
        trunks.append({
            "a": a_metro, "z": z_metro,
            "cap_mbps": int(round(cap / 1e6)),
            "in_mbps": _sfmix_mbps(in_bps), "out_mbps": _sfmix_mbps(out_bps),
            "util_pct": round(100.0 * max(in_bps, out_bps) / cap, 2)
                        if cap > 0 else 0.0,
            "status": g.get("status") or "up",
            "members": len(g.get("member_ids") or []),
            "reporting": seen,
            "path": _sfmix_simplify(g.get("path") or [], SFMIX_PATH_TOL),
        })
    if not trunks:
        raise ValueError("SFMIX map carried no metro trunks")

    # Every inter-site cable, whether or not it is inside a metro trunk. This
    # is the "13 backbone links" the panel can honestly claim; the intra-site
    # LAGs and cross-connects are not backbone and are not counted.
    inter = [c for c in (struct.get("cables") or [])
             if c.get("scope") == "inter"]

    total = get_json(SFMIX_METRICS_URL % "ix_total", timeout=30)
    stamps = [int(x) for x in (total.get("timestamps") or [])]
    series = {s.get("name", "").lower(): s.get("values") or []
              for s in (total.get("series") or [])}
    ingress = series.get("ingress") or []
    if not stamps or len(ingress) != len(stamps):
        raise ValueError("ix_total series does not line up with its timestamps")

    # Ingress and egress differ by about two parts in ten thousand, which is
    # what an exchange looks like when it is working: what a member sends into
    # the fabric is what some other member receives out of it. One curve, and
    # the panel says "exchanged" rather than picking a side.
    finite = [(v, ts) for v, ts in zip(ingress, stamps) if v is not None]
    if not finite:
        raise ValueError("ix_total series is entirely null")
    peak_v, peak_t = max(finite, key=lambda vt: vt[0])
    now_v, now_t = finite[-1]
    curve, curve_t = _sfmix_buckets(ingress, stamps, SFMIX_SERIES_POINTS)

    return {"generation": gen,
            "generated_at": struct.get("generated_at"),
            "metros": metros,
            "trunks": trunks,
            "backbone_links": len(inter),
            "sites": len(site_metro),
            "total": {"now_mbps": _sfmix_mbps(now_v), "now_at": int(now_t),
                      "peak_mbps": _sfmix_mbps(peak_v), "peak_at": int(peak_t),
                      "step_s": int(total.get("step") or 300),
                      "t": curve_t,
                      "mbps": [_sfmix_mbps(v) for v in curve]},
            "note": "util is max(in,out)/capacity per trunk, portal convention",
            }, SFMIX_MAP_URL


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
