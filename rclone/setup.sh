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

# Fix directory permissions — directories need execute (+x) to be accessible.
# Obsidian sync can create directories with mode 646 (rw-r--rw-) which blocks
# rclone bisync from reading files inside them (lstat: permission denied).
log "Fixing directory permissions..."
find /data -type d ! -perm -100 -exec chmod +x {} \; 2>/dev/null || true
# Cache cleanup handled by entrypoint.sh (targets correct path: /var/cache/rclone/.cache/rclone/bisync)
# setup.sh must not clear cache — would force --resync on every restart
