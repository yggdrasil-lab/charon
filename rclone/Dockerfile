FROM rclone/rclone:latest

RUN apk add --no-cache curl ca-certificates \
    && curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/scripts/notify-discord.sh -o /usr/local/bin/notify-discord.sh \
    && chmod +x /usr/local/bin/notify-discord.sh

COPY entrypoint.sh /entrypoint.sh
COPY setup.sh /setup.sh
COPY archive-job.sh /archive-job.sh
RUN chmod +x /entrypoint.sh /setup.sh /archive-job.sh
