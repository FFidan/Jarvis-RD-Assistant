ARG NODE_BUILD_IMAGE=node:22.22.2-alpine3.23@sha256:8ea2348b068a9544dae7317b4f3aafcdc032df1647bb7d768a05a5cad1a7683f
# nginxinc/nginx-unprivileged runs as uid 101 (nginx) — no root at runtime.
# Default upstream port is 8080, but our nginx.conf template binds to 3000
# explicitly, so the host mapping (127.0.0.1:3001:3000) is unchanged.
ARG NGINX_RUNTIME_IMAGE=nginxinc/nginx-unprivileged:1.31-alpine3.24

FROM ${NODE_BUILD_IMAGE} AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
ARG CACHE_BUST=unknown
COPY . .
RUN npm run build

FROM ${NGINX_RUNTIME_IMAGE}
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
