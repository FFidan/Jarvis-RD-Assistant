"""Pure-unit Pydantic tests for the LE CreateJobRequest model.

Behavioral router-level coverage (create_job enqueue, get_job owner/non-owner,
list_jobs scoping, cancel_job ownership, LE-002 str/int user_id coercion) is in
services/learning_engine/tests/contract/test_le_jobs_contract.py — the
handler-bypass mock-unit equivalents that previously lived here were retired in
the Cluster 11 contract pass on 2026-05-22 with survivor citations listed in
the contract file's module docstring.

These remaining tests cover the Pydantic validation surface of CreateJobRequest
(discriminated-union kind tag, mutable-default invariant) — pure-unit shape per
docs/contracts/07-testing.md §1.1; no I/O.
"""

from __future__ import annotations

import pytest
from learning_engine.routers.jobs import CreateJobRequest
from pydantic import ValidationError


def test_create_job_request_rejects_blank_kind():
    # In discriminated mode (card.generate schema) an unknown/blank kind tag
    # is rejected by the Pydantic model_validator with a union_tag_invalid error.
    with pytest.raises(ValidationError):
        CreateJobRequest(kind="   ")


def test_create_job_request_accepts_valid_kind():
    # RD-DA-001: card.generate now requires paper_id + deck_id in payload.
    req = CreateJobRequest(kind="card.generate", payload={"paper_id": 1, "deck_id": 1})
    assert req.kind == "card.generate"
    assert req.payload == {"paper_id": 1, "deck_id": 1}


@pytest.mark.parametrize("bad_kind", ["secret.internal", "totally.unknown.kind"])
def test_create_job_rejects_disallowed_kind(bad_kind):
    """Unknown kinds are rejected before the handler runs.

    With discriminated-union validation (RD-DA-001), ``CreateJobRequest`` only
    accepts ``kind="card.generate"``; any other kind tag raises ``ValidationError``
    at parse time (HTTP 422 via FastAPI) — the handler's 400 allowlist guard is
    never reached.

    Parametrized over two distinct unknown-kind strings to confirm the guard is
    generic (not a hard-coded string match).
    B2-18: test_create_job_unsupported_kind_returns_400 collapsed into this parametrize;
    both original kind values are preserved as cases.
    """
    with pytest.raises(ValidationError):
        CreateJobRequest(kind=bad_kind)


def test_create_job_request_default_payload_is_empty_dict():
    """Payload is stored as given and no shared mutable state exists.

    RD-DA-001: discriminated mode requires paper_id + deck_id for card.generate,
    so there is no longer a zero-argument constructor.  The test verifies that
    the explicit payload passed at construction is stored correctly on the
    instance (SYM-002 mutable-default invariant remains; see companion test).
    """
    req = CreateJobRequest(kind="card.generate", payload={"paper_id": 1, "deck_id": 2})
    assert req.payload == {"paper_id": 1, "deck_id": 2}


def test_create_job_request_payload_not_shared_between_instances():
    """Mutating one instance's payload must not affect another (SYM-002 — no mutable default).

    RD-DA-001: card.generate requires paper_id + deck_id; use explicit payloads.
    """
    req_a = CreateJobRequest(kind="card.generate", payload={"paper_id": 1, "deck_id": 1})
    req_b = CreateJobRequest(kind="card.generate", payload={"paper_id": 2, "deck_id": 2})
    req_a.payload["injected"] = True
    assert "injected" not in req_b.payload, (
        "Mutable default detected: req_b.payload was mutated via req_a"
    )
