# infra/vector.toml

Vector optionally receives redacted structured application logs over UDP and writes them to its stdout.

## Transport

`make observability-up` sets `LOG_FORWARD_ADDRESS=vector:9000` for core application
services. Their bounded forwarder exports only safe metadata and drops telemetry on
queue pressure, DNS failure, or Vector outage without delaying stdout. Vector keeps
the metadata-only aggregate available through `docker compose logs vector`; it has no Docker
socket, product API, product database credentials, or persistent log volume.

When the observability profile is off, applications emit their usual structured
stdout and operators use each service's `docker compose logs` output.

## Version
Pinned via `VECTOR_IMAGE` in `versions.env`.
