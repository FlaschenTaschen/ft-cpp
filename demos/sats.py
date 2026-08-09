#!/usr/bin/env python3
"""What is overhead, right now: fifteen satellites on a map of the world.

A 320x64 panel is a letterbox, and the one picture that has always been drawn
in a letterbox is a world map with satellites crawling over it. So that is what
this is: coastlines, the day/night terminator for this exact minute, fifteen
subsatellite points each trailing twenty minutes of ground track behind it and
carrying twenty ahead, the ISS labelled and ringed by its own footprint, San
Francisco marked, and a strip along the bottom saying when the next thing goes
over the workshop and how high it gets.

**The nice property, and the reason this demo exists.** Everything else in this
directory that shows live data is only as alive as the fetcher: a tide curve
with a dead fetcher is a still picture. Orbital elements are not a measurement,
they are a *description of an orbit*, and turning one into a position needs
nothing but the time of day. So this panel moves continuously and correctly on
a cache record that is three days old, and it would keep moving all week. The
fetcher exists to stop the elements drifting, not to make the picture go.

**The projection is a plate carree squashed exactly two and a half times.**
360 degrees across 320 columns is 1.125 deg a column; the whole 180 degrees of
latitude across 64 rows is 2.8125 deg a row. There is no honest way round that:
a true-scale world map 320 px wide is 160 rows tall and the panel has 64. The
alternative -- keeping the scale and cropping to the 72 degrees of latitude that
fit -- would cut off everything above 36 N, which is to say the ISS for most of
its orbit and every polar bird entirely. So the world is squashed instead.
Greenland is short and fat, the tracks are flatter sinusoids than an atlas would
draw, and nothing is missing. `--lat-span 72` crops instead, for comparison.

**The propagator is Kepler plus J2 secular rates, and it is not SGP4.** That
distinction is worth stating plainly, because the picture looks identical either
way and the difference only shows up as a satellite arriving somewhere a minute
early. What is implemented, from the mean elements as CelesTrak gives them:

  * Brouwer element recovery -- the mean motion in a TLE is a Kozai mean, and
    the semi-major axis it implies has to be corrected before anything else is
    done with it, or the period is wrong by a few seconds an orbit;
  * the three J2 secular rates: nodal regression (about -5 deg a day for the
    ISS, which is what makes its ground track walk west), rotation of the
    argument of perigee, and the correction to the mean-motion;
  * the TLE's own n-dot/2 term as a quadratic in mean anomaly, which is the one
    piece of atmospheric drag a Kepler propagator can carry.

What is **not** implemented, and where this breaks: SGP4's short-period J2 terms
in radius and argument of latitude (of order J2 (Re/p)^2, which is a twentieth
of a degree here -- under a pixel), and drag proper, meaning the BSTAR model.
That is why BSTAR is not even stored: a decaying orbit loses altitude, and
losing altitude speeds a satellite up, and the error from ignoring all of that
is along-track and grows with time since epoch.

Measured against a public SGP4 service on elements 18 hours old, the ISS
subsatellite point comes out **0.09 degrees** away -- 10 km, a twelfth of a
column on this map -- and propagating a further three days ahead takes it to
0.13 degrees, 14 km. Altitude agrees within 8 km. So over the whole span the
product's three-day TTL allows, this is inside a fifth of a pixel of SGP4,
which is a rather better answer than it has any right to be and comes almost
entirely from carrying the n-dot/2 term. It would not survive being pointed at
something with a large BSTAR and a bad afternoon of solar activity, and it is
still not SGP4: for drawing where things are it is indistinguishable, for
pointing a dish it is not, and nobody should use it for that.

The same propagator does the pass prediction, so the same caveat applies to
"ISS IN 42M": expect it to be right to well under a minute, not to the second.

**Data comes from the cache, never from the network.** `ftdata.py` fetches the
GP elements once a day in a process of its own; `build()` calls
`ftdata.load("sats")` and this file imports no HTTP library at all. It has to be
that way -- `ftsched` builds the next segment on a worker thread, Python threads
share the GIL, and a `build()` blocked on a socket stops the wall for everyone.

    $ python3 ftdata.py --once --only sats
    $ python3 ftdata.py --loop 900 &

Past the record's three-day TTL the panel stops drawing satellites and says so.
That is a harder line than the other data demos take, and it is the right one
here: a stale tide curve is visibly the wrong shape, but a stale orbit is a
perfectly plausible dot in the wrong place, and there is nothing on the panel
that would give it away.

**Frame budget: 50 ms at 20 fps**, which is what a segment gets on the wall's
Pi 3 with its clock pinned at 600 MHz. Measured on this desktop the frame is
**0.35 ms p50 and 0.37 ms p95**, 0.58 ms at its worst over fifteen hundred
frames; at the 100x this project keeps measuring between the two machines that
is about 37 ms on the Pi against the 50 available, and the extrapolation is
more likely pessimistic than optimistic because the cost here is dominated by
arithmetic over an 1815-element array rather than by call overhead. `build()`
is 17 ms, once, on the worker thread -- most of it the pass search over the
next day.

The shape of that cost is deliberate. Everything moves every frame, so nothing
about the satellites is cached; what is cached is everything that does not
move. A frame is one full-frame copy of the map, about sixty numpy calls to
propagate all fifteen satellites at 121 track samples each -- one array of
1815, so the call count does not depend on how many satellites there are -- and
four scatters. Three things paid for that budget and are worth knowing about
before touching this file:

  * **Kepler's equation is not iterated.** The equation of the centre to second
    order in e is already better than the propagation it feeds, and one Newton
    step on top squares that error; five blind iterations, which is what this
    started as, cost 45 numpy calls a frame for no accuracy whatever.
  * **Geodetic latitude is a closed form, not a fixed point.** The textbook
    iteration is a dozen calls over the whole track; the first-order expansion
    agrees with it to 0.0007 degrees, which is a four-hundredth of a row.
  * **Sidereal time over a track is a straight line.** GMST is a cubic in
    centuries; over the forty minutes of one ground track it is linear to a
    part in 1e10, so it is one scalar plus one array baked in `build()`.

The map, the terminator and the status strip are all rebuilt only when they
change, which for the terminator is every two and a bit minutes -- when the
subsolar point has moved half a column.
"""

import math
import sys
import time

import numpy as np

import defcon
import demoscene as ds
import ftdata

f32 = np.float32

# The wall's own address: Sequoia Fabrica, 1736 18th Street, San Francisco.
# Everything on the bottom strip is relative to this point.
SITE_LAT, SITE_LON = 37.7627, -122.3966
SITE_NAME = "SF"

PRODUCT = "sats"

# Physical constants. WGS-84 for the ellipsoid, since the subsatellite point is
# quoted as a geodetic latitude and the difference from geocentric runs to
# 0.19 degrees -- under a tenth of a row here, but the same size as the whole
# error budget of the propagator, so it is not worth throwing away.
MU = 398600.4418                        # km^3/s^2
RE = 6378.137                           # km, equatorial
J2 = 1.08262668e-3
FLAT = 1.0 / 298.257223563
E2 = FLAT * (2.0 - FLAT)
TWOPI = 2.0 * math.pi
# Earth's rotation rate in radians of sidereal time per second. Over a
# forty-minute ground track GMST is a straight line to a part in 1e10, so
# the whole track's sidereal time is one scalar plus one baked array.
OMEGA_EARTH = TWOPI * 1.0027379093 / 86400.0

# Colours. The map is nearly black so that fifteen small bright things are the
# only things on it; the coastline is a cool line rather than a filled landmass
# because on an LED wall a drawn coast reads from across the room and a filled
# continent just makes the panel grey.
C_SEA_DAY = (9, 20, 36)
C_SEA_NIGHT = (2, 3, 7)
C_COAST_DAY = (64, 126, 140)
C_COAST_NIGHT = (24, 42, 60)
C_GRID = (16, 26, 38)                   # equator and the meridians
C_SITE = (255, 72, 96)

# By kind, which is the group the elements came out of. The station white is
# the brightest thing on the panel on purpose: it is what people look for.
C_KIND = {
    "station": (255, 244, 214),
    "amateur": (72, 236, 150),
    "weather": (255, 162, 54),
}
C_KIND_DEFAULT = (170, 180, 200)

C_TEXT = (196, 214, 228)
C_DIM = (96, 110, 124)
C_WARN = (255, 96, 72)
C_GO = (130, 255, 180)

# --------------------------------------------------------------------------
# Type. defcon.py's 3x5 table, plus the four glyphs a tracking readout needs
# and a map of a nuclear exchange does not. Baked, so there is no font file to
# be missing on the Pi and nothing to look different from the machine this was
# written on.
# --------------------------------------------------------------------------

_FONT = dict(defcon._FONT)
_FONT.update({
    "+": "02720", "^": "25200", ">": "42124", "<": "12421",
    "'": "22000", "!": "22202", "?": "71602", ",": "00021",
})

_GLYPHS = {}
for _ch, _rows in _FONT.items():
    _g = np.zeros((5, 3), bool)
    for _r, _d in enumerate(_rows):
        _v = int(_d, 8)
        for _c in range(3):
            if _v & (4 >> _c):
                _g[_r, _c] = True
    _GLYPHS[_ch] = _g


def text_mask(s, scale=1):
    """A boolean mask for a string, one blank column between glyphs."""
    s = str(s).upper()
    if not s:
        return np.zeros((5 * scale, 1), bool)
    out = np.zeros((5, len(s) * 4 - 1), bool)
    for i, ch in enumerate(s):
        out[:, i * 4:i * 4 + 3] = _GLYPHS.get(ch, _GLYPHS[" "])
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


def text_width(s, scale=1):
    return max(1, (len(str(s)) * 4 - 1) * scale)


def blit_text(dst, y, x, s, rgb, scale=1):
    """Draw a string at (y, x), clipped to `dst`. Returns the width drawn."""
    m = text_mask(s, scale)
    gh, gw = m.shape
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(dst.shape[0], y + gh), min(dst.shape[1], x + gw)
    if y1 <= y0 or x1 <= x0:
        return gw
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    dst[y0:y1, x0:x1][sub] = rgb
    return gw


# --------------------------------------------------------------------------
# The coastline.
#
# Natural Earth 1:110m "Coastline" (public domain), simplified offline with
# Douglas-Peucker to about 0.8 px on the 3x supersampled canvas this is
# rasterised at, and encoded exactly the way defcon.py encodes its own: a space
# between polylines, four base-64 digits for the first point (x high, x low,
# y high, y low), then two digits a point for the delta from the previous one,
# biased by 32, with any segment longer than 31 split when baking so every
# delta fits one digit. 134 polylines and 1536 points in 3.5 kB of source,
# decoded in one Python loop at build time.
#
# The grid is 960x192 covering the whole Earth: x is (lon+180)/360, y is
# (90-lat)/180, which is the panel's own projection at 3x. Nothing is read at
# runtime and nothing needs the network, which is the same reason defcon carries
# its map -- a Pi that boots into the rotation has no guarantee that anything
# else is reachable.
# --------------------------------------------------------------------------

_ALPHA = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
          "abcdefghijklmnopqrstuvwxyz+/")
_IDX = dict((c, i) for i, c in enumerate(_ALPHA))

COAST_W, COAST_H = 960, 192

COAST = (
    "0h2qiXKV 7F0dXWUXNXZVUVcUbXUX DO1ZoZWXUWgaOWOTQYJVZVUUJUTVbVNVfWXYZX"
    "cUfX CH1RcUdXRZaYTWVYUXVYSWKVTTXUaWfT 3c0GOWeW 3c0DPWdW 3U0CZWSXSVbW"
    " 3r0H8TtXtXAX 2t0DaWMWcW 2u0CaWOWaW 4l1CYXSVYW 4H1CaXQWYV 4517rZNXXV"
    "JUXWNXeU 5C0fTYeWWXZXTXUVTXMVdSZW 400RgXDWZWXUbX 4E0JxYaXSXoYRYLUfZL"
    "WdYOWGTNWiVVWaUVVGU7WTWZVSWVViUUXhVbXWXbW 3a0HhWMYRVaV 3K0ITWhVVXaXR"
    "XLVbV 300FcWFYRVcWGWcUnXSVZWaX 2O0FoUUXGX 2S0HmXCYOVbUUWfW 0a0WYWRWZ"
    "W 170YZXRWVWZV 1+0cZWVXYXRVXV 2I0hbXIUfX 0M0SeWOW 370BlYIVYWTV 8u0wa"
    "WQXYV 8c0wPWdW 9a1jZZTXXXPfRWSVUUZTVUYUaWfS EU1nYXUV ET1nVVYXVW D61d"
    "XVVX 5U2pjXXX4XkT 4l2soVVXFW 4R2ibWXUaVbZOXMVZW 3F2jmWGW 2P2lgVMX 2D"
    "2keXOV EQ1saYNTbX Dt1aXXQUbX Dp1cPWgVTX EG1hXXTVYW EF1gTVZX ED1hTVZX"
    " EA1fSVaX E31dXXTVYW Dz1cZXUWVV Eu2BQXXVTVYVWVQTbXaYdWSZ Ea2EfTaXSYX"
    "XRWUYLWeT Dg2CXWXXSYTVTUeW Cm22QYHXOVYUPQZWTUYTXXnTcSYXVWcThXVVZUcWU"
    "VXWhXXWSZjZZUXSYUabaWYaiacZYZUaPaVYMYTUSXOWTUSVXVSXYTSYRUOVIW Aw1OWX"
    "SXVTXVaY Cv1ZaXOWaV Co1aUVZXVW Cr1UYXUXXXUUXV Ce1VcVIZYXdVRXaaSVVUUW"
    "WZUWVWXUUVZSeW CX1hSVbXVW Ca1faWOWaW CR1fZWPXaV C11dqY4UYVcX Bs1XbYV"
    "ZOVCLcWod CY1IVWZWUW Cd1LaVTYVV Cn1NWXWXVVUXWXTVWVVWRWgUVWZX C41DUUc"
    "WSY Cb16TZUUaUXX DQ0sUXVZVXKXUVLXZXSYTTfUeWZUdVYTZWXY DY0nYWWXQXSVUY"
    "TWWVaVYTdY 7t0qZXWXSWWUXW 7t0pYWUXWV 810aXXUXTVaV 7L0YZVTYcVTYiaVXXW"
    "DXcVRWZVUWXVaVPVVUZUYX 6v0PWXZXIXLWZVQWbVQWwV DT0dbbSVUYZYSWWRVVWUZW"
    " CS1MSXdTWXTX Cc1DXXTXXYcWWYNUXWTVYSaX Cb1KZWTXWV Cl1JWXUWXXVWUVYVUW"
    "aW 4I1NlSXWUXXYYVVVZVbYnWUWlbeWdYaYUYXWkYXXiWiYYZVYNaUbSaEaUXVZJcJVb"
    "ZTYKWWYOXXXaWRXVYSXVWbXNaYYOXMV 4G1OYaMbWXZXSYXXVXaXgeiZZXXXUeUYWaRb"
    "XYTaaVVYTWVYTXaWSYYa 4P2OfYdUgYKXIT 9O0AnWFW 9l0HqUrV4ZOYbYMWQUeUUW "
    "8f0BKWHVxX 8Y0DLWhW 880BnXHYITiW 4G1OTUTXXXVWNVRUVVSUBTATRVWUUVGPTUS"
    "VWYkdVXQUWUOUZWSURTNUMQWTYTUUcWUVKUVUFQFUFVJYXUYWCbFYoTYUJWWVLUaVgVU"
    "VYWLWOVnVIUxTxYxXsUwXwYZWTVYWnXhVaWWXbUQVWVZVcXaXUXbWWXcWVXZWbVWUhXU"
    "XYXKXSYNXNZVYZWZYxYWYcYZVTUfURTZVUUYVqYXYbXeUeZVXmZXXKYFWJanTYXTXYYe"
    "WZVYXHZUVaVPWMYYYMWbWRXTXVWXXTXXWUUWXVWYXXYHaWYaZVYSVRSPWNWXXKVMZXXU"
    "abaiWZUfVWXUZVVVYVXlXWXUZaY 441MkX 4X1BfXOXNVcWUVaW 870tYWVYPVcV 940"
    "mdY D10ycWSXUV 6r1CUTcRjSWVXUfTgWgVrVbXUWXXVYtaYVXVZVqYjWcSVUXWAWSUX"
    "VVWpTjXfVJTcULXaWPYTVZVVWPVObZXFXaZUWVWXXUWOSWUFSUXmbSWVXXXTXXUUVFSG"
    "XWXQXTYXXQYOXMVWUVWYUUTrVXUNTeWVVfWfSfWXVVVXUbVVXYXTXZXtWbVVUYWcWXVT"
    "VmVBVWVVVXWhUNVLZUYaXRXTZPXPSQXPWUSpSpSuUeXTWZXwYPYIVbXWXcXUVfWYWUVj"
    "VTUfXSWZXpUYWUWqVTWXVsYXWQVXVVVfUdXUXYXWXYXQYZWdVVVXVSWYVTVbVVVZXVXZ"
    "WUVlWUUnWTWvUuUwYKYuXtWhYZVqXUVaVvXuXbYxWTWYVuX E/0RQWbYWXHWNYEWSYZX"
    "WXSXXWIbSRYUqSZUKYUVPWPYYXFWFWDbjWaYUXUZSZNZPWJabZVYPWVUYWRUXVLXZUUV"
    "LZcXdWOZdZUXZXVXNbPYIXWXQVPYWXeaXaLZVVXVJTUagaZbOUTSRVXSSQOXXUORKXVY"
    "FaVcQZTVOOUQQXLR8WUURXNUTUQWdcXUYWVYXWcWcUYYdYRYWYPY1bUTWUMRVULRXVTY"
    "STfcWXZYYZmdUWbYnUWYNcCeUYUXYXVYaYXbUXIaYZWYOXXXVYPZLZCWVVXVORTRPRWU"
    "bSWUSQNSZSVVUVPWSUFYDWNTLRVUUVZVXT 070NPV 001nWX Cm1faWLYdU 002wxV/Y"
    "qWqW4UZVLVyV8UPVVVoXkVVVbVxWyVxWnXqWNTpYpUoXnXpVbVSTaTvSXXKXUXXXNXgZ"
    "Za6ZEWgXKXwYxZwVxVwVTVFVYVnV+UbVXWTWZVtUnWsVeXdVoWoWoUjXrUrUpYqWYYRX"
    "ZWTYbWhUpUpUqXhWeUfXqVnYqWpVXVdXrWfY+X+YBcfYIWRYwZxX 000MlZVVjYPWVXH"
    "USXYWUX 000KcWQW E/0KUWYW E/1oTWZV 4z2NfWNW AO2KaXRWXV Ex1pYWSWYW 4y"
    "1LYWTWXW 121BXWTWYW 101AUWYW 0+19UWYW 0x19XWVW 0t18VWXW 4F15YYUVWV 4"
    "D13ZXTV 4H13YXVWVV 4r0kcWPWXW 9S0mdVgVWYPXcXXYYVZXRXZXWZJVVVaUMR 4q0"
    "hdXPV 4A0UZWTW 400TcWQW 4N0ORWaVXX 3V0MYWLWfW 340IbYgXRXYWVX3WLUjWIW"
    "UVcWOWhUkWaYYVUVbW 4B0ITWjWMW DL0IgWMW Dh0GdWKWbW DI0FnWAXbV 3P0EYXR"
    "XOVhV Bo0BdXGXfU BQ0AnXWXQWEUdW 3W0AgVnYDXTWZVMVYW 3i09mVnVtXtW7ZHWa"
    "XIZ8VbWVVeWOVeVRVkWGWMV 5Z08xWwVmX3WwWwXCXcXQXXXZXNWbXXXTWZXLXaXPWeY"
    "MVXXTWgW9Z9YOZXXTYJVNTQSeTMXXUeXKVZVMS5WPViVFWqUXWPWlUVWuWuWQV 340Hb"
    "XQWXV"
)


def _coastlines(scale_x, scale_y):
    """Decode COAST into [[(x, y), ...], ...] on the supersampled canvas."""
    out = []
    for chunk in COAST.split(" "):
        if len(chunk) < 6:
            continue
        x = _IDX[chunk[0]] * 64 + _IDX[chunk[1]]
        y = _IDX[chunk[2]] * 64 + _IDX[chunk[3]]
        pts = [(x * scale_x, y * scale_y)]
        for i in range(4, len(chunk) - 1, 2):
            x += _IDX[chunk[i]] - 32
            y += _IDX[chunk[i + 1]] - 32
            pts.append((x * scale_x, y * scale_y))
        out.append(pts)
    return out


def coast_alpha(w, h, lat_span, sub=3):
    """The coastline as an antialiased (h, w) float alpha.

    Rasterised at `sub` times the panel and box-filtered down, which is the
    whole reason the coast reads as a line rather than as a rash of lit pixels:
    a 1x line on a 320-wide map of the entire Earth is either dashes or, once
    thickened, a blob. Every segment is sampled at a fixed 32 points and the
    whole lot is scattered in one go -- the encoding guarantees no delta is
    longer than 31 grid units, so 32 samples cannot leave a gap, and one
    scatter of forty thousand indices costs a fraction of what a numpy call per
    polyline would on the Pi.
    """
    gw, gh = w * sub, h * sub
    sx = gw / float(COAST_W)
    # `lat_span` crops symmetrically about the equator: at 180 the whole globe
    # fills the panel, at 72 the map is true-scale and the poles are gone.
    sy = gh / (COAST_H * (lat_span / 180.0))
    y_off = (COAST_H * (180.0 - lat_span) / 360.0) * sy

    xs, ys = [], []
    for pts in _coastlines(sx, sy):
        for i in range(len(pts) - 1):
            xs.append((pts[i][0], pts[i + 1][0]))
            ys.append((pts[i][1] - y_off, pts[i + 1][1] - y_off))
    if not xs:
        return np.zeros((h, w), f32)

    seg = np.array(xs, np.float64)
    segy = np.array(ys, np.float64)
    t = np.linspace(0.0, 1.0, 32)[None, :]
    px = seg[:, :1] + (seg[:, 1:] - seg[:, :1]) * t
    py = segy[:, :1] + (segy[:, 1:] - segy[:, :1]) * t
    cc = np.clip(px.astype(np.int32), 0, gw - 1)
    rr = py.astype(np.int32)
    ok = (py >= 0) & (py < gh)
    fine = np.zeros((gh, gw), f32)
    fine[np.clip(rr, 0, gh - 1)[ok], cc[ok]] = 1.0
    return fine.reshape(h, sub, w, sub).mean((1, 3))


# --------------------------------------------------------------------------
# Time. Everything downstream asks for `now` rather than reading the system
# clock, which is what makes a contact sheet across a whole orbit possible:
# --at moves the demo's idea of the present and --rate runs it fast.
# --------------------------------------------------------------------------

def parse_when(s):
    """'now', an epoch, or 'YYYY-MM-DD HH:MM' in local time."""
    if not s or s == "now":
        return None
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    raise ValueError("cannot read a time out of %r" % s)


def clock(at=None, rate=1.0):
    """A callable returning the demo's present moment in epoch seconds."""
    base = time.time() if at is None else float(at)
    start = time.monotonic()
    if at is None and rate == 1.0:
        return time.time
    return lambda: base + (time.monotonic() - start) * rate


def gmst(t):
    """Greenwich mean sidereal time in radians. Scalar or array.

    IAU 1982, fed UTC rather than UT1 because the wall does not know DUT1 and
    nobody is going to tell it. The two differ by under 0.9 s, which is 0.004
    degrees of Earth rotation -- a fortieth of a pixel here, and two orders of
    magnitude under what the propagator itself is worth.
    """
    jd = np.asarray(t, np.float64) / 86400.0 + 2440587.5
    tu = (jd - 2451545.0) / 36525.0
    sec = (67310.54841 + (876600.0 * 3600.0 + 8640184.812866) * tu
           + 0.093104 * tu * tu - 6.2e-6 * tu * tu * tu)
    return np.mod(sec, 86400.0) * (TWOPI / 86400.0)


def gmst_at(t):
    """The same for one moment, as a Python float and no numpy at all.

    Worth having separately: a numpy scalar costs 50 us an operation on the
    Pi against 1.6 us for a plain float, and this is a dozen operations that
    the frame loop wants once per frame.
    """
    tu = (float(t) / 86400.0 + 2440587.5 - 2451545.0) / 36525.0
    sec = (67310.54841 + (876600.0 * 3600.0 + 8640184.812866) * tu
           + 0.093104 * tu * tu - 6.2e-6 * tu * tu * tu)
    return (sec % 86400.0) * (TWOPI / 86400.0)


def sun_subpoint(t):
    """(declination, subsolar longitude) in degrees, for the terminator.

    The low-precision solar position everybody uses, good to about 0.01 degrees
    for a century either side of 2000 -- which is a hundredth of a pixel, and
    the terminator is a soft band twelve degrees wide anyway.
    """
    jd = float(t) / 86400.0 + 2440587.5
    n = jd - 2451545.0
    mean_long = math.radians((280.460 + 0.9856474 * n) % 360.0)
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = mean_long + math.radians(1.915) * math.sin(anomaly) \
        + math.radians(0.020) * math.sin(2.0 * anomaly)
    eps = math.radians(23.439 - 4.0e-7 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    lon = math.degrees(ra - gmst_at(t))
    return math.degrees(dec), (lon + 180.0) % 360.0 - 180.0


def geodetic_ecef(lat_deg, lon_deg, alt_km=0.0):
    """A geodetic position as ECEF kilometres. Used for the observing site."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    s = math.sin(lat)
    n = RE / math.sqrt(1.0 - E2 * s * s)
    return ((n + alt_km) * math.cos(lat) * math.cos(lon),
            (n + alt_km) * math.cos(lat) * math.sin(lon),
            (n * (1.0 - E2) + alt_km) * s)


# --------------------------------------------------------------------------
# The propagator. See the module docstring for what this is and is not.
#
# Everything that does not depend on time is computed once here, per satellite,
# which is most of it: the Brouwer-corrected semi-major axis and mean motion,
# and the three J2 secular rates. What a frame then costs is one pass of
# arithmetic over a flat array, and the number of numpy calls in that pass does
# not depend on how many satellites there are -- which is the only property
# that matters on a 600 MHz Pi, where a call costs 80 us whatever it is given.
# --------------------------------------------------------------------------

class Elements(object):
    """Mean elements for N satellites, ready to propagate."""

    def __init__(self, sats):
        self.sats = list(sats)
        self.n_sats = len(self.sats)
        col = lambda key: np.array([float(s[key]) for s in self.sats],
                                   np.float64)[:, None]
        self.epoch = col("epoch")
        self.ecc = col("e")
        inc = np.radians(col("i"))
        self.cosi, self.sini = np.cos(inc), np.sin(inc)
        self.raan0 = np.radians(col("raan"))
        self.argp0 = np.radians(col("argp"))
        self.ma0 = np.radians(col("ma"))

        n_kozai = col("n") * (TWOPI / 86400.0)          # rad/s
        beta = np.sqrt(1.0 - self.ecc * self.ecc)
        theta2 = self.cosi * self.cosi

        # Brouwer recovery. The mean motion in a TLE is a Kozai mean; the
        # semi-major axis implied by it directly is wrong by several kilometres
        # and the period by a few seconds an orbit, which over a day is a
        # degree of along-track error and quite visible as a satellite arriving
        # at the wrong place.
        a1 = (MU / (n_kozai * n_kozai)) ** (1.0 / 3.0)
        d1 = 0.75 * J2 * (RE / a1) ** 2 * (3.0 * theta2 - 1.0) / beta ** 3
        a0 = a1 * (1.0 - d1 / 3.0 - d1 * d1 - (134.0 / 81.0) * d1 ** 3)
        d0 = 0.75 * J2 * (RE / a0) ** 2 * (3.0 * theta2 - 1.0) / beta ** 3
        self.n = n_kozai / (1.0 + d0)
        self.a = a0 / (1.0 - d0)

        # J2 secular rates about the mean orbit. p is the semi-latus rectum.
        p = self.a * (1.0 - self.ecc * self.ecc)
        k = 1.5 * J2 * (RE / p) ** 2 * self.n
        self.raandot = -k * self.cosi
        self.argpdot = k * (2.0 - 2.5 * self.sini ** 2)
        self.madot = self.n + k * beta * (1.0 - 1.5 * self.sini ** 2)
        # The TLE's n-dot/2, rev/day^2 -> rad/s^2, as a quadratic in mean
        # anomaly. It is the only drag term a Kepler propagator can carry, and
        # for a low satellite it is not decoration: the ISS at 4.6e-5 rev/day^2
        # is about 0.9 degrees of along-track over three days.
        self.drag = col("ndot2") * (TWOPI / 86400.0 ** 2)
        self.beta = beta
        self.ecc2 = self.ecc * self.ecc
        self.period = TWOPI / self.n[:, 0]
        # How hard Kepler's equation has to be worked. The series start below
        # is right to O(e^3), so one Newton step squares that and lands at
        # machine precision for anything near-circular -- which is the entire
        # roster, the worst of it being FO-29 at 0.035. A genuinely elliptical
        # orbit put in here would need the iterations, and gets them; the count
        # is fixed at build time so a frame's numpy call count never varies.
        self.kepler_steps = 1 if float(self.ecc.max()) < 0.10 else 6

    def eci(self, t):
        """Inertial position in km. `t` broadcasts against (n_sats, 1)."""
        dt = np.asarray(t, np.float64) - self.epoch
        ma = self.ma0 + self.madot * dt + self.drag * dt * dt
        ecc = self.ecc
        # The equation of the centre to second order in e, which for these
        # orbits is already better than the propagation it feeds. Written as
        # e^2 sin(M) cos(M) rather than (e^2/2) sin(2M) because sin(M) and
        # cos(M) are both wanted anyway and a second transcendental call over
        # the whole track is 80 us on the Pi that buys nothing.
        sm, cm = np.sin(ma), np.cos(ma)
        ea = ma + ecc * sm + self.ecc2 * sm * cm
        for _ in range(self.kepler_steps):
            ea = ea - (ea - ecc * np.sin(ea) - ma) / (1.0 - ecc * np.cos(ea))
        cos_ea, sin_ea = np.cos(ea), np.sin(ea)
        one_ec = 1.0 - ecc * cos_ea
        r = self.a * one_ec
        nu = np.arctan2(self.beta * sin_ea, cos_ea - ecc)
        u = self.argp0 + self.argpdot * dt + nu       # argument of latitude
        node = self.raan0 + self.raandot * dt
        cu, su = np.cos(u), np.sin(u)
        cn, sn = np.cos(node), np.sin(node)
        su_ci = su * self.cosi
        return (r * (cn * cu - sn * su_ci),
                r * (sn * cu + cn * su_ci),
                r * su * self.sini)

    def subpoint(self, t, sidereal=None):
        """(lat, lon, altitude) in degrees and km, geodetic.

        Geodetic rather than geocentric because that is what every "where is
        it" service quotes and what the test asserts against; the two differ by
        as much as 0.19 degrees, which is under a tenth of a row on this panel
        but the same size as the whole error budget of the propagation, so it
        is not worth throwing away for nothing.

        And it costs nothing. The textbook conversion is a fixed point solved
        by iterating an arctangent, which over a whole track is a dozen numpy
        calls; `phi_d = phi_c + (e^2/2) sin(2 phi_c) (Re/r)` is a first-order
        expansion of the same thing and agrees with the converged answer to
        0.0007 degrees over every altitude in the roster -- two hundred times
        finer than the propagator and a four-hundredth of a row. The ellipsoid
        radius underneath the altitude is the matching first-order form and is
        good to 25 metres.

        `sidereal` overrides GMST, for the caller that has already worked out
        that sidereal time over a forty-minute track is a straight line.
        """
        x, y, z = self.eci(t)
        g = gmst(t) if sidereal is None else sidereal
        lon = np.degrees(np.arctan2(y, x) - g)
        lon = np.mod(lon + 180.0, 360.0) - 180.0
        p = np.hypot(x, y)
        latc = np.arctan2(z, p)
        r = np.hypot(p, z)
        s = np.sin(latc)
        lat = latc + (E2 * RE) * s * np.cos(latc) / r
        return np.degrees(lat), lon, r - RE * (1.0 - FLAT * s * s)

    def look(self, t, site_ecef, up, east, north):
        """(elevation, azimuth, range) in degrees and km from one site."""
        x, y, z = self.eci(t)
        g = gmst(t)
        cg, sg = np.cos(g), np.sin(g)
        dx = x * cg + y * sg - site_ecef[0]
        dy = -x * sg + y * cg - site_ecef[1]
        dz = z - site_ecef[2]
        rng = np.sqrt(dx * dx + dy * dy + dz * dz)
        u = dx * up[0] + dy * up[1] + dz * up[2]
        e = dx * east[0] + dy * east[1] + dz * east[2]
        n = dx * north[0] + dy * north[1] + dz * north[2]
        return (np.degrees(np.arcsin(np.clip(u / rng, -1.0, 1.0))),
                np.degrees(np.arctan2(e, n)) % 360.0, rng)


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(az):
    return COMPASS[int((az % 360.0) / 22.5 + 0.5) % 16]


# --------------------------------------------------------------------------
# Passes over the site. Computed once in build() over the next day, because a
# pass prediction is a search over thousands of times and doing it per frame
# would be the single most expensive thing on the panel by two orders of
# magnitude. render() only picks the next one out of the list.
# --------------------------------------------------------------------------

def _cross(grid, elev, i, level):
    """Where the elevation crossed `level`, interpolated across one cell.

    `i` is the index on the far side of the crossing -- the first sample of a
    run for a rise, the first sample past it for a set -- so the cell to
    interpolate across is the same one either way. At the ends of the search
    window there is no such cell and the window's own edge is returned, which
    is what `clipped` on the pass exists to warn about.
    """
    j = i - 1
    if j < 0 or j + 1 >= len(grid):
        return float(grid[min(max(i, 0), len(grid) - 1)])
    lo, hi = float(elev[j]), float(elev[j + 1])
    if hi == lo:
        return float(grid[j])
    f = min(1.0, max(0.0, (level - lo) / (hi - lo)))
    return float(grid[j] + f * (grid[j + 1] - grid[j]))


def find_passes(el, now, site, hours=24.0, step=30.0, min_el=10.0, limit=24):
    """Passes above `min_el` in the next `hours`, soonest first.

    The coarse grid is 30 s, which cannot miss a pass: the shortest thing in
    the roster that clears ten degrees at all is above the horizon for four
    minutes. The peak is then refined on a two-second grid, because the coarse
    maximum can sit fifteen seconds off the real one and "MAX EL 68" wants to
    be the number a rotator would agree with.
    """
    ecef = geodetic_ecef(*site)
    lat, lon = math.radians(site[0]), math.radians(site[1])
    up = (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon),
          math.sin(lat))
    east = (-math.sin(lon), math.cos(lon), 0.0)
    north = (-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon),
             math.cos(lat))

    grid = now + np.arange(0.0, hours * 3600.0 + step, step)[None, :]
    elev = el.look(grid, ecef, up, east, north)[0]
    grid = grid[0]

    out = []
    for i in range(el.n_sats):
        above = elev[i] > min_el
        if not above.any():
            continue
        # Run boundaries. The pad on each end is what makes a pass already in
        # progress at `now` come out as a pass rather than being skipped for
        # having no rising edge.
        edges = np.flatnonzero(np.diff(np.concatenate(
            ([False], above, [False])).astype(np.int8)))
        for a, b in zip(edges[0::2], edges[1::2]):
            j = a + int(np.argmax(elev[i, a:b]))
            # Clamped to the window, which is not fussiness. A pass already in
            # progress has its coarse maximum in the first cell, and a fine
            # search that reaches half a minute back past `now` will happily
            # find the real peak *in the past* and report it as the one coming.
            fine = np.clip(grid[j] + np.arange(-step, step + 2.0, 2.0),
                           grid[0], grid[-1])
            fel, faz, _ = el.look(fine[None, :], ecef, up, east, north)
            k = int(np.argmax(fel[i]))
            peak_t, peak_el = float(fine[k]), float(fel[i, k])
            # A parabola through the three samples about the maximum, which is
            # the same refinement a correlator uses on a peak and costs three
            # scalars. Without it the answer is only as good as the grid, and
            # the grid is not good enough: a pass that goes nearly overhead
            # moves a degree of elevation a second, so two-second samples miss
            # the top by up to four tenths of a degree. With it the answer is
            # the true maximum to a ten-thousandth.
            if 0 < k < len(fine) - 1:
                y0, y1, y2 = (float(fel[i, k - 1]), peak_el, float(fel[i, k + 1]))
                den = y0 - 2.0 * y1 + y2
                if den < -1e-12:
                    f = 0.5 * (y0 - y2) / den
                    peak_t += f * (fine[k + 1] - fine[k])
                    peak_el = y1 - 0.25 * (y0 - y2) * f
            # Rise and set are interpolated across the cell they fall in
            # rather than taken from the grid. Elevation is smooth and nearly
            # straight through the horizon, so a straight line between the two
            # samples either side is good to a second or two, where the raw
            # grid time is up to half a minute out -- and half a minute is
            # exactly the difference between "IN 42M" and being wrong about it.
            rise = _cross(grid, elev[i], a, min_el)
            fall = _cross(grid, elev[i], b, min_el)
            out.append({"sat": i, "rise": rise, "set": fall,
                        "peak": peak_t, "max_el": peak_el,
                        "peak_az": float(faz[i, k]),
                        # True when the run ran into an end of the window, so
                        # its rise or set is the edge of the search and not a
                        # horizon crossing. The strip does not care; anything
                        # checking the geometry does.
                        "clipped": bool(a == 0 or b >= len(grid) - 1)})
    out.sort(key=lambda p: p["peak"])
    return out[:limit]


# --------------------------------------------------------------------------
# Reading what ftdata left behind.
# --------------------------------------------------------------------------

def read_elements(cache_dir=None, now=None):
    """(record, problem, stale). Never raises; `problem` is a string to print.

    Four states, and only two of them mean the same thing on the panel. No
    record at all means the fetcher has never run; a record that does not parse
    or carries no satellites means something upstream is broken; both of those
    are "no orbits". A record past its TTL is the third and it is different in
    kind, because unlike a stale tide curve -- which is visibly the wrong shape
    -- a stale orbit draws a perfectly plausible dot in the wrong place, and
    nothing on the panel would give it away. So it is refused outright, and
    `stale` is how the caller knows to say which of the two happened.
    """
    got = ftdata.load(PRODUCT, cache_dir)
    if got is None:
        return None, "no cached orbital elements", False
    payload, age = got
    try:
        sats = [s for s in payload.get("sats", [])
                if all(k in s for k in ("epoch", "n", "e", "i", "raan",
                                        "argp", "ma"))]
    except Exception:                                        # noqa: BLE001
        return None, "element record is malformed", False
    if not sats:
        return None, "element record carries no satellites", False
    if not ftdata.is_fresh(PRODUCT, age):
        return None, "elements are %s old, past their %s life" % (
            ftdata.describe_age(age),
            ftdata.describe_age(ftdata.ttl_for(PRODUCT) or 0)), True
    # The age that bounds the propagation is the age of the oldest element set,
    # not the age of the fetch. A group that has quietly started 404ing leaves
    # a freshly written record full of week-old elements, and this is the only
    # number that notices.
    now = time.time() if now is None else now
    epochs = [float(s["epoch"]) for s in sats]
    return ({"sats": sats, "age": age,
             "elem_age": max(0.0, now - min(epochs)),
             "missing": payload.get("missing") or []}, None, False)


# --------------------------------------------------------------------------
# The map.
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, w, h, lat_span, strip=True):
        self.w, self.h = w, h
        self.lat_span = float(lat_span)
        self.strip_h = 7 if (strip and h >= 24) else 0
        self.strip_y = h - self.strip_h
        self.deg_per_col = 360.0 / w
        self.deg_per_row = self.lat_span / h

    def col_of(self, lon):
        return (np.asarray(lon) + 180.0) * (self.w / 360.0)

    def row_of(self, lat):
        return (self.lat_span * 0.5 - np.asarray(lat)) / self.lat_span * self.h


def base_maps(lay):
    """(day, night) images of the world, before any terminator is applied.

    Two finished uint8 pictures rather than one picture and a palette, because
    the terminator then costs one blend between them instead of a per-pixel
    colour computation -- and the blend is the thing that has to be redone every
    couple of minutes for the rest of the wall's life.
    """
    a = coast_alpha(lay.w, lay.h, lay.lat_span)[:, :, None]
    day = (np.array(C_SEA_DAY, f32) * (1.0 - a)
           + np.array(C_COAST_DAY, f32) * a)
    night = (np.array(C_SEA_NIGHT, f32) * (1.0 - a)
             + np.array(C_COAST_NIGHT, f32) * a)

    # The equator, dotted. One line is worth a whole graticule here: it is the
    # only latitude anybody can name from the shape of the coast alone, and it
    # is what makes the squash legible as a squash.
    r = int(round(float(lay.row_of(0.0))))
    if 0 <= r < lay.h:
        for img in (day, night):
            np.maximum(img[r, ::4], np.array(C_GRID, f32), out=img[r, ::4])
    return day.astype(np.uint8), night.astype(np.uint8)


def sun_geometry(lay):
    """Per-pixel terms of the solar zenith angle, minus the sun's position.

    cos(z) = sin(lat) sin(dec) + cos(lat) cos(dec) cos(lon - sunlon), and the
    last factor expands into cos(lon)cos(sunlon) + sin(lon)sin(sunlon). So
    three whole-frame arrays baked here leave the terminator as three
    multiplies and two adds, with the sun's two numbers arriving as scalars.
    """
    lat = np.radians(lay.lat_span * 0.5
                     - (np.arange(lay.h, dtype=np.float64) + 0.5)
                     * lay.deg_per_row)[:, None]
    lon = np.radians(-180.0 + (np.arange(lay.w, dtype=np.float64) + 0.5)
                     * lay.deg_per_col)[None, :]
    coslat = np.cos(lat)
    return (np.broadcast_to(np.sin(lat), (lay.h, lay.w)).astype(f32).copy(),
            (coslat * np.cos(lon)).astype(f32),
            (coslat * np.sin(lon)).astype(f32))


# The twilight band. cos(z) = 0 is the geometric terminator; these put the
# ramp between about +5 and -9 degrees of solar elevation, which is roughly
# civil twilight and, more to the point, is about six columns wide -- narrow
# enough to read as an edge and wide enough not to alias into a staircase.
TWI_HI, TWI_LO = 0.09, -0.16


def draw_site(dst, lay, lat, lon, name):
    """A small cross where the wall is, with its name beside it."""
    r = int(round(float(lay.row_of(lat))))
    c = int(round(float(lay.col_of(lon)))) % lay.w
    if not (0 <= r < lay.h):
        return
    for dr, dc in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
        rr, cc = r + dr, (c + dc) % lay.w
        if 0 <= rr < lay.h:
            dst[rr, cc] = C_SITE
    x = c + 4
    if x + text_width(name) > lay.w:
        x = c - 4 - text_width(name)
    blit_text(dst, min(max(0, r - 2), lay.h - 5), x, name, C_SITE)


# --------------------------------------------------------------------------
# Words for the bottom strip.
# --------------------------------------------------------------------------

def duration(seconds):
    """'45S', '42M', '2H14' -- short enough to sit next to three other fields."""
    s = max(0.0, float(seconds))
    if s < 90:
        return "%dS" % int(s)
    if s < 3600:
        return "%dM" % int(round(s / 60.0))
    return "%dH%02d" % (int(s // 3600), int((s % 3600) // 60))


def pass_words(state, lay):
    """(left, colour, right) for the strip, widest form that fits.

    A ladder of shorter phrasings rather than a clip, for the same reason
    tide.py has one: what falls off the end of a clipped status line is the
    right-hand field, and the right-hand field here is the age of the elements,
    which is the only thing on the panel that says whether the rest of it is
    true.
    """
    up = state["up_now"]
    right_forms = ["UP %d  ELEM %s" % (up, state["elem_age_text"]),
                   "ELEM %s" % state["elem_age_text"],
                   state["elem_age_text"]]

    p = state["pass"]
    if p is None:
        lefts = ["NO %s PASS ABOVE %d IN 24H" % (SITE_NAME, state["min_el"]),
                 "NO PASS IN 24H", "NO PASS"]
        colour = C_DIM
    elif p["rise"] <= state["now"] < p["set"]:
        el = state["live_el"]
        colour = C_GO
        lefts = ["%s NOW  %s EL %d %s  SETS %s"
                 % (SITE_NAME, p["label"], int(round(el)),
                    compass(state["live_az"]), duration(p["set"] - state["now"])),
                 "%s EL %d  SETS %s" % (p["label"], int(round(el)),
                                        duration(p["set"] - state["now"])),
                 "%s EL %d" % (p["label"], int(round(el)))]
    else:
        colour = C_TEXT
        lefts = ["%s NEXT  %s IN %s  MAX EL %d %s"
                 % (SITE_NAME, p["label"], duration(p["rise"] - state["now"]),
                    int(round(p["max_el"])), compass(p["peak_az"])),
                 "%s IN %s  EL %d" % (p["label"],
                                      duration(p["rise"] - state["now"]),
                                      int(round(p["max_el"]))),
                 "%s IN %s" % (p["label"], duration(p["rise"] - state["now"]))]

    for left in lefts:
        for right in right_forms:
            if text_width(left) + text_width(right) + 8 <= lay.w:
                return left, colour, right
    return lefts[-1], colour, ""


def draw_nodata(dst, lay, lines):
    """The honest panel: no map, no dots, no implication of a position."""
    dst[:] = (6, 6, 8)
    scale = 2 if lay.h >= 32 and lay.w >= 200 else 1
    y = max(0, lay.h // 2 - (len(lines) * (6 * scale + 2)) // 2)
    for i, (s, rgb) in enumerate(lines):
        sc = scale if i == 0 else 1
        x = max(0, (lay.w - text_width(s, sc)) // 2)
        blit_text(dst, y, x, s, rgb, sc)
        y += 5 * sc + 3
    return dst


# --------------------------------------------------------------------------
# Options.
# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--cache-dir", default=None,
                    help="ftdata cache to read (default: ftdata's own)")
    ap.add_argument("--site", default="%.4f,%.4f" % (SITE_LAT, SITE_LON),
                    help="observer as lat,lon; passes are computed for this")
    ap.add_argument("--site-name", default=SITE_NAME,
                    help="what to call it on the panel")
    ap.add_argument("--lat-span", type=float, default=180.0,
                    help="degrees of latitude across the panel; 180 is the "
                         "whole Earth squashed, 72 is true scale and cropped")
    ap.add_argument("--track-minutes", type=float, default=20.0,
                    help="ground track drawn either side of each satellite")
    ap.add_argument("--track-samples", type=int, default=121,
                    help="points per track; enough that it reads as a line")
    ap.add_argument("--min-el", type=float, default=10.0,
                    help="degrees of elevation that count as a pass")
    ap.add_argument("--footprint", default="pass",
                    choices=("pass", "iss", "none"),
                    help="whose visibility circle to draw: the satellite the "
                         "strip is talking about, the ISS, or nobody's")
    ap.add_argument("--label", default="ISS",
                    help="comma-separated labels to name on the map")
    ap.add_argument("--no-strip", dest="strip", action="store_false",
                    default=True, help="drop the pass strip, all map")
    ap.add_argument("--at", default="now",
                    help="pretend the present moment is this (epoch or "
                         "'YYYY-MM-DD HH:MM' local)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="displayed seconds per real second; 600 walks a "
                         "whole orbit past in nine seconds and 0 stops the "
                         "clock dead, which is how the screenshot is taken")
    ap.add_argument("--reload", type=float, default=1800.0,
                    help="seconds between re-reads of the cache (0 = never)")


def parse_site(s):
    lat, lon = [float(x) for x in s.replace(" ", "").split(",")]
    return lat, lon


# --------------------------------------------------------------------------
# build()
# --------------------------------------------------------------------------

def build(args):
    w, h = args.width, args.height
    lay = Layout(w, h, args.lat_span, args.strip)
    site = parse_site(args.site)
    site_ecef = geodetic_ecef(*site)
    slat, slon = math.radians(site[0]), math.radians(site[1])
    up = (math.cos(slat) * math.cos(slon), math.cos(slat) * math.sin(slon),
          math.sin(slat))
    east = (-math.sin(slon), math.cos(slon), 0.0)
    north = (-math.sin(slat) * math.cos(slon), -math.sin(slat) * math.sin(slon),
             math.cos(slat))
    now_of = clock(parse_when(args.at), args.rate)
    cache = args.cache_dir
    global SITE_NAME
    SITE_NAME = args.site_name.upper()

    day_img, night_img = base_maps(lay)
    sun_a, sun_b, sun_c = sun_geometry(lay)
    frame = np.zeros((h, w, 3), np.uint8)
    static = np.zeros((h, w, 3), np.uint8)
    flat = frame.reshape(-1, 3)

    # The footprint is drawn as a ring of points rather than as a polyline,
    # which is what makes the antimeridian free: a point wraps with a modulo
    # and a line would need clipping into two pieces at the seam.
    ring = np.linspace(0.0, TWOPI, 96, endpoint=False)[None, :]
    ring_cos, ring_sin = np.cos(ring), np.sin(ring)

    labels = set(x.strip().upper() for x in args.label.split(",") if x.strip())

    cell = {"rec": None, "el": None, "passes": [], "problem": None,
            "loaded": -1e18, "sun_key": None, "strip_key": None,
            "strip_img": None, "strip_mask": None, "state": None,
            "stale": False, "focus": 0}

    def reload_data(now):
        rec, problem, stale = read_elements(cache, now)
        cell["rec"], cell["problem"] = rec, problem
        # A record that exists and is too old is a different sentence from no
        # record at all -- one means the fetcher stopped, the other means it
        # never ran -- and the two want different things done about them.
        cell["stale"] = stale
        cell["loaded"] = now
        cell["sun_key"] = None
        cell["strip_key"] = None
        if rec is None:
            cell["el"] = None
            cell["passes"] = []
            return
        el = Elements(rec["sats"])
        cell["el"] = el
        cell["passes"] = find_passes(el, now, site, min_el=args.min_el)

        # Everything the drawing needs per satellite, baked once: the track
        # sample offsets, and a finished uint8 colour for every point of every
        # track. Per frame the whole thing is then one scatter of a constant
        # array rather than a fade computed over and over.
        m = max(3, int(args.track_samples) | 1)          # odd: `now` is centre
        span = float(args.track_minutes) * 60.0
        cell["toff"] = np.linspace(-span, span, m)[None, :]
        cell["sid"] = cell["toff"] * OMEGA_EARTH
        cell["mid"] = m // 2
        s = np.linspace(-1.0, 1.0, m)
        # Ahead of the satellite is brighter than behind it: the track is a
        # prediction and the useful half is the half that has not happened yet.
        fade = np.where(s >= 0.0, 0.85, 0.45) * (1.0 - 0.72 * np.abs(s))
        base = np.array([C_KIND.get(sat.get("kind"), C_KIND_DEFAULT)
                         for sat in rec["sats"]], f32)
        cell["track_rgb"] = (base[:, None, :] * fade[None, :, None]) \
            .astype(np.uint8).reshape(-1, 3)
        cell["dot_rgb"] = base.astype(np.uint8)
        cell["glow_rgb"] = np.tile((base * 0.42).astype(np.uint8), (4, 1))
        cell["ring_rgb"] = base * 0.30
        cell["label_at"] = [i for i, sat in enumerate(rec["sats"])
                            if sat["label"].upper() in labels]
        cell["label_txt"] = [rec["sats"][i]["label"] for i in cell["label_at"]]

    def rebuild_static(now):
        """The map with today's terminator on it. Costs a dozen whole-frame
        passes and happens once every couple of minutes, when the subsolar
        point has moved half a column."""
        dec, sunlon = sun_subpoint(now)
        d, l = math.radians(dec), math.radians(sunlon)
        cosz = (sun_a * math.sin(d) + sun_b * (math.cos(d) * math.cos(l))
                + sun_c * (math.cos(d) * math.sin(l)))
        night = np.clip((TWI_HI - cosz) / (TWI_HI - TWI_LO), 0.0, 1.0)[:, :, None]
        np.copyto(static, (day_img * (1.0 - night)
                           + night_img * night).astype(np.uint8))
        draw_site(static, lay, site[0], site[1], SITE_NAME)

    def sample_state(now):
        """The numbers the strip talks about. Asked twice a second, not 20."""
        el = cell["el"]
        rec = cell["rec"]
        nxt = None
        for p in cell["passes"]:
            if p["set"] > now:
                nxt = dict(p)
                nxt["label"] = rec["sats"][p["sat"]]["label"]
                break
        elev, az, _ = el.look(now, site_ecef, up, east, north)
        elev, az = elev[:, 0], az[:, 0]
        st = {"now": now, "pass": nxt, "min_el": int(round(args.min_el)),
              "up_now": int((elev > args.min_el).sum()),
              "live_el": float(elev[nxt["sat"]]) if nxt else 0.0,
              "live_az": float(az[nxt["sat"]]) if nxt else 0.0,
              "elev": elev, "az": az,
              "elem_age_text": ftdata.describe_age(rec["elem_age"]),
              "age": rec["age"], "elem_age": rec["elem_age"]}
        if args.footprint == "none":
            st["focus"] = None
        elif args.footprint == "iss" or nxt is None:
            st["focus"] = cell["label_at"][0] if cell["label_at"] else 0
        else:
            st["focus"] = nxt["sat"]
        return st

    def rebuild_strip(state):
        left, colour, right = pass_words(state, lay)
        img = np.zeros((lay.strip_h, lay.w, 3), np.uint8)
        blit_text(img, 1, 2, left, colour)
        if right:
            rw = text_width(right)
            blit_text(img, 1, lay.w - rw - 2, right,
                      C_WARN if state["elem_age"] > 2.0 * 86400 else C_DIM)
        cell["strip_img"] = img
        cell["strip_mask"] = img.any(axis=2)

    def render(t, i=0):
        now = now_of()
        if args.reload and now - cell["loaded"] >= args.reload:
            reload_data(now)

        if cell["el"] is None:
            lines = [("ORBITS TOO OLD" if cell["stale"] else "NO ORBITS",
                      C_WARN),
                     ("RUN  PYTHON3 FTDATA.PY --ONCE --ONLY SATS", C_TEXT)]
            if cell["problem"]:
                lines.append((cell["problem"].upper()[:56], C_DIM))
            return draw_nodata(frame, lay, lines)

        # Half a column of subsolar longitude is 135 s of wall time, so this
        # rebuild lands on one frame in two and a half thousand.
        key = int((now / 240.0) * (1440.0 / lay.w) * 2.0)
        if key != cell["sun_key"]:
            rebuild_static(now)
            cell["sun_key"] = key

        el = cell["el"]
        lat, lon, alt = el.subpoint(now + cell["toff"],
                                    gmst_at(now) + cell["sid"])
        rows = np.clip(lay.row_of(lat).astype(np.int32), -1, lay.h)
        cols = lay.col_of(lon).astype(np.int32) % lay.w

        np.copyto(frame, static)

        mid = cell["mid"]
        state = cell["state"]
        focus = state["focus"] if state else None

        # The footprint first, so the track and the dot sit on top of it.
        if focus is not None:
            rho = math.acos(RE / (RE + max(1.0, float(alt[focus, mid]))))
            fl = math.radians(float(lat[focus, mid]))
            fo = math.radians(float(lon[focus, mid]))
            sin_lat2 = (math.sin(fl) * math.cos(rho)
                        + math.cos(fl) * math.sin(rho) * ring_cos)
            lat2 = np.arcsin(np.clip(sin_lat2, -1.0, 1.0))
            lon2 = fo + np.arctan2(
                ring_sin * math.sin(rho) * math.cos(fl),
                math.cos(rho) - math.sin(fl) * sin_lat2)
            # Wrapped as (x + 180) % 360 - 180 and not as x % 360 - 180, which
            # is the same thing for a positive longitude and half a world out
            # for a negative one -- and puts a beautifully drawn footprint
            # around nothing at all, on the wrong side of the map.
            rr = lay.row_of(np.degrees(lat2)).astype(np.int32)
            cc = lay.col_of((np.degrees(lon2) + 180.0) % 360.0
                            - 180.0).astype(np.int32)
            ok = (rr >= 0) & (rr < lay.h)
            idx = (rr * lay.w + cc % lay.w)[ok]
            flat[idx] = cell["ring_rgb"][focus]

        ok = (rows >= 0) & (rows < lay.h)
        flat[(rows * lay.w + cols)[ok]] = cell["track_rgb"].reshape(
            rows.shape + (3,))[ok]

        # The dot, with a dim cross around it. A single lit pixel on a map this
        # dark reads as a star; five read as an object.
        r0, c0 = rows[:, mid], cols[:, mid]
        glow_r = np.concatenate([r0 - 1, r0 + 1, r0, r0])
        glow_c = np.concatenate([c0, c0, c0 - 1, c0 + 1]) % lay.w
        gok = (glow_r >= 0) & (glow_r < lay.h)
        flat[(glow_r * lay.w + glow_c)[gok]] = cell["glow_rgb"][gok]
        dok = (r0 >= 0) & (r0 < lay.h)
        flat[(r0 * lay.w + c0)[dok]] = cell["dot_rgb"][dok]

        for k, name in zip(cell["label_at"], cell["label_txt"]):
            if not dok[k]:
                continue
            x = int(c0[k]) + 3
            if x + text_width(name) > lay.w:
                x = int(c0[k]) - 3 - text_width(name)
            y = min(max(0, int(r0[k]) - 2), lay.h - 5 - lay.strip_h)
            blit_text(frame, y, x, name, cell["dot_rgb"][k])

        if lay.strip_h:
            skey = int(now // 2)
            if skey != cell["strip_key"]:
                cell["state"] = state = sample_state(now)
                rebuild_strip(state)
                cell["strip_key"] = skey
            reg = frame[lay.strip_y:]
            reg //= 5
            reg[cell["strip_mask"]] = cell["strip_img"][cell["strip_mask"]]
        return frame

    reload_data(now_of())
    if cell["el"] is not None and lay.strip_h:
        cell["state"] = sample_state(now_of())
        rebuild_strip(cell["state"])
        cell["strip_key"] = int(now_of() // 2)
    render.cell = cell                # tests reach in here; nothing else does
    render.layout = lay
    render.clock = now_of
    render.site = site
    return render


def main():
    # 20 fps. The subsatellite point of the ISS moves 0.06 px a second, so the
    # panel is not animating anything fast -- but the frame is cheap and 20 is
    # what makes the scheduler's crossfades in and out of it smooth.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
