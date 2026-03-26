#!/usr/bin/env zsh

# Find the window ID for FlaschenTaschen display
WINDOW_ID=$(windows | grep co.sstools.FlaschenTaschen | head -1 | awk '{print $1}')

# Generate timestamp for filename (down to the second)
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
FILENAME="ft-${TIMESTAMP}.png"

# Take a screenshot of just that window
screencapture -l $WINDOW_ID "$FILENAME"
echo "Screenshot saved to: $FILENAME"