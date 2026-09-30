#!/usr/bin/env bash
# Gateway administration inside the running container.
#
#   scripts/admin.sh grants list [--all]       active OAuth grants (connected clients)
#   scripts/admin.sh grants revoke <id>|--all  disconnect a client (it must re-authorize)
#   scripts/admin.sh mint-token --backend hevy [--ttl 600]   short-lived token for tests
#   scripts/admin.sh check-config              validate configuration and secrets
#   scripts/admin.sh audit [N]                 last N audit records (default 50)
source "$(dirname "$0")/lib.sh"
require_docker

case "${1:-}" in
  audit)
    lines="${2:-50}"
    [[ "$lines" =~ ^[0-9]+$ ]] || die "N musí být číslo."
    dc exec -T gateway tail -n "$lines" /data/audit.jsonl
    ;;
  grants|mint-token|check-config)
    dc exec -T gateway python -m gateway.cli "$@"
    ;;
  *)
    sed -n '2,8p' "$0"
    exit 2
    ;;
esac
