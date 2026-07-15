#!/bin/sh
set -eu

start_artifact_retention() {
  artifact_dir="$1"
  retention_days="${PORTAL_DEBUG_RETENTION_DAYS:-7}"
  (
    while true; do
      python /app/google-ai-solver/artifact_retention.py \
        --root "$artifact_dir" \
        --retention-days "$retention_days" \
        --min-age-seconds "${PORTAL_DEBUG_COMPACT_AFTER_SECONDS:-900}" || true
      find /app/output/empresas -path '*/portal_nacional/runs/*/logs/*' \
        -type f -mtime "+$retention_days" -delete 2>/dev/null || true
      sleep 3600
    done
  ) &
  echo "[startup] retencao/compactacao de artefatos ativa por ${retention_days} dias"
}

start_local_portal_solver() {
  if [ "${PORTAL_NACIONAL_LOCAL_SOLVER_ENABLED:-1}" != "1" ]; then
    echo "[startup] resolvedor residencial do Portal desativado"
    return
  fi

  solver_dir="/app/google-ai-solver"
  state_dir="${GOOGLE_AI_STATE_DIR:-/app/output/_api_data/google_ai_solver_state}"
  artifact_dir="${GOOGLE_AI_ARTIFACT_ROOT:-/app/output/_api_data/google_ai_solver_artifacts}"
  chrome_bin="${GOOGLE_CHROME_BIN:-}"
  if [ -z "$chrome_bin" ]; then
    chrome_bin="$(find /root/.cache/ms-playwright -type f -path '*/chrome-linux*/chrome' -print -quit 2>/dev/null || true)"
  fi
  if [ -z "$chrome_bin" ] || [ ! -x "$chrome_bin" ]; then
    echo "[startup] Chromium do resolvedor residencial nao encontrado" >&2
    return 1
  fi

  mkdir -p "$state_dir" "$artifact_dir"
  start_artifact_retention "$artifact_dir"
  wrapper="/tmp/prumo-google-chrome"
  cat > "$wrapper" <<EOF
#!/bin/sh
exec "$chrome_bin" --no-sandbox --disable-dev-shm-usage "\$@"
EOF
  chmod +x "$wrapper"

  export GOOGLE_AI_PROJECT="$solver_dir"
  export GOOGLE_AI_STATE_DIR="$state_dir"
  export GOOGLE_AI_ARTIFACT_ROOT="$artifact_dir"
  export MODO_IA_DETECTOR_PROJECT="$solver_dir"
  export GOOGLE_CHROME_BIN="$chrome_bin"
  export GOOGLE_AI_FIREFOX_FALLBACK=0
  export GOOGLE_AI_RECOVERY_POLICY=chrome
  export GOOGLE_AI_RECOVERY_VERBOSE=1
  export GOOGLE_AI_CHROME_RECOVERY_ATTEMPTS=3

  xvfb-run -a python -u "$solver_dir/api_resolvedora_resolver_google_ia.py" \
    --port 8876 \
    --browser "$wrapper" \
    --max-browsers 4 \
    --max-provider-failures 30 \
    --max-solver-failures 20 \
    --max-solve-seconds 150 \
    >> "$artifact_dir/service.log" 2>&1 &
  echo "[startup] resolvedor residencial do Portal iniciado em 127.0.0.1:8876"
}

start_local_portal_solver
exec uvicorn main:app --host 0.0.0.0 --port 8000
