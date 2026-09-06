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
RUN npm ci

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

# NEXT_PUBLIC_* values are inlined into the client bundle at build time
# (firebaseEnvironmentConfig.ts throws at startup when any is missing, so an
# empty placeholder here makes a forgotten --build-arg fail fast instead of
# silently pointing the browser at the wrong Firebase project). Real values
# are supplied by the deploy command — never bake defaults into this file.
ARG NEXT_PUBLIC_FIREBASE_API_KEY
ARG NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID
ARG NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET
ARG NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID
ARG NEXT_PUBLIC_FIREBASE_APP_ID
ARG NEXT_PUBLIC_API_PROXY_TARGET
ENV NEXT_PUBLIC_FIREBASE_API_KEY=$NEXT_PUBLIC_FIREBASE_API_KEY \
    NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=$NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN \
    NEXT_PUBLIC_FIREBASE_PROJECT_ID=$NEXT_PUBLIC_FIREBASE_PROJECT_ID \
    NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=$NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET \
    NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=$NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID \
    NEXT_PUBLIC_FIREBASE_APP_ID=$NEXT_PUBLIC_FIREBASE_APP_ID \
    NEXT_PUBLIC_API_PROXY_TARGET=$NEXT_PUBLIC_API_PROXY_TARGET

# Same command as the project FE build gate (root package.json build script).
RUN npm run build --workspace=@rag-ragre/web


FROM node:22-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

RUN useradd --create-home --uid 1000 appuser

# Root node_modules (hoisted workspace deps) + workspace sources. `next start`
# re-loads next.config.ts at runtime (rewrites are computed at server start)
# and next.config.ts imports ./src/lib/mediaPolicy, so src/ must ship too.
COPY --from=builder --chown=appuser:appuser /app/package.json /app/package-lock.json ./
COPY --from=builder --chown=appuser:appuser /app/node_modules ./node_modules
COPY --from=builder --chown=appuser:appuser /app/packages ./packages
COPY --from=builder --chown=appuser:appuser /app/apps/web ./apps/web

USER appuser
WORKDIR /app/apps/web

# Cloud Run injects PORT (default 8080 here); `--` forwards -p to next start.
# No HEALTHCHECK: Cloud Run uses its own HTTP probes.
EXPOSE 8080
CMD ["sh", "-c", "npm run start -- -p \"${PORT:-8080}\""]
