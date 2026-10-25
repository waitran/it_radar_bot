ARG NODE_IMAGE=public.ecr.aws/docker/library/node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914
ARG CADDY_IMAGE=mirror.gcr.io/library/caddy:2-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f
FROM ${NODE_IMAGE} AS frontend
WORKDIR /build
COPY miniapp/frontend/package.json miniapp/frontend/package-lock.json ./
RUN NODE_OPTIONS=--dns-result-order=ipv4first npm ci --no-audit --no-fund
COPY miniapp/frontend ./
ARG VITE_BOT_USERNAME=it_start_radar_bot
ENV VITE_BOT_USERNAME=${VITE_BOT_USERNAME}
RUN npm run typecheck && npm test && npm run build

FROM ${CADDY_IMAGE}
COPY --from=frontend /build/dist /srv
COPY miniapp/deploy/Caddyfile miniapp/deploy/app.caddy /etc/caddy/
COPY miniapp/deploy/start.sh /usr/local/bin/miniapp-web
RUN setcap -r /usr/bin/caddy \
    && chmod 755 /usr/local/bin/miniapp-web \
    && mkdir -p /data /config && chown 10001:10001 /data /config
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/miniapp-web"]
