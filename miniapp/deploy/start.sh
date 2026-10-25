#!/bin/sh
set -eu
umask 077
# Restrict Caddyfile environment substitution to a DNS name, never arbitrary config.
if ! printf '%s' "${MINIAPP_DOMAIN:-}" | grep -Eq '^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$'; then
    echo 'MINIAPP_DOMAIN must be a DNS hostname without scheme, port or path' >&2
    exit 1
fi
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
