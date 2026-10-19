#!/bin/sh
set -e

log() {
    echo "[$(date)] $1"
}

NODE="${NODE_HOSTNAME:-$(hostname)}"
CONF="/tmp/rclone.conf"

case "$1" in
    copy)
        log "Starting nightly rclone copy for node ${NODE}..."
        if rclone copy /data "gdrive:${GDRIVE_BACKUP_PATH}/${NODE}" --config "$CONF" --verbose; then
            log "Nightly rclone copy succeeded for node ${NODE}."
            touch /tmp/charon_archive_heartbeat
        else
            EXIT_CODE=$?
            log "ERROR: Nightly rclone copy failed with exit code ${EXIT_CODE}."
            /notify-discord.sh "charon-archive" "error" "Nightly Backup Failed" "rclone copy /data to gdrive:${GDRIVE_BACKUP_PATH}/${NODE} failed with exit code ${EXIT_CODE}."
            exit $EXIT_CODE
        fi
        ;;
    delete)
        log "Starting nightly rclone prune (>90d) for node ${NODE}..."
        if rclone delete "gdrive:${GDRIVE_BACKUP_PATH}/${NODE}" --min-age 90d --config "$CONF" --verbose; then
            log "Nightly rclone prune succeeded for node ${NODE}."
            touch /tmp/charon_archive_heartbeat
        else
            EXIT_CODE=$?
            log "ERROR: Nightly rclone prune failed with exit code ${EXIT_CODE}."
            /notify-discord.sh "charon-archive" "warning" "Backup Retention Prune Failed" "rclone delete (>90d) failed for node ${NODE} with exit code ${EXIT_CODE}."
            exit $EXIT_CODE
        fi
        ;;
    setup-cron)
        log "Initializing archive environment..."
        . /setup.sh
        touch /tmp/charon_archive_heartbeat

        # Create crontab: redirect stdout and stderr to /proc/1/fd/1 so docker service logs sees it
        log "Installing crontab..."
        cat << 'CRON' > /etc/crontabs/root
0 3 * * * /archive-job.sh copy > /proc/1/fd/1 2>&1
0 4 * * * /archive-job.sh delete > /proc/1/fd/1 2>&1
CRON
        log "Starting crond in foreground..."
        exec crond -f -d 8
        ;;
    *)
        echo "Usage: $0 {copy|delete|setup-cron}"
        exit 1
        ;;
esac
