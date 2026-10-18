# garmin-coach-mcp: curated Garmin Connect MCP server (Streamable HTTP, stateless).
# Dependencies are installed from hash-locked files (see README.md to regenerate).
# The base image is a literal pin (no ARG), so Dependabot and scripts/check-pins.sh see it.
FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS deps
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-deps --require-hashes -r requirements.lock

# Dev stage: the test tools, without the code (scripts/record-garmin-contract.sh runs the tests of a mounted tree).
FROM deps AS dev
COPY requirements-dev.lock .
RUN pip install --no-deps --require-hashes -r requirements-dev.lock

# Test stage: runs during every build (the runtime stage depends on its marker). Static checks first
# (ruff, then mypy; both configured in pyproject.toml), then the tests.
FROM dev AS test
COPY pyproject.toml ./
COPY src/ src/
COPY tests/ tests/
RUN python -m ruff check --no-cache src tests
RUN python -m mypy --cache-dir=/dev/null
RUN python -m pytest && touch /tests-passed

FROM deps AS runtime
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid "${APP_GID}" app \
 && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /tmp --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/garmin \
 && chown "${APP_UID}:${APP_GID}" /data/garmin \
 && chmod 0700 /data/garmin
COPY --from=test /tests-passed /tmp/.tests-passed
COPY src/garmin_coach_mcp /app/garmin_coach_mcp
RUN python -m compileall -q /app/garmin_coach_mcp
ENV PYTHONPATH=/app \
    GARMINTOKENS=/data/garmin \
    HOME=/tmp \
    TZ=Europe/Prague
USER ${APP_UID}:${APP_GID}
EXPOSE 8000
ENTRYPOINT ["python", "-m", "garmin_coach_mcp"]
