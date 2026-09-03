# syntax=docker/dockerfile:1
FROM node:26-alpine AS build
ARG DOCS_BASE=/docs
ENV DOCS_BASE=$DOCS_BASE
WORKDIR /docs
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
# astro bakes the base into asset urls but still emits at dist root; stage the served tree here
# the stub also displaces nginx's stock landing page at the html root
RUN npm run build && mkdir -p "/out${DOCS_BASE}" && cp -r dist/. "/out${DOCS_BASE}/" \
    && { [ "$DOCS_BASE" = / ] \
      || printf '<!doctype html><meta http-equiv="refresh" content="0; url=%s/">' "$DOCS_BASE" \
         > /out/index.html; }

# unprivileged nginx (uid 101, listens 8080) - read-only-rootfs friendly
FROM nginxinc/nginx-unprivileged:1.31-alpine
COPY --from=build /out /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# cap workers: base 'auto' = node cores, ooms the 64mi limit on big nodes
RUN sed -i 's/worker_processes .*/worker_processes 2;/' /etc/nginx/nginx.conf
EXPOSE 8080
