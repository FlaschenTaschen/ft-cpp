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

**Superseded.** Everything in this section was written before any of it had been run on
betelgeuse. It is kept as written, because the guesses it makes are instructive next to what the
hardware actually said — the architecture fork resolved the lucky way, the timings did not.
See [On the hardware](#on-the-hardware-what-betelgeuse-actually-says) below.

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

---

# On the hardware: what betelgeuse actually says

**Everything above was x86 speculation about a Pi. This section is the Pi.** A standalone
PGO+LTO CPython 3.13.15 with numpy 2.5.1 is installed at `/home/pi/modern-python` on betelgeuse,
every demo has been fingerprinted and timed against it, and **the wall has not been cut over** —
that is a decision for a human, and the numbers below are what it should be decided on.

The headline is not what the x86 run predicted. **The whole gain is numpy, and the new
interpreter is a wash.** Total CPU per frame across all 37 demos falls 1.19× on numpy alone and
then goes *back up* 1% when the PGO+LTO 3.13 is added underneath it. If all you want is the
speed, you can have essentially all of it by putting numpy 2.0.2 on the interpreter that is
already there.

## The architecture question, settled

The doubt that dominated "What is not verified" is gone. betelgeuse is a Pi 3 Model B rev 1.2
running Debian 11 bullseye **fully 64-bit**: `aarch64` kernel, `arm64` dpkg architecture,
`linux-aarch64` Python, 64-bit pointers, **glibc 2.31**. So the good branch of that fork is the
live one — `aarch64-unknown-linux-gnu` with pgo+lto exists, and manylinux aarch64 wheels exist
for everything. Nothing was compiled from source; nothing needed to be.

The one thing that had to be checked before anything else was whether a build targeting a much
older glibc would actually *run* on 2.31, and it does. The binary's highest required symbol
version is `GLIBC_2.17`, fourteen years older than what the Pi has, and the interpreter starts,
reports itself correctly, and imports `ssl`, `lzma`, `ctypes`, `sqlite3`, `bz2` and `readline`
without complaint. That was verified by running it, not by reading a support matrix.

## What is installed, and where

Nothing outside `/home/pi/modern-python`. The system Python, the apt-managed `python3-numpy`,
`/home/pi/ft-cpp` and `ftsched.service` are all untouched.

```
/home/pi/modern-python/
├── cpython-3.13.15/install/   the standalone interpreter (bin/python3.13, ~355 MB unstripped)
├── venv-np251/                a venv from it: numpy 2.5.1, Pillow 12.3.0, pip 26.2.1
├── work/                      a copy of demos/ and api/python from the demos/numpy-compat branch
├── work-live/                 a read-only copy of /home/pi/ft-cpp/demos, for the four demos
│                              that exist only in the live installation
├── results/  results-timing/  the JSON this section is built from
└── run-suite.sh run-timing.sh the two runs, as they were actually run
```

To reproduce it from nothing:

```sh
# On a machine with zstd -- the Pi has none, and installing one is not worth it.
curl -sSLO https://github.com/astral-sh/python-build-standalone/releases/download/20260807/\
cpython-3.13.15%2B20260807-aarch64-unknown-linux-gnu-pgo%2Blto-full.tar.zst
tar --zstd -xf cpython-3.13.15*.tar.zst
tar -czf install.tgz -C python install PYTHON.json licenses
scp install.tgz pi@betelgeuse:/home/pi/modern-python/dl/

# On the Pi.
mkdir -p /home/pi/modern-python/cpython-3.13.15
tar -xzf dl/install.tgz -C /home/pi/modern-python/cpython-3.13.15
cd /home/pi/modern-python
./cpython-3.13.15/install/bin/python3 -m venv venv-np251
./venv-np251/bin/pip install --only-binary=:all: numpy==2.5.1 Pillow
```

`--only-binary=:all:` is not decoration. It is the tripwire: if a wheel ever goes missing for
this platform, pip fails loudly instead of quietly starting a numpy build that would take the
rest of the evening at 600 MHz. Both packages installed as manylinux aarch64 wheels and nothing
compiled.

`/home/pi/np126` and `/home/pi/np202` from the earlier work were **left in place**. `np202` is
not dead weight — it is `pip install --target` of numpy 2.0.2 against the stock 3.9, and putting
it on `PYTHONPATH` is exactly the "new numpy, old interpreter" stack that splits the measurement
below in half. It is worth keeping for that reason alone.

## Speed, measured properly this time

Per-frame **CPU** time — `time.process_time()`, not wall — as p50 and p95 over 200 rendered
frames per demo, with the wall paused for the whole run. CPU rather than wall because the Pi is
never idle: `ft-server` pegs a core driving the panels (`isolcpus=3` reserves one for it) and
even a paused `ftsched` costs a quarter of another. p95 rather than a mean because the frame
budget is a deadline per frame, not an average — a demo whose median fits 30 fps and whose 95th
percentile does not is a demo that visibly hitches twice a second.

Three stacks, so the two variables come apart:

| stack | | total CPU p50, 37 demos | vs baseline |
|---|---|---|---|
| **A** | stock py3.9.2 + apt numpy 1.19.5 | 910.9 ms | 1.00× |
| **B** | stock py3.9.2 + numpy 2.0.2 | 766.2 ms | **1.19×** |
| **C** | standalone py3.13.15 pgo+lto + numpy 2.5.1 | 772.5 ms | **1.18×** |

Read the last two rows against each other, because that is the whole finding: **A→B is 1.19×
and B→C is 0.99×.** numpy buys 19%. The interpreter — a PGO+LTO 3.13 replacing a plain-`-O2`
3.9, which is the configuration that ought to be worth 20% on its own — gives it 1% back.
On p95 the picture is the same: A→C is 1.16×.

That is worth sitting with, because it contradicts the reasoning that led here. The argument for
the standalone build was that Debian's 3.9 is unoptimised and that four releases of interpreter
work were sitting on the table. Both premises are true. They just do not matter, because these
demos are not executing much Python: a frame is a couple of dozen numpy calls over 320×64
arrays, and the time goes into numpy's C loops and into `__array_function__` dispatch, neither
of which the interpreter touches. The adaptive specialising interpreter makes bytecode faster
and there is very little bytecode to make faster. Where the interpreter *does* show up is
exactly where you would predict — the demos with real Python-level work per frame: `daliclock`
19.4→17.0, `nyancat` 8.4→7.0, `sneakers` 3.3→2.7, `trench` 12.6→10.7, `mario` 9.8→8.5. It is
just that an equal weight of demos goes the other way, and the sum cancels.

Per demo, sorted by what the full move buys (all values ms of CPU per frame):

| demo | A p50 | A p95 | B p50 | C p50 | C p95 | A→C |
|---|---|---|---|---|---|---|
| splitflap | 3.5 | 9.5 | 0.5 | 0.5 | 3.4 | 7.68× |
| sneakers | 5.7 | 7.7 | 3.3 | 2.7 | 3.8 | 2.09× |
| daliclock | 25.8 | 28.1 | 19.4 | 17.0 | 19.5 | 1.52× |
| knit | 5.7 | 7.6 | 3.5 | 3.8 | 5.5 | 1.51× |
| metaballs | 33.3 | 37.2 | 22.1 | 22.2 | 25.6 | 1.50× |
| fireflies | 75.4 | 85.0 | 50.2 | 51.6 | 62.9 | 1.46× |
| lathe | 38.3 | 44.4 | 28.7 | 26.4 | 33.0 | 1.45× |
| goldengate | 23.0 | 27.7 | 16.9 | 16.7 | 19.7 | 1.38× |
| chladni | 21.9 | 34.4 | 15.4 | 15.9 | 23.5 | 1.38× |
| trench | 14.7 | 24.2 | 12.6 | 10.7 | 16.0 | 1.37× |
| laser | 16.6 | 19.5 | 12.2 | 12.4 | 14.1 | 1.35× |
| boing | 3.2 | 3.9 | 2.6 | 2.4 | 3.4 | 1.30× |
| fireworks | 33.2 | 40.5 | 26.0 | 25.5 | 32.1 | 1.30× |
| nyancat | 9.1 | 10.0 | 8.4 | 7.0 | 8.1 | 1.30× |
| karl | 39.6 | 44.6 | 31.1 | 31.0 | 35.2 | 1.28× |
| sunset | 34.8 | 42.4 | 27.3 | 27.4 | 31.9 | 1.27× |
| water | 66.3 | 72.2 | 51.7 | 52.3 | 57.7 | 1.27× |
| cycle | 3.2 | 3.7 | 2.6 | 2.6 | 3.3 | 1.24× |
| wopr | 5.2 | 6.6 | 4.3 | 4.3 | 5.8 | 1.21× |
| wheel | 16.0 | 18.0 | 12.8 | 13.3 | 14.7 | 1.20× |
| defcon | 9.8 | 11.5 | 8.5 | 8.2 | 10.0 | 1.19× |
| tron | 12.9 | 17.6 | 11.1 | 10.8 | 14.2 | 1.19× |
| esper | 7.2 | 8.9 | 6.0 | 6.2 | 7.8 | 1.16× |
| mario | 9.7 | 10.9 | 9.8 | 8.5 | 9.7 | 1.14× |
| grove | 23.8 | 28.8 | 21.7 | 21.0 | 24.4 | 1.13× |
| voxel | 74.2 | 81.1 | 65.7 | 65.9 | 75.5 | 1.13× |
| starfield | 13.3 | 16.7 | 10.6 | 11.9 | 16.9 | 1.12× |
| headroom | 12.2 | 18.7 | 11.3 | 11.2 | 16.9 | 1.09× |
| printer | 17.7 | 36.5 | 14.1 | 17.0 | 33.3 | 1.04× |
| sort | 5.6 | 6.4 | 5.9 | 5.5 | 6.4 | 1.02× |
| fire | 22.9 | 26.5 | 21.8 | 22.9 | 27.2 | 1.00× |
| rotozoom | 14.5 | 17.5 | 13.6 | 14.6 | 17.6 | 0.99× |
| fsn | 30.6 | 36.1 | 30.1 | 31.1 | 36.5 | 0.98× |
| floor | 11.8 | 12.7 | 12.0 | 12.0 | 13.2 | 0.98× |
| scroller | 22.3 | 23.5 | 22.2 | 23.1 | 24.7 | 0.96× |
| slime | 132.7 | 140.4 | 135.6 | 140.3 | 153.7 | 0.95× |
| tunnel | 15.2 | 16.1 | 14.6 | 16.3 | 17.3 | 0.93× |

`splitflap`'s 7.68× is real but is not a speedup of anything that was costing us: a flap board
spends almost all its frames not animating, and 0.5 ms against 3.5 ms is two numbers that are
both far inside any budget. `sneakers` at 2.09× is the honest star.

**Six demos got slower**, and this is the part that would have been invisible in a mean:
`tunnel` 0.93×, `slime` 0.95×, `scroller` 0.96×, `floor` and `fsn` 0.98×, `rotozoom` 0.99×. None
is dramatic, none crosses a frame budget downward except as noted below, but "nothing regressed"
— which was true on x86 — is **not** true here. `slime` is the one to watch: it was already the
most expensive thing in the rotation at 8 fps, and it is now 5% worse.

Every number here is a **600 MHz number.** The Pi is under-voltage throttled to half its rated
1200 MHz (`vcgencmd get_throttled` → `0x50005`, and not thermal — it sat at 61–64 °C throughout).
Fixing the supply is worth more than everything in this document put together, and would change
all of these figures by roughly a factor of two.

## Frame-rate brackets: what would actually change on the wall

`ftsched.py` sets each demo's fps to leave "roughly 40% headroom over the measured cost", so a
demo fits a rate when its p95 render time is under 60% of the frame period — `fps ≤ 600/p95`.

The harness's *absolute* p95 runs consistently higher than the `ms` values recorded in
`ROTATION` (the harness measured `daliclock` at 28.1 ms against a recorded 20.1, `water` at 72.2
against 45.8). The harness steps every demo at a fixed 20 fps regardless of the rate the
rotation runs it at, and it measures under whatever contention the box has; the recorded numbers
were taken differently. So the absolute brackets it computes are pessimistic and would tell you,
wrongly, that half the rotation is already over budget. **The ratio is what transfers, not the
level.** Applying the measured per-demo A→C ratio to the rotation's own recorded p95:

| demo | runs at | recorded p95 | scaled p95 | bracket now → after |
|---|---|---|---|---|
| daliclock | 30 | 20.1 | 14.0 | 24 → **40** |
| goldengate | 30 | 19.2 | 13.7 | 30 → **40** |
| headroom | 30 | 16.0 | 14.5 | 30 → **40** |
| tron | 30 | 16.1 | 13.0 | 30 → **40** |
| wheel | 40 | 13.1 | 10.7 | 40 → **50** |
| metaballs | 24 | 29.0 | 20.0 | 20 → **30** |
| trench | 24 | 29.2 | 19.3 | 20 → **30** |
| laser | 40 | 11.9 | 8.6 | 50 → **60** |
| esper | 30 | 10.9 | 9.5 | 50 → **60** |
| karl | 24 | 32.7 | 25.9 | 12 → 20 |
| fireworks | 24 | 33.7 | 26.7 | 12 → 20 |
| defcon | 30 | 25.7 | 22.3 | 20 → 24 |
| fireflies | 12 | 61.3 | 45.3 | 8 → **12** |

Seven of those are demos the rotation could actually be stepped up a rung: **daliclock,
goldengate, headroom and tron from 30 to 40; wheel from 40 to 50; metaballs and trench from 24
to 30.** `laser` and `esper` gain bracket but are already run below it for other reasons.
`karl`, `fireworks` and `defcon` improve without reaching what they are already set to, which is
worth knowing in the other direction: those three are being run *above* their headroom rule
today, and the upgrade narrows rather than closes that gap.

`fireflies` is the nicest result on this list. At 61.3 ms it did not fit its own 12 fps slot
under the 40% rule; at 45.3 ms it does. It stops being an exception.

Two brackets **fall**: `scroller` 50 → 40 and `starfield` 60 → 50. Both land exactly on the rate
the rotation already runs them at, so neither forces a change, but neither has any headroom left
to give.

None of this is a recommendation to raise any rate. Raising fps costs `ft-server` bandwidth and
the transition blender pays for pairs, not singles — `pair_check()` exists for that reason. It
is a statement of what the budget would allow.

## Compatibility on real hardware

All 37 demos import, build and render under all three stacks. No `IMPORT_FAIL`, `BUILD_FAIL`,
`RENDER_FAIL`, `CRASH` or `TIMEOUT` anywhere; nothing rendered black; nothing froze on one
image; and all 37 were self-stable under both A and C, so every one got a real frame-by-frame
comparison rather than the smoke-test fallback. The four demos that exist only in the live
installation and not on this branch — `console`, `life`, `maze`, `pixelart` — were run too, from
a scratch copy of `/home/pi/ft-cpp/demos` so that nothing wrote into the live tree.

Nine of 37 differ from the 1.19.5 baseline, plus `console` from the live-only set. **The
important discovery is that they split into two causes, and only one of them is numpy.**

Running the same interpreter and the same system Pillow with only numpy changed (stack A → stack
B) isolates it:

| demo | A→B, numpy only | A→C, numpy + interpreter + Pillow | cause |
|---|---|---|---|
| chladni | 10/10 frames, Δ111, 1547 px | same | **numpy** |
| slime | 10/10, Δ255, 11889 px | same | **numpy** |
| fireflies | 3/10, Δ1, 3 px | same | **numpy**, dither LSB |
| goldengate | 2/10, Δ1, 3 px | same | **numpy**, dither LSB |
| water | 6/10, Δ1, 38 px | same | **numpy**, dither LSB |
| headroom | 8/10, Δ2, 33 px | 10/10, Δ232, 1791 px | numpy dither + **Pillow** |
| scroller | **identical** | 9/10, Δ248, 39410 px | **Pillow** |
| wopr | **identical** | 10/10, Δ255, 22081 px | **Pillow** |
| console | **identical** | 10/10, Δ255, 21967 px | **Pillow** |

### The Pillow half, which nobody was looking for

The venv brings its own Pillow — 12.3.0, with its own bundled FreeType — and the stock
interpreter uses Debian's Pillow 8.1.2 against system FreeType 2.10.4. Every demo that
rasterises a TrueType face through `ImageFont.truetype` therefore draws different glyphs, and
`scroller`, `wopr`, `console` and most of `headroom`'s delta are that and nothing else. The
demos are innocent; the numbers above prove it, because with numpy alone changed they are
bit-identical.

Looked at as pixels: same string, same font file, same position, same layout, **visibly heavier
stems and different hinting.** `wopr`'s "GREETINGS PROFESSOR FALKEN" is legible and correct and
slightly bolder. This is a real, visible change on the wall, it is not a bug, and no amount of
numpy testing would ever have found it. It is the strongest argument in this document for
looking at rendered frames instead of trusting a status column — a text demo that silently
changes typeface is exactly the class of thing this codebase fails at.

If matching the current look exactly matters more than having a current Pillow, `pip install
"Pillow==8.1.2"` into the venv is not viable (no aarch64 wheel that old for 3.13), so the real
options are to accept the new rasterisation or to keep the stock interpreter — which is another
point in favour of the stack-B-only option.

### The numpy half

`chladni` and `slime` are the two with large pixel counts, and both were looked at side by side
against the baseline rather than judged from the diff statistics.

`chladni` is the same finding as x86: identical nodal figure, sand grains settling in adjacent
cells along the same lines. The Δ111 is one bright grain against a dark plate.

`slime` is new — on x86 it was five pixels, here it is 11889 across every frame. It is also, on
inspection, **the same slime**: the same filament network, the same junctions, the same
topology, differing by one-pixel jitter along the edges of thin bright strands, which registers
as Δ255 because a filament edge is either lit or not. The cause is almost certainly that numpy
2.x has NEON-vectorised float32 `sin`/`cos` loops on aarch64 that 1.19.5 does not, so the
per-agent heading arithmetic differs in the last bit and a chaotic system amplifies it. That
class of difference is what "ARM's libm differs" was always going to mean; it just lands harder
on a physarum simulation than on a plasma.

The caveat worth stating: this was checked over 10 seconds of demo time, and `slime` gets 60
seconds in the rotation. A chaotic system given six times as long may diverge further. It will
still be slime — it cannot render anything but slime — but it will not be *the same* slime, and
if that matters the check is `--frames 60 --only slime`.

`fireflies`, `goldengate` and `water` are one or two pixels at Δ1, which is dithering, and are
not worth further words.

**Nothing rendered a structurally different picture.** Every difference above is either a
rasterisation change with a known cause or last-bit arithmetic in a chaotic system.

### `lathe`, on hardware

`lathe` is `IDENTICAL` across all three stacks on the Pi. The NEP 50 fix in this branch holds on
ARM exactly as it did on x86, which is what one would expect of a fix that consists of writing
`float()` where a float was meant, but is worth having confirmed on the machine that matters.

## What a cutover would look like

**This has not been applied.** `ftsched.service` on betelgeuse is untouched and the wall is
running on stock Python 3.9.2 and numpy 1.19.5, exactly as it was.

The change is one line. In `/etc/systemd/system/ftsched.service`:

```diff
-ExecStart=/usr/bin/python3 /home/pi/ft-cpp/demos/ftsched.py \
+ExecStart=/home/pi/modern-python/venv-np251/bin/python /home/pi/ft-cpp/demos/ftsched.py \
     --host localhost --fps 20 --quiet \
     --listen 127.0.0.1:8081 \
     --rotation /home/pi/ft-cpp/demos/rotation-betelgeuse.json \
     --state-file /var/lib/ftsched/state.json
```

then

```sh
sudo systemctl daemon-reload
sudo systemctl restart ftsched.service
```

Four things about that line were checked rather than assumed:

- **`ProtectHome=read-only` does not block it.** The unit already reads its own checkout out of
  `/home/pi`; an interpreter there is read the same way. The venv never needs to write, and
  `__pycache__` writes into `/home/pi/ft-cpp/demos` are already silently refused today.
- **`MemoryMax=512M` is not at risk.** Resident set after `import numpy` goes from 24.4 MB on
  the stock stack to 28.7 MB on the new one, against a live `ftsched` that sits around 58 MB.
- **`ftsched` itself runs on 3.13.** `ftsched`, `ftsched_web`, `ftsched_opts`, `demoscene`,
  `megademo` and the four live-only demos all import cleanly under the venv, and
  `demoscene.Blender` — the preallocating transition blender that exists only in the deployed
  copy and not on this branch — produces correct frames from all four of its transitions. No
  removed stdlib module is used anywhere: no `distutils`, no `cgi`, no `imp`.
- **The venv's `bin/python` is the right thing to name**, not the interpreter under
  `cpython-3.13.15/install/bin`, which would not see numpy at all.

Rolling back is the same edit in reverse — put `/usr/bin/python3` back, `daemon-reload`,
`restart` — and it is complete, because nothing in the cutover modifies the demos, the rotation,
the state file or the apt-managed numpy. The rollback target is the stack that is running right
now, and `lathe`'s fix is verified bit-identical on it, so there is nothing that only works
forwards.

**If the Pillow change is unwelcome, there is a second option that keeps essentially all of
the speed:** point
`ExecStart` at the stock interpreter with numpy 2.0.2 in front of it, which is stack B —
measurably indistinguishable from stack C in aggregate, and leaves text rendering exactly as it
is today.

```
Environment=PYTHONPATH=/home/pi/np202
ExecStart=/usr/bin/python3 /home/pi/ft-cpp/demos/ftsched.py …unchanged…
```

That is a smaller change with a smaller blast radius and nearly all of the benefit. Its cost is
that it pins the wall to a Python 3.9 that bullseye will stop patching, and to a numpy installed
by `pip --target` rather than into a venv, which is a less tidy thing to inherit.

## What is still not verified

- **Nothing has been run through `ftsched` itself on the new interpreter** — every measurement
  here calls `build()` and `render()` directly, with no server, no socket and no transitions.
  The blender was exercised by hand and the modules import, but a full rotation has never
  actually played on 3.13. That is the obvious next step and it needs the cutover.
- **Timing runs are one pass of 200 frames per demo per stack**, not repeated trials, so the
  small numbers (anything inside ±5%) should be read as noise. The six regressions are all in
  that band except `tunnel` and `slime`.
- **Compatibility hashes cover 10 seconds of demo time**, not the 60 used on x86, chosen to keep
  the wall paused for half an hour rather than two. Late-cycle behaviour in `esper`, `defcon`
  and `slime` is correspondingly less well covered.
- **Everything is a 600 MHz measurement.** Whether the ranking survives at 1200 MHz is unknown;
  memory-bound demos would gain less from a clock increase than compute-bound ones, so the
  *shape* of the table would likely change even if the total did not.
- **`demos/voxel.py` remains untouched** and is `IDENTICAL` on hardware as it was on x86.
- The `--frames 60 --only slime` check described above has not been run.
