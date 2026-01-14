# Sync interval in seconds (default 30)
SYNC_INTERVAL="${SYNC_INTERVAL:-30}"

log() {
    echo "[$(date)] $1"
}

# Graceful Shutdown Handling
STOP_REQUESTED=false
shutdown_handler() {
    log "SIGTERM/SIGINT received. Shutting down gracefully..."
    STOP_REQUESTED=true
    
    # Clear cache on stop to ensure the next start is a clean resync
    log "Clearing bisync cache before exit..."
    rm -rf /root/.cache/rclone/bisync
    
    # If sleeping, kill sleep to exit
    if [ -n "$SLEEP_PID" ]; then
        kill "$SLEEP_PID" 2>/dev/null
    fi
}

trap 'shutdown_handler' SIGTERM SIGINT

log "Initializing rclone entrypoint..."

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

# Ensure we start fresh on every container boot
log "Forcing fresh start: Clearing bisync cache..."
rm -rf /root/.cache/rclone/bisync

while [ "$STOP_REQUESTED" = false ]; do
    log "----------------------------------------------------------------"
    
    # Check if we've ever synced before by looking for rclone's internal cache.
    # Since we clear it at boot and on stop, this will be TRUE for the first iteration.
    if [ ! -d "/root/.cache/rclone/bisync" ]; then
        log "First run of session: Initializing with --resync..."
        if ! rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --checksum --resync --create-empty-src-dirs; then
             log "WARNING: Initial resync failed. Cache will remain empty, retrying next loop..."
        else
             log "Initial resync successful."
        fi
    else
        log "Subsequent run: Syncing changes..."
        if ! rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --checksum --create-empty-src-dirs; then
            log "ERROR: Sync failed. Clearing bisync cache to force critical resync on next run."
            rm -rf /root/.cache/rclone/bisync
        else
            log "Sync successful."
        fi
    fi

    log "Sync complete. Sleeping for ${SYNC_INTERVAL} seconds..."
    
    # Sleep with interrupt capability
    sleep "$SYNC_INTERVAL" &
    SLEEP_PID=$!
    wait "$SLEEP_PID"
    SLEEP_PID=""
done

log "Rclone service stopped."