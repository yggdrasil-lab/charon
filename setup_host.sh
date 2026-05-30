#!/bin/bash
set -e

echo "=== Charon host setup ==="

# ---------------------------------------------------------------------------
# Obsidian Vault — create if missing, then enforce ownership every run.
# Charon-sync runs as UID 1000 and must write to the vault when bisync pulls
# changes from Google Drive.
# ---------------------------------------------------------------------------
VAULT_ROOT="${OBSIDIAN_VAULT_PATH:-/opt/atlas/vault}"
VAULT_PATH="${VAULT_ROOT}/second-brain"

echo "Vault: ${VAULT_PATH}"

if [ ! -d "${VAULT_PATH}" ]; then
    echo "  → Creating ${VAULT_PATH}..."
    sudo mkdir -p "${VAULT_PATH}"
fi

echo "  → Enforcing ownership (1000:1000)..."
sudo chown -R 1000:1000 "${VAULT_PATH}"
echo "  ✓ Done"

# ---------------------------------------------------------------------------
# Rclone Cache — create subdirectories if missing, then enforce ownership
# every run. When rclone bisync pulls from Google Drive it writes state
# files here, so UID 1000 must be able to write.
# ---------------------------------------------------------------------------
CACHE_ROOT="${RCLONE_CACHE_PATH:-/opt/charon/rclone}"

for subdir in sync archive; do
    CACHE_PATH="${CACHE_ROOT}/${subdir}"
    echo "Cache: ${CACHE_PATH}"

    if [ ! -d "${CACHE_PATH}" ]; then
        echo "  → Creating ${CACHE_PATH}..."
        sudo mkdir -p "${CACHE_PATH}"
    fi

    echo "  → Enforcing ownership (1000:1000)..."
    sudo chown -R 1000:1000 "${CACHE_PATH}"
    echo "  ✓ Done"
done

echo "=== Host setup complete ==="
