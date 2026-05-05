#!/bin/bash

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE}" )" && pwd )"
cd "$DIR"

echo "⚙️  Starting Setup for LogSentinel..."

# 1. Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
else
    echo "✅ Virtual environment already exists."
fi

# 2. Activate and Install dependencies
echo "📥 Installing dependencies from requirements.txt..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "✅ Setup Complete!"
echo "
