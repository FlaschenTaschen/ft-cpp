# Consolidation Summary: ft-cpp Repository

## Overview
Successfully consolidated two C++ repositories (`flaschen-taschen` - server/clients and `ft-demos` - demo implementations) into a single `ft-cpp` repository with a unified make-based build system.

**Status: COMPLETED** (2025-03-25)

## What Was Done

### Source Repositories (Consolidated From)

**flaschen-taschen** (at `../flaschen-taschen`) - C++ server, clients, and API library
**ft-demos** (at `../ft-demos`) - Demo implementations and Python scripts

### flaschen-taschen/ Structure
**Subdirectories and their purpose:**

| Directory | Purpose | Makefile |
|-----------|---------|----------|
| `api/lib/` | Core client library (libftclient.a/.so.1) | Yes |
| `client/` | Client demos and examples (send-*.cc) | Yes |
| `client/game/` | Game examples (pong, game-engine) | Yes |
| `examples-api-use/` | Additional API usage examples | Yes |
| `server/` | Main server with multiple backends | Yes |
| `server/rgb-matrix/` | LED matrix backend (submodule) | Yes |
| `server/spixels/` | Spixel backend (submodule) | Yes |
| `hardware/` | Hardware configuration files | Yes |

### Current Build System
**Makefile dependency chain:**
```
flaschen-taschen/
├── api/lib/Makefile
│   └── Builds: libftclient.a, libftclient.so.1
│       Compiles: udp-flaschen-taschen.cc, bdf-font.cc, graphics.cc
│       Flags: -Wall -Wextra -O3 -std=c++03 -fPIC
│
├── client/Makefile
│   └── Depends on: ../api/lib/libftclient.a
│       Compiles: send-text.cc, send-image.cc, send-video.cc (+ others)
│       Uses: GraphicsMagick++, FFmpeg
│
├── client/game/Makefile
│   └── Depends on: ../api/lib/libftclient.a
│       Compiles: pong-game.cc, game-engine.cc, game-client.cc
│
├── examples-api-use/Makefile
│   └── Depends on: ../api/lib/libftclient.a
│
├── server/Makefile
│   └── Depends on: ../api/lib/libftclient.a (indirectly)
│       FT_BACKEND support (terminal, rgb-matrix, spixels, ft)
│       Submodules: rgb-matrix/, spixels/
│
└── hardware/Makefile
```

## Consolidation Process

### 1. Final Directory Structure (Achieved)
```
ft-cpp/
├── Makefile                          # Top-level orchestrator
├── README.md
├── Migrate.md                        # This file
├── LICENSE
├── .gitignore
├── .gitmodules                       # Updated submodule references
│
├── api/
│   ├── include/
│   │   ├── ft-thread.h
│   │   ├── udp-flaschen-taschen.h
│   │   ├── graphics.h
│   │   └── ...
│   └── lib/
│       ├── Makefile                 # Unchanged - builds libftclient
│       ├── udp-flaschen-taschen.cc
│       ├── bdf-font.cc
│       ├── graphics.cc
│       └── ...
│
├── client/
│   ├── Makefile                     # Unchanged - builds send-*
│   ├── send-text.cc
│   ├── send-image.cc
│   ├── send-video.cc
│   └── game/
│       ├── Makefile                 # Unchanged - builds games
│       ├── pong-game.cc
│       ├── game-engine.cc
│       └── ...
│
├── examples-api-use/
│   ├── Makefile                     # Unchanged - builds examples
│   └── *.cc files
│
├── server/
│   ├── Makefile                     # Unchanged - handles FT_BACKEND
│   ├── ft-thread.cc
│   ├── udp-server.cc
│   ├── composite-flaschen-taschen.cc
│   ├── rgb-matrix/                  # Submodule (git submodule)
│   ├── spixels/                     # Submodule (git submodule)
│   └── ...
│
└── hardware/
    ├── Makefile
    └── ...
```

### 2. Top-Level Makefile
**Goals:**
- Route build commands to appropriate subdirectories
- Support selective builds and clean operations
- Maintain backward compatibility

**Proposed targets:**
```makefile
.PHONY: all api client server examples hardware clean

all: api client server examples hardware

api:
	$(MAKE) -C api/lib

client: api
	$(MAKE) -C client
	$(MAKE) -C client/game

server: api
	$(MAKE) -C server FT_BACKEND=$(FT_BACKEND)

examples: api
	$(MAKE) -C examples-api-use

hardware:
	$(MAKE) -C hardware

clean:
	$(MAKE) -C api/lib clean
	$(MAKE) -C client clean
	$(MAKE) -C client/game clean
	$(MAKE) -C server clean
	$(MAKE) -C examples-api-use clean
	$(MAKE) -C hardware clean
```

**Usage:**
```bash
make              # Build everything (default: FT_BACKEND=terminal)
make api          # Build only the library
make client       # Build client + game examples
make server       # Build server
make FT_BACKEND=ft server  # Build with real hardware backend
make clean        # Clean all
```

### 3. Submodule Handling
**Current state in flaschen-taschen:**
- `server/rgb-matrix/` - submodule
- `server/spixels/` - submodule

**Options:**
- **Option A (Recommended):** Keep as git submodules
  - Update `.gitmodules` to reference correct paths in ft-cpp
  - Users must clone with `--recursive` or run `git submodule update --init`

- **Option B:** Copy submodule code directly
  - Simplifies cloning but increases repository size
  - Lose upstream update ability

**Recommendation:** Use Option A (git submodules)

### 4. Path Updates
**Relative path references remain valid:**
- `client/Makefile` references `../api/lib/libftclient.a` → still works in ft-cpp
- `client/game/Makefile` references `../api/lib/libftclient.a` → still works
- All existing relative paths continue to work after consolidation

**No changes needed** to individual Makefiles - they reference paths relative to their location.

### 2. Consolidation Script (`consolidate.sh`)

A zsh script at `ft-cpp/consolidate.sh` was used to automate the consolidation:

**Features:**
- Validates source directories exist
- Creates backups of existing files before overwriting
- Supports dry-run mode for previewing changes
- Interactive prompts (can be bypassed with `--force`)
- Generates top-level Makefile
- Provides post-consolidation guidance

**What the script does:**

**Phase 1: Copy from flaschen-taschen**
1. **Validates Sources** - Verifies source repos exist with required directories
2. **Copies Core Directories:**
   - `api/` → Core libftclient library (headers + source)
   - `client/` → Client tools (send-text, send-image, send-video)
   - `client/game/` → Game examples (pong-game, game-engine)
   - `examples-api-use/` → Additional API usage examples
   - `server/` → Main server with backend support
   - `hardware/` → Hardware configuration utilities
3. **Copies Configuration Files:**
   - `.gitignore` → `.gitignore.ft-flaschen-taschen` (preserves existing ft-cpp .gitignore)
   - `.gitmodules` → Git submodule configuration

**Phase 2: Copy from ft-demos**
4. **Copies Demo Directories** (to `demos/` subdirectory):
   - `ft/client/` → Demo client implementations
   - `ft/examples-api-use/` → Demo examples
   - `ft/server/` → Demo server variations
5. **Copies Python Demo Scripts:**
   - `flaschen_np.py` - NumPy-based demo
   - `fsa.py` - FSA demo
   - `grid.py` - Grid display demo
   - `ripple.py` - Ripple effect demo
   - `sierpinski_rain.py` - Sierpinski triangle rain demo

**General Handling**
6. **File Management:**
   - Creates timestamped backups (e.g., `api.backup.1234567890`) before overwriting
   - Prompts for confirmation before overwriting existing files
   - Skips on user decline (unless `--force` flag used)
7. **Makefile Generation** - Creates top-level Makefile routing build commands
8. **Status Reporting** - Color-coded output showing success/warnings/errors

**Script Execution Flow:**
```
consolidate.sh [--dry-run] [--force]
├── validate_sources()
│   ├── Check flaschen-taschen exists at ../flaschen-taschen
│   ├── Verify api/, client/, server/ present
│   ├── Check ft-demos exists at ../ft-demos (optional)
│   └── Abort if critical directories missing
├── consolidate()
│   ├── Phase 1: Copy from flaschen-taschen
│   │   ├── Copy api/ → ft-cpp/api/
│   │   ├── Copy client/ → ft-cpp/client/
│   │   ├── Copy examples-api-use/ → ft-cpp/examples-api-use/
│   │   ├── Copy server/ → ft-cpp/server/
│   │   ├── Copy hardware/ → ft-cpp/hardware/
│   │   ├── Copy .gitignore → .gitignore.ft-flaschen-taschen
│   │   └── Copy .gitmodules → .gitmodules
│   │
│   └── Phase 2: Copy from ft-demos
│       ├── Copy ft/client/ → ft-cpp/demos/client/
│       ├── Copy ft/examples-api-use/ → ft-cpp/demos/examples-api-use/
│       ├── Copy ft/server/ → ft-cpp/demos/server/
│       └── Copy Python scripts → ft-cpp/demos/*.py
│
├── create_makefile()
│   └── Generate top-level Makefile with build targets
└── post_consolidation_info()
    └── Display next steps and testing instructions
```

### 3. Build System

#### Top-Level Makefile
The consolidation creates a top-level Makefile with the following targets:

```makefile
all           # Build everything (api, client, server, examples, hardware)
api           # Build libftclient library only
client        # Build client tools (send-text, send-image, send-video) + games
server        # Build server with FT_BACKEND=terminal (default)
examples      # Build example programs
hardware      # Build hardware utilities
clean         # Clean all build artifacts
help          # Show help message
```

#### Build Dependency Chain
```
make all
├── api                (builds: libftclient.a, libftclient.so.1)
├── client (depends on api)
│   ├── send-text.cc → send-text executable
│   ├── send-image.cc → send-image executable
│   ├── send-video.cc → send-video executable
│   └── game/pong-game.cc → pong-game executable
├── server (depends on api)
│   └── FT_BACKEND support:
│       ├── terminal (default)
│       ├── ft (real hardware)
│       ├── rgb-matrix (LED matrix)
│       └── spixels
├── examples (depends on api)
│   ├── simple-example
│   └── simple-animation
└── hardware
    └── CAD file generation (requires pstoedit, optional)
```

#### Environment Variables
- `FT_BACKEND` - Selects server backend (terminal, ft, rgb-matrix, spixels)
- Default: `FT_BACKEND=terminal` (from server/Makefile)

**Usage Examples:**
```bash
make                            # Build all with terminal backend
make client                     # Build client tools only
make FT_BACKEND=ft server       # Build server with hardware backend
make clean && make all          # Clean and rebuild everything
```

### 4. What Remains Unchanged
- Individual subdirectory Makefiles (api/lib, client, server, etc.)
- All source code files (.cc, .h)
- Build dependency structure
- Build flags and compiler options
- Submodule references (rgb-matrix, spixels)

### 5. What Changed
- **Directory structure** - Consolidated all sources under ft-cpp/
- **Top-level Makefile** - Created to route build commands to subdirectories
- **Conditional FT_BACKEND passing** - Top-level Makefile uses `$(if $(FT_BACKEND),FT_BACKEND=$(FT_BACKEND))` to allow server Makefile default (terminal) to be used when no backend specified
- **Hardware Makefile** - Added missing `clean` target and made `pstoedit` optional
- **demos/ directory** - Added for ft-demos code (client, examples-api-use, server variations, Python scripts)

### 6. Build Verification Checklist (Completed)
- [x] `make clean` removes all artifacts
- [x] `make api` builds libftclient.a and libftclient.so.1 (19K)
- [x] `make client` builds all client tools
  - [x] send-text (39K)
  - [x] send-image (41K)
  - [x] send-video (39K)
- [x] `make client` builds games (pong-game: 73K)
- [x] `make server` builds server with FT_BACKEND=terminal (default) (59K)
- [x] `make examples` builds examples
  - [x] simple-example (36K)
  - [x] simple-animation (52K)
- [x] `make all` successfully builds everything
- [x] Relative path references work correctly in all Makefiles
- [x] `make clean` successfully cleans all directories

### 7. Known Issues & Fixes Applied
1. **Hardware Makefile** - Added missing `clean` target and made `pstoedit` optional (tool not always installed)
2. **FT_BACKEND conditional passing** - Top-level Makefile now only passes FT_BACKEND if explicitly set, allowing server Makefile default to work
3. **send-video FFmpeg 8.1 compatibility** - Fixed three API compatibility issues:
   - Added TIMER_ABSTIME definition for macOS
   - Replaced deprecated `avcodec_close()` with `avcodec_free_context()`
   - Replaced Linux-specific `clock_nanosleep()` with portable `nanosleep()` implementation
4. **send-image library linking** - Added explicit libtool library path to MAGICK_LDFLAGS

### 8. Build System Design Decisions

**Decision 1: Submodule handling** ✓
- Use git submodules (Option A) - cleaner, allows upstream updates
- Submodules remain in place: server/rgb-matrix/, server/spixels/

**Decision 2: FT_BACKEND passing** ✓
- Conditional passing with `$(if $(FT_BACKEND),FT_BACKEND=$(FT_BACKEND))`
- Allows server Makefile's default (terminal) to be used when not specified
- Supports explicit override: `make FT_BACKEND=ft server`

**Decision 3: Build dependency order** ✓
- api always builds first (fundamental dependency)
- client/server/examples depend on api
- Top-level targets enforce this order

**Decision 4: Backward compatibility** ✓
- Maintained exact same build output paths and filenames
- No changes to individual subdirectory Makefiles
- All relative paths continue to work

### 9. Future Maintenance
- This `Migrate.md` documents consolidation rationale and architecture
- Update if adding new subdirectories or build targets
- Document any new FT_BACKEND options
- Note any breaking changes to build system

## Build System Architecture

### Directory Hierarchy
```
ft-cpp/
├── Makefile                        # Top-level orchestrator
├── api/lib/                        # Core libftclient library
├── client/                         # Client tools (send-*)
├── client/game/                    # Game examples (pong-game)
├── examples-api-use/               # API usage examples
├── server/                         # Main server implementation
│   ├── rgb-matrix/                # Submodule (LED matrix backend)
│   └── spixels/                   # Submodule (Spixel backend)
├── hardware/                       # Hardware CAD files
├── demos/                          # Demo implementations from ft-demos
│   ├── client/
│   ├── examples-api-use/
│   ├── server/
│   └── *.py (Python demos)
├── fonts/                          # Consolidated font files
├── SendVideoFix.md                 # FFmpeg 8.1 compatibility fixes
└── Migrate.md                      # This consolidation document
```

### Build Targets
| Target | Purpose | Dependencies | Notes |
|--------|---------|--------------|-------|
| `all` | Build everything | api → client → server → examples → hardware | Default target |
| `api` | Build libftclient library | — | Provides libftclient.a, libftclient.so.1 |
| `client` | Build client tools + games | api | Builds send-text, send-image, send-video, pong-game |
| `server` | Build server | api | Respects FT_BACKEND env var (default: terminal) |
| `examples` | Build examples | api | Builds simple-example, simple-animation |
| `hardware` | Build hardware utilities | — | Optional; requires pstoedit for CAD generation |
| `clean` | Clean all artifacts | — | Removes all .o files and executables |
| `help` | Show help message | — | Display usage and options |

### Supported Backends
```
make server                    # Terminal backend (default)
make FT_BACKEND=terminal server
make FT_BACKEND=ft server      # Real FlaschenTaschen hardware
make FT_BACKEND=rgb-matrix server   # LED matrix
```

## Consolidation Timeline (Actual)
- Consolidation execution: Completed via consolidate.sh
- Build system fixes: ~2 hours
  - Hardware Makefile: 10 min
  - FT_BACKEND conditional: 5 min
  - send-video FFmpeg fixes: 90 min
  - Documentation: 15 min
- Full test build: Successful ✓

## Testing the Build

**Quick test:**
```bash
make clean && make all    # Full build
```

**Test specific components:**
```bash
make api                  # Library only
make client              # Client tools only
make server              # Server (terminal backend)
make FT_BACKEND=terminal server  # Explicit terminal
```

**Verify binaries:**
```bash
ls -lh api/lib/libftclient.a client/send-* client/game/pong-game server/ft-server
```

## What's Included

**From flaschen-taschen:**
- Core API library (libftclient)
- Client tools: send-text, send-image, send-video
- Games: pong-game
- Server with multiple backends (terminal, rgb-matrix, spixels, ft)
- Examples: simple-example, simple-animation
- Hardware: CAD/design files

**From ft-demos:**
- Demo client implementations
- Demo examples
- Demo server variations
- Python demo scripts (flaschen_np.py, grid.py, ripple.py, sierpinski_rain.py, fsa.py)

**Additional improvements:**
- Consolidated font files (27 unique fonts in root fonts/ directory)
- Fixed FFmpeg 8.1 compatibility in send-video
- Fixed GraphicsMagick library linking in send-image
- Robust build system with proper dependency ordering

## Post-Consolidation Cleanup (2026-03-25)

### 1. Removed Duplicate Files
**Duplicate Client Tools:**
- Removed `demos/client/send-text.cc`, `send-image.cc`, `send-video.cc` (simplified variants)
- **Reason:** Root `client/` versions are more complete with additional features and compilation flags
- **Result:** Single source of truth for client tools

**Duplicate Game Implementations:**
- Removed entire `demos/client/game/` directory (contained copies of pong-game.cc, game-engine.cc)
- **Reason:** Identical functionality as root `client/game/` with only path adjustments
- **Result:** All games now in root `client/game/`

**Removed Entire Directory:**
- Removed `demos/client/` directory after consolidation (no remaining source files)

### 2. Fixed send-video Compilation Issues
Applied FFmpeg 8.1 compatibility fixes (commit c9df678e5432f62ce4e95ba935e97fd5add0f2b5):
- Added `TIMER_ABSTIME` macOS compatibility define
- Replaced Linux-specific `clock_nanosleep()` with portable `nanosleep()` implementation
- Replaced deprecated `avcodec_close()` with `avcodec_free_context()`
- Updated `client/Makefile` to build send-video in `all` target

### 3. Improved Top-Level Makefile
- **FT_BACKEND conditional:** Changed `$(MAKE) -C server FT_BACKEND=$(FT_BACKEND)` to `$(MAKE) -C server $(if $(FT_BACKEND),FT_BACKEND=$(FT_BACKEND))`
  - Allows server Makefile default (terminal) to be used when FT_BACKEND not explicitly set
  - Fixes build when FT_BACKEND environment variable is undefined

### 4. Fixed hardware/Makefile
- Made `pstoedit` tool optional with graceful fallback
- Added proper dependency for `pi-mounting-rig.dxf` (now depends on `.ps` file)
- Added helpful warning message if `pstoedit` is not installed
- Added `clean` target to remove generated `.dxf` files

### 5. Integrated Demo Builds
**Created `demos/Makefile`:**
- New top-level Makefile for demos directory
- `make all` target builds: examples, server, src (visual effect demos)
- Individual targets: `make examples`, `make server`, `make src`
- Builds all visual effect demos: hack, black, plasma, nb-logo, blur, lines, fractal, midi, kbd2midi, words, life, maze, sierpinski, matrix, random-dots, quilt

**Updated Root Makefile:**
- Added `demos` to `.PHONY` declaration
- Added `demos` to `all` target: `all: api client server examples hardware demos`
- New `demos: api` target builds all demos
- Added `$(MAKE) -C demos clean` to clean target
- Updated help text to include demos

### 6. Final Directory Structure (After Cleanup)
```
ft-cpp/
├── Makefile                          # Top-level build orchestrator
├── api/                              # libftclient library
├── client/                           # Client tools (send-text, send-image, send-video)
│   └── game/                         # Game implementations (pong-game, etc.)
├── examples-api-use/                 # API usage examples
├── server/                           # Main server with multiple backends
│   ├── rgb-matrix/                   # Submodule (LED matrix backend)
│   └── spixels/                      # Submodule (Spixel backend)
├── hardware/                         # Hardware utilities
├── demos/                            # Demo implementations
│   ├── Makefile                      # Demo build orchestrator
│   ├── examples-api-use/             # Demo examples
│   ├── server/                       # Demo server variations
│   ├── src/                          # Visual effect demos (20+ implementations)
│   ├── scripts/                      # Automation scripts
│   └── *.py                          # Python demo scripts
├── fonts/                            # Consolidated font files (27 unique)
└── Changelog/                        # This file and related documentation
```

### 7. Build System Summary

| Target | Purpose | Notes |
|--------|---------|-------|
| `make all` | Build everything | Includes all tools, servers, examples, games, and demos |
| `make api` | Build libftclient | Single library, used by all clients |
| `make client` | Build client tools | send-text, send-image, send-video + games |
| `make server` | Build server | Terminal backend default, supports ft/rgb-matrix/spixels |
| `make examples` | Build examples | API usage examples |
| `make hardware` | Build hardware | Optional CAD generation (requires pstoedit) |
| `make demos` | Build all demos | Examples, server variations, 20+ visual effects |
| `make clean` | Clean all | Removes all build artifacts |

### 8. Testing Commands
```bash
# Full build
make clean && make all

# Build specific components
make api                              # Library only
make client                           # Client tools + games
make server                           # Server
make demos                            # All demos
make -C demos/src hack                # Specific demo

# Clean
make clean                            # Everything
make -C demos clean                   # Just demos
```

### Known Improvements
- ✅ No duplicate source files in demos vs. root
- ✅ Single source of truth for all tools and games
- ✅ FFmpeg 8.1 compatibility fixed
- ✅ pstoedit gracefully optional
- ✅ Proper dependency ordering in build system
- ✅ All 20+ visual effect demos buildable via `make demos`
- ✅ Consolidated fonts system working correctly
