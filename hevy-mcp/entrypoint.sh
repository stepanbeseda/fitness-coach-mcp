#!/bin/sh
# Start hevy-mcp with secrets taken from Docker secret files, so they never appear
# in the compose file or in `docker inspect`.
set -eu

read_secret() {
  f="/run/secrets/$1"
  if [ ! -s "$f" ]; then
    echo "hevy-entrypoint: missing or empty secret $1" >&2
    exit 1
  fi
  tr -d '\r\n' < "$f"
}

HEVY_API_KEY="$(read_secret hevy_api_key)"
# Shared secret between the gateway and this container (defence in depth: the
# backend rejects anything that did not come through the gateway).
HEVY_MCP_HTTP_BEARER_TOKEN="$(read_secret hevy_backend_token)"
export HEVY_API_KEY HEVY_MCP_HTTP_BEARER_TOKEN

# Upstream enables Sentry + OpenTelemetry export to the author's collector by default.
export HEVY_MCP_TELEMETRY=0
export HEVY_MCP_TELEMETRY_DIAGNOSTICS=0

exec node /app/standalone.mjs "$@"
