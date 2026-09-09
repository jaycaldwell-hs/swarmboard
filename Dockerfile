FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.153.4
RUN npm install --global --omit=dev @openai/codex@${CODEX_VERSION}

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/swarmboard \
    USER=swarmboard \
    LOGNAME=swarmboard \
    SWARMBOARD_CODEX_VERSION=0.153.4 \
    SWARMBOARD_CODEX_AUTH=api_key \
    SWARMBOARD_HOSTED=1 \
    SWARMBOARD_REQUIRE_AUTH=1 \
    SWARMBOARD_DB_PATH=/var/data/swarmboard.db \
    SWARMBOARD_PERSONA_DIR=/var/data/persona
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && useradd --create-home --uid 10001 swarmboard \
    && mkdir -p /var/data \
    && chown swarmboard:swarmboard /var/data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY swarmboard ./swarmboard
RUN pip install --no-cache-dir . && codex --version

# Render builds must pass the same deterministic checks as local development.
# Keep test dependencies and fixtures out of the final service image.
FROM runtime AS verify
COPY tests ./tests
RUN pip install --no-cache-dir '.[dev]' \
    && env -u SWARMBOARD_REQUIRE_AUTH -u SWARMBOARD_HOSTED -u SWARMBOARD_CODEX_AUTH python -m pytest \
    && node --test tests/*.cjs \
    && touch /tmp/swarmboard-verified

FROM runtime
COPY --from=verify /tmp/swarmboard-verified /usr/local/share/swarmboard-verified
EXPOSE 10000
CMD ["python", "-m", "swarmboard.hosted"]
