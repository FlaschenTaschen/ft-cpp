# Some Simple examples to get started.
FLASCHEN_TASCHEN_API_DIR=../../api
BUILD_DIR=../../build/demos/examples-api-use

CXXFLAGS=-Wall -O3 -I$(FLASCHEN_TASCHEN_API_DIR)/include -I.
LDFLAGS=-L$(FLASCHEN_TASCHEN_API_DIR)/lib -lftclient
FTLIB=$(FLASCHEN_TASCHEN_API_DIR)/lib/libftclient.a

all : $(BUILD_DIR)/simple-example $(BUILD_DIR)/simple-animation

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/simple-animation: simple-animation.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

$(BUILD_DIR)/simple-example: simple-example.cc $(FTLIB) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

%.o : %.cc
	$(CXX) $(LIB_CXXFLAGS) -c -o $@ $<

$(FTLIB) :
	make -C $(FLASCHEN_TASCHEN_API_DIR)/lib

clean:
	rm -rf $(BUILD_DIR)
