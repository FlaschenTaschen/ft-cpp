#!/usr/bin/env python3
"""Sorting algorithms racing, one array element per column.

The wall is 320 pixels wide, which is a 320-element array with a pixel to
spare. That is the whole idea: the array is not drawn *on* the panel at some
scale, it *is* the panel, one column per element, so nothing is aggregated or
sampled away and every comparison the algorithm makes is a column you can
point at. A 5:1 letterbox is close to the ideal shape for this -- an array is
a long thin thing and so is this wall.

Value is carried twice, by bar height and by hue. Height alone is what every
textbook animation does and it is the weaker half here: 64 rows is six bits of
height, and from across a room a field of 320 one-pixel-wide bars is a texture
rather than a signal. Hue is what actually reads. A shuffled array is
confetti, a nearly-sorted one is visibly a rainbow with a few wrong stripes in
it, and the moment a partition settles you see a smooth ramp appear inside it.
The bars stay because up close they are what makes the shape of a partition or
a merge legible; the dim tinted column under each bar keeps the hue readable
across the whole panel. --style band drops the bars for a full-height colour
field, which carries further across a big room but lights every pixel at full
brightness, and loses the contrast that makes the working region stand out.

Each algorithm gets a slot: a shuffle that is itself an animation rather than
a cut, the sort, then the classic ascending confirmation sweep. Four of them
fit a 45 s segment.

**The steps are decoupled from the frames**, which is most of what makes this
work. Bubble sort on 320 elements takes 50,830 steps, quicksort 4,580 and
radix 960; played one step per frame the first would run for half an hour and
the last would be over in half a minute, and there is no single rate that
suits both. So each algorithm is run to completion inside build() and recorded
as a flat trace of steps -- at most two writes and a highlight state per step
-- and render() plays back however many steps its segment's clock says have
happened by now. Every algorithm therefore takes the same wall time regardless
of how much work it does, and the visible *rate* is what tells you how much
work that is: quicksort strolls, bubble sort tears along and still barely
gets there.

Recording rather than stepping live also makes render() a function of t. The
trace is replayed from a per-segment snapshot, so a seek, a restart at t=0 or
a preview baker stepping at its own rate all land on the same picture.

The working state is highlighted, because without it this is just bars moving:
quicksort's pivot and partition bounds, radix's write cursor and the sorted
prefix growing behind it, bubble's pair and its shrinking unsorted region,
heapsort's sift path. Two cursor columns light full height and the active
region is lifted out of the dim.

Run:  python3 sort.py --host 127.0.0.1
      python3 sort.py --algorithms quicksort,merge --style band
      python3 sort.py --algorithms bubble --cycle 60 --element-px 2
"""

import bisect
import sys

import numpy as np

import demoscene as ds

f32 = ds.f32

# A 3x5 bitmap font for the labels, baked here rather than loaded, so there is
# no font file to be missing on the Pi. Five rows of three bits, MSB leftmost.
_FONT = {
    " ": (0, 0, 0, 0, 0),
    "A": (0b010, 0b101, 0b111, 0b101, 0b101),
    "B": (0b110, 0b101, 0b110, 0b101, 0b110),
    "C": (0b011, 0b100, 0b100, 0b100, 0b011),
    "D": (0b110, 0b101, 0b101, 0b101, 0b110),
    "E": (0b111, 0b100, 0b110, 0b100, 0b111),
    "F": (0b111, 0b100, 0b110, 0b100, 0b100),
    "G": (0b011, 0b100, 0b101, 0b101, 0b011),
    "H": (0b101, 0b101, 0b111, 0b101, 0b101),
    "I": (0b111, 0b010, 0b010, 0b010, 0b111),
    "J": (0b001, 0b001, 0b001, 0b101, 0b010),
    "K": (0b101, 0b101, 0b110, 0b101, 0b101),
    "L": (0b100, 0b100, 0b100, 0b100, 0b111),
    "M": (0b101, 0b111, 0b111, 0b101, 0b101),
    "N": (0b101, 0b111, 0b111, 0b111, 0b101),
    "O": (0b010, 0b101, 0b101, 0b101, 0b010),
    "P": (0b110, 0b101, 0b110, 0b100, 0b100),
    "Q": (0b010, 0b101, 0b101, 0b111, 0b011),
    "R": (0b110, 0b101, 0b110, 0b101, 0b101),
    "S": (0b011, 0b100, 0b010, 0b001, 0b110),
    "T": (0b111, 0b010, 0b010, 0b010, 0b010),
    "U": (0b101, 0b101, 0b101, 0b101, 0b011),
    "V": (0b101, 0b101, 0b101, 0b101, 0b010),
    "W": (0b101, 0b101, 0b111, 0b111, 0b101),
    "X": (0b101, 0b101, 0b010, 0b101, 0b101),
    "Y": (0b101, 0b101, 0b010, 0b010, 0b010),
    "Z": (0b111, 0b001, 0b010, 0b100, 0b111),
    "0": (0b111, 0b101, 0b101, 0b101, 0b111),
    "1": (0b010, 0b110, 0b010, 0b010, 0b111),
    "2": (0b110, 0b001, 0b010, 0b100, 0b111),
    "3": (0b110, 0b001, 0b010, 0b001, 0b110),
    "4": (0b101, 0b101, 0b111, 0b001, 0b001),
    "5": (0b111, 0b100, 0b110, 0b001, 0b110),
    "6": (0b011, 0b100, 0b110, 0b101, 0b010),
    "7": (0b111, 0b001, 0b010, 0b010, 0b010),
    "8": (0b010, 0b101, 0b010, 0b101, 0b010),
    "9": (0b010, 0b101, 0b011, 0b001, 0b110),
    "!": (0b010, 0b010, 0b010, 0b000, 0b010),
    "-": (0b000, 0b000, 0b111, 0b000, 0b000),
    ".": (0b000, 0b000, 0b000, 0b000, 0b010),
}


def text_mask(text, scale=1):
    """A boolean (5*scale, (4n-1)*scale) stamp for a short string."""
    glyphs = [_FONT.get(ch, _FONT[" "]) for ch in text.upper()]
    if not glyphs:
        return np.zeros((0, 0), bool)
    out = np.zeros((5, len(glyphs) * 4 - 1), bool)
    for i, rows in enumerate(glyphs):
        for r, bits in enumerate(rows):
            for c in range(3):
                if bits & (1 << (2 - c)):
                    out[r, i * 4 + c] = True
    if scale > 1:
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)
    return out


# --------------------------------------------------------------------------
# Recording an algorithm.
#
# Every algorithm here is expressed as a sequence of steps, and a step is at
# most two writes plus what the algorithm was thinking about when it made
# them. That is enough to express all of them -- a swap is two writes, a merge
# or a radix pass is one write each, a comparison that changes nothing is a
# step with no writes at all -- and it means the playback in render() knows
# nothing about sorting.
#
# Highlights are (cursor a, cursor b, region low, region high), -1 for absent.
# --------------------------------------------------------------------------

class Trace(object):
    """An algorithm's whole run, as flat lists of small integers."""

    def __init__(self, values):
        self.a = list(values)
        self.wr = []                         # (p0, v0, p1, v1)
        self.hl = []                         # (ca, cb, lo, hi)

    def mark(self, ca=-1, cb=-1, lo=-1, hi=-1):
        """A comparison: nothing moves, but the cursors do."""
        self.wr.append((-1, -1, -1, -1))
        self.hl.append((ca, cb, lo, hi))

    def write(self, i, v, ca=-1, cb=-1, lo=-1, hi=-1):
        self.a[i] = v
        self.wr.append((i, v, -1, -1))
        self.hl.append((i if ca < 0 else ca, cb, lo, hi))

    def swap(self, i, j, ca=-1, cb=-1, lo=-1, hi=-1):
        a = self.a
        a[i], a[j] = a[j], a[i]
        self.wr.append((i, a[i], j, a[j]))
        self.hl.append((i if ca < 0 else ca, j if cb < 0 else cb, lo, hi))


def shuffle(tr, rng):
    """Fisher-Yates, recorded, so the reset between algorithms is animated.

    A cut back to a fresh random array reads as a glitch; 319 swaps scattered
    over a second reads as the array being thrown in the air.
    """
    n = len(tr.a)
    for i in range(n - 1, 0, -1):
        j = int(rng.integers(0, i + 1))
        if i != j:
            tr.swap(i, j)
        else:
            tr.mark(ca=i)


def bubble(tr):
    """The slow one, and the reason the playback rate has to be per algorithm.

    ~51,000 comparisons on 320 elements. The shape is the point: a diagonal
    creep as the largest element is walked to the right end on every pass, and
    a sorted tail growing back towards the middle.
    """
    n = len(tr.a)
    a = tr.a
    for end in range(n - 1, 0, -1):
        swapped = False
        for i in range(end):
            if a[i] > a[i + 1]:
                tr.swap(i, i + 1, lo=0, hi=end)
                swapped = True
            else:
                tr.mark(ca=i, cb=i + 1, lo=0, hi=end)
        if not swapped:
            break


def insertion(tr):
    """Bubble's better-behaved cousin: a sorted prefix and a shifting tail."""
    a = tr.a
    for i in range(1, len(a)):
        v = a[i]
        j = i - 1
        while j >= 0 and a[j] > v:
            tr.write(j + 1, a[j], ca=j + 1, cb=i, lo=0, hi=i)
            j -= 1
        tr.write(j + 1, v, ca=j + 1, cb=i, lo=0, hi=i)


def quicksort(tr):
    """Lomuto partitioning with a median-of-three pivot, iteratively.

    Recursion would work as well, but an explicit stack is what makes the
    order of the partitions visible and predictable: the panel shows one
    partition being scanned end to end, then the two halves of it, and so on
    down. Median-of-three is not just hygiene here -- a naive last-element
    pivot on an array we shuffled ourselves is fine, but on the near-sorted
    array a short --cycle can leave behind it degenerates to n**2 and blows
    the step budget.
    """
    a = tr.a
    stack = [(0, len(a) - 1)]
    while stack:
        lo, hi = stack.pop()
        if lo >= hi:
            continue
        mid = (lo + hi) // 2
        # Order lo, mid, hi and leave the median at hi as the pivot.
        if a[mid] < a[lo]:
            tr.swap(lo, mid, lo=lo, hi=hi)
        if a[hi] < a[lo]:
            tr.swap(lo, hi, lo=lo, hi=hi)
        if a[hi] < a[mid]:
            tr.swap(mid, hi, lo=lo, hi=hi)
        pivot = a[hi]
        i = lo
        for j in range(lo, hi):
            tr.mark(ca=hi, cb=j, lo=lo, hi=hi)
            if a[j] <= pivot:
                if i != j:
                    tr.swap(i, j, ca=hi, cb=j, lo=lo, hi=hi)
                i += 1
        tr.swap(i, hi, ca=i, cb=hi, lo=lo, hi=hi)
        stack.append((lo, i - 1))
        stack.append((i + 1, hi))


def heapsort(tr):
    """Heapify, then peel the root off the top n-1 times.

    The sift path is the thing worth watching -- a cursor pair walking down a
    binary tree laid out in an array is a stride that doubles, which is a
    motion nothing else here makes.
    """
    a = tr.a
    n = len(a)

    def sift(root, end):
        while True:
            child = 2 * root + 1
            if child > end:
                return
            if child + 1 <= end:
                tr.mark(ca=child, cb=child + 1, lo=0, hi=end)
                if a[child + 1] > a[child]:
                    child += 1
            tr.mark(ca=root, cb=child, lo=0, hi=end)
            if a[root] >= a[child]:
                return
            tr.swap(root, child, lo=0, hi=end)
            root = child

    for start in range(n // 2 - 1, -1, -1):
        sift(start, n - 1)
    for end in range(n - 1, 0, -1):
        tr.swap(0, end, lo=0, hi=end)
        sift(0, end - 1)


def mergesort(tr):
    """Bottom-up merge, so the runs double visibly with every pass.

    Top-down would produce the same writes in a different order and read as
    quicksort backwards; bottom-up gives the panel its own shape, a comb of
    sorted runs whose teeth double in width each time across.
    """
    a = tr.a
    n = len(a)
    width = 1
    while width < n:
        for lo in range(0, n, 2 * width):
            mid = min(lo + width, n)
            hi = min(lo + 2 * width, n)
            if mid >= hi:
                continue
            left, right = a[lo:mid], a[mid:hi]
            i = j = 0
            for k in range(lo, hi):
                if j >= len(right) or (i < len(left) and left[i] <= right[j]):
                    v, src = left[i], lo + i
                    i += 1
                else:
                    v, src = right[j], mid + j
                    j += 1
                tr.write(k, v, ca=k, cb=src, lo=lo, hi=hi - 1)
        width *= 2


def radix(tr, bits=4):
    """LSD radix, one stable counting sort per 4-bit digit.

    Three passes get 320 values sorted, and each pass is 320 writes -- an
    order of magnitude fewer steps than anything else here, which is exactly
    why the pacing is per algorithm. It plays as three deliberate left-to-
    right sweeps, and the intermediate states are the giveaway: after the
    first pass the array is nonsense that repeats every sixteen values, after
    the second it is sixteen ascending ramps, and the third lands it.
    """
    a = tr.a
    n = len(a)
    base = 1 << bits
    shift = 0
    while (max(a) >> shift) > 0:
        snapshot = list(a)
        count = [0] * base
        for v in snapshot:
            count[(v >> shift) & (base - 1)] += 1
        start = [0] * base
        total = 0
        for d in range(base):
            start[d] = total
            total += count[d]
        out = [0] * n
        for v in snapshot:
            d = (v >> shift) & (base - 1)
            out[start[d]] = v
            start[d] += 1
        # The array is rewritten in place from the computed pass, so what is
        # on the panel mid-pass is genuinely half old and half new -- which is
        # the sorted prefix creeping right that gives radix its look.
        for k in range(n):
            tr.write(k, out[k], ca=k, lo=0, hi=k)
        shift += bits


ALGORITHMS = {
    "quicksort": (quicksort, "QUICKSORT"),
    "merge": (mergesort, "MERGE SORT"),
    "heap": (heapsort, "HEAPSORT"),
    "radix": (radix, "RADIX LSD"),
    "bubble": (bubble, "BUBBLE SORT"),
    "insertion": (insertion, "INSERTION"),
}

DEFAULT_ALGORITHMS = "quicksort,radix,bubble,heap"


def add_arguments(ap):
    ds.palette_argument(ap, "rainbow")
    ap.add_argument("--algorithms", type=str, default=DEFAULT_ALGORITHMS,
                    help="comma-separated: " + ",".join(sorted(ALGORITHMS)))
    ap.add_argument("--style", default="bars", choices=["bars", "band"],
                    help="bars from the bottom, or a full-height colour band")
    ap.add_argument("--cycle", type=float, default=45.0,
                    help="seconds for the whole run, shared between algorithms")
    ap.add_argument("--shuffle-seconds", type=float, default=1.1,
                    help="the animated reset before each algorithm")
    ap.add_argument("--sweep-seconds", type=float, default=1.4,
                    help="the ascending confirmation pass after each")
    ap.add_argument("--element-px", type=int, default=1,
                    help="pixels per array element; 1 gives one per column")
    ap.add_argument("--floor", type=float, default=0.14,
                    help="brightness of the tinted column under a bar, 0..1")
    ap.add_argument("--labels", dest="labels", action="store_true", default=True)
    ap.add_argument("--no-labels", dest="labels", action="store_false",
                    help="drop the algorithm name in the corner")
    ap.add_argument("--label-scale", type=int, default=2, choices=[1, 2, 3],
                    help="label size; the font is 3x5 before scaling")
    ap.add_argument("--seed", type=int, default=0, help="0 picks one at random")


def _mix(lut, white, k):
    """Pull a palette toward white; k 0 leaves it alone."""
    return lut * (1.0 - k) + white * k


def build(args):
    W, H = args.width, args.height
    epx = max(1, args.element_px)
    n = max(8, W // epx)

    names = [s.strip().lower() for s in args.algorithms.split(",") if s.strip()]
    bad = [s for s in names if s not in ALGORITHMS]
    if bad:
        raise ValueError("unknown algorithm(s) %s; have %s"
                         % (", ".join(bad), ", ".join(sorted(ALGORITHMS))))
    if not names:
        names = DEFAULT_ALGORITHMS.split(",")

    rng = np.random.default_rng(args.seed or None)

    # ----------------------------------------------------------------------
    # Run every algorithm to completion here, once, and keep only the trace.
    # A segment is a slice of the timeline pointing at a slice of that trace,
    # plus the array state it starts from -- which is what makes a seek cheap
    # and a wrap back to t=0 exact.
    # ----------------------------------------------------------------------
    ordered = list(range(n))
    state = list(ordered)
    wr, hl, segments = [], [], []
    clock = 0.0
    slot = max(args.cycle / len(names), 3.0)

    def add_segment(kind, label, trace, seconds, state0):
        nonlocal clock
        step0 = len(wr)
        if trace is not None:
            wr.extend(trace.wr)
            hl.extend(trace.hl)
        segments.append({"t0": clock, "dur": max(seconds, 0.05), "kind": kind,
                         "label": label, "step0": step0, "step1": len(wr),
                         "state0": np.asarray(state0, np.int16)})
        clock += max(seconds, 0.05)

    for name in names:
        fn, label = ALGORITHMS[name]

        tr = Trace(state)
        shuffle(tr, rng)
        add_segment("shuffle", "SHUFFLE", tr, args.shuffle_seconds, state)
        state = list(tr.a)

        shuffled = list(state)
        tr = Trace(state)
        fn(tr)
        # Unusually easy to test properly, so test it: an algorithm that
        # dropped or duplicated an element would otherwise render perfectly
        # valid frames of the wrong thing for the rest of its slot.
        if tr.a != ordered:
            if sorted(tr.a) != ordered:
                raise ValueError("%s did not preserve the array" % name)
            raise ValueError("%s did not sort the array" % name)
        add_segment("sort", label, tr,
                    slot - args.shuffle_seconds - args.sweep_seconds, shuffled)
        state = list(tr.a)

        add_segment("sweep", "SORTED!", None, args.sweep_seconds, state)

    total = clock
    # Plain floats and bisect rather than numpy: this is scalar work on a list
    # of a dozen items, and a numpy scalar costs far more per operation than a
    # Python one on the hardware these run on.
    starts = [s["t0"] for s in segments]
    WR = np.asarray(wr, np.int16).reshape(-1, 4)
    HL = np.asarray(hl, np.int16).reshape(-1, 4)

    # ----------------------------------------------------------------------
    # Colour. Six tiers of the same ramp -- (normal, region, cursor) x (under
    # a bar, in a bar) -- concatenated into one table, so a frame is one
    # index image and one gather rather than any compositing.
    # ----------------------------------------------------------------------
    lut = ds.named_palette(args.palette, 256).astype(f32)
    white = np.full_like(lut, 255.0)
    floor = float(np.clip(args.floor, 0.0, 1.0))
    tiers = [
        lut * floor,                              # normal, under the bar
        lut,                                      # normal, bar
        _mix(lut, white, 0.10) * (floor + 0.22),  # active region, under
        _mix(lut, white, 0.30),                   # active region, bar
        _mix(lut, white, 0.55) * 0.80,            # cursor, under
        _mix(lut, white, 0.85),                   # cursor, bar
    ]
    table = np.clip(np.concatenate(tiers, axis=0), 0, 255).astype(np.uint8)

    # Value -> palette entry, and value -> the top row of its bar. Both are
    # lookups indexed by the value itself, so a frame never divides.
    pal_of = (np.arange(n, dtype=np.int32) * 255 // max(n - 1, 1)).astype(np.int16)
    if args.style == "band":
        top_of = np.zeros(n, np.int16)
    else:
        heights = 1 + (np.arange(n, dtype=np.int32) * (H - 1) // max(n - 1, 1))
        top_of = (H - heights).astype(np.int16)

    col_of = np.minimum(np.arange(W) // epx, n - 1)      # column -> element
    rows = np.arange(H, dtype=np.int16)[:, None]

    labels = {}
    if args.labels:
        for seg in segments:
            if seg["label"] not in labels:
                labels[seg["label"]] = text_mask(seg["label"],
                                                 max(1, args.label_scale))

    out = np.empty((H, W, 3), np.uint8)
    mask = np.empty((H, W), bool)
    idx = np.empty((H, W), np.int16)
    hcls = np.empty(n, np.int16)
    cur = [-1]                                   # steps applied to `live`
    live = list(ordered)
    label_rgb = np.array((228, 232, 244), np.uint8)

    def apply(seg, k):
        """Bring `live` up to step k, replaying from the segment's snapshot."""
        if cur[0] < seg["step0"] or cur[0] > k:
            live[:] = seg["state0"].tolist()
            cur[0] = seg["step0"]
        if k > cur[0]:
            for p0, v0, p1, v1 in WR[cur[0]:k].tolist():
                if p0 >= 0:
                    live[p0] = v0
                    if p1 >= 0:
                        live[p1] = v1
            cur[0] = k

    def render(t, frame):
        tt = t % total
        seg = segments[max(bisect.bisect_right(starts, tt) - 1, 0)]
        p = min(max((tt - seg["t0"]) / seg["dur"], 0.0), 1.0)

        k = seg["step0"] + int(p * (seg["step1"] - seg["step0"]))
        apply(seg, k)

        hcls[:] = 0
        if seg["kind"] == "sweep":
            # The confirmation pass: everything behind the front is lifted out
            # of the dim, and the front itself is a bright wedge running up
            # the finished rainbow.
            front = int(p * (n + 6)) - 3
            if front > 0:
                hcls[:min(front, n)] = 1
            # Both ends clamped into 0..n before slicing: a front still off
            # the left edge gives a negative stop, and hcls[0:-1] = 2 lights
            # the entire array as if every column were the cursor.
            lo = min(max(front - 2, 0), n)
            hi = min(max(front + 1, 0), n)
            if hi > lo:
                hcls[lo:hi] = 2
        elif k > seg["step0"]:
            ca, cb, lo, hi = HL[k - 1].tolist()
            if 0 <= lo <= hi < n:
                hcls[lo:hi + 1] = 1
            if 0 <= ca < n:
                hcls[ca] = 2
            if 0 <= cb < n:
                hcls[cb] = 2

        el = np.asarray(live, np.int16)
        base = (hcls * 512 + pal_of[el])[col_of]
        np.greater_equal(rows, top_of[el][col_of], out=mask)
        np.multiply(mask, np.int16(256), out=idx)
        np.add(idx, base, out=idx)
        np.take(table, idx, axis=0, out=out)

        stamp = labels.get(seg["label"])
        if stamp is not None and stamp.size:
            lh, lw = stamp.shape
            y, x = 3, 4
            if y + lh + 1 <= H and x + lw + 1 <= W:
                # Knock the plate back rather than filling it: the bars stay
                # faintly visible through the label, which keeps it reading as
                # an overlay instead of as a hole in the array.
                plate = out[y - 2:y + lh + 2, x - 2:x + lw + 2]
                np.right_shift(plate, 2, out=plate)
                out[y:y + lh, x:x + lw][stamp] = label_rgb
        return out

    return render


def main():
    ds.standalone(sys.modules[__name__], __doc__.split("\n", 1)[0], fps=30)


if __name__ == "__main__":
    main()
