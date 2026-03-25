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
