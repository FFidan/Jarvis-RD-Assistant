#!/usr/bin/env bash
# Generate locally-trusted dev certs via mkcert.
# Idempotent — re-run anytime.
set -euo pipefail

CERTS_DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
CERT_FILE="$CERTS_DIR/cert.pem"
KEY_FILE="$CERTS_DIR/key.pem"

if ! command -v mkcert >/dev/null 2>&1; then
    cat >&2 <<EOF
mkcert is not installed.

Install it:
  macOS:          brew install mkcert nss
  Debian/Ubuntu: sudo apt-get install -y mkcert libnss3-tools
  Fedora:         sudo dnf install -y mkcert nss-tools
  Arch:           sudo pacman -S mkcert nss
  Other:         https://github.com/FiloSottile/mkcert#installation

The NSS package lets mkcert add its CA to Firefox and Chromium-compatible
browser stores on this operating system.
EOF
    exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
    printf '%s\n' "openssl is required to inspect and validate the generated certificate." >&2
    exit 1
fi

# `mkcert -install` is idempotent and repairs a trust store whose CA entry was
# removed even when CAROOT/rootCA.pem still exists. Run it every time so
# `make certs` means both "certificate exists" and "this host trusts its CA".
echo "Installing mkcert root CA into local trust store..."
mkcert -install

mkdir -p "$CERTS_DIR"

# Regenerate if missing or expiring within 30 days
NEEDS_REGEN=1
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    if openssl x509 -in "$CERT_FILE" -noout -checkend 2592000 >/dev/null 2>&1; then
        NEEDS_REGEN=0
    fi
fi

if [ "$NEEDS_REGEN" -eq 1 ]; then
    echo "Generating mkcert certs at $CERTS_DIR ..."
    mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" \
        jarvis.localhost localhost 127.0.0.1 ::1
fi

chmod 0644 "$CERT_FILE"
chmod 0600 "$KEY_FILE"

echo "Certs ready: $CERT_FILE, $KEY_FILE"
echo "SAN:"
openssl x509 -in "$CERT_FILE" -noout -text | grep -A1 "Subject Alternative Name" || true
