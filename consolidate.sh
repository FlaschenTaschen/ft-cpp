#!/bin/zsh
#
# consolidate.sh - Consolidate flaschen-taschen and ft-demos into ft-cpp
#
# This script copies the C++ source repositories into the ft-cpp directory
# while preserving the build system and directory structure.
#
# Usage: ./consolidate.sh [--dry-run] [--force]
#
# Options:
#   --dry-run   Show what would be copied without making changes
#   --force     Overwrite existing files without prompting

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'  # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FT_CPP_DIR="$SCRIPT_DIR"

# Source repos (relative to ft-cpp: ../)
SOURCE_FLASCHEN_TASCHEN="$(cd "$SCRIPT_DIR" && cd ../flaschen-taschen && pwd)"
SOURCE_FT_DEMOS="$(cd "$SCRIPT_DIR" && cd ../ft-demos && pwd)"
SOURCE_FT_DEMOS_FT="$(cd "$SCRIPT_DIR" && cd ../ft-demos/ft && pwd)"
SOURCE_FT_DEMOS_SRC="$(cd "$SCRIPT_DIR" && cd ../ft-demos/src && pwd)"
SOURCE_FT_DEMOS_SCRIPTS="$(cd "$SCRIPT_DIR" && cd ../ft-demos/scripts && pwd)"

# Parse arguments
DRY_RUN=false
FORCE=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: $0 [--dry-run] [--force]"
      exit 1
      ;;
  esac
done

# Helper functions
log_info() {
  echo -e "${BLUE}ℹ${NC} $1"
}

log_success() {
  echo -e "${GREEN}✓${NC} $1"
}

log_warning() {
  echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
  echo -e "${RED}✗${NC} $1"
}

confirm() {
  local prompt="$1"
  if [[ "$FORCE" == true ]]; then
    return 0
  fi

  read -q "response?$prompt (y/n) "
  echo
  [[ "$response" == "y" || "$response" == "Y" ]]
}

# Validate source directories
validate_sources() {
  log_info "Validating source directories..."

  # Validate flaschen-taschen
  if [[ ! -d "$SOURCE_FLASCHEN_TASCHEN" ]]; then
    log_error "Source repo not found: $SOURCE_FLASCHEN_TASCHEN"
    return 1
  fi

  if [[ ! -d "$SOURCE_FLASCHEN_TASCHEN/api" ]]; then
    log_error "Missing api directory in flaschen-taschen"
    return 1
  fi

  if [[ ! -d "$SOURCE_FLASCHEN_TASCHEN/client" ]]; then
    log_error "Missing client directory in flaschen-taschen"
    return 1
  fi

  if [[ ! -d "$SOURCE_FLASCHEN_TASCHEN/server" ]]; then
    log_error "Missing server directory in flaschen-taschen"
    return 1
  fi

  log_success "✓ flaschen-taschen validated"

  # Validate ft-demos
  if [[ ! -d "$SOURCE_FT_DEMOS_FT" ]]; then
    log_warning "ft-demos not found at $SOURCE_FT_DEMOS_FT (optional)"
  else
    log_success "✓ ft-demos validated"
  fi

  log_success "Source directories validated"
  return 0
}

# Copy directory with confirmation
copy_directory() {
  local source="$1"
  local dest="$2"
  local dir_name=$(basename "$source")

  if [[ -d "$dest" ]]; then
    log_warning "Destination already exists: $dir_name"
    if ! confirm "Overwrite $dir_name? "; then
      log_warning "Skipped: $dir_name"
      return 0
    fi
  fi

  if [[ "$DRY_RUN" == true ]]; then
    log_info "[DRY-RUN] Would copy: $source → $dest"
    return 0
  fi

  # Create backup if destination exists
  if [[ -d "$dest" ]]; then
    local backup="$dest.backup.$(date +%s)"
    log_warning "Backing up existing $dir_name to $backup"
    mv "$dest" "$backup"
  fi

  cp -r "$source" "$dest"
  log_success "Copied: $dir_name"
}

# Copy file if different
copy_file_if_new() {
  local source="$1"
  local dest="$2"
  local file_name=$(basename "$source")

  if [[ -f "$dest" ]] && ! cmp -s "$source" "$dest"; then
    log_warning "File differs: $file_name"
    if ! confirm "Overwrite $file_name? "; then
      log_warning "Skipped: $file_name"
      return 0
    fi
  fi

  if [[ "$DRY_RUN" == true ]]; then
    log_info "[DRY-RUN] Would copy: $source → $dest"
    return 0
  fi

  cp "$source" "$dest"
  log_success "Copied: $file_name"
}

# Consolidate fonts from scattered locations to root fonts/
consolidate_fonts() {
  echo ""
  log_info "Phase 3: Consolidating fonts..."
  echo ""

  # Create root fonts directory
  if [[ "$DRY_RUN" == true ]]; then
    log_info "[DRY-RUN] Would create: $FT_CPP_DIR/fonts"
  else
    mkdir -p "$FT_CPP_DIR/fonts"
  fi

  # Copy fonts from client/fonts
  if [[ -d "$FT_CPP_DIR/client/fonts" ]]; then
    if [[ "$DRY_RUN" == true ]]; then
      log_info "[DRY-RUN] Would copy fonts from client/fonts to root fonts/"
    else
      cp "$FT_CPP_DIR/client/fonts"/*.bdf "$FT_CPP_DIR/fonts/" 2>/dev/null || true
      log_success "Copied fonts from client/fonts"
    fi
  fi

  # Copy additional fonts from server/rgb-matrix/fonts
  if [[ -d "$FT_CPP_DIR/server/rgb-matrix/fonts" ]]; then
    if [[ "$DRY_RUN" == true ]]; then
      log_info "[DRY-RUN] Would copy unique fonts from server/rgb-matrix/fonts to root fonts/"
    else
      for font in "$FT_CPP_DIR/server/rgb-matrix/fonts"/*.bdf; do
        name=$(basename "$font")
        if [[ ! -f "$FT_CPP_DIR/fonts/$name" ]]; then
          cp "$font" "$FT_CPP_DIR/fonts/"
        fi
      done
      log_success "Consolidated unique fonts from server/rgb-matrix/fonts"
    fi
  fi

  # Update font references in game code
  if [[ "$DRY_RUN" == false ]]; then
    update_font_references
  fi

  # Remove redundant font directories
  if [[ "$DRY_RUN" == true ]]; then
    log_info "[DRY-RUN] Would remove: client/fonts, demos/client/fonts"
  else
    [[ -d "$FT_CPP_DIR/client/fonts" ]] && rm -rf "$FT_CPP_DIR/client/fonts" && log_success "Removed: client/fonts"
    [[ -d "$FT_CPP_DIR/demos/client/fonts" ]] && rm -rf "$FT_CPP_DIR/demos/client/fonts" && log_success "Removed: demos/client/fonts"
  fi
}

# Update font path references in game code
update_font_references() {
  local files=(
    "client/game/pong-game.cc"
    "client/game/game-engine.cc"
    "demos/client/game/pong-game.cc"
    "demos/client/game/game-engine.cc"
  )

  for file in "${files[@]}"; do
    if [[ -f "$FT_CPP_DIR/$file" ]]; then
      if [[ "$file" == "demos/client/game/"* ]]; then
        sed -i '' 's#"fonts/5x5.bdf"#"../../../../fonts/5x5.bdf"#g' "$FT_CPP_DIR/$file"
      else
        sed -i '' 's#"fonts/5x5.bdf"#"../../fonts/5x5.bdf"#g' "$FT_CPP_DIR/$file"
      fi
      log_success "Updated font path in: $file"
    fi
  done
}

# Copy demos/src and create Makefile
consolidate_demos_src() {
  echo ""
  log_info "Phase 4: Consolidating demo implementations (src/)..."
  echo ""

  if [[ -d "$SOURCE_FT_DEMOS_SRC" ]]; then
    copy_directory "$SOURCE_FT_DEMOS_SRC" "$FT_CPP_DIR/demos/src"

    if [[ "$DRY_RUN" == false ]]; then
      create_demos_src_makefile
    fi
  else
    log_warning "ft-demos/src not found at $SOURCE_FT_DEMOS_SRC"
  fi
}

# Copy demo scripts
consolidate_demo_scripts() {
  echo ""
  log_info "Phase 5: Consolidating demo scripts..."
  echo ""

  if [[ -d "$SOURCE_FT_DEMOS_SCRIPTS" ]]; then
    copy_directory "$SOURCE_FT_DEMOS_SCRIPTS" "$FT_CPP_DIR/demos/scripts"
  else
    log_warning "ft-demos/scripts not found at $SOURCE_FT_DEMOS_SCRIPTS"
  fi
}

# Create Makefile for demos/src
create_demos_src_makefile() {
  if [[ "$DRY_RUN" == true ]]; then
    log_info "[DRY-RUN] Would create: demos/src/Makefile"
    return 0
  fi

  cat > "$FT_CPP_DIR/demos/src/Makefile" << 'EOF'
# Demo implementations Makefile
# Builds various visual effect demos

CXXFLAGS=-Wall -O3 -I../../api/include -I..
LDFLAGS=-L../../api/lib -lftclient
FTLIB=../../api/lib/libftclient.a

# List of demo binaries to build
ALL=simple-example simple-animation random-dots quilt black plasma nb-logo blur lines hack fractal midi kbd2midi words life maze sierpinski matrix

all: $(ALL)

%: %.cc $(FTLIB)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

$(FTLIB):
	$(MAKE) -C ../../api/lib

clean:
	rm -f $(ALL)

.PHONY: all clean
EOF

  log_success "Created: demos/src/Makefile"
}

# Main consolidation
consolidate() {
  echo ""
  log_info "Starting consolidation..."

  if [[ "$DRY_RUN" == true ]]; then
    log_warning "DRY RUN MODE - no changes will be made"
  fi

  # Phase 1: Copy from flaschen-taschen
  echo ""
  log_info "Phase 1: Consolidating from flaschen-taschen..."
  echo ""

  # Copy major directories
  copy_directory "$SOURCE_FLASCHEN_TASCHEN/api" "$FT_CPP_DIR/api"
  copy_directory "$SOURCE_FLASCHEN_TASCHEN/client" "$FT_CPP_DIR/client"
  copy_directory "$SOURCE_FLASCHEN_TASCHEN/examples-api-use" "$FT_CPP_DIR/examples-api-use"
  copy_directory "$SOURCE_FLASCHEN_TASCHEN/server" "$FT_CPP_DIR/server"
  copy_directory "$SOURCE_FLASCHEN_TASCHEN/hardware" "$FT_CPP_DIR/hardware"

  # Copy documentation and config files (if they exist and differ)
  if [[ -f "$SOURCE_FLASCHEN_TASCHEN/README.md" ]]; then
    copy_file_if_new "$SOURCE_FLASCHEN_TASCHEN/README.md" "$FT_CPP_DIR/README.md.ft-flaschen-taschen"
  fi

  if [[ -f "$SOURCE_FLASCHEN_TASCHEN/.gitignore" ]]; then
    copy_file_if_new "$SOURCE_FLASCHEN_TASCHEN/.gitignore" "$FT_CPP_DIR/.gitignore.ft-flaschen-taschen"
  fi

  if [[ -f "$SOURCE_FLASCHEN_TASCHEN/.gitmodules" ]]; then
    copy_file_if_new "$SOURCE_FLASCHEN_TASCHEN/.gitmodules" "$FT_CPP_DIR/.gitmodules"
  fi

  # Phase 2: Merge from ft-demos
  echo ""
  log_info "Phase 2: Consolidating from ft-demos..."
  echo ""

  if [[ -d "$SOURCE_FT_DEMOS_FT" ]]; then
    # For ft-demos, we'll copy into a ft-demos subdirectory to avoid conflicts
    copy_directory "$SOURCE_FT_DEMOS_FT/client" "$FT_CPP_DIR/demos/client"
    copy_directory "$SOURCE_FT_DEMOS_FT/examples-api-use" "$FT_CPP_DIR/demos/examples-api-use"
    copy_directory "$SOURCE_FT_DEMOS_FT/server" "$FT_CPP_DIR/demos/server"

    if [[ -f "$SOURCE_FT_DEMOS_FT/README.md" ]]; then
      copy_file_if_new "$SOURCE_FT_DEMOS_FT/README.md" "$FT_CPP_DIR/README.md.ft-demos"
    fi
  else
    log_warning "ft-demos/ft not found at $SOURCE_FT_DEMOS_FT"
  fi

  # Copy other Python demo files from ft-demos root if they exist
  if [[ -f "$SOURCE_FT_DEMOS/flaschen_np.py" ]]; then
    copy_file_if_new "$SOURCE_FT_DEMOS/flaschen_np.py" "$FT_CPP_DIR/demos/flaschen_np.py"
  fi
  if [[ -f "$SOURCE_FT_DEMOS/fsa.py" ]]; then
    copy_file_if_new "$SOURCE_FT_DEMOS/fsa.py" "$FT_CPP_DIR/demos/fsa.py"
  fi
  if [[ -f "$SOURCE_FT_DEMOS/grid.py" ]]; then
    copy_file_if_new "$SOURCE_FT_DEMOS/grid.py" "$FT_CPP_DIR/demos/grid.py"
  fi
  if [[ -f "$SOURCE_FT_DEMOS/ripple.py" ]]; then
    copy_file_if_new "$SOURCE_FT_DEMOS/ripple.py" "$FT_CPP_DIR/demos/ripple.py"
  fi
  if [[ -f "$SOURCE_FT_DEMOS/sierpinski_rain.py" ]]; then
    copy_file_if_new "$SOURCE_FT_DEMOS/sierpinski_rain.py" "$FT_CPP_DIR/demos/sierpinski_rain.py"
  fi

  # Phase 3: Consolidate fonts
  consolidate_fonts

  # Phase 4: Consolidate demo implementations (src/)
  consolidate_demos_src

  # Phase 5: Consolidate demo scripts
  consolidate_demo_scripts

  echo ""
  log_success "Consolidation complete!"
}

# Create top-level Makefile template
create_makefile() {
  if [[ -f "$FT_CPP_DIR/Makefile" ]]; then
    log_warning "Makefile already exists"
    if ! confirm "Overwrite Makefile? "; then
      log_warning "Skipped: Makefile"
      return 0
    fi
  fi

  if [[ "$DRY_RUN" == true ]]; then
    log_info "[DRY-RUN] Would create: Makefile"
    return 0
  fi

  cat > "$FT_CPP_DIR/Makefile" << 'EOF'
# Flaschen Taschen C++ - Top-level Makefile
# Routes build commands to subdirectories

.PHONY: all api client server examples hardware clean help

# Default target
all: api client server examples hardware

# Build individual components
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

# Clean all artifacts
clean:
	$(MAKE) -C api/lib clean
	$(MAKE) -C client clean
	$(MAKE) -C client/game clean
	$(MAKE) -C server clean
	$(MAKE) -C examples-api-use clean
	$(MAKE) -C hardware clean

# Help
help:
	@echo "Flaschen Taschen C++ Build System"
	@echo ""
	@echo "Targets:"
	@echo "  make              Build everything (api, client, server, examples, hardware)"
	@echo "  make api          Build libftclient library only"
	@echo "  make client       Build client tools and games"
	@echo "  make server       Build server (FT_BACKEND=terminal by default)"
	@echo "  make examples     Build example programs"
	@echo "  make hardware     Build hardware utilities"
	@echo "  make clean        Clean all build artifacts"
	@echo "  make help         Show this help message"
	@echo ""
	@echo "FT_BACKEND options:"
	@echo "  terminal          Terminal output (default)"
	@echo "  ft                Real FlaschenTaschen hardware"
	@echo "  rgb-matrix        RGB LED matrix backend"
	@echo "  spixels           Spixels backend"
	@echo ""
	@echo "Example: make FT_BACKEND=ft server"

.PHONY: help
EOF

  log_success "Created: Makefile"
}

# Post-consolidation instructions
post_consolidation_info() {
  echo ""
  echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${GREEN}Consolidation Summary${NC}"
  echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""

  log_info "Next steps:"
  echo ""
  echo "1. Review consolidated files:"
  echo "   cd $FT_CPP_DIR"
  echo "   ls -la api/ client/ server/ examples-api-use/ demos/"
  echo "   ls -la fonts/"
  echo ""
  echo "2. Verify files were consolidated:"
  echo "   ls fonts/*.bdf        # 27 unique fonts"
  echo "   ls demos/src/*.cc     # 20 demo implementations"
  echo "   ls demos/scripts/     # 9 automation scripts"
  echo ""
  echo "3. Test the build:"
  echo "   cd $FT_CPP_DIR"
  echo "   make clean"
  echo "   make api          # Build library"
  echo "   make client       # Build client tools"
  echo "   make server       # Build server"
  echo "   make -C demos/src # Build visual effect demos"
  echo ""
  echo "4. Update .gitmodules if using git submodules"
  echo ""
  echo "5. Commit the consolidation:"
  echo "   git add ."
  echo "   git commit -m 'Consolidate flaschen-taschen and ft-demos'"
  echo ""
  echo "See Migrate.md for consolidation overview."
  echo "See Changelog/Fonts.md for font consolidation details."
  echo ""
}

# Main execution
main() {
  echo ""
  echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${GREEN}Flaschen Taschen C++ Consolidation Script${NC}"
  echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""

  log_info "Source 1: $SOURCE_FLASCHEN_TASCHEN"
  log_info "Source 2: $SOURCE_FT_DEMOS_FT"
  log_info "Target: $FT_CPP_DIR"
  echo ""

  # Validate
  if ! validate_sources; then
    log_error "Validation failed. Aborting."
    exit 1
  fi

  echo ""

  # Confirm before proceeding (skip for dry-run)
  if [[ "$DRY_RUN" != true ]]; then
    if ! confirm "Proceed with consolidation? "; then
      log_warning "Aborted by user"
      exit 0
    fi
  fi

  # Consolidate
  consolidate

  # Create Makefile
  echo ""
  create_makefile

  # Summary
  post_consolidation_info
}

# Run main
main
