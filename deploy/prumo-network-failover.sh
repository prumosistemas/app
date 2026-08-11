#!/usr/bin/env bash
set -u

# Associacao Wi-Fi e gateway nao provam acesso a internet. Este verificador
# testa HTTPS IPv4 real e alterna somente entre perfis ja protegidos pelo
# NetworkManager. Nenhuma senha e lida ou mantida por este script.

LOCK_FILE="/run/prumo-network-failover.lock"
LOG_TAG="prumo-network-failover"
WIFI_IFACE="${PRUMO_WIFI_IFACE:-wlp0s20f3}"
PREFERRED_CONNECTIONS=(
  "AVANCAR CONTADORES ALARES_5G"
  "AVANÇAR_LINK_5G"
  "AVANÇAR_LINK_2G"
  "AVANCAR_CLARO"
)

exec 9>"${LOCK_FILE}"
flock -n 9 || exit 0

log() {
  logger -t "${LOG_TAG}" -- "$*"
}

connection_exists() {
  nmcli -g connection.id connection show "$1" >/dev/null 2>&1
}

internet_ok() {
  local url
  for url in \
    "https://connectivitycheck.gstatic.com/generate_204" \
    "https://cp.cloudflare.com/generate_204"
  do
    if curl -4 -fsS \
      --connect-timeout 4 \
      --max-time 7 \
      --output /dev/null \
      "${url}"
    then
      return 0
    fi
  done
  return 1
}

# Duas provas evitam troca por uma oscilacao de poucos segundos.
if internet_ok; then
  exit 0
fi
sleep 5
if internet_ok; then
  exit 0
fi

log "internet indisponivel; iniciando failover Wi-Fi"

for connection in "${PREFERRED_CONNECTIONS[@]}"; do
  if ! connection_exists "${connection}"; then
    log "perfil ausente, ignorado: ${connection}"
    continue
  fi
  if ! nmcli --wait 30 connection up "${connection}" ifname "${WIFI_IFACE}" >/dev/null 2>&1; then
    log "nao foi possivel ativar: ${connection}"
    continue
  fi
  sleep 4
  if internet_ok; then
    log "internet restaurada por: ${connection}"
    systemctl try-restart cloudflared.service >/dev/null 2>&1 || true
    exit 0
  fi
  log "associou sem internet, tentando proxima: ${connection}"
done

log "nenhum perfil restaurou a internet; nova tentativa ocorrera pelo timer"
exit 0
