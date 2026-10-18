#!/usr/bin/env bash
# Report newer versions of everything that is pinned. Nothing is changed:
# updates are applied deliberately (edit pin → scripts/lock-deps.sh → rebuild →
# scripts/e2e-test.sh → deploy → scripts/smoke-test.sh → commit). Dependabot
# proposes the GitHub Actions and base-image bumps as pull requests; this
# script also covers what it cannot: the Python packages and the uv image.
#
#   scripts/check-updates.sh
source "$(dirname "$0")/lib.sh"

row() { printf '%-28s %-22s %-22s %s\n' "$1" "$2" "$3" "$4"; }
status_for() { [[ "$1" == "$2" ]] && echo "current" || echo "${C_WARN}UPDATE${C_OFF}"; }

row "COMPONENT" "PINNED" "LATEST" "STATUS"

# Tailscale (stable releases have an even minor version)
ts_pin="$(sed -n 's|^FROM tailscale/tailscale:v\([0-9.]*\)@.*|\1|p' tailscale/Dockerfile)"
ts_latest="$(curl -fsS 'https://hub.docker.com/v2/repositories/tailscale/tailscale/tags?page_size=100&name=v1.' \
  | jq -r '.results[].name' | grep -E '^v1\.[0-9]*[02468]\.[0-9]+$' | sed 's/^v//' | sort -V | tail -1 || true)"
row "tailscale/tailscale" "$ts_pin" "${ts_latest:-?}" "$(status_for "$ts_pin" "$ts_latest")"

# hevy-mcp
hevy_pin="$(sed -n 's|^FROM ghcr.io/chrisdoc/hevy-mcp:\([0-9.]*\)@.*|\1|p' hevy-mcp/Dockerfile)"
hevy_latest="$(curl -fsS https://registry.npmjs.org/hevy-mcp/latest | jq -r .version || true)"
row "hevy-mcp" "$hevy_pin" "${hevy_latest:-?}" "$(status_for "$hevy_pin" "$hevy_latest")"

# Python base image of gateway, garmin-mcp and coach-mcp: same tag, but has the digest moved (security
# rebuilds)? The three pins must be equal (scripts/check-pins.sh).
docker_ok=0
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then docker_ok=1; fi
digest_of() { docker buildx imagetools inspect "$1" 2>/dev/null | sed -n 's/^Digest: *//p' | head -1; }
py_refs=()
for svc in gateway garmin-mcp coach-mcp; do
  ref="$(sed -n 's|^FROM \(python:[^@ ]*@sha256:[0-9a-f]*\).*|\1|p' "$svc/Dockerfile" | head -1)"
  if [[ -z "$ref" ]]; then
    row "$svc: python" "?" "?" "${C_WARN}no literal FROM python:<tag>@sha256 line${C_OFF}"
    continue
  fi
  py_refs+=("$ref")
  if (( docker_ok )); then
    py_tag="${ref%%@*}"; py_digest="${ref##*@}"; py_now="$(digest_of "$py_tag")"
    row "$svc: $py_tag" "${py_digest:7:12}" "${py_now:7:12}" "$(status_for "$py_digest" "$py_now")"
  fi
done
if (( ${#py_refs[@]} > 1 )) && [[ "$(printf '%s\n' "${py_refs[@]}" | sort -u | wc -l)" -gt 1 ]]; then
  warn "The python base pins of gateway, garmin-mcp and coach-mcp differ (scripts/check-pins.sh)."
fi

# uv image used by scripts/lock-deps.sh: newer uv release, and has the digest of the pinned tag moved?
uv_ref="$(sed -n 's|^UV_IMAGE="\([^"]*\)"|\1|p' scripts/lock-deps.sh)"
uv_tag="${uv_ref%%@*}"; uv_digest="${uv_ref##*@}"
uv_pin="$(sed -n 's|^ghcr.io/astral-sh/uv:\([0-9.]*\)-.*|\1|p' <<< "$uv_tag")"
uv_latest="$(curl -fsS https://pypi.org/pypi/uv/json | jq -r .info.version || true)"
row "uv (lock-deps.sh)" "${uv_pin:-?}" "${uv_latest:-?}" "$(status_for "$uv_pin" "$uv_latest")"
if (( docker_ok )) && [[ -n "$uv_tag" ]]; then
  uv_now="$(digest_of "$uv_tag")"
  row "uv image (digest)" "${uv_digest:7:12}" "${uv_now:7:12}" "$(status_for "$uv_digest" "$uv_now")"
fi

# Python packages (gateway, garmin-mcp and coach-mcp locks, and the pip-audit tool of the CI audit)
for lock in gateway/requirements.lock garmin-mcp/requirements.lock coach-mcp/requirements.lock \
  .github/audit/requirements.lock; do
  [[ -f "$lock" ]] || continue
  while IFS='=' read -r name _ version; do
    latest="$(curl -fsS "https://pypi.org/pypi/${name}/json" | jq -r .info.version 2>/dev/null || true)"
    if [[ "$version" != "$latest" ]]; then
      row "${lock%/requirements.lock}: $name" "$version" "${latest:-?}" "$(status_for "$version" "$latest")"
    fi
  done < <(sed -n 's/^\([A-Za-z0-9_.-]*\)==\([^ ]*\).*/\1==\2/p' "$lock")
done
echo
echo "The Python packages listed above have a newer version; the rest are current."
echo "How to update: docs/operations.md → Updates."
