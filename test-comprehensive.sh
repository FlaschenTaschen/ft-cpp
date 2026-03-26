#!/usr/bin/env zsh

# Comprehensive grayscale testing workflow

echo "==================================="
echo "COMPREHENSIVE GRAYSCALE TEST"
echo "==================================="
echo ""

mkdir -p screenshots/comprehensive

# Test 1: Baseline - blur (known working)
echo "Test 1: Baseline with blur..."
./build/demos/src/blur -h localhost -g 64x64 -l 0 -t 2 target >/dev/null 2>&1
sleep 1.5
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
WINDOW_ID=$(windows | grep co.sstools.FlaschenTaschen | head -1 | awk '{print $1}')
screencapture -l $WINDOW_ID "screenshots/comprehensive/01-blur-${TIMESTAMP}.png" 2>/dev/null
ls -lh "screenshots/comprehensive/01-blur-${TIMESTAMP}.png" | awk '{print "  Size:", $5}'

# Test 2: Checkerboard
echo ""
echo "Test 2: Checkerboard pattern..."
./test-checkerboard -h localhost -l 0 -s 64 >/dev/null 2>&1
sleep 1.5
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
screencapture -l $WINDOW_ID "screenshots/comprehensive/02-checkerboard-${TIMESTAMP}.png" 2>/dev/null
ls -lh "screenshots/comprehensive/02-checkerboard-${TIMESTAMP}.png" | awk '{print "  Size:", $5}'

# Test 3: Grayscale with default bounce mode
echo ""
echo "Test 3: Grayscale (rainbow, bounce mode)..."
./build/demos/src/grayscale -h localhost -g 64x64 -l 0 -t 2 -f content/space-invaders-1.json >/dev/null 2>&1
sleep 1.5
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
screencapture -l $WINDOW_ID "screenshots/comprehensive/03-grayscale-rainbow-${TIMESTAMP}.png" 2>/dev/null
ls -lh "screenshots/comprehensive/03-grayscale-rainbow-${TIMESTAMP}.png" | awk '{print "  Size:", $5}'

# Test 4: Grayscale with white (fixed color)
echo ""
echo "Test 4: Grayscale (white, center mode)..."
./build/demos/src/grayscale -h localhost -g 64x64 -l 0 -t 2 -m center -c FFFFFF -f content/space-invaders-1.json >/dev/null 2>&1
sleep 1.5
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
screencapture -l $WINDOW_ID "screenshots/comprehensive/04-grayscale-white-${TIMESTAMP}.png" 2>/dev/null
ls -lh "screenshots/comprehensive/04-grayscale-white-${TIMESTAMP}.png" | awk '{print "  Size:", $5}'

# Test 5: Grayscale with bright red
echo ""
echo "Test 5: Grayscale (bright red, center mode)..."
./build/demos/src/grayscale -h localhost -g 64x64 -l 0 -t 2 -m center -c FF0000 -f content/space-invaders-1.json >/dev/null 2>&1
sleep 1.5
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
screencapture -l $WINDOW_ID "screenshots/comprehensive/05-grayscale-red-${TIMESTAMP}.png" 2>/dev/null
ls -lh "screenshots/comprehensive/05-grayscale-red-${TIMESTAMP}.png" | awk '{print "  Size:", $5}'

# Test 6: Grayscale on layer 1 (overlaid on clear layer 0)
echo ""
echo "Test 6: Clearing display and showing grayscale on layer 1..."
./build/demos/src/black -h localhost -g 64x64 -l 0 -t 1 >/dev/null 2>&1
sleep 0.5
./build/demos/src/grayscale -h localhost -g 64x64 -l 1 -t 2 -m center -c FFFFFF -f content/space-invaders-1.json >/dev/null 2>&1
sleep 1.5
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
screencapture -l $WINDOW_ID "screenshots/comprehensive/06-grayscale-layer1-${TIMESTAMP}.png" 2>/dev/null
ls -lh "screenshots/comprehensive/06-grayscale-layer1-${TIMESTAMP}.png" | awk '{print "  Size:", $5}'

echo ""
echo "==================================="
echo "All tests complete. Screenshots saved to:"
echo "  screenshots/comprehensive/"
echo ""
echo "Review the screenshots to see which test shows visible output."
echo "==================================="
