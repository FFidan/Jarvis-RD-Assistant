#!/bin/sh
# Generate self-signed TLS certificate for the JARVIS dashboard.
# This script runs automatically on container startup via /docker-entrypoint.d/.
# Certificates are persisted in a Docker volume so they survive restarts.

CERT_DIR="/etc/nginx/certs"
CERT_FILE="${CERT_DIR}/selfsigned.crt"
KEY_FILE="${CERT_DIR}/selfsigned.key"

if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "TLS certificates already exist, skipping generation."
    exit 0
fi

mkdir -p "$CERT_DIR"

echo "Generating self-signed TLS certificate for JARVIS dashboard..."
openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -subj "/CN=jarvis-dashboard" \
    -addext "subjectAltName=DNS:localhost,DNS:jarvis-dashboard,IP:127.0.0.1" \
    2>/dev/null

echo "Self-signed TLS certificate generated at ${CERT_DIR}/"
