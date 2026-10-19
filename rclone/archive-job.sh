#!/bin/sh
set -e

log() {
    echo "[$(date)] $1"
}

NODE="${NODE_HOSTNAME:-$(hostname)}"
CONF="/tmp/rclone.conf"

notify_iris() {
    _SEVERITY="$1"
    _TITLE="$2"
    _DETAILS="$3"
    curl -s --max-time 5 --connect-timeout 3 -X POST http://iris:8000/notify/discord \
        -H "Content-Type: application/json" \
        -d "{\"service\":\"charon-archive\",\"bot_name\":\"Charon\",\"severity\":\"${_SEVERITY}\",\"title\":\"${_TITLE}\",\"details\":\"${_DETAILS}\",\"node\":\"${NODE}\"}" >/dev/null 2>&1 || true
}

case "$1" in
    copy)
        log "Starting nightly rclone copy for node ${NODE}..."
        if rclone copy /data "gdrive:${GDRIVE_BACKUP_PATH}/${NODE}" --config "$CONF" --verbose; then
            log "Nightly rclone copy succeeded for node ${NODE}."
            touch /tmp/charon_archive_heartbeat
        else
            EXIT_CODE=$?
            log "ERROR: Nightly rclone copy failed with exit code ${EXIT_CODE}."
            notify_iris "error" "Nightly Backup Failed" "rclone copy /data to gdrive:${GDRIVE_BACKUP_PATH}/${NODE} failed with exit code ${EXIT_CODE}."
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
            notify_iris "warning" "Backup Retention Prune Failed" "rclone delete (>90d) failed for node ${NODE} with exit code ${EXIT_CODE}."
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
