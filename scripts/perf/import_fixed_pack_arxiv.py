#!/usr/bin/env python3
"""Import the fixed scientific RAG paper pack by exact arXiv identifier.

Run this helper inside the ``paper_ingestion`` service environment. It uses the
same arXiv source parser, canonical paper upsert, and per-user library helper as
the product service, while avoiding broad discovery routes that can add
citation-neighbor papers to the benchmark user's library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from jarvis_common.app_factory import build_database_url
from jarvis_common.config import get_jarvis_common_settings
from jarvis_common.db_helpers import init_pg_connection
from jarvis_common.library import add_to_library
from paper_ingestion.models import PaperCreate, PaperSourceConfig, SourceType
from paper_ingestion.services.pdf_workflow import upsert_paper
from paper_ingestion.sources.arxiv_source import ArxivSource

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from seed_scientific_rag_pack import DEFAULT_MANIFEST, FixedPaper, SeedPackError, load_fixed_pack


@dataclass(frozen=True)
class ImportedPaper:
    """Summary row for one exact fixed-pack import."""

    paper_key: str
    identifier: str
    title: str
    local_paper_id: int
    inserted: bool


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse exact-import operator arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--pool-min", type=int, default=1)
    parser.add_argument("--pool-max", type=int, default=2)
    return parser.parse_args(argv)


async def import_fixed_pack(
    *,
    fixed_pack: list[FixedPaper],
    user_id: int,
    out_dir: Path,
    pool_min: int,
    pool_max: int,
) -> list[ImportedPaper]:
    """Import each fixed paper by exact arXiv ID and attach it to one library.

    Parameters
    ----------
    fixed_pack
        Manifest paper rows in benchmark order.
    user_id
        Existing product user whose library will own the benchmark pack.
    out_dir
        Ignored artifact directory where the import summary is written.
    pool_min
        Minimum asyncpg pool size.
    pool_max
        Maximum asyncpg pool size.

    Returns
    -------
    list[ImportedPaper]
        Imported canonical paper ids and insertion flags.
    """
    settings = get_jarvis_common_settings()
    pool = await asyncpg.create_pool(
        build_database_url(
            user=settings.postgres_user,
            password_file=settings.postgres_password_file,
        ),
        min_size=pool_min,
        max_size=pool_max,
        init=init_pg_connection,
    )
    if pool is None:
        raise SeedPackError("asyncpg.create_pool returned None")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as http_client:
            source = ArxivSource(
                PaperSourceConfig(id=0, source_type=SourceType.ARXIV, enabled=True, config={}),
                http_client,
                db_pool=pool,
            )
            rows: list[ImportedPaper] = []
            for paper in fixed_pack:
                fetched = await source.fetch_by_id(_canonical_arxiv_id(paper.identifier))
                if fetched is None:
                    raise SeedPackError(
                        f"arXiv returned no entry for {paper.paper_key}: {paper.identifier}"
                    )
                _validate_exact_paper(expected=paper, fetched=fetched)
                async with pool.acquire() as conn, conn.transaction():
                    row = await upsert_paper(conn, fetched, discovered_by=user_id)
                    paper_id = int(row["id"])
                    await add_to_library(
                        conn,
                        user_id=user_id,
                        paper_id=paper_id,
                        added_via="manual_save",
                    )
                    rows.append(
                        ImportedPaper(
                            paper_key=paper.paper_key,
                            identifier=paper.identifier,
                            title=fetched.title,
                            local_paper_id=paper_id,
                            inserted=bool(row["is_insert"]),
                        )
                    )
            _write_summary(out_dir, rows)
            return rows
    finally:
        await pool.close()


def _validate_exact_paper(*, expected: FixedPaper, fetched: PaperCreate) -> None:
    """Raise when arXiv did not return the manifest identifier."""
    expected_id = _canonical_arxiv_id(expected.identifier)
    fetched_id = _canonical_arxiv_id(fetched.external_id)
    if fetched_id != expected_id:
        raise SeedPackError(
            "arXiv identifier mismatch for "
            f"{expected.paper_key}: expected {expected_id}, got {fetched_id}"
        )


def _canonical_arxiv_id(value: str) -> str:
    """Normalize an arXiv identifier for exact equality checks."""
    return value.casefold().removeprefix("arxiv:").strip()


def _write_summary(out_dir: Path, rows: list[ImportedPaper]) -> None:
    """Write exact-import summary artifacts without secret material."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "imported_count": len(rows),
        "papers": [asdict(row) for row in rows],
    }
    (out_dir / "exact_arxiv_import.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for exact fixed-pack import."""
    args = parse_args(argv)
    fixed_pack = load_fixed_pack(args.manifest)
    rows = asyncio.run(
        import_fixed_pack(
            fixed_pack=fixed_pack,
            user_id=args.user_id,
            out_dir=args.out_dir,
            pool_min=args.pool_min,
            pool_max=args.pool_max,
        )
    )
    json.dump(
        {"imported_count": len(rows), "papers": [asdict(row) for row in rows]},
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
