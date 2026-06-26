#!/bin/bash
# Launcher for the Hospital Price Explorer — API version.
# Double-click this file to start a local server and open the viewer.
# Works on macOS. Requires Python 3 (pre-installed on macOS 12+).

# Change to the folder this launcher is in (works even if double-clicked
# from a different working directory)
cd "$(dirname "$0")/api"

PORT=8765

echo "================================================"
echo " Hospital Price Explorer — API Viewer"
echo "================================================"
echo ""
echo "Starting local server on http://localhost:$PORT"
echo ""
echo "The viewer will open in your browser shortly."
echo "Keep this window open while using the viewer."
echo ""
echo "To STOP: press Ctrl+C or close this Terminal window."
echo "================================================"
echo ""

# Open the browser after a 1.5-second delay (so the server has time to bind)
(sleep 1.5 && open "http://localhost:$PORT") &

# Start the server in the foreground (so Ctrl+C stops it cleanly)
python3 -m http.server $PORT
