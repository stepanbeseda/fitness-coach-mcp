# Upstream hevy-mcp (chrisdoc/hevy-mcp), pinned by digest. It speaks Streamable HTTP
# natively, so no stdio bridge is needed. The wrapper only reads secrets from Docker
# secret files and forces telemetry off.
FROM ghcr.io/chrisdoc/hevy-mcp:6.1.16@sha256:55c5ebe023723b3271fce7e5dd36f5c221323f976141769ab077fa23265ec449

COPY --chmod=0555 entrypoint.sh /usr/local/bin/hevy-entrypoint

ENTRYPOINT ["/usr/local/bin/hevy-entrypoint"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "3000"]
