"""C5 — discovery_origin stamping contract tests.

For each of the 8 paper-INSERT entry paths, verify that the correct
discovery_origin value is stamped before the row reaches the database.

Strategy by path
----------------
* Direct-INSERT paths (pdf_upload, citation_stub, local_pdf_import):
  Mock ``conn.fetchrow`` / ``conn.execute`` and inspect the SQL string +
  parameters passed into asyncpg.

* Caller-set paths (pulse_job, search_then_save, auto_fetch, batch_save_papers,
  zotero_sync): Mock ``upsert_paper`` and assert the ``discovery_origin``
  attribute on the ``PaperCreate`` object that was passed in.

No live DB is required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from paper_ingestion.models import PaperCreate, SourceType

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Minimal asyncpg.Record substitute."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key, default)


def _txn_cm() -> MagicMock:
    """Return a mock async context manager for conn.transaction()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_conn(**kw) -> AsyncMock:
    """Return a mock asyncpg connection.

    Keyword arguments are forwarded as side_effects/return_values:
      fetchrow:  return value for conn.fetchrow
      fetch:     return value for conn.fetch
      fetchval:  return value for conn.fetchval
      execute:   return value for conn.execute
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=kw.get("fetchrow"))
    conn.fetch = AsyncMock(return_value=kw.get("fetch", []))
    conn.fetchval = AsyncMock(return_value=kw.get("fetchval"))
    conn.execute = AsyncMock(return_value=kw.get("execute"))
    conn.executemany = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=_txn_cm())
    return conn


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Return a pool whose acquire() always yields *conn*."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_pool_multi(*conns: AsyncMock) -> MagicMock:
    """Pool that returns a different conn for each successive acquire() call."""
    pool = MagicMock()
    ctxs = []
    for c in conns:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=c)
        ctx.__aexit__ = AsyncMock(return_value=False)
        ctxs.append(ctx)
    pool.acquire = MagicMock(side_effect=ctxs)
    return pool


def _paper_create(**kw) -> PaperCreate:
    """Return a minimal PaperCreate (discovery_origin defaults to 'user_initiated')."""
    defaults: dict = {
        "external_id": "test:001",
        "source_type": SourceType.ARXIV,
        "title": "Test Paper",
        "authors": ["A. Researcher"],
        "abstract": "Test abstract.",
        "url": "https://example.com/paper",
        "pdf_url": None,
        "citation_count": 0,
        "metadata": {},
    }
    defaults.update(kw)
    return PaperCreate(**defaults)


# ---------------------------------------------------------------------------
# 1. pdf_upload — direct INSERT with literal 'user_initiated'
# ---------------------------------------------------------------------------


async def test_pdf_upload_stamps_user_initiated(tmp_path: Path) -> None:
    """upload_pdf inserts 'user_initiated' as a SQL literal in the INSERT."""
    from paper_ingestion.routers.pdf import upload_pdf

    # Build a minimal fake PDF file
    pdf_content = b"%PDF-1.4 fake content for testing"
    fake_file = MagicMock()
    fake_file.filename = "test.pdf"
    # file.read() returns content on first call, then b"" to signal EOF
    fake_file.read = AsyncMock(side_effect=[pdf_content, b""])

    # The INSERT row that fetchrow returns
    fake_row = FakeRecord(
        {
            "id": 1,
            "external_id": "local:abc",
            "source_type": "local",
            "title": "My Paper",
            "authors": [],
            "abstract": None,
            "url": "local://abc",
            "pdf_url": None,
            "published_date": None,
            "citation_count": 0,
            "metadata": {},
            "pdf_downloaded": True,
            "pdf_local_path": str(tmp_path / "1.pdf"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "discovery_origin": "user_initiated",
            "zotero_item_key": None,
            "zotero_citation_key": None,
            "zotero_last_pushed_at": None,
            "citations_fetched_at": None,
            "user_id": None,
        }
    )

    captured_sql: list[str] = []
    captured_params: list[tuple] = []

    async def _fake_fetchrow(sql: str, *params):
        captured_sql.append(sql)
        captured_params.append(params)
        return fake_row

    conn = _make_conn()
    conn.fetchrow = AsyncMock(side_effect=_fake_fetchrow)
    # First fetchrow call is the duplicate-check (returns None); subsequent are the INSERT.
    no_existing = FakeRecord.__new__(FakeRecord)
    FakeRecord.__init__(no_existing)  # empty dict

    call_count = 0

    async def _fetchrow_dispatch(sql: str, *params):
        nonlocal call_count
        call_count += 1
        captured_sql.append(sql)
        captured_params.append(params)
        if "WHERE external_id" in sql:
            return None  # no duplicate
        return fake_row  # INSERT RETURNING

    conn.fetchrow = AsyncMock(side_effect=_fetchrow_dispatch)

    pool = _make_pool(conn)

    # Patch PDF_STORAGE_PATH to use tmp_path so no real I/O is needed
    with (
        patch(
            "paper_ingestion.routers.pdf.PDF_STORAGE_PATH",
            str(tmp_path),
        ),
        patch(
            "paper_ingestion.routers.pdf.get_db_pool",
            return_value=pool,
        ),
    ):
        # Call the handler directly (bypassing @limiter.limit via __wrapped__)
        handler = getattr(upload_pdf, "__wrapped__", upload_pdf)
        await handler(
            request=MagicMock(),
            file=fake_file,
            title="My Paper",
            authors="",
            abstract="",
            db_pool=pool,
        )

    # Find the INSERT SQL call
    insert_sqls = [s for s in captured_sql if "INSERT INTO papers" in s]
    assert insert_sqls, "No INSERT INTO papers SQL was captured"
    insert_sql = insert_sqls[0]
    assert "'user_initiated'" in insert_sql, (
        f"Expected 'user_initiated' literal in SQL, got:\n{insert_sql}"
    )


# ---------------------------------------------------------------------------
# 2. citation_stub — get_or_create_stub_paper inserts 'citation_batch'
# ---------------------------------------------------------------------------


async def test_citation_stub_stamps_citation_batch() -> None:
    """get_or_create_stub_paper inserts 'citation_batch' literal in the SQL."""
    from paper_ingestion.citations import get_or_create_stub_paper

    s2_data = {
        "citedPaper": {
            "paperId": "abc123",
            "title": "Some External Paper",
            "authors": [{"name": "Jane Doe"}],
            "year": 2023,
            "citationCount": 5,
            "externalIds": {},
        }
    }

    captured_sql: list[str] = []
    captured_params: list[tuple] = []
    fake_row = FakeRecord({"id": 42})

    async def _fetchrow_dispatch(sql: str, *params):
        captured_sql.append(sql)
        captured_params.append(params)
        if "WHERE external_id" in sql:
            return None  # paper does not exist yet → force INSERT branch
        return fake_row  # INSERT RETURNING id

    conn = _make_conn()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow_dispatch)

    result = await get_or_create_stub_paper(conn, s2_data)

    assert result == 42, f"Expected paper id 42, got {result}"

    insert_sqls = [s for s in captured_sql if "INSERT INTO papers" in s]
    assert insert_sqls, "No INSERT INTO papers SQL was captured"
    insert_sql = insert_sqls[0]
    assert "'citation_batch'" in insert_sql, (
        f"Expected 'citation_batch' literal in SQL, got:\n{insert_sql}"
    )


# ---------------------------------------------------------------------------
# 3. local_pdf_import — scan_local_pdf_directory inserts 'user_initiated'
# ---------------------------------------------------------------------------


async def test_local_pdf_import_stamps_user_initiated(tmp_path: Path) -> None:
    """scan_local_pdf_directory inserts 'user_initiated' literal in the SQL."""
    from paper_ingestion.services.local_pdfs import scan_local_pdf_directory

    # Write a minimal valid PDF to the scan dir
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    pdf = scan_dir / "my_paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal content")

    captured_sql: list[str] = []
    fake_row = FakeRecord({"id": 7})

    async def _fetchrow_dispatch(sql: str, *params):
        captured_sql.append(sql)
        if "WHERE external_id" in sql:
            return None  # not a duplicate
        return fake_row  # INSERT RETURNING

    conn = _make_conn()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow_dispatch)
    conn.execute = AsyncMock(return_value=None)

    pool = _make_pool(conn)

    with (
        patch(
            "paper_ingestion.services.local_pdfs.PDF_STORAGE_PATH",
            str(tmp_path / "storage"),
        ),
    ):
        result = await scan_local_pdf_directory(pool, scan_dir=str(scan_dir))

    assert result["imported"] == 1, f"Expected 1 imported paper, got {result}"

    insert_sqls = [s for s in captured_sql if "INSERT INTO papers" in s]
    assert insert_sqls, "No INSERT INTO papers SQL was captured"
    insert_sql = insert_sqls[0]
    assert "'user_initiated'" in insert_sql, (
        f"Expected 'user_initiated' literal in SQL, got:\n{insert_sql}"
    )


# ---------------------------------------------------------------------------
# 4. pulse_job — run_pulse sets card.paper.discovery_origin = "pulse"
# ---------------------------------------------------------------------------


async def test_pulse_job_stamps_pulse() -> None:
    """run_pulse sets discovery_origin='pulse' on each card.paper before upsert."""
    from paper_ingestion.pulse.job import run_pulse
    from paper_ingestion.pulse.scoring import ScoredCandidate

    captured_papers: list[PaperCreate] = []

    async def _fake_upsert(conn, paper: PaperCreate):
        captured_papers.append(paper)
        return FakeRecord(
            {
                "id": 1,
                "external_id": paper.external_id,
                "source_type": paper.source_type.value,
                "title": paper.title,
                "authors": paper.authors,
                "abstract": paper.abstract,
                "url": paper.url,
                "pdf_url": None,
                "published_date": None,
                "citation_count": 0,
                "metadata": {},
                "pdf_downloaded": False,
                "pdf_local_path": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "discovery_origin": "pulse",
                "zotero_item_key": None,
                "zotero_citation_key": None,
                "zotero_last_pushed_at": None,
                "citations_fetched_at": None,
                "user_id": None,
                "is_insert": True,
            }
        )

    paper = _paper_create(external_id="arxiv:pulse01", source_type=SourceType.ARXIV)
    card = ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.9},
        llm_relevance=8,
        llm_novelty=7,
        reasoning="Highly relevant",
        final_score=0.85,
    )

    conn = _make_conn()
    pool = _make_pool(conn)

    mock_profile = MagicMock()
    mock_profile.topics = [MagicMock()]
    mock_profile.deck_size = 5
    mock_profile.stage2_top_k = 10
    mock_profile.weights = {}

    with (
        patch(
            "paper_ingestion.pulse.job.load_profile",
            AsyncMock(return_value=mock_profile),
        ),
        patch(
            "paper_ingestion.pulse.job.discover_candidates",
            AsyncMock(return_value=([], {})),
        ),
        patch(
            "paper_ingestion.pulse.job.stage1_embedding_filter",
            AsyncMock(return_value=[card]),
        ),
        patch(
            "paper_ingestion.pulse.job.stage2_llm_rerank",
            AsyncMock(return_value=[card]),
        ),
        patch(
            "paper_ingestion.pulse.job.stage3_combine",
            AsyncMock(return_value=[card]),
        ),
        patch(
            "paper_ingestion.pulse.job.assemble_deck",
            return_value=[card],
        ),
        patch(
            "paper_ingestion.pulse.job.persist_deck",
            AsyncMock(return_value=1),
        ),
        patch(
            "paper_ingestion.pulse.job.compute_citation_signals",
            AsyncMock(return_value={}),
        ),
        patch(
            "paper_ingestion.pulse.job.classifier_scores",
            AsyncMock(return_value=([0.0], {"available": False, "feature_names": []})),
        ),
        patch(
            "paper_ingestion.pulse.job.upsert_paper",
            AsyncMock(side_effect=_fake_upsert),
        ),
    ):
        await run_pulse(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
            now=datetime(2024, 1, 15, 4, 0, 0, tzinfo=UTC),
        )

    assert captured_papers, "upsert_paper was never called during run_pulse"
    for p in captured_papers:
        assert p.discovery_origin == "pulse", (
            f"Expected discovery_origin='pulse', got '{p.discovery_origin}'"
        )


# ---------------------------------------------------------------------------
# 5. search_then_save — search_papers sets paper.discovery_origin = "user_initiated"
# ---------------------------------------------------------------------------


async def test_search_then_save_stamps_user_initiated() -> None:
    """search_papers sets discovery_origin='user_initiated' on each paper before upsert."""
    from paper_ingestion.models import SearchRequest
    from paper_ingestion.routers.search import search_papers

    captured_papers: list[PaperCreate] = []
    fake_paper = _paper_create(
        external_id="s2:search01",
        source_type=SourceType.SEMANTIC_SCHOLAR,
        url="https://www.semanticscholar.org/paper/1",
    )

    async def _fake_upsert(conn, paper: PaperCreate):
        captured_papers.append(paper)
        return FakeRecord(
            {
                "id": 2,
                "external_id": paper.external_id,
                "source_type": paper.source_type.value,
                "title": paper.title,
                "authors": paper.authors,
                "abstract": paper.abstract,
                "url": paper.url,
                "pdf_url": None,
                "published_date": None,
                "citation_count": 0,
                "metadata": {},
                "pdf_downloaded": False,
                "pdf_local_path": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "discovery_origin": "user_initiated",
                "zotero_item_key": None,
                "zotero_citation_key": None,
                "zotero_last_pushed_at": None,
                "citations_fetched_at": None,
                "user_id": None,
                "is_insert": True,
            }
        )

    conn = _make_conn()
    pool = _make_pool(conn)

    mock_source = MagicMock()
    mock_source.search = AsyncMock(return_value=[fake_paper])

    body = SearchRequest(
        query="neural networks",
        source_types=[SourceType.SEMANTIC_SCHOLAR],
        max_results=5,
    )

    handler = getattr(search_papers, "__wrapped__", search_papers)

    with (
        patch(
            "paper_ingestion.routers.search.get_sources_for_types",
            AsyncMock(return_value=({SourceType.SEMANTIC_SCHOLAR: mock_source}, {})),
        ),
        patch(
            "paper_ingestion.routers.search.upsert_paper",
            AsyncMock(side_effect=_fake_upsert),
        ),
    ):
        await handler(
            request=MagicMock(),
            body=body,
            db_pool=pool,
            http_client=MagicMock(),
        )

    assert captured_papers, "upsert_paper was never called during search_papers"
    for p in captured_papers:
        assert p.discovery_origin == "user_initiated", (
            f"Expected discovery_origin='user_initiated', got '{p.discovery_origin}'"
        )


# ---------------------------------------------------------------------------
# 6. auto_fetch — run_auto_pipeline sets paper.discovery_origin = "recommender"
# ---------------------------------------------------------------------------


async def test_auto_fetch_stamps_recommender() -> None:
    """run_auto_pipeline sets discovery_origin='recommender' on each paper before upsert."""
    from paper_ingestion.pipelines.auto_fetch import run_auto_pipeline

    captured_papers: list[PaperCreate] = []
    fake_paper = _paper_create(
        external_id="arxiv:auto01",
        source_type=SourceType.ARXIV,
    )

    async def _fake_upsert(conn, paper: PaperCreate):
        captured_papers.append(paper)
        return FakeRecord(
            {
                "id": 3,
                "is_insert": True,
                "external_id": paper.external_id,
                "source_type": paper.source_type.value,
                "title": paper.title,
                "authors": paper.authors,
                "abstract": paper.abstract,
                "url": paper.url,
                "pdf_url": None,
                "published_date": None,
                "citation_count": 0,
                "metadata": {},
                "pdf_downloaded": False,
                "pdf_local_path": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "discovery_origin": "recommender",
                "zotero_item_key": None,
                "zotero_citation_key": None,
                "zotero_last_pushed_at": None,
                "citations_fetched_at": None,
                "user_id": None,
            }
        )

    # DB: sources + topics → 1 row each (two successive fetch() calls on same conn);
    # then to_download=[], to_process=[]
    source_row = FakeRecord(
        {
            "id": 1,
            "source_type": "arxiv",
            "enabled": True,
            "display_order": 1,
            "config": {},
        }
    )
    topic_row = FakeRecord({"name": "machine learning"})

    # conn1 handles both fetch() calls: sources first, then topics
    conn1 = _make_conn()
    conn1.fetch = AsyncMock(side_effect=[[source_row], [topic_row]])

    # conn_for_upsert: the acquire() inside the `for paper in results` loop
    conn_for_upsert = _make_conn()

    conn2 = _make_conn(fetch=[])  # to_download
    conn3 = _make_conn(fetch=[])  # to_process

    acquire_count = 0
    conns = [conn1, conn_for_upsert, conn2, conn3]

    pool = MagicMock()

    def _acquire():
        nonlocal acquire_count
        idx = min(acquire_count, len(conns) - 1)
        acquire_count += 1
        c = conns[idx]
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=c)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    pool.acquire = MagicMock(side_effect=lambda: _acquire())

    mock_source_instance = MagicMock()
    mock_source_instance.search = AsyncMock(return_value=[fake_paper])
    mock_source_class = MagicMock(return_value=mock_source_instance)

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            pdf_processor=MagicMock(),
            embedder=MagicMock(),
        )
    )

    with (
        patch.dict("os.environ", {"AUTO_FETCH_INTERVAL_HOURS": "1"}),
        patch(
            "paper_ingestion.pipelines.auto_fetch.get_source_class",
            return_value=mock_source_class,
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.upsert_paper",
            AsyncMock(side_effect=_fake_upsert),
        ),
    ):
        await run_auto_pipeline(app)

    assert captured_papers, "upsert_paper was never called during run_auto_pipeline"
    for p in captured_papers:
        assert p.discovery_origin == "recommender", (
            f"Expected discovery_origin='recommender', got '{p.discovery_origin}'"
        )


# ---------------------------------------------------------------------------
# 7. batch_save_papers — sets paper.discovery_origin = "citation_batch"
# ---------------------------------------------------------------------------


async def test_batch_save_papers_stamps_citation_batch() -> None:
    """batch_save_papers overrides discovery_origin to 'citation_batch' for each paper."""
    from paper_ingestion.routers.papers import batch_save_papers

    captured_papers: list[PaperCreate] = []

    async def _fake_upsert(conn, paper: PaperCreate):
        captured_papers.append(paper)
        return FakeRecord(
            {
                "id": 4,
                "external_id": paper.external_id,
                "source_type": paper.source_type.value,
                "title": paper.title,
                "authors": paper.authors,
                "abstract": paper.abstract,
                "url": paper.url,
                "pdf_url": None,
                "published_date": None,
                "citation_count": 0,
                "metadata": {},
                "pdf_downloaded": False,
                "pdf_local_path": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "discovery_origin": "citation_batch",
                "zotero_item_key": None,
                "zotero_citation_key": None,
                "zotero_last_pushed_at": None,
                "citations_fetched_at": None,
                "user_id": None,
                "is_insert": True,
            }
        )

    # Paper sent with the DEFAULT 'user_initiated' discovery_origin —
    # the router MUST override it to 'citation_batch'.
    incoming = _paper_create(
        external_id="s2:batch01",
        source_type=SourceType.SEMANTIC_SCHOLAR,
        url="https://www.semanticscholar.org/paper/batch01",
    )
    assert incoming.discovery_origin == "user_initiated"  # pre-condition

    conn = _make_conn()
    pool = _make_pool(conn)

    handler = getattr(batch_save_papers, "__wrapped__", batch_save_papers)

    with patch(
        "paper_ingestion.routers.papers.upsert_paper",
        AsyncMock(side_effect=_fake_upsert),
    ):
        await handler(
            request=MagicMock(),
            papers=[incoming],
            db_pool=pool,
        )

    assert captured_papers, "upsert_paper was never called during batch_save_papers"
    for p in captured_papers:
        assert p.discovery_origin == "citation_batch", (
            f"Expected discovery_origin='citation_batch', got '{p.discovery_origin}'"
        )


# ---------------------------------------------------------------------------
# 8. zotero_sync — poll_zotero_library constructs PaperCreate with 'user_initiated'
# ---------------------------------------------------------------------------


async def test_zotero_sync_stamps_user_initiated() -> None:
    """poll_zotero_library passes discovery_origin='user_initiated' in PaperCreate."""
    from paper_ingestion.integrations.zotero_service import poll_zotero_library

    captured_papers: list[PaperCreate] = []

    async def _fake_upsert(conn, paper: PaperCreate):
        captured_papers.append(paper)
        return FakeRecord(
            {
                "id": 5,
                "external_id": paper.external_id,
                "source_type": paper.source_type.value,
                "title": paper.title,
                "authors": paper.authors,
                "abstract": paper.abstract,
                "url": paper.url,
                "pdf_url": None,
                "published_date": None,
                "citation_count": 0,
                "metadata": {},
                "pdf_downloaded": False,
                "pdf_local_path": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "discovery_origin": "user_initiated",
                "zotero_item_key": None,
                "zotero_citation_key": None,
                "zotero_last_pushed_at": None,
                "citations_fetched_at": None,
                "user_id": None,
                "is_insert": True,
            }
        )

    # Config rows for an enabled Zotero poll
    cfg_rows = [
        FakeRecord({"key": "zotero.enabled", "value": True, "encrypted_value": None}),
        FakeRecord({"key": "zotero.poll_enabled", "value": True, "encrypted_value": None}),
        FakeRecord({"key": "zotero.api_key", "value": "test_key", "encrypted_value": None}),
        FakeRecord({"key": "zotero.user_id", "value": "99999", "encrypted_value": None}),
        FakeRecord({"key": "zotero.library_type", "value": "user", "encrypted_value": None}),
    ]

    # One Zotero item that does NOT originate from JARVIS (no 'jarvis_paper_id=' in extra)
    zotero_item = {
        "key": "ZTST0001",
        "data": {
            "key": "ZTST0001",
            "title": "Test Zotero Paper",
            "abstractNote": "Abstract here.",
            "url": "https://example.com/zotero-paper",
            "DOI": "",
            "creators": [{"firstName": "Jane", "lastName": "Doe"}],
            "extra": "",
        },
    }

    # Pool for config read
    conn_cfg = _make_conn(fetch=cfg_rows)
    # Pool for paper upsert + user state insert + zotero_item_key update
    conn_paper = _make_conn()
    conn_paper.execute = AsyncMock(return_value=None)
    # Pool for persisting last_library_version
    conn_ver = _make_conn()

    acquire_idx = 0
    acquire_seq = [conn_cfg, conn_paper, conn_ver]

    pool = MagicMock()

    def _acquire():
        nonlocal acquire_idx
        idx = min(acquire_idx, len(acquire_seq) - 1)
        acquire_idx += 1
        c = acquire_seq[idx]
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=c)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    pool.acquire = MagicMock(side_effect=lambda: _acquire())

    # Mock the ZoteroClient so no real HTTP is attempted
    mock_client = AsyncMock()
    mock_client.fetch_items_since = AsyncMock(return_value=([zotero_item], 42))

    # Mock jobs_lib.enqueue so no DB calls for job enqueueing are made
    with (
        patch(
            "paper_ingestion.integrations.zotero_service.upsert_paper",
            AsyncMock(side_effect=_fake_upsert),
        ),
        patch(
            "paper_ingestion.integrations.zotero_service.jobs_lib.enqueue",
            AsyncMock(return_value="job-123"),
        ),
        patch(
            "paper_ingestion.integrations.zotero_client.ZoteroClient",
            return_value=mock_client,
        ),
    ):
        await poll_zotero_library(pool, MagicMock())

    assert captured_papers, "upsert_paper was never called during poll_zotero_library"
    for p in captured_papers:
        assert p.discovery_origin == "user_initiated", (
            f"Expected discovery_origin='user_initiated', got '{p.discovery_origin}'"
        )
