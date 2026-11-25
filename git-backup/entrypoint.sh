#!/bin/sh
set -e

echo "[$(date)] Initializing Charon Git Backup scheduler..."

# Setup SSH keys in /tmp/.ssh for charon user
SSH_DIR="/tmp/.ssh"
mkdir -p "${SSH_DIR}"
if [ -f "/run/secrets/charon_ssh_key" ]; then
    tr -d '\r' < /run/secrets/charon_ssh_key > "${SSH_DIR}/id_rsa"
    echo "" >> "${SSH_DIR}/id_rsa"
fi
chmod 700 "${SSH_DIR}"
chmod 600 "${SSH_DIR}"/* 2>/dev/null || true

if ! ssh-keygen -F github.com -f "${SSH_DIR}/known_hosts" >/dev/null 2>&1; then
    echo "Adding github.com to known_hosts..."
    ssh-keyscan github.com >> "${SSH_DIR}/known_hosts" 2>/dev/null
fi
chmod 600 "${SSH_DIR}/known_hosts" 2>/dev/null || true
chown -R 1000:1000 "${SSH_DIR}"

touch /tmp/.git_ready
touch /tmp/charon_git_heartbeat
chown 1000:1000 /tmp/.git_ready /tmp/charon_git_heartbeat

# Run initial backup on container startup as user 1000
echo "[$(date)] Running initial startup git backup..."
cd /tmp && env HOME=/tmp su-exec charon /usr/local/bin/git-backup.sh || echo "[$(date)] Initial git backup finished with warnings, continuing to scheduler..."

# Install cron job daily (streams output to container stdout /proc/1/fd/1)
CRON_SCHEDULE="${GIT_BACKUP_CRON:-0 2 * * *}"
echo "[$(date)] Installing crontab schedule: ${CRON_SCHEDULE}..."
cat << CRON > /etc/crontabs/root
${CRON_SCHEDULE} cd /tmp && env HOME=/tmp su-exec charon /usr/local/bin/git-backup.sh > /proc/1/fd/1 2>&1
CRON

echo "[$(date)] Starting crond in foreground..."
exec crond -f -d 8
