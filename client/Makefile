FLASCHEN_TASCHEN_API_DIR=../api
BUILD_DIR=../build/client

CXXFLAGS=-Wall -Wextra -pedantic -O3 -I$(FLASCHEN_TASCHEN_API_DIR)/include -I. -std=c++03
LDFLAGS=-L$(FLASCHEN_TASCHEN_API_DIR)/lib -lftclient
FTLIB=$(FLASCHEN_TASCHEN_API_DIR)/lib/libftclient.a

MAGICK_CXXFLAGS=$(shell GraphicsMagick++-config --cppflags --cxxflags)
MAGICK_LDFLAGS=$(shell GraphicsMagick++-config --ldflags --libs) -L/opt/homebrew/Cellar/libtool/2.5.4/lib

FFMPEG_LDFLAGS=$(shell pkg-config --cflags --libs  libavcodec libavformat libswscale libavutil libavdevice)
all : $(BUILD_DIR)/send-text $(BUILD_DIR)/send-image $(BUILD_DIR)/send-video

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/send-text: send-text.cc | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

$(BUILD_DIR)/send-image : send-image.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(MAGICK_CXXFLAGS) -o $@ $< $(MAGICK_LDFLAGS) $(LDFLAGS)

# We're using a couple of deprecated functions. Pull request to update this to
# the latest libraries is welcome.
$(BUILD_DIR)/send-video: send-video.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -Wno-deprecated-declarations -o $@ $< $(FFMPEG_LDFLAGS) $(LDFLAGS)

%.o : %.cc
	$(CXX) $(LIB_CXXFLAGS) -c -o $@ $<

$(FTLIB):
	make -C $(FLASCHEN_TASCHEN_API_DIR)/lib

clean:
	rm -rf $(BUILD_DIR)
