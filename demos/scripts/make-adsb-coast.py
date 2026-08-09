#!/usr/bin/env python3
"""Bake the coarse Bay Area land/water outline that adsb.py draws under the traffic.

Downloads bare-earth elevation from the USGS 3D Elevation Program, works out
which of it is water, and writes one small compressed .npz next to the demo.

    python3 scripts/make-adsb-coast.py                # rebuild demos/adsb-coast.npz
    python3 scripts/make-adsb-coast.py --cells 512    # coarser, smaller

Why this exists rather than `voxel-dem.npz`
-------------------------------------------
`voxel-dem.npz` already carries a sea mask for this bay, and adsb.py would much
rather have reused it. It cannot: that DEM's box is 37.635-38.035 N,
122.680-122.280 W, and **SFO is at 37.619 N** -- a mile and a half south of its
bottom edge. An air traffic panel whose map stops just short of the airport
every one of its aircraft is going to or coming from is not a map worth
drawing, and neither is one whose east edge is Berkeley when half the arrivals
come over the Diablo range. So this is a second, much wider and much coarser
bake: three times the area of the voxel DEM, at an eighth of its resolution,
and no elevation at all.

That is the whole trade. The voxel demo flies over its terrain and needs
metre-accurate heights; this demo draws a coastline three hundred metres to the
pixel and needs nothing else, so it stores one bit per cell and comes out a
tenth the size. Two files, each honest about what it is for, beats one file
that is wrong for both.

Provenance
----------
  Source     USGS 3D Elevation Program (3DEP), 1/3 arc-second seamless DEM,
             served by The National Map's 3DEPElevation ImageServer:
             https://elevation.nationalmap.gov/arcgis/rest/services/
                 3DEPElevation/ImageServer
  Datum      NAVD88 heights, WGS84 lat/lon (EPSG:4326)
  Extent     37.400 N .. 38.000 N, 123.000 W .. 121.800 W
  Native     ~10 m posting; resampled here to ~103 x 130 m cells
  Licence    Public domain. USGS data carries no copyright; see
             https://www.usgs.gov/information-policies-and-instructions/
                 copyrights-and-credits
  Retrieved  2026-08-09

The box is a little outside adsb.py's map crop on every side, so that resampling
the mask to the panel is never an extrapolation off the edge of the array. What
is in it: the whole of San Francisco Bay from the Carquinez Strait down to the
bottom of the South Bay, the Golden Gate and the Marin headlands, the Pacific
out past the shipping lanes, SFO, OAK, SJC, Hayward, Palo Alto and Half Moon
Bay, and the Diablo range on the east edge that the eastern arrivals cross.

This is a bake script, not part of the demo. It needs the network and Pillow;
adsb.py needs neither, and reads only the committed .npz.
"""

import argparse
import io
import os
import sys
import urllib.request

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMOS = os.path.dirname(_HERE)

SERVICE = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
           "3DEPElevation/ImageServer/exportImage")

# lon_w, lat_s, lon_e, lat_n. Exactly 2:1 in degrees, and the requested image
# is exactly 2:1 in pixels, because the ImageServer will not letterbox: hand it
# a bbox whose aspect disagrees with the size and it silently widens the bbox
# and returns *that*, with nothing in the response to say so. make-voxel-dem.py
# lost an afternoon to this and put Mount Tamalpais three kilometres south of
# itself; the same trap is here and the same discipline avoids it.
BBOX = (-123.000, 37.400, -121.800, 38.000)

# Anything at or below this, and connected to the open water at the edge of the
# map, is sea. The threshold alone is not enough -- the salt ponds at Alviso,
# the Alameda flats and the reclaimed land along the bay shore all sit within a
# metre or two of datum -- and the connectivity is what keeps them from coming
# out as lagoons scattered across the South Bay. Same number and same reasoning
# as make-voxel-dem.py; it was arrived at there against a shoreline that can be
# checked against a chart.
SEA_LEVEL = 1.4

# Mount Diablo is 1173 m and is the highest ground in this box. Anything above
# this is the NoData sentinel showing through rather than a mountain.
CEILING = 1300.0


def fetch(bbox, cols, rows):
    """Pull the whole grid as float32 metres in one request.

    One request rather than tiles, for the reason make-voxel-dem.py gives: the
    server resamples each request against its own alignment, so two tiles
    disagree by a metre or so along the join. That is invisible in a coastline
    -- a metre of step either side of a seam is still land on both sides -- but
    it costs nothing to avoid and the whole grid is 8 MB.
    """
    from PIL import Image

    lon0, lat0, lon1, lat1 = bbox
    # See BBOX. This is the check that stops a silently georeferenced-wrong
    # grid, which is the failure mode that looks entirely fine.
    if abs(((lon1 - lon0) / (lat1 - lat0)) - (float(cols) / rows)) > 1e-6:
        raise SystemExit("bbox is %.4f x %.4f degrees but the image is %dx%d; "
                         "the aspects must match or the server will widen the "
                         "box" % (lon1 - lon0, lat1 - lat0, cols, rows))
    url = (SERVICE + "?bbox=%.6f,%.6f,%.6f,%.6f" % (lon0, lat0, lon1, lat1)
           + "&bboxSR=4326&imageSR=4326&size=%d,%d" % (cols, rows)
           + "&format=tiff&pixelType=F32&f=image"
           + "&interpolation=RSP_BilinearInterpolation"
           + "&noDataInterpretation=esriNoDataMatchAny")
    sys.stderr.write("  GET %dx%d\n" % (cols, rows))
    with urllib.request.urlopen(url, timeout=600) as r:
        blob = r.read()
    a = np.asarray(Image.open(io.BytesIO(blob)), np.float32)
    if a.shape != (rows, cols):
        raise SystemExit("came back %s, wanted %s" % (a.shape, (rows, cols)))
    return a


def voids(a):
    """Where the DEM has nothing to say. 3DEP stops at the continental shelf.

    NoData comes back as a float32 sentinel around 3.4e38 and bilinear
    resampling *interpolates* it, so cells for a few hundred metres inside the
    coverage edge come back as NaN, as absurd finite numbers, or as plausible
    900 m peaks standing in the middle of the ocean. The test is therefore a
    plausibility range rather than equality with a magic value, and the result
    is grown a couple of cells to catch the contaminated ring around each void.

    Everything this finds out here is the Pacific, so the caller calls it sea.
    """
    bad = ~np.isfinite(a) | (a > CEILING) | (a < -100.0)
    for _ in range(2):
        grown = bad.copy()
        grown[1:] |= bad[:-1]
        grown[:-1] |= bad[1:]
        grown[:, 1:] |= bad[:, :-1]
        grown[:, :-1] |= bad[:, 1:]
        bad = grown
    return bad


def boxdown(a, factor):
    """Average `factor` x `factor` blocks. Cheaper aliasing than point sampling."""
    h, w = a.shape
    h -= h % factor
    w -= w % factor
    return a[:h, :w].reshape(h // factor, factor, w // factor, factor).mean((1, 3))


def sea_mask(low):
    """Water: at or below datum *and* reachable from the edge of the map.

    An iterative four-neighbour dilation rather than a real flood fill, because
    scipy is not a dependency here. It converges once nothing changes, which on
    this grid takes a couple of thousand steps -- the Bay is long and thin and
    the frontier crawls up it a cell at a time -- so the frontier is tracked
    rather than the whole set being re-dilated each pass.

    What this deliberately excludes is every inland reservoir: Crystal Springs,
    San Andreas, Calaveras, Del Valle, Lexington. None of them drains to the
    sea through anything a 103 m grid can see, so all of them come out as land.
    On a panel where one pixel is three hundred metres that is the right
    answer: they would be two or three pixels of water in the hills and would
    read as noise in the coastline rather than as lakes.
    """
    cur = np.zeros_like(low)
    cur[0], cur[-1], cur[:, 0], cur[:, -1] = True, True, True, True
    cur &= low
    out = cur.copy()
    while True:
        nxt = np.zeros_like(cur)
        nxt[1:] |= cur[:-1]
        nxt[:-1] |= cur[1:]
        nxt[:, 1:] |= cur[:, :-1]
        nxt[:, :-1] |= cur[:, 1:]
        nxt &= low & ~out
        if not nxt.any():
            return out
        out |= nxt
        cur = nxt


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--cells", type=int, default=1024,
                    help="output grid width; height is half of it")
    ap.add_argument("--super", type=int, default=2,
                    help="download this many times finer and box-average down")
    ap.add_argument("--out", default=os.path.join(_DEMOS, "adsb-coast.npz"))
    ap.add_argument("--cache", default="/tmp/adsb-coast-raw.npy",
                    help="keep the downloaded grid here, to re-bake offline")
    args = ap.parse_args()

    cols, rows = args.cells, args.cells // 2
    n = cols * args.super, rows * args.super
    raw = None
    if os.path.exists(args.cache):
        raw = np.load(args.cache)
        if raw.shape != (n[1], n[0]):
            raw = None
        else:
            sys.stderr.write("using cached %s\n" % args.cache)
    if raw is None:
        sys.stderr.write("fetching %dx%d from 3DEP\n" % n)
        raw = fetch(BBOX, n[0], n[1])
        np.save(args.cache, raw)

    # Voids are found at the fetched resolution and flattened before the
    # average, or one sentinel would poison every cell of its block.
    void = voids(raw)
    sys.stderr.write("  void: %.2f%% of fetched cells\n" % (100.0 * void.mean()))
    raw = np.where(void, np.float32(0.0), raw)

    s = args.super
    elev = boxdown(raw, s).astype(np.float32) if s > 1 else raw.copy()
    void = boxdown(void.astype(np.float32), s) > 0.5 if s > 1 else void

    # Box-averaging a shoreline gives a fractional cell that is neither land nor
    # sea, and the threshold is applied *after* it on purpose: a half-water cell
    # averages to half the sea level and lands on the water side, which grows
    # the drawn water by half a cell and keeps narrow channels -- Raccoon
    # Strait, the Carquinez Strait, the sloughs at the bottom of the Bay -- open
    # instead of pinching them shut.
    sea = sea_mask((elev <= SEA_LEVEL) | void)

    lon0, lat0, lon1, lat1 = BBOX
    np.savez_compressed(
        args.out, sea=np.packbits(sea, axis=None),
        shape=np.array(sea.shape, np.int32),
        bbox=np.array(BBOX, np.float64))
    size = os.path.getsize(args.out)
    sys.stderr.write("%s  %dx%d  %.0f x %.0f m cells  %.1f%% water  %.1f kB\n"
                     % (args.out, sea.shape[1], sea.shape[0],
                        (lon1 - lon0) * 88100.0 / sea.shape[1],
                        (lat1 - lat0) * 110574.0 / sea.shape[0],
                        100.0 * sea.mean(), size / 1024.0))


if __name__ == "__main__":
    main()
