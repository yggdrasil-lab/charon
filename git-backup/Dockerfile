FROM alpine/git:latest

RUN apk add --no-cache curl ca-certificates

COPY git-backup.sh /usr/local/bin/
COPY notify-discord.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/git-backup.sh /usr/local/bin/notify-discord.sh
