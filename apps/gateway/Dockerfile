FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS web-build
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
WORKDIR /build
RUN corepack enable && corepack prepare pnpm@10.32.1 --activate
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/web/ ./
RUN pnpm build

FROM python:3.12.13-alpine3.22@sha256:a190708a2dec1bd18b1decb539f8e8f5407abaa9bf39cacda583f7f8c11db322
ARG SOURCE_COMMIT_SHA
RUN test "$(printf '%s' "$SOURCE_COMMIT_SHA" | wc -c)" -eq 40 \
    && case "$SOURCE_COMMIT_SHA" in *[!0-9a-f]*) exit 1 ;; *) : ;; esac
LABEL org.opencontainers.image.source="https://github.com/appolon1908-hue/klyrow.com" \
      org.opencontainers.image.revision="$SOURCE_COMMIT_SHA" \
      org.opencontainers.image.version="$SOURCE_COMMIT_SHA"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KLYROW_SOURCE_SHA=$SOURCE_COMMIT_SHA
RUN apk add --no-cache --upgrade \
    libuuid=2.41.6-r1 \
    libcrypto3=3.5.9-r0 \
    libssl3=3.5.9-r0 \
    && rm -f /var/log/apk.log
RUN adduser --disabled-password --gecos "" --uid 10001 klyrow
WORKDIR /app
COPY apps/gateway/requirements.txt .
RUN /usr/local/bin/python3 -m pip install --no-cache-dir --no-compile --no-deps \
    -r requirements.txt \
    && /usr/local/bin/python3 -m pip check
COPY apps/gateway/app ./app
# The base gateway serves the same reviewed Vue artifact for /login, /app and
# /admin. The optional dedicated web image remains available for split-edge
# deployments, but the base Compose application can no longer return a 503
# merely because the SPA was absent from the Python image.
COPY --from=web-build /build/dist ./app/auth_web
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --retries=5 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health')"
CMD ["python3", "-m", "uvicorn", "app.platform:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
