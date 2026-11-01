#!/bin/sh
set -e

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $1"
}

NODE="${NODE_HOSTNAME:-$(hostname)}"
BACKUP_BASE="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"

notify_iris() {
  _SEVERITY="$1"
  _TITLE="$2"
  _DETAILS="$3"
  curl -s --max-time 5 --connect-timeout 3 -X POST http://iris:8000/notify/discord \
    -H "Content-Type: application/json" \
    -d "{\"service\":\"charon-dumper\",\"bot_name\":\"Charon\",\"severity\":\"${_SEVERITY}\",\"title\":\"${_TITLE}\",\"details\":\"${_DETAILS}\",\"node\":\"${NODE}\"}" >/dev/null 2>&1 || true
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

  log "Processing PostgreSQL target '${_TARGET}' (${_HOST}:${_PORT}/${_DB})..."
  mkdir -p "${_DIR}"

  if ! PGPASSWORD="${_PASS}" pg_isready -h "${_HOST}" -p "${_PORT}" -U "${_USER}" -d "${_DB}" >/dev/null 2>&1; then
    log "ERROR: Target '${_TARGET}' is unreachable on ${_HOST}:${_PORT}."
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}\n- ${_TARGET}: Unreachable on ${_HOST}:${_PORT}"
    return 1
  fi

  _FILE="${_DIR}/${_TARGET}_db_${TIMESTAMP}.dump"
  if PGPASSWORD="${_PASS}" pg_dump -h "${_HOST}" -p "${_PORT}" -U "${_USER}" -d "${_DB}" -F c -f "${_FILE}"; then
    _SIZE="$(ls -lh "${_FILE}" | awk '{print $5}')"
    log "SUCCESS: Dumped '${_TARGET}' (${_SIZE}) to ${_FILE}"
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}\n- ${_TARGET}: ${_SIZE} (Postgres)"
  else
    log "ERROR: pg_dump failed for '${_TARGET}'."
    rm -f "${_FILE}"
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}\n- ${_TARGET}: pg_dump failed"
    return 1
  fi

  if [ -n "${KEEP_DAYS}" ] && [ "${KEEP_DAYS}" -gt 0 ]; then
    find "${_DIR}" -name "${_TARGET}_db_*.dump" -type f -mtime +"${KEEP_DAYS}" -delete
  fi
  return 0
}

backup_sqlite() {
  _TARGET="$1"
  _SOURCE="$2"
  _DIR="${BACKUP_BASE}/${_TARGET}"

  log "Processing SQLite target '${_TARGET}' (${_SOURCE})..."
  mkdir -p "${_DIR}"

  if [ ! -f "${_SOURCE}" ]; then
    log "WARNING: Source file '${_SOURCE}' does not exist for '${_TARGET}'. Skipping."
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}\n- ${_TARGET}: File not found (${_SOURCE})"
    return 1
  fi

  _FILE="${_DIR}/$(basename "${_TARGET}")_db_${TIMESTAMP}.sqlite"
  if sqlite3 "${_SOURCE}" ".backup '${_FILE}'"; then
    _SIZE="$(ls -lh "${_FILE}" | awk '{print $5}')"
    log "SUCCESS: Backed up '${_TARGET}' (${_SIZE}) to ${_FILE}"
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}\n- ${_TARGET}: ${_SIZE} (SQLite)"
  else
    log "ERROR: sqlite3 backup failed for '${_TARGET}'."
    rm -f "${_FILE}"
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    SUMMARY_MSGS="${SUMMARY_MSGS}\n- ${_TARGET}: sqlite3 backup failed"
    return 1
  fi

  if [ -n "${KEEP_DAYS}" ] && [ "${KEEP_DAYS}" -gt 0 ]; then
    find "${_DIR}" -name "$(basename "${_TARGET}")_db_*.sqlite" -type f -mtime +"${KEEP_DAYS}" -delete
  fi
  return 0
}

log "=== Starting Centralized Database Backup Run ==="

# 1. Eir Health Telemetry DB
backup_postgres "eir" "${EIR_DB_HOST:-eir-db}" "${EIR_DB_PORT:-5432}" "${EIR_DB_USER:-eir}" "${EIR_DB_PASSWORD:-eir}" "${EIR_DB_NAME:-eir}" || true

# 2. Kratos Resistance Training DB
backup_postgres "kratos" "${KRATOS_DB_HOST:-kratos-db}" "${KRATOS_DB_PORT:-5432}" "${KRATOS_DB_USER:-kratos}" "${KRATOS_DB_PASSWORD:-kratos}" "${KRATOS_DB_NAME:-kratos}" || true

# 3. Odin Mnemosyne Agent Memory DB
backup_sqlite "odin/mnemosyne" "/data/mnemosyne/mnemosyne.db" || true

touch /tmp/charon_dumper_heartbeat

log "=== Centralized Database Backup Run Finished ==="
log "Results: ${SUCCESS_COUNT} succeeded, ${FAILURE_COUNT} failed."

if [ "${FAILURE_COUNT}" -gt 0 ]; then
  notify_iris "warning" "Daily Database Backup Completed with Issues" "Node: ${NODE}\nSucceeded: ${SUCCESS_COUNT}, Failed: ${FAILURE_COUNT}\n\nTargets:${SUMMARY_MSGS}"
else
  notify_iris "info" "Daily Database Backup Succeeded" "Node: ${NODE}\nAll ${SUCCESS_COUNT} databases dumped successfully.\n\nTargets:${SUMMARY_MSGS}\n\nLocal retention pruned > ${KEEP_DAYS} days. Cloud replication scheduled for 03:00 NZDT."
fi
