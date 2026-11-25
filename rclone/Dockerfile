FROM rclone/rclone:latest

RUN apk add --no-cache ca-certificates python3 && \
    mkdir -p /opt/charon

ENV PYTHONPATH=/opt/charon \
    PYTHONDONTWRITEBYTECODE=1

COPY charon_runtime /opt/charon/charon_runtime
COPY rclone/entrypoint.sh /entrypoint.sh
COPY rclone/archive-job.sh /archive-job.sh
RUN chmod -R a+rX /opt/charon && \
    chmod +x /entrypoint.sh /archive-job.sh
