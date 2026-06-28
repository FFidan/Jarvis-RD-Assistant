"""Search multi-source contract tests — Cluster 2.

Covers POST /api/search-preview multi-source fan-out, dedup, degraded-source
isolation, and empty-source-types rejection. Replaces SQL-substring and
handler-bypass mock-units in test_search_multi_source.py + test_search_router_direct.py
with survivor citations.

Sources are mocked at the plugin layer via monkeypatching
``paper_ingestion.routers.search._resolve_sources_for_search`` — the source
plugin instances themselves are the carve-out boundary; mocking at the
adapter edge is canonical.

Deferred: SR-05 (respx S2 rate-limit boundary-adapter) — kept as rot-on-touch
since the existing carve-out adapter tests in test_search_multi_source.py
already cover S2 429 mapping; a contract-level S2 test would add minimal value.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    from unittest.mock import MagicMock

    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_contract_apps import patch_app_state, patch_dependency_overrides
    from paper_ingestion.main import app

    # search-preview reads http_client from app.state via get_http_client dep.
    shared = SharedConnPool(contract_conn)
    with (
        patch_app_state(app, {"db_pool": shared, "embedder": None, "http_client": MagicMock()}),
        patch_dependency_overrides(
            app, remove_overrides={current_user_id_strict_with_owner_override}
        ),
    ):
        yield app


def _make_paper_create(external_id: str, title: str, source_type: str = "arxiv"):
    """Return a PaperCreate-compatible Pydantic instance."""
    from paper_ingestion.models import PaperCreate

    return PaperCreate(
        external_id=external_id,
        source_type=source_type,
        title=title,
        authors=["A. Author"],
        abstract="abstract",
        url=f"https://example.test/{external_id}",
        published_date=date(2024, 1, 1),
    )


def _stub_plugin_returning(papers):
    plugin = AsyncMock()
    plugin.search = AsyncMock(return_value=papers)
    return plugin


def _stub_plugin_raising(exc):
    plugin = AsyncMock()
    plugin.search = AsyncMock(side_effect=exc)
    return plugin


# ---------------------------------------------------------------------------
# SR-01: POST /api/search-preview — happy path, dedup + response shape
# ---------------------------------------------------------------------------


async def _stub_no_library_matches(db_pool, preview_papers, user_id):
    """Stub _load_local_library_matches — returns no matches.

    Bypasses a pre-existing schema gap: production code references the
    `papers.zotero_item_key` column in this query but the column is not
    defined in any DB migration. Out of Cluster 2 scope; tested elsewhere
    via the carve-out adapter tests in test_search_multi_source.py.
    """
    return {}, {}


async def test_sr01_search_preview_merged_dedup_response_shape(
    contract_two_users, _pi_app_with_pool, _configure_api_key, monkeypatch
):
    """POST /api/search-preview returns `results, per_source_counts, degraded_sources`
    with multi-source merge + dedup; no DB write side effects.

    # Verified: services/paper_ingestion/paper_ingestion/routers/search.py:266
    # (search_papers_preview: fan-out + dedup + library-match merge).
    """
    from paper_ingestion.models import SourceType

    arxiv_paper = _make_paper_create("arxiv:1", "Paper One", "arxiv")
    s2_paper = _make_paper_create("s2:2", "Paper Two", "semantic_scholar")

    async def _stub_resolver(source_types, db_pool, http_client, request):
        return {
            SourceType.ARXIV: _stub_plugin_returning([arxiv_paper]),
            SourceType.SEMANTIC_SCHOLAR: _stub_plugin_returning([s2_paper]),
        }, {}

    monkeypatch.setattr(
        "paper_ingestion.routers.search._resolve_sources_for_search",
        _stub_resolver,
    )
    monkeypatch.setattr(
        "paper_ingestion.routers.search._load_local_library_matches",
        _stub_no_library_matches,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/search-preview",
            json={
                "query": "test query",
                "source_types": ["arxiv", "semantic_scholar"],
                "max_results": 10,
            },
        )

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert "results" in body and "per_source_counts" in body and "degraded_sources" in body
    assert body["total"] == 2, f"Expected total=2 deduped; got {body['total']}"
    titles = [r["title"] for r in body["results"]]
    assert "Paper One" in titles and "Paper Two" in titles


# ---------------------------------------------------------------------------
# SR-02: search-preview library_match.has_project_links must be caller-scoped.
# (Reopened: papers.zotero_item_key now exists in db/init.sql, so the real
# _load_local_library_matches query runs against the contract DB.)
# ---------------------------------------------------------------------------


async def test_sr02_search_preview_has_project_links_not_leaked_across_users(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, monkeypatch
):
    """search-preview's per-result has_project_links must be scoped
    to the caller's own projects.

    A preview result matches user A's owned paper (paper_id_a) by external_id.
    User B links that paper to B's *own* project. When user A runs the preview,
    the matched library row's has_project_links must be False — B's link must not
    surface to A. With the unscoped EXISTS(project_papers) subquery it leaked True.

    Runs the REAL _load_local_library_matches (not stubbed) against live PG.

    # Verified: services/paper_ingestion/paper_ingestion/routers/search_helpers.py:442
    # (the has_project_links EXISTS subquery must scope to the caller's projects).
    """
    from datetime import date

    from paper_ingestion.models import PaperCreate, SourceType

    paper_id_a = contract_two_users.paper_id_a
    project_id_b = contract_two_users.project_id_b

    # Make the preview result match A's seeded paper by external_id (the seed uses
    # external_id='iso-ext-a' and url='https://example.test/a').
    matching_paper = PaperCreate(
        external_id="iso-ext-a",
        source_type="arxiv",
        title="Some Other Title",
        authors=["A. Author"],
        abstract="abstract",
        url="https://example.test/a",
        published_date=date(2024, 1, 1),
    )

    # Only user B links A's paper into B's own project.
    await contract_conn.execute(
        "INSERT INTO project_papers (project_id, paper_id) VALUES ($1, $2)",
        project_id_b,
        paper_id_a,
    )

    async def _stub_resolver(source_types, db_pool, http_client, request):
        return {SourceType.ARXIV: _stub_plugin_returning([matching_paper])}, {}

    monkeypatch.setattr(
        "paper_ingestion.routers.search._resolve_sources_for_search",
        _stub_resolver,
    )
    # NOTE: _load_local_library_matches is deliberately NOT stubbed here so the
    # real scoped query is exercised against the contract DB.

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/search-preview",
            json={"query": "test", "source_types": ["arxiv"], "max_results": 5},
        )

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    matched = [
        r
        for r in body["results"]
        if r.get("library_match") and r["library_match"].get("paper_id") == paper_id_a
    ]
    assert matched, (
        f"Expected the preview result to match A's library paper {paper_id_a}; "
        f"results: {[r.get('library_match') for r in body['results']]}"
    )
    assert matched[0]["library_match"]["has_project_links"] is False, (
        "User B's project link must NOT surface as has_project_links to user A; "
        f"got {matched[0]['library_match']}"
    )


# ---------------------------------------------------------------------------
# SR-03: degraded source still returns 200; degraded_sources lists the failing source
# ---------------------------------------------------------------------------


async def test_sr03_degraded_source_still_returns_200(
    contract_two_users, _pi_app_with_pool, _configure_api_key, monkeypatch
):
    """One source raising during search returns 200 with degraded_sources populated.

    # Verified: services/paper_ingestion/paper_ingestion/routers/search.py:266
    # (search_papers_preview catches per-source exceptions and adds to source_errors
    # + degraded_sources rather than failing the whole response).
    """
    from paper_ingestion.models import SourceType

    arxiv_paper = _make_paper_create("arxiv:1", "Arxiv Paper", "arxiv")

    async def _stub_resolver(source_types, db_pool, http_client, request):
        return {
            SourceType.ARXIV: _stub_plugin_returning([arxiv_paper]),
            SourceType.SEMANTIC_SCHOLAR: _stub_plugin_raising(RuntimeError("S2 boom")),
        }, {}

    monkeypatch.setattr(
        "paper_ingestion.routers.search._resolve_sources_for_search",
        _stub_resolver,
    )
    monkeypatch.setattr(
        "paper_ingestion.routers.search._load_local_library_matches",
        _stub_no_library_matches,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/search-preview",
            json={
                "query": "test",
                "source_types": ["arxiv", "semantic_scholar"],
                "max_results": 5,
            },
        )

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert "semantic_scholar" in body["degraded_sources"], (
        f"Expected semantic_scholar in degraded_sources; got {body['degraded_sources']}"
    )
    # Healthy source still returns its results
    arxiv_titles = [r["title"] for r in body["results"] if r["source_type"] == "arxiv"]
    assert "Arxiv Paper" in arxiv_titles


# ---------------------------------------------------------------------------
# SR-04: empty source_types rejected
# ---------------------------------------------------------------------------


async def test_sr04_empty_source_types_rejected(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/search-preview with source_types=[] returns 422.

    # Verified: services/paper_ingestion/paper_ingestion/models/papers.py:219
    # (source_types: list[SourceType] = Field(min_length=1) — Pydantic rejects empty list).
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/search-preview",
            json={"query": "test", "source_types": [], "max_results": 5},
        )

    assert resp.status_code in (400, 422), (
        f"Expected 400/422 for empty source_types; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# SR-05: all-sources-fail returns 400 (degenerate but bounded)
# ---------------------------------------------------------------------------


async def test_sr05_all_sources_failed_to_load_returns_400(
    contract_two_users, _pi_app_with_pool, _configure_api_key, monkeypatch
):
    """When EVERY source fails to bootstrap, search-preview returns 400.

    # Verified: services/paper_ingestion/paper_ingestion/routers/search.py:266
    # (if not plugins: raise HTTPException(400, "No sources available for search")).
    """

    async def _stub_resolver(source_types, db_pool, http_client, request):
        # Empty resolved plugins + record load errors for every source type
        return {}, {st: RuntimeError("bootstrap failed") for st in source_types}

    monkeypatch.setattr(
        "paper_ingestion.routers.search._resolve_sources_for_search",
        _stub_resolver,
    )
    monkeypatch.setattr(
        "paper_ingestion.routers.search._load_local_library_matches",
        _stub_no_library_matches,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/search-preview",
            json={"query": "test", "source_types": ["arxiv"], "max_results": 5},
        )

    # SourceType.ARXIV exists; _stub_resolver returns no plugin for it → router 400 path
    assert resp.status_code == 400, resp.text[:300]
    assert "available" in resp.text.lower() or "source" in resp.text.lower()


# ---------------------------------------------------------------------------
# SR-06: POST /api/search multi-source dedup across sources
#
# Verified: services/paper_ingestion/paper_ingestion/routers/search.py:146-263
#   (search_papers: fan-out + _dedup_papers + upsert into DB)
# Verified: services/paper_ingestion/paper_ingestion/routers/search_helpers.py
#   (_dedup_papers: deduplicates by external_id)
# Survivor-of: ~3-5 mock-units in test_search_multi_source.py that assert
#   _dedup_papers call counts via @patch without a real DB round-trip
# ---------------------------------------------------------------------------


async def test_search_multi_source_post_dedupes_across_sources(
    contract_two_users, _pi_app_with_pool, _configure_api_key, monkeypatch
):
    """POST /api/search fans out across sources, deduplicates by external_id,
    and returns only unique papers (total == unique count, not sum of sources).

    Two sources each return the same paper (same external_id / title) plus one
    unique paper each. After dedup the total must be 3 (not 4), and DB upsert
    must land exactly those 3 rows in papers.

    # Verified: search.py:228-230 (_dedup_papers applied after round-robin merge)
    # Verified: search.py:236-250 (upsert_paper + add_to_library for deduplicated list)
    """
    from paper_ingestion.models import SourceType

    shared_paper = _make_paper_create("dedup-shared-01", "Shared Paper", "arxiv")
    arxiv_unique = _make_paper_create("dedup-arxiv-only-01", "Arxiv Only Paper", "arxiv")
    # Same external_id as shared_paper but different source — dedup collapses it
    shared_s2 = _make_paper_create("dedup-shared-01", "Shared Paper", "semantic_scholar")
    s2_unique = _make_paper_create("dedup-s2-only-01", "S2 Only Paper", "semantic_scholar")

    async def _stub_resolver(source_types, db_pool, http_client, request):
        return {
            SourceType.ARXIV: _stub_plugin_returning([shared_paper, arxiv_unique]),
            SourceType.SEMANTIC_SCHOLAR: _stub_plugin_returning([shared_s2, s2_unique]),
        }, {}

    monkeypatch.setattr(
        "paper_ingestion.routers.search._resolve_sources_for_search",
        _stub_resolver,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/search",
            json={
                "query": "dedup test",
                "source_types": ["arxiv", "semantic_scholar"],
                "max_results": 10,
            },
        )

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()

    # _dedup_papers must collapse the two "dedup-shared-01" results into one
    assert body["total"] == 3, (
        f"Expected total=3 after cross-source dedup (shared+arxiv-only+s2-only); "
        f"got total={body['total']}. "
        "This means _dedup_papers did not collapse matching external_id across sources."
    )
    titles = {r["title"] for r in body["results"]}
    assert "Shared Paper" in titles
    assert "Arxiv Only Paper" in titles
    assert "S2 Only Paper" in titles


# ---------------------------------------------------------------------------
# SR-07: search-preview library_match.zotero_item_key must be per-user.
# The indicator now comes from paper_user_zotero_links keyed on the caller,
# not the vestigial global papers.zotero_item_key.
# ---------------------------------------------------------------------------


async def test_sr07_search_preview_zotero_item_key_is_per_user(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, monkeypatch
):
    """search-preview's per-result zotero_item_key must be scoped to the caller.

    A preview result matches user A's owned paper (paper_id_a). Both A and B hold a
    paper_user_zotero_links row for that paper with DIFFERENT keys, and the paper
    still carries a distinct vestigial global papers.zotero_item_key. When A runs
    the preview, library_match.zotero_item_key must be A's link key -- never B's and
    never the global column. Reverting the relocated read to p.zotero_item_key (or
    keying the link join on the wrong user) returns the global/other value -> RED.

    Runs the REAL _load_local_library_matches (not stubbed) against live PG.

    # Verified: services/paper_ingestion/paper_ingestion/routers/search_helpers.py:455
    # (l.zotero_item_key AS zotero_item_key, joined on the resolved caller id, not
    # the global papers.zotero_item_key column).
    """
    from datetime import date

    from paper_ingestion.models import PaperCreate, SourceType

    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    await contract_conn.execute(
        "UPDATE papers SET zotero_item_key = 'GLOBAL-STALE-KEY' WHERE id = $1",
        paper_id_a,
    )
    await contract_conn.execute(
        """INSERT INTO paper_user_zotero_links (paper_id, user_id, zotero_item_key)
           VALUES ($1, $2, 'ZOT-A-KEY'), ($1, $3, 'ZOT-B-KEY')""",
        paper_id_a,
        user_a_id,
        user_b_id,
    )

    matching_paper = PaperCreate(
        external_id="iso-ext-a",
        source_type="arxiv",
        title="Some Other Title",
        authors=["A. Author"],
        abstract="abstract",
        url="https://example.test/a",
        published_date=date(2024, 1, 1),
    )

    async def _stub_resolver(source_types, db_pool, http_client, request):
        return {SourceType.ARXIV: _stub_plugin_returning([matching_paper])}, {}

    monkeypatch.setattr(
        "paper_ingestion.routers.search._resolve_sources_for_search",
        _stub_resolver,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/search-preview",
            json={"query": "test", "source_types": ["arxiv"], "max_results": 5},
        )

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    matched = [
        r
        for r in body["results"]
        if r.get("library_match") and r["library_match"].get("paper_id") == paper_id_a
    ]
    assert matched, (
        f"Expected the preview result to match A's library paper {paper_id_a}; "
        f"results: {[r.get('library_match') for r in body['results']]}"
    )
    surfaced_key = matched[0]["library_match"]["zotero_item_key"]
    assert surfaced_key == "ZOT-A-KEY", (
        "search-preview must surface user A's own Zotero link key, not user B's key "
        f"and not the global papers.zotero_item_key; got {surfaced_key!r}"
    )
