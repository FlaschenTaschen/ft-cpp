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
