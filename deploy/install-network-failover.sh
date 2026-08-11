#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Execute com sudo: sudo bash deploy/install-network-failover.sh" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIFI_IFACE="${PRUMO_WIFI_IFACE:-wlp0s20f3}"
FALLBACK_CONNECTIONS=(
  "AVANÇAR_LINK_5G"
  "AVANÇAR_LINK_2G"
  "AVANCAR_CLARO"
)

for command_name in nmcli curl flock systemctl; do
  command -v "${command_name}" >/dev/null || {
    echo "Comando obrigatorio ausente: ${command_name}" >&2
    exit 1
  }
done

if ! nmcli -t -f DEVICE,TYPE device status | grep -Fq "${WIFI_IFACE}:wifi"; then
  echo "Interface Wi-Fi nao encontrada: ${WIFI_IFACE}" >&2
  exit 1
fi

nmcli radio wifi on
nmcli device wifi rescan ifname "${WIFI_IFACE}" || true

ensure_profile() {
  local ssid="$1"
  if nmcli -g connection.id connection show "${ssid}" >/dev/null 2>&1; then
    echo "Perfil ja cadastrado: ${ssid}"
    return 0
  fi

  echo
  echo "Cadastre ${ssid}; a senha sera solicitada diretamente pelo NetworkManager."
  if nmcli --ask --wait 45 device wifi connect "${ssid}" ifname "${WIFI_IFACE}" name "${ssid}"; then
    return 0
  fi

  echo "Tentando ${ssid} como rede oculta."
  nmcli --ask --wait 45 device wifi connect "${ssid}" ifname "${WIFI_IFACE}" name "${ssid}" hidden yes
}

for ssid in "${FALLBACK_CONNECTIONS[@]}"; do
  ensure_profile "${ssid}"
done

# A ALARES continua primaria. As demais entram na ordem abaixo quando a rede
# atual permanece associada, mas falha nos testes reais de internet.
nmcli connection modify "AVANCAR CONTADORES ALARES_5G" \
  connection.autoconnect yes connection.autoconnect-priority 400 connection.autoconnect-retries 0
nmcli connection modify "AVANÇAR_LINK_5G" \
  connection.autoconnect yes connection.autoconnect-priority 300 connection.autoconnect-retries 0
nmcli connection modify "AVANÇAR_LINK_2G" \
  connection.autoconnect yes connection.autoconnect-priority 200 connection.autoconnect-retries 0
nmcli connection modify "AVANCAR_CLARO" \
  connection.autoconnect yes connection.autoconnect-priority 100 connection.autoconnect-retries 0

install -m 0755 "${ROOT_DIR}/deploy/prumo-network-failover.sh" /usr/local/sbin/prumo-network-failover
install -m 0644 "${ROOT_DIR}/deploy/prumo-network-failover.service" /etc/systemd/system/prumo-network-failover.service
install -m 0644 "${ROOT_DIR}/deploy/prumo-network-failover.timer" /etc/systemd/system/prumo-network-failover.timer

systemctl daemon-reload
systemctl enable --now prumo-network-failover.timer
systemctl start prumo-network-failover.service

echo
echo "Failover instalado. Estado atual:"
nmcli -f NAME,TYPE,DEVICE,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
systemctl --no-pager status prumo-network-failover.timer | sed -n '1,10p'
