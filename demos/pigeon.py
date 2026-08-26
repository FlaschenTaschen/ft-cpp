#!/usr/bin/env python3
"""Pigeons.

A stretch of Potrero Hill pavement with the local rock doves on it. They walk,
they peck, they look suspicious about it, one of them struts at another who
could not care less, two of them squabble over a burrito end, and every so
often the whole flock goes up at once and clatters off the top of the panel --
leaving one pale scruffy bird who does not fly, because he never does, standing
there pecking at the pavement until everybody lands again.

The entire demo is the walk. A walking pigeon does not bob its head for
decoration: it holds its head *still in space* while the body walks forward
underneath it, then snaps the head forward to a new fixed point and holds it
there again. That is how a bird with immobile eyes stabilises an image. Almost
every animated pigeon gets this backwards and swings the head like a pendulum,
which is why almost every animated pigeon looks like a chicken toy.

Getting it right is one line of arithmetic, and it falls out of drawing the
head as a separate sprite from the body:

    body_x(t) = x0 + v*t                                (smooth)
    head_x(t) = x0 + v*P*(k + f(p))                     (stepped)
    head_rel  = head_x - body_x = STEP * (f(p) - p)

where the step index k and phase p come from t/P, and f(p) is zero for the
first 78% of the step and then ramps to one. Relative to the body, the head
slides *backwards* through the step and snaps forward at the end; in the world
it is nailed to a point. STEP is v*P, about four pixels, so the head moves
three pixels back and one frame forward, which at 20 fps is exactly the twitch
you see on a real pavement.

Everything else is a timeline. render() has to be a pure function of t -- the
scheduler builds segments on a worker thread and starts them at t=0 -- so none
of the behaviour here is a state machine ticking over. build() draws each
bird's whole performance for the cycle from --seed: a contiguous list of
segments (idle / walk / strut / squabble / fly) and, inside each idle segment,
a list of head beats (stab, hold, look back, look up). render() bisects into
that script and interpolates. Purity comes for free, and more usefully the
*timing* becomes something you compose rather than something you hope for: the
startles are placed where they land best, the squabble is put next to the
thing being squabbled over, and every bird's script is generated so that it
ends the cycle standing exactly where it started, which is what makes the loop
close invisibly.

Drawing: birds are dark on a light pavement, which is the way round it really
is and the way round that survives being seen at an angle from three metres. A
1px dark underside line and a baked shadow keep the silhouette off the
concrete. The only colour on a pigeon is the iridescent neck, and it does a lot
of work here: it flips green to purple as the head turns, because that is
exactly what iridescence does when the angle changes.

Cost is sprites and blits: one background copy plus three small blits per bird,
so --birds is the knob. Nothing is computed per pixel.

Run:  python3 pigeon.py --host 127.0.0.1
      python3 pigeon.py --birds 12 --seed 11
      python3 pigeon.py --cycle 40 --speed 13
"""

import bisect
import sys

import numpy as np

import demoscene as ds

f32 = np.float32


# --------------------------------------------------------------------------
# The art, as data. '.' is transparent; every other character indexes a
# palette. Same convention as mario.py, nyancat.py and fine.py -- drawn here
# from a description, never traced or downloaded.
#
# The body and the head are *separate* sprites and that is the whole design.
# A pigeon's head and body move independently, so they have to be independent
# sprites; fusing them into one grid would make the head bob impossible to
# animate correctly no matter how many frames you drew.
# --------------------------------------------------------------------------

BASE_PALETTE = {
    "B": (132, 136, 148),     # back and mantle, catching the sky
    "b": (98, 102, 116),      # body, side
    "W": (76, 80, 92),        # folded wing coverts
    "w": (40, 42, 52),        # the two dark wing bars -- a pigeon's one marking
    "s": (68, 70, 82),        # belly, in its own shadow
    "o": (22, 22, 28),        # underside line; what lifts it off the concrete
    "t": (86, 90, 102),       # tail
    "T": (38, 40, 50),        # tail band

    "H": (112, 116, 130),     # head, lit side
    "h": (78, 82, 96),        # head, shaded
    "k": (34, 34, 40),        # beak
    "c": (226, 224, 230),     # cere, the white waxy saddle over the beak
    "E": (236, 138, 44),      # iris -- orange, and the second colour accent
    "e": (26, 22, 22),        # pupil

    "g": (44, 186, 122),      # neck, iridescent green
    "p": (168, 88, 202),      # neck, iridescent purple

    "l": (192, 96, 94),       # leg
    "f": (166, 74, 74),       # foot
}

# The plumage morphs you actually see on a San Francisco pavement. Each is a
# handful of overrides on the base palette rather than a whole second bird.
MORPHS = {
    # Blue bar: the wild type, the default pigeon.
    "bar": {},
    # Checker: darker, the wing coverts mottled rather than barred.
    "checker": {"B": (104, 108, 120), "b": (78, 82, 94), "W": (56, 58, 70),
                "s": (54, 56, 66), "H": (88, 92, 104), "h": (62, 64, 76)},
    # Red bar: the warm brown one, surprisingly common here.
    "red": {"B": (150, 118, 100), "b": (118, 88, 74), "W": (96, 68, 56),
            "w": (58, 36, 28), "s": (86, 62, 52), "t": (104, 78, 64),
            "H": (128, 100, 84), "h": (94, 70, 58)},
    # Pied: the pale scruffy one. This is the individual -- see PIED below.
    "pied": {"B": (228, 226, 230), "b": (198, 196, 204), "W": (126, 128, 138),
             "w": (66, 68, 80), "s": (182, 180, 190), "t": (204, 202, 210),
             "T": (96, 98, 110), "H": (222, 220, 226), "h": (184, 184, 194)},
}

# The body, facing right: tail at the left, breast at the right. Eighteen
# columns is the smallest a pigeon can be and still have a distinguishable
# breast, wing bar and tail; below that it is a pear.
BODY_TOP = (
    "..........BBBBBB..",
    "........BBBBBBBBB.",
    "......BBBBBBBBBBBb",
    "tttTWWWWWWBBBBBbbb",
    "ttTTWWWWWWWWbbbbbb",
    "...TWWWWWWWWbbbbb.",
    "....wwwwwwwssbbb..",
    ".....oooooooooo...",
)

# Three leg pairs, pasted under the body. A pigeon's stride is short and its
# legs are mostly hidden, so the gait reads from the *scissor* -- feet apart,
# feet together, feet crossing -- and from the one pixel the body rises on the
# passing beat, not from any detail in the leg itself.
LEGS = (
    ("........l...l.....", "........ff..ff...."),   # planted
    (".......l.....l....", ".......ff....ff..."),   # stride
    ("........l..l......", "........ff.ff....."),   # passing
)
LEG_LIFT = (0, 0, 1)          # body rises a pixel on the passing beat

# The strut: chest inflated, neck sunk into it, tail dropped and dragging on
# the pavement. Twenty columns because the whole point of the display is that
# he is bigger than he was a second ago.
BODY_STRUT = (
    "..........BBBBBB....",
    "........BBBBBBBBBB..",
    "......BBBBBBBBBBBBb.",
    "tttTWWWWWWBBBBBBbbbb",
    "ttTTWWWWWWWWBBbbbbbb",
    ".ttTWWWWWWWWbbbbbbb.",
    "..ttwwwwwwwssbbbbb..",
    "...ttooooooooooo....",
    "........l...l.......",
    "........ff..ff......",
)

# Flight, two frames. The body sits on the *same* rows in both so the flap is
# wings moving and not the whole bird jumping up and down -- which is the one
# way a two-frame flap can look wrong. Wings up is a V over the back, wings
# down is the same V mirrored below it, and at 8 Hz that is the clatter.
FLY_BODY = (
    "...tttTBBBBBBBBBBHHH......",
    "...ttTTbbbbbbbbbbHHEekk...",
    ".....Tossssssssoohh.......",
    "........ll...ll...........",
    "........f....f............",
)
# Wings thrown open over the back, for the squabble: the same V, but sized
# to sit over a *standing* body rather than a flying one.
SQUAB_WINGS = (
    "..WWW..............WWW..",
    "..WWWW............WWWW..",
    "...WWWW..........WWWW...",
    "....WWWWW......WWWWW....",
    "......WWWWWWWWWWWW......",
    "........WWWWWWWW........",
)
WING_V = (
    "...WW................WW...",
    "...WWW..............WWW...",
    "....WWW............WWW....",
    ".....WWWW........WWWW.....",
    "......WWWWWWWWWWWWWW......",
)

# Heads. Seven rows, seven columns, beak to the right. The bottom three rows
# are the neck and they sit *on* the body, which is why the iridescence lands
# where a pigeon's iridescence actually is -- and why the head can be lifted
# clear of the shoulders without the bird coming apart.
HEAD_FWD = (
    "..HHHH.",
    ".HHHHHH",
    ".HHEeck",
    ".hhhhkk",
    ".hhhh..",
    ".gpgp..",
    ".pgpg..",
)
# Head up: neck stretched, beak level, the pose a pigeon holds for about a
# second after anything at all happens.
HEAD_UP = (
    "..HHHH.",
    ".HHHHHk",
    ".HHEekk",
    "..hhhh.",
    ".hhhh..",
    ".gpgp..",
    ".pgpg..",
)
# Head down: the stab. No neck rows -- at full extension the neck is behind
# the head, not under it, and the head is eight rows lower anyway.
HEAD_DOWN = (
    ".HHHH..",
    "HHHHHH.",
    "HHEeHH.",
    ".hhhcc.",
    "...kk..",
    "....k..",
    ".......",
)

# The far birds, up by the kerb. Eight pixels of pigeon: enough for a back, a
# tail and a beak, which at that distance is all there is.
FAR_A = (
    "..BBB...",
    ".BBBBBh.",
    "tTbbbHk.",
    "..oooo..",
    "...l.l..",
)
FAR_B = (
    "...BBB..",
    "..BBBBh.",
    "tTbbbHk.",
    "..oooo..",
    "...ll...",
)
FAR_FLY = (
    "W......W",
    ".W....W.",
    "..WBBW..",
    "...bbk..",
    "........",
)

# The gag, and it is deliberately small: a foil-wrapped burrito end on the
# pavement. It is what the squabble is over, it is two thirds the length of a
# pigeon, and if you do not notice it the squabble is still funny.
BURRITO = (
    "..FFFFFF..",
    ".FFFNNNFF.",
    "FFNNNNNFFF",
    ".FFFFFFFF.",
)
BURRITO_PALETTE = {"F": (196, 200, 206), "N": (206, 176, 120)}

# A moulted feather, drifting down after a startle.
FEATHER_COL = (182, 184, 192)


def rasterize(grid, palette):
    """(rows of chars) -> ((h,w,3) float32, (h,w) bool). Ragged rows raise."""
    h, w = len(grid), len(grid[0])
    for row in grid:
        if len(row) != w:
            raise ValueError("ragged sprite row %r: %d wide, expected %d"
                             % (row, len(row), w))
    rgb = np.zeros((h, w, 3), f32)
    mask = np.zeros((h, w), bool)
    for r in range(h):
        for c in range(w):
            ch = grid[r][c]
            if ch != ".":
                rgb[r, c] = palette[ch]
                mask[r, c] = True
    return rgb, mask


def paste(rows, art, y=0, x=0):
    """Stamp character art over a list-of-lists grid, ignoring '.'."""
    for r, line in enumerate(art):
        for c, ch in enumerate(line):
            if ch != ".":
                rows[y + r][x + c] = ch
    return rows


def body_grid(leg_pose, pied):
    """The standing/walking body, with one of the three leg pairs under it."""
    rows = [list(r) for r in BODY_TOP] + [list(LEGS[leg_pose][0]),
                                          list(LEGS[leg_pose][1])]
    if pied:
        # Missing a toe. Half a pixel of characterisation, on the bird that
        # also will not fly: he has been here longer than the rest of them.
        for c, ch in enumerate(rows[-1]):
            if ch == "f":
                rows[-1][c] = "."
                break
    return ["".join(r) for r in rows]


def strut_grid():
    return list(BODY_STRUT)


def fly_grid(up):
    """Wings above the body, or the same V mirrored below it."""
    blank = "." * 26
    wings = list(WING_V)
    if up:
        rows = wings + list(FLY_BODY) + [blank] * 5
    else:
        rows = [blank] * 5 + list(FLY_BODY) + wings[::-1]
    return rows


def squab_grid():
    """Standing, with the wings thrown open over the back.

    Not the flight sprite: a flying pigeon's body is short and streamlined and
    sits five rows lower than a standing one, so reusing it for a bird that is
    still on the pavement drops the body through its own legs and draws a
    wing through the concrete. It looked like a trestle table.
    """
    pad = "..."
    return list(SQUAB_WINGS) + [pad + r + pad for r in BODY_TOP] \
        + [pad + r + pad for r in LEGS[0]]


# Where the head's top-left corner sits in body coordinates, for a bird facing
# right: (width of the body sprite, head dy, head dx). Every pose puts the
# bird's feet on its own row 9, so a blit is always feet_y - 9 and the poses
# are interchangeable. The head shuffles a pixel towards the tail when it is
# looking back over its shoulder.
POSE_GEOMETRY = {
    #  pose      width  foot_row  head_dy  head_dx
    "walk0":    (18, 9, -4, 11),
    "walk1":    (18, 9, -4, 11),
    "walk2":    (18, 9, -4, 11),
    "strut":    (20, 9, -3, 11),
    "squab":    (24, 15, 2, 14),
}
HEAD_W = 7
HEAD_H = 7
# The flight sprite's body is deliberately five rows higher than a standing
# bird's feet, because a pigeon in the air is not standing on anything: 13 is
# what puts the flying body where the walking body was at the instant of
# take-off, so the launch is a leap rather than a jump-cut.
FLY_FOOT = 13


# --------------------------------------------------------------------------
# The pavement. Painted once at build time into a float image, then dithered.
# A second copy at 55% is baked alongside it, and *that* is what the bird
# shadows are blitted from: a shadow is then one masked copy from an image
# that already has the right concrete texture under it, rather than a
# multiply on a uint8 slice (which cannot be done in place) or a float round
# trip per bird per frame.
# --------------------------------------------------------------------------

def paint_scene(W, H, rng, geo):
    img = np.zeros((H, W, 3), f32)
    wall_y = geo["wall_y"]
    plinth_y = geo["plinth_y"]
    pave_y = geo["pave_y"]

    def rect(y0, y1, x0, x1, col):
        y0, y1 = max(0, int(y0)), min(H, int(y1))
        x0, x1 = max(0, int(x0)), min(W, int(x1))
        if y1 > y0 and x1 > x0:
            img[y0:y1, x0:x1] = col

    # --- the building behind them ---------------------------------------
    # Deliberately dim and low contrast. It exists so the back birds have
    # something to be a silhouette against, and for no other reason; anything
    # brighter up here competes with the birds, and the birds are the panel.
    rect(0, wall_y, 0, W, (44, 41, 44))
    rect(0, 3, 0, W, (30, 28, 31))
    # Vertical siding. Potrero Hill is board and batten most of the way up.
    for x in range(0, W, 13):
        rect(3, wall_y, x, x + 1, (52, 48, 51))
    # A roll-up garage door, ribbed, and a shop doorway with a red door in it.
    gx0, gx1 = int(W * 0.17), int(W * 0.40)
    rect(2, wall_y, gx0, gx1, (58, 54, 55))
    for y in range(4, wall_y, 4):
        rect(y, y + 1, gx0 + 1, gx1 - 1, (44, 41, 43))
    rect(2, wall_y, gx0, gx0 + 1, (34, 32, 34))
    rect(2, wall_y, gx1 - 1, gx1, (34, 32, 34))
    dx0, dx1 = int(W * 0.71), int(W * 0.80)
    rect(4, wall_y, dx0 - 2, dx1 + 2, (36, 33, 35))
    rect(6, wall_y, dx0, dx1, (108, 44, 46))
    rect(6, 7, dx0, dx1, (140, 62, 62))

    # --- the plinth and the kerb line ------------------------------------
    rect(wall_y, plinth_y, 0, W, (92, 90, 86))
    rect(wall_y, wall_y + 1, 0, W, (132, 130, 124))
    rect(plinth_y, pave_y, 0, W, (46, 45, 44))

    # --- the pavement -----------------------------------------------------
    rect(pave_y, H, 0, W, (124, 122, 116))
    ys = np.arange(H, dtype=f32)[:, None]
    # Slab courses, spaced wider as they come towards you. Three joints is
    # enough to say "concrete slabs" and few enough not to fight the birds.
    joints = geo["joints"]
    for i, jy in enumerate(joints):
        rect(jy, jy + 1, 0, W, (94, 92, 88))
        rect(jy + 1, jy + 2, 0, W, (140, 138, 132))
        # The cross joints step sideways from course to course, which is all
        # the perspective 36 rows of pavement can carry.
        step = 74
        for x in range(-40 + 9 * i, W, step):
            y1 = joints[i + 1] if i + 1 < len(joints) else H
            xx = x + int((jy - pave_y) * 0.35)
            rect(jy, y1, xx, xx + 1, (100, 98, 94))

    # Aggregate speckle, and the gum. San Francisco pavement is mostly gum.
    spx = rng.integers(0, W, 900)
    spy = rng.integers(pave_y, H, 900)
    img[spy, spx] += rng.uniform(-9.0, 9.0, 900)[:, None].astype(f32)
    gx = rng.integers(4, W - 4, 34)
    gy = rng.integers(pave_y + 2, H - 2, 34)
    img[gy, gx] = (66, 62, 60)
    img[gy, gx + 1] = (74, 70, 68)

    # A utility plate, because every stretch of pavement has one.
    px, py = geo["plate"]
    rect(py, py + 11, px, px + 26, (104, 100, 94))
    rect(py, py + 1, px, px + 26, (138, 134, 128))
    for y in range(py + 2, py + 10, 3):
        rect(y, y + 1, px + 2, px + 24, (84, 80, 76))

    # The burrito end, baked into the ground: it never moves, so it is part of
    # the pavement rather than something render() has to think about.
    bur_rgb, bur_mask = rasterize(BURRITO, BURRITO_PALETTE)
    by, bx = geo["burrito"]
    sub = img[by:by + bur_mask.shape[0], bx:bx + bur_mask.shape[1]]
    np.copyto(sub, bur_rgb, where=bur_mask[:, :, None])

    # Light falls off towards the back of the pavement -- shopfront shade.
    # This is what stops the pavement reading as a flat grey rectangle.
    fall = np.clip((ys - pave_y) / float(max(1, H - pave_y)), 0.0, 1.0)
    img[pave_y:] *= (0.80 + 0.28 * fall[pave_y:])[:, :, None]
    return np.clip(img, 0, 255)


# --------------------------------------------------------------------------
# The script. Every bird's whole performance for one cycle is drawn here, once,
# from --seed. A segment is (t0, t1, kind, payload); render() bisects.
# --------------------------------------------------------------------------

IDLE, WALK, STRUT, SQUAB, FLY = range(5)

# Head beats inside an idle segment.
BEAT_STILL, BEAT_PECK, BEAT_BACK, BEAT_UP = range(4)

PECK_BEAT = 0.72                  # one stab plus its pause
STEP_PERIOD = 0.40                # seconds per stride
HEAD_HOLD = 0.78                  # fraction of the stride the head is fixed

# The strut is a circle and the squabble is a pair of lunges, and both are
# oscillations *about* the position the script recorded. Their durations are
# therefore snapped to whole cycles, so each ends exactly where it began: an
# oscillation cut off mid-swing leaves the bird up to five pixels from where
# the next segment expects it, and it snaps back on the boundary.
STRUT_PERIOD = 3.4                # seconds for one turn of the display
STRUT_R = 5.0                     # how far he drags himself round it
STRUT_TURNS = 2
SQUAB_RATE = 2.6                  # lunges per second
SQUAB_LUNGES = 7


def whole_cycles(nominal, period):
    """The multiple of `period` closest to `nominal`, at least one."""
    return max(1.0, round(nominal / period)) * period


def make_beats(rng, dur, pecky):
    """A head performance for one idle segment.

    `pecky` biases towards stabbing at the ground rather than standing around
    looking suspicious. Beats are contiguous and cover [0, dur); render()
    bisects into the start times.
    """
    beats = []
    t = 0.0
    while t < dur - 0.05:
        r = rng.random()
        if r < pecky:
            beats.append((t, BEAT_PECK))
            t += PECK_BEAT
        elif r < pecky + (1.0 - pecky) * 0.45:
            # The suspicious look: head snapped round to look behind, held for
            # about a second. This is the beat that makes a standing pigeon
            # look like it is thinking rather than paused.
            beats.append((t, BEAT_BACK))
            t += float(rng.uniform(0.7, 1.5))
        elif r < pecky + (1.0 - pecky) * 0.7:
            beats.append((t, BEAT_UP))
            t += float(rng.uniform(0.5, 1.2))
        else:
            beats.append((t, BEAT_STILL))
            t += float(rng.uniform(0.6, 1.6))
    if not beats:
        beats = [(0.0, BEAT_STILL)]
    return [b[0] for b in beats], [b[1] for b in beats]


class Script(object):
    """A growing, contiguous list of segments for one bird.

    Contiguous is the important word: segment i ends exactly where segment
    i+1 begins, so render() can find the current segment with one bisect and
    never has to ask what happens in a gap.
    """

    def __init__(self, x0, face=1, t0=0.0):
        self.t = t0
        self.x = x0
        self.face = face
        self.segs = []

    def add(self, dur, kind, payload):
        dur = max(0.05, float(dur))
        self.segs.append((self.t, self.t + dur, kind, payload))
        self.t += dur

    def idle(self, rng, dur, pecky=0.55):
        # A standing bird faces the way it last walked. Re-rolling it would
        # make the flock twitch on segment boundaries for no reason.
        self.add(dur, IDLE, (self.x, self.face, make_beats(rng, dur, pecky)))

    def walk(self, dur, x1):
        self.add(dur, WALK, (self.x, x1, dur))
        self.face = 1 if x1 >= self.x else -1
        self.x = x1

    def finish(self, t_end):
        """Stretch (or trim) the last segment so it ends exactly at t_end.

        Trimming a *walk* has to shorten the walk itself, not just its slot:
        leaving the destination in the payload while cutting the segment
        short leaves the bird a few pixels behind where the next segment was
        written against, and it teleports on the boundary. That bug put a
        strutting pigeon fourteen pixels from where he had just walked to.
        """
        if not self.segs or t_end <= self.segs[-1][0] + 0.05:
            self.t = max(self.t, t_end)
            return
        t0, t1, kind, payload = self.segs[-1]
        if kind == WALK and t_end < t1:
            x0, x1, dur = payload
            nx = x0 + (x1 - x0) * ((t_end - t0) / dur)
            self.segs[-1] = (t0, t_end, WALK, (x0, nx, t_end - t0))
            self.x = nx
        else:
            self.segs[-1] = (t0, t_end, kind, payload)
        self.t = t_end

    def approach(self, rng, at, target, speed, lo, hi):
        """Idle and wander until it is time to walk over to `target`.

        The distance has to be re-measured *after* the filling, because the
        filling moves the bird. Computing it once up front and reusing it is
        how the strut ended up starting from the wrong place.
        """
        # Reserve the walk, plus enough slack for however far the filling is
        # allowed to wander in the meantime -- which is why fill() takes a
        # cap on its walk length at all.
        slack = 26.0 / speed
        fill(self, rng, at - abs(target - self.x) / speed - slack,
             speed, lo, hi, max_dx=24.0)
        avail = at - self.t
        d = abs(target - self.x) / speed
        if avail > 0.2:
            if d <= avail:
                self.walk(max(d, 0.05), target)
                if at - self.t > 0.25:
                    self.idle(rng, at - self.t, 0.5)
            else:
                # Not enough time to get all the way there; get as far as it
                # can and be late rather than teleport.
                self.walk(avail, self.x + (target - self.x) * (avail / d))
        self.finish(at)


def fill(sc, rng, t_end, speed, lo, hi, pecky=0.55, max_dx=52.0):
    """Fill a bird's timeline up to t_end with walks and idling.

    Nothing here is allowed to overrun t_end, because t_end is somebody's
    appointment -- a startle, or the squabble, and both need every bird to
    arrive on the beat.
    """
    while t_end - sc.t > 0.9:
        left = t_end - sc.t
        if rng.random() < 0.42 and left > 1.6:
            # A walk of a plausible length, kept inside the bird's home range
            # and inside whatever time is left before the next appointment.
            span = min(max_dx, left * speed)
            dx = float(rng.uniform(12.0, max(13.0, span)))
            if rng.random() < 0.5:
                dx = -dx
            x1 = float(np.clip(sc.x + dx, lo, hi))
            if abs(x1 - sc.x) < 6.0:
                x1 = float(np.clip(sc.x - dx, lo, hi))
            dur = abs(x1 - sc.x) / speed
            if 0.4 < dur <= left:
                sc.walk(dur, x1)
                continue
        sc.idle(rng, min(left, float(rng.uniform(1.4, 3.6))), pecky)


def return_home(sc, rng, t_end, speed, home):
    """Close the loop: walk back to where the bird started, then stand.

    The cycle has to end with every bird exactly where it began or the loop
    seam is a flock teleporting. Reserving the tail of the cycle for one
    return walk is cheaper than trying to make a random walk sum to zero, and
    it looks like nothing at all: a bird walking back the way it came.
    """
    avail = max(0.1, t_end - sc.t)
    dur = abs(home - sc.x) / speed
    if dur <= 1e-9:
        sc.idle(rng, avail, 0.6)
    elif dur >= avail - 0.25:
        # The reserve is normally ample, but a late appointment can eat into
        # it. Closing the loop is not negotiable, so the fallback is to spend
        # the entire remaining window walking home, however brisk that makes
        # it: a bird half a second quick is invisible, a flock that jumps at
        # the loop seam is not.
        sc.walk(avail, home)
    else:
        # Even a fraction of a pixel is walked rather than snapped, so the
        # position at t=cycle is exactly the position at t=0 and the test can
        # assert equality instead of a tolerance.
        sc.walk(max(dur, 0.1), home)
        sc.idle(rng, t_end - sc.t, 0.6)
    sc.finish(t_end)


# --------------------------------------------------------------------------

def add_arguments(ap):
    ap.add_argument("--birds", type=int, default=8,
                    help="pigeons in the foreground; this is the cost knob")
    ap.add_argument("--far", type=int, default=4,
                    help="little ones up by the kerb")
    ap.add_argument("--cycle", type=float, default=62.0,
                    help="seconds before the whole scene repeats")
    ap.add_argument("--speed", type=float, default=10.0,
                    help="walking speed, pixels/sec")
    ap.add_argument("--startles", type=int, default=2,
                    help="times per cycle the flock goes up")
    ap.add_argument("--seed", type=int, default=5)


def build(args):
    W, H = args.width, args.height
    rng = np.random.default_rng(args.seed)
    cycle = max(18.0, float(args.cycle))
    speed = max(2.0, float(args.speed))

    # --- geometry ---------------------------------------------------------
    wall_y = int(round(H * 0.33))
    plinth_y = wall_y + 4
    pave_y = plinth_y + 2
    geo = {
        "wall_y": wall_y, "plinth_y": plinth_y, "pave_y": pave_y,
        "joints": [pave_y + 6, pave_y + 15, pave_y + 27],
        "plate": (int(W * 0.845), pave_y + 17),
        "burrito": (H - 8, int(W * 0.47)),
    }
    if H - pave_y < 26 or W < 120:
        raise ValueError("pigeon needs at least 120x%d; got %dx%d"
                         % (26 + pave_y, W, H))

    scene = paint_scene(W, H, rng, geo)
    bg = ds.dither(scene)
    bg_shadow = ds.dither(scene * 0.56)

    # --- the shadow stamp -------------------------------------------------
    # One ellipse, baked. Blitted from bg_shadow so it picks up the real
    # concrete underneath instead of being a flat grey smear.
    sh_h, sh_w = 4, 15
    yy = (np.arange(sh_h, dtype=f32)[:, None] - (sh_h - 1) * 0.5) / (sh_h * 0.5)
    xx = (np.arange(sh_w, dtype=f32)[None, :] - (sh_w - 1) * 0.5) / (sh_w * 0.5)
    shadow_mask = ((yy * yy + xx * xx) < 1.0)[:, :, None]

    # --- the birds --------------------------------------------------------
    n = max(1, int(args.birds))
    # Feet rows across a shallow band. Same-size sprites means this cannot be
    # a deep scene -- a bird twice as far away would have to be half the size
    # -- so the foreground is a frieze about twenty rows deep and the depth
    # cue is the little birds up at the kerb instead. Feet increase with i,
    # which makes the list itself the painter's order.
    foot_lo, foot_hi = pave_y + 15, H - 3
    feet = [int(round(foot_lo + (foot_hi - foot_lo) * (i + 0.5) / n))
            for i in range(n)]

    # The pied one is the individual: pale, missing a toe, and he does not
    # fly. He is placed in the middle of the panel so that when everything
    # else leaves, what is left in the middle of the frame is him.
    pied_i = n // 2

    morph_names = ["bar", "checker", "bar", "red", "checker", "bar"]

    birds = []
    for i in range(n):
        fy = feet[i]
        pied = (i == pied_i)
        morph = "pied" if pied else morph_names[i % len(morph_names)]
        pal = dict(BASE_PALETTE)
        pal.update(MORPHS[morph])
        # Aerial perspective: the back of the frieze is very slightly hazed
        # towards the pavement it is standing on. Small -- two rows of birds
        # cannot carry more than a hint -- but it stops the back row from
        # looking pasted on top of the front row.
        depth = 1.0 - (fy - foot_lo) / float(max(1, foot_hi - foot_lo))
        haze = 0.20 * depth
        tone = 1.0 - 0.10 * depth
        hz = np.array((118.0, 116.0, 112.0), f32) * haze
        pal = dict((k, tuple(np.array(v, f32) * tone * (1.0 - haze) + hz))
                   for k, v in pal.items())

        art = {}

        def add_pose(name, grid, facing):
            rgb, mask = rasterize(grid, pal)
            if facing < 0:
                rgb, mask = rgb[:, ::-1], mask[:, ::-1]
            art[(name, facing)] = (np.ascontiguousarray(
                np.clip(rgb, 0, 255).astype(np.uint8)),
                np.ascontiguousarray(mask)[:, :, None])

        for fa in (1, -1):
            for lp in range(3):
                add_pose("walk%d" % lp, body_grid(lp, pied), fa)
            add_pose("strut", strut_grid(), fa)
            add_pose("squab", squab_grid(), fa)
            add_pose("flyup", fly_grid(True), fa)
            add_pose("flydn", fly_grid(False), fa)
            # The iridescence flips green to purple as the head turns, which
            # is what iridescence does: it is a structural colour and it is
            # angle dependent. Two bakes of each head, and the pose picks.
            for hp, grid in (("hf", HEAD_FWD), ("hu", HEAD_UP),
                             ("hd", HEAD_DOWN)):
                add_pose(hp, grid, fa)
                flipped = [r.replace("g", "\x01").replace("p", "g")
                           .replace("\x01", "p") for r in grid]
                add_pose(hp + "P", flipped, fa)

        # Homes on a jittered even grid rather than n uniform draws. Uniform
        # draws clump -- the first version put six of eight birds in the
        # right-hand third and left a third of the panel empty, which on a
        # 5:1 letterbox reads as a bug rather than as a flock.
        home = float(2 + (i + rng.uniform(0.15, 0.85)) / n * (W - 26))
        birds.append({
            "feet": fy, "home": home, "pied": pied, "art": art,
            "face": int(rng.choice([1, -1])),
            "lo": max(-4.0, home - 46.0), "hi": min(W - 14.0, home + 46.0),
        })

    # --- the choreography -------------------------------------------------
    # Appointments first, then the gaps get filled. This is the whole reason
    # the script is generated rather than simulated: a startle is worth
    # waiting for only if it is *placed*, and placing it means knowing when it
    # happens before anybody decides to go for a walk.
    ns = max(0, int(args.startles))
    FLY_DUR = 2.5
    STRUT_DUR = STRUT_PERIOD * STRUT_TURNS
    SQUAB_DUR = SQUAB_LUNGES / SQUAB_RATE
    startles = []
    for i in range(ns):
        base = cycle * (0.30 + 0.36 * i) if ns > 1 else cycle * 0.42
        startles.append(float(base + rng.uniform(-1.5, 1.5)))
    # The last startle has to be far enough from the end that everybody can
    # walk home before the loop closes.
    tail_reserve = 46.0 / speed + 3.0
    startles = [s for s in startles
                if s < cycle - FLY_DUR - tail_reserve - 3.0]

    # The squabble: two neighbours, over the burrito, in a gap between
    # startles. Both birds get the same window, so it is choreographed rather
    # than two birds independently deciding to be annoyed.
    bur_x = float(geo["burrito"][1])
    squab_pair = None
    if n >= 3:
        # The pied one is never in it. He does not fly, he does not squabble,
        # he pecks; keeping him out of every event is what makes him read as
        # a character rather than as a bird with a different palette.
        cands = sorted([i for i in range(n) if not birds[i]["pied"]],
                       key=lambda i: abs(birds[i]["home"] - bur_x))
        if len(cands) >= 2:
            squab_pair = (cands[0], cands[1])
    squab_t = None
    if squab_pair and startles:
        gap0 = startles[0] + FLY_DUR + 2.0
        gap1 = (startles[1] if len(startles) > 1 else cycle - tail_reserve) - 3.0
        if gap1 - gap0 > 4.0:
            squab_t = float(rng.uniform(gap0, gap1 - 3.0))
    elif squab_pair:
        squab_t = cycle * 0.5

    # The strut: the bird immediately behind the pied one displays at him for
    # a good long while. He is pecking. This is the joke that does not need
    # to announce itself.
    strut_i = (pied_i + 1) % n if n >= 2 else None
    strut_t = None
    if strut_i is not None and strut_i != pied_i:
        lim = startles[0] - 5.0 if startles else cycle * 0.4
        if lim > 6.0:
            strut_t = float(rng.uniform(2.0, max(2.5, lim - 6.0)))

    for i, b in enumerate(birds):
        sc = Script(b["home"], b["face"])
        # The cycle opens and closes on a standing bird, so the seam is never
        # a walk cycle restarting mid-stride.
        sc.idle(rng, float(rng.uniform(1.0, 2.4)), 0.6)

        appts = []
        if strut_t is not None and i == strut_i:
            appts.append((strut_t, strut_t + STRUT_DUR, "strut"))
        if squab_t is not None and squab_pair and i in squab_pair:
            appts.append((squab_t, squab_t + SQUAB_DUR, "squab"))
        if not b["pied"]:
            for s in startles:
                # A shared trigger, individual reaction times. Identical
                # launches and identical landings read as one object with
                # eight parts; a fifth of a second of stagger reads as eight
                # birds who all heard the same thing.
                lag = float(rng.uniform(0.0, 0.30))
                appts.append((s + lag,
                              s + lag + FLY_DUR + float(rng.uniform(-0.3, 0.7)),
                              "fly"))
        appts.sort()

        for (at, aend, kind) in appts:
            if at < sc.t + 0.4:
                continue
            if kind == "strut":
                # Walk over to whoever is being displayed at, first.
                target = birds[pied_i]["home"] + (18 if
                                                  birds[pied_i]["home"] <
                                                  b["home"] else -18)
                target = float(np.clip(target, 2, W - 24))
                sc.approach(rng, at, target, speed, b["lo"], b["hi"])
                # Phase 0 or pi: both start and end the circle at sin = 0,
                # which is what keeps the display an oscillation about a
                # fixed spot rather than a slow drift down the pavement.
                sc.add(aend - at, STRUT,
                       (sc.x, STRUT_R, STRUT_PERIOD,
                        float(np.pi * rng.integers(0, 2))))
            elif kind == "squab":
                other = squab_pair[1] if i == squab_pair[0] else squab_pair[0]
                side = -1 if b["home"] <= birds[other]["home"] else 1
                target = float(np.clip(bur_x + side * 13, 2, W - 24))
                sc.approach(rng, at, target, speed, b["lo"], b["hi"])
                sc.add(aend - at, SQUAB, (sc.x, -side))
            else:
                fill(sc, rng, at, speed, b["lo"], b["hi"])
                sc.finish(at)
                # Where they land. "Slightly rearranged" -- a shuffle, not a
                # redistribution; the flock has to look like the same flock.
                land = float(np.clip(sc.x + rng.uniform(-34, 34),
                                     b["lo"], b["hi"]))
                sc.add(aend - at, FLY, (sc.x, land, aend - at))
                sc.x = land

        fill(sc, rng, cycle - tail_reserve, speed, b["lo"], b["hi"])
        return_home(sc, rng, cycle, speed, b["home"])
        b["segs"] = sc.segs
        b["starts"] = [s[0] for s in sc.segs]

    # --- the far birds ----------------------------------------------------
    # Up on the kerb, eight pixels each. They bob, they shuffle a couple of
    # pixels, and they go up when everybody else does. They are here to give
    # the pavement a far end.
    nf = max(0, int(args.far))
    far = []
    far_pal = dict(BASE_PALETTE)
    far_pal.update(MORPHS["checker"])
    far_pal = dict((k, tuple(np.array(v, f32) * 0.80
                             + np.array((118.0, 116.0, 112.0), f32) * 0.26))
                   for k, v in far_pal.items())
    far_art = {}
    for fa in (1, -1):
        for nm, grid in (("a", FAR_A), ("b", FAR_B), ("f", FAR_FLY)):
            rgb, mask = rasterize(grid, far_pal)
            if fa < 0:
                rgb, mask = rgb[:, ::-1], mask[:, ::-1]
            far_art[(nm, fa)] = (
                np.ascontiguousarray(np.clip(rgb, 0, 255).astype(np.uint8)),
                np.ascontiguousarray(mask)[:, :, None])
    for i in range(nf):
        far.append({
            "x": float(rng.uniform(8, W - 16)),
            "y": int(pave_y + 3 + rng.integers(0, 4)),
            "face": int(rng.choice([1, -1])),
            "bob": float(rng.uniform(0.7, 1.5)),
            "phase": float(rng.uniform(0, 6.28)),
            "shuf": float(rng.uniform(6.0, 13.0)),
        })

    # --- feathers ---------------------------------------------------------
    # Six per startle, drifting down for a few seconds afterwards. They are
    # what makes a startle feel like it cost the birds something.
    nfe = 6 * len(startles)
    fe_t0 = np.array([startles[j // 6] + 0.35 + rng.uniform(0, 0.5)
                      for j in range(nfe)], f32)
    fe_x = rng.uniform(20, W - 20, nfe).astype(f32)
    fe_y1 = rng.uniform(foot_lo, foot_hi, nfe).astype(f32)
    fe_fall = rng.uniform(7.0, 13.0, nfe).astype(f32)
    fe_amp = rng.uniform(2.5, 7.0, nfe).astype(f32)
    fe_w = rng.uniform(1.4, 3.0, nfe).astype(f32)
    FEATHER = np.array(FEATHER_COL, np.uint8)

    # --- render -----------------------------------------------------------
    buf = np.empty((H, W, 3), np.uint8)
    STEP = speed * STEP_PERIOD

    def blit(art, mask3, y, x):
        """One masked copy, clipped to the panel. This is the whole renderer."""
        h, w = mask3.shape[:2]
        y0, x0 = (0 if y > 0 else -y), (0 if x > 0 else -x)
        y1 = min(h, H - y)
        x1 = min(w, W - x)
        if y1 <= y0 or x1 <= x0:
            return
        np.copyto(buf[y + y0:y + y1, x + x0:x + x1],
                  art[y0:y1, x0:x1], where=mask3[y0:y1, x0:x1])

    def stab(p):
        """How far down the beak is, through one peck beat. 0..1."""
        if p < 0.20:
            return (p / 0.20) ** 0.55
        if p < 0.40:
            return 1.0
        if p < 0.62:
            return 1.0 - (p - 0.40) / 0.22
        return 0.0

    def render(t, frame):
        tt = t % cycle
        np.copyto(buf, bg)

        for b in birds:
            segs = b["segs"]
            si = bisect.bisect_right(b["starts"], tt) - 1
            if si < 0:
                si = 0
            t0, t1, kind, pay = segs[si]
            rel = tt - t0
            art = b["art"]
            feet = b["feet"]

            if kind == FLY:
                x0, x1, dur = pay
                u = min(1.0, rel / dur)
                # Up hard, off the top of the panel, and back down somewhere
                # else. They vanish entirely for most of it, which is the
                # point: for a second and a half the pavement is empty except
                # for the one who would not go.
                fx = x0 + (x1 - x0) * u
                fy = feet - 118.0 * (np.sin(np.pi * u) ** 0.55)
                up = int(rel / 0.062) % 2 == 0
                fa = 1 if x1 >= x0 else -1
                a, m = art[("flyup" if up else "flydn", fa)]
                # The flight sprite is 26 wide against the body's 18 and is
                # symmetric about that difference, so the same 4px inset
                # aligns it either way round.
                blit(a, m, int(fy) - FLY_FOOT, int(fx) - 4)
                continue

            head_pose = "hf"
            head_dy = 0.0
            head_dx = 0.0
            hface = None
            lift = 0

            if kind == WALK:
                x0, x1, dur = pay
                u = min(1.0, rel / dur)
                bx = x0 + (x1 - x0) * u
                fa = 1 if x1 >= x0 else -1
                ph = (rel / STEP_PERIOD) % 1.0
                # The head bob. f(p) is flat then ramps, so head_rel slides
                # back through the stride and snaps forward at the end of it,
                # which in the world means the head does not move at all and
                # then jumps a whole step. This is the demo.
                fp = 0.0 if ph < HEAD_HOLD else \
                    ((ph - HEAD_HOLD) / (1.0 - HEAD_HOLD)) ** 0.55
                head_dx = fa * STEP * (fp - ph)
                head_dy = -0.9 * (fp - ph) * fa * fa   # dips as it thrusts
                lp = int(ph * 3.0) % 3
                lift = LEG_LIFT[lp]
                pose = "walk%d" % lp
            elif kind == STRUT:
                cx, r, per, phase = pay
                ang = 2.0 * np.pi * rel / per + phase
                bx = cx + r * np.sin(ang)
                fa = 1 if np.cos(ang) >= 0 else -1
                pose = "strut"
                head_dx = -1.0 * fa
                head_dy = 1.0
                # A strutting pigeon bobs too, hard, on the spot.
                head_dy += -1.0 if (rel * 3.1) % 1.0 < 0.4 else 0.0
            elif kind == SQUAB:
                hx, fa = pay
                # Lunge, back off, lunge again -- and throw the wings open on
                # the lunge, which is what a pigeon squabble mostly is: two
                # birds threatening to leave at each other.
                lu = abs(np.sin(np.pi * rel * SQUAB_RATE))
                bx = hx + fa * 4.0 * lu
                pose = "squab" if lu > 0.55 else "walk0"
                head_pose = "hu"
                head_dy = -1.0 if lu > 0.3 else 0.0
            else:                                     # IDLE
                hx, fa, (btimes, bkinds) = pay
                bx = hx
                bi = bisect.bisect_right(btimes, rel) - 1
                if bi < 0:
                    bi = 0
                bk = bkinds[bi]
                brel = rel - btimes[bi]
                pose = "walk0"
                if bk == BEAT_PECK:
                    sa = stab(brel / PECK_BEAT)
                    if sa > 0.04:
                        head_pose = "hd"
                        head_dy = 8.0 * sa
                        head_dx = fa * 3.0 * sa
                elif bk == BEAT_BACK:
                    hface = -fa
                elif bk == BEAT_UP:
                    head_pose = "hu"
                    head_dy = -1.0

            pw, pfoot, hdy, hdx = POSE_GEOMETRY[pose]
            bxi = int(round(bx))
            # Poses are different widths, so they are centred on the walking
            # bird's centre column rather than left-aligned; otherwise a bird
            # that throws its wings open also jumps sideways.
            pxi = bxi + 9 - pw // 2
            byi = feet - pfoot - lift
            if hface is None:
                hface = fa
            # Iridescence: green one way, purple the other. The neck is the
            # only colour on the bird and it changes with the angle, so the
            # variant is chosen by which way the head is pointing relative to
            # the body -- which is exactly the physical thing it depends on.
            hp = head_pose + ("" if hface == fa else "P")

            # Shadow, from the pre-darkened copy of the pavement.
            sy = feet - 1
            sx = bxi + (pw - sh_w) // 2
            if 0 <= sy < H - sh_h:
                y0 = max(0, sy)
                x0 = max(0, sx)
                x1 = min(W, sx + sh_w)
                if x1 > x0:
                    np.copyto(buf[y0:y0 + sh_h, x0:x1],
                              bg_shadow[y0:y0 + sh_h, x0:x1],
                              where=shadow_mask[:, x0 - sx:x1 - sx])

            a, m = art[(pose, fa)]
            blit(a, m, byi, pxi)

            # Head anchor, mirrored with the body when it faces left.
            if hface != fa:
                hdx -= 1
            if fa < 0:
                hdx = pw - HEAD_W - hdx
            a, m = art[(hp, hface)]
            blit(a, m, byi + hdy + int(round(head_dy)),
                 pxi + hdx + int(round(head_dx)))

        # --- the far birds ------------------------------------------------
        for fb in far:
            fx = fb["x"] + 3.0 * np.sin(tt / fb["shuf"] * 6.28 + fb["phase"])
            flying = False
            for s in startles:
                if s <= tt < s + FLY_DUR:
                    flying = True
                    fu = (tt - s) / FLY_DUR
                    break
            if flying:
                fy = fb["y"] - 90.0 * (np.sin(np.pi * fu) ** 0.5)
                a, m = far_art[("f", fb["face"])]
                blit(a, m, int(fy) - 4, int(fx) - 4)
            else:
                nm = "a" if (tt * 1.7 + fb["phase"]) % 1.0 < 0.5 else "b"
                a, m = far_art[(nm, fb["face"])]
                blit(a, m, fb["y"] - 4, int(fx))

        # --- feathers -----------------------------------------------------
        if nfe:
            age = tt - fe_t0
            live = (age > 0.0) & (age < 4.5)
            if live.any():
                ag = age[live]
                fy = fe_y1[live] - fe_fall[live] * (4.5 - ag)
                fx = fe_x[live] + fe_amp[live] * np.sin(fe_w[live] * ag)
                ok = (fy >= 0) & (fy < H)
                yi = fy[ok].astype(np.int32)
                xi = fx[ok].astype(np.int32) % W
                buf[yi, xi] = FEATHER

        return buf

    # Hung off the callback so scripts/test-pigeon.py can assert against the
    # timeline that was actually drawn rather than re-deriving it and quietly
    # drifting from the demo. Nothing in render() reads them.
    render.birds = birds
    render.bg = bg
    render.startles = startles
    render.squabble = (squab_pair, squab_t)
    render.strut = (strut_i, strut_t)
    render.cycle = cycle
    render.geo = geo
    render.step = STEP
    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=20)


if __name__ == "__main__":
    main()
