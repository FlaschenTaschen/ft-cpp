"""Where the wall is. One file, read once, with the answer baked in as a default.

Every panel that draws a map needs the installation's own address: quake.py
rings it, adsb.py measures slant range from it, sats.py puts a tick on the world
map at it, ftdata's wx/adsb/quake fetchers ask three different APIs about it.
Until this module existed that coordinate was written out seven times in four
files, and the inevitable happened -- it was wrong by 273 m in all seven places
at once, and fixing it meant finding all seven.

Two things follow from that, and they are the whole design:

**The address is installation config, not code.** This tree has two long-lived
branches: `demos/pack`, which goes upstream to FlaschenTaschen, and the branch
for this one wall in this one makerspace. A latitude compiled into `adsb.py` is
exactly the kind of fact that makes those two diverge and stay diverged. In a
file, the code goes upstream unchanged and the installation carries its own
`site.json`.

**A missing config file must not take the wall down.** The defaults below are a
real place -- the same spirit as ftdata's default tide station being a real
station -- so a fresh checkout with no `site.json`, a checkout whose file is
unreadable, and a file missing half its keys all draw a real panel. A warning at
most, never an exception. The wall coming up showing the wrong city is a bug
somebody notices and fixes; the wall not coming up is an evening lost.

The file is JSON because the Pi is on Python 3.9, which has no `tomllib`, and
because `rotation-*.json` already set the precedent. `FT_SITE` points at another
one, matching `FT_DATA_CACHE`, `FT_GBFS_BASE` and `FT_PIXELART_DIR`.

    {
      "name":  "Sequoia Fabrica",     # long form, for a label with room
      "short": "SF",                  # <= 4 chars, for a label without room
      "lat":   37.7624929274026,
      "lon":   -122.39969356310202
    }

Unknown keys are kept and ignored, so a file can carry settings this version
does not know about yet -- which is how the next things to move here (the tide
station, the swell buoy, the ADS-B radius) arrive without a flag day.

Deliberately dependency-free: json, os, sys and nothing else. Non-data demos
should be able to ask where they are without dragging in the whole fetch layer,
so this must never import ftdata -- the dependency runs the other way.
"""

import json
import os
import sys

# The compiled-in fallback: Sequoia Fabrica, 1736 18th Street, San Francisco --
# Dogpatch/Potrero, not the Mission, whatever the old comments said. Surveyed
# from the building, which is why it carries more digits than a wall-sized
# pixel could ever use; they are kept because rounding is the caller's business
# and the 4-decimal wx product name is one of the callers.
DEFAULTS = {
    "name": "Sequoia Fabrica",
    "short": "SF",
    "lat": 37.7624929274026,
    "lon": -122.39969356310202,
}

# demos/site.json unless told otherwise. Relative to this file rather than the
# working directory: the scheduler, the fetcher timer and a person running one
# demo by hand all start from different places.
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "site.json")


def config_path():
    """The file load() will read. FT_SITE wins; empty FT_SITE means the default."""
    return os.environ.get("FT_SITE") or DEFAULT_PATH


def _warn(msg):
    print("ftsite: %s" % msg, file=sys.stderr)


def load(path=None):
    """The site: DEFAULTS, overlaid with whatever the config file validly says.

    Never raises and never returns a partial dict. Every failure mode -- absent,
    unreadable, not JSON, not an object, a latitude that is a string or off the
    globe -- falls back key by key, so a file that gets `lon` right and `lat`
    wrong still contributes its longitude and gets a complaint about the rest.

    Absence is silent: a checkout that never had a site.json is the normal
    upstream case, not a fault. Everything else says something on stderr, once,
    where the fetcher's log and an interactive run will both show it.
    """
    site = dict(DEFAULTS)
    path = path or config_path()
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except IOError:                 # includes FileNotFoundError on 3.9
        return site
    except ValueError as exc:       # includes json.JSONDecodeError
        _warn("%s is not valid JSON (%s); using built-in defaults" % (path, exc))
        return site
    except OSError as exc:          # noqa: BLE001  -- permissions, a directory
        _warn("cannot read %s (%s); using built-in defaults" % (path, exc))
        return site

    if not isinstance(raw, dict):
        _warn("%s is not a JSON object; using built-in defaults" % path)
        return site

    # Unknown keys ride along untouched; known ones are type- and range-checked,
    # because a bad latitude here is a panel that silently draws the wrong
    # hemisphere and a wx product fetched for the middle of the Pacific.
    for key, value in raw.items():
        if key in ("lat", "lon"):
            limit = 90.0 if key == "lat" else 180.0
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _warn("%s: %s is not a number; keeping %r"
                      % (path, key, site[key]))
                continue
            if not -limit <= float(value) <= limit:
                _warn("%s: %s=%r is outside +/-%g; keeping %r"
                      % (path, key, value, limit, site[key]))
                continue
            site[key] = float(value)
        elif key in ("name", "short"):
            if not isinstance(value, str) or not value.strip():
                _warn("%s: %s is not a non-empty string; keeping %r"
                      % (path, key, site[key]))
                continue
            site[key] = value.strip()
        else:
            site[key] = value
    return site


# Read once, at import. The file is a few hundred bytes and every demo wants it
# at build time; re-reading it per call would buy nothing but a chance of two
# panels in one rotation disagreeing about where they are. Call load() directly
# if you genuinely need to re-read.
SITE = load()

NAME = SITE["name"]
SHORT = SITE["short"]
LAT = SITE["lat"]
LON = SITE["lon"]


def latlon():
    """(lat, lon) as a tuple, for the callers that want one."""
    return (LAT, LON)


if __name__ == "__main__":
    print("%-6s %s" % ("file", config_path()))
    print("%-6s %s" % ("found", "yes" if os.path.exists(config_path()) else
                       "no (using built-in defaults)"))
    for _k in sorted(SITE):
        print("%-6s %r" % (_k, SITE[_k]))
