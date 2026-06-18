from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.routers import analyze


@pytest.mark.asyncio
async def test_analyze_stream_forwards_force_to_run_process_pdf(tmp_path, monkeypatch):
    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")

    # local + already-downloaded is required: any other shape routes through the
    # download branch, which never reaches the process call the assertion targets.
    row = {
        "id": 7,
        "source_type": "local",
        "pdf_url": None,
        "pdf_downloaded": True,
        "pdf_local_path": str(pdf_file),
    }

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    pool = MagicMock()
    pool.acquire = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    monkeypatch.setattr(analyze, "current_user_id_strict", AsyncMock(return_value=1))
    monkeypatch.setattr(analyze, "assert_paper_ownership", AsyncMock(return_value=None))
    monkeypatch.setattr(analyze, "check_pdf_path_safe", lambda *a, **k: True)

    # config, pdf_workflow and summarization are imported inside the generator,
    # so a setattr on the analyze module would not be seen — the stub must be
    # installed in sys.modules to intercept the call-time import.
    settings_stub = MagicMock()
    settings_stub.get_paper_ingestion_settings = MagicMock(
        return_value=SimpleNamespace(pdf_storage_path=str(tmp_path))
    )
    monkeypatch.setitem(sys.modules, "paper_ingestion.config", settings_stub)

    mock_rpp = AsyncMock(return_value={"paper_id": 7, "chunk_count": 3, "status": "processed"})
    workflow_stub = MagicMock()
    workflow_stub.run_process_pdf = mock_rpp
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", workflow_stub)
    summ_stub = MagicMock()
    summ_stub.generate_paper_summary = AsyncMock()
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", summ_stub)

    gen = analyze._analyze_stream(
        request=MagicMock(),
        paper_id=7,
        db_pool=pool,
        http_client=MagicMock(),
        pdf_processor=MagicMock(),
        embedder=MagicMock(),
        verifier=MagicMock(),
        force=True,
    )
    async for _ in gen:
        pass

    mock_rpp.assert_awaited_once()
    rpp_call = mock_rpp.await_args
    assert rpp_call is not None
    assert rpp_call.kwargs.get("force") is True, (
        f"run_process_pdf must be called with force=True; got {rpp_call.kwargs}"
    )
