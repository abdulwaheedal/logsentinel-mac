#!/bin/bash

echo "🛑 Stopping LogSentinel processes..."

# Kill the backend and the agent
pkill -f uvicorn
pkill -f mac_log_agent.py

echo "✅ LogSentinel shut down successfully."

# Wait 2 seconds before finishing so you can see the message
sleep 2

# Closes the terminal window
killall Terminal