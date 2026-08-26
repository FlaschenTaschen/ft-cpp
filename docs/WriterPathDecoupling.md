# Getting the re-render and the vsync wait off the writer path

Follow-up to the double-buffering work in `0c517b4` / `a238529` / `70f991e` and to
`PerformanceImprovements.md`.

**Measured on hardware: 73–75% UDP datagram loss under two concurrent writers → 0.0%.**

## The problem

On a 320×64 display (5 × 64×64 chained, Raspberry Pi 3), two writers pushing 60 fps each on
separate layers caused the server to drop three quarters of incoming datagrams before ever
reading them. Clients reported zero send errors, so the loss was entirely server-side
backpressure.

Frame intake measured **~33 presents/sec against a ~64 Hz refresh** — almost exactly
`refresh / 2`. That ratio reproduced across refresh rates and across runs, and it points at two
defects that compound:

**1. `CompositeFlaschenTaschen::Send()` re-rendered the entire composite per packet.** All 20,480
pixels from the z-buffer through `FrameCanvas::SetPixel` — CIE1931 lookup plus an 11-iteration
bitplane read-modify-write on scattered memory. `70f991e` estimated this at ~10 ms on a Pi 3 and
that estimate was accurate; the frame period at 71 Hz is 14 ms.

**2. `SwapOnVSync()` blocked while holding the global writer mutex.** It is called from
`udp-server.cc` between `mutex->Lock()` and `mutex->Unlock()`. While it waits for the refresh
boundary the single UDP thread cannot call `recvfrom()`, so the socket buffer overflows and the
kernel discards whole frames.

Together: a re-render that overruns one frame period makes the swap miss the next vsync and land
on the one after — two frame periods per packet, hence `intake ≈ refresh / 2`.

`70f991e` was verified with *"plasma on layer 0 + send-text on higher layers"*. `send-text` is
essentially static, so sustained concurrent writer load was never exercised.

## The fix

### Commit 1 — `CopyFrom` instead of re-rendering

`70f991e` was protecting a real invariant: `SwapOnVSync()` returns the previously-displayed
canvas, which is two frames stale, and FlaschenTaschen applies *incremental* updates, so writes
would land on stale content. Keep the invariant, change how it is maintained:

```cpp
void RGBMatrixFlaschenTaschen::Send() {
    rgb_matrix::FrameCanvas *previous = matrix_->SwapOnVSync(back_buffer_);
    previous->CopyFrom(*back_buffer_);   // resync the spare to what is displayed
    back_buffer_ = previous;
}
```

`FrameCanvas::CopyFrom()` is `memcpy(bitplane_buffer_, other->bitplane_buffer_, buffer_size_)` —
~450 KiB, well under a millisecond, versus 20,480 gamma lookups and bitplane scatters. Reading a
canvas while the refresh thread scans it is safe: nothing writes to the on-screen canvas.

### Commit 2 — present on a dedicated thread

Writers must never touch a `FrameCanvas`; moving the swap to another thread while they did would
relocate the race rather than remove it. The backend now keeps a plain framebuffer as source of
truth:

```
  writer thread (mutex already held by udp-server):
    SetPixel  → fb_[y*w+x] = c; extend dirty rect
    Send      → frame_ready_ = true; signal            // O(1), never blocks

  pusher thread:
    lock; wait for frame_ready_; blit dirty region into back_buffer_; unlock
    SwapOnVSync(); CopyFrom()                          // vsync wait, lock RELEASED
```

Updates arriving faster than the panel can present coalesce in `fb_` instead of applying
backpressure to the socket. Shutdown mirrors `LayerGarbageCollector`
(`TriggerExit()` / `WaitStopped()`), with an explicit `StopDisplayThread()` after
`udp_server_run_blocking()` returns.

## Results

Two writers, 60 fps each, on separate layers writing different row ranges:

| | intake | **drop** | refresh worst |
|---|---|---|---|
| before | 33.4/s | **75.2%** | 32.4 Hz |
| commit 1 | 60.1/s | 51.1% | 36.6 Hz |
| **commit 1 + 2** | **121.7/s** | **0.0%** | **38.0 Hz** |

Intake went `refresh/2 → refresh → unbound`, matching the predicted mechanism at each stage.
121.7/s against a 57.7 Hz refresh is 2.1× refresh — genuinely decoupled. At 240 fps offered,
intake reaches 160.6/s and the ceiling becomes CPU in the parse + composite path rather than the
panel; `FrameCanvas::SetPixels` bulk fills (`PerformanceImprovements.md` item 2) would be the next
lever. Refresh jitter did not regress — worst-case frame time improved at every stage.

## Validation

Throughput cannot detect a correctness regression, so both were checked.

- **Strobe probe.** One writer, two packets per logical frame: top half on layer 1, bottom half on
  layer 2, in strict antiphase at 4 Hz. Correct rendering shows exactly one half bright at any
  instant; a stale offscreen canvas produces frames where both are bright or both dark. An
  absolute criterion requiring no reference. Both commits passed.
- **Blind A/B for smoothness.** Both binaries present at the same rate; the difference is that one
  discards ~75% of packets arbitrarily in the kernel while the other receives all of them and
  decimates evenly. Presented unlabelled with the expected winner shown first, so recency bias cut
  against it. The fixed binary was chosen decisively.
- **Soak.** Single long-lived process, 16 cycles of 240 s load (2 × 60 fps) + 60 s idle over
  1h15m: RSS flat at 5904 kB with zero drift, thread count 4, fd count 4, and **zero drops in
  every cycle** — 460,800 datagrams delivered without loss.
- **Lifecycle.** Clean start, survives traffic, exits properly on SIGTERM, no build warnings.

## Related findings, not addressed here

- **`--led-limit-refresh` above the achievable rate is inert.** This panel reaches ~71 Hz at
  `--led-slowdown-gpio=2`, so a limit of 120 never engages (`limit=120` and `limit=0` measure
  identically). `--led-slowdown-gpio=1` raises the ceiling to ~82 Hz with no ghosting observed on a
  5-panel chain; a limit set just under the achievable rate then engages and pins the period.
  Avoid pinning near 60 Hz — it beats against 120 Hz ambient from 60 Hz mains and produces a
  rolling horizontal band.
- **Perceived flicker tracks worst-case frame time, not average jitter.** A configuration with the
  best standard deviation and fewest hitches still read as flickery because its worst-case frame
  was unchanged. `--led-limit-refresh` can only pad frames that finish early; it cannot shorten one
  that overran. Headroom is the lever.
- **Layer GC** still sweeps the whole display per expired layer while holding the writer mutex,
  `x`-outer / `y`-inner against row-major layout.
- **`SO_SNDBUF`-derived `max_udp_size_`** can exceed the 65507 datagram limit (typically 212992 on
  Linux). Harmless where a frame fits one datagram; `EMSGSIZE` on wider displays. Wants a clamp.
- **No fragment marker in the protocol.** `Send()` runs once per datagram, so a chunked frame is
  presented once per chunk. Cannot trigger at 320×64 (a frame fits one datagram) but matters for
  larger displays.
