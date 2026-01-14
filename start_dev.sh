#!/bin/bash
set -e
source ./scripts/load_env.sh
./scripts/deploy.sh "charon_dev" docker-compose.yml
