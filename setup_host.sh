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

# =============================================================================
# Obsidian Vault Ownership (for Charon bisync)
# Charon-sync runs as UID 1000 and must write to the vault when pulling
# changes from Google Drive. This enforces correct ownership on every run
# to catch any files that land as root from other processes or restores.
# =============================================================================
VAULT_PATH="${OBSIDIAN_VAULT_PATH:-/opt/atlas/vault}/second-brain"
if [ -d "${VAULT_PATH}" ]; then
    echo "Enforcing vault ownership at ${VAULT_PATH}..."
    sudo chown -R 1000:1000 "${VAULT_PATH}"
    echo "Vault ownership updated."
else
    echo "Warning: Vault not found at ${VAULT_PATH}. Skipping ownership update."
    echo "Set OBSIDIAN_VAULT_PATH and re-run this script after the vault is restored."
fi

# =============================================================================
# Rclone Cache Ownership
# When rclone bisync pulls from Google Drive, it writes bisync state files
# to the cache dir. UID 1000 must be able to write there — enforced every run.
# =============================================================================
CACHE_PATH="${RCLONE_CACHE_PATH:-/opt/charon/rclone}/sync"
if [ -d "${CACHE_PATH}" ]; then
    echo "Enforcing cache ownership at ${CACHE_PATH}..."
    sudo chown -R 1000:1000 "${CACHE_PATH}"
    echo "Cache ownership updated."
else
    echo "Warning: Cache directory not found at ${CACHE_PATH}. Skipping ownership update."
    echo "Rclone bisync may fail if the cache dir doesn't exist when charon-sync runs."
fi
