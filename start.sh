#!/bin/bash
set -e
docker compose down --remove-orphans || true
docker compose up -d --build --remove-orphans
