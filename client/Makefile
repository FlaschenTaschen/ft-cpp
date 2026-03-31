FLASCHEN_TASCHEN_API_DIR=../api
BUILD_DIR=../build/client

CXXFLAGS=-Wall -Wextra -pedantic -O3 -I$(FLASCHEN_TASCHEN_API_DIR)/include -I. -std=c++03
LDFLAGS=-L$(FLASCHEN_TASCHEN_API_DIR)/lib -lftclient
FTLIB=$(FLASCHEN_TASCHEN_API_DIR)/lib/libftclient.a

MAGICK_CXXFLAGS=$(shell GraphicsMagick++-config --cppflags --cxxflags)
MAGICK_LDFLAGS=$(shell GraphicsMagick++-config --ldflags --libs) -L/opt/homebrew/Cellar/libtool/2.5.4/lib

FFMPEG_LDFLAGS=$(shell pkg-config --cflags --libs libavcodec libavformat libswscale libavutil libavdevice 2>/dev/null)
HAVE_FFMPEG=$(shell pkg-config --exists libavcodec libavformat libswscale libavutil libavdevice 2>/dev/null && echo yes || echo no)

# ft-detect is Linux-only (requires mDNS/Avahi)
UNAME_S=$(shell uname -s)
ifeq ($(UNAME_S),Linux)
    AVAHI_CFLAGS=$(shell pkg-config --cflags avahi-client 2>/dev/null)
    AVAHI_LDFLAGS=$(shell pkg-config --libs avahi-client 2>/dev/null)
    COMMON_TARGETS=$(BUILD_DIR)/send-text $(BUILD_DIR)/send-image $(BUILD_DIR)/ft-detect
else
    AVAHI_CFLAGS=
    AVAHI_LDFLAGS=
    COMMON_TARGETS=$(BUILD_DIR)/send-text $(BUILD_DIR)/send-image
endif

ifeq ($(HAVE_FFMPEG),yes)
    ALL_TARGETS=$(COMMON_TARGETS) $(BUILD_DIR)/send-video
else
    ALL_TARGETS=$(COMMON_TARGETS)
endif

all : $(ALL_TARGETS)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/send-text: send-text.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

$(BUILD_DIR)/send-image : send-image.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(MAGICK_CXXFLAGS) -o $@ $< $(MAGICK_LDFLAGS) $(LDFLAGS)

# We're using a couple of deprecated functions. Pull request to update this to
# the latest libraries is welcome.
$(BUILD_DIR)/send-video: send-video.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -Wno-deprecated-declarations -o $@ $< $(FFMPEG_LDFLAGS) $(LDFLAGS)

$(BUILD_DIR)/ft-detect: ft-detect.cc $(FLASCHEN_TASCHEN_API_DIR)/lib/ft-discovery.cc | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(AVAHI_CFLAGS) -o $@ $^ $(AVAHI_LDFLAGS)

%.o : %.cc
	$(CXX) $(LIB_CXXFLAGS) -c -o $@ $<

$(FTLIB):
	make -C $(FLASCHEN_TASCHEN_API_DIR)/lib

clean:
	rm -rf $(BUILD_DIR)
