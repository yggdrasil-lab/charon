#!/bin/sh
set -eu
exec python3 -m charon_runtime.bisync_recovery run "$@"
