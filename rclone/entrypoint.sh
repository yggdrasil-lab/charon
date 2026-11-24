#!/bin/sh
set -euo pipefail
# Sync interval in seconds (default 300 - 5 min, down from 30s to avoid Google Drive API rate limits)
SYNC_INTERVAL="${SYNC_INTERVAL:-300}"

log() {
    echo "[$(date)] $1"
}

notify_iris() {
    _SEVERITY="$1"
    _TITLE="$2"
    _DETAILS="$3"
    curl -s --max-time 5 --connect-timeout 3 -X POST http://iris:8000/notify/discord \
        -H "Content-Type: application/json" \
        -d "{\"service\":\"charon-sync\",\"bot_name\":\"Charon\",\"severity\":\"${_SEVERITY}\",\"title\":\"${_TITLE}\",\"details\":\"${_DETAILS}\"}" >/dev/null 2>&1 || true
}

# Graceful Shutdown Handling
STOP_REQUESTED=false
SYNC_IN_PROGRESS=false
SLEEP_PID=""
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

# Validate bisync cache health on startup - only force --resync if both
# primary (.lst) and backup (.lst-old) state files are missing. Cache survives
# restarts so a container bounce can do a fast normal sync without --resync overhead.
BISYNC_CACHE="/var/cache/rclone/.cache/rclone/bisync"

has_lst_files() {
    [ -d "$BISYNC_CACHE" ] && [ -n "$(find "$BISYNC_CACHE" -maxdepth 1 -name '*.lst' -print -quit 2>/dev/null)" ]
}

has_backup_lst_files() {
    [ -d "$BISYNC_CACHE" ] && [ -n "$(find "$BISYNC_CACHE" -maxdepth 1 -name '*.lst-old' -print -quit 2>/dev/null)" ]
}

if has_lst_files; then
    log "Bisync cache healthy - will use normal (fast) sync"
elif has_backup_lst_files; then
    log "Bisync primary listings missing, backup .lst-old present - will attempt --recover on first sync"
elif [ -d "$BISYNC_CACHE" ]; then
    log "Bisync cache incomplete (missing .lst and .lst-old files) - clearing to force --resync"
    find /var/cache/rclone -mindepth 1 -delete 2>/dev/null || true
else
    log "No bisync cache found - first run, will use --resync"
fi

CONSECUTIVE_STATE_FAILURES=0
BISYNC_COMMON_FLAGS="--verbose --checksum --resilient --recover --max-lock 15m --create-empty-src-dirs"

while [ "$STOP_REQUESTED" = false ]; do
    log "----------------------------------------------------------------"
    
    if [ ! -d "$BISYNC_CACHE" ]; then
        log "First run of session: Initializing with --resync..."
        SYNC_IN_PROGRESS=true
        if ! rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --resync $BISYNC_COMMON_FLAGS; then
             # Non-zero exit doesn't always mean resync failed - permission errors on
             # individual files (e.g. directories missing +x, causing lstat "permission denied")
             # produce a non-zero exit code even though the resync completed successfully.
             # If the bisync state cache was created, the resync worked - proceed.
             if has_lst_files; then
                 CONSECUTIVE_STATE_FAILURES=0
                 log "WARNING: Resync completed with non-critical errors. Proceeding with normal sync."
             else
                 CONSECUTIVE_STATE_FAILURES=0
                 log "WARNING: Initial resync failed entirely (missing state files). Cache will remain empty, retrying next loop..."
                 find /var/cache/rclone -mindepth 1 -delete 2>/dev/null || true
                 notify_iris "error" "Initial Resync Failed" "Initial bisync resync failed entirely (missing state files). Retrying next loop."
             fi
        else
             CONSECUTIVE_STATE_FAILURES=0
             log "Initial resync successful."
        fi
        SYNC_IN_PROGRESS=false
    else
        log "Subsequent run: Syncing changes..."
        SYNC_IN_PROGRESS=true
        if ! rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data $BISYNC_COMMON_FLAGS; then
            # Non-zero exit may be from non-critical errors (permission denied on some files)
            # or transient vanished-file aborts when --resilient keeps .lst-old intact.
            if has_lst_files; then
                CONSECUTIVE_STATE_FAILURES=0
                log "WARNING: Sync completed with non-critical or retryable errors (state files intact)."
            elif has_backup_lst_files && [ "$CONSECUTIVE_STATE_FAILURES" -lt 1 ]; then
                CONSECUTIVE_STATE_FAILURES=$((CONSECUTIVE_STATE_FAILURES + 1))
                log "WARNING: Sync aborted with missing .lst files, but .lst-old backup exists (attempt ${CONSECUTIVE_STATE_FAILURES}). Deferring cache clear so next cycle can self-heal via --recover."
            else
                CONSECUTIVE_STATE_FAILURES=0
                log "ERROR: Sync failed with unrecoverable state loss. Clearing bisync cache to force resync on next run."
                find /var/cache/rclone -mindepth 1 -delete 2>/dev/null || true
                notify_iris "error" "Bisync Aborted (Cache Cleared)" "Sync failed with unrecoverable state loss after recovery attempt. Cache cleared; next cycle will trigger full resync."
            fi
        else
            CONSECUTIVE_STATE_FAILURES=0
            log "Sync successful."
        fi
        SYNC_IN_PROGRESS=false
    fi

    SYNC_IN_PROGRESS=false
    touch /tmp/charon_sync_heartbeat

    if [ "$STOP_REQUESTED" = true ]; then
        log "Shutdown was requested during sync. Exiting gracefully without sleep..."
        break
    fi

    log "Sync complete. Sleeping for ${SYNC_INTERVAL} seconds..."
    
    # Sleep with interrupt capability
    sleep "$SYNC_INTERVAL" &
    SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null || true
    SLEEP_PID=""
done

log "Rclone service stopped."
