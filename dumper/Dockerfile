FROM alpine:3.20

RUN apk add --no-cache \
    postgresql16-client \
    sqlite \
    tzdata \
    python3 && \
    mkdir -p /backups /var/log /etc/charon-dumper /opt/charon

ENV PYTHONPATH=/opt/charon \
    PYTHONDONTWRITEBYTECODE=1

COPY charon_runtime /opt/charon/charon_runtime
COPY dumper/targets.json /etc/charon-dumper/targets.json
COPY dumper/dumper.sh /usr/local/bin/dumper.sh
COPY dumper/restore.sh /usr/local/bin/restore.sh
COPY dumper/entrypoint.sh /entrypoint.sh

RUN chmod -R a+rX /opt/charon && \
    chmod +x /usr/local/bin/dumper.sh /usr/local/bin/restore.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
