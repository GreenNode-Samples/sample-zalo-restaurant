# Caddy with the rate-limit module (the stock Caddy image has no rate limiting).
# Both Caddy images and the plugin are pinned: bump them together and rebuild.
FROM caddy:2.11.6-builder AS builder
RUN xcaddy build --with github.com/mholt/caddy-ratelimit@v0.1.0

FROM caddy:2.11.6
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
