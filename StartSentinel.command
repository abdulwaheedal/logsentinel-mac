#!/bin/bash

# Activate the venv that's in ~/Downloads (where you ran pip install)
source ~/Downloads/venv/bin/activate

# Move into the project folder
cd ~/Downloads/logsentinel-mac

# Start the backend API in the background
uvicorn api.main:app --host 0.0.0.0 --port 8000 &

# Wait for API to boot
sleep 2

# Start the log agent in the background
python agent/mac_log_agent.py &

# Open the dashboard
open dashboard/index.html

echo "✅ LogSentinel started. Check terminal for logs."
echo "   API:       http://localhost:8000/health"
echo "   Dashboard: dashboard/index.html"