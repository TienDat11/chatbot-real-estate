# rag-real-estate FE — ragre-web Cloud Run (asia-southeast1) container image.
#
# BUILD CONTEXT IS THE MONOREPO ROOT, not apps/web: npm workspaces require the
# root package.json/package-lock.json and the packages/* sources, and
# `gcloud run deploy --source <dir>` uses <dir> itself as the context, so the
# deploy goes through Cloud Build / docker CLI with an explicit Dockerfile:
#   docker build -f apps/web/Dockerfile .
# (full commands: deploy/DEPLOY_RUNBOOK.md, step 3).
#
# Multi-stage: builder installs the workspace graph and compiles the Next app;
# the runtime stage ships node_modules + the compiled app only. No secrets are
# baked in — NEXT_PUBLIC_* values are public (they ship to the browser) and
# come from --build-arg at build time.

FROM node:22-slim AS builder

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

# Workspace manifests first so `npm ci` stays in Docker layer cache unless the
# dependency graph changes. Workspaces resolve through the ROOT manifest.
COPY package.json package-lock.json ./
COPY packages/contracts/package.json packages/contracts/
COPY packages/ui/package.json packages/ui/
COPY apps/web/package.json apps/web/
# npm bug 4828: the Windows-generated lock pruned the workspace-scoped
# @tailwindcss/oxide optionals to win32 only, so `npm ci` on linux lacks the
# native binding the build requires. Install the same locked version at the
# root; Node resolution walks up from apps/web/node_modules and finds it.
RUN npm ci \
 && oxide_ver="$(node -p "require('./package-lock.json').packages['apps/web/node_modules/@tailwindcss/oxide'].version")" \
 && npm i --no-save --no-audit --no-fund "@tailwindcss/oxide-linux-x64-gnu@${oxide_ver}"

# Workspace packages ship unbundled source (transpilePackages in
# apps/web/next.config.ts consumes their sources directly). Copy src/ etc.
# explicitly — a blanket apps/web COPY would drag .env.local and local logs
# into the image (no root-level .dockerignore exists for the root context).
COPY packages ./packages

# packages/*/package.json export only ./dist (gitignored), so compile the
# workspace libs before `next build` resolves @rag-ragre/contracts and
# @rag-ragre/ui. Manifests + packages are copied above, so this layer stays
# cached unless the lib sources change.
RUN npm run build:libs

COPY apps/web/src apps/web/src
COPY apps/web/public apps/web/public
COPY apps/web/next.config.ts apps/web/tsconfig.json apps/web/postcss.config.mjs apps/web/
# tsconfig includes **/*.ts, so vitest.setup.ts is part of the type-checked
# program; its jest-dom/vitest import augments the Assertion matchers the
# *.test.tsx files rely on. Omitting it makes `next build` typecheck fail.
COPY apps/web/vitest.setup.ts apps/web/

# NEXT_PUBLIC_* values are inlined into the client bundle and the route
# manifest at build time: firebaseEnvironmentConfig.ts throws when the
# Firebase six are missing, and next.config.ts bakes rewrites into
# routes-manifest.json from NEXT_PUBLIC_API_PROXY_TARGET — `next start`
# does NOT recompute them from runtime env, so a forgotten --build-arg here
# yields a deployed FE whose /api/* all 404. Real values come from the
# deploy command — never bake defaults into this file.
ARG NEXT_PUBLIC_FIREBASE_API_KEY
ARG NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID
ARG NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET
ARG NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID
ARG NEXT_PUBLIC_FIREBASE_APP_ID
ARG NEXT_PUBLIC_FIREBASE_VAPID_KEY
ARG NEXT_PUBLIC_MEDIA_ORIGINS_JSON
ARG NEXT_PUBLIC_MAP_TILE_URL
ARG NEXT_PUBLIC_API_PROXY_TARGET
ENV NEXT_PUBLIC_FIREBASE_API_KEY=$NEXT_PUBLIC_FIREBASE_API_KEY \
    NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=$NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN \
    NEXT_PUBLIC_FIREBASE_PROJECT_ID=$NEXT_PUBLIC_FIREBASE_PROJECT_ID \
    NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=$NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET \
    NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=$NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID \
    NEXT_PUBLIC_FIREBASE_APP_ID=$NEXT_PUBLIC_FIREBASE_APP_ID \
    NEXT_PUBLIC_FIREBASE_VAPID_KEY=$NEXT_PUBLIC_FIREBASE_VAPID_KEY \
    NEXT_PUBLIC_MEDIA_ORIGINS_JSON=$NEXT_PUBLIC_MEDIA_ORIGINS_JSON \
    NEXT_PUBLIC_MAP_TILE_URL=$NEXT_PUBLIC_MAP_TILE_URL \
    NEXT_PUBLIC_API_PROXY_TARGET=$NEXT_PUBLIC_API_PROXY_TARGET

# Same command as the project FE build gate (root package.json build script).
# Fail fast: an empty API_PROXY_TARGET bakes zero rewrites and the deployed
# FE 404s on every /api/* call — catch it at build, not in production.
RUN { test -n "$NEXT_PUBLIC_API_PROXY_TARGET" || (echo "FATAL: pass --build-arg NEXT_PUBLIC_API_PROXY_TARGET=<ragre-api Cloud Run URL>" && exit 1); } \
 && { test -n "$NEXT_PUBLIC_MEDIA_ORIGINS_JSON" || (echo "FATAL: pass --build-arg NEXT_PUBLIC_MEDIA_ORIGINS_JSON='<the JSON array from apps/web/.env.local>'" && exit 1); } \
 && npm run build --workspace=@rag-ragre/web


FROM node:22-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# `next start` needs these two in the runtime env: next.config.ts loads
# src/lib/mediaPolicy to compute images.remotePatterns at server start, and an
# empty value silently yields an empty allowlist (next/image 400s on R2 assets).
# Note rewrites are baked at BUILD time into routes-manifest.json, so
# NEXT_PUBLIC_API_PROXY_TARGET is a build-arg here and is NOT re-read at
# runtime despite what the name suggests. Empty strings are safe: mediaPolicy
# and mapTiles both treat "" as unset.
ARG NEXT_PUBLIC_MEDIA_ORIGINS_JSON
ARG NEXT_PUBLIC_MAP_TILE_URL
ENV NEXT_PUBLIC_MEDIA_ORIGINS_JSON=$NEXT_PUBLIC_MEDIA_ORIGINS_JSON \
    NEXT_PUBLIC_MAP_TILE_URL=$NEXT_PUBLIC_MAP_TILE_URL
WORKDIR /app

# node:22-slim already ships a `node` user (uid 1000); creating another
# uid-1000 user fails with `useradd: UID 1000 is not unique`.

# Root node_modules (hoisted workspace deps) + workspace sources. `next start`
# re-loads next.config.ts at runtime (rewrites are computed at server start)
# and next.config.ts imports ./src/lib/mediaPolicy, so src/ must ship too.
COPY --from=builder --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=builder --chown=node:node /app/node_modules ./node_modules
COPY --from=builder --chown=node:node /app/packages ./packages
COPY --from=builder --chown=node:node /app/apps/web ./apps/web

USER node
WORKDIR /app/apps/web

# Cloud Run injects PORT (default 8080 here); `--` forwards -p to next start.
# No HEALTHCHECK: Cloud Run uses its own HTTP probes.
EXPOSE 8080
CMD ["sh", "-c", "npm run start -- -p \"${PORT:-8080}\""]
