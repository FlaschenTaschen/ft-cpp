# Flaschen Taschen: C++

Unified C++ repository consolidating the Flaschen Taschen server, clients, and demo applications into a single codebase with a unified make-based build system.

## Overview

This repository combines:
- **Server** - The Flaschen Taschen LED display server with support for multiple backends (terminal, RGB matrix, spixels)
- **Client Library** - libftclient C++ library for communicating with the server
- **Client Examples** - Demo applications and utilities (send-text, send-image, send-video, games)
- **Example Programs** - Additional API usage examples

## Dependencies

**Debian/Ubuntu (including Raspberry Pi OS):**
```bash
sudo apt update
sudo apt install build-essential pkg-config libgraphicsmagick++1-dev nlohmann-json3-dev \
    libavahi-client-dev libavahi-common-dev
```

**macOS:**
```bash
brew install pkg-config graphicsmagick nlohmann-json
```

**Required packages:**
- `build-essential` - C++ compiler, make, and build tools
- `pkg-config` - Used by the Makefiles to locate optional libraries
- `libgraphicsmagick++1-dev` / `graphicsmagick` - GraphicsMagick C++ development headers (send-image, pixelate)

**Optional packages** (features are skipped with a notice if missing):
- `nlohmann-json3-dev` / `nlohmann-json` - C++ JSON library headers (grayscale demo)
- `libavahi-client-dev`, `libavahi-common-dev` - mDNS service discovery for `ft-server` and `ft-detect` (Linux only). At runtime the Pi also needs the daemon: `sudo apt install avahi-daemon && sudo systemctl enable --now avahi-daemon`. See [docs/mDNS.md](docs/mDNS.md).
- FFmpeg dev libraries (`libavcodec-dev libavformat-dev libswscale-dev libavutil-dev libavdevice-dev` / `brew install ffmpeg`) - required for `send-video`

## Building

```bash
make              # Build everything (server + client tools + examples)
make api          # Build just the client library
make client       # Build client tools and games
make server       # Build server (FT_BACKEND=terminal by default)
make clean        # Clean all artifacts
```

**Build with specific backend:**
```bash
make FT_BACKEND=ft server          # Hardware backend
make FT_BACKEND=rgb-matrix server  # RGB matrix backend
make FT_BACKEND=spixels server     # Spixels backend
```

See [Migrate.md](Changelog/Migrate.md) for consolidation details and directory structure.

## Source Repositories

This project consolidates code from:
- [hzeller/flaschen-taschen](https://github.com/hzeller/flaschen-taschen) - Original server and core libraries
- [cgorringe/ft-demos](https://github.com/cgorringe/ft-demos) - Demo applications and utilities

## Related Projects

Ports from original C++ code:
* [Flaschen Taschen: Swift](../ft-swift)
* [Flaschen Taschen: Python](../ft-py)

## License

This project is licensed under the **GNU General Public License v3.0** (GPLv3).
See [LICENSE](LICENSE) for details.

## Attribution

This consolidation builds upon the original [Flaschen Taschen](https://noisebridge.net/wiki/Flaschen_Taschen) project developed by the [Noisebridge](https://noisebridge.net/) community.

For a complete list of original authors and contributors, see [AUTHORS.md](AUTHORS.md).

