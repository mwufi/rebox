# syntax=docker/dockerfile:1.7
# Rebox worker image: apps/worker installed into /opt/venv with uv; console script rebox-worker.
# The source keeps the repo layout (/app/apps/worker, /app/fixtures) and is installed editable,
# so paths resolved relative to the package or to cwd (/app) behave as in a checkout.
# Build from the repo root:  docker build -f infra/docker/worker.Dockerfile .
#
# Optional build secret `extra_ca` (PEM): an extra CA for networks with a TLS-inspecting
# proxy. Used only while installing packages; never copied into the image.

ARG PYTHON_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app/apps/worker
# Dependencies first (cached layer), then the project itself.
COPY apps/worker/pyproject.toml apps/worker/uv.loc[k] ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem; export SSL_CERT_FILE=/tmp/ca.pem; fi; \
    if [ -f uv.lock ]; then l=--locked; else l=; fi; \
    uv sync $l --no-dev --no-install-project
COPY apps/worker/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem; export SSL_CERT_FILE=/tmp/ca.pem; fi; \
    if [ -f uv.lock ]; then l=--locked; else l=; fi; \
    uv sync $l --no-dev \
 && /opt/venv/bin/rebox-worker --help > /dev/null

FROM ${PYTHON_IMAGE} AS runtime
# libpq5 in case psycopg is installed without the binary extra.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 rebox \
 && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/rebox rebox \
 && mkdir -p /data/raw \
 && chown -R 10001:10001 /data
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/apps/worker /app/apps/worker
WORKDIR /app
# Recorded fixtures (offline example connector), if present.
COPY fixture[s]/ ./fixtures/
USER 10001:10001
ENTRYPOINT ["rebox-worker"]
CMD ["run"]
