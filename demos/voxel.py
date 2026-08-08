#!/usr/bin/env python3
"""A hang glider's tour of San Francisco Bay, in voxel space.

Comanche-style terrain: for every screen column, march a ray out along the
ground, look up the height of the real San Francisco Bay under it, and work
out how far up the screen that lands. The nearest thing wins. It is the oldest
trick for drawing landscape in real time and it is still the right one for a
320x64 panel, because the cost is set by the number of columns and the depth
budget rather than by any amount of geometry, and because a heightmap of the
Bay Area is a 200 kB file.

The terrain is not noise. It is USGS 3DEP elevation over a 35 x 44 km box
holding Mount Tamalpais, the Marin Headlands, the strait, San Francisco out to
Twin Peaks and San Bruno Mountain, Angel Island, Alcatraz, Yerba Buena and the
Berkeley hills -- baked into `voxel-dem.npz` by `scripts/make-voxel-dem.py`,
which is where the provenance is written down. Sea level is stored as exactly
zero, so the Bay and the Pacific are a comparison rather than a second map.

The flier does not circle. It flies a tour: in off the Pacific with the Gate
opening ahead, through it *between the towers* and under the cables, east
along the city front with Alcatraz opening to port, out toward Treasure Island
and the Bay Bridge, north up the bay past Angel Island and round its north end,
back south-west over Sausalito and up over Hawk Hill with Tamalpais on the
horizon, then out past Point Bonita to the open sea and round. Twenty-nine
kilometres of it, and every heading has something in it -- which is the whole
point, because a circle over open water reads as rotation rather than as going
anywhere.

Which is also the one dishonest thing here, said out loud: twenty-nine
kilometres in three and a half minutes is 138 m/s, about eleven times what a
hang glider does. A wing that stayed honest to 13 m/s would need thirty-seven
minutes to fly this, and nobody is watching that. `--loop` is right there.

Two bridges and one mast stand in the landscape as objects, depth-tested
against it: the Golden Gate, which you fly through; the Bay Bridge's western
crossing, a silver line low on the haze with two nubs on it; and Sutro Tower,
which is five pixels of trident on the ridge behind the city. That is what
they look like from here, and making them any more legible than that would
mean making them wrong.

Run:  python3 voxel.py --host 127.0.0.1
      python3 voxel.py --light dusk --fog 1.4
      python3 voxel.py --loop 420 --altitude -60
      python3 voxel.py --coarse            # half-width landscape, half the frame
      python3 voxel.py --no-tower --birds 0 --steps 96
      python3 voxel.py --wing
"""

import math
import os
import sys
from bisect import bisect_right

import numpy as np

import demoscene as ds

f32 = ds.f32

DEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voxel-dem.npz")

# --------------------------------------------------------------------------
# What a frame costs, on the Pi 3 this has to run on, and the three numpy
# facts the rest of the file is arranged around. None of them shows in the
# code that uses them and between them they are worth about eighteen
# milliseconds a frame here, so they are written down rather than left to be
# rediscovered.
#
# Measured on that machine: a numpy call costs 55 to 80 us whatever size the
# array is, and one pass over a float32 array costs about 21 ns an element. So
# a frame is roughly (calls x 60 us) + (elements touched x 20 ns), and both
# terms are worth real effort.
#
# The dtype matters more than it should, and it is the biggest single lever
# left in this file. Per element, on the Pi's numpy: float32 21 ns, int32
# 5 ns, int16 4 ns, a cast between them 6 ns, and a scattered gather 60 ns --
# so an integer pass is *four* times cheaper than a float one, not the three
# this note used to claim, and it is worth casting early to get one. The two
# running scans are the extreme case, because they cannot be vectorised across
# the axis they run down: np.minimum.accumulate over the depth grid is 1.37 ms
# in float32, 1.88 in int32 and 0.64 in int16. Where a value is a screen row,
# a bin number or a step index it is carried as a short, and the ceiling, the
# clamp and the narrowing are all done *before* the scan rather than after --
# which is free to do, since every one of them is monotonic.
#
# On top of that, a *dispatched* numpy function -- anything routed through
# __array_function__, which is np.copyto, np.take, np.cumsum, np.min,
# np.searchsorted and most of the namespace that is not a ufunc -- pays 25 to
# 70 us more on the way in, and there were forty of them in a frame. Where the
# same thing exists as an ndarray method it is written that way instead:
# `a.cumsum(...)`, `x.min()`, `pal.take(...)`, `ok.nonzero()`, and `a[...] = b`
# for np.copyto. Ufuncs -- np.multiply, np.add, np.rint, np.minimum and the
# rest -- are not dispatched, and are used freely.
#
# And two named cases. np.clip() has two Python-level deprecation checks
# standing in front of it that cost a quarter of a millisecond a call, more
# than the clip does; the ufunc underneath also does in one pass what a
# maximum/minimum pair does in two. It has moved namespace once already, so it
# is fetched by name with the pair behind it as a fallback. And np.take
# defaults to mode="raise", which bounds-checks every index with the error
# path inline: every index here is in range by construction, so mode="clip" --
# which therefore cannot give a different answer -- is 25% to 40% quicker.
#
# And one trap that costs nothing to avoid and a great deal not to.
# np.putmask cannot write through a non-contiguous array: handed one it makes
# a C-contiguous copy, puts into the copy, and writes it back on the way out.
# It is correct and it is silent, and it means a putmask into a sub-rectangle
# of the frame secretly costs two copies of that rectangle. The bridges do
# four of those into the same box, so the box is gathered into a contiguous
# scratch once and written back once instead. See draw_bridge().
#
# Finally, the version. All of the above is numpy 1.19.5, which is what
# Raspberry Pi OS ships and what the wall runs. The same file on the same
# machine under numpy 2.0.2 is about 15% quicker for nothing at all -- 2.x
# rebuilt the ufunc loops and made dispatch cheap -- so if the wall is ever
# upgraded, every number in this file is pessimistic and none of the code
# needs to change.
# --------------------------------------------------------------------------

try:
    _umath = np._core._multiarray_umath   # numpy >= 2
except AttributeError:
    try:
        _umath = np.core._multiarray_umath                     # numpy < 2
    except AttributeError:                # pragma: no cover - moved again
        _umath = None

try:
    _clip = _umath.clip
except AttributeError:                    # pragma: no cover - moved again
    def _clip(a, lo, hi, out=None):
        out = np.maximum(a, lo, out=out)
        return np.minimum(out, hi, out=out)

# The rest of the dispatcher dodge. np.putmask, np.bincount and np.interp are
# thin Python shims that numpy decorates with __array_function__ and then hands
# straight back to the C function underneath -- so the C function is what is
# called here. It is the same code doing the same work; what is skipped is the
# dispatch, which on the Pi's numpy costs about a third of a millisecond a call
# and happens eight or nine times a frame. Each one falls back to the public
# name if numpy ever stops exporting it.
_putmask = getattr(_umath, "putmask", None) or np.putmask
_bincount = getattr(_umath, "bincount", None) or np.bincount
# np.interp's shim also asks whether fp is complex and converts everything to
# float64 before calling this; the tables are baked float64 so only `x` is left
# to convert, which the C function does itself. `period` is what the shim is
# really for and nothing here uses it.
_cinterp = getattr(_umath, "interp", None)
if _cinterp is None:                      # pragma: no cover - moved again
    def _interp(x, xp, fp):
        return np.interp(x, xp, fp)
else:
    def _interp(x, xp, fp):
        return _cinterp(x, xp, fp, None, None)

# --------------------------------------------------------------------------
# The bridges.
#
# Two of them, and they are described rather than drawn: a table of real
# dimensions, a real position and bearing, and a colour, from which build()
# bakes a height profile and render() paints it. One compositor handles both,
# and would handle a third.
#
# The Golden Gate is in metres converted from the feet goldengate.py works in
# -- 4200 ft of main span between the towers, 1125 ft of side span either
# side, 526 ft of tower over a deck 220 ft above the water -- so the two demos
# cannot drift apart.
#
# The Bay Bridge is the western crossing, and the whole point of including it
# is that it is *not* a second Golden Gate: two suspension spans back to back
# meeting at a central anchorage, which gives a tower-cable-tower-anchorage-
# tower-cable-tower silhouette nothing like a single span, and silver-grey
# steel rather than International Orange. At the eleven kilometres it is seen
# from here the colour carries most of the recognition. Its eastern span is
# not modelled: it is a further two kilometres behind Yerba Buena Island and
# at this range would be under a pixel tall behind the island that hides it.
#
# Dimensions, feet: main spans 2310 either side of the central anchorage, side
# spans 1160, towers 519 above the water, upper deck 220 above the water.
# --------------------------------------------------------------------------

FT = 0.3048

BRIDGES = [
    dict(
        name="golden gate",
        # The strait runs roughly east-west and the bridge crosses it a few
        # degrees off true north, the Marin end very slightly west of the Fort
        # Point end.
        lat=37.8199, lon=-122.4783, bearing=353.0,
        deck=220.0 * FT, tower=(220.0 + 526.0) * FT, anchor=26.0,
        camber=11.0, thick=16.0, depth=11.0,
        # Anchorage, tower, tower, anchorage. `sag` is where the cable hangs
        # to between one support and the next: on the main span the vertex
        # sits *on* the deck at midspan, which is the one detail that makes a
        # silhouette read as this bridge and not any suspension bridge, and it
        # is the same call goldengate.py makes.
        spans=[1125.0 * FT, 4200.0 * FT, 1125.0 * FT],
        sag=[None, "deck", None],
        rgb=((198.0, 70.0, 38.0), (126.0, 52.0, 30.0), (232.0, 104.0, 56.0))),
    dict(
        name="bay bridge west",
        # Rincon Hill to Yerba Buena Island: 37.7908 N 122.3880 W to
        # 37.8080 N 122.3640 W, which is a bearing of 48 degrees and a
        # midpoint of 37.7994 N 122.3760 W. Only the suspended 6940 ft is
        # modelled; the approach viaducts at either end are ordinary elevated
        # roadway and do not survive the range.
        lat=37.7994, lon=-122.3760, bearing=48.0,
        deck=220.0 * FT, tower=519.0 * FT, anchor=30.0,
        camber=8.0, thick=14.0, depth=14.0,
        # Two main spans meeting at the central anchorage, which is a squat
        # concrete block the cables die into well below the tower tops -- so
        # it is a support like the towers, just a much shorter one.
        spans=[1160.0 * FT, 2310.0 * FT, 2310.0 * FT, 1160.0 * FT],
        sag=[None, "deck", "deck", None],
        centre=125.0,
        rgb=((178.0, 182.0, 192.0), (104.0, 108.0, 120.0),
             (208.0, 212.0, 220.0))),
]

# --------------------------------------------------------------------------
# And one mast.
#
# Sutro Tower, 298 m of it standing on a 255 m ridge, which makes the top of
# it the highest thing in San Francisco by a couple of hundred metres and the
# only part of the city that is legible from the far side of the bay. The
# skyline itself was tried and left out: downtown from six kilometres is four
# rows of very slightly lighter grey against a hazy hill, which is to say it
# is nothing, and drawing it bigger would be drawing something that is not
# there. The tower is different -- it is a shape, and the shape is the whole
# recognition.
#
# The sprite is sunset.py's, unchanged, for the same reason the Golden Gate's
# dimensions are goldengate.py's: three prongs on a lattice body with flanged
# platforms stepping out to a splayed tripod base. Without the trident top and
# the stepped taper it is any old transmitter mast; with them it is Sutro at a
# handful of pixels. It is drawn fatter than life -- the real tower is about
# six times taller than it is wide and this is closer to one and a half --
# because at the range it is seen from here a faithful width is under a pixel
# and a tower that keeps dropping out between columns reads as a flicker
# rather than as accuracy. That is the same call the bridge towers get.
# --------------------------------------------------------------------------

SUTRO = [
    "      X      ",
    "   X  X  X   ",
    "   X  X  X   ",
    "   X  X  X   ",
    "   X  X  X   ",
    "  XXXXXXXXX  ",
    "   X  X  X   ",
    "   X  X  X   ",
    "  XXXXXXXXX  ",
    "   X  X  X   ",
    "   X  X  X   ",
    "  XXXXXXXXX  ",
    "  X   X   X  ",
    "  X   X   X  ",
    " XXXXXXXXXXX ",
    " X    X    X ",
    " X    X    X ",
    "XXXXXXXXXXXXX",
    "X     X     X",
    "X     X     X",
]

MASTS = [
    dict(name="sutro tower", lat=37.7552, lon=-122.4528, height=298.0,
         art=SUTRO, rgb=(104.0, 84.0, 82.0)),
]

# The size range it is drawn at. Below five rows the trident is gone and what
# is left is a three-pixel smudge; above twenty-two it would be bigger than the
# source art and only upscaled mush. The lower bound was six for a while, which
# was the size at which it still reads properly -- and that was wrong, because
# the tower crosses that threshold *during* a pass and dropped out for a single
# frame on the way, which is a blink. A landmark that is slightly too small is
# better than one that flickers, so the bound is where it stops being drawn at
# all rather than where it stops being pretty.
#
# From most of the tour Sutro is either behind you or too far, so it is on
# screen for about fourteen seconds a loop, in two passes.
MAST_MIN, MAST_MAX = 5, 22

# --------------------------------------------------------------------------
# The palette is one flat table and every pixel on screen is one index into
# it. The layout is arithmetic rather than a lookup:
#
#   surface:  (class * NSHADE + shade) * NFOG + haze band
#   sky:      SKY0 + rows above the horizon
#
# so brightening a water pixel one step is +NFOG, hazing anything is a change
# of the low digits, and the bridge is a class like any other. Nothing in the
# frame ever computes an RGB value; the last thing it does is one gather.
# --------------------------------------------------------------------------

NSHADE = 12                 # hillshade levels per surface class
NFOG = 20                   # distance-haze levels
# The sky ramp is indexed by how far a pixel is above the horizon, in *thirds*
# of a row. Whole rows was the obvious thing and it was visibly wrong: the
# horizon shears with the bank, so the index steps by one somewhere along the
# width, and a one-entry step in a gradient this smooth draws a vertical seam
# right down the panel. At a third of a row the seams are finer than the
# dither and disappear.
SKY_SUB = 3
NSKY = 3 * 176              # sky ramp entries
SKY_MID = 3 * 112           # which entry of that ramp sits on the horizon
SKY_SPAN = 3 * 40.0         # thirds of a row from the horizon to the top colour

# Surface classes. Water is class 0 and must stay there: the per-frame water
# treatment is the single compare `index < NSHADE * NFOG`, which only works
# because water owns the bottom of the table.
CLS_WATER, CLS_LOW, CLS_MID, CLS_HIGH = 0, 1, 2, 3
CLS_BIRD = 4
CLS_MAST = 5                # Sutro; one flat colour, it is five pixels tall
# Each bridge owns three consecutive classes -- tower, deck, cable -- starting
# here, so a structure's palette entries are one base number and the
# compositor paints `base + part` without knowing which bridge it is drawing.
CLS_STEEL0 = 6
NPART = 3                   # tower, deck, cable
P_TOWER, P_DECK, P_CABLE = 0, 1, 2
NCLS = CLS_STEEL0 + NPART * len(BRIDGES)

# Water shades are not a hillshade. Shade 0 is the flat surface, 1 the chop,
# 2 and 3 the sun's glitter -- reserved so the per-frame bump can be an
# integer add that cannot climb out of the class.
W_FLAT, W_CHOP, W_GLINT = 0, 1, 2

SKY0 = NCLS * NSHADE * NFOG

# Times of day. The sun azimuth and elevation feed the hillshade *and* where
# the sun is drawn, so the long shadows and the glare always agree.
LIGHTS = {
    # The default, and what the demo is for: an hour before sunset, sun out
    # over the Pacific and a little south, which is the light that picks out
    # the west faces of the Headlands and lays the glitter across the strait.
    "golden": dict(
        sky=[(0.00, (16, 42, 112)), (0.40, (70, 92, 158)),
             (0.72, (196, 138, 122)), (1.00, (250, 190, 128))],
        haze=(196, 152, 120), sun=(255, 232, 168), glow=(255, 168, 96),
        sun_az=254.0, sun_el=13.0,
        water=(20, 32, 58), chop=(28, 41, 69), glint=(255, 214, 150),
        land=[(46, 54, 40), (68, 70, 46), (94, 88, 58)],
        ambient=0.42, diffuse=0.92, warm=(1.20, 0.96, 0.68)),
    # First light from behind Diablo: cold, pink, the water nearly black.
    "dawn": dict(
        sky=[(0.00, (8, 14, 48)), (0.40, (36, 42, 94)),
             (0.74, (130, 94, 130)), (1.00, (214, 142, 132))],
        haze=(146, 110, 116), sun=(255, 208, 186), glow=(226, 118, 110),
        sun_az=74.0, sun_el=11.0,
        water=(12, 18, 40), chop=(28, 36, 62), glint=(226, 176, 168),
        land=[(30, 36, 42), (44, 48, 50), (62, 62, 62)],
        ambient=0.36, diffuse=0.86, warm=(1.14, 0.88, 0.86)),
    # Flat overhead light: the least interesting and the most legible, which
    # is why it is here. It is the one to switch to when the geometry looks
    # wrong, because nothing is hiding in a shadow.
    "noon": dict(
        sky=[(0.00, (24, 72, 166)), (0.45, (70, 124, 198)),
             (0.80, (148, 182, 216)), (1.00, (196, 212, 224))],
        haze=(162, 182, 200), sun=(255, 255, 240), glow=(220, 232, 245),
        sun_az=196.0, sun_el=58.0,
        water=(30, 62, 104), chop=(46, 80, 122), glint=(240, 248, 255),
        land=[(58, 74, 46), (84, 92, 52), (112, 108, 66)],
        ambient=0.46, diffuse=0.72, warm=(1.02, 1.02, 1.00)),
    # Just after the sun has gone. Everything is silhouette and the water
    # holds the last of the light -- the prettiest of the four on an LED wall
    # and the hardest to keep out of the mud.
    "dusk": dict(
        sky=[(0.00, (6, 12, 40)), (0.38, (30, 32, 82)),
             (0.72, (108, 60, 104)), (1.00, (206, 104, 88))],
        haze=(138, 84, 82), sun=(255, 176, 120), glow=(232, 108, 78),
        sun_az=282.0, sun_el=2.0,
        water=(14, 18, 44), chop=(28, 30, 58), glint=(238, 152, 120),
        land=[(24, 26, 34), (36, 36, 42), (54, 50, 54)],
        ambient=0.34, diffuse=0.74, warm=(1.22, 0.82, 0.64)),
}

# The one number that turns the flight path into a bank angle. It converts the
# rate of turn (rad/s, and scale-invariant -- it does not depend on how big the
# circuit is) into the tangent of a displayed roll, and it is measured rather
# than chosen: it puts the 95th percentile of the turn rate exactly on the
# limiter's knee. It was 22.0 once, which is fifty times too big -- a signal 40
# to 60 times the clamp, so 99.4% of the loop sat pinned hard over at one limit
# or the other and the horizon flipped between them as a square wave.
ROLL_GAIN = 0.82
# Six degrees. The turns on the tour, flown at the speed the tour is flown at,
# are banked most of the way over -- and anything past about ten degrees is
# unusable on a panel five times wider than it is tall, because the horizon
# rises five pixels per degree of roll and leaves through the corner. What has
# to read is which way you are banking and that it keeps changing, not how far.
ROLL_LIMIT = 0.105

# --------------------------------------------------------------------------
# The tour.
#
# Latitude, longitude, height above the water, and what the leg is there for.
# Read it top to bottom and it is the flight: in off the Pacific, through the
# Gate, east along the city front, north up the bay, round Angel Island, home
# over the Headlands and back out to sea. It is a *closed* list -- the last
# point flows into the first -- because the path has to be exactly periodic in
# t, and 28.9 km of it, which at the default loop is 138 m/s.
#
# The heights are the interesting column, and they were the hardest thing here
# to get right. Parallax -- which is all that says you are moving -- goes as
# one over the distance to what you pass, so the instinct is to fly as low as
# possible. That was tried, at 130 to 200 m the whole way round, and it is
# wrong: on a panel 64 rows tall, an eye at 150 m over a bay 10 km wide puts
# every far shore inside two pixels of the horizon and the picture becomes a
# flat line with water under it. There is nothing to have parallax *against*.
#
# The second reason is worse, and it is the one that is easy to miss. At this
# focal length a 64 row panel has a *25 degree vertical field*, so anything
# nearer than about four and a half times your height is below the bottom edge
# of the frame. Alcatraz was routed past at 200 m and eight hundred metres
# abeam, which is not subtle -- it is invisible, and it took a contact sheet to
# notice, because a frame with an island missing from it looks exactly like a
# frame with no island in it.
#
# So the height goes with what is beside you, and so does the stand-off. The
# Gate is flown at 145 m, between the deck at 67 and the tower tops at 227, so
# the transit really does pass under the cables and between the towers; the
# open crossings are 235 to 265, where you look *down* on the bay and Alcatraz,
# Angel Island and the Berkeley hills are separately visible instead of stacked
# on one line; and the landmarks are passed at one to two kilometres rather
# than at three hundred metres. Hawk Hill at 350 is the same trick: 270 m of
# headland with the glider 80 m over it, close enough to feel and high enough
# to see Tamalpais behind it.
# --------------------------------------------------------------------------

TOUR = [
    (37.8060, -122.5030, 230.0),   # in off the Pacific, the Gate opening ahead
    (37.8199, -122.4783, 145.0),   # THE GATE, between the towers
    (37.8130, -122.4380, 205.0),   # east along the city front, Alcatraz to port
    (37.8160, -122.4030, 235.0),   # off the Wharf; Treasure Island, Bay Bridge
    (37.8450, -122.3970, 250.0),   # left, north up the bay
    (37.8690, -122.4180, 265.0),   # Angel Island two kilometres to port
    (37.8700, -122.4600, 265.0),   # round its north end, Tamalpais on the bow
    (37.8480, -122.4850, 300.0),   # south-west over Richardson Bay to Sausalito
    (37.8300, -122.4950, 350.0),   # over Hawk Hill, the whole bay astern
    (37.8120, -122.5230, 265.0),   # out past Point Bonita to the open sea
    (37.8000, -122.5180, 235.0),   # the turn at sea, and round
]

# How hard the route is smoothed, in harmonics: the width of the Gaussian that
# rolls the circuit's Fourier coefficients off. The waypoints above are corners
# and a glider does not fly corners; six harmonics of roll-off rounds them into
# turns of half a kilometre to a kilometre's radius. It is also what keeps the
# bank honest, because the curve stays smooth to every order and the second
# derivative the roll is built out of has no steps in it anywhere. An
# interpolating spline would have passed exactly through the waypoints and put
# a discontinuity in that second derivative at every one of them -- eleven
# places a loop where the wing would snap from one bank to another.
ROUTE_SIGMA = 6.0
# Where the series is cut. Three sigma, past which the Gaussian has taken the
# coefficients to a thousandth and they are not worth the multiply.
ROUTE_HARMONICS = 18
# Resolution of the arc-length fit. 1024 samples over 25 km is a 24 m step,
# well under anything the curve does.
ROUTE_SAMPLES = 1024
# Reparameterisation passes; see fit_route(). Twelve takes the variation in
# ground speed round the loop from 60% to about 5%, and that matters: a curve
# whose speed surges and stalls between waypoints is exactly the lurching this
# demo already fixed once.
ROUTE_PASSES = 12

_BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], f32)


def add_arguments(ap):
    ap.add_argument("--light", default="golden", choices=sorted(LIGHTS),
                    help="time of day: sun position, haze and palette")
    ap.add_argument("--fog", type=float, default=1.0,
                    help="haze density, 0 clear to ~2 socked in")
    ap.add_argument("--steps", type=int, default=64,
                    help="depth samples per terrain column")
    ap.add_argument("--coarse", dest="coarse", action="store_true", default=False,
                    help="march the landscape at half width and double it back")
    ap.add_argument("--no-coarse", dest="coarse", action="store_false",
                    help="march the landscape at every column")
    ap.add_argument("--far", type=float, default=17000.0,
                    help="far plane, metres")
    ap.add_argument("--near", type=float, default=120.0,
                    help="near plane, metres")
    ap.add_argument("--fov", type=float, default=96.0,
                    help="horizontal field of view, degrees")
    ap.add_argument("--altitude", type=float, default=0.0,
                    help="raise or lower the whole tour, metres")
    ap.add_argument("--climb", type=float, default=24.0,
                    help="how far the air lifts and drops you, metres")
    ap.add_argument("--loop", type=float, default=210.0,
                    help="seconds for one circuit of the tour")
    ap.add_argument("--bank", type=float, default=1.0,
                    help="how far the horizon tilts in the turns")
    ap.add_argument("--roll-lag", type=float, default=1.2,
                    help="seconds the wing takes to roll into a turn")
    ap.add_argument("--phase", type=float, default=0.0,
                    help="where in the circuit to start, 0..1")
    ap.add_argument("--no-bridge", dest="bridge", action="store_false",
                    help="the bay with none of its bridges built")
    ap.add_argument("--no-tower", dest="tower", action="store_false",
                    help="leave Sutro Tower off the ridge")
    ap.add_argument("--wing", dest="wing", action="store_true", default=False,
                    help="frame the shot with the glider's leading edge")
    ap.add_argument("--no-wing", dest="wing", action="store_false",
                    help="no glider wing in the frame")
    ap.add_argument("--birds", type=int, default=3,
                    help="other birds strung out along the same route")
    ap.add_argument("--dither", type=float, default=1.0,
                    help="ordered dither depth in LSBs (0 = off)")
    ap.add_argument("--seed", type=int, default=1937,
                    help="seed for the water noise and the birds")


# --------------------------------------------------------------------------
# Terrain.
# --------------------------------------------------------------------------

def load_dem():
    """Heights in metres, the water mask, metres per cell, and the bbox.

    The file stores the horizontal *difference* of the integer heights, which
    is what gets a 768x768 grid into 200 kB: terrain is smooth, so the
    differences are small numbers around zero and DEFLATE eats them. One
    cumulative sum puts it back, and costs a millisecond once.
    """
    d = np.load(DEM)
    hgt = np.cumsum(d["dh"].astype(np.int32), axis=1).astype(f32)
    shape = tuple(int(v) for v in d["shape"])
    sea = np.unpackbits(d["sea"])[:shape[0] * shape[1]].reshape(shape).astype(bool)
    mx, my = (float(v) for v in d["metres"])
    return hgt, sea, mx, my, tuple(float(v) for v in d["bbox"])


def world_of(lat, lon, bbox, mx, my, shape):
    """A place on the earth as metres east and metres south of the map corner.

    Everything in the demo is metres in this frame: u runs east, v runs
    south, because the DEM's rows run north to south the way it came off the
    server, and turning it over to make the axes tidy would only mean two
    more sign errors somewhere else.
    """
    lon0, lat0, lon1, lat1 = bbox
    u = (lon - lon0) / (lon1 - lon0) * shape[1] * mx
    v = (lat1 - lat) / (lat1 - lat0) * shape[0] * my
    return u, v


# --------------------------------------------------------------------------
# The route, as a Fourier series.
#
# A closed flight path has to be three things at once: exactly periodic, so a
# segment that overruns the loop lands back where it started; smooth to the
# second derivative, because that is what the bank is built out of; and
# uniform in arc length, because otherwise the glider surges and stalls
# between waypoints. A Fourier series is all three for free -- it is periodic
# by construction and infinitely differentiable, and its derivatives are
# closed-form rather than divided differences -- and the arc-length part is
# the only one that takes any work, which is done once here.
#
# It is also the same object the flight path has always been in this file.
# What used to be two hand-written harmonics is now twelve fitted ones.
# --------------------------------------------------------------------------

def series_of(z, harmonics, sigma=None):
    """Fit `z`, sampled evenly round one turn, as sum(c_k exp(i k a)).

    Returns the constant term and the positive and negative coefficients, k
    running 1..harmonics in both. `z` may be complex -- the route is (east,
    south) as one complex number -- so the two halves are independent and
    there is no conjugate symmetry to exploit.

    With `sigma` the coefficients are rolled off by a Gaussian rather than cut
    off at the last one, and the difference is not cosmetic. A sharp cutoff is
    a rectangular window, which rings: the curve acquires a ripple at the
    cutoff frequency all the way round, and since the bank is built out of the
    second derivative -- where a harmonic at k times the fundamental picks up
    a factor of k squared -- a ripple far too small to see in the flight path
    is a wobble you cannot miss in the horizon. A Gaussian rolls off with no
    ringing at all, and it is exactly the shape a corner takes if you let it
    diffuse, which is a good description of what a wing does to a corner.
    """
    c = np.fft.fft(np.asarray(z, complex)) / len(z)
    k = int(harmonics)
    w = 1.0
    if sigma:
        w = np.exp(-0.5 * (np.arange(1, k + 1, dtype=float) / sigma) ** 2)
    return complex(c[0]), c[1:k + 1] * w, c[-k:][::-1] * w


def series_at(ser, a):
    """Evaluate a fitted series at an array of phases. Build time only."""
    c0, cp, cm = ser
    e = np.exp(1j * np.outer(np.asarray(a, float),
                             np.arange(1, len(cp) + 1, dtype=float)))
    return c0 + e.dot(cp) + np.conj(e).dot(cm)


def resample_closed(p, h, n):
    """n points evenly spaced *along* the closed curve through p, with h."""
    p = np.append(p, p[:1])
    h = np.append(h, h[:1])
    s = np.concatenate([[0.0], np.cumsum(np.abs(np.diff(p)))])
    want = np.linspace(0.0, s[-1], n, endpoint=False)
    return (np.interp(want, s, p.real) + 1j * np.interp(want, s, p.imag),
            np.interp(want, s, h), float(s[-1]))


def fit_route(pts, alt, harmonics=ROUTE_HARMONICS, sigma=ROUTE_SIGMA,
              samples=ROUTE_SAMPLES, passes=ROUTE_PASSES):
    """Waypoints in, a smooth arc-length-parameterised circuit out.

    Two separate jobs, and doing them in one step is what went wrong first
    time. The *shape* is settled once, by fitting the polyline and rolling the
    coefficients off with the Gaussian: that is what rounds the corners. Then
    the *parameterisation* is fixed, by evaluating the curve densely,
    resampling it at even arc length along itself and refitting -- with no
    roll-off this time, because the curve is already band-limited and a plain
    refit is very nearly lossless. Smoothing again on every pass instead is a
    heat flow, and a heat flow shrinks a closed curve: twelve passes of it
    took a 26 km tour down to 7 km, which looked entirely reasonable until
    somebody measured it.

    What the reparameterisation buys is a constant ground speed. Without it
    the glider slows to a crawl through the tight turns and sprints down the
    straights, which is precisely the lurch this file spent a commit removing.

    Returns the position series, the altitude series, and the circuit length
    in metres -- which is the number the ground speed comes out of.
    """
    z, hz, _ = resample_closed(np.asarray(pts, complex),
                               np.asarray(alt, float), samples)
    dense = np.linspace(0.0, 2.0 * math.pi, 8 * samples, endpoint=False)
    pos = series_of(z, harmonics, sigma)
    hgt = series_of(hz, harmonics, sigma)
    for _ in range(max(0, int(passes))):
        z, hz, _ = resample_closed(series_at(pos, dense),
                                   series_at(hgt, dense).real, samples)
        pos, hgt = series_of(z, harmonics), series_of(hz, harmonics)
    _, _, length = resample_closed(series_at(pos, dense),
                                   series_at(hgt, dense).real, samples)
    return pos, hgt, length


def series_evaluator(ser):
    """A scalar `a -> (value, d/da, d2/da2)` for use inside render().

    Plain Python complex arithmetic on plain floats: a float32 numpy scalar
    costs about thirty times more per operation, which fsn.py measured the
    hard way, and this runs a few times a frame. The powers of exp(ia) come
    out of one multiply each rather than one call to exp each, so the whole
    evaluation costs two trig calls however many harmonics there are, and
    exp(-ika) is the conjugate of exp(ika) because the modulus is one.
    """
    c0, cp, cm = ser
    terms = list(zip(range(1, len(cp) + 1),
                     (complex(v) for v in cp), (complex(v) for v in cm)))

    def at(a):
        e = complex(math.cos(a), math.sin(a))
        p, d1, d2 = c0, 0j, 0j
        w = 1.0 + 0j
        for k, ck, dk in terms:
            w *= e
            up = ck * w
            dn = dk * w.conjugate()
            p += up + dn
            d1 += k * (up - dn)
            d2 -= k * k * (up + dn)
        return p, d1 * 1j, d2

    return at


def series_position(ser):
    """The same series, evaluated for the point alone.

    The camera wants the curve and its first two derivatives, and pays about
    ten complex operations a harmonic for them; something that only wants to
    be somewhere pays three. There are three birds a frame and one glider, so
    the cheap version is most of the calls.
    """
    c0, cp, cm = ser
    terms = list(zip((complex(v) for v in cp), (complex(v) for v in cm)))

    def at(a):
        e = complex(math.cos(a), math.sin(a))
        p = c0
        w = 1.0 + 0j
        for ck, dk in terms:
            w *= e
            p += ck * w + dk * w.conjugate()
        return p

    return at


def hillshade(hgt, mx, my, az_deg, el_deg):
    """Cosine of the angle between the surface normal and the sun, 0..1.

    Central differences rather than forward ones: a forward difference shifts
    the whole shading half a cell downhill, which at 45 m cells and a sun this
    low puts the lit edge visibly off the ridge line -- and the ridge lines
    are the only thing at this size that says which hill you are looking at.
    """
    dzdu = np.zeros_like(hgt)
    dzdv = np.zeros_like(hgt)
    dzdu[:, 1:-1] = (hgt[:, 2:] - hgt[:, :-2]) / (2.0 * mx)
    dzdv[1:-1] = (hgt[2:] - hgt[:-2]) / (2.0 * my)
    az, el = math.radians(az_deg), math.radians(el_deg)
    lu, lv, lz = (math.cos(el) * math.sin(az), -math.cos(el) * math.cos(az),
                  math.sin(el))
    ndot = -dzdu * lu - dzdv * lv + lz
    ndot /= np.sqrt(dzdu * dzdu + dzdv * dzdv + 1.0)
    return np.maximum(ndot, 0.0)


def terrain_index(hgt, sea, mx, my, light):
    """One uint8 (class, shade) per map cell.

    Resolving every lighting decision into a small integer here is what makes
    the frame affordable: which face is lit, how high the ground is and
    whether it is water are all settled at build time, and render() only ever
    gathers the answer.
    """
    lit = hillshade(hgt, mx, my, light["sun_az"], light["sun_el"])
    shade = np.rint(lit * (NSHADE - 1)).astype(np.uint8)
    # Three land bands by height, and the boundaries are not arbitrary: 70 m
    # is about where the Headlands stop being beach and start being hill, and
    # 260 m is the top of the coastal scrub, above which Tam is grass and
    # rock. They are also far enough apart to read at 64 rows.
    cls = np.where(hgt > 260.0, CLS_HIGH,
                   np.where(hgt > 70.0, CLS_MID, CLS_LOW)).astype(np.uint8)
    idx = cls * NSHADE + shade
    idx[sea] = CLS_WATER * NSHADE + W_FLAT
    return idx.astype(np.uint8)


def pad_sea(a, fill):
    """Ring the map with one cell of sea, so a ray that runs off it finds ocean.

    The alternative -- clamping the sample to the last real cell -- smears
    that cell outward forever, and the Berkeley hills become a ridge running
    to the horizon. One border cell costs nothing and makes the edge of the
    world open water, which on three of the four sides is what is there.
    """
    out = np.full((a.shape[0] + 2, a.shape[1] + 2), fill, a.dtype)
    out[1:-1, 1:-1] = a
    return out


# --------------------------------------------------------------------------
# Colour tables.
# --------------------------------------------------------------------------

def ramp(stops, x):
    """Colour stops interpolated at positions `x`, kept in float.

    Not ds.gradient(), for the reason sunset.py sets out: gradient() rounds to
    eight bits, and ordered dithering a value that has already been rounded
    adds noise and nothing else. The fraction has to survive to the last cast.
    """
    pos = np.array([p for p, _ in stops], f32)
    cols = np.array([c for _, c in stops], f32)
    return np.stack([np.interp(x, pos, cols[:, ch]) for ch in range(3)],
                    axis=-1).astype(f32)


def build_palette(light, fog_gain):
    """The one table every pixel indexes. See the layout note at the top."""
    haze = np.array(light["haze"], f32)
    warm = np.array(light["warm"], f32)
    base = np.zeros((NCLS, NSHADE, 3), f32)

    # Land: the class colour lifted by the hillshade, and warmed as it lights,
    # because a low sun does not only make a slope brighter, it makes it a
    # different colour from the slope beside it in shadow.
    amb, dif = light["ambient"], light["diffuse"]
    s = np.linspace(0.0, 1.0, NSHADE, dtype=f32)[:, None]
    for c in (CLS_LOW, CLS_MID, CLS_HIGH):
        col = np.array(light["land"][c - 1], f32)[None, :]
        base[c] = col * (amb + dif * s) * (1.0 + (warm - 1.0) * s)

    # Water: four fixed shades rather than a ramp -- flat, chop, and two
    # levels of glitter, which is what the per-frame integer bump moves
    # between.
    water = np.array(light["water"], f32)
    chop = np.array(light["chop"], f32)
    glint = np.array(light["glint"], f32)
    base[CLS_WATER, W_FLAT] = water
    base[CLS_WATER, W_CHOP] = chop
    # The two glitter steps are mixed well back towards the water. Taken at
    # full strength they came out as pale blotches the size of Alcatraz
    # rather than as light on a moving surface: at 64 rows there is no room
    # for a highlight to be both bright and small, so it has to be small.
    base[CLS_WATER, W_GLINT] = glint * 0.30 + water * 0.70
    base[CLS_WATER, W_GLINT + 1:] = glint * 0.62 + water * 0.38

    # A bridge is a class like any other, which is the whole trick that makes
    # it cost nothing: painting it is writing an integer into the index image
    # before the gather, so it picks up the right amount of haze for its
    # distance without a single colour operation. Three classes per bridge,
    # laid out consecutively so the compositor addresses them as base + part.
    base[CLS_BIRD] = np.array((18.0, 16.0, 20.0), f32)[None, :]
    base[CLS_MAST] = np.array(MASTS[0]["rgb"], f32)[None, :]
    for b, spec in enumerate(BRIDGES):
        for part, rgb in enumerate(spec["rgb"]):
            base[CLS_STEEL0 + NPART * b + part] = np.array(rgb, f32)[None, :]

    # Haze. Every colour above, faded towards the horizon colour over NFOG
    # steps, so distance costs the frame nothing at all: the depth step picks
    # a band and the band is already the right colour.
    #
    # The curve matters more than the density. A linear fade lays a grey veil
    # over the near ground; what real haze does is almost nothing for the
    # first kilometre and then everything, which is the exponent below.
    f = (np.linspace(0.0, 1.0, NFOG, dtype=f32) ** 1.8)[:, None]
    np.minimum(f * fog_gain, 1.0, out=f)
    pal = np.empty((SKY0 + NSKY, 3), f32)
    flat = base.reshape(-1, 3)
    for i in range(flat.shape[0]):
        pal[i * NFOG:(i + 1) * NFOG] = flat[i] + (haze - flat[i]) * f

    # Sky, indexed by how many rows above the horizon a pixel is. Below the
    # horizon it holds at the haze colour, which is also what a pixel gets if
    # the depth budget ran out before the ray hit anything.
    above = (SKY_MID - np.arange(NSKY, dtype=f32)) / SKY_SPAN
    np.clip(above, 0.0, 1.0, out=above)
    pal[SKY0:] = ramp(light["sky"], 1.0 - above)
    return np.clip(pal, 0.0, 253.0).astype(f32)


def fixed_palette(pal, dith):
    """The palette and the dither, re-expressed as a uint16 add and a shift.

    The last thing a frame does is `trunc(pal[index] + dither)`, and in float
    that is three passes over 240 kB of colour: gather it, add the dither
    plane, truncate to bytes. Nearly all of that traffic is carrying fractions
    that only ever decide one bit each, and on a Pi 3 it is seven milliseconds.

    So it is done in fixed point instead, and *exactly*, not approximately.
    The dither cells are (2b+1)/128 for b in 0..63, so at any whole `--dither`
    every dither value is a multiple of 1/128, and then

        floor(p + c/128) == (floor(p*128) + c) >> 7

    for integer c -- because floor(p*128) is under p*128 by less than one, and
    adding less than one to an integer cannot carry it over a multiple of 128.
    The palette becomes floor(p*128) in uint16, the dither plane becomes c
    beside it, and the frame is one gather, one add and one shift, all 16 bit.

    Returns (pal16, cdith), or None if the identity would not hold -- which is
    the honest answer for a fractional `--dither`, and the caller keeps the
    float path. It is *verified* rather than argued: every palette entry
    against every dither cell, once, here. The claim being made is that the
    picture does not change by a single bit, so it is worth checking rather
    than reasoning about.
    """
    d = np.unique(dith)
    c = np.rint(d.astype(np.float64) * 128.0)
    v = np.floor(pal.astype(np.float64) * 128.0)
    if c.min() < 0.0 or v.min() < 0.0 or v.max() + c.max() > 65535.0:
        return None
    want = np.trunc(pal[:, :, None] + d.astype(f32)[None, None, :])
    got = (v[:, :, None] + c[None, None, :]) // 128.0
    if not np.array_equal(want, got):
        return None
    cd = np.rint(dith.astype(np.float64) * 128.0).astype(np.uint16)
    return v.astype(np.uint16), cd


def cable_span(ss, s0, s1, h0, h1, sag=None):
    """One length of main cable, as a height over `ss` in 0..1 along the deck.

    Two shapes, and the difference is the whole silhouette. With `sag` -- a
    suspended span -- the cable is a parabola hanging to that height at the
    centre and rising to h0 and h1 at the supports, which is the real curve of
    a cable under a uniform deck load. Without it -- a side span, or the run
    out to an anchorage -- the cable goes more or less straight from the tower
    down into the ground, and the 0.8 power is the slight bow in it. The
    caller is expected to put s0 at the tower end so the bow falls the right
    way.
    """
    u = np.clip((ss - s0) / (s1 - s0), 0.0, 1.0)
    if sag is None:
        return h0 + (h1 - h0) * u ** 0.8
    x = 2.0 * u - 1.0
    return (sag + (h0 - sag) * np.clip(-x, 0.0, 1.0) ** 2
            + (h1 - sag) * np.clip(x, 0.0, 1.0) ** 2)


def bake_bridge(spec, index, bbox, mx, my, shape, nodes=193):
    """Turn one entry of BRIDGES into everything the compositor needs.

    The result is deliberately flat and read-only: an anchor point and a span
    vector in the demo's metres-east/metres-south frame, height profiles for
    the deck and the cable sampled along the span, where the supports are, and
    the three palette base indices. Doing it here rather than per frame is why
    a second bridge costs the frame nothing but the pixels it covers.
    """
    lengths = [float(x) for x in spec["spans"]]
    total = sum(lengths)
    # Support positions along s, and their heights. The two ends are
    # anchorages; interior supports are towers, except the middle one of an
    # odd number when the spec names a `centre` -- which is the Bay Bridge's
    # central anchorage, a support the cables die into far below tower height.
    edges = np.cumsum([0.0] + lengths) / total
    inner = len(lengths) - 1
    heights = [spec["anchor"]]
    for i in range(inner):
        h = spec["tower"]
        if "centre" in spec and inner % 2 == 1 and i == inner // 2:
            h = spec["centre"]
        heights.append(h)
    heights.append(spec["anchor"])

    ss = np.linspace(0.0, 1.0, nodes, dtype=f32)
    # A little camber, high in the middle, because a deck drawn dead flat
    # against a sagging cable reads as a mistake even when nobody can say why.
    deck = (spec["deck"] - spec["camber"] * (2.0 * ss - 1.0) ** 2).astype(f32)
    cable = np.zeros_like(ss)
    for i, sag in enumerate(spec["sag"]):
        s0, s1 = float(edges[i]), float(edges[i + 1])
        h0, h1 = heights[i], heights[i + 1]
        if sag is None:
            # Run it from the tower end outward, so cable_span's bow is the
            # right way round whichever end of the bridge this is.
            seg = (cable_span(ss, s1, s0, h1, h0) if i == 0
                   else cable_span(ss, s0, s1, h0, h1))
        else:
            seg = cable_span(ss, s0, s1, h0, h1, sag=deck)
        cable = np.where((ss >= s0) & (ss <= s1), seg, cable)

    br = math.radians(spec["bearing"])
    bu, bv = math.sin(br), -math.cos(br)
    u, v = world_of(spec["lat"], spec["lon"], bbox, mx, my, shape)
    u += mx                                       # into padded coordinates
    v += my
    base = np.int32((CLS_STEEL0 + NPART * index) * (NSHADE * NFOG))
    return dict(
        name=spec["name"],
        ax=u - bu * 0.5 * total, ay=v - bv * 0.5 * total,
        ex=bu * total, ey=bv * total,
        ss=ss, deck=deck, cable=cable.astype(f32),
        top=max(heights), length=total,
        # Only the real towers get piers drawn up from the water; an anchorage
        # is a block on the shore and there is nothing to draw under it.
        towers=np.array([float(edges[i + 1]) for i in range(inner)
                         if heights[i + 1] == spec["tower"]], f32),
        hw=spec["thick"] / total, depth=spec["depth"],
        pidx=base + np.array([0, NFOG, 2 * NFOG], np.int32) * NSHADE)


def bake_mast(spec, ground, bbox, mx, my, shape):
    """A mast as a stack of silhouettes, one per whole pixel of height.

    Built here rather than scaled per frame, and by nearest neighbour rather
    than by anything smoother, because the art is a one-pixel lattice: any
    interpolation turns the gaps between the legs into grey and the tower
    stops being a lattice at all. It only approaches slowly, so a sprite per
    integer height is a couple of dozen tiny arrays and the frame does a
    lookup.
    """
    art = np.array([[c != ' ' for c in row] for row in spec["art"]], bool)
    ah, aw = art.shape
    table = []
    for h in range(MAST_MIN, MAST_MAX + 1):
        w = max(1, int(round(h * aw / float(ah))))
        rows = (np.arange(h) * ah) // h
        cols = (np.arange(w) * aw) // w
        table.append(np.ascontiguousarray(art[rows][:, cols]))
    u, v = world_of(spec["lat"], spec["lon"], bbox, mx, my, shape)
    return dict(name=spec["name"], u=u + mx, v=v + my,
                height=float(spec["height"]), ground=float(ground),
                spr=table, pidx=np.int32(CLS_MAST * NSHADE * NFOG))


def haze_band(z, far):
    """Which haze band a depth falls in. Same curve as build_palette()."""
    return np.rint(np.clip(z / far, 0.0, 1.0) * (NFOG - 1)).astype(np.int32)


def value_noise(rng, h, w, cy, cx):
    """Tileable value noise as uint8, for the water surface.

    Separate cell counts per axis, and they are nothing like equal: water seen
    from above is banded, not spotted, and a square-celled noise stretched
    across a 5:1 panel gives round blobs the size of Alcatraz.
    """
    g = rng.random((cy, cx)).astype(f32)

    def axis(n, cells):
        ff = np.arange(n, dtype=f32) * (cells / float(n))
        i0 = np.floor(ff).astype(np.int32) % cells
        tt = ff - np.floor(ff)
        return i0, (i0 + 1) % cells, (tt * tt * (3.0 - 2.0 * tt)).astype(f32)

    y0, y1, ty = axis(h, cy)
    x0, x1, tx = axis(w, cx)
    top = g[y0][:, x0] * (1 - tx) + g[y0][:, x1] * tx
    bot = g[y1][:, x0] * (1 - tx) + g[y1][:, x1] * tx
    v = top * (1 - ty)[:, None] + bot * ty[:, None]
    v -= v.min()
    v /= max(float(v.max()), 1e-6)
    return np.rint(v * 255.0).astype(np.uint8)


# --------------------------------------------------------------------------
# The glider, drawn once, because it does not move.
#
# That is not a shortcut, it is the physics: in a coordinated turn the pilot
# and the wing keep the same relationship and it is the *world* that tilts. So
# the wing is a static screen-space overlay costing one composite a frame, and
# the horizon rolling behind something nailed to the frame is exactly what
# banking looks like from underneath a sail.
# --------------------------------------------------------------------------

def build_wing(W, H):
    """The leading edges, and only as much of the sail as they carry in with them.

    An earlier version filled the whole top of the frame with sail, and at 64
    rows that does not read as a wing at all -- it reads as a lens vignette,
    a dark arch over the picture. What reads is the *lines*: two straight
    spars going back and out to the tips, entering the frame only in the outer
    quarters, so the middle of the panel -- where the horizon, the sun and the
    skyline are -- is left completely alone. The nose is ahead of you and
    above the top edge, which is why the spars leave through the sides.

    They are steeper and thicker than the first attempt at them. Shallow
    one-pixel spars crossing most of the width did not read as a wing, they
    read as two scratches on the sky; swept hard into the top corners at two
    pixels wide they read as structure overhead. It is the same wing -- what
    changed is how much of it the panel is being shown.
    """
    sy = H / 64.0
    x = np.arange(W, dtype=f32)
    y = np.arange(H, dtype=f32)[:, None]
    cx = 0.5 * W
    d = np.abs(x - cx) / (0.5 * W)
    edge = (-20.0 + d * 42.0) * sy
    spar = max(1.0, 2.2 * sy)
    cov = np.clip(np.minimum(y + 0.5, edge + spar) - np.maximum(y - 0.5, edge),
                  0.0, 1.0)
    # A sliver of sail above each spar rather than a filled corner. Backlit
    # dacron at this hour is not black, so it is a warm grey a couple of steps
    # off the sky rather than a silhouette.
    sail = np.clip(edge - y + 0.5, 0.0, 1.0) * np.clip(1.0 - (edge - y) / (5.0 * sy),
                                                       0.0, 1.0) * 0.85
    col = np.zeros((H, W, 3), f32)
    a = np.zeros((H, W), f32)
    for rgb, m in ((np.array((58.0, 52.0, 54.0), f32), sail),
                   (np.array((126.0, 116.0, 110.0), f32), cov)):
        col *= (1.0 - m)[..., None]
        col += rgb * m[..., None]
        a += (1.0 - a) * m
    # One front wire each side, a single dim pixel wide, running down and in
    # from the spar. Without them the spars read as two scratches; with them
    # they read as something you are hanging underneath.
    for sign in (-1, 1):
        for k in range(int(9 * sy)):
            xx = int(cx + sign * (0.34 * W - k * 1.6))
            yy = int(1 + k * 1.05 * sy)
            if 0 <= xx < W and 0 <= yy < H:
                col[yy, xx] = (92.0, 84.0, 82.0)
                a[yy, xx] = max(float(a[yy, xx]), 0.7)
    return (col * a[..., None]).astype(f32), (1.0 - a[..., None]).astype(f32)


def build_sun(light, H):
    """A small additive disc and halo, blitted where the sky shows through."""
    r = max(1.6, 0.075 * H)
    n = 2 * int(math.ceil(r * 3.6)) + 1
    c = n // 2
    yy = (np.arange(n, dtype=f32) - c)[:, None]
    xx = (np.arange(n, dtype=f32) - c)[None, :]
    # A low sun is refracted wider than it is tall, which is also what stops
    # something this small reading as one stray bright pixel.
    # The edge is deliberately soft and the disc deliberately short of full
    # scale. Drawn hard and bright it saturates every channel over an area
    # the eye can measure, and a flat white ellipse with a crisp rim reads as
    # a sprite pasted onto the sky; most of what makes it read as the sun is
    # the halo bleeding into the sky around it, not the disc.
    d = np.sqrt((xx / 1.45) ** 2 + yy ** 2) / r
    disc = np.clip((1.0 - d) * 2.4, 0.0, 1.0) ** 1.4 * 0.86
    halo = np.exp(-(d * 0.72) ** 2) * 0.62
    spr = (np.array(light["sun"], f32) * disc[..., None]
           + np.array(light["glow"], f32) * halo[..., None])
    return spr.astype(f32), c


# At this size a bird is five pixels, and its only distinguishing feature is
# that the wings are not level with the body. The shallow M is the whole read
# and the deep one is the downstroke; anything more detailed is a smudge.
BIRD_POSES = [(" X X ",
               "X   X"),
              ("XX XX",
               "  X  ")]


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    light = LIGHTS[args.light]

    hgt, sea, mx, my, bbox = load_dem()
    shape = hgt.shape
    cmap = pad_sea(terrain_index(hgt, sea, mx, my, light),
                   np.uint8(CLS_WATER * NSHADE + W_FLAT))
    hmap = pad_sea(hgt, f32(0.0))
    del hgt, sea
    MH, MW = hmap.shape
    hflat = np.ascontiguousarray(hmap.reshape(-1))
    # Left as the uint8 it was baked as, rather than widened to int32 for the
    # convenience of the gather that reads it. It is a 600 kB table read at
    # twenty thousand scattered addresses a frame and every one of them is a
    # cache line; at int32 the same table is 2.4 MB and the misses cost four
    # times as much. One narrowing pass on the way out is far cheaper.
    cflat = np.ascontiguousarray(cmap.reshape(-1))
    # Sample coordinates are metres from the padded corner, so the border
    # cell is already in the arithmetic and the clamp below is a plain clamp
    # to the array.
    inv_mx, inv_my = f32(1.0 / mx), f32(1.0 / my)

    # --- the tour ------------------------------------------------------------
    # Waypoints to padded map metres, then one smooth closed circuit through
    # them. Everything about where the glider is and which way it is pointing
    # comes off this curve and its derivatives; nothing integrates.
    wp = []
    for lat, lon, _ in TOUR:
        u, v = world_of(lat, lon, bbox, mx, my, shape)
        wp.append(complex(u + mx, v + my))
    route, route_z, route_len = fit_route(wp, [z for _, _, z in TOUR])
    at_pos = series_evaluator(route)
    at_where = series_position(route)
    at_alt = series_evaluator(route_z)
    # The trim is applied to the constant term, so --altitude shifts the whole
    # tour without touching its shape or any of its derivatives.
    alt_trim = float(args.altitude)

    # --- camera -------------------------------------------------------------
    focal = 0.5 * W / math.tan(0.5 * math.radians(
        min(max(args.fov, 20.0), 150.0)))
    colf = np.arange(W, dtype=f32) + 0.5
    # Distance of each column's centre from the middle of the panel, which is
    # what the roll shears the horizon by. Built here rather than as
    # `colf - 0.5 * W` in the frame, where it was an allocation and a pass a
    # frame for a number that never changes.
    colc = (colf - f32(0.5 * W)).astype(f32)
    colx = (colc / focal).astype(f32)               # ray shear per column
    colidx = np.arange(W, dtype=np.int32)

    # --- how wide the landscape is drawn -------------------------------------
    # Everything from the ray march to the haze band is per *terrain* column,
    # and --coarse marches one column in two and doubles it back on the way
    # out. That is nearly two thirds of the frame halved, and the price is
    # exactly what it sounds like: hillsides and the shoreline step in twos.
    # The bridges, Sutro Tower, the birds, the sun and the palette are still
    # drawn at full width afterwards, so what coarsens is the landscape and
    # nothing that has an edge you were looking at.
    #
    # A doubled column samples the ray through the middle of the *pair* rather
    # than through either of its two pixels, so the landscape does not slide
    # half a pixel sideways when the flag is turned on.
    coarse = bool(args.coarse) and W % 2 == 0 and W >= 4
    TW = W // 2 if coarse else W
    if coarse:
        colct = ((np.arange(TW, dtype=f32) * f32(2.0) + f32(1.0))
                 - f32(0.5 * W)).astype(f32)
        colxt = (colct / focal).astype(f32)
    else:
        colct, colxt = colc, colx
    colidxt = np.arange(TW, dtype=np.int32)
    # How many screen pixels a terrain column covers, which the glitter path is
    # the only thing that has to know about: it is baked in screen widths and
    # then read at the sun's screen column.
    gscale = float(W) / TW

    # --- depth schedule ------------------------------------------------------
    # Geometric, because screen space is. Linear steps put nearly all the
    # samples out in the far field where a whole kilometre of depth is one
    # row, and none of them near you where the ground is going past.
    N = max(16, int(args.steps))
    near = max(20.0, args.near)
    far = max(near * 4.0, args.far)
    Z = (near * (far / near) ** np.linspace(0.0, 1.0, N, dtype=f32)).astype(f32)
    invZ = (f32(focal) / Z)[:, None]
    Zbuf = np.concatenate([Z, [1e9]]).astype(f32)   # sentinel: sky is infinite
    # The same schedule as a plain Python list, for the objects that place one
    # scalar depth in it. np.searchsorted costs fifteen times what bisect does
    # for a single value, nearly all of it spent getting into numpy and back
    # out, and it happens five or six times a frame.
    Zlist = [float(v) for v in Zbuf]
    fogb = np.concatenate([haze_band(Z, far), [NFOG - 1]]).astype(np.int32)
    Zcol = Z[:, None]
    # Whether a sample coordinate can be truncated to an integer *before* it is
    # clamped, which is what lets the clamp be an integer one. It can whenever
    # the untruncated value fits an int32, and the largest one possible is the
    # far plane in cells plus the map: true by a factor of a thousand at the
    # default far plane of 17 km, and false only for a --far of ten thousand
    # kilometres, which gets the float clamp and a slower frame.
    int_cells = far * 2.0 / min(mx, my) + max(MW, MH) < 1e9

    pal = build_palette(light, max(args.fog, 0.0))
    dith = (np.tile((_BAYER8 + 0.5) / 64.0, (H // 8 + 1, W // 8 + 1))[:H, :W, None]
            .astype(f32) * f32(args.dither))
    # The fixed-point form of the same two tables. `fixed` is None if it would
    # not be bit-for-bit the float answer, and the wing wants a float frame to
    # multiply through anyway, so the float path stays and is what those cases
    # get. See fixed_palette().
    fixed = None if args.wing else fixed_palette(pal, dith)
    if fixed is not None:
        pal16, cdith = fixed
        # Three real planes rather than an (H,W,1) broadcast, for the reason
        # the wing gives below: broadcasting a trailing 1 against three
        # channels is several times slower here than the plain arithmetic.
        cdith = np.ascontiguousarray(np.repeat(cdith, 3, axis=2))

    # --- water ---------------------------------------------------------------
    # Two tileable noise fields stored doubled, so scrolling them is a plain
    # contiguous slice rather than a modulo gather. They are read in *screen*
    # space, not world space: a depth-scrolled water texture cannot win at 64
    # rows -- tight enough to show crests near you and it aliases to hash
    # across the whole strait, which is the lesson sunset.py paid for.
    nh, nw = max(16, H), max(32, TW)
    tex1 = np.tile(value_noise(rng, nh, nw, 20, 104), (2, 2))
    tex2 = np.tile(value_noise(rng, nh, nw, 26, 104), (2, 2))

    # --- sun ------------------------------------------------------------------
    saz, sel = math.radians(light["sun_az"]), math.radians(light["sun_el"])
    sun_dir = (math.cos(sel) * math.sin(saz), -math.cos(sel) * math.cos(saz),
               math.sin(sel))
    sun_spr, sun_c = build_sun(light, H)
    sun_h, sun_w = sun_spr.shape[:2]

    # The sun's glitter path, baked as the threshold the water noise has to
    # beat, three panels wide with the sun at the middle. Per frame it is a
    # *slice* taken at the sun's column and nothing else, and because it is a
    # field rather than one number per column it can do what a real glitter
    # path does -- narrow out at the horizon, fanning towards you -- since a
    # row further down the panel is water that is nearer.
    gx = ((np.arange(3 * TW, dtype=f32) - 1.5 * TW) * gscale)[None, :]
    gw = (5.0 + 0.85 * np.arange(H, dtype=f32) * (64.0 / H))[:, None]
    glint_field = np.clip(252.0 - 56.0 * np.exp(-(gx / gw) ** 2),
                          188.0, 255.0).astype(np.uint8)
    glint_off = np.full((H, TW), 255, np.uint8)    # sun behind: nothing lights

    # --- bridges --------------------------------------------------------------
    bridges = ([bake_bridge(spec, i, bbox, mx, my, shape)
                for i, spec in enumerate(BRIDGES)] if args.bridge else [])

    # --- masts ----------------------------------------------------------------
    # The ridge a mast stands on comes out of the DEM rather than out of a
    # table, so it cannot end up floating over the hill or buried in it if the
    # terrain is ever re-baked at a different cell size.
    masts = []
    if args.tower:
        for spec in MASTS:
            gu, gv = world_of(spec["lat"], spec["lon"], bbox, mx, my, shape)
            gc, gr = int(gu / mx) + 1, int(gv / my) + 1
            g = float(hmap[max(0, gr - 1):gr + 2, max(0, gc - 1):gc + 2].max())
            masts.append(bake_mast(spec, g, bbox, mx, my, shape))

    # --- birds ----------------------------------------------------------------
    bird_masks = [np.array([[ch != ' ' for ch in r] for r in p], bool)
                  for p in BIRD_POSES]
    nbirds = max(0, int(args.birds))
    # Strung out along the tour rather than circling one thermal, because
    # there is no longer one thermal: each bird hangs a fixed way ahead of or
    # behind the glider on the same curve, circling a little as it goes. That
    # keeps them a depth cue rather than decoration -- you look down on them
    # against the water and they cross in front of a headland -- and it keeps
    # them in frame, since a bird parked over Hawk Hill is out of sight for
    # nine tenths of the loop.
    #
    # Kept as plain Python floats rather than as the arrays they were drawn
    # from. Every one of them is only ever used one at a time in scalar
    # arithmetic, and a numpy float64 scalar costs an order of magnitude more
    # per operation than a Python one -- which for the twenty-odd operations
    # it takes to place one bird, three times a frame, was a millisecond.
    bird_r = [float(v) for v in rng.uniform(90.0, 260.0, nbirds)]
    bird_ph = [float(v) for v in rng.uniform(0.0, 2.0 * math.pi, nbirds)]
    bird_dz = [float(v) for v in rng.uniform(-90.0, -25.0, nbirds)]
    bird_rate = [float(v) for v in rng.uniform(0.055, 0.085, nbirds)]
    bird_flap = [float(v) for v in rng.uniform(1.6, 2.6, nbirds)]
    # Where on the circuit, as a fraction of it. Small: a tenth of a 25 km
    # tour is 2.5 km, which is already past the range a five-pixel bird has
    # any business being drawn at.
    bird_along = [float(v) for v in rng.uniform(-0.035, 0.045, nbirds)]
    bird_pix = int(CLS_BIRD * NSHADE * NFOG)

    # --- wing -----------------------------------------------------------------
    wing_pre, wing_inv = build_wing(W, H) if args.wing else (None, None)
    if args.wing:
        # Three real planes rather than an (H,W,1) broadcast: on this hardware
        # broadcasting a trailing 1 against three channels is about four times
        # slower than the same arithmetic on a full array, which karl.py found
        # and is worth a quarter of a megabyte to avoid.
        wing_inv = np.ascontiguousarray(np.repeat(wing_inv, 3, axis=2))
        wing_dith = (wing_pre + dith).astype(f32)

    # --- scratch, all of it owned --------------------------------------------
    nbins = (H + 1) * TW
    # Three of the frame's arrays hold nothing bigger than a screen row, a bin
    # number in a table of (H+1) x TW, or a depth step, so on any panel this can
    # be pointed at they all fit in an int16 -- and it is worth checking,
    # because the two running scans are where the frame spends its longest
    # uninterruptible stretch. np.minimum.accumulate down a column of shorts is
    # half what it costs down a column of floats and a third of what it costs
    # down ints; the prefix sum that follows is half again. That is a
    # millisecond and a half a frame for a dtype. `narrow` is what says the
    # panel is small enough to promise it, and anything bigger keeps int32 and
    # pays.
    narrow = nbins <= 32767 and N <= 32767
    kdt = np.int16 if narrow else np.int32
    kTW, kN = kdt(TW), kdt(N)
    su = np.empty((N, TW), f32)
    sv = np.empty((N, TW), f32)
    tt = np.empty((N, TW), f32)
    mi = np.zeros((N + 1, TW), np.int32)          # row N is the sky sentinel
    miv = mi[:N]
    miflat = mi.reshape(-1)
    tmpi = np.empty((N, TW), np.int32)
    bkey = np.empty((N, TW), kdt)
    kcolidx = colidxt.astype(kdt)
    horiz = np.empty(W, f32)
    horizt = np.empty(TW, f32) if coarse else horiz
    hrowf = np.empty(TW, f32)
    hrow = np.empty(TW, np.int32)
    skyoff = np.empty(TW, np.int32)
    acc = np.empty((H, TW), kdt)
    idxt = np.empty((H, TW), np.int32)
    pidxt = np.empty((H, TW), np.int32)
    # The landscape's index image doubled out to the panel, or the same array
    # under two names when it is already the panel's width.
    idx = np.empty((H, W), np.int32) if coarse else idxt
    pidx = np.empty((H, W), np.int32) if coarse else pidxt
    gath = np.empty((H, TW), np.int32)
    bump = np.empty((H, TW), np.uint8)
    aux8 = np.empty((H, TW), bool)
    maskt = np.empty((H, TW), bool)
    mask = np.empty((H, W), bool) if coarse else maskt
    rows_i3 = np.arange(H, dtype=np.int32)[:, None] * SKY_SUB
    hist32 = np.empty(nbins, kdt)
    histv = hist32.reshape(H + 1, TW)[:H]
    buf = np.empty((H, W, 3), f32)
    buf16 = None if fixed is None else np.empty((H, W, 3), np.uint16)
    out = np.empty((H, W, 3), np.uint8)
    # The clamps the march makes, as the scalars the ufunc wants: a Python int
    # against an int16 array would be the ufunc's business to type every call.
    i_zero, i_hiu, i_hiv = kdt(0), np.int32(MW - 2), np.int32(MH - 2)
    i32_zero = np.int32(0)
    TWO_U8, CHOP_U8 = np.uint8(2), np.uint8(206)
    # Screen rows as shorts, and the two values a bridge's edges are clamped
    # to: one row above the panel and one below everything. See edge().
    rows16 = np.arange(H, dtype=np.int16)[:, None]
    ROW_LO, ROW_OFF = f32(-1.0), f32(H + 1)
    # A contiguous scratch big enough for any bridge's box.
    brflat = np.empty(H * W, np.int32)
    SKY_LO, SKY_HI = np.int32(SKY0), np.int32(SKY0 + NSKY - 1)
    watermax = np.int32(NSHADE * NFOG)
    need_z = bool(bridges or masts or nbirds)

    period = max(args.loop, 8.0)
    omega = 2.0 * math.pi / period
    start_phase = args.phase * 2.0 * math.pi
    roll_lag = max(args.roll_lag, 0.0)

    def camera(t):
        """Where the glider is and which way it is pointing.

        A closed curve rather than an integrated heading, so the tour is
        exactly periodic: a segment that overruns the loop point lands back
        where it started instead of drifting off the map, and two calls at the
        same t give the same answer, which is what lets the demo be seeked.

        Heading and bank come from the first and second derivatives of that
        same curve rather than being animated separately, so the wing is
        always banked into the turn it is really making, and the nose is
        always pointed along the track -- which is most of what makes a tour
        read as going somewhere rather than as being swung around.

        The bank is read off the curve a second or so *behind* where the
        glider is, which is the wing's roll inertia: a real one does not adopt
        a new bank the instant the air asks for it, it rolls in over about a
        second. A first-order lag would need an integrator and integrator
        state, and render() has to stay a pure function of t. Evaluating the
        same closed curve at t - lag is the same thing done analytically --
        every harmonic comes out shifted by its own share of the delay -- and
        it is still exactly periodic.
        """
        a = omega * t + start_phase
        p, d1, _ = at_pos(a)
        _, ld1, ld2 = at_pos(a - omega * roll_lag)
        du, dv = d1.real * omega, d1.imag * omega
        sp2 = max(du * du + dv * dv, 1e-6)
        psi = math.atan2(dv, du)
        # Rate of turn, in rad/s: the cross product over the *square* of the
        # speed rather than the cube, so this is dpsi/dt and not the geometric
        # curvature. That makes it independent of how big the circuit is,
        # which is what lets ROLL_GAIN be one number rather than something
        # scaled per flight.
        lsp2 = max(ld1.real * ld1.real + ld1.imag * ld1.imag, 1e-6)
        kappa = omega * (ld1.real * ld2.imag - ld1.imag * ld2.real) / lsp2
        # The route's own height profile, plus air that lifts and drops you.
        # The two rates are whole numbers of cycles per circuit -- anything
        # else would make the flight not quite close, and this one has to --
        # but they are coprime and far apart, so nothing lines up into a
        # visible beat inside a loop.
        h, hd, _ = at_alt(a)
        z = (h.real + alt_trim
             + args.climb * (0.72 * math.sin(5.0 * a + 1.1)
                             + 0.34 * math.sin(11.0 * a + 0.2)))
        dz = omega * (hd.real
                      + args.climb * (3.60 * math.cos(5.0 * a + 1.1)
                                      + 3.74 * math.cos(11.0 * a + 0.2)))
        return p.real, p.imag, z, psi, kappa, math.atan2(dz, math.sqrt(sp2))

    # ----------------------------------------------------------------------
    # A bridge, composited into the *index* image rather than into colour.
    #
    # A heightmap cannot have sky under a road deck, so this is an object, and
    # an object in front of a raycast has to be depth-tested against it or a
    # headland stops occluding it. Both come out cheap here. The ray for each
    # column is intersected with the vertical plane of the bridge -- a 2x2
    # solve, vectorised across the whole width -- which gives that column's
    # distance along the deck and its distance from the eye in one step; and
    # because the raycast already left a depth per pixel, hiding the bridge
    # behind Lime Point is one compare.
    #
    # That compare is made in *step numbers* rather than in metres. The march
    # leaves `idx`, the depth step each pixel stopped at, and the schedule Z is
    # increasing, so "the terrain here is further away than the bridge" is
    # `idx >= searchsorted(Z, tz)` -- the same answer as comparing depths, off
    # one 320-column gather to turn `idx` into a depth buffer that nothing else
    # wanted. The searchsorted is over the depth budget, once per column.
    #
    # Then it is painted as class numbers, so the towers pick up exactly the
    # haze their distance earns with no colour arithmetic at all.
    #
    # Nothing below knows which bridge it is drawing; everything that differs
    # between the Gate and the Bay -- the profile, the support positions, the
    # colours -- came out of bake_bridge(). That is the only reason a second
    # structure was a table entry rather than a second copy of this function.
    # ----------------------------------------------------------------------

    def draw_bridge(b, camu, camv, camz, fu, fv, ru, rv):
        b_ax, b_ay, b_ex, b_ey = b["ax"], b["ay"], b["ex"], b["ey"]
        # Where the two ends of it are, in plain scalars, before a single array
        # is touched. For most of the tour a bridge is behind you or off the
        # side of the frame, and settling that from two endpoints is a dozen
        # floating point operations against twenty passes over the width. When
        # it *is* on screen the same two numbers bound the columns it can
        # possibly cover, and the solve below runs over those and no others --
        # which for the Bay Bridge, seen from eleven kilometres and thirty
        # columns wide, is a tenth of the work it was doing.
        #
        # A straight segment cannot re-enter the frustum between its ends: if
        # both are behind the near plane the whole thing is, and if both are in
        # front then its image is the segment between their two screen columns.
        # So both tests are exact rather than conservative, and the column
        # bounds are widened by one either way for the half-pixel between a
        # column's index and the ray through its centre.
        c0, c1 = 0, W
        qx, qy = b_ax - camu, b_ay - camv
        e1u, e1v = qx + b_ex, qy + b_ey
        z0 = qx * fu + qy * fv
        z1 = e1u * fu + e1v * fv
        if z0 <= near and z1 <= near:
            return
        if z0 > near and z1 > near:
            xa = 0.5 * W + focal * (qx * ru + qy * rv) / z0
            xb = 0.5 * W + focal * (e1u * ru + e1v * rv) / z1
            if xa > xb:
                xa, xb = xb, xa
            c0 = max(0, int(math.floor(xa)))
            c1 = min(W, int(math.ceil(xb)) + 1)
            if c1 - c0 < 2:
                return
        cx = colx[c0:c1]
        dx = cx * f32(ru) + f32(fu)
        dy = cx * f32(rv) + f32(fv)
        det = b_ex * dy - b_ey * dx
        adet = np.abs(det)
        # Put a floor under the divisor rather than standing a numpy
        # error-state context around the division. The floor is a thousand
        # times smaller than the threshold `ok` rejects at on the next line, so
        # no column whose answer is ever used sees a different number -- and
        # entering and leaving an errstate costs several times what this
        # division does, on a function called twice a frame.
        np.copysign(np.maximum(adet, f32(1e-12)), det, out=det)
        tz = (b_ex * qy - b_ey * qx) / det
        sp = (qy * dx - qx * dy) / det
        ok = (adet > 1e-9) & (tz > near) & (sp >= 0.0) & (sp <= 1.0)
        nz = ok.nonzero()[0]
        if len(nz) < 2:
            return
        lo, hi = int(nz[0]), int(nz[-1]) + 1
        sl = slice(c0 + lo, c0 + hi)
        sp, tz, ok = sp[lo:hi], tz[lo:hi], ok[lo:hi]
        n = hi - lo
        sc = f32(focal) / tz
        deck = _interp(sp, b["ss"], b["deck"])
        cable = _interp(sp, b["ss"], b["cable"])
        base = horiz[sl] + camz * sc
        r_deck = base - deck * sc
        r_cable = base - cable * sc
        r_top = base - b["top"] * sc
        # A tower is only a dozen or so metres thick along the deck, which
        # from a kilometre out is well under a pixel. Widen it to whatever
        # covers a column and a half, the same call goldengate.py makes for
        # the cables: at this size the silhouette is the whole point and a
        # tower that keeps dropping out between columns reads as a flicker,
        # not as accuracy.
        # np.diff() for two subtractions and a concatenate is a millisecond of
        # Python a frame here, which is more than the whole rest of the bridge.
        dsdc = np.empty(n, f32)
        np.subtract(sp[1:], sp[:-1], out=dsdc[:-1])
        dsdc[-1] = 0.0
        np.abs(dsdc, out=dsdc)
        hw = np.maximum(b["hw"], 1.5 * dsdc)
        istow = (np.abs(sp[None, :] - b["towers"][:, None])
                 < hw[None, :]).any(axis=0) & ok
        # Suspenders every third column, which at any distance either of these
        # bridges is seen from is denser than the real 50 ft spacing resolves.
        issus = ok & ((colidx[sl] % 3) == 0)

        r0 = int(max(0, math.floor(min(float(r_top[istow].min())
                                       if istow.any() else 1e9,
                                       float(r_cable[ok].min())))))
        r1 = int(min(H, math.ceil(float(r_deck[ok].max())) + 2))
        if r1 <= r0:
            return
        nrow = r1 - r0
        # The bridge's box, gathered into a *contiguous* scratch and written
        # back once. pidx[r0:r1, sl] is a strided view, and np.putmask cannot
        # write through one: given a non-contiguous array it silently makes a
        # C-contiguous copy, puts into that, and writes it back on the way out,
        # so every one of the four parts was paying for two copies of the whole
        # box. Doing that copy here, once, instead of four times inside the
        # painter is most of a millisecond in the seconds the Gate fills the
        # frame -- which is exactly where the frame is at its worst.
        dst = brflat[:nrow * n].reshape(nrow, n)
        dst[...] = pidx[r0:r1, sl]
        # Step numbers on both sides of the depth test, rather than letting
        # searchsorted's platform int drag a whole int32 box up to int64 to
        # meet it.
        vis = idx[r0:r1, sl] >= Zbuf.searchsorted(tz, "right").astype(np.int32)[None, :]
        fb = np.rint(_clip(tz / far, f32(0.0), f32(1.0)) * (NFOG - 1)).astype(np.int32)
        thick = np.maximum(1.0, b["depth"] * sc)
        bp = b["pidx"]

        def edge(v, sel, up):
            """One of a part's edges as the whole row it really is.

            The mask below asks `row >= top` and `row <= bot` for integer rows,
            and that is the same question as `row >= ceil(top)` and
            `row <= floor(bot)` -- so it is asked of shorts instead of floats,
            which on this machine is four times quicker over a box that can be
            the whole panel. The clamp to one row either side of the panel is
            what makes the shorts safe whatever the geometry does, and it is
            also where `sel` goes: a column this part does not reach is given
            an edge no row can satisfy.
            """
            v = np.where(sel, v, ROW_OFF) if sel is not None else v
            v = np.ceil(v) if up else np.floor(v)
            return _clip(v, ROW_LO, ROW_OFF, v).astype(np.int16)

        def paint(top, bot, part, sel):
            # Only the rows this part actually reaches. The four parts have
            # wildly different heights -- the cable curtain fills the frame
            # between cable and deck, while the roadway and the cable itself
            # are a pixel or two -- and painting all four over the union of
            # their extents was three quarters of the bridge's cost for two
            # parts that could not possibly be there.
            tops = top[sel]
            if tops.size == 0:
                return
            lo = float(tops.min())
            hi = float(bot[sel].max())
            p0 = max(r0, int(math.floor(lo))) - r0
            p1 = min(r1, int(math.ceil(hi)) + 1) - r0
            if p1 <= p0:
                return
            y = rows16[r0 + p0:r0 + p1]
            m = ((y >= edge(top, sel, True)[None, :])
                 & (y <= edge(bot, None, False)[None, :]) & vis[p0:p1])
            _putmask(dst[p0:p1], m, (bp[part] + fb)[None, :])

        # Suspenders every third column, which at any distance either of
        # these bridges is seen from is denser than the real 50 ft spacing
        # resolves -- taken as a stride rather than as a mask over every
        # column, so the curtain costs a third of the box and not all of it.
        paint(r_cable, r_deck, P_CABLE, issus)                # the curtain
        paint(r_top, base + 2.0 * sc, P_TOWER, istow)         # towers and piers
        paint(r_deck, r_deck + thick, P_DECK, ok)             # roadway
        paint(r_cable - 0.5, r_cable + 0.5, P_CABLE, ok)      # main cable
        pidx[r0:r1, sl] = dst

    def draw_mast(m, camu, camv, camz, fu, fv, ru, rv):
        """One mast, as a depth-tested billboard.

        Simpler than a bridge and for a good reason: a bridge is a kilometre
        of object crossing the view, so every column of it is at its own
        distance and has to be solved for. A mast is a point. One projection
        settles where it is, one sprite settles what it looks like, and the
        depth test is against a single number -- so Twin Peaks in front of it
        hides it, which from most of this tour is exactly what happens.
        """
        du, dv = m["u"] - camu, m["v"] - camv
        zc = du * fu + dv * fv
        if zc < near or zc > far:
            return
        xs = 0.5 * W + focal * (du * ru + dv * rv) / zc
        sc = focal / zc
        hp = int(round(m["height"] * sc))
        if hp < MAST_MIN or not (-W < xs < 2 * W):
            return
        spr = m["spr"][min(hp, MAST_MAX) - MAST_MIN]
        bh, bw = spr.shape
        # Same horizon shear the terrain uses, or the tower leans away from
        # the hill it is standing on every time the wing is banked.
        col = min(W - 1, max(0, int(xs)))
        base = float(horiz[col]) + sc * (camz - m["ground"])
        x0, y0 = int(round(xs)) - bw // 2, int(round(base)) - bh
        cx0, cy0 = max(0, x0), max(0, y0)
        cx1, cy1 = min(W, x0 + bw), min(H, y0 + bh)
        if cx1 <= cx0 or cy1 <= cy0:
            return
        sub = spr[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
        vis = sub & (idx[cy0:cy1, cx0:cx1] >= bisect_right(Zlist, zc))
        fb = int(round(min(zc / far, 1.0) * (NFOG - 1)))
        _putmask(pidx[cy0:cy1, cx0:cx1], vis, m["pidx"] + fb)

    def draw_birds(t, camu, camv, camz, fu, fv, ru, rv):
        pose = bird_masks[int(t * 6.0) % len(bird_masks)]
        bh, bw = pose.shape
        for i in range(nbirds):
            a = 2.0 * math.pi * bird_rate[i] * t + bird_ph[i]
            seat = omega * t + start_phase + 2.0 * math.pi * bird_along[i]
            here = at_where(seat)
            bu_ = here.real + bird_r[i] * math.sin(a) * 1.6
            bv_ = here.imag + bird_r[i] * math.cos(a) * 1.3
            # Height taken off the glider rather than off the route, which
            # saves a second series evaluation per bird per frame and is the
            # same number to within the handful of metres the route climbs
            # over the couple of hundred a bird sits ahead of or behind you.
            bz = camz + bird_dz[i] + 22.0 * math.sin(a * 3.0 + bird_flap[i])
            du, dv = bu_ - camu, bv_ - camv
            zc = du * fu + dv * fv
            if zc < near * 0.4 or zc > 3500.0:
                continue
            xs = 0.5 * W + focal * (du * ru + dv * rv) / zc
            # Same projection the terrain uses, horizon shear and all, or a
            # bird sits at a different attitude from the world behind it.
            ys = (float(horiz[min(W - 1, max(0, int(xs)))])
                  - focal * (bz - camz) / zc)
            x0, y0 = int(round(xs)) - bw // 2, int(round(ys)) - bh // 2
            if not (0 <= x0 and x0 + bw <= W and 0 <= y0 and y0 + bh <= H):
                continue
            if int(idx[y0:y0 + bh, x0:x0 + bw].min()) < bisect_right(Zlist, zc):
                continue
            fb = int(round(min(zc / far, 1.0) * (NFOG - 1)))
            _putmask(pidx[y0:y0 + bh, x0:x0 + bw], pose, bird_pix + fb)

    def render(t, frame):
        # Local aliases for every scratch buffer this function writes through
        # with an augmented assignment. `buf += x` on a closure name does not
        # write through the buffer, it *rebinds the name* -- and because the
        # name is then local for the whole function, the first read of it
        # earlier in the frame raises instead of quietly doing the wrong
        # thing. Which is the good outcome; the bad one is when it does not.
        a_su, a_sv, a_tt, a_mi = su, sv, tt, miv
        a_bk, a_pi, a_ga, a_bp = bkey, pidxt, gath, bump
        a_hz, a_hzt, a_buf, a_b16 = horiz, horizt, buf, buf16

        camu, camv, camz, psi, kappa, pitch = camera(t)
        fu, fv = math.cos(psi), math.sin(psi)
        ru, rv = -fv, fu                          # right hand, looking along f

        # Bank and pitch are a shear and a shift of the horizon rather than a
        # rotation of the rays. At a 27 degree vertical field that is well
        # inside where the approximation shows, and it makes both of them
        # free: every projection below already adds this per-column number.
        # What has to read is *which way* you are banking and that it keeps
        # changing, so the tilt is scaled well down from true and eased into a
        # limit at six degrees -- which still walks the horizon a third of the
        # way up the panel from one edge to the other.
        #
        # tanh rather than min/max. A hard clamp has a corner in it, and a
        # corner in the roll is a corner in the horizon's motion: it stops
        # dead. The soft limiter has the same slope through zero and the same
        # asymptote, and the approach to the bound is smooth, so an unusually
        # tight moment reads as the turn tightening rather than as a stop.
        # With the gain above it barely engages at all; it is there for
        # --bank, --loop and --roll-lag well away from their defaults.
        x = kappa * ROLL_GAIN * args.bank
        roll = math.atan(ROLL_LIMIT * math.tanh(x / ROLL_LIMIT))
        h0 = 0.5 * H + math.tan(pitch) * focal
        tanroll = f32(math.tan(roll))
        np.multiply(colc, tanroll, out=horiz)
        a_hz += f32(h0)
        if coarse:
            # The same shear read at the middle of each doubled column. Two
            # more passes over a hundred and sixty floats, against the sky and
            # the whole march being taken at half width.
            np.multiply(colct, tanroll, out=horizt)
            a_hzt += f32(h0)
        np.multiply(a_hzt, f32(SKY_SUB), out=hrowf)
        np.rint(hrowf, out=hrowf)
        hrow[...] = hrowf
        np.subtract(SKY_MID + SKY0, hrow, out=skyoff)

        # Where the sun is on screen, worked out before anything is drawn
        # because two different things need it: the disc itself, and the
        # columns the glitter path is allowed to light. A sun behind you does
        # neither, and `fdot` is the whole test.
        fdot = sun_dir[0] * fu + sun_dir[1] * fv
        sun_x = sun_y = 0.0
        if fdot > 0.05:
            sun_x = 0.5 * W + focal * (sun_dir[0] * ru + sun_dir[1] * rv) / fdot
            sun_y = (float(horiz[min(W - 1, max(0, int(sun_x)))])
                     - focal * sun_dir[2] / fdot)
            # Slide the baked glitter path so its centre lands on the sun's
            # column. No arithmetic: the whole thing is one slice.
            goff = max(0, min(2 * TW, int(round(1.5 * TW - sun_x / gscale))))
            gsl = glint_field[:, goff:goff + TW]
        else:
            gsl = glint_off

        # ---- the march -----------------------------------------------------
        # Every column at once, and not even a loop over depth: the whole
        # (steps x columns) grid is built in one go and the painter's ordering
        # falls out of a running minimum. A Python loop over depth steps would
        # be a couple of thousand numpy calls a frame, and on a Pi 3 a numpy
        # call costs about 80 us whatever size the array is.
        np.multiply(Zcol, colxt * f32(ru) + f32(fu), out=su)
        a_su += f32(camu)
        np.multiply(Zcol, colxt * f32(rv) + f32(fv), out=sv)
        a_sv += f32(camv)
        a_su *= inv_mx
        a_sv *= inv_my
        # Truncate to a cell first and clamp in integers afterwards, rather
        # than the other way round. It is the same cell -- trunc() of a float
        # clamped to MW-1.001 is MW-2, so that is where the integer clamp goes,
        # and a negative sample truncates towards zero and then clamps to zero
        # exactly as it did -- and an integer clamp is a fifth of the cost of a
        # float one on this numpy. See `int_cells` for the one case where the
        # truncation could overflow and the old order is kept.
        if int_cells:
            tmpi[...] = sv                        # truncation, towards zero
            _clip(tmpi, i32_zero, i_hiv, tmpi)
            np.multiply(tmpi, MW, out=miv)
            tmpi[...] = su
            _clip(tmpi, i32_zero, i_hiu, tmpi)
        else:
            _clip(sv, f32(0.0), f32(MH - 1.001), sv)
            _clip(su, f32(0.0), f32(MW - 1.001), su)
            tmpi[...] = sv                        # truncation, which is a floor
            np.multiply(tmpi, MW, out=miv)
            tmpi[...] = su
        a_mi += tmpi
        # Fancy indexing rather than np.take(out=), which is the one place in
        # this file where allocating beats reusing: `hflat[miv]` is a fifth
        # quicker than the same gather written with an output buffer, because
        # take's out= path re-dispatches the whole thing through a cast that
        # the plain index does not need. The buffer it returns is thrown away
        # a few lines later either way.
        hs = hflat[miv]

        # Screen row of every sample, then the highest reached so far.
        np.subtract(hs, f32(camz), out=tt)
        a_tt *= invZ
        np.subtract(horizt, tt, out=tt)

        # Which depth step is visible in each pixel, without ever looping over
        # rows. Down the depth axis the running minimum only ever decreases, so
        # the number of steps whose ceiling is still below a row *is* the index
        # of the first step that covers it -- and that count, for every row at
        # once, is a histogram of the ceilings followed by a cumulative sum.
        # This is the whole reason the effect fits in a frame.
        #
        # The running minimum is taken *after* the ceiling, the clamp and the
        # narrowing rather than before, which is free to do because all three
        # are monotonic -- ceil(min x) is min(ceil x) and so on -- and it moves
        # the one genuinely serial pass in the frame off floats and onto
        # shorts, where it costs less than half as much.
        np.ceil(tt, out=tt)
        _clip(tt, f32(0.0), f32(H), tt)
        bkey[...] = tt
        np.minimum.accumulate(bkey, axis=0, out=bkey)
        a_bk *= kTW
        a_bk += kcolidx
        # The counts and the scan over them are the same short: a bin holds at
        # most the depth budget and the running total at most the same, so
        # nothing here needs more, and the scan down 64 rows is half the
        # traffic it was. bincount still hands back platform ints.
        hist = _bincount(bkey.reshape(-1), None, nbins)[:nbins]
        hist32[...] = hist
        np.add.accumulate(histv, axis=0, out=acc)
        # One pass that both turns the running count into a step number and
        # widens it back to the int32 everything downstream indexes with, so
        # the narrow scan above costs nothing to undo.
        np.subtract(kN, acc, out=idxt)

        # ---- surface, haze and water ----------------------------------------
        np.multiply(idxt, TW, out=gath)
        a_ga += colidxt
        pidxt[...] = cflat[miflat[gath]]       # the class and shade under it
        a_pi *= NFOG
        a_pi += fogb[idxt]                     # and its haze band

        # Water, in the same integer. A brighter shade is +NFOG on the index,
        # so the chop and the sun's glitter are an integer add on the pixels
        # that are water rather than any colour arithmetic -- and they pick up
        # the right haze for their distance for free.
        #
        # Both tests are made against the uint8 noise where it lies, with a
        # uint8 threshold, so nothing is widened to int32 on the way: the
        # glitter's per-column threshold is a uint8 array of 255s outside the
        # sun's column, and 255 is a number the noise cannot beat.
        ox, oy = int(t * 11.0) % nw, int(t * 3.0) % nh
        ox2, oy2 = int(t * 5.0) % nw, int(t * 2.0) % nh
        np.greater(tex1[oy:oy + H, ox:ox + TW], gsl, out=maskt)
        np.greater(tex2[oy2:oy2 + H, ox2:ox2 + TW], CHOP_U8, out=aux8)
        np.multiply(maskt, TWO_U8, out=bump)      # glitter is two shades up
        a_bp += aux8
        a_bp *= NFOG
        np.less(pidxt, watermax, out=maskt)
        a_bp *= maskt
        a_pi += bump

        # Sky where the march found nothing: the same table, indexed by how
        # far above the horizon the pixel is, so it is one gather for the
        # whole frame and there is no compositing pass anywhere.
        np.add(rows_i3, skyoff, out=gath)
        _clip(gath, SKY_LO, SKY_HI, gath)
        np.equal(idxt, N, out=maskt)
        # putmask, not copyto(where=): three times quicker for the same
        # traffic, because copyto's masked path is a scalar loop. Shapes
        # match, so there is no repeat to reason about.
        _putmask(pidxt, maskt, gath)

        # The landscape doubled out to the panel, if it was drawn at half of
        # it. Two strided copies rather than np.repeat, which allocates and is
        # eight times dearer for the same bytes. The depth image goes with it,
        # because the bridges, the tower and the birds are drawn at full width
        # and depth-test against it.
        if coarse:
            pidx[:, 0::2] = pidxt
            pidx[:, 1::2] = pidxt
            idx[:, 0::2] = idxt
            idx[:, 1::2] = idxt
            np.greater_equal(pidx, SKY_LO, out=mask)

        for b in bridges:
            draw_bridge(b, camu, camv, camz, fu, fv, ru, rv)
        for m in masts:
            draw_mast(m, camu, camv, camz, fu, fv, ru, rv)
        if nbirds:
            draw_birds(t, camu, camv, camz, fu, fv, ru, rv)

        # Re-derive what is still sky now that the bridge and the birds have
        # been written in, because the sun is drawn through that mask and a
        # sun shining through the roadway is the sort of thing nobody notices
        # until they see it once. Only worth a pass if something was drawn.
        if need_z:
            np.greater_equal(pidx, SKY0, out=mask)

        # Where the sun is going to land, in pixels, worked out before the
        # frame is coloured because the fixed-point path below paints
        # everything but that box and leaves it to be done in float.
        sunbox = None
        if fdot > 0.05:
            x0, y0 = int(round(sun_x)) - sun_c, int(round(sun_y)) - sun_c
            cx0, cy0 = max(0, x0), max(0, y0)
            cx1, cy1 = min(W, x0 + sun_w), min(H, y0 + sun_h)
            if cx1 > cx0 and cy1 > cy0:
                sunbox = (x0, y0, cx0, cy0, cx1, cy1)

        if fixed is not None:
            # Gather, dither and quantise in one 16-bit sweep. See
            # fixed_palette(): this is exactly `trunc(pal[pidx] + dith)`, done
            # in half the memory traffic.
            pal16.take(pidx, axis=0, out=buf16, mode="clip")
            a_b16 += cdith
            np.right_shift(buf16, 7, out=buf16)
            out[...] = buf16
            if sunbox is not None:
                # The sun is the one thing in the frame that is not a palette
                # entry -- it is added light, and it can go over full scale --
                # so its box is redone the long way, in float, over the couple
                # of thousand pixels it covers.
                x0, y0, cx0, cy0, cx1, cy1 = sunbox
                sub = pal[pidx[cy0:cy1, cx0:cx1]]
                sub += (sun_spr[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
                        * mask[cy0:cy1, cx0:cx1, None])
                np.minimum(sub, 254.0, out=sub)
                sub += dith[cy0:cy1, cx0:cx1]
                out[cy0:cy1, cx0:cx1] = sub
            return out

        pal.take(pidx, axis=0, out=buf, mode="clip")

        # ---- the sun, seen through the sky -----------------------------------
        # `mask` is still the sky mask, which is exactly the depth test the sun
        # needs: it is at infinity, so anything at all in front of it wins --
        # including the bridge deck, which is the point.
        if sunbox is not None:
            x0, y0, cx0, cy0, cx1, cy1 = sunbox
            sub = buf[cy0:cy1, cx0:cx1]
            sub += (sun_spr[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
                    * mask[cy0:cy1, cx0:cx1, None])
            # The only thing in the frame that can go over full scale, so it is
            # also the only thing that gets clipped. Clamping the whole frame
            # instead was two more passes over 61440 floats for pixels that
            # were already in range by construction: the palette is built
            # clipped and nothing else here adds.
            np.minimum(sub, 254.0, out=sub)

        # The wing, and the dither, in two passes rather than three: the
        # dither is folded into the wing's premultiplied colour at build time,
        # since `buf * inv + pre + dither` is `buf * inv + (pre + dither)`.
        if args.wing:
            a_buf *= wing_inv
            a_buf += wing_dith
        else:
            a_buf += dith
        out[...] = buf                            # truncates, as dither expects
        return out

    return render


def main():
    # 30 fps. Nothing here moves fast enough to want more, and it doubles the
    # per-frame budget on the Pi this has to fit inside.
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
