"""Tests for the fixed scientific RAG benchmark seeder."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "perf" / "seed_scientific_rag_pack.py"
_MANIFEST = _REPO_ROOT / "docs" / "perf" / "eval_sets" / "2026-07-03-scientific-rag-eval.jsonl"


def _load_module() -> Any:
    """Load the seeder script as a test module."""
    spec = importlib.util.spec_from_file_location("seed_scientific_rag_pack", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("seed_scientific_rag_pack", module)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_manifest_extraction_returns_fixed_pack_papers_only() -> None:
    """Verify manifest extraction keeps only the ten fixed paper rows."""
    module = _load_module()

    papers = module.load_fixed_pack(_MANIFEST)

    assert len(papers) == 10
    assert {paper.paper_key for paper in papers} == {
        "p1_attention",
        "p2_neural_ode",
        "p3_lora",
        "p4_resnet",
        "p5_ddpm",
        "p6_gat",
        "p7_unet",
        "p8_adam",
        "p9_bert",
        "p10_rag",
    }
    assert all(paper.identifier.startswith("arXiv:") for paper in papers)


class FakeClient:
    """In-memory product client for seeder unit tests."""

    def __init__(self, library: list[dict], details: dict[int, dict] | None = None) -> None:
        """Store fake library and detail payloads for later calls."""
        self.library = library
        self.details = details or {}
        self.calls: list[tuple[str, object]] = []

    def list_library(self) -> list[dict]:
        """Return configured library rows."""
        self.calls.append(("list_library", None))
        return self.library

    def get_paper_detail(self, paper_id: int) -> dict:
        """Return configured detail payload for a local paper id."""
        self.calls.append(("get_paper_detail", paper_id))
        return self.details.get(paper_id, {"paper": {"pdf_downloaded": False}, "chunks": []})

    def search(self, paper) -> list[dict]:
        """Record a legacy search call; seeding should not use it."""
        self.calls.append(("search", paper.paper_key))
        return []

    def save_paper(self, paper_id: int) -> None:
        """Record a legacy save call; seeding should not use it."""
        self.calls.append(("save_paper", paper_id))

    def download_pdf(self, paper_id: int) -> None:
        """Record a PDF download request."""
        self.calls.append(("download_pdf", paper_id))

    def process_pdf(self, paper_id: int) -> None:
        """Record a synchronous PDF processing request."""
        self.calls.append(("process_pdf", paper_id))


def test_check_only_does_not_call_mutating_client_methods(tmp_path: Path) -> None:
    """Verify check-only mode avoids mutating product client methods."""
    module = _load_module()
    papers = module.load_fixed_pack(_MANIFEST)[:1]
    client = FakeClient(
        library=[{"id": 42, "title": papers[0].title}],
        details={42: {"paper": {"id": 42, "pdf_downloaded": True}, "chunks": [{"id": 1}]}},
    )

    result = module.run_check_only(client, papers, tmp_path, allow_extra_library_papers=False)

    assert result.ready is True
    assert [call[0] for call in client.calls] == ["list_library", "get_paper_detail"]


def test_fixed_pack_only_rejects_extra_library_papers() -> None:
    """Verify non-diagnostic checks reject libraries with extra papers."""
    module = _load_module()
    papers = module.load_fixed_pack(_MANIFEST)[:1]
    client = FakeClient(
        library=[
            {"id": 1, "title": papers[0].title},
            {"id": 2, "title": "An unrelated paper in the same library"},
        ]
    )

    with pytest.raises(module.SeedPackError, match="outside the fixed pack"):
        module.run_check_only(client, papers, None, allow_extra_library_papers=False)


def test_seed_fails_closed_when_fixed_paper_is_missing(tmp_path: Path) -> None:
    """Verify seed mode cannot fall back to broad discovery search.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for ignored readiness artifacts.
    """
    module = _load_module()
    papers = module.load_fixed_pack(_MANIFEST)[:1]
    client = FakeClient(library=[])

    with pytest.raises(module.SeedPackError, match="Cannot seed missing fixed-pack papers"):
        module.run_seed(client, papers, tmp_path, allow_extra_library_papers=False)

    assert [call[0] for call in client.calls] == ["list_library"]


def test_seed_processes_existing_fixed_papers_without_save_side_effect(
    tmp_path: Path,
) -> None:
    """Verify seed mode does not enqueue analysis through the save endpoint.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for ignored readiness artifacts.
    """
    module = _load_module()
    papers = module.load_fixed_pack(_MANIFEST)[:1]
    client = FakeClient(
        library=[{"id": 42, "title": papers[0].title}],
        details={
            42: {"paper": {"id": 42, "pdf_downloaded": False}, "chunks": []},
        },
    )

    result = module.run_seed(client, papers, tmp_path, allow_extra_library_papers=False)

    assert result.ready is False
    assert ("save_paper", 42) not in client.calls
    assert ("download_pdf", 42) in client.calls
    assert ("process_pdf", 42) in client.calls


def test_allow_extra_library_papers_is_diagnostic_only(tmp_path: Path) -> None:
    """Verify diagnostic extra-library checks do not write capture maps or seed.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for readiness artifacts.
    """
    module = _load_module()
    papers = module.load_fixed_pack(_MANIFEST)[:1]
    client = FakeClient(
        library=[
            {"id": 1, "title": papers[0].title},
            {"id": 2, "title": "An unrelated paper"},
        ],
        details={1: {"paper": {"id": 1, "pdf_downloaded": True}, "chunks": [{"id": 1}]}},
    )

    result = module.run_check_only(client, papers, tmp_path, allow_extra_library_papers=True)

    assert result.ready is True
    assert (tmp_path / "readiness.json").exists()
    assert not (tmp_path / "paper_map.json").exists()
    with pytest.raises(module.SeedPackError, match="diagnostic-only"):
        module.run_seed(client, papers, tmp_path, allow_extra_library_papers=True)


def test_duplicate_fixed_pack_titles_are_rejected() -> None:
    """Verify duplicate fixed-pack title matches cannot overwrite paper maps."""
    module = _load_module()
    papers = module.load_fixed_pack(_MANIFEST)[:1]
    client = FakeClient(
        library=[
            {"id": 1, "title": papers[0].title},
            {"id": 2, "title": papers[0].title},
        ]
    )

    with pytest.raises(module.SeedPackError, match="duplicate fixed-pack titles"):
        module.run_check_only(client, papers, None, allow_extra_library_papers=False)


def test_cli_accepts_plan_and_readme_argument_names(tmp_path: Path) -> None:
    """Verify operator-facing CLI names match the execution plan.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for ignored artifact paths.
    """
    module = _load_module()
    cookie_path = tmp_path / "cookie.txt"
    out_dir = tmp_path / "run"

    args = module.parse_args(
        [
            "--check-only",
            "--api-base",
            "http://127.0.0.1:8080",
            "--auth-cookie-file",
            str(cookie_path),
            "--out-dir",
            str(out_dir),
        ]
    )

    assert args.api_base == "http://127.0.0.1:8080"
    assert args.auth_cookie_file == cookie_path
    assert args.out_dir == out_dir


def test_cli_defaults_to_the_product_gateway() -> None:
    """Verify the operator default exercises the public gateway boundary."""
    module = _load_module()

    args = module.parse_args(["--check-only"])

    assert args.api_base == "http://127.0.0.1:3001"


def test_pdf_processing_uses_the_public_job_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify processing enqueues and polls without the synchronous query path."""
    module = _load_module()
    observed: list[tuple[str, str, float]] = []
    payloads = iter(
        [
            {"job_id": "job-1", "status": "queued"},
            {"id": "job-1", "status": "running"},
            {"id": "job-1", "status": "succeeded"},
        ]
    )

    class Response:
        """Minimal context-managed JSON response."""

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._payload).encode()

    def fake_urlopen(request: Any, *, timeout: float) -> Response:
        observed.append((request.get_method(), request.full_url, timeout))
        return Response(next(payloads))

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    client = module.ProductHttpClient("http://127.0.0.1:3001", {})

    client.process_pdf(42)

    assert observed == [
        ("POST", "http://127.0.0.1:3001/api/process-pdf/42", 120.0),
        ("GET", "http://127.0.0.1:3001/api/jobs/job-1", 120.0),
        ("GET", "http://127.0.0.1:3001/api/jobs/job-1", 120.0),
    ]


def test_pdf_processing_fails_closed_on_terminal_job_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a failed owner job cannot be reported as seeded readiness."""
    module = _load_module()
    payloads = iter(
        [
            {"job_id": "job-1", "status": "queued"},
            {"id": "job-1", "status": "failed"},
        ]
    )

    class Response:
        """Minimal context-managed JSON response."""

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._payload).encode()

    monkeypatch.setattr(
        module,
        "urlopen",
        lambda _request, *, timeout: Response(next(payloads)),
    )
    client = module.ProductHttpClient("http://127.0.0.1:3001", {})

    with pytest.raises(module.SeedPackError, match="ended with status failed"):
        client.process_pdf(42)


def test_credential_file_handling_loads_values_without_printing_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify auth files are loaded without printing secret values."""
    module = _load_module()
    headers_path = tmp_path / "headers.json"
    cookies_path = tmp_path / "cookies.txt"
    headers_path.write_text(json.dumps({"Authorization": "Bearer very-secret"}), encoding="utf-8")
    cookies_path.write_text("jarvis_session=secret-cookie\n", encoding="utf-8")

    auth = module.load_auth_files(headers_path=headers_path, cookies_path=cookies_path)

    assert auth.headers["Authorization"] == "Bearer very-secret"
    assert auth.headers["Cookie"] == "jarvis_session=secret-cookie"
    captured = capsys.readouterr()
    assert "very-secret" not in captured.out + captured.err
    assert "secret-cookie" not in captured.out + captured.err


def test_auth_cookie_file_accepts_mozilla_cookie_jar(tmp_path: Path) -> None:
    """Verify browser/curl cookie jars are converted to a Cookie header.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for the cookie jar fixture.
    """
    module = _load_module()
    cookies_path = tmp_path / "jarvis-cookie.txt"
    cookies_path.write_text(
        "# Netscape HTTP Cookie File\n"
        "127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tjarvis_session\tsecret-cookie\n",
        encoding="utf-8",
    )

    auth = module.load_auth_files(headers_path=None, cookies_path=cookies_path)

    assert auth.headers["Cookie"] == "jarvis_session=secret-cookie"


def test_paper_map_is_not_written_until_every_paper_has_local_id(tmp_path: Path) -> None:
    """Verify partial readiness never writes a capture paper map."""
    module = _load_module()
    papers = module.load_fixed_pack(_MANIFEST)[:2]
    client = FakeClient(
        library=[{"id": 1, "title": papers[0].title}],
        details={1: {"paper": {"id": 1, "pdf_downloaded": True}, "chunks": [{"id": 1}]}},
    )

    result = module.run_check_only(client, papers, tmp_path, allow_extra_library_papers=False)

    assert result.ready is False
    assert not (tmp_path / "paper_map.json").exists()
