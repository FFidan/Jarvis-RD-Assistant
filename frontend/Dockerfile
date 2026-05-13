ARG NODE_BUILD_IMAGE=node:22.14-alpine3.21
ARG NGINX_RUNTIME_IMAGE=nginx:1.27-alpine3.21

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
RUN apk add --no-cache openssl
COPY scripts/generate-certs.sh /docker-entrypoint.d/00-generate-certs.sh
RUN chmod +x /docker-entrypoint.d/00-generate-certs.sh
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY nginx-security-headers.conf /etc/nginx/nginx-security-headers.conf
COPY nginx-rate-limit.conf /etc/nginx/conf.d/nginx-rate-limit.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 3000
