ARG NODE_BUILD_IMAGE=node:22.14-alpine3.21
# nginxinc/nginx-unprivileged runs as uid 101 (nginx) — no root at runtime.
# Default upstream port is 8080, but our nginx.conf template binds to 3000
# explicitly, so the host mapping (127.0.0.1:3001:3000) is unchanged.
ARG NGINX_RUNTIME_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine3.21

FROM ${NODE_BUILD_IMAGE} AS build
WORKDIR /app
COPY package*.json ./
# react-joyride 2.9.3 still declares a React 15-18 peer range while this app
# runs React 19. Keep the lockfile authoritative and tolerate that stale peer
# range in reproducible Docker builds.
RUN npm ci --legacy-peer-deps
ARG CACHE_BUST=unknown
COPY . .
RUN npm run build

FROM ${NGINX_RUNTIME_IMAGE}
# Switch to root only for package install + file setup; drop back to nginx after.
USER root
RUN apk add --no-cache openssl
COPY scripts/generate-certs.sh /docker-entrypoint.d/00-generate-certs.sh
RUN chmod +x /docker-entrypoint.d/00-generate-certs.sh
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY nginx-security-headers.conf /etc/nginx/nginx-security-headers.conf
# Prefix with 00- so this file sorts before default.conf in conf.d include
# order — the log_format "real_ip" defined here must be parsed before the
# access_log directive in default.conf references it (nginx processes includes
# sequentially in alphabetical order within each glob).
COPY nginx-rate-limit.conf /etc/nginx/conf.d/00-nginx-rate-limit.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER nginx
EXPOSE 3000
