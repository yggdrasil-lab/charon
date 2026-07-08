#!/bin/sh
# Sync interval in seconds (default 300 — 5 min, down from 30s to avoid Google Drive API rate limits)
SYNC_INTERVAL="${SYNC_INTERVAL:-300}"

log() {
    echo "[$(date)] $1"
}

# Graceful Shutdown Handling
STOP_REQUESTED=false
SYNC_IN_PROGRESS=false
shutdown_handler() {
    if [ "$SYNC_IN_PROGRESS" = true ]; then
        log "SIGTERM/SIGINT received. Sync in progress, will complete before shutdown..."
    else
        log "SIGTERM/SIGINT received. Shutting down gracefully..."
    fi
    STOP_REQUESTED=true
    
    # If sleeping, kill sleep to exit
    if [ -n "$SLEEP_PID" ]; then
        kill "$SLEEP_PID" 2>/dev/null
    fi
}

trap 'shutdown_handler' SIGTERM SIGINT

log "Initializing rclone entrypoint..."

# Run Setup
. /setup.sh

# Validate bisync cache health on startup — only force --resync if the
# state files (.lst) are genuinely missing. Cache survives restarts so
# a container bounce can do a fast normal sync without --resync overhead.
BISYNC_CACHE="/var/cache/rclone/.cache/rclone/bisync"
if [ -d "$BISYNC_CACHE" ]; then
    if [ -z "$(find "$BISYNC_CACHE" -maxdepth 1 -name '*.lst' -print -quit 2>/dev/null)" ]; then
        log "Bisync cache incomplete (missing .lst files) — clearing to force --resync"
        find /var/cache/rclone -mindepth 1 -delete 2>/dev/null || true
    else
        log "Bisync cache healthy — will use normal (fast) sync"
    fi
else
    log "No bisync cache found — first run, will use --resync"
fi

while [ "$STOP_REQUESTED" = false ]; do
    log "----------------------------------------------------------------"
    
    if [ ! -d "$BISYNC_CACHE" ]; then
        log "First run of session: Initializing with --resync..."
        SYNC_IN_PROGRESS=true
        if ! rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --checksum --resync --create-empty-src-dirs; then
             # Non-zero exit doesn't always mean resync failed — permission errors on
             # individual files (e.g. directories missing +x, causing lstat "permission denied")
             # produce a non-zero exit code even though the resync completed successfully.
             # If the bisync state cache was created, the resync worked — proceed.
             if [ -d "$BISYNC_CACHE" ]; then
                 log "WARNING: Resync completed with non-critical errors. Proceeding with normal sync."
             else
                 log "WARNING: Initial resync failed entirely. Cache will remain empty, retrying next loop..."
                 find /var/cache/rclone -mindepth 1 -delete 2>/dev/null || true
             fi
        else
             log "Initial resync successful."
        fi
        SYNC_IN_PROGRESS=false
    else
        log "Subsequent run: Syncing changes..."
        SYNC_IN_PROGRESS=true
        if ! rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --checksum --create-empty-src-dirs; then
            # Non-zero exit may be from non-critical errors (permission denied on some files).
            # Only force a resync if the bisync state cache is missing entirely.
            if [ -d "$BISYNC_CACHE" ]; then
                log "WARNING: Sync completed with non-critical errors."
            else
                log "ERROR: Sync failed entirely. Clearing bisync cache to force resync on next run."
                find /var/cache/rclone -mindepth 1 -delete 2>/dev/null || true
            fi
        else
            log "Sync successful."
        fi
        SYNC_IN_PROGRESS=false
    fi

    SYNC_IN_PROGRESS=false
    log "Sync complete. Sleeping for ${SYNC_INTERVAL} seconds..."
    
    # Sleep with interrupt capability
    sleep "$SYNC_INTERVAL" &
    SLEEP_PID=$!
    wait "$SLEEP_PID"
    SLEEP_PID=""
done

log "Rclone service stopped."
