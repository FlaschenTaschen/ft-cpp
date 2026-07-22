# Demo implementations Makefile
# Builds various visual effect demos

# grayscale requires nlohmann-json headers, located via pkg-config
# (Debian/Ubuntu: sudo apt install nlohmann-json3-dev; macOS: brew install nlohmann-json).
# If not found, grayscale is skipped and the remaining demos still build.
JSON_CFLAGS=$(shell pkg-config --cflags nlohmann_json 2>/dev/null)
HAVE_JSON=$(shell pkg-config --exists nlohmann_json 2>/dev/null && echo yes || echo no)
ifeq ($(HAVE_JSON),no)
    # Fallback: header installed in default include path (no pkg-config needed)
    ifneq ($(wildcard /usr/include/nlohmann/json.hpp),)
        HAVE_JSON=yes
    endif
endif

CXXFLAGS=-Wall -O3 -I../../api/include -I.. $(JSON_CFLAGS)
LDFLAGS=-L../../api/lib -lftclient
FTLIB=../../api/lib/libftclient.a
BUILD_DIR=../../build/demos/src

# List of demo binaries to build
ALL=simple-example simple-animation random-dots quilt black plasma nb-logo blur lines hack fractal midi kbd2midi words life maze sierpinski matrix
ifeq ($(HAVE_JSON),yes)
    ALL+=grayscale
else
    $(info NOTE: nlohmann-json not found; skipping grayscale demo. Install nlohmann-json3-dev (Debian) or 'brew install nlohmann-json' (macOS) to enable it.)
endif
ALL_TARGETS=$(addprefix $(BUILD_DIR)/,$(ALL))

all: $(BUILD_DIR) $(ALL_TARGETS)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/%: %.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

$(FTLIB):
	$(MAKE) -C ../../api/lib

clean:
	rm -rf $(BUILD_DIR)

.PHONY: all clean
