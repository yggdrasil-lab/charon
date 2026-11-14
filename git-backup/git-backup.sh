#!/bin/sh
set -e

umask 0002

# Ensure current UID exists in /etc/passwd for OpenSSH
if ! getent passwd "$(id -u)" >/dev/null 2>&1; then
    echo "charon:x:$(id -u):$(id -g):charon:${HOME:-/tmp}:/bin/sh" >> /etc/passwd 2>/dev/null || true
fi

echo "Initializing centralized git backup run..."

TARGETS_CONFIG="${TARGETS_CONFIG:-/etc/charon-git/targets.json}"

notify_iris() {
    _SERVICE="${1:-git-backup}"
    _SEVERITY="${2:-error}"
    _TITLE="${3:-Alert}"
    _DETAILS="${4:-No details}"
    curl -s --max-time 5 --connect-timeout 3 -X POST http://iris:8000/notify/discord \
        -H "Content-Type: application/json" \
        -d "{\"service\":\"${_SERVICE}\",\"bot_name\":\"Charon\",\"severity\":\"${_SEVERITY}\",\"title\":\"${_TITLE}\",\"details\":\"${_DETAILS}\"}" >/dev/null 2>&1 || true
}

# Validate that shared git author environment variables are set
if [ -z "$GIT_USER_EMAIL" ] || [ -z "$GIT_USER_NAME" ]; then
    echo "Error: GIT_USER_EMAIL and GIT_USER_NAME must be set."
    exit 1
fi

if [ ! -f "${TARGETS_CONFIG}" ]; then
    echo "Error: Targets configuration file '${TARGETS_CONFIG}' not found."
    exit 1
fi

# Configure Git Author Identity
git config --global user.email "${GIT_USER_EMAIL}"
git config --global user.name "${GIT_USER_NAME}"
git config --global --add safe.directory "*"

SSH_DIR="${HOME:-/tmp}/.ssh"
export GIT_SSH_COMMAND="ssh -i ${SSH_DIR}/id_rsa -o UserKnownHostsFile=${SSH_DIR}/known_hosts -o StrictHostKeyChecking=accept-new -o BatchMode=yes"

backup_target() {
    _NAME="$1"
    _PATH="$2"
    _REPO_URL="$3"
    _BRANCH="${4:-main}"
    _PREFIX="$5"
    _RESOLVE_SUBMODULES="${6:-false}"
    _CLEAN_IGNORED="${7:-false}"

    echo "[$(date)] Processing target '${_NAME}' at '${_PATH}'..."

    if [ ! -d "${_PATH}" ]; then
        echo "Error: Target directory '${_PATH}' not found for '${_NAME}'."
        return 1
    fi

    if [ -z "${_REPO_URL}" ]; then
        echo "Error: Remote repository URL is empty for '${_NAME}'."
        return 1
    fi

    cd "${_PATH}"

    # Initialize repository if not already present
    if [ ! -d ".git" ]; then
        echo "No .git directory found in ${_PATH}. Initializing repository..."
        git init
        git remote add origin "${_REPO_URL}"
    fi

    # Ensure remote URL and shared permissions remain configured
    git remote set-url origin "${_REPO_URL}"
    git config core.sharedRepository all

    # Ensure local branch matches requested branch
    _CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [ "${_CURRENT_BRANCH}" != "${_BRANCH}" ]; then
        echo "Current branch is '${_CURRENT_BRANCH}'. Switching to '${_BRANCH}'..."
        if ! git show-ref --verify --quiet "refs/heads/${_BRANCH}"; then
            git checkout -b "${_BRANCH}" "origin/${_BRANCH}" 2>/dev/null || git checkout -b "${_BRANCH}"
        else
            git checkout "${_BRANCH}"
        fi
    fi

    # 1. Pull latest changes
    echo "Pulling latest changes for ${_NAME}..."
    if ! git pull --ff-only origin "${_BRANCH}"; then
        echo "Standard pull failed. Checking remote reachability..."
        if ! git fetch origin "${_BRANCH}"; then
            echo "Remote is unreachable or fetch failed for ${_NAME}."
            return 1
        fi

        if git merge --ff-only "origin/${_BRANCH}" >/dev/null 2>&1; then
            echo "Fast-forward merge succeeded after fetch for ${_NAME}."
        else
            echo "Branch diverged for ${_NAME}. Attempting rebase prioritizing local state..."
            git add .
            git commit --allow-empty -m "Pre-rebase save: $(date)"
            _PRE_REBASE_SAVE=true

            if [ -d ".git/rebase-merge" ]; then
                git rebase --abort || rm -rf .git/rebase-merge
            fi

            if ! git pull --rebase -X theirs origin "${_BRANCH}"; then
                echo "Rebase failed with strategy options. Attempting aggressive resolution..."
                git checkout --theirs . >/dev/null 2>&1 || true

                if [ "${_RESOLVE_SUBMODULES}" = "true" ]; then
                    _UNMERGED_SUBMODULES=$(git ls-files -u 2>/dev/null | grep '^160000' | cut -f2 | sort -u)
                    if [ -n "${_UNMERGED_SUBMODULES}" ]; then
                        echo "Resolving submodule conflicts favoring local working tree:"
                        echo "${_UNMERGED_SUBMODULES}" | while IFS= read -r sub; do
                            if [ -n "$sub" ]; then
                                echo "  Staging local pointer for submodule: $sub"
                                git add "$sub" >/dev/null 2>&1 || true
                            fi
                        done
                    fi
                fi

                git add -u >/dev/null 2>&1 || true
                git add . >/dev/null 2>&1 || true

                if GIT_EDITOR=true git rebase --continue; then
                    echo "Rebase conflict resolved favoring local state for ${_NAME}."
                    _PRE_REBASE_SAVE=false
                else
                    echo "Critical: Aggressive resolution failed for ${_NAME}. Aborting."
                    git rebase --abort || rm -rf .git/rebase-merge
                    if [ "${_PRE_REBASE_SAVE}" = "true" ]; then
                        _LAST_MSG=$(git log -1 --pretty=%s 2>/dev/null || echo "")
                        case "${_LAST_MSG}" in
                            "Pre-rebase save:"*)
                                echo "Rolling back temporary pre-rebase save commit..."
                                git reset HEAD~1
                                ;;
                        esac
                        _PRE_REBASE_SAVE=false
                    fi
                    return 1
                fi
            else
                echo "Rebase successful for ${_NAME}."
                _PRE_REBASE_SAVE=false
            fi
        fi
    fi

    # 2. Untrack files covered by newly updated .gitignore patterns
    if [ "${_CLEAN_IGNORED}" = "true" ] && [ -f ".gitignore" ]; then
        _IGNORED_FILES=$(git ls-files --cached | git check-ignore --stdin --no-index 2>/dev/null || true)
        if [ -n "${_IGNORED_FILES}" ]; then
            echo "Cleaning up tracked files now covered by .gitignore in ${_NAME}:"
            printf '%s\n' "${_IGNORED_FILES}" | sed 's/^/  /'
            printf '%s\n' "${_IGNORED_FILES}" | xargs -r git rm --cached --
        fi
    fi

    # 3. Stage changes
    git add .

    # 4. Commit if changes exist
    if git diff --staged --quiet --ignore-submodules=dirty; then
        echo "Working directory is clean for ${_NAME}. Proceeding to push..."
    else
        echo "Changes detected in ${_NAME}. Committing..."
        _ENV_PREFIX=""
        if [ -n "$ENV" ]; then
            _ENV_PREFIX="[${ENV}] "
        fi
        git commit -m "${_ENV_PREFIX}${_PREFIX}$(date)"
    fi

    # 5. Push to remote
    echo "Ensuring remote is in sync for ${_NAME}..."
    if ! git push origin "${_BRANCH}"; then
        echo "Push failed for ${_NAME}. Checking remote reachability..."
        if ! git fetch origin "${_BRANCH}"; then
            echo "Remote unreachable or fetch failed for ${_NAME}."
            return 1
        fi

        echo "Attempting final rebase for ${_NAME}..."
        if git pull --rebase origin "${_BRANCH}"; then
            if ! git push origin "${_BRANCH}"; then
                echo "Final push failed for ${_NAME}."
                return 1
            fi
        else
            echo "Rebase failed. Aborting rebase before resync for ${_NAME}..."
            git rebase --abort >/dev/null 2>&1 || rm -rf .git/rebase-merge
            echo "Performing soft reset to align with remote for ${_NAME}..."
            git fetch origin "${_BRANCH}"
            git reset --soft "origin/${_BRANCH}"
            git add .
            git commit -m "Consolidated Backup: Resync after divergence $(date)"
            if ! (git push origin "${_BRANCH}" || git push --force origin "${_BRANCH}"); then
                echo "Consolidated push failed for ${_NAME}."
                return 1
            fi
        fi
    fi

    echo "Remote is in sync for ${_NAME}."
    return 0
}

echo "[$(date)] --- Starting centralized git backup run ---"

TOTAL_TARGETS=0
FAILED_TARGETS=0
FAILURES_DETAILS=""

# Read each target from targets.json
jq -c '.[]' "${TARGETS_CONFIG}" > /tmp/targets_to_run.jsonl

while IFS= read -r target_json; do
    [ -z "$target_json" ] && continue
    TOTAL_TARGETS=$((TOTAL_TARGETS + 1))

    TARGET_NAME=$(echo "$target_json" | jq -r '.name')
    TARGET_PATH=$(echo "$target_json" | jq -r '.path')
    TARGET_REPO_ENV=$(echo "$target_json" | jq -r '.repo_url_env')
    TARGET_BRANCH=$(echo "$target_json" | jq -r '.branch // "main"')
    TARGET_PREFIX=$(echo "$target_json" | jq -r '.commit_prefix // "Daily Backup: "')
    TARGET_RESOLVE_SUB=$(echo "$target_json" | jq -r '.resolve_submodules // false')
    TARGET_CLEAN_IGN=$(echo "$target_json" | jq -r '.clean_ignored // false')

    # Dynamically resolve repository URL from environment variable
    TARGET_REPO_URL=$(eval echo "\$$TARGET_REPO_ENV")

    if ! backup_target "$TARGET_NAME" "$TARGET_PATH" "$TARGET_REPO_URL" "$TARGET_BRANCH" "$TARGET_PREFIX" "$TARGET_RESOLVE_SUB" "$TARGET_CLEAN_IGN"; then
        FAILED_TARGETS=$((FAILED_TARGETS + 1))
        FAILURES_DETAILS="${FAILURES_DETAILS}Target '${TARGET_NAME}' failed backup. "
    fi
done < /tmp/targets_to_run.jsonl

touch /tmp/charon_git_heartbeat

if [ "$FAILED_TARGETS" -gt 0 ]; then
    echo "[$(date)] Git backup completed with ${FAILED_TARGETS}/${TOTAL_TARGETS} failures."
    notify_iris "git-backup" "error" "Git Backup Failed" "${FAILURES_DETAILS}"
    exit 1
else
    echo "[$(date)] All ${TOTAL_TARGETS} git backup targets completed successfully."
fi
