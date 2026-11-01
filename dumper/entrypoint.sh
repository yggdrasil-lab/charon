#!/bin/sh
set -e

NODE="${NODE_HOSTNAME:-$(hostname)}"
export NODE

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

echo "[$(date)] Initializing Charon Dumper on node ${NODE}..."
touch /tmp/charon_dumper_heartbeat

# Run initial dump on startup
echo "[$(date)] Running initial startup database backup..."
/usr/local/bin/dumper.sh || echo "[$(date)] Initial backup finished with warnings, continuing to scheduler..."

# Install cron job at 02:00 NZDT daily (streams output to container stdout /proc/1/fd/1)
CRON_SCHEDULE="${DUMPER_CRON:-0 2 * * *}"
echo "[$(date)] Installing crontab schedule: ${CRON_SCHEDULE}..."
cat << CRON > /etc/crontabs/root
${CRON_SCHEDULE} /usr/local/bin/dumper.sh > /proc/1/fd/1 2>&1
CRON

echo "[$(date)] Starting crond in foreground..."
exec crond -f -d 8
