FLASCHEN_TASCHEN_API_DIR=../../api
BUILD_DIR=../../build/client/game

CXXFLAGS=-Wall -O3 -I$(FLASCHEN_TASCHEN_API_DIR)/include -I.
LDFLAGS=-L$(FLASCHEN_TASCHEN_API_DIR)/lib -lftclient
FTLIB=$(FLASCHEN_TASCHEN_API_DIR)/lib/libftclient.a

pong: $(BUILD_DIR)/game-client $(BUILD_DIR)/pong-game

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/game-client: game-client.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

$(BUILD_DIR)/pong-game: pong-game.cc game-engine.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ pong-game.cc game-engine.cc $(LDFLAGS)

send-image : send-image.cc $(FTLIB)
	$(CXX) $(CXXFLAGS) $(MAGICK_CXXFLAGS) -o $@ $< $(MAGICK_LDFLAGS) $(LDFLAGS)

send-video: send-video.cc libftclient.a
	$(CXX) $(CXXFLAGS) -o $@ $< $(FFMPEG_LDFLAGS) $(LDFLAGS)

%.o : %.cc
	$(CXX) $(LIB_CXXFLAGS) -c -o $@ $<

$(FTLIB) :
	make -C $(FLASCHEN_TASCHEN_API_DIR)/lib

clean:
	rm -rf $(BUILD_DIR)
