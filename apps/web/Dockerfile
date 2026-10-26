# Production image for the web app: Next.js standalone server, non-root, no source or dev
# dependencies. NEXT_PUBLIC_API_URL is inlined into the browser code at build time, so it is a
# build argument (the API's public URL, where the browser opens live run streams).
#
# The image also serves the Groundcrew agent at /agent/ (binaries for linux/amd64 and arm64,
# SHA256SUMS and its release signature SHA256SUMS.sig, install.sh, the systemd unit, an example
# configuration). Its source comes in as
# the named build context `agent-src` (apps/agent): docker build --build-context
# agent-src=apps/agent ... (infra/platform/host/bin/gc-release does).
# Pinned by digest, the same image apps/agent/go.sh uses, so these builds match the signed release
# byte for byte (apps/agent/release.sh sign).
# Always the build machine's platform: it cross-compiles both binaries (also for agent-image arm64).
FROM --platform=$BUILDPLATFORM golang:1.23-bookworm@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db AS agent
WORKDIR /src
COPY --from=agent-src . .
# apps/agent/VERSION is the agent's version (the API's AGENT_VERSION must match it). The build
# stops unless the binaries are exactly those in the signed apps/agent/release/SHA256SUMS: the
# server only serves what the release key's holder signed.
RUN set -eu; mkdir -p /out; AGENT_VERSION=$(cat VERSION); \
    for arch in amd64 arm64; do \
      CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -trimpath -buildvcs=false \
        -ldflags "-s -w -buildid= -X main.version=$AGENT_VERSION" \
        -o /out/groundcrew-agent-linux-$arch ./cmd/groundcrew-agent; \
    done; \
    cd /out && sha256sum groundcrew-agent-linux-* > SHA256SUMS; \
    if ! cmp -s SHA256SUMS /src/release/SHA256SUMS; then \
      echo "The agent binaries do not match the signed release (apps/agent/release/SHA256SUMS)."; \
      echo "Built:"; cat SHA256SUMS; echo "Signed:"; cat /src/release/SHA256SUMS 2>/dev/null || echo "(none)"; \
      echo "Run apps/agent/release.sh sign after any agent change, and commit the result."; \
      exit 1; \
    fi; \
    cp /src/release/SHA256SUMS.sig /out/; \
    cp /src/packaging/install.sh /src/packaging/groundcrew-agent.service /src/packaging/agent.example.yaml \
      /src/packaging/ecs-agent.yaml /out/

# Only the downloads (docker build --target agent-files -o DIR exports just these).
FROM scratch AS agent-files
COPY --from=agent /out /

# The agent as a container, for Kubernetes and ECS: the same signed binary, CA certificates,
# nothing else; non-root. docker build --target agent-image --platform linux/<arch>
# (gc-release publishes it at <domain>/public/groundcrew-agent:<version>).
FROM scratch AS agent-image
ARG TARGETARCH
COPY --from=agent /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=agent /out/groundcrew-agent-linux-${TARGETARCH} /groundcrew-agent
USER 65532:65532
ENTRYPOINT ["/groundcrew-agent"]
CMD ["run", "--config", "/etc/groundcrew-agent/agent.yaml"]

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-alpine AS build
WORKDIR /app
ARG NEXT_PUBLIC_API_URL
# Optional: the product video the Example workspace's banner links to.
ARG NEXT_PUBLIC_PRODUCT_VIDEO_URL=
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_PRODUCT_VIDEO_URL=$NEXT_PUBLIC_PRODUCT_VIDEO_URL
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
