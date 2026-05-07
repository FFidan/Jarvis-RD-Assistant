#!/bin/sh
# Generate self-signed TLS certificate for the JARVIS dashboard.
# This script runs automatically on container startup via /docker-entrypoint.d/.
# Certificates are persisted in a Docker volume so they survive restarts.
#
# The Subject Alternative Name (SAN) is controlled by the JARVIS_CERT_SAN
# environment variable, which is set by setup.sh based on access mode:
#   localhost: DNS:localhost,IP:127.0.0.1
#   LAN:       DNS:localhost,IP:127.0.0.1,IP:<LAN_IP>
#   tunnel:    DNS:localhost,IP:127.0.0.1,DNS:<TUNNEL_HOSTNAME>

CERT_DIR="/etc/nginx/certs"
CERT_FILE="${CERT_DIR}/selfsigned.crt"
KEY_FILE="${CERT_DIR}/selfsigned.key"

if [ "${JARVIS_SKIP_SELFSIGNED_GEN:-false}" = "true" ]; then
    echo "Skipping self-signed cert generation (JARVIS_SKIP_SELFSIGNED_GEN=true)"
    exit 0
fi
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    # Reuse existing cert if it is valid for more than 30 days; otherwise regenerate.
    if openssl x509 -checkend $((30*86400)) -noout -in "$CERT_FILE" 2>/dev/null; then
        echo "[generate-certs] existing cert valid for >30 days; skipping regen"
        exit 0
    else
        echo "[generate-certs] existing cert expires in <=30 days; regenerating"
    fi
fi

mkdir -p "$CERT_DIR"

# Read SAN from env; fall back to localhost-only if not set.
SAN="${JARVIS_CERT_SAN:-DNS:localhost,IP:127.0.0.1}"

echo "Generating self-signed TLS certificate for JARVIS dashboard (SAN: ${SAN})..."
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -subj "/CN=jarvis-dashboard" \
    -addext "subjectAltName=DNS:jarvis-dashboard,${SAN}" \
    2>/dev/null

chmod 600 "$KEY_FILE"
echo "Self-signed TLS certificate generated at ${CERT_DIR}/"
