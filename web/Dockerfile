# Build the console, then serve it from an unprivileged nginx. No Node in the runtime image.
FROM node:22.11.0-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27.2-alpine
# The image renders /etc/nginx/templates/*.template into conf.d with envsubst at start-up.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
ENV GATEWAY_UPSTREAM=http://app:8000 \
    OIDC_ORIGIN="" \
    NGINX_ENVSUBST_FILTER="^(GATEWAY_UPSTREAM|OIDC_ORIGIN)$"
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
