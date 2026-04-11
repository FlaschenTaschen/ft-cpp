# Performance Improvement Estimates: UDP Server & RGB Panel Pipeline

## Identified Issues and Proposed Fixes

---

### 1. Frame Tearing / Flickering — Double Buffering (HIGH IMPACT) ✅ IMPLEMENTED

**Root Cause:**  
`RGBMatrixFlaschenTaschen::SetPixel` writes directly to the live framebuffer while the `UpdateThread` continuously reads the same buffer to drive GPIO. Pixel writes race with frame scans, causing partial frames to flash on the panels. `Send()` is currently a no-op.

**Fix (Committed):**  
Added `FrameCanvas *back_buffer_` to `RGBMatrixFlaschenTaschen`. Write all pixels to the back buffer, then call `matrix_->SwapOnVSync(back_buffer_)` in `Send()` to atomically present the completed frame at the next vsync.

Additionally, `CompositeFlaschenTaschen::Send()` was updated to re-render the complete composited scene from the z-buffer and layer screen buffers before each swap. This ensures the back buffer always contains the full composite of all visible layers, eliminating flicker when multiple layers are active.

**Files changed:** 
- `server/led-flaschen-taschen.h`, `server/rgb-matrix-flaschen-taschen.cc` (initial double-buffering)
- `server/composite-flaschen-taschen.cc` (layer composite fix, commit `70f991e`)

**Verified Results (see `DOUBLE_BUFFERING_PERF_ANALYSIS.md`):**
- **Process context switches:** 5x reduction over 3-minute plasma load (3,940 → 785)
- **CPU efficiency:** Similar per-process CPU, achieved with fewer lock/wait cycles
- **Multi-layer rendering:** No flicker when plasma runs on layer 0 and send-text displays on higher layers
- **Stability:** No crashes, clean startup, normal thermal operation

**Performance Achieved:**
- Flickering/tearing: effectively eliminated — panels only update at vsync boundaries, never mid-frame
- CPU: 5x reduction in process context switches under sustained multi-layer load
- Latency: ~1 display refresh period (≈7ms at 140Hz) vsync wait — acceptable for display use
- Multi-layer support: full composite of all visible layers guaranteed before each frame swap

**Confidence:** High — verified on hardware under sustained load with measurable performance metrics.

---

### 2. Per-Pixel Bitplane Encoding on Hot Path (MEDIUM IMPACT)

**Root Cause:**  
Every `SetPixel` call in the compositor chains through to `Framebuffer::SetPixel`, which runs:
1. A CIE1931 luminance lookup (3 table reads)
2. An 11-iteration bitplane loop with read-modify-write on scattered memory

This is called `width * height` times per UDP packet while the global `ft::Mutex` is held. For a 45×35 panel = ~1,575 calls; for a 128×32 matrix = ~4,096 calls per frame.

**Potential fix:**  
Use `FrameCanvas::SetPixels(x, y, width, height, rgb_matrix::Color *colors)` — a bulk rectangle fill that amortizes the function-call overhead. Requires the compositor to accumulate a flat pixel array and call `SetPixels` once in `Send()` instead of per-pixel in `SetPixel`. Involves a virtual method addition to the `FlaschenTaschen` base class interface.

**Estimated improvement:**
- Function call overhead: ~1,575–4,096 virtual dispatch calls reduced to 1 per frame
- Cache behavior: sequential write into a flat array is more L1/L2 cache-friendly than the scattered bitplane buffer writes
- Mutex hold time: essentially unchanged (encoding still happens during `Send()`)
- Rough CPU reduction: 10–25% on the pixel-write hot path — noticeable at high frame rates, marginal at low frame rates

**Confidence:** Medium. The bulk path still does the same CIE1931 + bitplane encoding per pixel internally; the gain is purely from reduced function call and indirect branch overhead. Worth doing as a follow-on after double-buffering is confirmed stable.

---

### 3. Mutex Hold Time During Frame Write (LOW-MEDIUM IMPACT)

**Root Cause:**  
`ft::Mutex` is held for the entire frame: all `SetPixel` calls + `Send()` (which with the fix above will block on `SwapOnVSync` for up to one refresh period). The `LayerGarbageCollector` thread is blocked out for this entire duration.

**Potential fix:**  
Write pixels into a private staging buffer (not the compositor's live `ScreenBuffer`) without the mutex, then acquire it only for the final composite + swap. Requires refactoring the compositor to support a staged-update path.

**Estimated improvement:**
- Mutex contention: reduces lock hold time from `(W*H pixel writes + vsync wait)` to just `(vsync wait)`
- GarbageCollector responsiveness: allows layer expiry to run more promptly
- Frame drop rate: reduced under high packet load

**Confidence:** Low for current setup — `LayerGarbageCollector` runs on a 1-second tick so contention is rarely an issue in practice. Only worth pursuing if packet drop rates are observed under load.

---

### 4. `UpdateThread` Busy-Waiting (CONSTANT CPU DRAIN — HARDWARE TRADEOFF)

**Root Cause:**  
The `UpdateThread` (runs on CPU core 3, SCHED_FIFO priority 99) busy-waits between GPIO frame dumps by default (`allow_busy_waiting_ = true`). This burns 100% of one CPU core continuously.

**Existing mitigation:**  
The flag `--led-no-busy-waiting` switches to `SleepMicroseconds()` between frames, freeing that core for other work. The tradeoff is slightly less precise panel refresh timing, which can itself introduce low-frequency flicker at very high PWM bit depths.

**Estimated improvement:**
- CPU core utilization: ~100% of one core → near 0% between frames with `--led-no-busy-waiting`
- Thermal: significant — this is the primary heat source on the Pi when running panels
- Flickering tradeoff: `--led-no-busy-waiting` may reintroduce subtle timing jitter at high bit depths (kBitPlanes=11). Test at your target `--led-pwm-bits` setting.

**Recommendation:** Try `--led-no-busy-waiting` at `--led-pwm-bits=7` or `--led-pwm-bits=8` as a starting point. The perceived visual difference between 8 and 11 PWM bits is small; the CPU savings are significant.

**Confidence:** High for CPU reduction. Tradeoff with timing precision is real but manageable.

---

### 5. DirectMultiSPI Bit-Banging (SPI CRATE PATH ONLY)

**Applies to:** WS2801 crate-based displays using `ColumnAssembly` / `CrateColumnFlaschenTaschen`. Not applicable to the RGB matrix path.

**Root Cause:**  
`DirectMultiSPI::SendBuffers()` bit-bangs all parallel SPI streams on CPU with no DMA.

**Fix:** Use `CreateDMAMultiSPI()` instead. Zero CPU during transfer; tradeoff is a 1–2 MHz speed cap.

**Estimated improvement:**
- CPU during Send(): ~100% of one core → ~0% with DMA
- Throughput cap: DMA SPI is limited to ~1–2 MHz; for large displays this may reduce max frame rate

---

## Summary Table

| Issue | Impact on Flickering | CPU Reduction | Effort | Priority |
|---|---|---|---|---|
| Double buffering (SwapOnVSync) | Eliminates tearing | None | Low (4 lines) | **Do first** |
| `--led-no-busy-waiting` flag | Minor indirect benefit | ~1 full CPU core | Zero (runtime flag) | **Do second** |
| Bulk SetPixels in compositor | None | 10–25% on write path | Medium (interface change) | Follow-on |
| Reduce mutex hold time | None | Minor | High (refactor) | Only if drops observed |
| DMA SPI (crate path only) | None | High on crate path | Low | If using crate hardware |
