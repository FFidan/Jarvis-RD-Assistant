"""Contract tests: Zotero DOI de-duplication is visibility-scoped to the syncing user.

``_link_existing_by_doi`` matches a Zotero item against the canonical corpus by
``metadata->>'doi'``. Without a visibility predicate, a caller-controlled DOI that
collides with another tenant's PRIVATE row would ``add_to_library`` the poller onto
that row — leaking the raw PDF and private metadata (the same predicate ``pdfs.py``
enforces). These tests pin the fix against real Postgres so the SQL predicate itself
is evaluated (a mock pool would return rows regardless of the SQL and prove nothing):

- a private un-owned DOI match is NOT linked (returns ``None``, no membership granted);
- a persisted-public DOI match still links (dedup preserved for the common case);
- the poller's own private match still links idempotently (re-sync unaffected).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import jarvis_common.task_registry as task_registry
import pytest
from jarvis_common.paper_visibility import paper_visibility_sql
from jarvis_common.testing import SharedConnPool, shelve_paper
from paper_ingestion.integrations._zotero_poll import _link_existing_by_doi


async def _seed_paper_with_doi(
    conn,
    external_id: str,
    doi: str,
    *,
    visibility_scope: str,
    discovered_by: int | None = None,
) -> int:
    """Insert a canonical paper carrying ``metadata->>'doi'`` (mirrors the shape a
    private Zotero-pulled paper persists via ``_parse_zotero_item``)."""
    return await conn.fetchval(
        """INSERT INTO papers (
               external_id, source_type, title, authors, url,
               discovered_by, visibility_scope, metadata
           )
           VALUES ($1, 'arxiv', 'DOI Dedup Paper', ARRAY['Author'],
                   'https://shared.test/paper', $2, $3, jsonb_build_object('doi', $4::text))
           RETURNING id""",
        external_id,
        discovered_by,
        visibility_scope,
        doi,
    )


async def _is_member(conn, user_id: int, paper_id: int) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM user_library WHERE user_id = $1 AND paper_id = $2)",
        user_id,
        paper_id,
    )


def _patched_annotation_enqueue():
    """Neutralize the ``zotero.sync_annotations`` enqueue so the unit test does not
    depend on a live task broker (mirrors test_zotero_service.py's DOI-link test)."""
    mock_ann_task = MagicMock()
    mock_ann_task.defer_async = AsyncMock()
    return patch.dict(task_registry._TASK_MAP, {"zotero.sync_annotations": mock_ann_task})


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_private_unowned_doi_match_is_not_linked(contract_two_users, contract_conn):
    """A's PRIVATE row shares a DOI; B's poll must NOT link onto it.

    The de-dup match is visibility-scoped, so B's caller-controlled DOI cannot grant
    ``user_library`` membership on A's private row. The helper returns ``None`` and B
    falls through to a namespaced ingest. The visibility predicate over A's row for B
    is False — the exact proxy for the raw-PDF 404 that ``pdfs.py`` enforces.
    """
    user_a = contract_two_users.user_a_id
    user_b = contract_two_users.user_b_id
    private_id = await _seed_paper_with_doi(
        contract_conn, "zotero:priv:a", "10.1/x", visibility_scope="private", discovered_by=user_a
    )
    await shelve_paper(contract_conn, user_a, private_id)

    with _patched_annotation_enqueue():
        result = await _link_existing_by_doi(
            SharedConnPool(contract_conn), "10.1/x", "KEYB", polling_user_id=user_b
        )

    assert result is None, "a private un-owned DOI match must not be linked"
    assert not await _is_member(contract_conn, user_b, private_id), (
        "B must not gain user_library membership on A's private row"
    )
    visible_to_b = await contract_conn.fetchval(
        f"SELECT {paper_visibility_sql(1, alias='p')} FROM papers p WHERE p.id = $2",
        user_b,
        private_id,
    )
    assert visible_to_b is False, "A's private row must remain invisible to B (PDF 404 proxy)"


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_public_doi_match_still_links(contract_two_users, contract_conn):
    """A persisted-public DOI match is still linked — the common dedup case is intact."""
    user_b = contract_two_users.user_b_id
    public_id = await _seed_paper_with_doi(
        contract_conn, "zotero:pub", "10.1/pub", visibility_scope="public"
    )

    with _patched_annotation_enqueue():
        result = await _link_existing_by_doi(
            SharedConnPool(contract_conn), "10.1/pub", "KEYPUB", polling_user_id=user_b
        )

    assert result == "linked", "public dedup must still link"
    assert await _is_member(contract_conn, user_b, public_id), (
        "B must be added to user_library on a public dedup link"
    )


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_own_private_doi_match_is_idempotent(contract_two_users, contract_conn):
    """A re-syncing A's own private row (with existing membership) still links.

    The membership branch of the visibility predicate admits the poller's own prior
    copy, so re-sync stays idempotent — no error, no duplicate membership.
    """
    user_a = contract_two_users.user_a_id
    own_id = await _seed_paper_with_doi(
        contract_conn, "zotero:own:a", "10.1/own", visibility_scope="private", discovered_by=user_a
    )
    await shelve_paper(contract_conn, user_a, own_id)

    with _patched_annotation_enqueue():
        result = await _link_existing_by_doi(
            SharedConnPool(contract_conn), "10.1/own", "KEYOWN", polling_user_id=user_a
        )

    assert result == "linked", "own private re-link must still link"
    assert await _is_member(contract_conn, user_a, own_id), (
        "A must remain a member of their own private row after idempotent re-link"
    )


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_ambiguous_polling_user_links_only_public_rows(contract_two_users, contract_conn):
    """Fail-safe: an ambiguous poll (``polling_user_id=None`` with 2+ active users)
    resolves to no user, so the membership branch matches nothing.

    ``poll_zotero_library`` defaults ``polling_user_id=None`` for scheduled
    system-wide polls; ``_resolve_zotero_user_id`` then returns None. A foreign
    PRIVATE row sharing the DOI is never linked, and even a public dedup grants no
    ``user_library`` membership (the attach is skipped) — private never leaks.
    """
    user_a = contract_two_users.user_a_id
    private_id = await _seed_paper_with_doi(
        contract_conn,
        "zotero:amb:priv",
        "10.1/amb-priv",
        visibility_scope="private",
        discovered_by=user_a,
    )
    public_id = await _seed_paper_with_doi(
        contract_conn, "zotero:amb:pub", "10.1/amb-pub", visibility_scope="public"
    )

    with _patched_annotation_enqueue():
        priv_result = await _link_existing_by_doi(
            SharedConnPool(contract_conn), "10.1/amb-priv", "KAMB1", polling_user_id=None
        )
        pub_result = await _link_existing_by_doi(
            SharedConnPool(contract_conn), "10.1/amb-pub", "KAMB2", polling_user_id=None
        )

    assert priv_result is None, "ambiguous poll must not link a foreign private row"
    assert pub_result == "linked", "public dedup still resolves under ambiguity"
    granted = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM user_library WHERE paper_id = ANY($1::int[])",
        [private_id, public_id],
    )
    assert granted == 0, "ambiguous poll must not grant any user_library membership"
