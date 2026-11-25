FROM alpine/git:latest

RUN apk add --no-cache ca-certificates python3 su-exec tzdata && \
    addgroup -g 1000 charon && \
    adduser -D -u 1000 -G charon -h /tmp -s /bin/sh charon && \
    chmod g+w /etc/passwd && \
    mkdir -p /etc/charon-git /opt/charon

ENV PYTHONPATH=/opt/charon \
    PYTHONDONTWRITEBYTECODE=1

COPY charon_runtime /opt/charon/charon_runtime
COPY git-backup/targets.json /etc/charon-git/targets.json
COPY git-backup/git-backup.sh /usr/local/bin/git-backup.sh
COPY git-backup/entrypoint.sh /entrypoint.sh

RUN chmod -R a+rX /opt/charon && \
    chmod +x /usr/local/bin/git-backup.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
