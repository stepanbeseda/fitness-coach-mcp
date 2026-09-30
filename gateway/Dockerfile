# OAuth 2.1 gateway for the fitness-coach MCP servers.
# Dependencies are installed from a hash-locked requirements file; the test stage
# runs the full test suite during the build.
FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS deps
COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /venv \
 && /venv/bin/pip install --no-deps --require-hashes -r /tmp/requirements.lock

FROM deps AS test
COPY requirements-dev.lock /tmp/requirements-dev.lock
RUN /venv/bin/pip install --no-deps --require-hashes -r /tmp/requirements-dev.lock
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests
RUN /venv/bin/python -m pytest -q -p no:cacheprovider && touch /tests-passed

FROM base AS runtime
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown "${APP_UID}:${APP_GID}" /data \
 && chmod 0700 /data
COPY --from=deps /venv /venv
# Building the runtime image requires the test stage to have passed.
COPY --from=test /tests-passed /tmp/.tests-passed
COPY src/gateway /app/gateway
COPY config.toml /app/config.toml
ENV PATH=/venv/bin:$PATH \
    PYTHONPATH=/app
WORKDIR /app
USER ${APP_UID}:${APP_GID}
EXPOSE 8080
ENTRYPOINT ["python", "-m", "gateway"]
