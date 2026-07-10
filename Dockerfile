FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        asterisk \
        asterisk-core-sounds-en-gsm \
        ca-certificates \
        gettext-base \
    && rm -rf /var/lib/apt/lists/*

COPY config/asterisk.conf /etc/asterisk/asterisk.conf
COPY config/extensions.conf /etc/asterisk/extensions.conf
COPY config/logger.conf /etc/asterisk/logger.conf
COPY config/modules.conf /etc/asterisk/modules.conf
COPY config/pjsip.conf.template /opt/rbk/templates/pjsip.conf.template
COPY config/rtp.conf.template /opt/rbk/templates/rtp.conf.template
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && mkdir -p \
        /opt/rbk/templates \
        /var/lib/asterisk \
        /var/log/asterisk \
        /var/run/asterisk \
        /var/spool/asterisk \
    && chown -R asterisk:asterisk \
        /etc/asterisk \
        /var/lib/asterisk \
        /var/log/asterisk \
        /var/run/asterisk \
        /var/spool/asterisk

EXPOSE 5160/udp
EXPOSE 10000-10010/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD asterisk -rx "core show uptime" >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
