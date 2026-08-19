#!/usr/bin/env python3
"""Seed and verify the fixed scientific RAG benchmark paper pack."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "artifacts/perf"
DEFAULT_RUN_ID = "scientific-rag-pack"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0
PDF_PROCESS_DEADLINE_SECONDS = 900.0
JOB_POLL_INTERVAL_SECONDS = 1.0


class SeedPackError(RuntimeError):
    """Raised when the fixed-pack seeder cannot safely continue."""


@dataclass(frozen=True)
class FixedPaper:
    """A public paper entry from the fixed scientific RAG manifest."""

    paper_key: str
    title: str
    identifier: str


@dataclass(frozen=True)
class AuthMaterial:
    """HTTP authentication material loaded from non-CLI secret files."""

    headers: dict[str, str]


@dataclass(frozen=True)
class ReadinessResult:
    """Readiness rows plus aggregate status for the fixed pack."""

    rows: list[dict[str, Any]]
    ready: bool


class SeedClient(Protocol):
    """Product HTTP operations used by the seeder."""

    def list_library(self) -> list[dict[str, Any]]:
        """Return lightweight rows for the authenticated library.

        Returns
        -------
        list[dict[str, Any]]
            Paper rows visible to the authenticated owner.

        """
        ...

    def get_paper_detail(self, paper_id: int) -> dict[str, Any]:
        """Return the current PDF and indexing state for one paper.

        Parameters
        ----------
        paper_id : int
            Local paper identifier.

        Returns
        -------
        dict[str, Any]
            Current product representation of the paper.

        """
        ...

    def download_pdf(self, paper_id: int) -> None:
        """Request a PDF download for one local paper.

        Parameters
        ----------
        paper_id : int
            Local paper identifier.

        """
        ...

    def process_pdf(self, paper_id: int) -> None:
        """Request and await PDF processing for one local paper.

        Parameters
        ----------
        paper_id : int
            Local paper identifier.

        """
        ...


class ProductHttpClient:
    """Minimal stdlib HTTP client for product-supported seeding routes."""

    def __init__(self, base_url: str, headers: dict[str, str]) -> None:
        """Initialize a client without retaining caller-owned header state.

        Parameters
        ----------
        base_url : str
            Product gateway base URL.
        headers : dict[str, str]
            Authentication headers loaded from protected files.

        """
        self._base_url = base_url.rstrip("/")
        self._headers = dict(headers)

    def list_library(self) -> list[dict[str, Any]]:
        """Return the caller's lightweight library rows.

        Returns
        -------
        list[dict[str, Any]]
            Paper rows visible to the authenticated owner.

        """
        body = self._request("GET", "/api/papers/brief")
        if not isinstance(body, list):
            raise SeedPackError("/api/papers/brief returned a non-list response")
        return [dict(row) for row in body]

    def get_paper_detail(self, paper_id: int) -> dict[str, Any]:
        """Return paper detail, including PDF and chunk readiness fields.

        Parameters
        ----------
        paper_id : int
            Local paper identifier.

        Returns
        -------
        dict[str, Any]
            Current product representation of the paper.

        """
        body = self._request("GET", f"/api/papers/{paper_id}")
        if not isinstance(body, dict):
            raise SeedPackError(f"/api/papers/{paper_id} returned a non-object response")
        return body

    def download_pdf(self, paper_id: int) -> None:
        """Download a paper PDF through the product PDF endpoint.

        Parameters
        ----------
        paper_id : int
            Local paper identifier.

        """
        self._request("POST", f"/api/download-pdf/{paper_id}")

    def process_pdf(self, paper_id: int) -> None:
        """Process a paper through the public background-job workflow.

        Parameters
        ----------
        paper_id : int
            Local paper identifier to process.

        Raises
        ------
        SeedPackError
            If enqueueing fails, the job reports failure or cancellation, or
            the bounded processing deadline expires.

        """
        queued = self._request("POST", f"/api/process-pdf/{paper_id}")
        if not isinstance(queued, dict):
            raise SeedPackError("PDF processing enqueue returned a non-object response")
        job_id = queued.get("job_id")
        if queued.get("status") != "queued" or not isinstance(job_id, str) or not job_id:
            raise SeedPackError("PDF processing enqueue returned an invalid job response")

        deadline = time.monotonic() + PDF_PROCESS_DEADLINE_SECONDS
        while True:
            job = self._request("GET", f"/api/jobs/{job_id}")
            if not isinstance(job, dict):
                raise SeedPackError("PDF processing status returned a non-object response")
            status = job.get("status")
            if status == "succeeded":
                return
            if status in {"failed", "cancelled"}:
                raise SeedPackError(f"PDF processing job ended with status {status}")
            if status not in {"queued", "running"}:
                raise SeedPackError("PDF processing job returned an invalid status")
            if time.monotonic() >= deadline:
                raise SeedPackError("PDF processing job exceeded the bounded wait")
            time.sleep(JOB_POLL_INTERVAL_SECONDS)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", **self._headers}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(  # noqa: S310 - operator-supplied local URL.
                request,
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                payload = response.read()
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise SeedPackError(f"{method} {path} failed with HTTP {exc.code}: {message}") from exc
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))


def load_fixed_pack(path: Path = DEFAULT_MANIFEST) -> list[FixedPaper]:
    """Load exactly the paper rows from the scientific RAG eval manifest.

    Parameters
    ----------
    path : Path, optional
        JSONL manifest containing mixed ``paper`` and ``question`` rows.

    Returns
    -------
    list[FixedPaper]
        Paper rows in manifest order; question rows are ignored.

    """
    papers: list[FixedPaper] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") != "paper":
            continue
        try:
            papers.append(
                FixedPaper(
                    paper_key=_required_str(row, "paper_key", line_no),
                    title=_required_str(row, "title", line_no),
                    identifier=_required_str(row, "identifier", line_no),
                )
            )
        except TypeError as exc:
            raise SeedPackError(f"Invalid paper row at line {line_no}: {exc}") from exc
    if len(papers) != 10:
        raise SeedPackError(f"Expected 10 fixed-pack paper rows, found {len(papers)}")
    return papers


def load_auth_files(headers_path: Path | None, cookies_path: Path | None) -> AuthMaterial:
    """Load secret-bearing HTTP headers from files without printing values.

    Parameters
    ----------
    headers_path : Path or None
        Optional JSON object mapping header names to values.
    cookies_path : Path or None
        Optional raw Cookie header or Mozilla/curl cookie-jar file.

    Returns
    -------
    AuthMaterial
        Headers suitable for authenticated product API calls.

    """
    headers: dict[str, str] = {}
    if headers_path is not None:
        raw_headers = json.loads(headers_path.read_text(encoding="utf-8"))
        if not isinstance(raw_headers, dict):
            raise SeedPackError("Header file must contain a JSON object")
        headers.update(_string_header_map(raw_headers))
    if cookies_path is not None:
        headers["Cookie"] = _read_cookie_header(cookies_path)
    return AuthMaterial(headers=headers)


def _read_cookie_header(path: Path) -> str:
    """Return a Cookie header from a raw header file or Mozilla cookie jar.

    Parameters
    ----------
    path : Path
        File containing either a raw Cookie header or a browser/curl cookie jar.

    Returns
    -------
    str
        Cookie header value safe to attach to product API requests.

    """
    jar = MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except (LoadError, OSError):
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise SeedPackError(f"auth cookie file is empty: {path}")
        return raw
    cookies = [f"{cookie.name}={cookie.value}" for cookie in jar]
    if not cookies:
        raise SeedPackError(f"auth cookie file has no cookies: {path}")
    return "; ".join(cookies)


def run_check_only(
    client: SeedClient,
    fixed_pack: list[FixedPaper],
    output_dir: Path | None,
    *,
    allow_extra_library_papers: bool,
) -> ReadinessResult:
    """Check fixed-pack readiness without mutating product state.

    Parameters
    ----------
    client : SeedClient
        Injected product client.
    fixed_pack : list[FixedPaper]
        Fixed scientific RAG paper pack.
    output_dir : Path or None
        Optional artifact directory for readiness JSON and paper map.
    allow_extra_library_papers : bool
        Permit diagnostic inventory when the authenticated library has extras.

    Returns
    -------
    ReadinessResult
        Per-paper readiness rows and aggregate readiness.

    """
    result = _build_readiness(client, fixed_pack, allow_extra_library_papers)
    _write_artifacts(
        output_dir,
        result,
        write_paper_map=not allow_extra_library_papers,
    )
    return result


def run_seed(
    client: SeedClient,
    fixed_pack: list[FixedPaper],
    output_dir: Path,
    *,
    allow_extra_library_papers: bool,
    process_delay_seconds: float = 0.0,
) -> ReadinessResult:
    """Seed missing fixed-pack papers through supported product HTTP APIs.

    Parameters
    ----------
    client : SeedClient
        Injected product client.
    fixed_pack : list[FixedPaper]
        Fixed scientific RAG paper pack.
    output_dir : Path
        Artifact directory under ``artifacts/perf/<run-id>``.
    allow_extra_library_papers : bool
        Permit diagnostic inventory when the authenticated library has extras.
    process_delay_seconds : float, optional
        Delay after each completed processing job, for constrained local hosts.

    Returns
    -------
    ReadinessResult
        Final readiness after search, save, download, and process attempts.

    """
    if allow_extra_library_papers:
        raise SeedPackError(
            "--allow-extra-library-papers is diagnostic-only and cannot be used with --seed"
        )
    initial = _build_readiness(client, fixed_pack, allow_extra_library_papers)
    missing = [row["paper_key"] for row in initial.rows if row["local_paper_id"] is None]
    if missing:
        preview = ", ".join(missing[:5])
        raise SeedPackError(
            "Cannot seed missing fixed-pack papers through broad product search. "
            "Run scripts/perf/import_fixed_pack_arxiv.py inside the paper_ingestion "
            f"service first. Missing: {preview}"
        )

    for row in initial.rows:
        paper_id = row["local_paper_id"]
        if paper_id is None:
            continue
        if not row["downloaded"]:
            client.download_pdf(paper_id)
        if not row["chunked"]:
            client.process_pdf(paper_id)
            if process_delay_seconds > 0:
                time.sleep(process_delay_seconds)

    result = _build_readiness(client, fixed_pack, allow_extra_library_papers)
    _write_artifacts(output_dir, result, write_paper_map=True)
    if not _all_papers_mapped(result.rows):
        raise SeedPackError("Not every fixed-pack paper mapped to a confirmed local paper id")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for fixed-pack readiness and seeding."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="Inspect readiness without mutation",
    )
    mode.add_argument(
        "--seed",
        action="store_true",
        help="Seed missing papers through product APIs",
    )
    parser.add_argument(
        "--api-base",
        "--base-url",
        dest="api_base",
        default="http://127.0.0.1:3001",
        help="Product API base URL",
    )
    parser.add_argument(
        "--header-file",
        "--headers-file",
        dest="header_file",
        type=Path,
        help="JSON object of HTTP headers",
    )
    parser.add_argument(
        "--auth-cookie-file",
        "--cookies-file",
        dest="auth_cookie_file",
        type=Path,
        help="File containing a raw Cookie header or Mozilla/curl cookie jar",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out-dir", type=Path, help="Artifact directory for this run")
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--allow-extra-library-papers", action="store_true")
    parser.add_argument(
        "--process-delay-seconds",
        type=float,
        default=0.0,
        help="Pause after each sync process request to respect product rate limits",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for fixed-pack readiness and seeding."""
    args = parse_args(argv)
    fixed_pack = load_fixed_pack(args.manifest)
    auth = load_auth_files(args.header_file, args.auth_cookie_file)
    output_dir = args.out_dir or (args.artifact_root / args.run_id)
    client = ProductHttpClient(args.api_base, auth.headers)

    if args.check_only:
        result = run_check_only(
            client,
            fixed_pack,
            output_dir,
            allow_extra_library_papers=args.allow_extra_library_papers,
        )
    else:
        result = run_seed(
            client,
            fixed_pack,
            output_dir,
            allow_extra_library_papers=args.allow_extra_library_papers,
            process_delay_seconds=args.process_delay_seconds,
        )
    json.dump({"ready": result.ready, "papers": result.rows}, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result.ready else 2


def _required_str(row: dict[str, Any], key: str, line_no: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SeedPackError(f"Manifest line {line_no} missing string field {key!r}")
    return value


def _string_header_map(raw_headers: dict[Any, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SeedPackError("Header file keys and values must be strings")
        headers[key] = value
    return headers


def _build_readiness(
    client: SeedClient,
    fixed_pack: list[FixedPaper],
    allow_extra_library_papers: bool,
) -> ReadinessResult:
    library = client.list_library()
    fixed_by_title = {_normalize_title(paper.title): paper for paper in fixed_pack}
    extra_titles = [
        str(row.get("title") or "<missing title>")
        for row in library
        if _normalize_title(str(row.get("title") or "")) not in fixed_by_title
    ]
    if extra_titles and not allow_extra_library_papers:
        preview = ", ".join(extra_titles[:3])
        raise SeedPackError(
            f"Authenticated library contains papers outside the fixed pack: {preview}"
        )

    library_by_title = _index_fixed_library_rows(library, fixed_by_title)
    rows = [
        _readiness_row(client, paper, library_by_title.get(_normalize_title(paper.title)))
        for paper in fixed_pack
    ]
    return ReadinessResult(rows=rows, ready=all(row["searchable"] for row in rows))


def _index_fixed_library_rows(
    library: list[dict[str, Any]], fixed_by_title: dict[str, FixedPaper]
) -> dict[str, dict[str, Any]]:
    rows_by_title: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in library:
        title_key = _normalize_title(str(row.get("title") or ""))
        if title_key not in fixed_by_title:
            continue
        if title_key in rows_by_title:
            duplicates.append(str(row.get("title") or fixed_by_title[title_key].title))
            continue
        rows_by_title[title_key] = row
    if duplicates:
        preview = ", ".join(sorted(set(duplicates))[:3])
        raise SeedPackError(f"Authenticated library has duplicate fixed-pack titles: {preview}")
    return rows_by_title


def _readiness_row(
    client: SeedClient,
    paper: FixedPaper,
    library_row: dict[str, Any] | None,
) -> dict[str, Any]:
    paper_id = _paper_id(library_row)
    downloaded = False
    chunked = False
    if paper_id is not None:
        detail = client.get_paper_detail(paper_id)
        paper_detail = detail.get("paper") if isinstance(detail, dict) else {}
        chunks = detail.get("chunks") if isinstance(detail, dict) else []
        downloaded = (
            bool(paper_detail.get("pdf_downloaded")) if isinstance(paper_detail, dict) else False
        )
        chunked = bool(chunks) if isinstance(chunks, list) else False
    searchable = paper_id is not None and downloaded and chunked
    row: dict[str, Any] = {
        "paper_key": paper.paper_key,
        "title": paper.title,
        "identifier": paper.identifier,
        "local_paper_id": paper_id,
        "downloaded": downloaded,
        "chunked": chunked,
        "searchable": searchable,
    }
    reason = _not_runnable_reason(paper_id, downloaded, chunked)
    if reason is not None:
        row["not_runnable_reason"] = reason
    return row


def _paper_id(row: dict[str, Any] | None) -> int | None:
    if row is None:
        return None
    value = row.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _not_runnable_reason(paper_id: int | None, downloaded: bool, chunked: bool) -> str | None:
    if paper_id is None:
        return "missing_from_authenticated_library"
    if not downloaded:
        return "pdf_not_downloaded"
    if not chunked:
        return "not_chunked"
    return None


def _write_artifacts(
    output_dir: Path | None, result: ReadinessResult, *, write_paper_map: bool
) -> None:
    if output_dir is None:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "readiness.json").write_text(
        json.dumps({"ready": result.ready, "papers": result.rows}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if write_paper_map and _all_papers_mapped(result.rows):
        paper_map = {row["paper_key"]: row["local_paper_id"] for row in result.rows}
        (output_dir / "paper_map.json").write_text(
            json.dumps(paper_map, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _all_papers_mapped(rows: list[dict[str, Any]]) -> bool:
    return all(isinstance(row.get("local_paper_id"), int) for row in rows)


def _normalize_title(title: str) -> str:
    return " ".join(title.casefold().split())


if __name__ == "__main__":
    raise SystemExit(main())
