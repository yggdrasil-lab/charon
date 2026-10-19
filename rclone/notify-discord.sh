#!/bin/sh
# Usage: notify-discord.sh "<SERVICE>" "<SEVERITY>" "<TITLE>" "<DETAILS>"
# SEVERITY: error (0xE74C3C), warning (0xF1C40F), info (0x2ECC71)

WEBHOOK_URL="${DISCORD_ALERTS_WEBHOOK_URL:-$DISCORD_WEBHOOK_URL}"
if [ -z "$WEBHOOK_URL" ] && [ -f "/run/secrets/charon_discord_webhook" ]; then
    WEBHOOK_URL=$(cat /run/secrets/charon_discord_webhook 2>/dev/null || true)
fi

# If no webhook is configured, exit cleanly without error
if [ -z "$WEBHOOK_URL" ]; then
    exit 0
fi

SERVICE="${1:-charon}"
SEVERITY="${2:-error}"
TITLE="${3:-Alert}"
DETAILS="${4:-No details provided}"
NODE="${NODE_HOSTNAME:-$(hostname)}"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

COLOR=15158332 # Default Red (0xE74C3C)
if [ "$SEVERITY" = "warning" ]; then
    COLOR=15844351 # Amber (0xF1C40F)
elif [ "$SEVERITY" = "info" ]; then
    COLOR=3066993 # Green (0x2ECC71)
fi

# Mention user if configured, which automatically adds them to the created thread
CONTENT=""
if [ -n "$DISCORD_USER_ID" ]; then
    CONTENT="<@${DISCORD_USER_ID}>"
fi

# Escape JSON characters in details and title
ESCAPED_DETAILS=$(printf '%s' "$DETAILS" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a' -e 'N' -e '$!ba' -e 's/\n/\\n/g')
ESCAPED_TITLE=$(printf '%s' "$TITLE" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
THREAD_NAME="🚨 Charon Alert: ${ESCAPED_TITLE}"

# Primary payload with thread_name (creates new thread in Forum channels)
PAYLOAD=$(cat <<EOF
{
  "content": "${CONTENT}",
  "thread_name": "${THREAD_NAME}",
  "embeds": [{
    "title": "🚨 Charon Alert: ${ESCAPED_TITLE}",
    "color": ${COLOR},
    "fields": [
      {"name": "Service", "value": "\`${SERVICE}\`", "inline": true},
      {"name": "Node", "value": "\`${NODE}\`", "inline": true},
      {"name": "Severity", "value": "\`${SEVERITY}\`", "inline": true},
      {"name": "Details", "value": "\`\`\`text\n${ESCAPED_DETAILS}\n\`\`\`"}
    ],
    "footer": {"text": "Yggdrasil Storage Ferryman"},
    "timestamp": "${TIMESTAMP}"
  }]
}
EOF
)

# Fallback payload without thread_name (for standard text channels or existing threads)
FALLBACK_PAYLOAD=$(cat <<EOF
{
  "content": "${CONTENT}",
  "embeds": [{
    "title": "🚨 Charon Alert: ${ESCAPED_TITLE}",
    "color": ${COLOR},
    "fields": [
      {"name": "Service", "value": "\`${SERVICE}\`", "inline": true},
      {"name": "Node", "value": "\`${NODE}\`", "inline": true},
      {"name": "Severity", "value": "\`${SEVERITY}\`", "inline": true},
      {"name": "Details", "value": "\`\`\`text\n${ESCAPED_DETAILS}\n\`\`\`"}
    ],
    "footer": {"text": "Yggdrasil Storage Ferryman"},
    "timestamp": "${TIMESTAMP}"
  }]
}
EOF
)

if command -v curl >/dev/null 2>&1; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "$PAYLOAD" "$WEBHOOK_URL" || true)
    # If 400 Bad Request (e.g. thread_name provided to a non-forum channel), retry with fallback payload
    if [ "$HTTP_CODE" = "400" ]; then
        curl -s -X POST -H "Content-Type: application/json" -d "$FALLBACK_PAYLOAD" "$WEBHOOK_URL" >/dev/null 2>&1 || true
    fi
elif command -v wget >/dev/null 2>&1; then
    wget -q -O- --header="Content-Type: application/json" --post-data="$PAYLOAD" "$WEBHOOK_URL" >/dev/null 2>&1 || \
    wget -q -O- --header="Content-Type: application/json" --post-data="$FALLBACK_PAYLOAD" "$WEBHOOK_URL" >/dev/null 2>&1 || true
fi
