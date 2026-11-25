#!/bin/sh
set -eu

# Ensure current UID exists in /etc/passwd for OpenSSH
if ! getent passwd "$(id -u)" >/dev/null 2>&1; then
    echo "charon:x:$(id -u):$(id -g):charon:${HOME:-/tmp}:/bin/sh" >> /etc/passwd 2>/dev/null || true
fi

exec python3 -m charon_runtime.git_backup "$@"
