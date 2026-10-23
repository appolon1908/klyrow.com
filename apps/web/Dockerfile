FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS build
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
WORKDIR /build
RUN corepack enable && corepack prepare pnpm@10.32.1 --activate
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/web/ ./
RUN pnpm build

FROM nginxinc/nginx-unprivileged:1.30.6-alpine3.24@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03
ARG SOURCE_COMMIT_SHA
RUN test "$(printf '%s' "$SOURCE_COMMIT_SHA" | wc -c)" -eq 40 \
    && case "$SOURCE_COMMIT_SHA" in *[!0-9a-f]*) exit 1 ;; *) : ;; esac
LABEL org.opencontainers.image.source="https://github.com/appolon1908-hue/klyrow.com" \
      org.opencontainers.image.revision="$SOURCE_COMMIT_SHA" \
      org.opencontainers.image.version="$SOURCE_COMMIT_SHA"
# Pin an exact published patched Nginx runtime rather than a moving minor tag.
# Install the independently checksum-pinned Alpine packages from the source tree;
# neither a moving repository index nor network access participates in this layer.
USER root
COPY apps/web/vendor/libexpat-2.9.0-r0.apk /opt/klyrow/vendor/libexpat-2.9.0-r0.apk
COPY apps/web/vendor/libuuid-2.42.3-r1.apk /opt/klyrow/vendor/libuuid-2.42.3-r1.apk
COPY apps/web/vendor/libx11-1.8.14-r0.apk /opt/klyrow/vendor/libx11-1.8.14-r0.apk
COPY apps/web/vendor/pcre2-10.49-r0.apk /opt/klyrow/vendor/pcre2-10.49-r0.apk
COPY apps/web/vendor/tiff-4.7.2-r0.apk /opt/klyrow/vendor/tiff-4.7.2-r0.apk
COPY apps/web/vendor/SHA256SUMS /opt/klyrow/vendor/SHA256SUMS
RUN cd /opt/klyrow/vendor \
    && sha256sum -c SHA256SUMS \
    && apk verify --keys-dir /etc/apk/keys libexpat-2.9.0-r0.apk \
    && apk verify --keys-dir /etc/apk/keys libuuid-2.42.3-r1.apk \
    && apk verify --keys-dir /etc/apk/keys libx11-1.8.14-r0.apk \
    && apk verify --keys-dir /etc/apk/keys pcre2-10.49-r0.apk \
    && apk verify --keys-dir /etc/apk/keys tiff-4.7.2-r0.apk \
    && apk add --no-cache --no-network ./libexpat-2.9.0-r0.apk ./libuuid-2.42.3-r1.apk ./libx11-1.8.14-r0.apk ./pcre2-10.49-r0.apk ./tiff-4.7.2-r0.apk \
    && apk info --exists libexpat=2.9.0-r0 \
    && apk info --exists libuuid=2.42.3-r1 \
    && apk info --exists libx11=1.8.14-r0 \
    && apk info --exists pcre2=10.49-r0 \
    && apk info --exists tiff=4.7.2-r0 \
    && rm -f /var/log/apk.log \
    && nginx -v 2>&1 | grep -q 'nginx/1.30.6'
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
RUN printf '{"source_sha":"%s"}\n' "$SOURCE_COMMIT_SHA" > /usr/share/nginx/html/version.json \
    && chmod 0444 /usr/share/nginx/html/version.json
USER 101
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=5s --retries=5 CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
