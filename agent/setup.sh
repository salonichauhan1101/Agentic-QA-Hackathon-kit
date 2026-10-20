#!/bin/bash
# One-time setup for the DemoBank QA agent. Run from anywhere: ./agent/setup.sh
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
if [ ! -f .env ]; then
  echo "GEMINI_API_KEY=paste_your_key_here" > .env
  echo "Created .env. Add your Gemini API key to it."
fi
echo "Setup complete. Activate the environment with: source .venv/bin/activate"
