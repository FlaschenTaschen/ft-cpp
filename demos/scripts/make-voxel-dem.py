#!/usr/bin/env python3
"""Bake the San Francisco Bay elevation model that voxel.py flies over.

Downloads bare-earth elevation from the USGS 3D Elevation Program, resamples
it to the small grid the demo actually needs, works out which of it is water,
and writes one compressed .npz next to the demo.

    python3 scripts/make-voxel-dem.py                 # rebuild demos/voxel-dem.npz
    python3 scripts/make-voxel-dem.py --cells 512     # coarser, smaller

This is a bake script, not part of the demo. It needs the network and Pillow;
voxel.py needs neither, and reads only the committed .npz. That split is the
same one previews/ uses, and for the same reason -- the Pi boots into the
rotation with no guarantee that anything else on the internet is reachable,
and a demo that phones home for its terrain is a demo that is sometimes a
black rectangle.

Provenance
----------
  Source     USGS 3D Elevation Program (3DEP), 1/3 arc-second seamless DEM,
             served by The National Map's 3DEPElevation ImageServer:
             https://elevation.nationalmap.gov/arcgis/rest/services/
                 3DEPElevation/ImageServer
  Datum      NAVD88 heights, WGS84 lat/lon (EPSG:4326)
  Extent     37.635 N .. 38.035 N, 122.680 W .. 122.280 W
  Native     ~10 m posting; resampled here to 45.8 x 57.6 m cells
  Licence    Public domain. USGS data carries no copyright; see
             https://www.usgs.gov/information-policies-and-instructions/
                 copyrights-and-credits
  Retrieved  2026-08-07

The extent is chosen for what is *in* it: Mount Tamalpais in the northwest,
the Marin Headlands and Hawk Hill over the strait, the Golden Gate itself, the
whole San Francisco peninsula out to Twin Peaks, Mount Davidson and San Bruno
Mountain, Angel Island, Alcatraz and Yerba Buena in the bay, and the Berkeley
hills on the east edge. Mount Diablo is a further 30 km east and does not fit
at a cell size that still reads in the foreground; extending to it would have
doubled the map for a bump on the horizon.
"""

import argparse
import io
import math
import os
import sys
import urllib.request

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMOS = os.path.dirname(_HERE)

SERVICE = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
           "3DEPElevation/ImageServer/exportImage")

# lon_w, lat_s, lon_e, lat_n
#
# Both spans are 0.4 degrees, and that is not tidiness -- it is the only way to
# ask this service a question it will answer honestly. The request below asks
# for a square *image*, and the ImageServer will not letterbox: if the bbox
# aspect and the size aspect disagree it silently widens the bbox until they
# agree and returns that instead, with nothing in the response to say so. This
# box was 0.31 degrees of latitude for a while, and what came back was 0.4 --
# so the grid held 44 km of Marin and California labelled as 34 km, Mount
# Tamalpais sat three kilometres south of where it is, and the Bay Bridge got
# built a kilometre clear of Yerba Buena Island. Everything looked plausible,
# which is what made it expensive. Match the aspect and the box is the box.
#
# Cells are therefore not square in metres -- 45.8 east-west against 57.6
# north-south, because a degree of longitude here is 79% of a degree of
# latitude -- and both numbers go in the file for the demo to work in.
BBOX = (-122.680, 37.635, -122.280, 38.035)

# Anything at or below this, and connected to the open water at the edge of
# the map, is sea. A flat threshold on its own is wrong: Crissy Field, the
# Marina fill and the flats around Bay Farm sit within a couple of metres of
# datum and would come out as lagoons in the middle of the city. Requiring
# connectivity to the ocean fixes that without needing a hydrography layer.
SEA_LEVEL = 1.4


def fetch(bbox, cells):
    """Pull the whole DEM as float32 metres in one request.

    One request rather than tiles on purpose. Tiling this worked and looked
    fine until the check render, which showed a hard horizontal step across
    the middle of San Francisco exactly on the tile boundary: the server
    resamples each request against its own alignment, so two tiles disagree
    by a metre or two along the join, and a metre or two of step is a
    kilometre-long cliff once you are flying at it. 1536 square is 9 MB of
    float32 and the service serves it without complaint.
    """
    from PIL import Image

    lon0, lat0, lon1, lat1 = bbox
    # The one check worth making before the request. See BBOX: a bbox whose
    # aspect does not match the requested image gets quietly widened, and the
    # result is a grid that is georeferenced wrong and looks entirely fine.
    if abs((lon1 - lon0) / (lat1 - lat0) - 1.0) > 1e-6:
        raise SystemExit("bbox is %.4f x %.4f degrees; a square image needs a "
                         "square box or the server will widen it"
                         % (lon1 - lon0, lat1 - lat0))
    url = (SERVICE + "?bbox=%.6f,%.6f,%.6f,%.6f" % (lon0, lat0, lon1, lat1)
           + "&bboxSR=4326&imageSR=4326&size=%d,%d" % (cells, cells)
           + "&format=tiff&pixelType=F32&f=image"
           + "&interpolation=RSP_BilinearInterpolation"
           + "&noDataInterpretation=esriNoDataMatchAny")
    sys.stderr.write("  GET %dx%d\n" % (cells, cells))
    with urllib.request.urlopen(url, timeout=600) as r:
        blob = r.read()
    a = np.asarray(Image.open(io.BytesIO(blob)), np.float32)
    if a.shape != (cells, cells):
        raise SystemExit("came back %s, wanted %s" % (a.shape, (cells, cells)))
    return a


# No real ground in this box is above Mt Tamalpais at 785 m, so anything
# over this is the NoData sentinel showing through rather than a mountain.
CEILING = 850.0


def voids(a):
    """Where the DEM has nothing to say. 3DEP stops at the continental shelf.

    NoData comes back as a float32 sentinel around 3.4e38, and asking for
    bilinear resampling means the server *interpolates* it: cells for a few
    hundred metres inside the coverage edge come back as NaN, or as absurd
    finite numbers, or -- the ones that cost an afternoon -- as perfectly
    plausible-looking 900 m peaks sitting in the middle of the bay. So the
    test is a plausibility range rather than equality with a magic value,
    and the result is grown by a couple of cells to catch the partly
    contaminated ring around each void that no threshold can separate from
    real terrain.

    Everything it finds here is ocean, so the caller fills it with sea.
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


def despike(elev, jump=70.0, passes=2):
    """Knock down cells that tower over all four of their neighbours.

    What is left after the void grow is the odd isolated cell, and an
    isolated cell is exactly what a voxel raycast turns into a black spike
    standing out of the bay. Real terrain has neighbours: at 45 m cells a
    genuine summit is at most a few tens of metres above the ring around it,
    so a cell 70 m clear of every neighbour is not a summit.
    """
    for _ in range(passes):
        nb = np.zeros_like(elev)
        nb[1:] = np.maximum(nb[1:], elev[:-1])
        nb[:-1] = np.maximum(nb[:-1], elev[1:])
        nb[:, 1:] = np.maximum(nb[:, 1:], elev[:, :-1])
        nb[:, :-1] = np.maximum(nb[:, :-1], elev[:, 1:])
        hit = elev > nb + jump
        if not hit.any():
            break
        elev[hit] = nb[hit]
    return elev


def boxdown(a, factor):
    """Average `factor` x `factor` blocks. Cheaper aliasing than point sampling."""
    h, w = a.shape
    h -= h % factor
    w -= w % factor
    return a[:h, :w].reshape(h // factor, factor, w // factor, factor).mean((1, 3))


def sea_mask(low):
    """Water: at or below datum *and* reachable from the edge of the map.

    An iterative dilation rather than a real flood fill, because scipy is not
    a dependency here and 30-odd whole-array steps on a 800x800 grid is under
    a second. Each step is the four-neighbour dilation of the frontier
    intersected with the low ground, and it converges once nothing changes.
    """
    seed = np.zeros_like(low)
    seed[0], seed[-1], seed[:, 0], seed[:, -1] = True, True, True, True
    cur = low & seed
    while True:
        nxt = cur.copy()
        nxt[1:] |= cur[:-1]
        nxt[:-1] |= cur[1:]
        nxt[:, 1:] |= cur[:, :-1]
        nxt[:, :-1] |= cur[:, 1:]
        nxt &= low
        if nxt.sum() == cur.sum():
            return nxt
        cur = nxt


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--cells", type=int, default=768,
                    help="output grid size (square)")
    ap.add_argument("--super", type=int, default=2,
                    help="download this many times finer and box-average down")
    ap.add_argument("--out", default=os.path.join(_DEMOS, "voxel-dem.npz"))
    ap.add_argument("--cache", default="/tmp/voxel-dem-raw.npy",
                    help="keep the downloaded grid here, to re-bake offline")
    args = ap.parse_args()

    n = args.cells * args.super
    raw = None
    if os.path.exists(args.cache):
        raw = np.load(args.cache)
        if raw.shape != (n, n):
            raw = None
        else:
            sys.stderr.write("using cached %s\n" % args.cache)
    if raw is None:
        sys.stderr.write("fetching %dx%d from 3DEP\n" % (n, n))
        raw = fetch(BBOX, n)
        np.save(args.cache, raw)

    # Voids are found at the fetched resolution and flattened before the
    # average, or one sentinel would poison every cell of its 2x2 block.
    void = voids(raw)
    sys.stderr.write("  void: %.2f%% of fetched cells\n" % (100.0 * void.mean()))
    raw = np.where(void, np.float32(0.0), raw)

    s = args.super
    elev = boxdown(raw, s).astype(np.float32) if s > 1 else raw.copy()
    void = boxdown(void.astype(np.float32), s) > 0.0 if s > 1 else void
    elev = despike(elev)

    # Water is stored as exactly zero so the demo's water test is a compare
    # against one number rather than a second array to carry around; a bay
    # whose bed is a metre below datum is not something a 64 row panel has an
    # opinion about.
    sea = sea_mask((elev <= SEA_LEVEL) | void)
    elev[sea] = 0.0
    elev = np.maximum(elev, 0.0)

    # Quantised to whole metres and stored as the horizontal difference. The
    # grid is smooth, so the differences are small integers clustered around
    # zero and DEFLATE gets four or five times what it gets on the raw
    # heights -- which is the difference between an asset you commit and one
    # you do not. int16 rather than uint8: 4 m steps came out as visible
    # terracing on the long shallow slopes above Rodeo Lagoon, and a metre is
    # under what the panel can show anywhere.
    h = np.rint(elev).astype(np.int16)
    dh = h.copy()
    dh[:, 1:] -= h[:, :-1]

    lon0, lat0, lon1, lat1 = BBOX
    mid = math.radians(0.5 * (lat0 + lat1))
    # Metres per cell, on a local equirectangular approximation. The demo
    # works in metres and needs both, because a degree of longitude here is
    # only 79% of a degree of latitude and square cells would put Mt Tam in
    # the wrong place relative to the Gate.
    mx = (lon1 - lon0) * 111320.0 * math.cos(mid) / elev.shape[1]
    my = (lat1 - lat0) * 110574.0 / elev.shape[0]

    np.savez_compressed(
        args.out, dh=dh, sea=np.packbits(sea, axis=None),
        shape=np.array(sea.shape, np.int32),
        bbox=np.array(BBOX, np.float64),
        metres=np.array([mx, my], np.float32))
    size = os.path.getsize(args.out)
    sys.stderr.write(
        "%s  %dx%d  %.1f x %.1f m cells  max %.0f m  %.1f%% water  %.1f kB\n"
        % (args.out, elev.shape[1], elev.shape[0], mx, my, elev.max(),
           100.0 * sea.mean(), size / 1024.0))


if __name__ == "__main__":
    main()
