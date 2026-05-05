#!/bin/bash

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# 1. Activate the venv (assuming it's inside the project folder)
if [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "❌ Error: Virtual environment (venv) not found."
    echo "Please run 'python3 -m venv venv && pip install -r requirements.txt' first."
    exit 1
fi

# 2. Start the backend API in the background
uvicorn api.main:app --host 0.0.0.0 --port 8000 &

# 3. Wait for API to boot
sleep 2

# 4. Start the log agent in the background
python agent/mac_log_agent.py &

# 5. Open the dashboard
open dashboard/index.html

echo "✅ LogSentinel started."
echo "   API:       http://localhost:8000/health"
echo "   Dashboard: dashboard/index.html"
