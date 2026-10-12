FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM node:24.14.0-bookworm-slim AS mmx
RUN npm install --global mmx-cli@1.0.16

FROM python:3.10.19-slim-bookworm

RUN apt-get update \
    && apt-get install --no-install-recommends -y poppler-utils \
    && pdftoppm -v \
    && rm -rf /var/lib/apt/lists/*

ARG MPLUS_SOURCE_COMMIT
ARG MPLUS_RUNNER_VERSION
LABEL org.opencontainers.image.revision=$MPLUS_SOURCE_COMMIT
LABEL org.opencontainers.image.version=$MPLUS_RUNNER_VERSION

COPY --from=uv /uv /uvx /bin/
COPY --from=mmx /usr/local/bin/node /usr/local/bin/node
COPY --from=mmx /usr/local/lib/node_modules/mmx-cli /usr/local/lib/node_modules/mmx-cli
RUN ln -s /usr/local/lib/node_modules/mmx-cli/dist/mmx.mjs /usr/local/bin/mmx \
    && test "$(mmx --version)" = "mmx 1.0.16"
WORKDIR /srv/mplus
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
RUN test "$(printf %s "$MPLUS_SOURCE_COMMIT" | wc -c)" -eq 40 \
    && test -n "$MPLUS_RUNNER_VERSION" \
    && uv run --locked --no-dev --python 3.10 python -m mvp_v2.command version

ENV PATH="/srv/mplus/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLUS_PROFILE=stable \
    MPLUS_EXECUTION_BACKEND=cloud \
    MPLUS_PDFTOPPM_COMMAND=/usr/bin/pdftoppm \
    MPLUS_SOURCE_COMMIT=$MPLUS_SOURCE_COMMIT \
    MPLUS_RUNNER_VERSION=$MPLUS_RUNNER_VERSION

ENTRYPOINT ["./mplus"]
CMD ["version"]
