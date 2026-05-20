@echo off
:: Start the MSP to OpenProject importer.
:: Creates a virtual environment on first run, installs dependencies, then launches.

if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

echo Installing dependencies...
venv\Scripts\pip install -r requirements.txt -q

echo Starting MSP Importer at http://localhost:8080
venv\Scripts\uvicorn web_app:app --host 0.0.0.0 --port 8080
