#!/bin/sh
proxy_arg=""
if [ "${PRUMO_MODAL_PROXY_ENABLED:-0}" = "1" ] && [ -n "${PRUMO_MODAL_PROXY_HOSTNAME:-}" ] && [ -n "${PRUMO_MODAL_PROXY_LISTENER:-}" ]; then
  proxy_arg="--proxy-server=http://${PRUMO_MODAL_PROXY_LISTENER}"
fi
exec /usr/bin/google-chrome --no-sandbox --disable-dev-shm-usage $proxy_arg "$@"
