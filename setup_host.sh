#!/bin/bash
set -e

# Setup Host Directories
echo "Ensuring host directories exist..."

# Obsidian Vault Host Directory
if [ ! -d "/opt/atlas/vault" ]; then
    echo "Creating /opt/atlas/vault..."
    sudo mkdir -p /opt/atlas/vault
    # Ensure readable/writable by standard Docker UID/GID
    sudo chown -R 1000:1000 /opt/atlas/vault
fi

# Rclone Cache Sync
if [ ! -d "/opt/charon/rclone/sync" ]; then
    echo "Creating /opt/charon/rclone/sync..."
    sudo mkdir -p /opt/charon/rclone/sync
    sudo chown -R 1000:1000 /opt/charon/rclone/sync
fi

# Rclone Cache Archive
if [ ! -d "/opt/charon/rclone/archive" ]; then
    echo "Creating /opt/charon/rclone/archive..."
    sudo mkdir -p /opt/charon/rclone/archive
    sudo chown -R 1000:1000 /opt/charon/rclone/archive
fi

echo "Host setup complete."
