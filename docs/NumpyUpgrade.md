# Moving the demo wall off numpy 1.19.5

**All 37 Python demos run unmodified on numpy 2.5.1 under CPython 3.13, and after one
one-line-class fix in `lathe.py` they render frames that are byte-identical to numpy 2.0.2 and
visually identical to the deployed 1.19.5 baseline. Nothing crashes, nothing renders the wrong
picture, and per-call numpy overhead — the thing the Pi profiling actually blamed — drops 2.5–4.6×.**

Measured locally on x86_64. Nothing in this document has been run on the Pi; see
[What is not verified](#what-is-not-verified) at the end, which is the part that matters.

## Why this came up

The wall's Pi 3 runs Debian 11 bullseye: Python 3.9.2 and `python3-numpy 1:1.19.5-1`. apt's
candidate is the same version, so that installation will sit there until somebody moves it
deliberately. Profiling on the Pi found roughly **6 ms of every frame going into
`__array_function__` dispatch** across about forty dispatched calls — a numpy call costing
55–80 µs *regardless of array size*, which is the signature of protocol overhead rather than
arithmetic. At 320×64 these demos make a lot of small calls, so that overhead is not a rounding
error; it is a large share of a frame at 600 MHz.

Dispatch got substantially cheaper after 1.19 (`__array_function__` moved into C in 1.21, and
the dispatch path has been shaved repeatedly since). So there is real performance on the table.
The question was never whether newer numpy is faster. It was **what breaks**.

## What was tested, and how

`demos/scripts/numpy-compat.py` walks every demoscene module, calls `build()` with the module's
own defaults, steps `render(t, i)` at a fixed 20 fps for sixty seconds of demo time, and hashes
one frame a second. No server, no display, no network — `build()` and `render()` are just
functions. Each demo runs in its own subprocess, so a demo that segfaults an old numpy costs us
that demo rather than the run.

The methodological point that the whole exercise rests on is **self-stability**. A demo with an
unseeded RNG or a clock read differs from *itself* between two runs, never mind between two
numpys, and reporting that as "changed by numpy" would be noise dressed up as a finding. So
every demo is run twice, in two separate processes, under the *same* interpreter and numpy, and
marked self-stable or not before anything is compared across versions. Three things pull demos
into the comparable set:

- **Seeds.** 28 of the 37 demos take `--seed`; the harness sets one.
- **A pinned clock.** `daliclock` and `splitflap` put the time of day on the wall. The harness
  patches `time.time`/`localtime`/`datetime.now` before the demo is imported, to a fixed epoch
  that *advances with `t`* rather than stopping. Stopping it is simpler and useless — a clock
  demo driven by a stopped clock renders the same image sixty times and the comparison then
  covers one frame. (This was a real bug in the harness's first draft; `daliclock` was reported
  as "identical across all versions" when what it had actually shown was one still.)
- **`PYTHONHASHSEED=0`**, so dict and set iteration order is not a variable.

With those three, **all 37 demos are self-stable under every version tested**, so every demo got
a real frame-by-frame comparison and none had to fall back to the smoke-test path. The
smoke-test path exists anyway (`RUNS_ONLY`: ran, lit pixels, did not freeze on one image),
because a demo that quietly starts returning a black rectangle at 60 fps looks perfectly healthy
from the outside, and that is precisely how this codebase fails.

## The compatibility matrix

Nothing failed to import, build, or render anywhere. Twenty-eight of thirty-seven demos are
byte-identical from 1.19.5 to 2.5.1. The nine that are not:

| demo | 1.26.4 | 2.0.2 | 3.11+2.4.6 | 3.12+2.5.1 | 3.13+2.5.1 | verdict |
|---|---|---|---|---|---|---|
| chladni | 34f, Δ110, 433px | same as 1.26.4 | ″ | ″ | ″ | grains land in different cells |
| headroom | 3f, Δ13, 3px | 46f, Δ19, 345px | ″ | ″ | ″ | invisible; dither LSB |
| wheel | 24f, Δ2, 36px | 25f, Δ127, 38px | ″ | ″ | ″ | two antialiased spoke pixels |
| laser | — | 6f, Δ255, 19px | ″ | ″ | ″ | four spark pixels on/off |
| goldengate | 2f, Δ1, 2px | 9f, Δ1, 13px | ″ | ″ | ″ | dither LSB |
| slime | 1f, Δ2, 1px | 5f, Δ2, 5px | ″ | ″ | ″ | dither LSB |
| grove | — | 4f, Δ1, 4px | ″ | ″ | ″ | dither LSB |
| fireflies | — | 1f, Δ1, 1px | ″ | ″ | ″ | dither LSB |
| sunset | 1f, Δ1, 1px | 1f, Δ1, 1px | ″ | ″ | ″ | dither LSB |
| **lathe** | — | **59f, Δ241, 230212px** | — | — | — | **real; fixed, see below** |

`Nf, ΔD, Ppx` = N of 60 sampled frames differ, worst per-channel delta D, P pixels differ in
total across the whole run. For scale, one run is 60 × 20480 = 1.23 M pixels; "13px" means
thirteen of them.

Every one of these was looked at as pixels, side by side with the baseline and with a
difference map, not judged from shapes and dtypes. The five demos in the Δ1–2 band differ by one
or two pixels by one or two levels and are not worth further words. `chladni`, `headroom`,
`wheel` and `laser` have larger *deltas* but on a handful of pixels each: a grain of sand in an
adjacent cell, a spark that exists in one run and not the other, one pixel on the edge of a
spoke. The pictures are the same pictures.

The last row is the one that mattered, and it is fixed.

### Two independent causes, only one of them ours

**Last-bit changes in numpy's own kernels.** Between 1.19.5 and 2.5.1, float64 `np.sin` changed
in 1.22 and changed back by 1.26, `np.exp` changed in 1.26 and again in 2.5, `x ** 0.72` changed
in 1.26, and the unstable sort breaks ties in a different order. (The float32 transcendentals,
which is where one would look first, are bit-stable across the whole range.) All sub-ULP or
tie-order. Nothing can be done
about these and nothing should be: a demo whose output depends on the last bit of `sin` is a
demo whose output was already arbitrary. This accounts for everything that appears at the
1.26.4 column.

**NEP 50, in numpy 2.0.** Value-based promotion is gone. The rule that changed here is narrow
and easy to miss: in numpy 1.x, an expression mixing a *Python* float with a **numpy scalar** —
not an array — promoted to float64, because value-based casting only ever applied when one
operand was an array. `np.float32(3.0) * 1.6` was `np.float64`. Under NEP 50 the Python float is
weak and the result stays float32. Nothing raises, nothing warns, the arithmetic silently loses
sixteen bits of mantissa.

That this is exactly what happened was confirmed by re-running the whole suite under
`NPY_PROMOTION_STATE=legacy` on numpy 2.0.2, which reproduced the 1.26.4 results exactly. Every
2.0-specific difference in the table is NEP 50 and nothing else.

### The one real breakage: `lathe`

`lathe` renders a woodturning lathe: a gouge walks the length of a spinning blank, and the
toolpath position is accumulated across frames and then rounded to a column. Its feed rate was
an `np.float32` scalar, so `dt * v` was float64 in numpy 1.x and float32 in 2.0. Losing that
precision made a sweep land a column early or late, which changed how many chatter samples got
drawn from the RNG, which desynchronised the generator, which turned out **a different piece of
wood**. 59 of 60 frames, 230 thousand pixels. It never raised and it never looked broken — it
looked like a lathe turning a different blank, which is exactly the failure mode this codebase
specialises in and exactly why the harness compares frames rather than checking that things ran.

The fix is to say what was meant: the toolpath and the gouge geometry are plain Python floats,
so they are float64 on every numpy. Four sites, all in `demos/lathe.py`:

```python
v = float(feed if kind == "cut" else feed * f32(2.6))   # toolpath feed rate
contact = 1.6 * float(sc)                               # gouge contact width
st["tool"] += float((want - st["tool"]) * (1.0 - np.exp(-dt / 0.20)))
_draw_gouge(frame, gx, gy + 1.0 + 7.0 * float(sc) * st["tool"], float(sc), ...)
```

The third is a variant of the same thing from the other direction: `np.exp()` of a Python float
returns a numpy `float64` *scalar*, and a numpy float64 mixed with a float32 array promotes
differently before and after NEP 50, so the gouge came out a different colour. `float()` on the
step keeps it a Python float and both numpys agree.

Verified both ways: with the fix, 1.19.5 output is **bit-identical to what it was before the
fix**, and 2.0.2 is bit-identical to 1.19.5. A fix that only worked on 2.x would have been a
regression, since 1.19.5 is what is deployed and what a rollback lands on.

### What did *not* break

Worth stating, because it is the reason this is a short document. A grep across all 44 demo
files, `demoscene.py`, and `api/python/` for every alias numpy 2.0 removed — `np.float_`,
`np.unicode_`, `np.NaN`/`np.NAN`, `np.in1d`, `np.alltrue`, `np.product`, `np.round_`,
`np.cumproduct`, `np.msort`, `np.row_stack`, bare `np.float`/`np.int`/`np.bool`/`np.object`,
`np.find_common_type` — returns **nothing**. Nor does anything rely on `np.array(copy=False)`
never copying. These demos were written recently against a modern idiom, and the numpy 2.0
migration cost for them is zero.

## Which version, and on what interpreter

Python 3.9 caps numpy at 2.0.2 (2.1 requires ≥3.10), and that was the assumed ceiling until it
became clear the Pi does not have to keep its distro interpreter. `astral-sh/python-build-standalone`
publishes prebuilt CPython for ARM, and Debian built the Pi's 3.9 with plain `-O2`, no PGO and no
LTO, so a standalone build is likely faster *before* any numpy change.

**Recommendation: a standalone CPython 3.12 or 3.13 with the current numpy 2.x.**

The compatibility argument for it is strong and slightly surprising: **numpy 2.0.2, 2.4.6 and
2.5.1, on Python 3.9, 3.11, 3.12 and 3.13, produce byte-identical frames for all 37 demos.** Not
"similar" — identical hashes, all 2220 sampled frames. Whatever risk exists in this upgrade is
entirely in the 1.x → 2.0 step, which is characterised above and consists of one demo, now
fixed. Going on past 2.0.2 to 2.5.1, and past 3.9 to 3.13, costs nothing in compatibility. So
there is no reason to accept 3.9's ceiling in exchange for a safety that turns out not to exist.

If the standalone interpreter turns out to be impractical on the Pi (see below), the fallback is
**stock Python 3.9 + numpy 2.0.2**, which by the same measurements is pixel-identical to the
recommendation. That is a genuinely comfortable position: the two candidate destinations differ
in speed, not in behaviour, so the decision can be made on whatever the hardware says without
re-doing any of this work.

## Speed, coarsely

Performance is not the acceptance criterion here — the demos need to fit their frame budget, not
to win a benchmark — so these are single rough runs, not careful benchmarks, and x86 timings are
only *indicative* of the Pi. The measured desktop-to-Pi ratio on this workload is 76–114×, and
the Pi is currently under-voltage throttled to 600 MHz against a rated 1200, so the point of
timing locally is ranking options, not predicting frame rates.

Sum of mean per-frame render time across all 37 demos:

| stack | total | vs baseline |
|---|---|---|
| py3.9 + numpy 1.19.5 | 11.9 ms | 1.00× |
| py3.9 + numpy 1.26.4 | 10.5 ms | 1.13× |
| py3.9 + numpy 2.0.2 | 10.5 ms | 1.13× |
| py3.11 + numpy 2.4.6 | 9.7 ms | 1.23× |
| py3.12 + numpy 2.5.1 | 9.9 ms | 1.20× |
| py3.13 + numpy 2.5.1 | 9.4 ms | 1.27× |

**Nothing regressed.** No demo got dramatically slower on any rung, which is the only thing this
table needed to establish.

The interesting number is elsewhere. Per-call overhead on an 8×8 array, where the arithmetic is
free and what is being measured is the dispatch machinery:

| call | 1.19.5 | 2.5.1 (3.13) | |
|---|---|---|---|
| `np.clip` | 6.60 µs | 1.43 µs | 4.6× |
| `np.stack` | 2.88 µs | 1.25 µs | 2.3× |
| `np.take` | 1.86 µs | 0.73 µs | 2.5× |
| `np.where` | 1.36 µs | 1.07 µs | 1.3× |
| `a + b` (ufunc, never dispatched) | 0.29 µs | 0.22 µs | 1.3× |

That is the shape the Pi profiling described: the ufunc path barely moves, and the dispatched
functions get 2–5× cheaper. The demos only gain 1.27× locally because on a desktop the arrays
are big enough that real work dominates the fixed cost. On the Pi, where a numpy call was
measured at 55–80 µs *independent of array size*, the fixed cost is most of the bill, and the
share of the frame this recovers should be much larger. **How much larger is a guess until it is
measured on the hardware.**

## Regression procedure after any change

This is meant to be re-run, not admired once.

```sh
# one venv per stack under test
python3.9 -m venv .venvs/py39-np1195 && .venvs/py39-np1195/bin/pip install numpy==1.19.5 Pillow
python3.13 -m venv .venvs/py313      && .venvs/py313/bin/pip install numpy Pillow

# fingerprint each, sixty seconds of demo time per demo
for v in py39-np1195 py313; do
  .venvs/$v/bin/python demos/scripts/numpy-compat.py \
      --python .venvs/$v/bin/python --frames 60 --out .compat/$v.json
done

# the matrix
python3 demos/scripts/numpy-compat.py --compare .compat/py39-np1195.json .compat/py313.json
```

Read it in this order:

1. **Any status other than `IDENTICAL`, `DIFFERS` or `RUNS_ONLY` is a stop.** `IMPORT_FAIL`,
   `BUILD_FAIL`, `RENDER_FAIL`, `CRASH`, `TIMEOUT` all carry the traceback's last line.
2. **`SMOKE_FAIL`** means a demo that could not be compared frame-for-frame rendered black, or
   rendered one image for the whole run. Treat as a failure.
3. **`DIFFERS` is not automatically a failure and is never automatically a pass.** Look at the
   pixels: `--dump-frames /tmp/png --only <demo>` under each stack, and compare the PNGs. A few
   pixels at Δ1–2 is dithering and is fine. A demo where a large fraction of frames differ over
   a large area — the `lathe` signature — is a real change and needs the cause found before it
   is accepted.
4. `NPY_PROMOTION_STATE=weak_and_warn` on numpy 2.x is the fastest way to find the cause of a
   `DIFFERS` on the 1.x → 2.x step. It reports, per source line, where a result dtype changed
   because value-based promotion went away. That is how the four `lathe` sites were found in
   about a minute, having spent considerably longer than that guessing beforehand.
5. Any fix must be verified **on both ends of the range**: 1.19.5 output unchanged by the fix,
   *and* the new numpy now matching it. The deployment may sit on 1.19.5 for a while yet, and a
   rollback has to land somewhere that works.

The whole suite takes about a minute per stack on a 24-thread desktop. It is cheap enough to run
on every change to a demo, not only on upgrades.

## What is not verified

Everything below can only be settled on the hardware, and none of it was touched here.

- **Nothing was run on the Pi.** Every number in this document is x86_64. The compatibility
  findings should carry over — they are about numpy semantics, not about the CPU — but the
  timings do not, and ARM's libm differs from glibc's on x86, so the sub-ULP class of
  differences may land on a different, similarly harmless, set of pixels there.
- **The Pi's architecture is unconfirmed, and it decides the whole plan.**
  `python-build-standalone` ships `aarch64-unknown-linux-gnu` with **pgo+lto**, but for 32-bit
  `armv7-unknown-linux-gnueabihf` the best available is **lto only, no PGO**. Bullseye on a Pi 3
  is very often the 32-bit armhf image. If it is, the interpreter half of the win is
  materially smaller than assumed.
- **numpy wheels for 32-bit ARM do not exist on PyPI.** aarch64 has manylinux wheels for every
  version tested here. `linux_armv7l` has none, so a 32-bit Pi means either piwheels or building
  numpy from source on a 600 MHz Pi 3 — which is a long afternoon, and a different plan.
- **The under-voltage throttling is unaddressed** and is worth more than any of this. A Pi
  pegged at 600 MHz against a rated 1200 is giving up a factor of two before a line of Python
  runs. Fixing the supply should probably precede the upgrade, if only so the upgrade's effect
  can be measured against a stable baseline.
- **`demos/voxel.py` was tested but not modified**, being owned by other work at the time. It
  reports `IDENTICAL` on every version tested, so it needs no numpy fix — but if it changes,
  re-run the harness against it.
- The C++ demos under `demos/src/` are out of scope; they do not use numpy.
- 60 seconds of demo time per demo covers a full cycle for most of these but not for all —
  `esper` and `defcon` in particular run longer arcs. Raise `--frames` if a demo's late
  behaviour is suspected.
