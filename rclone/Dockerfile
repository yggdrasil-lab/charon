FROM rclone/rclone:latest

RUN apk add --no-cache curl ca-certificates

COPY entrypoint.sh /entrypoint.sh
COPY setup.sh /setup.sh
COPY archive-job.sh /archive-job.sh
COPY notify-discord.sh /notify-discord.sh
RUN chmod +x /entrypoint.sh /setup.sh /archive-job.sh /notify-discord.sh
