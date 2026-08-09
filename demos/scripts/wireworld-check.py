#!/usr/bin/env python3
"""Assertions for demos/wireworld.py -- the rule, the parts, and the circuit.

A mis-laid Wireworld circuit still animates convincingly while computing
nothing: electrons run around, gates flash, and the picture is wrong in a way
no amount of looking at it will show. So everything here is checked by driving
it and reading the answer off, not by eye.

    python3 scripts/wireworld-check.py
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

import demoscene as ds                                       # noqa: E402
import wireworld as ww                                       # noqa: E402

EMPTY, HEAD, TAIL, WIRE = ww.EMPTY, ww.HEAD, ww.TAIL, ww.WIRE


def check(name, got, want):
    if got != want:
        raise AssertionError("%s: got %r, want %r" % (name, got, want))
    print("  ok  %-46s %r" % (name, got))


# ---------------------------------------------------------------- the rule
def test_rule():
    """head -> tail, tail -> conductor, conductor -> head on 1 or 2 heads."""
    print("rule")
    for n in range(9):
        # A centre cell of each state with exactly n head neighbours around it.
        g = np.zeros((5, 5), np.uint8)
        ring = [(1, 1), (1, 2), (1, 3), (2, 3), (3, 3), (3, 2), (3, 1), (2, 1)]
        for state in (EMPTY, HEAD, TAIL, WIRE):
            g[:] = EMPTY
            g[2, 2] = state
            for cell in ring[:n]:
                g[cell] = HEAD
            out = int(ww.step(g)[2, 2])
            want = {EMPTY: EMPTY, HEAD: TAIL, TAIL: WIRE}.get(state)
            if want is None:
                want = HEAD if n in (1, 2) else WIRE
            check("state %d with %d head neighbours" % (state, n), out, want)


# --------------------------------------------------------------- the clock
def test_clock():
    """A loop of N conductor cells carries an electron round in N steps."""
    print("clock")
    for run, side in ((8, 4), (13, 5), (4, 3)):
        cells = ww.check_ring(ww.ring_path(2, 3, run, side))
        h = max(r for r, _ in cells) + 3
        w = max(c for _, c in cells) + 3
        g = np.zeros((h, w), np.uint8)
        for cell in cells:
            g[cell] = WIRE
        ww.seed(g, cells, 0)
        start, period = g.copy(), None
        for i in range(1, 4 * len(cells)):
            g = ww.step(g)
            if np.array_equal(g, start):
                period = i
                break
        check("loop of %d cells has period" % len(cells), period, len(cells))


# --------------------------------------------------------------- the diode
def diode_board(reverse):
    """A straight wire with the circuit's own diode in the middle of it."""
    b = ww.Board(7, 20)
    path = b.lay("wire", ww.route((3, 1), (3, 18)))
    b.diode(path, 8)
    g = b.g.copy()
    inject, probe = (1, 18) if not reverse else (18, 1)
    g[3, inject] = HEAD
    return g, (3, probe)


def test_diode():
    """One pulse forwards; a pulse the other way dies at the junction."""
    print("diode")
    for reverse, want in ((False, 1), (True, 0)):
        g, probe = diode_board(reverse)
        arrivals = 0
        for _ in range(60):
            g = ww.step(g)
            arrivals += int(g[probe] == HEAD)
        check("pulses out of the %s side" % ("near" if reverse else "far"),
              arrivals, want)
    # and it must not leave anything behind either way
    for reverse in (False, True):
        g, _ = diode_board(reverse)
        for _ in range(60):
            g = ww.step(g)
        check("quiet after a %s pulse" % ("reverse" if reverse else "forward"),
              int((g == HEAD).sum() + (g == TAIL).sum()), 0)


# --------------------------------------------------------------- the gates
def gate_board(inhibit):
    """One gate on its own, with a long wire on each of its two inputs."""
    b = ww.Board(ww.GRID_H, 60)
    g = ww.gate(b, "g", 30, inhibit=inhibit)
    sig = b.lay("sig", ww.route((ww.GATE_ROW - 12, g["sig"][1]), g["sig"]))
    ctrl = b.lay("ctrl", ww.route((ww.GATE_ROW, g["x"][1] - 12), g["x"]))
    tail = ww.descend(g["out"], ww.GATE_ROW + 8)
    out = b.lay("out", ww.route(g["out"], tail, (ww.GATE_ROW + 8, 55)))
    return b, sig, ctrl, out


def drive(b, sig, ctrl, out, a, c, delay=40):
    """Put a pulse on either or both inputs; count what leaves the output."""
    g = b.g.copy()
    fired = 0
    # The control fork has to have its three cells alight on the generation the
    # centre cell is deciding, which is one after the signal reaches it: the
    # control must arrive at its port exactly one step before the signal does.
    # The two input wires here are not the same length, so that is a launch
    # offset rather than a simultaneous start.
    at_sig = len(ctrl) - len(sig) + 1
    for s in range(delay):
        if c and s == 0:
            g[ctrl[0]] = HEAD
        if a and s == at_sig:
            g[sig[0]] = HEAD
        g = ww.step(g)
        fired += int(g[out[-1]] == HEAD)
    return fired


def test_gates():
    """Both gates, over their whole truth table."""
    for inhibit, name, table in (
            (False, "or", {(0, 0): 0, (0, 1): 1, (1, 0): 1, (1, 1): 1}),
            (True, "and-not", {(0, 0): 0, (0, 1): 0, (1, 0): 1, (1, 1): 0})):
        print(name)
        b, sig, ctrl, out = gate_board(inhibit)
        for (a, c), want in sorted(table.items()):
            check("sig=%d ctrl=%d -> out" % (a, c),
                  drive(b, sig, ctrl, out, a, c), want)


# ------------------------------------------------------------- the circuit
def circuit_events(steps=72 * 10):
    b, ports = ww.circuit()
    g = b.g.copy()
    ww.seed(g, b.nets["clock.a"], ww.PHASE_A)
    ww.seed(g, b.nets["clock.b"], ww.PHASE_B)
    probes = {"or": ports["g0"]["t"], "and-not": ports["g1"]["t"],
              "and": ports["g2"]["t"],
              "a": ports["g1"]["sig"], "b": ports["g1"]["x"]}
    hits = {k: [] for k in probes}
    live = []
    states = []
    for s in range(steps):
        g = ww.step(g)
        states.append(g.copy())
        live.append(int((g == HEAD).sum()))
        for k, cell in probes.items():
            if g[cell] == HEAD:
                hits[k].append(s)
    return hits, live, states


def test_circuit():
    """The board: the truth table, the period, and no death or flood."""
    print("circuit")
    hits, live, states = circuit_events()
    cycles = 4
    lo, hi = 72 * 5, 72 * (5 + cycles)          # after the loops have filled
    per = {k: len([x for x in v if lo <= x < hi]) / cycles
           for k, v in hits.items()}
    check("clock A pulses per 72 generations", per["a"], 3.0)
    check("clock B pulses per 72 generations", per["b"], 2.0)
    check("OR fires per cycle (either clock)", per["or"], 4.0)
    check("AND-NOT fires per cycle (A without B)", per["and-not"], 2.0)
    check("AND fires per cycle (A with B)", per["and"], 1.0)

    # A and B land together exactly once a cycle, one step apart at the gate.
    a, bb = set(hits["a"]), set(hits["b"])
    check("coincidences per cycle",
          len([x for x in a if x - 1 in bb and lo <= x < hi]) / cycles, 1.0)

    settled = ww.WARMUP
    check("the board repeats every 72 generations",
          all(np.array_equal(states[i], states[i + 72])
              for i in range(settled, len(states) - 72)), True)
    # Dying out and flooding are the two ways this fails while still looking
    # plausible, so both get a number rather than a glance.
    check("never goes dead (fewest live electrons)",
          min(live[ww.WARMUP:]) > 0, True)
    check("never floods (most live electrons)", max(live) < 60, True)


def test_diodes_earn_their_keep():
    """Without them the merge back-feeds and a quarter of the events vanish."""
    print("diodes matter")
    real = ww.Board.diode
    try:
        ww.Board.diode = lambda self, path, k: None
        hits, _, _ = circuit_events()
        n = len([x for x in hits["or"] if 216 <= x < 504]) / 4.0
    finally:
        ww.Board.diode = real
    check("OR fires per cycle with the diodes removed", n < 4.0, True)


# ----------------------------------------------------------------- the demo
def test_render():
    """render() is a function of t: same t, same frame, at any --fps."""
    print("render")
    r = ds.build(ww)
    period = ww.PERIOD / ds.options(ww).rate
    check("shape", r(0.0, 0).shape, (64, 320, 3))
    a = r(1.7, 0).copy()
    r(11.0, 1)                                   # seek away and come back
    check("same t gives the same frame", np.array_equal(r(1.7, 999), a), True)
    check("a whole cycle later is the same frame",
          np.array_equal(r(1.7 + period, 0), a), True)

    # 8 fps and 30 fps must land on the same generation at the same wall time.
    def at(fps, seconds):
        rr = ds.build(ww, fps=fps)
        return rr(seconds, int(seconds * fps)).copy()
    check("8 fps and 30 fps agree at t=3.4s",
          np.array_equal(at(8, 3.4), at(30, 3.4)), True)

    for w, h in ((320, 64), (256, 64), (320, 32), (128, 32), (400, 96)):
        rr = ds.build(ww, width=w, height=h)
        f = rr(9.0, 0)
        check("%dx%d renders and is not blank" % (w, h),
              f.shape == (h, w, 3) and bool(f.any()), True)


if __name__ == "__main__":
    for t in (test_rule, test_clock, test_diode, test_gates, test_circuit,
              test_diodes_earn_their_keep, test_render):
        t()
    print("\nall wireworld checks passed")
