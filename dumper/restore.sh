#!/bin/sh
set -eu
exec python3 -m charon_runtime.snapshot_lifecycle "$@"
