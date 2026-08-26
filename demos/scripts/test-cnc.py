#!/usr/bin/env python3
"""Checks for cnc.py, the top-down milling panel.

The whole demo is one claim: the picture is a min-composite of a disc swept
along a toolpath, and everything visible is a consequence of that. So the
checks are mostly about the **height field**, not about pixels -- if the field
is right, the shading is a pure function of it and cannot be interestingly
wrong. Specifically:

  * the pocket floor is flat and at the commanded depth, everywhere,
  * the boss is still at the top of the stock, because nothing reached it,
  * the four holes go all the way through,
  * the engraving is in the top of the boss and nowhere else,
  * and Z never goes *up*. That last one is the whole representation: milling
    only ever removes, so a frame that raises the field is a bug by
    definition, and it is the check that would have caught the first version
    of the finishing raster, which ramped Z down over the length of each lane
    and left a wedge of uncut stock at the start of every one.

Then the arc: roughing has to leave a measurably rougher floor than finishing,
or the finishing pass is decoration. That is asserted as a drop in the
standard deviation of the floor between the end of roughing and the end of the
program.

And the contract: render() must be a pure function of t. A cold `render(t0)`
is compared against the same t0 reached frame by frame from zero, and 8 fps is
compared against 30 fps, because the panel's own loop drifts and the scheduler
starts segments at t=0 on a worker thread.

The height field is reached by walking render()'s closure. That is deliberate
-- exposing it as module state purely for a test would put a name in the demo
that nothing else needs -- and it is why this file knows the name `Z`.

    $ python3 scripts/test-cnc.py
    $ python3 scripts/test-cnc.py --dump /tmp/cnc      # write PNGs to look at
"""

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import cnc                                                    # noqa: E402
import demoscene as ds                                        # noqa: E402

FAILED = []
PASSED = [0]

# A window on the pocket floor, in stock-local rows and columns at the default
# panel size: inside the upper band, clear of the wall, clear of the boss, and
# clear of both floor holes. Everything about the floor is asserted here.
#
# It stops short of the boss on purpose. The roughing envelope scallops in
# and out by the trochoid radius, so the row or two next to the island is
# still part stock when the spiral finishes -- which is correct, it is what
# the contour pass is for, but it would swamp a measurement of *surface*
# roughness with a measurement of uncleared material.
FLOOR_WIN = (slice(13, 17), slice(67, 134))


def check(name, ok, detail=""):
    PASSED[0] += 1
    if ok:
        print("  ok   %-54s %s" % (name, detail))
    else:
        print("  FAIL %-54s %s" % (name, detail))
        FAILED.append(name)


def closure(fn):
    if not fn.__closure__:
        return {}
    return dict(zip(fn.__code__.co_freevars,
                    [c.cell_contents for c in fn.__closure__]))


def machine(**kw):
    """A built demo plus the internals the height-field checks need."""
    render = ds.build(cnc, **kw)
    top = closure(render)
    field = closure(top["shade"])["Z"]
    pad, sh, sw = top["PAD"], top["SH"], top["SW"]
    return {"render": render, "Z": field, "PAD": pad, "SH": sh, "SW": sw,
            "CYCLE": top["CYCLE"], "T_CUT": top["T_CUT"], "N": top["N"],
            "PT": top["PT"], "PLAB": top["PLAB"],
            "stock": lambda: field[pad:pad + sh, pad:pad + sw]}


def run_to(m, t, fps=20.0):
    """Drive frame by frame from zero, the way the wall does."""
    n = int(round(t * fps))
    for i in range(n):
        m["render"](i / fps, i)
    return m["render"](t, n)


# --------------------------------------------------------------------------

def test_contract():
    print("\ncontract")
    m = machine()
    out = m["render"](0.0, 0)
    check("frame shape", out.shape == (64, 320, 3), str(out.shape))
    check("frame dtype", out.dtype == np.uint8, str(out.dtype))
    check("cycle length sane", 30.0 <= m["CYCLE"] <= 110.0,
          "%.1f s" % m["CYCLE"])
    check("cutting is most of the cycle", m["T_CUT"] > 0.85 * m["CYCLE"],
          "%.1f of %.1f s" % (m["T_CUT"], m["CYCLE"]))

    # Every operation must actually appear, in order, and none of them may be
    # so short it is a flicker on a panel people walk past.
    labs = m["PLAB"]
    order, starts = [], []
    prev = None
    for i in range(m["N"]):
        if labs[i] != prev:
            order.append(int(labs[i]))
            starts.append(float(m["PT"][i]))
            prev = labs[i]
    check("five operations, each once", order == [0, 1, 2, 3, 4], str(order))
    durs = [starts[k + 1] - starts[k] for k in range(len(starts) - 1)]
    durs.append(m["T_CUT"] - starts[-1])
    check("no operation under 3 s", min(durs) >= 3.0,
          " ".join("%.1f" % d for d in durs))

    n_err = 0
    r = ds.build(cnc)
    for i in range(int(m["CYCLE"] * 20) + 40):
        try:
            r(i / 20.0, i)
        except Exception as exc:                              # noqa: BLE001
            n_err += 1
            if n_err == 1:
                print("      %r" % (exc,))
    check("full loop renders clean", n_err == 0, "%d exceptions" % n_err)


def test_purity():
    print("\npurity")
    for t0 in (3.3, 17.0, 33.0, 48.0, 61.0):
        warm = machine()
        a = run_to(warm, t0).copy()
        cold = machine()
        b = cold["render"](t0, int(t0 * 20)).copy()
        check("cold render(%.1f) == stepped" % t0, np.array_equal(a, b))

    # Past the cycle boundary: the field has to be rebuilt, not carried over.
    m = machine()
    c = m["CYCLE"]
    warm = machine()
    a = run_to(warm, c + 6.0).copy()
    cold = machine()
    b = cold["render"](c + 6.0, 0).copy()
    check("survives the cycle wrap", np.array_equal(a, b))

    # Frame rate must not change what is on screen at a given t. Rates in this
    # demo are per second, and the cut is indexed by path sample rather than
    # integrated, precisely so this holds.
    lo = run_to(machine(), 40.0, fps=8.0).copy()
    hi = run_to(machine(), 40.0, fps=30.0).copy()
    check("8 fps == 30 fps at t=40", np.array_equal(lo, hi))

    a = run_to(machine(), 25.0).copy()
    b = run_to(machine(), 25.0).copy()
    check("same seed, same pixels", np.array_equal(a, b))
    d = run_to(machine(seed=99), 25.0)
    check("different seed, different pixels", not np.array_equal(a, d))


def test_milling():
    print("\nthe height field")
    m = machine()
    run_to(m, m["T_CUT"])
    z = m["stock"]()
    sh, sw = z.shape
    top = float(z.max())
    check("nothing above the stock top", top <= 0.12, "max %.3f mm" % top)

    # The pocket floor: sample well inside the band between the wall and the
    # boss, away from both.
    band = z[FLOOR_WIN]
    floor = float(np.median(band))
    check("pocket floor at depth", -4.0 < floor < -2.5, "%.2f mm" % floor)
    check("pocket floor is flat", float(np.ptp(band)) < 0.08,
          "ptp %.3f mm" % float(np.ptp(band)))

    # The boss is not drawn anywhere. It is what the spiral never reached.
    boss = z[int(sh * 0.46):int(sh * 0.54), int(sw * 0.30):int(sw * 0.34)]
    check("boss survives at stock top", float(np.median(boss)) > -0.2,
          "%.2f mm" % float(np.median(boss)))

    deep = int((z < -6.0).sum())
    check("four holes go through", deep >= 4 * 5, "%d px below -6 mm" % deep)

    eng = (z < -1.2) & (z > -2.2)
    check("engraving present", int(eng.sum()) >= 120, "%d px" % int(eng.sum()))
    rows = np.flatnonzero(eng.any(axis=1))
    check("engraving is only on the boss",
          rows.size and (rows.min() > sh * 0.35) and (rows.max() < sh * 0.65),
          "rows %s..%s of %d" % (rows.min() if rows.size else "-",
                                 rows.max() if rows.size else "-", sh))


def test_subtractive():
    print("\nsubtraction")
    # Milling only removes. Sampled through the program, the field must be
    # monotonically non-increasing everywhere, always.
    m = machine()
    prev = None
    worst = 0.0
    for i in range(int(m["T_CUT"] * 20)):
        m["render"](i / 20.0, i)
        if i % 40 == 0:
            cur = m["stock"]().copy()
            if prev is not None:
                worst = max(worst, float((cur - prev).max()))
            prev = cur
    check("Z never increases", worst <= 1e-6, "worst rise %.2e mm" % worst)


def test_finishing_arc():
    print("\nroughing -> finishing")
    m = machine()
    labs = m["PLAB"]
    # The last sample of operation 0 (adaptive) and of operation 2 (finish).
    t_rough = float(m["PT"][int(np.flatnonzero(np.asarray(labs) == 0)[-1])])
    t_fin = float(m["PT"][int(np.flatnonzero(np.asarray(labs) == 2)[-1])])
    run_to(m, t_rough)
    rough = float(m["stock"]()[FLOOR_WIN].std())
    run_to(m, t_fin)
    fine = float(m["stock"]()[FLOOR_WIN].std())
    check("roughing leaves a rough floor", rough > 0.030, "sd %.4f mm" % rough)
    check("finishing flattens it", fine < 0.45 * rough,
          "sd %.4f -> %.4f mm" % (rough, fine))


def test_variants():
    print("\noptions and panel sizes")
    cases = [dict(width=128, height=32), dict(width=192, height=48),
             dict(width=256, height=64), dict(chips=False), dict(speed=2.4),
             dict(speed=0.5), dict(stepover=0.9), dict(trochoid=1.4),
             dict(text="MADE HERE"), dict(text=""), dict(seed=12345)]
    for kw in cases:
        try:
            r = ds.build(cnc, **kw)
            for i in range(0, 2000, 3):
                out = r(i / 20.0, i)
            ok, detail = True, "%dx%d" % (out.shape[1], out.shape[0])
        except Exception as exc:                              # noqa: BLE001
            ok, detail = False, repr(exc)
        check("builds and renders %s" % kw, ok, detail)


def test_readout():
    print("\nreadout")
    m = machine()
    out = run_to(m, 12.0)
    h = out.shape[0]
    band = out[h - 8:h]
    amber = (band[:, :, 0] > 120) & (band[:, :, 2] < 90)
    check("DRO band carries type", int(amber.sum()) > 60,
          "%d lit px" % int(amber.sum()))
    # The picture must not be leaking into the readout band, and the readout
    # must not be leaking into the picture: the billet stops above it.
    check("readout band is dark ground",
          float(band[band[:, :, 0] <= 120].mean()) < 40.0)


def test_no_network():
    print("\nhygiene")
    src = open(os.path.join(HERE, "cnc.py")).read()
    bad = [w for w in ("urllib", "requests", "socket", "http.client",
                       "subprocess") if w in src]
    check("no network or subprocess", not bad, ",".join(bad))
    check("no PIL dependency", "PIL" not in src and "Image" not in src)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dump", help="write representative frames here as PNG")
    args = ap.parse_args()

    test_contract()
    test_purity()
    test_milling()
    test_subtractive()
    test_finishing_arc()
    test_variants()
    test_readout()
    test_no_network()

    if args.dump:
        from PIL import Image                     # test-side only, see §5
        os.makedirs(args.dump, exist_ok=True)
        m = machine()
        r = m["render"]
        n = 0
        for i in range(int(m["CYCLE"] * 20) + 20):
            f = r(i / 20.0, i)
            if i % 20 == 0:
                Image.fromarray(f).resize((960, 192), Image.NEAREST).save(
                    os.path.join(args.dump, "cnc-%05.1f.png" % (i / 20.0)))
                n += 1
        print("\nwrote %d frames to %s" % (n, args.dump))

    print("\n%d checks, %d failed" % (PASSED[0], len(FAILED)))
    for name in FAILED:
        print("  FAILED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
