FROM python:3.12.13-alpine3.22@sha256:a190708a2dec1bd18b1decb539f8e8f5407abaa9bf39cacda583f7f8c11db322
ARG SOURCE_COMMIT_SHA
RUN test "$(printf '%s' "$SOURCE_COMMIT_SHA" | wc -c)" -eq 40 \
    && case "$SOURCE_COMMIT_SHA" in *[!0-9a-f]*) exit 1 ;; *) : ;; esac
LABEL org.opencontainers.image.source="https://github.com/appolon1908-hue/klyrow.com" \
      org.opencontainers.image.revision="$SOURCE_COMMIT_SHA" \
      org.opencontainers.image.version="$SOURCE_COMMIT_SHA"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apk add --no-cache --upgrade \
    libuuid=2.41.6-r1 \
    libcrypto3=3.5.9-r0 \
    libssl3=3.5.9-r0 \
    && rm -f /var/log/apk.log
RUN adduser --disabled-password --gecos "" --uid 10002 klyrow-migrate
WORKDIR /app
COPY apps/gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --no-compile --no-deps -r requirements.txt \
    && pip check
COPY scripts/migrate ./scripts/migrate
COPY migrations ./migrations
USER 10002
ENTRYPOINT ["python", "/app/scripts/migrate"]
