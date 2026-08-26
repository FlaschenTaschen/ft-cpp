#!/usr/bin/env python3
"""Amiga Boing Ball.

The 1984 Amiga demo: a red and white checkered sphere spinning about a tilted
axis, bouncing around a purple wireframe room, with a shadow behind it.

The sphere is never actually projected per frame. For a ball of fixed radius
the mapping from a pixel of its bounding box to the point of the surface
facing the viewer never changes, so the surface coordinates -- latitude and
longitude, in the ball's own tilted frame -- are computed once at startup,
along with the edge shading and the silhouette mask. A frame is then: add the
current spin to the precomputed longitude, truncate to a checker cell, xor
with the precomputed latitude cell, and pick red or white. An add, two integer
ops and a select, over the 56x56 pixels the ball can cover.

Run:  python3 boing.py --host 127.0.0.1
      python3 boing.py --segments 12 --bands 6 --spin 0.5
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32


# --------------------------------------------------------------------------
# The pieces, each baked once.
# --------------------------------------------------------------------------

def sphere_tables(radius, tilt, segments, bands):
    """Precompute everything about the ball that does not depend on time.

    Returns (mask, lat_cell, lon_cells, shade) over the ball's bounding box.
    `lon_cells` is longitude already scaled into checker cells, so the spin is
    an add; `lat_cell` is the latitude cell index, so the checker is an xor.
    """
    n = 2 * radius + 1
    yy, xx = np.mgrid[0:n, 0:n].astype(f32)
    # Sample pixel centres relative to the ball centre, in units of radius.
    px = (xx - radius) / radius
    py = (yy - radius) / radius
    d2 = px * px + py * py
    mask = d2 <= 1.0
    pz = np.sqrt(np.maximum(1.0 - d2, 0.0))          # towards the viewer

    # Rotate the viewing frame into the ball's frame: the spin axis leans by
    # `tilt` in the plane of the screen. That lean is a signature part of the
    # look -- with a vertical axis it reads as a beach ball, not the Boing.
    ca, sa = np.cos(tilt), np.sin(tilt)
    lx = px * ca + py * sa
    ly = -px * sa + py * ca
    lz = pz

    # Latitude off the ball's equator, longitude around its axis. asin/atan2
    # are what make the checkers crowd together towards the silhouette, which
    # is the whole reason this reads as a sphere and not a printed disc.
    lat = np.arcsin(np.clip(-ly, -1.0, 1.0))
    lon = np.arctan2(lx, lz)

    lat_cell = np.floor((lat / np.pi + 0.5) * bands).astype(np.int32)
    lat_cell = np.clip(lat_cell, 0, bands - 1)
    # Bias well positive so the per-frame truncation towards zero behaves like
    # a floor: a cell boundary landing on 0 would otherwise double in width.
    lon_cells = ((lon / (2.0 * np.pi)) * segments + 4096.0 * segments).astype(f32)

    # Light from the upper left, plus enough ambient that the dark side keeps
    # its checkers. Shading is a single factor applied to both colours, so the
    # checker edges stay hard.
    nx, ny, nz = px, py, pz
    lam = np.clip(nx * -0.45 + ny * -0.55 + nz * 0.70, 0.0, 1.0)
    shade = (0.42 + 0.58 * lam).astype(f32)
    # A touch of extra falloff right at the rim so the silhouette does not end
    # on a bright pixel and look cut out.
    shade *= (0.55 + 0.45 * np.clip(pz * 3.0, 0.0, 1.0)).astype(f32)
    return mask, lat_cell, lon_cells, shade.astype(f32)


def shadow_table(radius, softness=1.5):
    """A soft round silhouette, as a multiply-by factor over its own box."""
    n = 2 * radius + 1
    yy, xx = np.mgrid[0:n, 0:n].astype(f32)
    r = np.hypot(xx - radius, yy - radius)
    return np.clip((radius - r) / max(softness, 1e-3), 0.0, 1.0).astype(f32)


def background(W, H, args):
    """The wireframe room: a magenta grid inside a darker border."""
    bg = np.empty((H, W, 3), np.uint8)
    bg[:] = np.array(args.bg_color, np.uint8)
    line = np.array(args.grid_color, np.uint8)

    border = max(2, H // 16)
    ix0, iy0 = border, border
    ix1, iy1 = W - border, H - border
    inner_w, inner_h = ix1 - ix0, iy1 - iy0

    # Whole numbers of cells across the room, so the grid meets the border
    # squarely instead of ending on a sliver.
    cols = max(2, int(round(inner_w / float(args.grid))))
    rows = max(2, int(round(inner_h / float(args.grid))))
    for i in range(cols + 1):
        x = ix0 + int(round(i * inner_w / cols))
        bg[iy0:iy1 + 1, min(x, ix1)] = line
    for j in range(rows + 1):
        y = iy0 + int(round(j * inner_h / rows))
        bg[min(y, iy1), ix0:ix1 + 1] = line

    # The border itself: darker than the grid, brighter than the field, so the
    # room has a frame rather than just running off the edge of the panel.
    edge = np.array(args.border_color, np.uint8)
    bg[:iy0, :] = edge
    bg[iy1 + 1:, :] = edge
    bg[:, :ix0] = edge
    bg[:, ix1 + 1:] = edge
    return bg


# --------------------------------------------------------------------------
# Compositing.
# --------------------------------------------------------------------------

def clip_box(x0, y0, n, W, H):
    """Intersect an n x n box placed at (x0, y0) with the canvas.

    Returns (dst_slice, src_slice) or None if it misses entirely.
    """
    sx0, sy0 = max(0, -x0), max(0, -y0)
    sx1, sy1 = n - max(0, x0 + n - W), n - max(0, y0 + n - H)
    if sx0 >= sx1 or sy0 >= sy1:
        return None
    dst = (slice(y0 + sy0, y0 + sy1), slice(x0 + sx0, x0 + sx1))
    src = (slice(sy0, sy1), slice(sx0, sx1))
    return dst, src


def color_arg(text):
    """--red 255,60,60 -> (255, 60, 60)."""
    parts = [int(v) for v in text.replace(" ", "").split(",")]
    if len(parts) != 3 or not all(0 <= v <= 255 for v in parts):
        raise ValueError("expected three values 0..255, got %r" % text)
    return tuple(parts)


def add_arguments(ap):
    ap.add_argument("--radius", type=int, default=0,
                    help="ball radius in pixels (0 = fit the panel height)")
    ap.add_argument("--segments", type=int, default=0,
                    help="checker cells around the ball (0 = fit the radius)")
    ap.add_argument("--bands", type=int, default=0,
                    help="checker cells pole to pole (0 = fit the radius)")
    ap.add_argument("--tilt", type=float, default=17.0,
                    help="lean of the spin axis, degrees")
    ap.add_argument("--spin", type=float, default=0.55,
                    help="turns/sec; reverses when the ball hits a wall")
    ap.add_argument("--speed", type=float, default=70.0,
                    help="horizontal travel, pixels/sec")
    ap.add_argument("--bounce", type=float, default=0.85,
                    help="seconds to fall from the top of the room")
    ap.add_argument("--shadow", type=float, default=0.62,
                    help="how far the shadow darkens the grid, 0..1")
    ap.add_argument("--shadow-offset", type=float, default=0.42,
                    help="shadow offset down and right, in ball radii")
    ap.add_argument("--grid", type=int, default=8, help="grid cell size, pixels")
    ap.add_argument("--red", type=color_arg, default=(230, 20, 40))
    ap.add_argument("--white", type=color_arg, default=(240, 240, 245))
    ap.add_argument("--grid-color", type=color_arg, default=(150, 30, 165))
    ap.add_argument("--bg-color", type=color_arg, default=(16, 2, 22))
    ap.add_argument("--border-color", type=color_arg, default=(58, 8, 70))


def build(args):
    W, H = args.width, args.height

    # The panel is far wider than it is tall, so the height is what limits the
    # ball -- and it has to limit it by more than the border, or there is no
    # room left above the floor for the ball to bounce in. A third of the
    # height keeps the checkers readable while leaving a visible arc.
    radius = args.radius or max(3, int(round(H * 0.32)))
    radius = max(3, min(radius, (H - 2) // 2, (W - 2) // 2))
    n = 2 * radius + 1

    # The Boing ball is 16 segments by 8 bands, but that is for a ball far
    # bigger than this panel can hold. A cell is roughly 4R/segments pixels
    # wide at the centre of the disc and much narrower at the silhouette, so
    # below about a 40 pixel ball the classic counts alias into speckle.
    # Scaling the counts with the radius keeps the cells five-ish pixels
    # across whatever the ball ends up being; even counts keep the equator on
    # a cell boundary and the checker consistent across the longitude wrap.
    def fit(x, lo, hi):
        return int(min(hi, max(lo, 2 * round(x / 2.0))))

    segments = max(2, args.segments) if args.segments else fit(radius * 0.6, 4, 16)
    bands = max(2, args.bands) if args.bands else fit(radius * 0.36, 4, 8)
    mask, lat_cell, lon_cells, shading = sphere_tables(
        radius, np.radians(args.tilt), segments, bands)

    # Both colours pre-shaded, so a frame picks between two ready pixels.
    red = (np.array(args.red, f32) * shading[..., None]).clip(0, 255).astype(np.uint8)
    white = (np.array(args.white, f32) * shading[..., None]).clip(0, 255).astype(np.uint8)

    shadow_mul = (1.0 - args.shadow * shadow_table(radius)).astype(f32)[..., None]
    sx_off = int(round(args.shadow_offset * radius))
    sy_off = int(round(args.shadow_offset * radius * 0.5))

    bg = background(W, H, args)
    buf = np.empty((H, W, 3), np.uint8)

    # Bounds for the ball centre. The room's border is a wall it rolls along.
    inset = max(2, H // 16)
    x_lo, x_hi = inset + radius, W - inset - radius - 1
    y_lo, y_hi = inset + radius, H - inset - radius - 1
    if x_hi < x_lo:
        x_lo = x_hi = W // 2
    if y_hi < y_lo:
        y_lo = y_hi = H // 2

    # Motion is analytic rather than integrated: a dropped frame then costs a
    # frame, not a drifting trajectory, and render() stays stateless.
    span_x = max(1.0, float(x_hi - x_lo))
    period_x = 2.0 * span_x / max(args.speed, 1e-3)
    fall = max(0.05, args.bounce)
    grav = 2.0 * max(1.0, float(y_hi - y_lo)) / (fall * fall)

    def render(t, frame):
        # Horizontal: fold time into a there-and-back sweep.
        u = (t % period_x) / period_x                    # 0..1 of the round trip
        tri = 2.0 * u if u < 0.5 else 2.0 * (1.0 - u)
        cx = int(round(x_lo + tri * span_x))

        # Vertical: fall under gravity, reflect at the floor, so the motion
        # arcs instead of tracing a straight reflection off the walls.
        v = t % (2.0 * fall)
        dt = v if v < fall else 2.0 * fall - v
        cy = int(round(min(y_lo + 0.5 * grav * dt * dt, float(y_hi))))

        # Spin off the same folded time as the horizontal sweep, in checker
        # cells: constant rate, and it reverses exactly on a wall hit, the way
        # a rolling ball would.
        phase = args.spin * segments * (tri * 0.5 * period_x)

        np.copyto(buf, bg)

        box = clip_box(cx - radius + sx_off, cy - radius + sy_off, n, W, H)
        if box is not None:
            dst, src = box
            region = buf[dst]
            np.multiply(region, shadow_mul[src], out=region, casting="unsafe")

        box = clip_box(cx - radius, cy - radius, n, W, H)
        if box is not None:
            dst, src = box
            # The whole per-frame cost of the ball: one add, a truncation, an
            # xor for the checker and a select between two baked colours.
            cells = (lon_cells[src] + f32(phase)).astype(np.int32)
            odd = ((cells ^ lat_cell[src]) & 1).astype(bool)[..., None]
            pick = np.where(odd, white[src], red[src])
            buf[dst] = np.where(mask[src][..., None], pick, buf[dst])

        return buf

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0])


if __name__ == "__main__":
    main()
