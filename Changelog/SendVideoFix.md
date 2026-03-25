# send-video Build Fix

## Status

✅ **FIXED** - All changes applied and verified (2025-03-25)
- `send-video` now builds successfully with FFmpeg 8.1
- Added to default `make client` target
- Produces 39K executable binary

## Problem

`send-video` fails to build with FFmpeg 8.1 due to API compatibility issues.

### Errors

1. **`TIMER_ABSTIME` undefined** (line 342)
   - macOS doesn't define this Linux constant
   - Used in `clock_nanosleep()` call

2. **`avcodec_close()` removed** (line 361)
   - Deprecated and removed in modern FFmpeg
   - Function signature incompatibility with newer API

## Solution

### Fix 1: Define TIMER_ABSTIME for macOS

**File:** `client/send-video.cc`
**Line:** ~1 (add to includes)

Add conditional definition for macOS compatibility:
```cpp
#ifndef TIMER_ABSTIME
#define TIMER_ABSTIME 0
#endif
```

### Fix 2: Replace avcodec_close with avcodec_free_context

**File:** `client/send-video.cc`
**Line:** 366

Replace:
```cpp
avcodec_close(codec_context);
```

With:
```cpp
avcodec_free_context(&codec_context);
```

### Fix 3: Replace clock_nanosleep with nanosleep (macOS compatibility)

**File:** `client/send-video.cc`
**Line:** 347

Replace:
```cpp
clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next_frame, NULL);
```

With macOS-compatible absolute-to-relative time conversion:
```cpp
// Sleep until next frame time (macOS compatible)
struct timespec now;
clock_gettime(CLOCK_MONOTONIC, &now);
if (now.tv_sec < next_frame.tv_sec ||
    (now.tv_sec == next_frame.tv_sec && now.tv_nsec < next_frame.tv_nsec)) {
    struct timespec sleep_time;
    sleep_time.tv_sec = next_frame.tv_sec - now.tv_sec;
    sleep_time.tv_nsec = next_frame.tv_nsec - now.tv_nsec;
    if (sleep_time.tv_nsec < 0) {
        sleep_time.tv_sec--;
        sleep_time.tv_nsec += 1000000000;
    }
    nanosleep(&sleep_time, NULL);
}
```

## Build Command

After fixes:
```bash
make send-video
```

Or add to default client build by updating `client/Makefile`:
```makefile
all : send-text send-image send-video
```

## Applied Changes

### Files Modified

1. **`client/send-video.cc`** - Three fixes applied:
   - Added TIMER_ABSTIME definition (lines 28-31)
   - Changed `avcodec_close()` to `avcodec_free_context()` (line 366)
   - Replaced `clock_nanosleep()` with portable `nanosleep()` implementation (lines 347-359)

2. **`client/Makefile`** - Updated default build target:
   - Changed: `all : send-text send-image`
   - To: `all : send-text send-image send-video`

### Build Result

```
✓ client/send-text   (39K)
✓ client/send-image  (41K)
✓ client/send-video  (39K)
✓ client/game/pong-game (73K)
```

Command: `make client` builds all four targets successfully.

## Reference

- **FFmpeg 8.1**: Removed deprecated codec functions
- **macOS POSIX timing**: Uses standard constants without TIMER_ABSTIME
- **macOS platform**: `clock_nanosleep()` not available; must use `nanosleep()` with calculated relative times
