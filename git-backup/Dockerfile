FROM alpine/git:latest

RUN apk add --no-cache curl ca-certificates && \
    addgroup -g 1000 charon && \
    adduser -D -u 1000 -G charon -h /tmp -s /bin/sh charon && \
    chmod g+w /etc/passwd

COPY git-backup.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/git-backup.sh
