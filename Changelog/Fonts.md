# Font Consolidation and Demo Integration

## Overview
Consolidated font files from multiple scattered locations into a single root `fonts/` directory and updated all references. Additionally, integrated complete C++ demo implementations from `ft-demos/src/`.

## Font Consolidation

### Before
Fonts were scattered across multiple locations:
- `client/fonts/` - 24 font files
- `server/rgb-matrix/fonts/` - 27 font files (with 4 unique: clR6x12.bdf, helvR12.bdf, texgyre-27.bdf, tom-thumb.bdf)
- `demos/client/fonts/` - duplicate copy of client fonts
- `demos/server/rgb-matrix/fonts/` - duplicate copy of rgb-matrix fonts

### After
- **Root `fonts/` directory** - 27 consolidated, unique font files
- Removed redundant subdirectories:
  - `client/fonts/` (deleted)
  - `demos/client/fonts/` (deleted)
- Kept `server/rgb-matrix/fonts/` and `demos/server/rgb-matrix/fonts/` (part of third-party rgb-matrix library)

### Files Updated
Updated hardcoded font path references to point to consolidated root fonts:

| File | Before | After |
|------|--------|-------|
| `client/game/pong-game.cc` | `"fonts/5x5.bdf"` | `"../../fonts/5x5.bdf"` |
| `client/game/game-engine.cc` | `"fonts/5x5.bdf"` | `"../../fonts/5x5.bdf"` |
| `demos/client/game/pong-game.cc` | `"fonts/5x5.bdf"` | `"../../../../fonts/5x5.bdf"` |
| `demos/client/game/game-engine.cc` | `"fonts/5x5.bdf"` | `"../../../../fonts/5x5.bdf"` |

### Font Files Consolidated (27 total)
- 10x20.bdf
- 4x6.bdf, 5x5.bdf, 5x7.bdf, 5x8.bdf
- 6x9.bdf, 6x10.bdf, 6x12.bdf, 6x13.bdf, 6x13B.bdf, 6x13O.bdf
- 7x13.bdf, 7x13B.bdf, 7x13O.bdf, 7x14.bdf, 7x14B.bdf
- 8x13.bdf, 8x13B.bdf, 8x13O.bdf
- 9x15.bdf, 9x15B.bdf, 9x18.bdf, 9x18B.bdf
- clR6x12.bdf, helvR12.bdf, texgyre-27.bdf, tom-thumb.bdf

## Demo Integration

### Integrated from `ft-demos/src/`
Copied 20 C++ demo implementations with their effect code:
- black.cc, blur.cc, fractal.cc, hack.cc
- kbd2midi.cc, life.cc, lines.cc, matrix.cc, maze.cc, midi.cc
- nb-logo.cc, plasma.cc, plasma1.cc, plasma2.cc, quilt.cc
- random-dots.cc, sierpinski.cc
- simple-animation.cc, simple-example.cc, words.cc

### Integrated from `ft-demos/scripts/`
Copied 9 automation and testing scripts:
- run-makerfaire.sh, run-nb1.sh, run-nb2.sh, run-nb3.sh, run-nb10.sh
- run-steam.sh, runtest1.sh
- playlist.txt, schedule.pl

### Build System
Created `demos/src/Makefile` with pattern rules to build all demo executables:
```makefile
% : %.cc $(FTLIB)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)
```

Builds to targets like:
- `make -C demos/src` - build all 20 demos
- `make -C demos/src simple-example` - build specific demo
- `make -C demos/src clean` - clean demo binaries

## Summary
- 27 unique fonts consolidated to root `fonts/` directory
- All font path references updated to use consolidated location
- 20 C++ demo implementations integrated from ft-demos/src/
- 9 automation/test scripts integrated from ft-demos/scripts/
- Build system ready for compiling all demos
