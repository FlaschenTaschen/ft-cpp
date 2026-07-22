FLASCHEN_TASCHEN_API_DIR=../api
BUILD_DIR=../build/client

CXXFLAGS=-Wall -Wextra -pedantic -O3 -I$(FLASCHEN_TASCHEN_API_DIR)/include -I. -std=c++03
LDFLAGS=-L$(FLASCHEN_TASCHEN_API_DIR)/lib -lftclient
FTLIB=$(FLASCHEN_TASCHEN_API_DIR)/lib/libftclient.a

MAGICK_CXXFLAGS=$(shell GraphicsMagick++-config --cppflags --cxxflags)
MAGICK_LDFLAGS=$(shell GraphicsMagick++-config --ldflags --libs) -L/opt/homebrew/Cellar/libtool/2.5.4/lib

FFMPEG_LDFLAGS=$(shell pkg-config --cflags --libs libavcodec libavformat libswscale libavutil libavdevice 2>/dev/null)
HAVE_FFMPEG=$(shell pkg-config --exists libavcodec libavformat libswscale libavutil libavdevice 2>/dev/null && echo yes || echo no)

# ft-detect is Linux-only (requires mDNS/Avahi).
# It is only built when avahi-client is available
# (Debian/Ubuntu: sudo apt install libavahi-client-dev pkg-config).
COMMON_TARGETS=$(BUILD_DIR)/send-text $(BUILD_DIR)/send-image
AVAHI_CFLAGS=
AVAHI_LDFLAGS=
UNAME_S=$(shell uname -s)
ifeq ($(UNAME_S),Linux)
    HAVE_AVAHI=$(shell pkg-config --exists avahi-client 2>/dev/null && echo yes || echo no)
    ifeq ($(HAVE_AVAHI),yes)
        AVAHI_CFLAGS=$(shell pkg-config --cflags avahi-client)
        AVAHI_LDFLAGS=$(shell pkg-config --libs avahi-client)
        COMMON_TARGETS+=$(BUILD_DIR)/ft-detect
    else
        $(info NOTE: avahi-client not found; skipping ft-detect. Install libavahi-client-dev and pkg-config to enable it.)
    endif
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
