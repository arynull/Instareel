FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN npm install

FROM node:20-alpine AS builder
WORKDIR /app
# Build-time only (both are baked into the bundle / routes manifest):
# - NEXT_PUBLIC_API_URL: what the BROWSER calls. Empty = same-origin
#   (nginx profile, recommended). Set to http://host:8000 only when exposing
#   the frontend container directly.
# - API_INTERNAL_URL: where the Next SERVER proxies /api/* (rewrites are
#   resolved at build time) — must resolve inside compose network.
ARG NEXT_PUBLIC_API_URL=
ARG API_INTERNAL_URL=http://backend:8000
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV API_INTERNAL_URL=$API_INTERNAL_URL
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
# The standalone server binds to $HOSTNAME when set — Docker defaults it to
# the container ID (resolves to the container IP only), so loopback checks
# (healthcheck, exec) get connection refused while nginx still works.
# Bind all interfaces instead.
ENV HOSTNAME="0.0.0.0"
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
