# Thin wrapper around the official image. It only pre-creates the state directory
# owned by the unprivileged runtime user, so the sidecar can run as non-root in
# userspace-networking mode (no TUN device, no NET_ADMIN).
FROM tailscale/tailscale:v1.102.5@sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065

ARG APP_UID=10001
ARG APP_GID=10001

RUN mkdir -p /var/lib/tailscale \
 && chown "${APP_UID}:${APP_GID}" /var/lib/tailscale \
 && chmod 0700 /var/lib/tailscale
