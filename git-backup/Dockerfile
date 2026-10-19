FROM alpine/git:latest

RUN apk add --no-cache curl ca-certificates \
    && curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/scripts/notify-discord.sh -o /usr/local/bin/notify-discord.sh \
    && chmod +x /usr/local/bin/notify-discord.sh

COPY git-backup.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/git-backup.sh
