"""Contracts for the versioned PostgreSQL ownership inventory."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from jarvis_common.jobs import JOB_HANDLER_OWNER

from scripts.database_inventory import (
    cross_domain_writes,
    database_caller_scripts,
    inventory_queries,
    inventory_schema_objects,
    load_manifest,
    validate_inventory,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "db" / "ownership-manifest.json"


def _declared_queue_owners(manifest: dict[str, Any]) -> dict[str, str]:
    """Return the queue owner declared for each registered job kind."""
    return {
        kind: queue
        for queue, queue_data in manifest["queues"].items()
        for kind in queue_data["job_kinds"]
    }


def test_database_inventory_matches_ownership_manifest() -> None:
    """Every current schema object, query relation, and write seam is classified."""
    manifest = load_manifest(_MANIFEST_PATH)
    queries = inventory_queries(_REPO_ROOT)
    schema_objects = inventory_schema_objects(_REPO_ROOT)

    assert validate_inventory(manifest, queries, schema_objects, _REPO_ROOT) == []
    assert len(schema_objects["tables"]) == 70
    assert manifest["target_release"] == "v1.2.6"
    assert manifest["compatibility_baseline"]["source_release"] == "v1.2.5"


def test_schema_qualified_owner_functions_remain_distinct() -> None:
    """Same-named owner capabilities in separate schemas remain inventory-visible."""
    functions = inventory_schema_objects(_REPO_ROOT)["functions"]

    assert "research.erase_user_data" in functions
    assert "learning.erase_user_data" in functions
    assert "erase_user_data" not in functions


def test_retained_migration_fingerprints_match_files() -> None:
    """Every retained migration has the exact digest recorded in the manifest."""
    manifest = load_manifest(_MANIFEST_PATH)
    migrations_dir = _REPO_ROOT / "db" / "migrations"
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(migrations_dir.glob("*.sql"))
    }

    baseline = manifest["compatibility_baseline"]
    declared = {entry["path"]: entry["sha256"] for entry in baseline["retained_migrations"]}
    assert actual == declared
    assert baseline["unhashed_revisions"] == {
        "first": 1,
        "last": 101,
        "marker": "squashed_baseline_source_unavailable",
    }


def test_queue_manifest_matches_runtime_registry() -> None:
    """The ownership manifest and runtime queue registry classify the same jobs."""
    manifest = load_manifest(_MANIFEST_PATH)

    assert _declared_queue_owners(manifest) == JOB_HANDLER_OWNER


def test_supported_database_callers_match_source() -> None:
    """Every detected operator database caller is explicitly supported."""
    manifest = load_manifest(_MANIFEST_PATH)

    assert database_caller_scripts(_REPO_ROOT) == manifest["supported_database_callers"]


def test_every_dynamic_query_is_counted_per_source_path() -> None:
    """Every computed SQL statement contributes to its reviewed source count."""
    manifest = load_manifest(_MANIFEST_PATH)
    dynamic_counts = Counter(
        record.path for record in inventory_queries(_REPO_ROOT) if record.dynamic
    )

    assert dict(sorted(dynamic_counts.items())) == manifest["reviewed_dynamic_sql"]


def test_write_targets_exclude_joined_reads_and_retired_foreign_locks() -> None:
    """Joined reads stay read-only and retired foreign row locks stay absent."""
    records = inventory_queries(_REPO_ROOT)
    cards_with_paper_reads = [
        record
        for record in records
        if record.path.endswith("learning_engine/routers/cards.py") and "papers" in record.relations
    ]
    executive_user_reads = [
        record
        for record in records
        if record.path.endswith("learning_engine/routers/executive.py")
        and "users" in record.relations
    ]
    assert cards_with_paper_reads
    assert all(not record.write_relations for record in cards_with_paper_reads)
    assert not executive_user_reads


def test_required_transition_seams_match_current_writes() -> None:
    """Declared transition seams exactly match current cross-domain writes."""
    manifest = load_manifest(_MANIFEST_PATH)
    detected = {
        (write.writer, write.relation, write.destination)
        for write in cross_domain_writes(manifest, inventory_queries(_REPO_ROOT))
    }
    declared = {
        (seam["current_writer"], relation, seam["destination"])
        for seam in manifest["transition_seams"]
        for relation in seam["relations"]
    }

    assert declared == detected


def test_erasure_contract_is_closed_and_bounded() -> None:
    """Erasure transitions, acknowledgements, and retries form a closed contract."""
    erasure = load_manifest(_MANIFEST_PATH)["erasure"]
    states = set(erasure["states"])
    transitions = erasure["transitions"]

    assert set(transitions) == states
    assert erasure["initial_state"] in states
    assert set(erasure["terminal_states"]).issubset(states)
    assert {
        destination for destinations in transitions.values() for destination in destinations
    }.issubset(states)
    assert set(erasure["required_acknowledgements"]) == {"qdrant", "research", "learning"}
    assert {"request_id", "residual_points", "scan_completed_at", "acknowledged_at"}.issubset(
        erasure["qdrant_acknowledgement"]
    )
    retry = erasure["retry"]
    assert 0 < retry["alert_after_attempt"] < retry["max_attempts"]
    assert retry["initial_delay_seconds"] <= retry["maximum_delay_seconds"]
    assert retry["persist_resume_state"] is True
