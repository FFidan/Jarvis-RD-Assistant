"""Zotero push service — business logic and job handlers.

Compatibility facade. The implementation lives in the ``_zotero_*`` submodules
with imports flowing one way (``config`` ← ``push`` / ``highlights`` / ``poll`` ←
``jobs``). This module re-exports the full previous surface — public and private
— so existing imports of ``paper_ingestion.integrations.zotero_service`` and the
test/production patch targets that reference it keep resolving unchanged.
"""

from __future__ import annotations

from paper_ingestion.integrations._zotero_config import (  # noqa: F401
    _CRITICAL_ZOTERO_CONFIG_KEYS,
    ZoteroConfigDecryptError,
    _get_zotero_config,
    _resolve_zotero_user_id,
    logger,
)
from paper_ingestion.integrations._zotero_highlights import (  # noqa: F401
    _annotation_page_number,
    _AttachmentUnavailableError,
    _build_annotation_item,
    _ensure_zotero_attachment,
    _export_one_highlight,
    _get_page_sizes,
    _paper_pdf_path,
    _pdf_page_sizes_sync,
    _persist_attachment_key,
    _pick_pdf_attachment,
    push_highlight_to_zotero,
    push_highlights_for_paper,
    sync_annotations_for_paper,
)
from paper_ingestion.integrations._zotero_jobs import (  # noqa: F401
    _zotero_push_highlights_job,
    _zotero_push_job,
    _zotero_resync_job,
    _zotero_sync_annotations_job,
    _zotero_sync_from_zotero_job,
)
from paper_ingestion.integrations._zotero_poll import (  # noqa: F401
    MAX_ENQUEUE_PER_SYNC,
    _ingest_new_item,
    _link_existing_by_doi,
    _load_poll_config,
    _migrate_unambiguous_legacy_identity,
    _namespace_from_poll_config,
    _parse_zotero_item,
    _ParsedZoteroItem,
    _persist_poll_cursor,
    _PollConfig,
    _safe_parse_zotero_item,
    _ZoteroLibraryNamespace,
    poll_zotero_library,
)
from paper_ingestion.integrations._zotero_push import (  # noqa: F401
    _push_paper_with_conn,
    _resolve_project_collection_keys,
    _session_push_lock,
    push_paper_to_zotero,
    resync_paper_to_zotero,
)
