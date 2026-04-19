#!/bin/bash

# Claude Code SessionStart Hook for Ben Oracle
# Runs automatically before each session starts

set -e

echo "Setting up Ben Oracle environment..."

# Create .env from .env.example if it doesn't exist
if [ ! -f .env ]; then
  echo "Creating .env from .env.example..."
  cp .env.example .env
  echo ".env created. Update with your own values if needed."
fi

# Install backend dependencies
echo "Installing Python dependencies..."
pip install -q -r requirements.txt 2>/dev/null || true

# Install frontend dependencies
if [ -d frontend ]; then
  echo "Installing frontend dependencies..."
  cd frontend
  npm install -q 2>/dev/null || true
  cd ..
fi

echo "Setup complete. Ben Oracle is ready."
