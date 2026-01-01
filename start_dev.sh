#!/bin/bash
set -e

echo "Starting Charon in Development mode..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "Error: .env file not found. Please create it by copying .env.example:"
    echo "  cp .env.example .env"
    echo "Then update it with your configuration and secrets."
    exit 1
fi

# Build and start the containers
docker compose down
docker compose up --build -d

echo "Development environment started."
docker compose logs -f
