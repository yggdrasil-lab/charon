FROM alpine:3.20

RUN apk add --no-cache \
    postgresql16-client \
    sqlite \
    curl \
    jq \
    tzdata

COPY targets.json /etc/charon-dumper/targets.json
COPY dumper.sh /usr/local/bin/dumper.sh
COPY restore.sh /usr/local/bin/restore.sh
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /usr/local/bin/dumper.sh /usr/local/bin/restore.sh /entrypoint.sh && \
    mkdir -p /backups /var/log /etc/charon-dumper

ENTRYPOINT ["/entrypoint.sh"]
