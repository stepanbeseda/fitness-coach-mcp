# Thin wrapper around the official image. It only pre-creates the state directory
# owned by the unprivileged runtime user, so the sidecar can run as non-root in
# userspace-networking mode (no TUN device, no NET_ADMIN).
FROM tailscale/tailscale:v1.104.2@sha256:b11583ad2e93e4671128538c88cf11f460444a5d7e449311dd1933a400ab849d

ARG APP_UID=10001
ARG APP_GID=10001

RUN mkdir -p /var/lib/tailscale \
 && chown "${APP_UID}:${APP_GID}" /var/lib/tailscale \
 && chmod 0700 /var/lib/tailscale
