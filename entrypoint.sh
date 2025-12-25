#!/bin/sh

# Check if we've ever synced before by looking for rclone's internal cache
# If the cache doesn't exist, we run with --resync
if [ ! -d "/root/.cache/rclone/bisync" ]; then
    echo "First run: Initializing with --resync..."
    rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --resync --create-empty-src-dirs
else
    echo "Subsequent run: Syncing changes..."
    rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --create-empty-src-dirs
fi

# (Optional) If you want it to keep running every 5 minutes:
# while true; do
#   rclone bisync "gdrive:${GDRIVE_VAULT_PATH}" /data --verbose --create-empty-src-dirs
#   sleep 300
# done