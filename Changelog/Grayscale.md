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
