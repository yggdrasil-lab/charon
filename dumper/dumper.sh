#!/bin/sh
set -e

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $1"
}

NODE="${NODE_HOSTNAME:-$(hostname)}"
BACKUP_BASE="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
TARGETS_CONFIG="${TARGETS_CONFIG:-/etc/charon-dumper/targets.json}"

notify_iris() {
  _SEVERITY="$1"
  _TITLE="$2"
  _DETAILS="$3"

  _PAYLOAD="$(jq -n \
    --arg service "charon-dumper" \
    --arg bot_name "Charon" \
    --arg severity "$_SEVERITY" \
    --arg title "$_TITLE" \
    --arg details "$_DETAILS" \
    --arg node "$NODE" \
    '{service: $service, bot_name: $bot_name, severity: $severity, title: $title, details: $details, node: $node}')"

  curl -s --max-time 5 --connect-timeout 3 -X POST http://iris:8000/notify/discord \
    -H "Content-Type: application/json" \
    -d "$_PAYLOAD" >/dev/null 2>&1 || true
}

SUCCESS_COUNT=0
FAILURE_COUNT=0
SUMMARY_MSGS=""

backup_postgres() {
  _TARGET="$1"
  _HOST="$2"
  _PORT="$3"
  _USER="$4"
  _PASS="$5"
  _DB="$6"
  _DIR="${BACKUP_BASE}/${_TARGET}"
  _BASE_NAME="$(basename "${_TARGET}")"

  log "Processing PostgreSQL target '${_TARGET}' (${_HOST}:${_PORT}/${_DB})..."
  mkdir -p "${_DIR}"

  if ! PGPASSWORD="${_PASS}" pg_isready -h "${_HOST}" -p "${_PORT}" -U "${_USER}" -d "${_DB}" >/dev/null 2>&1; then
    log "ERROR: Target '${_TARGET}' is unreachable on ${_HOST}:${_PORT}."
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}
- ${_TARGET}: Unreachable on ${_HOST}:${_PORT}"
    return 1
  fi

  _FILE="${_DIR}/${_BASE_NAME}_db_${TIMESTAMP}.dump"
  if PGPASSWORD="${_PASS}" pg_dump -h "${_HOST}" -p "${_PORT}" -U "${_USER}" -d "${_DB}" -F c -f "${_FILE}"; then
    _SIZE="$(ls -lh "${_FILE}" | awk '{print $5}')"
    log "SUCCESS: Dumped '${_TARGET}' (${_SIZE}) to ${_FILE}"
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}
- ${_TARGET}: ${_SIZE} (Postgres)"
  else
    log "ERROR: pg_dump failed for '${_TARGET}'."
    rm -f "${_FILE}"
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}
- ${_TARGET}: pg_dump failed"
    return 1
  fi

  if [ -n "${KEEP_DAYS}" ] && [ "${KEEP_DAYS}" -gt 0 ]; then
    find "${_DIR}" -name "${_BASE_NAME}_db_*.dump" -type f -mtime +"${KEEP_DAYS}" -delete
  fi
  return 0
}

backup_sqlite() {
  _TARGET="$1"
  _SOURCE="$2"
  _DIR="${BACKUP_BASE}/${_TARGET}"
  _BASE_NAME="$(basename "${_TARGET}")"

  log "Processing SQLite target '${_TARGET}' (${_SOURCE})..."
  mkdir -p "${_DIR}"

  if [ ! -f "${_SOURCE}" ]; then
    log "WARNING: Source file '${_SOURCE}' does not exist for '${_TARGET}'. Skipping."
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}
- ${_TARGET}: File not found (${_SOURCE})"
    return 1
  fi

  _FILE="${_DIR}/${_BASE_NAME}_db_${TIMESTAMP}.sqlite"
  if sqlite3 "${_SOURCE}" ".backup '${_FILE}'"; then
    _SIZE="$(ls -lh "${_FILE}" | awk '{print $5}')"
    log "SUCCESS: Backed up '${_TARGET}' (${_SIZE}) to ${_FILE}"
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}
- ${_TARGET}: ${_SIZE} (SQLite)"
  else
    log "ERROR: sqlite3 backup failed for '${_TARGET}'."
    rm -f "${_FILE}"
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}
- ${_TARGET}: sqlite3 backup failed"
    return 1
  fi

  if [ -n "${KEEP_DAYS}" ] && [ "${KEEP_DAYS}" -gt 0 ]; then
    find "${_DIR}" -name "${_BASE_NAME}_db_*.sqlite" -type f -mtime +"${KEEP_DAYS}" -delete
  fi
  return 0
}

log "=== Starting Centralized Database Backup Run on node ${NODE} ==="

if [ ! -f "${TARGETS_CONFIG}" ]; then
  log "ERROR: Targets configuration file '${TARGETS_CONFIG}' not found."
  exit 1
fi

jq -c --arg node "$NODE" '.[] | select(.node == null or .node == "*" or .node == $node)' "${TARGETS_CONFIG}" > /tmp/targets_to_run.jsonl

TARGET_COUNT=0
while IFS= read -r target; do
  [ -z "$target" ] && continue
  TARGET_COUNT=$((TARGET_COUNT + 1))
  _NAME="$(echo "$target" | jq -r '.name')"
  _TYPE="$(echo "$target" | jq -r '.type')"

  if [ "$_TYPE" = "postgres" ]; then
    _HOST="$(echo "$target" | jq -r '.host // "localhost"')"
    _PORT="$(echo "$target" | jq -r '.port // 5432')"
    _USER="$(echo "$target" | jq -r '.user // "postgres"')"
    _DB="$(echo "$target" | jq -r '.database // "postgres"')"
    _PASS_FILE="$(echo "$target" | jq -r '.password_file // empty')"
    _PASS_ENV="$(echo "$target" | jq -r '.password_env // empty')"
    _PASS_DEF="$(echo "$target" | jq -r '.password_default // empty')"

    _PASS=""
    if [ -n "$_PASS_FILE" ] && [ -f "$_PASS_FILE" ]; then
      _PASS="$(cat "$_PASS_FILE")"
    elif [ -n "$_PASS_ENV" ]; then
      _PASS="$(eval "echo \"\${$_PASS_ENV:-}\"")"
    fi
    if [ -z "$_PASS" ] && [ -n "$_PASS_DEF" ]; then
      _PASS="$_PASS_DEF"
    fi

    backup_postgres "$_NAME" "$_HOST" "$_PORT" "$_USER" "$_PASS" "$_DB" || true

  elif [ "$_TYPE" = "sqlite" ]; then
    _SOURCE="$(echo "$target" | jq -r '.source')"
    backup_sqlite "$_NAME" "$_SOURCE" || true
  else
    log "WARNING: Unknown target type '${_TYPE}' for target '${_NAME}'. Skipping."
  fi
done < /tmp/targets_to_run.jsonl
rm -f /tmp/targets_to_run.jsonl

touch /tmp/charon_dumper_heartbeat

log "=== Centralized Database Backup Run Finished ==="
log "Results: ${SUCCESS_COUNT} succeeded, ${FAILURE_COUNT} failed out of ${TARGET_COUNT} targets."

if [ "${TARGET_COUNT}" -eq 0 ]; then
  log "No backup targets configured for node '${NODE}'."
  exit 0
fi

if [ "${FAILURE_COUNT}" -gt 0 ]; then
  notify_iris "warning" "Daily Database Backup Completed with Issues" "Node: ${NODE}
Succeeded: ${SUCCESS_COUNT}, Failed: ${FAILURE_COUNT}

Targets:${SUMMARY_MSGS}"
else
  notify_iris "info" "Daily Database Backup Succeeded" "Node: ${NODE}
All ${SUCCESS_COUNT} databases dumped successfully.

Targets:${SUMMARY_MSGS}

Local retention pruned > ${KEEP_DAYS} days. Cloud replication scheduled for 03:00 NZDT."
fi
