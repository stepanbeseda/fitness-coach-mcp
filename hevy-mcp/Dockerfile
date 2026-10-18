# Upstream hevy-mcp (chrisdoc/hevy-mcp), pinned by digest. It speaks Streamable HTTP
# natively, so no stdio bridge is needed. The wrapper only reads secrets from Docker
# secret files and forces telemetry off.
FROM ghcr.io/chrisdoc/hevy-mcp:6.2.1@sha256:4b1f7ca0025c86dda3510d45eac5022c48b6f9aad36392468576c5627b709016

COPY --chmod=0555 entrypoint.sh /usr/local/bin/hevy-entrypoint

ENTRYPOINT ["/usr/local/bin/hevy-entrypoint"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "3000"]
