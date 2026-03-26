# Demo implementations Makefile
# Builds various visual effect demos

CXXFLAGS=-Wall -O3 -I../../api/include -I..
LDFLAGS=-L../../api/lib -lftclient
FTLIB=../../api/lib/libftclient.a
BUILD_DIR=../../build/demos/src

# List of demo binaries to build
ALL=simple-example simple-animation random-dots quilt black plasma nb-logo blur lines hack fractal midi kbd2midi words life maze sierpinski matrix
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
