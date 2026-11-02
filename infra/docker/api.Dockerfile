# syntax=docker/dockerfile:1.7
# Rebox api image: Hono API (@rebox/api) + explorer build (@rebox/web) + db/ and psql,
# so the same image also runs migrations (infra/docker/migrate.sh).
# Build from the repo root:  docker build -f infra/docker/api.Dockerfile .
#
# Optional build secret `extra_ca` (PEM): an extra CA for networks with a TLS-inspecting
# proxy. Used only while installing packages; never copied into the image.

ARG NODE_IMAGE=node:22-slim

FROM ${NODE_IMAGE} AS base
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    COREPACK_HOME=/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    CI=true
WORKDIR /app
# Root manifests only; corepack installs the pnpm version pinned in package.json.
COPY package.json pnpm-workspace.yaml pnpm-lock.yam[l] ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    corepack enable && corepack install && pnpm --version
COPY apps/api/package.json apps/api/
# apps/web may not exist yet; the glob makes this copy optional.
COPY apps/web/package.jso[n] apps/web/

# Install everything (dev deps included) for the build.
FROM base AS build
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    if [ -f pnpm-lock.yaml ]; then pnpm install --frozen-lockfile; else pnpm install --no-frozen-lockfile; fi
COPY apps/api/ apps/api/
COPY apps/we[b]/ apps/web/
# The web build writes into apps/api/public. Skipped when apps/web or its build script is absent.
RUN if [ -f apps/web/package.json ]; then pnpm --filter @rebox/web run --if-present build; fi \
 && pnpm --filter @rebox/api run build \
 && test -f apps/api/dist/server.js

# Production dependencies of @rebox/api only.
FROM base AS prod-deps
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    if [ -f pnpm-lock.yaml ]; then f=--frozen-lockfile; else f=--no-frozen-lockfile; fi; \
    pnpm install --prod "$f" --filter @rebox/api

FROM ${NODE_IMAGE} AS runtime
# psql for db/migrate.sh (Debian's client talks to Postgres 16 fine).
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV NODE_ENV=production \
    PORT=3000
WORKDIR /app
COPY --from=prod-deps /app/node_modules ./node_modules
COPY --from=prod-deps /app/apps/api/node_modules ./apps/api/node_modules
COPY --from=build /app/apps/api/package.json ./apps/api/package.json
COPY --from=build /app/apps/api/dist ./apps/api/dist
# Explorer build output, present only when the web app was built.
COPY --from=build /app/apps/api/publi[c] ./apps/api/public/
COPY db/ ./db/
COPY infra/docker/migrate.sh ./infra/docker/migrate.sh
WORKDIR /app/apps/api
# uid/gid 1000 = the image's `node` user
USER 1000:1000
EXPOSE 3000
CMD ["node", "dist/server.js"]
