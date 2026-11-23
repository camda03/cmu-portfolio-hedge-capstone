#!/usr/bin/env bash
# Standard MCP client; Cloud Shell for interactive setup, no local OAuth implementation.
set -euo pipefail
set +x
case "${1:-connect}" in connect|de-price|history|snapshot|catalog) ;; *) printf 'Usage: bash connect_ibkr.sh [connect|de-price|history REQUEST_JSON OUTPUT|snapshot REQUEST_JSON OUTPUT|catalog OUTPUT]\n' >&2; exit 2 ;; esac
if [ "${1:-connect}" = connect ]; then
  : "${WEB_HOST:?Run this in Google Cloud Shell, not Mac Terminal.}"
fi
cd "$(dirname "$0")"
umask 077
export MCP_STORAGE_DIR="$HOME/.config/capstone-ibkr-paper"
mkdir -p "$MCP_STORAGE_DIR"
chmod 700 "$MCP_STORAGE_DIR"
export MCP_INSPECTOR_SECRET_STORE=file
export MCP_INSPECTOR_SECRET_FILE="$MCP_STORAGE_DIR/secrets.json"
export MCP_INSPECTOR_SECRET_KEY_FILE="$MCP_STORAGE_DIR/secret-key"
unset MCP_INSPECTOR_OAUTH_STATE_PATH MCP_INSPECTOR_SECRET_KEY DANGEROUSLY_OMIT_AUTH
if [ "${1:-connect}" = connect ] && [ ! -e "$MCP_INSPECTOR_SECRET_KEY_FILE" ]; then
  openssl rand -hex 32 > "$MCP_INSPECTOR_SECRET_KEY_FILE"
fi
inspector=(npx --yes --package=node@24.22.1 --package=@modelcontextprotocol/inspector@2.14.0 mcp-inspector)
if [ "${1:-connect}" != connect ]; then
  # Serialize stored OAuth use across securities, including token rotation.
  exec 9>"$MCP_STORAGE_DIR/.market-data.lock"
  # Seven simultaneous workers may each need 90s. Queue time is separate from I/O.
  flock -E 75 -w 900 9
  printf '%s\n' '{"broker_diagnostic_stage":"lock_acquired"}' >&2
  mkdir -p reports
  if [ "$1" = catalog ]; then
    output="${2:?Supply the catalog response file path.}"
    method_args=(--method tools/list)
  else
    tool_name=get_price_history
    if [ "$1" = snapshot ]; then tool_name=get_price_snapshot; fi
    if [ "$1" = de-price ]; then
      request='{"contract_id":6393,"security_type":"STK","period":"ONE_WEEK","step":"ONE_DAY","outside_rth":false}'
      output="reports/ibkr-de-history-$(date -u +%Y%m%dT%H%M%S%NZ).json"
    else
      request="${2:?Supply the market-data request JSON.}"
      output="${3:?Supply the response file path.}"
    fi
    method_args=(--method tools/call --tool-name "$tool_name" --tool-args-json "$request")
  fi
  timeout --kill-after=5s 90s "${inspector[@]}" --cli --transport http --server-url https://api.ibkr.com/v1/api/mcp-public \
    --use-stored-auth --stored-auth-only "${method_args[@]}" \
    --output "$output.pending" > /dev/null
  mv -- "$output.pending" "$output"
  date -u +%Y-%m-%dT%H:%M:%SZ > "${output%.json}.retrieved_at.txt"
  printf 'MCP response saved: %s\n' "$output"
  if [ "$1" = de-price ]; then cat "$output"; fi
  exit 0
fi
export HOST=127.0.0.1 CLIENT_PORT=8080 MCP_AUTO_OPEN_ENABLED=false
export ALLOWED_ORIGINS="https://8080-$WEB_HOST"
printf 'When the server is ready, open: %s\n' "$ALLOWED_ORIGINS"
printf 'Use only the approved paper or separate capstone account for market data. The legacy ibkr-paper label does not identify the account.\n'
exec "${inspector[@]}" --web --config ibkr_mcp.json
