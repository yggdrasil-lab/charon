#!/bin/sh
set -euo pipefail

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $1"
}

NODE="${NODE_HOSTNAME:-$(hostname)}"
BACKUP_BASE="${BACKUP_DIR:-/backups}"
TARGETS_CONFIG="${TARGETS_CONFIG:-/etc/charon-dumper/targets.json}"

if [ ! -f "${TARGETS_CONFIG}" ]; then
  log "ERROR: Configuration file '${TARGETS_CONFIG}' not found."
  exit 1
fi

list_backups() {
  _TARGET="${1:-}"
  if [ -n "$_TARGET" ]; then
    _CONFIG="$(jq -c --arg name "$_TARGET" '.[] | select(.name == $name)' "${TARGETS_CONFIG}" | head -n 1)"
    _TYPE=""
    if [ -n "$_CONFIG" ]; then
      _TYPE="$(echo "$_CONFIG" | jq -r '.type')"
    fi
    _DIR="${BACKUP_BASE}/${_TARGET}"
    if [ ! -d "$_DIR" ]; then
      echo "No backup directory found for '${_TARGET}' at ${_DIR}."
      return 0
    fi
    _PATTERN="*"
    case "$_TYPE" in
      postgres) _PATTERN="*.dump" ;;
      sqlite)   _PATTERN="*.sqlite" ;;
      archive)  _PATTERN="*.tar.gz" ;;
    esac
    echo "=== Available backups for '${_TARGET}' (${_DIR}) ==="
    find "$_DIR" -maxdepth 1 -type f -name "$_PATTERN" -exec ls -lh {} + | awk '{print $9, "(" $5 ")", "Modified: " $6, $7, $8}'
  else
    echo "=== Backup Inventory on node '${NODE}' (${BACKUP_BASE}) ==="
    jq -c --arg node "$NODE" '.[] | select(.node == null or .node == "*" or .node == $node)' "${TARGETS_CONFIG}" | while read -r t; do
      _NAME="$(echo "$t" | jq -r '.name')"
      _TYPE="$(echo "$t" | jq -r '.type')"
      _DIR="${BACKUP_BASE}/${_NAME}"
      _PATTERN="*"
      case "$_TYPE" in
        postgres) _PATTERN="*.dump" ;;
        sqlite)   _PATTERN="*.sqlite" ;;
        archive)  _PATTERN="*.tar.gz" ;;
      esac
      if [ -d "$_DIR" ]; then
        _COUNT="$(find "$_DIR" -maxdepth 1 -type f -name "$_PATTERN" | wc -l)"
        _LATEST="$(find "$_DIR" -maxdepth 1 -type f -name "$_PATTERN" -exec ls -1t {} + 2>/dev/null | head -n 1)"
        if [ -n "$_LATEST" ]; then
          _SIZE="$(ls -lh "$_LATEST" | awk '{print $5}')"
          printf "  * %-20s [%s] - %d snapshot(s), Latest: %s (%s)\n" "$_NAME" "$_TYPE" "$_COUNT" "$(basename "$_LATEST")" "$_SIZE"
        else
          printf "  * %-20s [%s] - 0 snapshots found\n" "$_NAME" "$_TYPE"
        fi
      else
        printf "  * %-20s [%s] - No directory at %s\n" "$_NAME" "$_TYPE" "$_DIR"
      fi
    done
  fi
}

restore_target() {
  _TARGET="${1:-}"
  _REQ_FILE="${2:-}"
  _CONFIRM_FLAG="${3:-}"

  _CONFIG="$(jq -c --arg name "$_TARGET" '.[] | select(.name == $name)' "${TARGETS_CONFIG}" | head -n 1)"
  if [ -z "$_CONFIG" ]; then
    log "ERROR: Target '${_TARGET}' not found in ${TARGETS_CONFIG}."
    exit 1
  fi

  _TYPE="$(echo "$_CONFIG" | jq -r '.type')"
  _DIR="${BACKUP_BASE}/${_TARGET}"

  if [ ! -d "$_DIR" ]; then
    log "ERROR: Backup directory '${_DIR}' does not exist."
    exit 1
  fi

  _PATTERN="*"
  case "$_TYPE" in
    postgres) _PATTERN="*.dump" ;;
    sqlite)   _PATTERN="*.sqlite" ;;
    archive)  _PATTERN="*.tar.gz" ;;
  esac

  _FILE=""
  if [ -n "$_REQ_FILE" ] && [ "$_REQ_FILE" != "latest" ] && [ "$_REQ_FILE" != "--confirm" ]; then
    if [ -f "$_REQ_FILE" ]; then
      _FILE="$_REQ_FILE"
    elif [ -f "${_DIR}/${_REQ_FILE}" ]; then
      _FILE="${_DIR}/${_REQ_FILE}"
    else
      log "ERROR: Specified backup file '${_REQ_FILE}' not found."
      exit 1
    fi
  else
    _FILE="$(find "$_DIR" -maxdepth 1 -type f -name "$_PATTERN" -exec ls -1t {} + 2>/dev/null | head -n 1)"
    if [ -z "$_FILE" ]; then
      log "ERROR: No matching snapshot files (${_PATTERN}) found in '${_DIR}'."
      exit 1
    fi
  fi

  if [ "$_REQ_FILE" = "--confirm" ] || [ "$_CONFIRM_FLAG" = "--confirm" ]; then
    _PROCEED="yes"
  else
    echo "======================================================================"
    echo "RESTORE WARNING: Target '${_TARGET}'"
    echo "Using backup:    ${_FILE}"
    echo "Target Type:     ${_TYPE}"
    echo "======================================================================"
    echo "This operation will overwrite current data for target '${_TARGET}'."
    printf "Are you sure you want to proceed? (yes/no): "
    read -r _PROCEED
  fi

  if [ "$_PROCEED" != "yes" ]; then
    log "Restore cancelled by user."
    exit 0
  fi

  log "Starting restore for '${_TARGET}' from ${_FILE}..."

  if [ "$_TYPE" = "postgres" ]; then
    _HOST="$(echo "$_CONFIG" | jq -r '.host // "localhost"')"
    _PORT="$(echo "$_CONFIG" | jq -r '.port // 5432')"
    _USER="$(echo "$_CONFIG" | jq -r '.user // "postgres"')"
    _DB="$(echo "$_CONFIG" | jq -r '.database // "postgres"')"
    _PASS_FILE="$(echo "$_CONFIG" | jq -r '.password_file // empty')"
    _PASS_ENV="$(echo "$_CONFIG" | jq -r '.password_env // empty')"
    _PASS_DEF="$(echo "$_CONFIG" | jq -r '.password_default // empty')"

    _PASS=""
    if [ -n "$_PASS_FILE" ] && [ -f "$_PASS_FILE" ]; then
      _PASS="$(cat "$_PASS_FILE")"
    elif [ -n "$_PASS_ENV" ]; then
      _PASS="$(eval "echo \"\${$_PASS_ENV:-}\"")"
    fi
    if [ -z "$_PASS" ] && [ -n "$_PASS_DEF" ]; then
      _PASS="$_PASS_DEF"
    fi

    if ! PGPASSWORD="${_PASS}" pg_isready -h "${_HOST}" -p "${_PORT}" -U "${_USER}" -d "${_DB}" >/dev/null 2>&1; then
      log "ERROR: Target '${_TARGET}' is unreachable on ${_HOST}:${_PORT}."
      exit 1
    fi

    log "Executing pg_restore on ${_HOST}:${_PORT}/${_DB}..."
    PGPASSWORD="${_PASS}" pg_restore -h "${_HOST}" -p "${_PORT}" -U "${_USER}" -d "${_DB}" --clean --if-exists "${_FILE}"
    log "SUCCESS: PostgreSQL restore complete for '${_TARGET}'."

  elif [ "$_TYPE" = "sqlite" ]; then
    _SOURCE="$(echo "$_CONFIG" | jq -r '.source')"
    log "Restoring SQLite database to ${_SOURCE}..."

    mkdir -p "$(dirname "${_SOURCE}")"
    if [ -f "${_SOURCE}" ]; then
      cp -f "${_SOURCE}" "${_SOURCE}.pre_restore_bak"
      log "Created safety snapshot at ${_SOURCE}.pre_restore_bak"
    fi

    if sqlite3 "${_SOURCE}" ".restore '${_FILE}'"; then
      _CHECK="$(sqlite3 "${_SOURCE}" "PRAGMA integrity_check;" 2>&1 || true)"
      if [ "$_CHECK" = "ok" ]; then
        log "Integrity check passed (ok)."
        rm -f "${_SOURCE}.pre_restore_bak"
        log "SUCCESS: SQLite restore complete for '${_TARGET}'."
      else
        log "ERROR: Integrity check failed: ${_CHECK}. Rolling back..."
        if [ -f "${_SOURCE}.pre_restore_bak" ]; then
          mv -f "${_SOURCE}.pre_restore_bak" "${_SOURCE}"
        fi
        exit 1
      fi
    else
      log "ERROR: sqlite3 restore failed. Rolling back..."
      if [ -f "${_SOURCE}.pre_restore_bak" ]; then
        mv -f "${_SOURCE}.pre_restore_bak" "${_SOURCE}"
      fi
      exit 1
    fi

  elif [ "$_TYPE" = "archive" ]; then
    _SOURCE_DIR="$(echo "$_CONFIG" | jq -r '.source_dir')"
    log "Extracting archive to ${_SOURCE_DIR}..."
    mkdir -p "${_SOURCE_DIR}"
    tar -xzf "${_FILE}" -C "${_SOURCE_DIR}"
    log "SUCCESS: Configuration archive extracted to ${_SOURCE_DIR}."

  else
    log "ERROR: Unsupported target type '${_TYPE}'."
    exit 1
  fi
}

verify_single_target() {
  _TARGET="$1"
  _CONFIG="$(jq -c --arg name "$_TARGET" '.[] | select(.name == $name)' "${TARGETS_CONFIG}" | head -n 1)"
  if [ -z "$_CONFIG" ]; then
    printf "  [FAIL] %-20s - Target not found in config\n" "$_TARGET"
    return 1
  fi

  _TYPE="$(echo "$_CONFIG" | jq -r '.type')"
  _DIR="${BACKUP_BASE}/${_TARGET}"
  if [ ! -d "$_DIR" ]; then
    printf "  [FAIL] %-20s - Directory not found (%s)\n" "$_TARGET" "$_DIR"
    return 1
  fi

  _PATTERN="*"
  case "$_TYPE" in
    postgres) _PATTERN="*.dump" ;;
    sqlite)   _PATTERN="*.sqlite" ;;
    archive)  _PATTERN="*.tar.gz" ;;
  esac

  _FILE="$(find "$_DIR" -maxdepth 1 -type f -name "$_PATTERN" -exec ls -1t {} + 2>/dev/null | head -n 1)"
  if [ -z "$_FILE" ]; then
    printf "  [FAIL] %-20s - No matching snapshots found (%s)\n" "$_TARGET" "$_PATTERN"
    return 1
  fi

  _SIZE="$(ls -lh "$_FILE" | awk '{print $5}')"
  _FNAME="$(basename "$_FILE")"

  if [ "$_TYPE" = "postgres" ]; then
    if pg_restore --list "$_FILE" > /tmp/verify_pg.toc 2>&1; then
      _ENTRIES="$(grep -c "^[0-9]" /tmp/verify_pg.toc 2>/dev/null || true)"
      rm -f /tmp/verify_pg.toc
      printf "  [PASS] %-20s [postgres] - %s (%s, %s TOC entries)\n" "$_TARGET" "$_FNAME" "$_SIZE" "$_ENTRIES"
      return 0
    else
      rm -f /tmp/verify_pg.toc
      printf "  [FAIL] %-20s [postgres] - pg_restore TOC verification failed on %s\n" "$_TARGET" "$_FNAME"
      return 1
    fi

  elif [ "$_TYPE" = "sqlite" ]; then
    _SANDBOX="/tmp/sandbox_$$.sqlite"
    rm -f "$_SANDBOX"
    if sqlite3 "$_SANDBOX" ".restore '$_FILE'" >/dev/null 2>&1; then
      _CHECK="$(sqlite3 "$_SANDBOX" "PRAGMA integrity_check;" 2>&1 || true)"
      if [ "$_CHECK" = "ok" ]; then
        _TABLES="$(sqlite3 "$_SANDBOX" "SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || true)"
        rm -f "$_SANDBOX"
        printf "  [PASS] %-20s [sqlite]   - %s (%s, %s tables, integrity OK)\n" "$_TARGET" "$_FNAME" "$_SIZE" "$_TABLES"
        return 0
      else
        rm -f "$_SANDBOX"
        printf "  [FAIL] %-20s [sqlite]   - Integrity check failed (%s) on %s\n" "$_TARGET" "$_CHECK" "$_FNAME"
        return 1
      fi
    else
      rm -f "$_SANDBOX"
      printf "  [FAIL] %-20s [sqlite]   - sqlite3 .restore failed on %s\n" "$_TARGET" "$_FNAME"
      return 1
    fi

  elif [ "$_TYPE" = "archive" ]; then
    if tar -tzf "$_FILE" > /tmp/verify_tar.lst 2>&1; then
      _FILES="$(wc -l < /tmp/verify_tar.lst 2>/dev/null | tr -d ' ')"
      rm -f /tmp/verify_tar.lst
      printf "  [PASS] %-20s [archive]  - %s (%s, %s entries)\n" "$_TARGET" "$_FNAME" "$_SIZE" "$_FILES"
      return 0
    else
      rm -f /tmp/verify_tar.lst
      printf "  [FAIL] %-20s [archive]  - tar decompression failed on %s\n" "$_TARGET" "$_FNAME"
      return 1
    fi
  fi
}

verify_backups() {
  _TARGET="${1:-}"
  if [ -n "$_TARGET" ] && [ "$_TARGET" != "--all" ]; then
    echo "=== Verifying Backup for Target '${_TARGET}' on node '${NODE}' ==="
    verify_single_target "$_TARGET"
  else
    echo "=== Auditing Backup Snapshot Integrity on node '${NODE}' ==="
    _TOTAL=0
    _PASSED=0
    _FAILED=0
    for t in $(jq -r --arg node "$NODE" '.[] | select(.node == null or .node == "*" or .node == $node) | .name' "${TARGETS_CONFIG}"); do
      _TOTAL=$((_TOTAL + 1))
      if verify_single_target "$t"; then
        _PASSED=$((_PASSED + 1))
      else
        _FAILED=$((_FAILED + 1))
      fi
    done
    echo "----------------------------------------------------------------------"
    echo "Integrity Audit Complete: ${_PASSED} passed, ${_FAILED} failed out of ${_TOTAL} targets."
    if [ "$_FAILED" -gt 0 ]; then
      return 1
    fi
  fi
}

case "${1:-}" in
  list|ls)
    list_backups "${2:-}"
    ;;
  verify|check)
    verify_backups "${2:-}"
    ;;
  restore)
    if [ -z "${2:-}" ]; then
      echo "Usage: $0 restore <target> [snapshot_file|latest] [--confirm]"
      exit 1
    fi
    restore_target "$2" "${3:-}" "${4:-}"
    ;;
  *)
    echo "Charon Database & Config Restore Utility"
    echo ""
    echo "Usage:"
    echo "  $0 list                               List all targets and available snapshots"
    echo "  $0 list <target>                      List snapshots for a specific target"
    echo "  $0 verify [target|--all]              Verify snapshot integrity non-destructively"
    echo "  $0 restore <target>                   Restore latest snapshot for target (prompts confirmation)"
    echo "  $0 restore <target> <file>            Restore specific snapshot file"
    echo "  $0 restore <target> latest --confirm  Restore non-interactively"
    echo ""
    exit 1
    ;;
esac
