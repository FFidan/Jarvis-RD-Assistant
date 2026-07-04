from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.perf.litellm_route_alias import (
    DeploymentRecord,
    OpenAICompatibleRoute,
    RouteAliasError,
    build_openai_compatible_params,
    db_deployment_ids,
    records_from_snapshot,
    snapshot_payload,
    yaml_alias_count,
)


def _record(
    alias: str,
    dep_id: str,
    *,
    db_model: bool = True,
    model: str = "ollama_chat/qwen3:8b",
) -> DeploymentRecord:
    return DeploymentRecord(
        model_name=alias,
        litellm_params={"model": model},
        model_info={"id": dep_id, "db_model": db_model},
    )


def test_openai_compatible_params_require_v1_suffix() -> None:
    with pytest.raises(RouteAliasError, match="/v1"):
        build_openai_compatible_params(
            OpenAICompatibleRoute(
                served_model="Qwen/Qwen2.5-7B-Instruct-AWQ",
                api_base="http://vllm:8080",
                api_key="EMPTY",
                timeout=300,
                num_retries=0,
                temperature=0.2,
            )
        )


def test_openai_compatible_params_prefix_served_model() -> None:
    params = build_openai_compatible_params(
        OpenAICompatibleRoute(
            served_model="Qwen/Qwen2.5-7B-Instruct-AWQ",
            api_base="http://vllm:8080/v1/",
            api_key="EMPTY",
            timeout=300,
            num_retries=1,
            temperature=0.1,
        )
    )

    assert params == {
        "model": "openai/Qwen/Qwen2.5-7B-Instruct-AWQ",
        "api_base": "http://vllm:8080/v1",
        "api_key": "EMPTY",
        "timeout": 300,
        "num_retries": 1,
        "temperature": 0.1,
    }


def test_db_deployment_ids_ignore_yaml_seeded_rows() -> None:
    records = [
        _record("smart", "db-smart"),
        _record("smart", "yaml-smart", db_model=False),
        _record("fast", "db-fast"),
    ]

    assert db_deployment_ids(records, "smart") == ["db-smart"]
    assert yaml_alias_count(records, "smart") == 1


def test_snapshot_round_trips_db_deployments_only(tmp_path: Path) -> None:
    snapshot = snapshot_payload(
        "smart",
        [
            _record("smart", "db-smart", model="ollama_chat/qwen3:30b-a3b"),
            _record("smart", "yaml-smart", db_model=False),
            _record("fast", "db-fast"),
        ],
    )
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    records = records_from_snapshot(path, "smart")

    assert records == [
        DeploymentRecord(
            model_name="smart",
            litellm_params={"model": "ollama_chat/qwen3:30b-a3b"},
            model_info={"id": "db-smart", "db_model": True},
        )
    ]


def test_snapshot_rejects_alias_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot_payload("smart", [])), encoding="utf-8")

    with pytest.raises(RouteAliasError, match="does not match"):
        records_from_snapshot(path, "fast")


def test_snapshot_rejects_unmasked_api_key() -> None:
    record = DeploymentRecord(
        model_name="smart",
        litellm_params={"model": "openai/example", "api_key": "plain-live-value"},
        model_info={"id": "db-smart", "db_model": True},
    )

    with pytest.raises(RouteAliasError, match="unmasked"):
        snapshot_payload("smart", [record])


def test_snapshot_allows_masked_api_key() -> None:
    record = DeploymentRecord(
        model_name="smart",
        litellm_params={"model": "openai/example", "api_key": "sk-***"},
        model_info={"id": "db-smart", "db_model": True},
    )

    payload = snapshot_payload("smart", [record])

    assert payload["deployments"][0]["litellm_params"]["api_key"] == "sk-***"
