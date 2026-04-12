ARG NODE_BUILD_IMAGE=node:22-alpine
ARG NGINX_RUNTIME_IMAGE=nginx:alpine

FROM ${NODE_BUILD_IMAGE} AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM ${NGINX_RUNTIME_IMAGE}
RUN apk add --no-cache openssl
COPY scripts/generate-certs.sh /docker-entrypoint.d/00-generate-certs.sh
RUN chmod +x /docker-entrypoint.d/00-generate-certs.sh
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 3000
