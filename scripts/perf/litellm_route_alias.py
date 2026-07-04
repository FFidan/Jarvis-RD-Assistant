#!/usr/bin/env python3
"""Temporarily route a LiteLLM alias to a benchmark-only backend.

This helper exists for reproducible performance evidence, not for product
settings. It creates LiteLLM admin-DB deployments with create-first/delete-after
ordering so a failed route change does not silently drop the old alias.
Snapshots are intended for ignored ``artifacts/perf/<run-id>/`` directories.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_NAME = "scripts/perf/litellm_route_alias.py"
SNAPSHOT_VERSION = 1
_SAFE_API_KEY_VALUES = {"", "EMPTY", None}


class RouteAliasError(RuntimeError):
    """Raised when alias routing cannot safely continue."""


@dataclass(frozen=True)
class DeploymentRecord:
    """Serializable subset of one LiteLLM deployment row."""

    model_name: str
    litellm_params: dict[str, Any]
    model_info: dict[str, Any]


@dataclass(frozen=True)
class OpenAICompatibleRoute:
    """Settings for one OpenAI-compatible LiteLLM deployment."""

    served_model: str
    api_base: str
    api_key: str
    timeout: int
    num_retries: int
    temperature: float


def deployment_id(record: DeploymentRecord) -> str | None:
    """Return the LiteLLM deployment id when present."""
    value = record.model_info.get("id")
    return str(value) if value else None


def is_db_deployment(record: DeploymentRecord) -> bool:
    """Return True when the deployment can be deleted by LiteLLM admin API."""
    return bool(record.model_info.get("db_model"))


def alias_records(records: Sequence[DeploymentRecord], alias: str) -> list[DeploymentRecord]:
    """Return deployment records for one alias."""
    return [record for record in records if record.model_name == alias]


def db_deployment_ids(records: Sequence[DeploymentRecord], alias: str) -> list[str]:
    """Return deletable deployment ids for one alias."""
    ids: list[str] = []
    for record in alias_records(records, alias):
        if not is_db_deployment(record):
            continue
        dep_id = deployment_id(record)
        if dep_id is not None:
            ids.append(dep_id)
    return ids


def yaml_alias_count(records: Sequence[DeploymentRecord], alias: str) -> int:
    """Return how many alias rows are YAML-seeded and therefore not deletable."""
    return sum(1 for record in alias_records(records, alias) if not is_db_deployment(record))


def build_openai_compatible_params(route: OpenAICompatibleRoute) -> dict[str, Any]:
    """Build LiteLLM params for a vLLM/OpenAI-compatible endpoint."""
    normalized_base = route.api_base.rstrip("/")
    if not normalized_base.endswith("/v1"):
        raise RouteAliasError("OpenAI-compatible api_base must include the /v1 suffix")
    return {
        "model": f"openai/{route.served_model}",
        "api_base": normalized_base,
        "api_key": route.api_key,
        "timeout": route.timeout,
        "num_retries": route.num_retries,
        "temperature": route.temperature,
    }


def safe_litellm_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return params only when they are safe to persist in benchmark artifacts."""
    api_key = params.get("api_key")
    if api_key not in _SAFE_API_KEY_VALUES and "*" not in str(api_key):
        raise RouteAliasError("Refusing to snapshot an unmasked LiteLLM api_key")
    return dict(params)


def snapshot_payload(alias: str, records: Sequence[DeploymentRecord]) -> dict[str, Any]:
    """Build a portable snapshot for ignored benchmark artifacts."""
    return {
        "schema_version": SNAPSHOT_VERSION,
        "created_by": SCRIPT_NAME,
        "alias": alias,
        "deployments": [
            {
                "model_name": record.model_name,
                "litellm_params": safe_litellm_params(record.litellm_params),
                "model_info": record.model_info,
            }
            for record in alias_records(records, alias)
        ],
    }


def records_from_snapshot(path: Path, alias: str) -> list[DeploymentRecord]:
    """Load DB deployment records for *alias* from a snapshot file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != SNAPSHOT_VERSION:
        raise RouteAliasError(f"Unsupported snapshot schema in {path}")
    if raw.get("alias") != alias:
        raise RouteAliasError(f"Snapshot alias {raw.get('alias')!r} does not match {alias!r}")
    deployments = raw.get("deployments")
    if not isinstance(deployments, list):
        raise RouteAliasError(f"Snapshot {path} has no deployment list")
    records: list[DeploymentRecord] = []
    for item in deployments:
        if not isinstance(item, dict):
            continue
        params = item.get("litellm_params")
        info = item.get("model_info")
        if not isinstance(params, dict) or not isinstance(info, dict):
            continue
        records.append(
            DeploymentRecord(
                model_name=str(item.get("model_name", "")),
                litellm_params=dict(params),
                model_info=dict(info),
            )
        )
    return [record for record in records if is_db_deployment(record)]


def write_snapshot(path: Path, alias: str, records: Sequence[DeploymentRecord]) -> None:
    """Write an ignored benchmark snapshot file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot_payload(alias, records), indent=2, sort_keys=True) + "\n")


async def fetch_deployments() -> list[DeploymentRecord]:
    """Fetch and serialize LiteLLM deployments through product config helpers."""
    from paper_ingestion.services.litellm_api import get_litellm_deployments  # noqa: PLC0415

    records: list[DeploymentRecord] = []
    for deployment in await get_litellm_deployments():
        records.append(
            DeploymentRecord(
                model_name=deployment.model_name,
                litellm_params=dict(deployment.litellm_params),
                model_info=deployment.model_info.model_dump(),
            )
        )
    return records


async def create_deployment(alias: str, params: dict[str, Any]) -> str | None:
    """Create a LiteLLM admin-DB deployment for *alias*."""
    from paper_ingestion.services.litellm_api import _post_model_new  # noqa: PLC0415

    return await _post_model_new(alias, params)


async def delete_deployment(deployment_id_value: str) -> None:
    """Delete a LiteLLM admin-DB deployment by id."""
    from paper_ingestion.services.litellm_api import _post_model_delete  # noqa: PLC0415

    await _post_model_delete(deployment_id_value)


async def route_alias(args: argparse.Namespace) -> None:
    """Route an alias to one OpenAI-compatible benchmark endpoint."""
    records = await fetch_deployments()
    if yaml_alias_count(records, args.alias) and not args.allow_yaml_stack:
        raise RouteAliasError(
            f"Alias {args.alias!r} has YAML deployments; refusing a stacked route"
        )
    if args.snapshot_out is not None:
        write_snapshot(args.snapshot_out, args.alias, records)

    stale_ids = db_deployment_ids(records, args.alias)
    params = build_openai_compatible_params(
        OpenAICompatibleRoute(
            served_model=args.served_model,
            api_base=args.api_base,
            api_key=args.api_key,
            timeout=args.timeout,
            num_retries=args.num_retries,
            temperature=args.temperature,
        )
    )
    new_id = await create_deployment(args.alias, params)
    try:
        for stale_id in stale_ids:
            if stale_id != new_id:
                await delete_deployment(stale_id)
    except Exception:
        if new_id is not None:
            await delete_deployment(new_id)
        raise


def _restore_records(path: Path, alias: str, allow_empty: bool) -> list[DeploymentRecord]:
    records = records_from_snapshot(path, alias)
    if not records and not allow_empty:
        raise RouteAliasError(
            "Snapshot has no DB deployments; pass --allow-empty-restore to delete only"
        )
    return records


async def restore_alias(args: argparse.Namespace) -> None:
    """Restore an alias from a snapshot created before a benchmark route."""
    current = await fetch_deployments()
    stale_ids = db_deployment_ids(current, args.alias)
    restore_records = _restore_records(args.snapshot, args.alias, args.allow_empty_restore)
    created_ids: list[str] = []
    try:
        for record in restore_records:
            new_id = await create_deployment(args.alias, record.litellm_params)
            if new_id is not None:
                created_ids.append(new_id)
        for stale_id in stale_ids:
            if stale_id not in created_ids:
                await delete_deployment(stale_id)
    except Exception:
        for created_id in created_ids:
            await delete_deployment(created_id)
        raise


async def status_alias(args: argparse.Namespace) -> None:
    """Print non-secret deployment status for one alias."""
    records = alias_records(await fetch_deployments(), args.alias)
    print(json.dumps(snapshot_payload(args.alias, records), indent=2, sort_keys=True))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Print alias deployment status")
    status.add_argument("--alias", default="smart")

    route = subparsers.add_parser("route-openai", help="Route alias to OpenAI-compatible API")
    route.add_argument("--alias", default="smart")
    route.add_argument("--served-model", required=True)
    route.add_argument("--api-base", required=True)
    route.add_argument("--api-key", default="EMPTY")
    route.add_argument("--timeout", type=int, default=300)
    route.add_argument("--num-retries", type=int, default=0)
    route.add_argument("--temperature", type=float, default=0.2)
    route.add_argument("--snapshot-out", type=Path)
    route.add_argument("--allow-yaml-stack", action="store_true")

    restore = subparsers.add_parser("restore", help="Restore alias from a snapshot")
    restore.add_argument("--alias", default="smart")
    restore.add_argument("--snapshot", type=Path, required=True)
    restore.add_argument("--allow-empty-restore", action="store_true")
    return parser.parse_args(argv)


async def async_main(argv: Sequence[str] | None = None) -> int:
    """Run the selected subcommand."""
    args = parse_args(argv)
    if args.command == "status":
        await status_alias(args)
    elif args.command == "route-openai":
        await route_alias(args)
    elif args.command == "restore":
        await restore_alias(args)
    else:  # pragma: no cover - argparse prevents this branch
        raise RouteAliasError(f"unknown command: {args.command}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Synchronous CLI entry point."""
    try:
        return asyncio.run(async_main(argv))
    except RouteAliasError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
