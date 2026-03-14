#!/usr/bin/env bash
# SyncRow Data Explorer launcher

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Install/upgrade dependencies
echo "Installing dependencies..."
pip install -q -e ".[dev]"

# Run the Panel application
echo "Starting SyncRow Data Explorer..."
panel serve app.py --show --autoreload
