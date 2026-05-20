#!/usr/bin/env bash
# Start the MSP to OpenProject importer.
# Creates a virtual environment on first run, installs dependencies, then launches.
set -e

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

echo "Installing dependencies..."
venv/bin/pip install -r requirements.txt -q

echo "Starting MSP Importer at http://localhost:8080"
venv/bin/uvicorn web_app:app --host 0.0.0.0 --port 8080
