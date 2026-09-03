# syntax=docker/dockerfile:1
FROM node:24-alpine AS build
ARG VERSION
ENV VITE_APP_VERSION=$VERSION
WORKDIR /ui
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run build

# unprivileged nginx (uid 101, listens 8080) - read-only-rootfs friendly
FROM nginxinc/nginx-unprivileged:1.31-alpine
COPY --from=build /ui/build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# cap workers: base 'auto' = node cores, ooms the 64mi limit on big nodes
RUN sed -i 's/worker_processes .*/worker_processes 2;/' /etc/nginx/nginx.conf
EXPOSE 8080
