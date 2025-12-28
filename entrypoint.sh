#!/bin/sh

# Graceful Shutdown Handling
STOP_REQUESTED=false
shutdown_handler() {
    echo "[$(date)] SIGTERM/SIGINT received. Shutting down gracefully..."
    STOP_REQUESTED=true
    # If sleeping, kill sleep to exit
    if [ -n "$SLEEP_PID" ]; then
        kill "$SLEEP_PID" 2>/dev/null
    fi
}

trap 'shutdown_handler' SIGTERM SIGINT

echo "Initializing rclone entrypoint..."

while [ "$STOP_REQUESTED" = false ]; do
    # Check if we've ever synced before by looking for rclone's internal cache
    # If the cache doesn't exist, we run with --resync
    if [ ! -d "/root/.cache/rclone/bisync" ]; then
        echo "First run: Initializing with --resync..."
        rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --resync --create-empty-src-dirs
    else
        echo "Subsequent run: Syncing changes..."
        rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --create-empty-src-dirs
    fi

    echo "Sync complete. Sleeping for 30 seconds..."
    
    # Sleep with interrupt capability
    # This prevents tight looping and allows for graceful exit
    sleep 30 &
    SLEEP_PID=$!
    wait "$SLEEP_PID"
    SLEEP_PID=""
done

echo "[$(date)] Rclone service stopped."
