# Grayscale Demo Changelog

## 2026-03-25

### Added
- **C++ port of Grayscale demo** from ft-swift
  - Full implementation of JSON-based pixel mask rendering
  - Grayscale conversion using luminance formula (0.299*R + 0.587*G + 0.114*B)
  - Support for multiple positioning modes: bounce, center, left, right, top, bottom
  - Horizontal and vertical mask combining with configurable padding
  - Rainbow color palette with optional fixed color override
  - Comprehensive command-line interface matching Swift version
  - nlohmann/json dependency for JSON file parsing

### Implementation Details
- **File**: `demos/src/grayscale.cc`
- **Build**: Added to demos build targets in `demos/src/Makefile`
- **Dependencies**: nlohmann-json (v3.12.0)

### Command-line Options
- `-f <filepath[,...]>` - JSON files with hex color arrays (required)
- `-o <orientation>` - horizontal or vertical mask combining
- `-m <mode>` - positioning mode (default: bounce)
- `-c <RRGGBB>` - fixed color in hex (default: rainbow palette)
- Standard display options: geometry, layer, timeout, hostname, delay

## 2026-03-26

### Added
- **Render mode option (`-r`)** — Control how JSON colors are displayed
  - `grayscale` (default): Modulate colors with grayscale intensity values from JSON
    - Enables rainbow palette animation and fixed color (`-c`) overrides
    - Allows grayscale JSON files to be colored dynamically
  - `original`: Display colors directly from JSON files without modulation
    - Overrides `-c` fixed color option
    - Useful for pre-colored JSON images that should render as-is

- **Sequential image animation** — Multiple JSON files display in sequence instead of side-by-side
  - New option: `-D <ms>` — Duration (milliseconds) each image displays before advancing (default 200)
  - New option: `-y <mode>` — Sequence animation behavior
    - `forward` (default): Linear loop 0 → 1 → 2 → ... → 0 → ...
    - `reverse`: Bouncing animation 0 → 1 → 2 → 1 → 0 → 1 → ...
  - Position state maintained across images (bounce direction, animation progress)
  - Images centered relative to first frame's center position

- **Transparency color control** — Configure which color represents transparency
  - New option: `-T <color>` — Transparency color (default: white)
    - `white`: Skip bright pixels (grayscale ≥ 240)
    - `black`: Skip dark pixels (grayscale ≤ 15)

- **Random bounce initialization** — Bounce mode now starts with random position and direction
  - Initial position: Random within valid display bounds
  - Initial direction: Random (forward or backward on each axis independently)
  - Each run produces different starting animation without explicit configuration

### Changed
- Mode selection: Program now supports two modes
  - **Combining mode** (default): Multiple JSON files combined side-by-side or stacked
    - Uses `-o` option (horizontal or vertical)
    - Activated when `-D` is NOT provided
  - **Sequential animation mode**: Multiple JSON files display one at a time
    - Uses `-D` option for frame duration (enables this mode)
    - Uses `-y` option for animation direction (forward/reverse)
    - `-o` option ignored in this mode
- `-c` (fixed color) now ignored when using `-r original` mode
- `-d` option continues to control rendering loop delay (unchanged semantics)

### Preserved Behavior
- All positioning modes (`-m`) work with sequential images
- Palette cycling continues across images (unless `-r original`)
- All display options (`-g`, `-l`, `-t`, `-h`) remain unchanged

## 2026-03-25 (Updated)

### Fixed
- **UDP packet size detection** — Large canvases (e.g., 64×64) now work correctly on all platforms
  - **Problem**: Code assumed 65507-byte UDP limit; macOS kernel limit is ~9216 bytes
  - **Result**: 64×64 canvases failed with "Message too long" error
  - **Solution**: Detect actual kernel UDP send buffer via `getsockopt(SO_SNDBUF)`
  - **Implementation**: Updated `UDPFlaschenTaschen` constructor to query socket before sending
  - **Priority order**: FT_UDP_SIZE env var → getsockopt() → fallback to 65507
  - **Benefit**: Existing packet chunking in `Send()` now correctly splits large images

### Verification
- 64×64 checkerboard pattern renders correctly (split into 2 packets: 47+17 rows)
- Tested on macOS with localhost socket (9216-byte kernel limit detected)
- Packet chunking confirmed: Packet 0 (9,048 bytes), Packet 1 (3,289 bytes)
