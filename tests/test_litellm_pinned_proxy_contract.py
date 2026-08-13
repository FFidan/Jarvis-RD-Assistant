"""Exact-image contract for guarded LiteLLM custom-provider delivery."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
IMAGE = (
    "docker.litellm.ai/berriai/litellm@"
    "sha256:29252f25ed1b538d44f6b76ec97412c5537a180b39ede744b9f3e86ffdd278f5"
)


def _write_tls_material(directory: Path) -> tuple[Path, Path, Path]:
    """Create a one-test CA and a server certificate for the Docker name ``vllm``."""
    now = dt.datetime.now(dt.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LiteLLM contract CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vllm")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("vllm")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


@pytest.mark.integration
def test_exact_image_routes_custom_chat_through_guarded_session(tmp_path: Path) -> None:
    """A bad environment proxy cannot divert the real custom-provider request."""
    ca_path, cert_path, key_path = _write_tls_material(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        """model_list:
  - model_name: vllm-test
    litellm_params:
      model: openai/test-model
      api_base: https://vllm:18443/v1
      api_key: test-provider-key
  - model_name: ollama-test
    litellm_params:
      model: ollama_chat/test-model
      api_base: http://ollama:18080
  - model_name: blocked-test
    litellm_params:
      model: openai/test-model
      api_base: http://rebind.example:18080/v1
      api_key: test-provider-key
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
litellm_settings:
  drop_params: true
""",
        encoding="utf-8",
    )
    harness = tmp_path / "harness.py"
    harness.write_text(
        """import http.server
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

seen = []
sni = []

class Provider(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        seen.append((self.path, self.headers.get("Host"), request.get("model")))
        if self.path == "/api/chat":
            response = {
                "model": "test-model",
                "created_at": "2026-08-09T00:00:00Z",
                "message": {"role": "assistant", "content": "ollama-ok"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 1,
                "eval_count": 1,
            }
        else:
            response = {
                "id": "chatcmpl-pinned",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "vllm-ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return

provider_http = http.server.ThreadingHTTPServer(("127.0.0.1", 18080), Provider)
provider_tls = http.server.ThreadingHTTPServer(("127.0.0.1", 18443), Provider)
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain("/tmp/server.pem", "/tmp/server.key")
tls.set_servername_callback(lambda _sock, name, _ctx: sni.append(name))
provider_tls.socket = tls.wrap_socket(provider_tls.socket, server_side=True)
threading.Thread(target=provider_http.serve_forever, daemon=True).start()
threading.Thread(target=provider_tls.serve_forever, daemon=True).start()
proxy = subprocess.Popen(
    [
        sys.executable,
        "/app/pinned_launcher.py",
        "--config",
        "/tmp/config.yaml",
        "--host",
        "127.0.0.1",
        "--port",
        "4000",
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)
try:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proxy.poll() is not None:
            output = proxy.stdout.read() if proxy.stdout else ""
            raise RuntimeError("proxy exited early: " + output)
        try:
            with socket.create_connection(("127.0.0.1", 4000), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    else:
        raise RuntimeError("proxy did not start")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def send(model):
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 8,
        }).encode()
        request = urllib.request.Request(
            "http://127.0.0.1:4000/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": "Bearer test-master-key-123456",
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode()) from exc

    try:
        send("blocked-test")
    except RuntimeError:
        pass
    else:
        raise AssertionError("private rebind destination was not rejected")
    assert seen == []

    vllm = send("vllm-test")
    ollama = send("ollama-test")
    assert vllm["choices"][0]["message"]["content"] == "vllm-ok"
    assert ollama["choices"][0]["message"]["content"] == "ollama-ok"
    assert seen == [
        ("/v1/chat/completions", "vllm:18443", "test-model"),
        ("/api/chat", "ollama:18080", "test-model"),
    ]
    assert sni == ["vllm"]
finally:
    proxy.terminate()
    try:
        proxy.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proxy.kill()
        proxy.wait(timeout=10)
    provider_http.shutdown()
    provider_tls.shutdown()
""",
        encoding="utf-8",
    )

    command = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python3",
        "-e",
        "HTTP_PROXY=http://127.0.0.1:1",
        "-e",
        "HTTPS_PROXY=http://127.0.0.1:1",
        "-e",
        "NO_PROXY=ollama",
        "-e",
        "LITELLM_MASTER_KEY=test-master-key-123456",
        "-v",
        f"{ROOT / 'litellm' / 'pinned_launcher.py'}:/app/pinned_launcher.py:ro",
        "-v",
        f"{ROOT / 'libs/jarvis_common/jarvis_common/pinned_transport.py'}:"
        "/app/jarvis_common/pinned_transport.py:ro",
        "-v",
        f"{ROOT / 'libs/jarvis_common/jarvis_common/net.py'}:/app/jarvis_common/net.py:ro",
        "-v",
        f"{config}:/tmp/config.yaml:ro",
        "-v",
        f"{harness}:/tmp/harness.py:ro",
        "-v",
        f"{ca_path}:/app/.venv/lib/python3.13/site-packages/certifi/cacert.pem:ro",
        "-v",
        f"{cert_path}:/tmp/server.pem:ro",
        "-v",
        f"{key_path}:/tmp/server.key:ro",
        "--add-host",
        "vllm:127.0.0.1",
        "--add-host",
        "ollama:127.0.0.1",
        "--add-host",
        "rebind.example:127.0.0.1",
        IMAGE,
        "/tmp/harness.py",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
