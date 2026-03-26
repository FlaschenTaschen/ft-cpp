# Testing Workflow for FlaschenTaschen Demos

## Overview

This document covers the automated testing approach for validating demo rendering on the FlaschenTaschen display, using incremental layer testing and screenshot capture.

## Layer-Based Testing Strategy

### Rationale

When debugging display rendering issues, it's useful to:
- **Isolate rendering layers**: Each demo frame can be placed on a different layer to see if the display receives and renders it
- **Verify connectivity**: Screenshots confirm whether frames are actually appearing on the display
- **Create reproducible test runs**: Automated scripts ensure consistent test conditions
- **Build test history**: Timestamped screenshots create an audit trail of what rendered at each step

### Workflow

1. **Run demo with incrementing layers**: Execute the same demo multiple times, each on a different layer (0, 1, 2, ...)
2. **Capture screenshot after each frame**: Let the display update, then take a screenshot showing what's currently rendered
3. **Compare results**: Review screenshots to see which layers appeared, which didn't, and any visual anomalies

## Tools

### screenshot.sh

Enhanced shell script that captures the FlaschenTaschen display window with timestamped filenames.

**Features:**
- Automatically finds the FlaschenTaschen window
- Generates filenames with timestamp: `ft-YYYYMMDD-HHMMSS.png`
- Prints confirmation with filename

**Usage:**
```bash
./screenshot.sh
# Output: Screenshot saved to: ft-20260325-144532.png
```

**Manual Testing:**
```bash
# Run a single demo
./build/demos/src/grayscale -h localhost -g 64x64 -l 0 -t 3 -f content/space-invaders-1.json
sleep 1
./screenshot.sh

# Run again on next layer
./build/demos/src/grayscale -h localhost -g 64x64 -l 1 -t 3 -f content/space-invaders-1.json
sleep 1
./screenshot.sh
```

### test-grayscale-layers.sh

Automated testing script that runs the grayscale demo across a range of layers and captures screenshots for each.

**Features:**
- Runs demo with layers 0-10 (configurable)
- Sleeps briefly between runs for display update
- Captures screenshot after each layer renders
- Saves all screenshots to `screenshots/` directory with layer number and timestamp
- Prints progress and file locations

**Configuration (edit script to customize):**
```bash
START_LAYER=0       # Starting layer number
END_LAYER=10        # Ending layer number
GEOMETRY="64x64"    # Canvas geometry
TIMEOUT="3"         # Timeout per frame (seconds)
JSON_FILE="content/space-invaders-1.json"  # Demo data file
DISPLAY_HOST="localhost"  # Display hostname
```

**Usage:**
```bash
./test-grayscale-layers.sh
```

**Output:**
```
Starting grayscale layer test sequence...
Config: 64x64, host=localhost, timeout=3, json=content/space-invaders-1.json
Layer range: 0 to 10

=== Layer 0 ===
Running: ./build/demos/src/grayscale -h localhost -g 64x64 -l 0 -t 3 -f content/space-invaders-1.json
Screenshot saved to: screenshots/layer-0-20260325-144532.png

=== Layer 1 ===
...
```

## Debugging Workflow

### Scenario: Nothing appears on display

1. Run a known-working demo on layer 0:
   ```bash
   ./build/demos/src/blur -h localhost -g 64x64 -l 0 -t 3
   ./screenshot.sh
   ```
   - If blur displays: display connection works, issue is demo-specific
   - If nothing: check display connectivity and socket configuration

2. Run grayscale on layer 0:
   ```bash
   ./build/demos/src/grayscale -h localhost -g 64x64 -l 0 -t 3 -f content/space-invaders-1.json
   ./screenshot.sh
   ```
   - Compare screenshot to blur result
   - Check debug output for packet structure and size information

3. Use automated layer testing:
   ```bash
   ./test-grayscale-layers.sh
   ```
   - Review all screenshots in `screenshots/` directory
   - Look for patterns: does layer 0 work, but others don't? Does nothing appear at all?
   - Examine debug output for any error messages or anomalies

### Expected Debug Output

The grayscale demo (and other demos) print debug information including:
- **Socket creation**: "Opening socket to {host}:{port}"
- **Canvas details**: "Created canvas {width}×{height}"
- **UDP send info**: "UDP Send: max_udp_size={size}, max_send_height={rows}, canvas={w}×{h}"
- **Packet details**: "Packet {n}: {rows} rows, header {h} bytes, data {d} bytes, total {t} bytes"

If you see:
- ✓ "Packet X: N rows" — data is being chunked and sent correctly
- ✓ UDP size near ~9000 on macOS or ~65000+ on Linux — kernel limit detection working
- ✗ "Error sending packet" — check display connectivity and FT_UDP_SIZE environment variable

## Automating Across Demos

The layer-based testing approach works for any demo. Adapt the script for different demos:

```bash
# Test blur with layers
for layer in $(seq 0 5); do
    ./build/demos/src/blur -h localhost -g 64x64 -l $layer -t 3
    sleep 0.5
    ./screenshot.sh
done

# Test checkerboard with different geometries
for geom in "32x32" "64x64" "128x128"; do
    ./build/demos/src/checkerboard -h localhost -g $geom -l 0 -t 3
    sleep 0.5
    ./screenshot.sh
done
```

## Implementation Details

**2026-03-25**
- Added screenshot.sh timestamp enhancement: `ft-YYYYMMDD-HHMMSS.png` format
- Created test-grayscale-layers.sh for automated multi-layer testing
- Documented layer-based testing strategy and debugging workflows
