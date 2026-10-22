# Production image for the web app: Next.js standalone server, non-root, no source or dev
# dependencies. NEXT_PUBLIC_API_URL is inlined into the browser code at build time, so it is a
# build argument (the API's public URL, where the browser opens live run streams).
#
# The image also serves the Groundcrew agent at /agent/ (binaries for linux/amd64 and arm64,
# SHA256SUMS, install.sh, the systemd unit, an example configuration). Its source comes in as
# the named build context `agent-src` (apps/agent): docker build --build-context
# agent-src=apps/agent ... (infra/platform/host/bin/gc-release does).
FROM golang:1.23-bookworm AS agent
ARG AGENT_VERSION=0.1.0
WORKDIR /src
COPY --from=agent-src . .
RUN set -eu; mkdir -p /out; \
    for arch in amd64 arm64; do \
      CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -trimpath -buildvcs=false \
        -ldflags "-s -w -X main.version=$AGENT_VERSION" \
        -o /out/groundcrew-agent-linux-$arch ./cmd/groundcrew-agent; \
    done; \
    cp packaging/install.sh packaging/groundcrew-agent.service packaging/agent.example.yaml /out/; \
    cd /out && sha256sum groundcrew-agent-linux-* > SHA256SUMS

# Only the downloads (docker build --target agent-files -o DIR exports just these).
FROM scratch AS agent-files
COPY --from=agent /out /

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-alpine AS build
WORKDIR /app
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN test -n "$NEXT_PUBLIC_API_URL" && npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
RUN addgroup -S -g 10001 app && adduser -S -D -H -u 10001 -G app app
COPY --from=build --chown=root:root /app/.next/standalone ./
COPY --from=build --chown=root:root /app/.next/static ./.next/static
COPY --from=build --chown=root:root /app/public ./public
COPY --from=agent-files --chown=root:root / ./public/agent
USER 10001:10001
EXPOSE 3000
CMD ["node", "server.js"]
