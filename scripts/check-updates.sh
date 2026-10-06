#!/usr/bin/env bash
# Report newer versions of everything that is pinned. Nothing is changed:
# updates are applied deliberately (edit pin → scripts/lock-deps.sh → rebuild →
# scripts/e2e-test.sh → deploy → scripts/smoke-test.sh → commit).
#
#   scripts/check-updates.sh
source "$(dirname "$0")/lib.sh"

row() { printf '%-28s %-22s %-22s %s\n' "$1" "$2" "$3" "$4"; }
status_for() { [[ "$1" == "$2" ]] && echo "aktuální" || echo "${C_WARN}UPDATE${C_OFF}"; }

row "KOMPONENTA" "PINNUTO" "NEJNOVĚJŠÍ" "STAV"

# Tailscale (stable releases have an even minor version)
ts_pin="$(sed -n 's|^FROM tailscale/tailscale:v\([0-9.]*\)@.*|\1|p' tailscale/Dockerfile)"
ts_latest="$(curl -fsS 'https://hub.docker.com/v2/repositories/tailscale/tailscale/tags?page_size=100&name=v1.' \
  | jq -r '.results[].name' | grep -E '^v1\.[0-9]*[02468]\.[0-9]+$' | sed 's/^v//' | sort -V | tail -1 || true)"
row "tailscale/tailscale" "$ts_pin" "${ts_latest:-?}" "$(status_for "$ts_pin" "$ts_latest")"

# hevy-mcp
hevy_pin="$(sed -n 's|^FROM ghcr.io/chrisdoc/hevy-mcp:\([0-9.]*\)@.*|\1|p' hevy-mcp/Dockerfile)"
hevy_latest="$(curl -fsS https://registry.npmjs.org/hevy-mcp/latest | jq -r .version || true)"
row "hevy-mcp" "$hevy_pin" "${hevy_latest:-?}" "$(status_for "$hevy_pin" "$hevy_latest")"

# Python base image: same tag, but has the digest moved (security rebuilds)?
py_ref="$(sed -n 's|^FROM \(python:[^@]*\)@\(sha256:[0-9a-f]*\).*|\1 \2|p' gateway/Dockerfile | head -1)"
py_tag="${py_ref%% *}"; py_digest="${py_ref##* }"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  py_now="$(docker buildx imagetools inspect "$py_tag" 2>/dev/null | sed -n 's/^Digest: *//p' | head -1)"
  row "$py_tag (digest)" "${py_digest:7:12}" "${py_now:7:12}" "$(status_for "$py_digest" "$py_now")"
fi

# Python packages (gateway, garmin-mcp and coach-mcp locks)
for lock in gateway/requirements.lock garmin-mcp/requirements.lock coach-mcp/requirements.lock; do
  [[ -f "$lock" ]] || continue
  while IFS='=' read -r name _ version; do
    latest="$(curl -fsS "https://pypi.org/pypi/${name}/json" | jq -r .info.version 2>/dev/null || true)"
    if [[ "$version" != "$latest" ]]; then
      row "${lock%%/*}: $name" "$version" "${latest:-?}" "$(status_for "$version" "$latest")"
    fi
  done < <(sed -n 's/^\([A-Za-z0-9_.-]*\)==\([^ ]*\).*/\1==\2/p' "$lock")
done
echo
echo "Python balíčky vypsané výše mají novější verzi; ostatní jsou aktuální."
echo "Postup aktualizace: README.md → Aktualizace."
