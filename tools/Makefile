CXXFLAGS=-Wall -Wextra -pedantic -O3 -std=c++03
BUILD_DIR=../build/tools

MAGICK_CXXFLAGS=$(shell GraphicsMagick++-config --cppflags --cxxflags)
MAGICK_LDFLAGS=$(shell GraphicsMagick++-config --ldflags --libs) -L/opt/homebrew/Cellar/libtool/2.5.4/lib

all: $(BUILD_DIR)/pixelate

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/pixelate: pixelate.cc | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(MAGICK_CXXFLAGS) -o $@ $< $(MAGICK_LDFLAGS)

clean:
	rm -rf $(BUILD_DIR)
