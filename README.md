# Flaschen Taschen: C++

Unified C++ repository consolidating the Flaschen Taschen server, clients, and demo applications into a single codebase with a unified make-based build system.

## Overview

This repository combines:
- **Server** - The Flaschen Taschen LED display server with support for multiple backends (terminal, RGB matrix, spixels)
- **Client Library** - libftclient C++ library for communicating with the server
- **Client Examples** - Demo applications and utilities (send-text, send-image, send-video, games)
- **Example Programs** - Additional API usage examples

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

