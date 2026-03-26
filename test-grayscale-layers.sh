#!/usr/bin/env zsh

# Automated testing script for grayscale demo with incrementing layers
# Captures screenshots at each layer level for debugging/testing

# Configuration
GRAYSCALE_BIN="./build/demos/src/grayscale"
DISPLAY_HOST="localhost"
GEOMETRY="64x64"
TIMEOUT="3"
JSON_FILE="content/space-invaders-1.json"
START_LAYER=0
END_LAYER=10

# Check if grayscale binary exists
if [[ ! -f "$GRAYSCALE_BIN" ]]; then
    echo "Error: $GRAYSCALE_BIN not found. Please build first."
    exit 1
fi

# Check if screenshot.sh exists
if [[ ! -f "./screenshot.sh" ]]; then
    echo "Error: screenshot.sh not found in current directory."
    exit 1
fi

echo "Starting grayscale layer test sequence..."
echo "Config: $GEOMETRY, host=$DISPLAY_HOST, timeout=$TIMEOUT, json=$JSON_FILE"
echo "Layer range: $START_LAYER to $END_LAYER"
echo ""

# Create screenshots directory if it doesn't exist
mkdir -p screenshots

# Run grayscale with incrementing layers
for layer in $(seq $START_LAYER $END_LAYER); do
    echo "=== Layer $layer ==="

    # Run grayscale demo with this layer
    echo "Running: $GRAYSCALE_BIN -h $DISPLAY_HOST -g $GEOMETRY -l $layer -t $TIMEOUT -f $JSON_FILE"
    $GRAYSCALE_BIN -h $DISPLAY_HOST -g $GEOMETRY -l $layer -t $TIMEOUT -f $JSON_FILE

    # Wait a moment for the display to update
    sleep 0.5

    # Capture screenshot
    TIMESTAMP=$(date +%Y%m%d-%H%M%S)
    SCREENSHOT="screenshots/layer-${layer}-${TIMESTAMP}.png"

    echo "Capturing screenshot..."
    WINDOW_ID=$(windows | grep co.sstools.FlaschenTaschen | head -1 | awk '{print $1}')
    if [[ -n "$WINDOW_ID" ]]; then
        screencapture -l $WINDOW_ID "$SCREENSHOT"
        echo "Screenshot saved to: $SCREENSHOT"
    else
        echo "Warning: Could not find FlaschenTaschen window"
    fi

    echo ""
done

echo "Test sequence complete. Screenshots saved in ./screenshots/ directory"
