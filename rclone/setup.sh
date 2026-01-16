#!/bin/sh
log() {
    echo "[$(date)] $1"
}

log "Initializing rclone environment..."

# Setup writable config from secret
SECRET_NAME="${RCLONE_CONFIG_SECRET_NAME:-charon_rclone_config}"
if [ -f "/run/secrets/$SECRET_NAME" ]; then
    log "Copying rclone config from secret to writable location..."
    cp "/run/secrets/$SECRET_NAME" /tmp/rclone.conf
    chmod 600 /tmp/rclone.conf
    export RCLONE_CONFIG=/tmp/rclone.conf
else
    log "WARNING: Secret /run/secrets/$SECRET_NAME not found!"
fi

# Ensure we start fresh on every container boot if using bisync
log "Clearing bisync cache..."
rm -rf /root/.cache/rclone/bisync
