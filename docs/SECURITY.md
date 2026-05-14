# Security Notes

Operational security guidance for JARVIS RD Assistant deployments. This is a
living document; new sections are added per audit closeout.

## Pulse Model Signing

The Pulse classifier persists a serialized scikit-learn `LogisticRegression`
model into the `pulse_models` table as an HMAC-signed pickle blob. Verification
happens in `services/paper_ingestion/paper_ingestion/pulse/training.py::_verify_and_unpickle`
before `pickle.loads` is called — without the HMAC gate, anyone with DB write
access could forge a blob and trigger RCE.

### Configuration

The HMAC key is resolved at call time, in this order:

1. **`JARVIS_MODEL_HMAC_KEY`** (preferred) — a dedicated secret used solely for
   signing model blobs. Generate with `openssl rand -hex 32`. Keeping this
   separate from `JARVIS_API_KEY` means a compromise of the HTTP bearer does
   not also let an attacker forge model blobs, and vice versa.
2. **Derived from `JARVIS_API_KEY`** — when `JARVIS_MODEL_HMAC_KEY` is unset,
   the signing key is `sha256(b"model-signing:" + JARVIS_API_KEY)`. The
   `model-signing:` prefix domain-separates this key from any direct use of
   the bearer.

If neither is set, `_hmac_key()` raises `RuntimeError`. The previous
public-literal fallback (`"jarvis-dev-unsafe-hmac-key"`) was removed by audit
H14 (2026-05-14).

In production (`ENVIRONMENT=production`), `validate_production_config()` —
called at lifespan startup — refuses to start unless at least one of the two
paths above is configured.

### Key Rotation

There is no in-place rotation framework. To rotate:

1. Update `JARVIS_MODEL_HMAC_KEY` (or `JARVIS_API_KEY` if you are relying on
   derivation).
2. Restart the affected services.
3. Existing rows in `pulse_models` will fail HMAC verification on load. The
   service handles this gracefully: `load_active_classifier` returns
   `(None, {"available": False, "degradation_reason": "active model could
   not be loaded"})`, and the scoring path falls back to zeros until a fresh
   model is trained.
4. The nightly `pulse.train_classifier` job (cron `30 3 * * *`) re-trains and
   persists a new model signed with the new key. No manual migration is
   required.

If you need an immediate re-train rather than waiting for the cron tick,
enqueue `pulse.train_classifier` via the jobs API (one job per user with
ratings).
