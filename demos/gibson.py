#!/usr/bin/env python3
"""Flying through the Gibson: a city of data, seen from the windshield.

Hackers (1995) had to show what it looks like to be inside a mainframe, and
what it chose was a night flight over a city -- glowing wireframe towers
rushing past on both sides, a grid floor running to a vanishing point, and no
horizon to speak of. It is a completely fictional way to depict a computer and
it is the most durable image the genre produced, because it gets one thing
right that the accurate depictions do not: it has *speed* in it.

A 320x64 panel is the right shape for this and the wrong shape for almost
anything else 3D. A 5:1 letterbox has a vertical field of view of about 25
degrees, which would ruin a tunnel or a landscape -- but it is exactly what you
see through a windscreen, and the towers only have to be tall enough to leave
the top of the frame to read as tall.

**Every line is drawn in one operation.** The obvious way to draw a few hundred
wireframe edges is a loop with a couple of numpy calls per edge, which on the
Pi 3 driving this wall would be several hundred calls at 55-80 microseconds of
overhead each -- 20 ms before a single pixel is written. So instead: all of the
edges are projected as arrays, clipped to the frame with a vectorised
Liang-Barsky, sorted into four length classes, sampled into one flat cloud of
points, and written in a single indexed assignment. Two hundred and eighty
edges cost the same handful of operations as one, and the cost is set by the
size of the point cloud rather than by how many objects are in the scene.

**The whole shape of this file is what the Pi measured, not what read well.**
Three separate attempts at making it fast were wrong, and each is worth
recording because none of them is obvious from the desktop:

  - It began accumulating with np.bincount, which sums where points coincide
    and so makes crossing lines glow for free. On the Pi that costs 8.5 to
    10 ms for three calls *regardless of how few points go in* -- the price is
    the 20480-bin output and the float64 it insists on, not the cloud. Three
    indexed assignments over the same data cost 1 to 2 ms. The glow was not
    worth a fixed 9 ms, so lines are written rather than summed and where two
    cross the pixel is whichever was laid down last.
  - Making the length classes finer halved the point cloud and changed the
    frame time by *nothing*, because each class costs about fifteen array
    operations whatever is in it and the two effects cancelled exactly. Four
    classes is the setting that won.
  - np.linspace was being called once per class per frame to produce a fixed
    array of numbers, at 3.9 ms of a 45 ms frame. It is baked in build() now.

Together those took the frame from 44 ms to 23 on the wall's own hardware. The
colour is packed into one integer per point rather than carried as three
channels for the same reason: it makes the concatenation and the write
singular.

**Depth is the only shading.** There is no lighting model. An edge's weight
falls off with distance, so the far end of the city is a dim haze and the tower
about to pass the camera is white-hot; and because a tower crossing the near
plane would otherwise pop out of existence, the same weight is faded to nothing
over the last few units before the clip. Edges too faint to see are dropped
before any work is done on them, which matters more than it sounds: those are
also the longest ones on screen, and culling them halved the worst frame.
Nothing is ever cut off mid-flight -- things dissolve into the fog at one end
and past the windscreen at the other.

**It loops exactly.** The tower field repeats every 112 units and the camera
covers that in a fixed time, so the flight is periodic; the slow sway that
keeps it from feeling like it is on rails is given exactly half that frequency,
so the sway and the field come back into phase together rather than beating
against each other forever. render() is a pure function of t with no state
between frames, which is what ftsched, the preview baker and the wall's own
drifting frame clock all require.

Run:  python3 gibson.py --host 127.0.0.1
      python3 gibson.py --speed 26 --fov 130
      python3 gibson.py --no-floor --gain 1.8
"""

import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# The neon the towers are lit in. Cyan and magenta are the film's, the green is
# the terminal green everything else in this genre is written in, and the amber
# is there so that a run of four towers never repeats a colour exactly.
NEON = (
    (90, 255, 255),
    (255, 90, 220),
    (110, 255, 140),
    (255, 190, 70),
)

LANES = (-9.5, -4.4, 4.4, 9.5)      # tower centres; the gap is the corridor
SLOTS = 14                          # depth positions before the field repeats
SPACING = 8.0                       # units between depth positions


def add_arguments(ap):
    ap.add_argument("--speed", type=float, default=22.0,
                    help="units per second down the corridor")
    ap.add_argument("--fov", type=float, default=110.0,
                    help="focal length in pixels; smaller is wider and makes "
                         "the towers rush past faster at the edges")
    ap.add_argument("--far", type=float, default=62.0,
                    help="how far ahead the city is drawn; the last quarter of "
                         "it is fading up out of the fog")
    ap.add_argument("--samples", type=int, default=340,
                    help="points used for the longest class of edge. Edges are "
                         "sorted into length classes and sampled accordingly; "
                         "this sets the top one, which has to be at least the "
                         "panel's diagonal or long floor lines come out dotted")
    ap.add_argument("--floor", dest="floor", action="store_true", default=True)
    ap.add_argument("--no-floor", dest="floor", action="store_false",
                    help="drop the ground grid and fly through towers alone")
    ap.add_argument("--gain", type=float, default=1.35,
                    help="overall brightness of the lines. Above 1 because the "
                         "earlier additive version got part of its glow from "
                         "points piling up on a pixel, and writing rather than "
                         "summing gives that back only if it is asked for")
    ap.add_argument("--seed", type=int, default=7,
                    help="the city's layout: heights, widths and colours")


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed or None)

    near = 5.0
    far = max(float(args.far), near + 20.0)
    repeat = SLOTS * SPACING
    f = float(args.fov)
    cam_y = 2.2                                  # eye height above the grid
    cx = W * 0.5
    cy = H * 0.42                                # the horizon
    S = max(8, int(args.samples))

    # ------------------------------------------------------------- the city
    # One tower per (lane, depth slot), baked once. Nothing about the layout
    # depends on time; the camera moves through it and the slots wrap.
    n_lanes = len(LANES)
    n = n_lanes * SLOTS
    lane_of = np.tile(np.arange(n_lanes), SLOTS)
    slot_of = np.repeat(np.arange(SLOTS), n_lanes)

    t_x = np.array(LANES, f32)[lane_of] + rng.uniform(-1.1, 1.1, n).astype(f32)
    t_z = slot_of.astype(f32) * f32(SPACING) + rng.uniform(-1.8, 1.8, n).astype(f32)
    t_hw = rng.uniform(0.9, 1.9, n).astype(f32)          # half width
    t_hd = rng.uniform(0.9, 1.9, n).astype(f32)          # half depth
    # Heights are drawn from a squared distribution so that most of the city is
    # low and a few towers are tall. A uniform height gives a suburb.
    t_h = (2.0 + 6.0 * rng.random(n) ** 2).astype(f32)
    t_col = np.array(NEON, f32)[rng.integers(0, len(NEON), n)]

    # The eight corners, as offsets from the tower's centre column.
    #   0-3 the base, 4-7 the top, going round in the same order
    ox = np.array([-1, 1, 1, -1], f32)
    oz = np.array([-1, -1, 1, 1], f32)

    # Edge list in corner indices: four verticals, then the top square. The
    # base square is deliberately absent -- it sits in the floor grid, and
    # leaving it out is 96 fewer edges for a difference nobody can see.
    E_A = np.array([0, 1, 2, 3, 4, 5, 6, 7], np.int32)
    E_B = np.array([4, 5, 6, 7, 5, 6, 7, 4], np.int32)
    n_edge = len(E_A)

    # ------------------------------------------------------------- the floor
    if args.floor:
        grid_x = np.arange(-30.0, 30.1, 6.0, dtype=f32)
        n_long = len(grid_x)
        n_cross = 13
    else:
        n_long = n_cross = 0

    # How densely an edge is sampled has to depend on how long it is on
    # screen. One fixed count cannot serve both: a tower edge twelve pixels
    # long wants a dozen points, and a floor line running the width of the
    # panel wants three hundred or it comes out as a dotted line. So edges are
    # sorted into a few length classes and each class is sampled at its own
    # rate -- still a handful of array operations per class, not per edge.
    #
    # The classes are (longest edge in the class, points used). The last one is
    # open ended, and can be: every edge has already been clipped to the frame
    # by then, so nothing is longer than the panel's diagonal.
    #
    # How many classes is a real trade and it was measured the wrong way round
    # first. A finer ladder puts every edge closer to its own length and so
    # shrinks the point cloud -- but each class costs about fifteen array
    # operations whatever is in it, and on this Pi an operation costs 55-80
    # microseconds before it touches data. Going from four classes to six
    # halved the cloud and changed the frame time by nothing at all, because
    # the two effects cancelled exactly. Four is the setting that won.
    #
    # The sample positions are baked here rather than built per frame:
    # np.linspace was being called once per class per frame and cost 3.9 ms of
    # a 45 ms frame, which is a remarkable price for a fixed array of numbers.
    buckets = [(14.0, 16), (44.0, 48), (120.0, 128), (1e9, max(S, 340))]
    bucket_ss = [np.linspace(0.0, 1.0, count, dtype=f32)[None, :]
                 for _, count in buckets]
    # One packed RGB integer per pixel, not three planes. See splat().
    acc = np.zeros(H * W, np.uint32)
    out = np.zeros((H, W, 3), np.uint8)

    # The sway is half the field's own frequency, so the two come back into
    # phase together and the whole flight is periodic. See the docstring.
    loop = repeat / max(args.speed, 1e-6)
    sway_hz = 0.5 / loop

    def project(x, y, z):
        """World -> screen. z must already be clear of the near plane."""
        inv = f32(f) / z
        return cx + x * inv, cy - (y - f32(cam_y)) * inv

    def clip_to_frame(xa, ya, xb, yb):
        """Liang-Barsky against the panel. -> endpoints and a keep mask.

        Without this, an edge belonging to a tower that is level with the
        camera projects to something thousands of pixels long, and the sampler
        would either spend its whole budget on the fraction of it that is
        actually on screen or draw it as a dotted line. Clipping first is what
        lets the length classes above be bounded by the panel rather than by
        the geometry.
        """
        dx = xb - xa
        dy = yb - ya
        t0 = np.zeros(xa.shape, f32)
        t1 = np.ones(xa.shape, f32)
        keep = np.ones(xa.shape, bool)
        for p, q in ((-dx, xa), (dx, f32(W - 1) - xa),
                     (-dy, ya), (dy, f32(H - 1) - ya)):
            par = p == 0.0
            keep &= ~(par & (q < 0.0))               # parallel and outside
            safe = np.where(par, f32(1.0), p)
            r = q / safe
            t0 = np.where(~par & (p < 0.0), np.maximum(t0, r), t0)
            t1 = np.where(~par & (p > 0.0), np.minimum(t1, r), t1)
        keep &= t0 <= t1
        return (xa + dx * t0, ya + dy * t0,
                xa + dx * t1, ya + dy * t1, keep)

    def splat(xa, ya, xb, yb, weight, colour):
        """Accumulate a batch of lines. weight is per edge, colour per edge.

        Returns False if nothing was drawn, so the caller can blank the frame
        rather than leave the previous one on the wall.
        """
        if xa.size == 0:
            return False
        # Faintness first, before anything expensive touches these. The edges
        # fading out at the near plane are also by far the *longest* on screen
        # -- a tower level with the camera projects to something the width of
        # the panel -- so culling on weight is worth more than it looks: it is
        # the difference between a 137 ms frame and an average one. Measured on
        # the Pi, this alone took the worst frame down by more than half.
        alive = weight > f32(0.006)
        if not alive.any():
            return False
        xa, ya, xb, yb = xa[alive], ya[alive], xb[alive], yb[alive]
        weight, colour = weight[alive], colour[alive]

        xa, ya, xb, yb, keep = clip_to_frame(xa, ya, xb, yb)
        if not keep.any():
            return False
        xa, ya, xb, yb = xa[keep], ya[keep], xb[keep], yb[keep]
        weight, colour = weight[keep], colour[keep]
        length = np.maximum(np.abs(xb - xa), np.abs(yb - ya))

        # Each class produces a cloud of points; they are concatenated and
        # written *once* rather than per class.
        #
        # The write is a plain indexed assignment, and that is the single
        # biggest decision in this file. The natural thing is np.bincount,
        # which sums where points coincide and so makes crossing lines glow --
        # and on a desktop it is free. On the Pi driving this wall it is not:
        # measured there, three bincounts into a 20480-bin buffer cost 8.5 to
        # 10 ms *no matter how few points go in*, because the cost is the
        # output buffer and the float64 it insists on, not the cloud. Three
        # indexed assignments over the same cloud cost 1 to 2 ms. Nothing else
        # in this demo was ever going to buy back a fixed 9 ms.
        #
        # What is given up is the summing: where two lines cross, the pixel is
        # now whichever was written last rather than the sum of both. Points
        # are laid down shortest class first, so a long near edge overwrites a
        # short far one, which is the right way round anyway.
        # The colour a pixel of this edge will be, worked out once per *edge*
        # and packed into a single integer. Doing it here costs a handful of
        # operations over a few hundred edges; carrying three separate channel
        # arrays through the sampler instead costs three passes over a cloud of
        # tens of thousands of points, three concatenations and three indexed
        # writes. One packed integer per point makes all of that singular.
        #
        # No division by the sample count: writing, unlike summing, puts the
        # value on the pixel once however many points landed there. Scaling by
        # length/count is right for np.bincount and halves the brightness here,
        # which is exactly what it did before this was noticed.
        lit = np.clip(weight[:, None] * colour, 0.0, 255.0).astype(np.uint32)
        packed = (lit[:, 0] << 16) | (lit[:, 1] << 8) | lit[:, 2]

        dx = xb - xa
        dy = yb - ya
        flats, packs = [], []
        lo = 0.0
        for (hi, count), ss in zip(buckets, bucket_ss):
            sel = (length > lo) & (length <= hi) if hi < 1e8 else (length > lo)
            lo = hi
            if not sel.any():
                continue
            xs = xa[sel][:, None] + dx[sel][:, None] * ss
            ys = ya[sel][:, None] + dy[sel][:, None] * ss
            # No clip on these: every endpoint came out of clip_to_frame, so
            # the samples are already inside the panel. The one guard that is
            # kept is on the flat index below, which is a single operation over
            # the whole cloud rather than two per class.
            flats.append((ys.astype(np.int32) * W
                          + xs.astype(np.int32)).reshape(-1))
            packs.append(np.broadcast_to(packed[sel][:, None],
                                         xs.shape).reshape(-1))
        if not flats:
            return False
        flat = np.clip(np.concatenate(flats), 0, H * W - 1)
        acc[:] = 0
        acc[flat] = np.concatenate(packs)
        return True

    def render(t, frame):
        cam_z = float(args.speed) * t
        sway = float(np.sin(2.0 * np.pi * sway_hz * t)) * 1.6
        edges = []                       # (xa, ya, xb, yb, weight, colour)

        # --------------------------------------------------------- the towers
        # Wrap each slot into the window ahead of the camera. Doing it with a
        # modulo rather than by moving the towers is what makes the city
        # endless without any bookkeeping between frames.
        z_rel = (t_z - cam_z) % repeat
        live = (z_rel > near) & (z_rel < far)
        if live.any():
            zc = z_rel[live]
            xc = t_x[live] - sway
            hw = t_hw[live]
            hd = t_hd[live]
            hh = t_h[live]
            col = t_col[live]

            # corners: (m, 8)
            bx = xc[:, None] + ox[None, :] * hw[:, None]
            bz = zc[:, None] + oz[None, :] * hd[:, None]
            cxs = np.concatenate([bx, bx], axis=1)
            czs = np.concatenate([bz, bz], axis=1)
            cys = np.concatenate([np.zeros_like(bx),
                                  np.broadcast_to(hh[:, None], bx.shape)], axis=1)
            # A corner that has slipped behind the near plane would project to
            # infinity, so it is held at the plane; the whole tower is fading
            # out by then anyway.
            czs = np.maximum(czs, near * 0.6)
            px, py = project(cxs, cys, czs)

            # Fog at the far end, and a fade over the last few units before the
            # camera so nothing ever pops.
            fade_far = np.clip((far - zc) / (far * 0.32), 0.0, 1.0)
            fade_near = np.clip((zc - near) / 7.0, 0.0, 1.0)
            bright = (fade_far * fade_near / (0.35 + zc * 0.055)).astype(f32)

            edges.append((px[:, E_A].reshape(-1), py[:, E_A].reshape(-1),
                          px[:, E_B].reshape(-1), py[:, E_B].reshape(-1),
                          np.repeat(bright, n_edge) * f32(args.gain),
                          np.repeat(col, n_edge, axis=0)))

        # ---------------------------------------------------------- the floor
        if args.floor:
            grid_col = np.array((40, 130, 150), f32)
            # Lines running away from the camera, and cross ties sliding
            # towards it. The ties are placed relative to the camera so they
            # appear to move rather than being fixed in the world and popping.
            zz = np.array([near, far], f32)
            ax, ay = project(grid_x - sway, np.zeros(n_long, f32),
                             np.full(n_long, zz[0], f32))
            bx2, by2 = project(grid_x - sway, np.zeros(n_long, f32),
                               np.full(n_long, zz[1], f32))
            edges.append((ax, ay, bx2, by2, np.full(n_long, 0.55, f32),
                          np.broadcast_to(grid_col, (n_long, 3))))

            k = np.arange(1, n_cross + 1, dtype=f32)
            zt = k * (far - near) / (n_cross + 1) + near
            zt = zt - (cam_z % ((far - near) / (n_cross + 1)))
            zt = np.maximum(zt, near)
            e = np.full(n_cross, 30.0, f32)
            ax, ay = project(-e - sway, np.zeros(n_cross, f32), zt)
            bx2, by2 = project(e - sway, np.zeros(n_cross, f32), zt)
            edges.append((ax, ay, bx2, by2,
                          (np.clip((far - zt) / (far * 0.5), 0.0, 1.0)
                           * 0.5).astype(f32),
                          np.broadcast_to(grid_col, (n_cross, 3))))

        # Towers and floor go through the sampler together, as one batch. They
        # are different objects but they are the same *work*, and splitting
        # them would triple the per-frame call count for nothing.
        drew = edges and splat(
            *[np.concatenate([e[i] for e in edges]) for i in range(6)])
        if not drew:
            out[:] = 0
            return out

        # ------------------------------------------------------------- output
        # Unpack the three channels back out. Already clamped to 0..255 when
        # they were packed, so there is no clipping and no palette lookup here.
        plane = acc.reshape(H, W)
        np.copyto(out[:, :, 0], plane >> 16, casting="unsafe")
        np.copyto(out[:, :, 1], (plane >> 8) & 0xFF, casting="unsafe")
        np.copyto(out[:, :, 2], plane & 0xFF, casting="unsafe")
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
