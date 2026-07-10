#!/bin/sh
set -eu

: "${PUBLIC_IP:?A variável PUBLIC_IP é obrigatória.}"
: "${RAMAL_7001_SENHA:?A variável RAMAL_7001_SENHA é obrigatória.}"

SIP_PORT="${SIP_PORT:-5160}"
export PUBLIC_IP SIP_PORT RAMAL_7001_SENHA

case "$PUBLIC_IP" in
  *[!0-9.]*|"")
    echo "PUBLIC_IP inválido: use somente o IPv4 público da VPS." >&2
    exit 1
    ;;
esac

case "$SIP_PORT" in
  *[!0-9]*|"")
    echo "SIP_PORT inválido." >&2
    exit 1
    ;;
esac

if [ "${#RAMAL_7001_SENHA}" -lt 24 ]; then
  echo "RAMAL_7001_SENHA deve ter pelo menos 24 caracteres." >&2
  exit 1
fi

envsubst '${PUBLIC_IP} ${SIP_PORT} ${RAMAL_7001_SENHA}' \
  < /opt/rbk/templates/pjsip.conf.template \
  > /etc/asterisk/pjsip.conf

envsubst '${PUBLIC_IP}' \
  < /opt/rbk/templates/rtp.conf.template \
  > /etc/asterisk/rtp.conf

chown asterisk:asterisk /etc/asterisk/pjsip.conf /etc/asterisk/rtp.conf
chmod 640 /etc/asterisk/pjsip.conf /etc/asterisk/rtp.conf

echo "Iniciando Asterisk RBK no SIP UDP ${SIP_PORT}..."
exec /usr/sbin/asterisk \
  -f \
  -U asterisk \
  -G asterisk \
  -C /etc/asterisk/asterisk.conf \
  -vvv
