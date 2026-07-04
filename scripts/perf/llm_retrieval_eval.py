#!/usr/bin/env python3
"""Scientific RAG benchmark harness for local model/retrieval decisions.

The harness is intentionally conservative: it validates a fixed manifest,
captures or aggregates answer rows, and refuses to emit a promotion decision
when coverage is too small. CI can run ``--dry-run`` to validate parser and
report logic without a model, network, or database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib import error, request

MIN_PAPERS = 8
MIN_QUESTIONS = 24
SCORE_DIMENSIONS = (
    "scientific_correctness",
    "evidence_grounding",
    "citation_label_stability",
    "quantitative_fidelity",
    "synthesis_quality",
    "uncertainty_behavior",
    "visible_answer_quality",
)
GROUNDING_DIMENSIONS = ("evidence_grounding", "citation_label_stability")
ANSWER_KEY_DEFAULT = Path("docs/perf/eval_sets/2026-07-03-scientific-rag-answer-key.jsonl")
HIDDEN_REASONING_MARKERS = (
    "<think>",
    "</think>",
    "harmony analysis",
    "scratchpad",
    "<|im_start|>",
    "<|im_end|>",
    "i need to",
    "let me",
    "we need to",
)
REQUIRED_JUDGED_BOOL_FIELDS = (
    "structured_output_valid",
    "wrong_paper_central_claim",
    "unanswerable_fabricated_positive_claim",
    "promotion_eligible",
)


class EvalHarnessError(RuntimeError):
    """Raised for manifest, answer, or execution errors."""


@dataclass(frozen=True)
class EvalPaper:
    """Fixed-paper metadata from the scientific RAG manifest.

    Attributes
    ----------
    paper_key
        Stable manifest key used by questions and local paper maps.
    identifier
        Public paper identifier such as DOI, arXiv id, or URL.
    title
        Human-readable paper title for reports and fixtures.
    expected_sections
        Sections expected to be available after ingestion.
    known_terms
        Terms useful for sanity-checking retrieved evidence.
    forbidden_confusions
        Nearby papers or concepts the benchmark should not conflate.
    """

    paper_key: str
    identifier: str
    title: str
    expected_sections: list[str]
    known_terms: list[str]
    forbidden_confusions: list[str]


@dataclass(frozen=True)
class EvalQuestion:
    """Fixed scientific RAG question and scoring contract.

    Attributes
    ----------
    question_id
        Stable identifier used for exact answer-row coverage.
    question
        User-facing question sent to the product RAG endpoint.
    scope
        Retrieval scope: single-paper, cross-paper, or unanswerable.
    required_papers
        Manifest paper keys that bound the expected evidence.
    expected_evidence
        Evidence anchors used by human or executor judging.
    scientific_traps
        Known failure modes the answer should avoid.
    rubric_weights
        Per-dimension weights for the aggregate quality score.
    """

    question_id: str
    question: str
    scope: str
    required_papers: list[str]
    expected_evidence: list[str]
    scientific_traps: list[str]
    rubric_weights: dict[str, float]


@dataclass(frozen=True)
class EvalManifest:
    """Validated paper and question pack for one benchmark run.

    Attributes
    ----------
    papers
        Paper metadata keyed by stable manifest paper key.
    questions
        Fixed question list that every candidate must answer exactly once.
    """

    papers: dict[str, EvalPaper]
    questions: list[EvalQuestion]


@dataclass(frozen=True)
class CaptureConfig:
    """Runtime settings for product RAG capture mode.

    Attributes
    ----------
    api_base
        Base URL for the product API under test.
    candidate
        Candidate label written to raw answer rows.
    paper_map
        Mapping from manifest paper keys to local database paper ids.
    api_key
        Optional API key for deployments that accept header auth.
    auth_cookie_file
        Optional cookie file for browser-session authenticated capture.
    timeout
        Per-request timeout in seconds.
    max_chunks
        Maximum retrieved chunks requested from product RAG endpoints.
    max_papers
        Maximum papers requested from the cross-paper endpoint.
    decompose
        Whether the product cross-paper route may decompose the question.
    fixed_pack_library_confirmed
        Operator assertion that the authenticated library contains only the
        fixed paper pack for library-wide retrieval.
    """

    api_base: str
    candidate: str
    paper_map: dict[str, int | str]
    api_key: str | None
    auth_cookie_file: Path | None
    timeout: float
    max_chunks: int
    max_papers: int
    decompose: bool
    fixed_pack_library_confirmed: bool


@dataclass(frozen=True)
class RawCaptureGateResult:
    """Pre-judging hard-gate summary for capture-only product rows.

    The raw gate verifies that capture rows are complete enough to hand to a
    human or executor judge. It is intentionally not a scoring path and does
    not make promotion decisions.
    """

    row_count: int
    candidate_labels: list[str]
    question_coverage: dict[str, Any]
    http_status_counts: dict[str, int]
    empty_answer_count: int
    hidden_reasoning_leak_count: int
    retrieval_scope_counts: dict[str, int]
    outside_fixed_pack_source_count: int | str
    latency_ms: dict[str, float | str]
    vram_peak_mb: float | str
    null_scores_count: int
    eligible_for_judging: bool
    blocking_reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable representation."""
        return {
            "row_count": self.row_count,
            "candidate_labels": self.candidate_labels,
            "question_coverage": self.question_coverage,
            "http_status_counts": self.http_status_counts,
            "empty_answer_count": self.empty_answer_count,
            "hidden_reasoning_leak_count": self.hidden_reasoning_leak_count,
            "retrieval_scope_counts": self.retrieval_scope_counts,
            "outside_fixed_pack_source_count": self.outside_fixed_pack_source_count,
            "latency_ms": self.latency_ms,
            "vram_peak_mb": self.vram_peak_mb,
            "null_scores_count": self.null_scores_count,
            "eligible_for_judging": self.eligible_for_judging,
            "blocking_reasons": self.blocking_reasons,
        }


@dataclass
class CandidateStats:
    """Accumulated hard-fail counters and scores for one candidate.

    Attributes
    ----------
    empty_count
        Number of rows with no visible answer.
    hidden_count
        Number of rows leaking visible hidden-reasoning markers.
    wrong_paper_count
        Number of rows whose central claim is grounded in the wrong paper.
    unanswerable_fabrication_count
        Number of unanswerable rows that fabricate a positive claim.
    structured_fail_count
        Number of rows that failed the expected output structure.
    promotion_eligible
        False when any row is fixture-only or otherwise non-promotable.
    latencies
        Per-question latency measurements in milliseconds.
    quality_scores
        Weighted per-question quality percentages.
    grounding_scores
        Per-question grounding percentages.
    """

    empty_count: int = 0
    hidden_count: int = 0
    wrong_paper_count: int = 0
    unanswerable_fabrication_count: int = 0
    structured_fail_count: int = 0
    promotion_eligible: bool = True
    latencies: list[float] = field(default_factory=list)
    quality_scores: list[float] = field(default_factory=list)
    grounding_scores: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreSet:
    """Validated rubric scores for one judged answer row.

    Attributes
    ----------
    values
        Per-dimension scores normalized to floats in the inclusive range 0..2.
    """

    values: dict[str, float]

    @classmethod
    def from_raw(cls, raw: Any, candidate: str, question_id: str) -> ScoreSet:
        """Build a score set from a raw answer row field.

        Parameters
        ----------
        raw
            Value of the row's ``scores`` field.
        candidate
            Candidate label used in failure messages.
        question_id
            Question id used in failure messages.

        Returns
        -------
        ScoreSet
            Validated rubric scores.
        """

        if not isinstance(raw, dict):
            raise EvalHarnessError(f"{candidate}/{question_id}: missing scores object")
        values: dict[str, float] = {}
        for dimension in SCORE_DIMENSIONS:
            score = raw.get(dimension)
            if not isinstance(score, int | float) or score < 0 or score > 2:
                raise EvalHarnessError(
                    f"{candidate}/{question_id}: score {dimension!r} must be 0..2"
                )
            values[dimension] = float(score)
        return cls(values=values)


@dataclass(frozen=True)
class JudgedAnswerRow:
    """Validated answer row ready for fixed-pack aggregation.

    Attributes
    ----------
    raw
        Original row dictionary. Preserved so downstream summary code keeps the
        stable JSONL contract.
    candidate
        Candidate label.
    question_id
        Fixed manifest question id.
    scores
        Validated rubric score set.
    """

    raw: dict[str, Any]
    candidate: str
    question_id: str
    scores: ScoreSet

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> JudgedAnswerRow:
        """Validate one raw answer row.

        Parameters
        ----------
        row
            Parsed JSONL row from a judged answer file.

        Returns
        -------
        JudgedAnswerRow
            Row wrapper with validated identifiers, score object, provenance,
            and hard-fail metadata.
        """

        candidate = _required_answer_label(row, "candidate", "answer row")
        question_id = _required_answer_label(row, "question_id", candidate)
        scores = ScoreSet.from_raw(row.get("scores"), candidate, question_id)
        _validate_judge_provenance(row, candidate, question_id)
        _validate_complete_judged_row(row, candidate, question_id)
        return cls(raw=row, candidate=candidate, question_id=question_id, scores=scores)


@dataclass(frozen=True)
class RunMetadata:
    """Reproducibility metadata attached to generated summaries.

    Attributes
    ----------
    manifest_sha256
        SHA-256 digest of the fixed eval manifest.
    answers_sha256
        SHA-256 digest of the judged answer rows used for aggregation.
    answer_key_sha256
        SHA-256 digest of the answer key, or ``not_provided``.
    git_commit
        Current repository commit when the report was generated.
    route_label
        Optional LiteLLM route or product route label.
    backend_label
        Optional serving backend label.
    runtime_inventory
        Optional ignored runtime-inventory artifact path.
    """

    manifest_sha256: str
    answers_sha256: str
    answer_key_sha256: str
    git_commit: str
    route_label: str
    backend_label: str
    runtime_inventory: str

    def as_summary_fields(self) -> dict[str, str]:
        """Return CSV-safe metadata fields for a summary row."""

        return {
            "manifest_sha256": self.manifest_sha256,
            "answers_sha256": self.answers_sha256,
            "answer_key_sha256": self.answer_key_sha256,
            "git_commit": self.git_commit,
            "route_label": self.route_label,
            "backend_label": self.backend_label,
            "runtime_inventory": self.runtime_inventory,
        }


@dataclass(frozen=True)
class RunMetadataInputs:
    """Input paths and labels used to build reproducibility metadata.

    Attributes
    ----------
    manifest_path
        Fixed eval manifest used for the run.
    answers_path
        Judged answer JSONL path used for aggregation.
    answer_key_path
        Optional answer-key path used by the human/executor judge.
    route_label
        Optional product route label.
    backend_label
        Optional backend label.
    runtime_inventory
        Optional ignored artifact path with non-secret runtime inventory.
    """

    manifest_path: Path
    answers_path: Path
    answer_key_path: Path | None
    route_label: str | None
    backend_label: str | None
    runtime_inventory: Path | None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise EvalHarnessError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise EvalHarnessError(f"{path}:{lineno}: JSONL row must be an object")
        rows.append(row)
    return rows


def load_manifest(path: Path) -> EvalManifest:
    """Load and validate a fixed scientific RAG manifest.

    Parameters
    ----------
    path
        JSONL manifest containing paper and question rows.

    Returns
    -------
    EvalManifest
        Validated manifest with stable paper keys and question ids.
    """

    papers: dict[str, EvalPaper] = {}
    questions: list[EvalQuestion] = []
    question_ids: set[str] = set()

    for row in _read_jsonl(path):
        row_type = row.get("type")
        if row_type == "paper":
            paper = EvalPaper(
                paper_key=_required_str(row, "paper_key"),
                identifier=_required_str(row, "identifier"),
                title=_required_str(row, "title"),
                expected_sections=_required_list(row, "expected_sections"),
                known_terms=_required_list(row, "known_terms"),
                forbidden_confusions=_required_list(row, "forbidden_confusions"),
            )
            if paper.paper_key in papers:
                raise EvalHarnessError(f"duplicate paper_key: {paper.paper_key}")
            papers[paper.paper_key] = paper
        elif row_type == "question":
            question = EvalQuestion(
                question_id=_required_str(row, "question_id"),
                question=_required_str(row, "question"),
                scope=_required_str(row, "scope"),
                required_papers=_required_list(row, "required_papers"),
                expected_evidence=_required_list(row, "expected_evidence"),
                scientific_traps=_required_list(row, "scientific_traps"),
                rubric_weights=_required_weights(row),
            )
            if question.question_id in question_ids:
                raise EvalHarnessError(f"duplicate question_id: {question.question_id}")
            question_ids.add(question.question_id)
            questions.append(question)
        else:
            raise EvalHarnessError(f"unknown manifest row type: {row_type!r}")

    manifest = EvalManifest(papers=papers, questions=questions)
    validate_manifest(manifest)
    return manifest


def _required_str(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalHarnessError(f"field {key!r} must be a non-empty string")
    return value


def _required_list(row: dict[str, Any], key: str) -> list[str]:
    value = row.get(key)
    if not isinstance(value, list) or not value:
        raise EvalHarnessError(f"field {key!r} must be a non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise EvalHarnessError(f"field {key!r} must contain non-empty strings")
    return list(value)


def _required_weights(row: dict[str, Any]) -> dict[str, float]:
    value = row.get("rubric_weights")
    if not isinstance(value, dict) or not value:
        raise EvalHarnessError("field 'rubric_weights' must be a non-empty object")
    weights: dict[str, float] = {}
    for dimension in SCORE_DIMENSIONS:
        raw = value.get(dimension, 0)
        if not isinstance(raw, int | float):
            raise EvalHarnessError(f"rubric weight {dimension!r} must be numeric")
        weights[dimension] = float(raw)
    if sum(weights.values()) <= 0:
        raise EvalHarnessError("rubric weights must have positive total weight")
    return weights


def validate_manifest(manifest: EvalManifest) -> None:
    """Validate minimum fixed-pack coverage and question references.

    Parameters
    ----------
    manifest
        Manifest to validate before dry-run, capture, or aggregation.
    """

    if len(manifest.papers) < MIN_PAPERS:
        raise EvalHarnessError(
            f"manifest has {len(manifest.papers)} paper rows; need at least {MIN_PAPERS}"
        )
    if len(manifest.questions) < MIN_QUESTIONS:
        raise EvalHarnessError(
            f"manifest has {len(manifest.questions)} questions; need at least {MIN_QUESTIONS}"
        )
    paper_keys = set(manifest.papers)
    category_counts: dict[str, int] = defaultdict(int)
    for question in manifest.questions:
        missing = sorted(set(question.required_papers) - paper_keys)
        if missing:
            raise EvalHarnessError(f"{question.question_id}: unknown required papers {missing}")
        if question.scope not in {"single_paper", "cross_paper", "unanswerable"}:
            raise EvalHarnessError(f"{question.question_id}: invalid scope {question.scope!r}")
        category_counts[question.scope] += 1
    if category_counts["unanswerable"] < 3:
        raise EvalHarnessError("manifest must include at least 3 unanswerable questions")
    if category_counts["cross_paper"] < 4:
        raise EvalHarnessError("manifest must include at least 4 cross-paper questions")


def write_dry_run_answers(
    manifest: EvalManifest,
    out_dir: Path,
    candidates: Sequence[str],
) -> Path:
    """Write deterministic fixture rows for CI-only parser validation.

    Parameters
    ----------
    manifest
        Fixed manifest used to generate one row per candidate and question.
    out_dir
        Directory receiving ``answers.jsonl``.
    candidates
        Candidate labels to include in the fixture rows.

    Returns
    -------
    Path
        Path to the generated JSONL answer fixture.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    answers_path = out_dir / "answers.jsonl"
    now_ms = int(time.time() * 1000)
    with answers_path.open("w", encoding="utf-8") as fh:
        for candidate in candidates:
            for index, question in enumerate(manifest.questions):
                scores = _dry_run_scores(question)
                answer = _dry_run_answer(manifest, question)
                row = {
                    "candidate": candidate,
                    "question_id": question.question_id,
                    "scope": question.scope,
                    "required_papers": question.required_papers,
                    "answer": answer,
                    "citations": [
                        {
                            "paper_key": key,
                            "identifier": manifest.papers[key].identifier,
                            "evidence": question.expected_evidence[0],
                        }
                        for key in question.required_papers
                    ],
                    "scores": scores,
                    "latency_ms": 50 + index,
                    "vram_peak_mb": "not_runnable:dry_run_fixture",
                    "structured_output_valid": True,
                    "wrong_paper_central_claim": False,
                    "unanswerable_fabricated_positive_claim": False,
                    "promotion_eligible": False,
                    "run_note": "dry_run_fixture_not_model_evidence",
                    "ts_ms": now_ms,
                }
                fh.write(json.dumps(row, sort_keys=True) + "\n")
    return answers_path


def _dry_run_scores(question: EvalQuestion) -> dict[str, int]:
    if question.scope == "unanswerable":
        return {
            "scientific_correctness": 2,
            "evidence_grounding": 2,
            "citation_label_stability": 2,
            "quantitative_fidelity": 1,
            "synthesis_quality": 1,
            "uncertainty_behavior": 2,
            "visible_answer_quality": 2,
        }
    return {dimension: 2 for dimension in SCORE_DIMENSIONS}


def _dry_run_answer(manifest: EvalManifest, question: EvalQuestion) -> str:
    titles = "; ".join(manifest.papers[key].title for key in question.required_papers)
    if question.scope == "unanswerable":
        return f"The fixed corpus does not contain evidence for this claim. Checked: {titles}."
    evidence = question.expected_evidence[0]
    return f"Dry-run fixture answer for {question.question_id}. Evidence anchor: {evidence}."


def load_raw_capture_rows(path: Path) -> list[dict[str, Any]]:
    """Load capture-only answer rows without treating them as judged evidence.

    Parameters
    ----------
    path
        JSONL file written by ``--capture-only``.

    Returns
    -------
    list[dict[str, Any]]
        Raw rows for pre-judging completeness checks.
    """

    return _read_jsonl(path)


def summarize_raw_capture_gate(
    manifest: EvalManifest,
    rows: Sequence[dict[str, Any]],
    *,
    paper_map: dict[str, int | str] | None = None,
    vram_csv: Path | None = None,
) -> RawCaptureGateResult:
    """Summarize whether raw capture rows are eligible for judging.

    Parameters
    ----------
    manifest
        Fixed question pack used to verify exact raw-row coverage.
    rows
        Raw capture rows, usually from ``raw_answers.jsonl``.
    paper_map
        Optional fixed-pack local paper-id map used to detect outside sources.
    vram_csv
        Optional GPU monitor CSV used to report peak observed VRAM.

    Returns
    -------
    RawCaptureGateResult
        Hard-gate summary for operator/reviewer intake.
    """

    manifest_questions = {question.question_id: question for question in manifest.questions}
    blocking_reasons: list[str] = []
    candidate_labels = sorted(
        {value for row in rows if isinstance(value := row.get("candidate"), str) and value}
    )
    question_coverage = _raw_question_coverage(rows, set(manifest_questions))
    http_status_counts = _string_count(row.get("http_status", "missing") for row in rows)
    empty_answer_count = sum(1 for row in rows if not str(row.get("answer", "")).strip())
    hidden_reasoning_leak_count = _raw_hidden_reasoning_leak_count(rows)
    retrieval_scope_counts = _string_count(row.get("retrieval_scope", "missing") for row in rows)
    missing_evidence_count = _raw_missing_evidence_count(rows)
    outside_count = _raw_outside_count(rows, paper_map)
    latency_ms = _raw_latency_summary(rows)
    vram_peak_mb = (
        _read_vram_peak_mb(vram_csv) if vram_csv is not None else "not_checked:no_vram_csv"
    )
    null_scores_count = sum(1 for row in rows if row.get("scores") is None)
    gate_metrics = {
        "row_count": len(rows),
        "candidate_labels": candidate_labels,
        "question_coverage": question_coverage,
        "http_status_counts": http_status_counts,
        "empty_answer_count": empty_answer_count,
        "hidden_reasoning_leak_count": hidden_reasoning_leak_count,
        "scope_blockers": _raw_scope_blockers(rows, manifest_questions),
        "missing_evidence_count": missing_evidence_count,
        "outside_count": outside_count,
        "latency_ms": latency_ms,
        "null_scores_count": null_scores_count,
    }
    blocking_reasons = _raw_gate_blocking_reasons(gate_metrics)

    return RawCaptureGateResult(
        row_count=len(rows),
        candidate_labels=candidate_labels,
        question_coverage=question_coverage,
        http_status_counts=http_status_counts,
        empty_answer_count=empty_answer_count,
        hidden_reasoning_leak_count=hidden_reasoning_leak_count,
        retrieval_scope_counts=retrieval_scope_counts,
        outside_fixed_pack_source_count=outside_count,
        latency_ms=latency_ms,
        vram_peak_mb=vram_peak_mb,
        null_scores_count=null_scores_count,
        eligible_for_judging=not blocking_reasons,
        blocking_reasons=sorted(set(blocking_reasons)),
    )


def write_raw_capture_gate(path: Path, result: RawCaptureGateResult) -> None:
    """Write raw capture gate output as stable JSON.

    Parameters
    ----------
    path
        Destination JSON path.
    result
        Gate result returned by ``summarize_raw_capture_gate``.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _raw_hidden_reasoning_leak_count(rows: Sequence[dict[str, Any]]) -> int:
    return sum(
        1
        for row in rows
        if any(marker in str(row.get("answer", "")).lower() for marker in HIDDEN_REASONING_MARKERS)
    )


def _raw_missing_evidence_count(rows: Sequence[dict[str, Any]]) -> int:
    return sum(1 for row in rows if not _has_non_empty_evidence_list(row))


def _raw_outside_count(
    rows: Sequence[dict[str, Any]], paper_map: dict[str, int | str] | None
) -> int | str:
    if paper_map is None:
        return "not_checked:no_paper_map"
    return _outside_fixed_pack_source_count(rows, paper_map)


def _raw_gate_blocking_reasons(metrics: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    checks = (
        (len(metrics["candidate_labels"]) != 1, "expected_exactly_one_candidate_label"),
        (
            _raw_coverage_has_errors(metrics["question_coverage"]),
            "fixed_question_coverage_mismatch",
        ),
        (set(metrics["http_status_counts"]) != {"200"}, "non_200_http_status"),
        (metrics["empty_answer_count"] > 0, "empty_visible_answer"),
        (metrics["hidden_reasoning_leak_count"] > 0, "visible_hidden_reasoning_or_control_token"),
        (metrics["missing_evidence_count"] > 0, "missing_source_or_citation_evidence"),
        (_raw_has_outside_sources(metrics["outside_count"]), "outside_fixed_pack_source"),
        (metrics["latency_ms"].get("missing_or_invalid_count") != 0, "missing_or_invalid_latency"),
        (metrics["null_scores_count"] != metrics["row_count"], "raw_rows_must_have_scores_null"),
    )
    reasons.extend(reason for failed, reason in checks if failed)
    reasons.extend(metrics["scope_blockers"])
    return sorted(set(reasons))


def _raw_coverage_has_errors(question_coverage: dict[str, Any]) -> bool:
    return bool(
        question_coverage["missing"]
        or question_coverage["unknown"]
        or question_coverage["duplicate"]
    )


def _raw_has_outside_sources(outside_count: int | str) -> bool:
    return outside_count != "not_checked:no_paper_map" and bool(outside_count)


def _raw_question_coverage(
    rows: Sequence[dict[str, Any]], manifest_question_ids: set[str]
) -> dict[str, Any]:
    question_ids = [
        row.get("question_id") for row in rows if isinstance(row.get("question_id"), str)
    ]
    counts = Counter(question_ids)
    seen = set(counts)
    return {
        "expected_count": len(manifest_question_ids),
        "seen_count": len(seen),
        "missing": sorted(manifest_question_ids - seen),
        "unknown": sorted(seen - manifest_question_ids),
        "duplicate": sorted(question_id for question_id, count in counts.items() if count > 1),
    }


def _string_count(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _raw_scope_blockers(
    rows: Sequence[dict[str, Any]], manifest_questions: dict[str, EvalQuestion]
) -> list[str]:
    blockers: list[str] = []
    for row in rows:
        question_id = row.get("question_id")
        if not isinstance(question_id, str) or question_id not in manifest_questions:
            continue
        question = manifest_questions[question_id]
        if row.get("scope") != question.scope:
            blockers.append("scope_mismatch")
        if row.get("required_papers") != question.required_papers:
            blockers.append("required_papers_mismatch")
        if question.scope == "single_paper":
            if row.get("retrieval_scope") != "single_paper_endpoint":
                blockers.append("single_paper_scope_mismatch")
            continue
        if row.get("retrieval_scope") != "fixed_pack_isolated_library":
            blockers.append("library_scope_mismatch")
        if row.get("fixed_pack_library_confirmed") is not True:
            blockers.append("fixed_pack_library_not_confirmed")
    return blockers


def _outside_fixed_pack_source_count(
    rows: Sequence[dict[str, Any]], paper_map: dict[str, int | str]
) -> int:
    allowed_ids = _paper_map_ids(paper_map)
    outside_count = 0
    for row in rows:
        for item in _evidence_items(row):
            source_id = _source_paper_id(item)
            if source_id is not None and source_id not in allowed_ids:
                outside_count += 1
    return outside_count


def _paper_map_ids(paper_map: dict[str, int | str]) -> set[int]:
    ids: set[int] = set()
    for paper_key, raw_id in paper_map.items():
        try:
            ids.add(int(raw_id))
        except (TypeError, ValueError) as exc:
            raise EvalHarnessError(f"paper map value for {paper_key!r} must be an int") from exc
    return ids


def _source_paper_id(item: Any) -> int | None:
    if not isinstance(item, dict):
        return None
    raw_id = item.get("paper_id")
    if raw_id is None:
        return None
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def _raw_latency_summary(rows: Sequence[dict[str, Any]]) -> dict[str, float | int | str]:
    latencies: list[float] = []
    missing_or_invalid_count = 0
    for row in rows:
        latency = row.get("latency_ms")
        if isinstance(latency, int | float) and latency >= 0:
            latencies.append(float(latency))
        else:
            missing_or_invalid_count += 1
    if not latencies:
        return {
            "count": 0,
            "missing_or_invalid_count": missing_or_invalid_count,
            "min": "not_runnable:no_latency",
            "median": "not_runnable:no_latency",
            "p95": "not_runnable:no_latency",
            "max": "not_runnable:no_latency",
        }
    return {
        "count": len(latencies),
        "missing_or_invalid_count": missing_or_invalid_count,
        "min": round(min(latencies), 2),
        "median": round(statistics.median(latencies), 2),
        "p95": _p95(latencies),
        "max": round(max(latencies), 2),
    }


def _read_vram_peak_mb(path: Path) -> float | str:
    if not path.exists():
        return "not_checked:vram_csv_missing"
    values: list[float] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return "not_checked:vram_csv_empty"
        preferred_fields = [
            field
            for field in reader.fieldnames
            if "vram" in field.lower() or "memory" in field.lower()
        ]
        fields = preferred_fields or reader.fieldnames
        for row in reader:
            for field in fields:
                raw_value = row.get(field)
                if raw_value is None:
                    continue
                try:
                    values.append(float(raw_value))
                except ValueError:
                    continue
    if not values:
        return "not_checked:no_numeric_vram"
    return round(max(values), 2)


def load_answer_rows(path: Path) -> list[dict[str, Any]]:
    """Load judged answer rows and reject incomplete benchmark evidence.

    Parameters
    ----------
    path
        JSONL file containing one judged row per candidate and question.

    Returns
    -------
    list[dict[str, Any]]
        Validated answer rows ready for exact-coverage aggregation.
    """

    rows = _read_jsonl(path)
    return [JudgedAnswerRow.from_mapping(row).raw for row in rows]


def _required_answer_label(row: dict[str, Any], key: str, owner: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        if key == "candidate":
            raise EvalHarnessError("answer row missing non-empty candidate")
        raise EvalHarnessError(f"{owner}: answer row missing non-empty {key}")
    return value


def _validate_judge_provenance(row: dict[str, Any], candidate: str, question_id: str) -> None:
    if row.get("run_note") == "dry_run_fixture_not_model_evidence":
        raise EvalHarnessError(
            f"{candidate}/{question_id}: dry-run rows are not benchmark evidence"
        )
    if row.get("judge_reviewed") is not True:
        raise EvalHarnessError(f"{candidate}/{question_id}: missing judge_reviewed=true")
    judge_type = row.get("judge_type")
    if judge_type not in {"executor", "owner", "human"}:
        raise EvalHarnessError(
            f"{candidate}/{question_id}: judge_type must be executor, owner, or human"
        )


def _validate_complete_judged_row(row: dict[str, Any], candidate: str, question_id: str) -> None:
    if row.get("run_note") == "dry_run_fixture_not_model_evidence":
        return
    _validate_visible_answer(row, candidate, question_id)
    _validate_evidence_presence(row, candidate, question_id)
    _validate_required_boolean_fields(row, candidate, question_id)
    _validate_latency_value(row, candidate, question_id)
    _validate_vram_value(row, candidate, question_id)


def _validate_visible_answer(row: dict[str, Any], candidate: str, question_id: str) -> None:
    answer = str(row.get("answer", ""))
    if not answer.strip():
        raise EvalHarnessError(f"{candidate}/{question_id}: missing visible answer")


def _validate_evidence_presence(row: dict[str, Any], candidate: str, question_id: str) -> None:
    if not _has_non_empty_evidence_list(row):
        raise EvalHarnessError(
            f"{candidate}/{question_id}: missing non-empty citations or sources list"
        )


def _validate_required_boolean_fields(
    row: dict[str, Any], candidate: str, question_id: str
) -> None:
    for field_name in REQUIRED_JUDGED_BOOL_FIELDS:
        if not isinstance(row.get(field_name), bool):
            raise EvalHarnessError(f"{candidate}/{question_id}: {field_name} must be boolean")


def _validate_latency_value(row: dict[str, Any], candidate: str, question_id: str) -> None:
    latency = row.get("latency_ms")
    if not isinstance(latency, int | float) or latency < 0:
        raise EvalHarnessError(
            f"{candidate}/{question_id}: latency_ms must be a non-negative number"
        )


def _has_non_empty_evidence_list(row: dict[str, Any]) -> bool:
    return any(_evidence_item_has_content(item) for item in _evidence_items(row))


def _evidence_item_has_content(item: Any) -> bool:
    if isinstance(item, str):
        return bool(item.strip())
    if not isinstance(item, dict):
        return False
    evidence_keys = (
        "evidence",
        "text",
        "snippet",
        "quote",
        "content",
    )
    for key in evidence_keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, int) and not isinstance(value, bool):
            return True
    return False


def _validate_vram_value(row: dict[str, Any], candidate: str, question_id: str) -> None:
    vram_peak_mb = row.get("vram_peak_mb")
    if not isinstance(vram_peak_mb, int | float) or vram_peak_mb < 0:
        raise EvalHarnessError(
            f"{candidate}/{question_id}: vram_peak_mb must be a non-negative number"
        )


def summarize_answers(
    manifest: EvalManifest,
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate complete judged rows into candidate summary decisions.

    Parameters
    ----------
    manifest
        Fixed question pack used to enforce exact candidate coverage.
    rows
        Judged rows returned by ``load_answer_rows``.

    Returns
    -------
    list[dict[str, Any]]
        One summary row per candidate.
    """

    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_candidate[row["candidate"]].append(row)

    manifest_questions = {question.question_id: question for question in manifest.questions}
    manifest_question_ids = set(manifest_questions)
    summaries: list[dict[str, Any]] = []
    for candidate, candidate_rows in sorted(by_candidate.items()):
        _validate_candidate_question_coverage(candidate, candidate_rows, manifest_question_ids)
        _validate_fixed_pack_scope(candidate, candidate_rows, manifest_questions)
        stats = _collect_candidate_stats(manifest, candidate_rows)
        summaries.append(_summary_row(candidate, candidate_rows, stats))
    return summaries


def _validate_fixed_pack_scope(
    candidate: str,
    rows: Sequence[dict[str, Any]],
    manifest_questions: dict[str, EvalQuestion],
) -> None:
    for row in rows:
        question = manifest_questions[row["question_id"]]
        row_scope = row.get("scope")
        if row_scope is not None and row_scope != question.scope:
            raise EvalHarnessError(
                f"{candidate}/{question.question_id}: row scope {row_scope!r} "
                "does not match manifest"
            )
        row_required = row.get("required_papers")
        if row_required is not None and list(row_required) != question.required_papers:
            raise EvalHarnessError(
                f"{candidate}/{question.question_id}: required_papers do not match manifest"
            )
        if question.scope == "single_paper":
            if row.get("retrieval_scope") != "single_paper_endpoint":
                raise EvalHarnessError(
                    f"{candidate}/{question.question_id}: single-paper row missing endpoint scope"
                )
            _validate_single_paper_evidence(candidate, row, question)
            continue
        if row.get("retrieval_scope") != "fixed_pack_isolated_library":
            raise EvalHarnessError(
                f"{candidate}/{question.question_id}: "
                "library-wide row missing fixed-pack retrieval_scope"
            )
        if row.get("fixed_pack_library_confirmed") is not True:
            raise EvalHarnessError(
                f"{candidate}/{question.question_id}: fixed_pack_library_confirmed must be true"
            )


def _validate_single_paper_evidence(
    candidate: str, row: dict[str, Any], question: EvalQuestion
) -> None:
    expected_paper = question.required_papers[0]
    for item in _evidence_items(row):
        paper_key = item.get("paper_key") if isinstance(item, dict) else None
        if paper_key is not None and paper_key != expected_paper:
            raise EvalHarnessError(
                f"{candidate}/{question.question_id}: evidence paper_key {paper_key!r} "
                f"does not match {expected_paper!r}"
            )


def _evidence_items(row: dict[str, Any]) -> list[Any]:
    items: list[Any] = []
    for field_name in ("citations", "sources"):
        value = row.get(field_name)
        if isinstance(value, list):
            items.extend(value)
    return items


def _validate_candidate_question_coverage(
    candidate: str,
    rows: Sequence[dict[str, Any]],
    manifest_question_ids: set[str],
) -> None:
    seen_question_ids: set[str] = set()
    duplicate_question_ids: set[str] = set()
    for row in rows:
        question_id = row["question_id"]
        if question_id in seen_question_ids:
            duplicate_question_ids.add(question_id)
        seen_question_ids.add(question_id)

    unknown_question_ids = seen_question_ids - manifest_question_ids
    missing_question_ids = manifest_question_ids - seen_question_ids
    if not (unknown_question_ids or duplicate_question_ids or missing_question_ids):
        return

    details = []
    if unknown_question_ids:
        details.append(f"unknown={sorted(unknown_question_ids)}")
    if duplicate_question_ids:
        details.append(f"duplicate={sorted(duplicate_question_ids)}")
    if missing_question_ids:
        details.append(f"missing={sorted(missing_question_ids)}")
    detail_text = "; ".join(details)
    raise EvalHarnessError(f"{candidate}: incomplete fixed-question coverage ({detail_text})")


def _collect_candidate_stats(
    manifest: EvalManifest,
    rows: Sequence[dict[str, Any]],
) -> CandidateStats:
    stats = CandidateStats()
    for row in rows:
        _update_stats_from_answer(stats, manifest, row)
    return stats


def _update_stats_from_answer(
    stats: CandidateStats,
    manifest: EvalManifest,
    row: dict[str, Any],
) -> None:
    answer = str(row.get("answer", ""))
    lowered = answer.lower()
    stats.empty_count += int(not answer.strip())
    stats.hidden_count += int(any(marker in lowered for marker in HIDDEN_REASONING_MARKERS))
    stats.wrong_paper_count += int(bool(row.get("wrong_paper_central_claim")))
    stats.unanswerable_fabrication_count += int(
        bool(row.get("unanswerable_fabricated_positive_claim"))
    )
    stats.structured_fail_count += int(row.get("structured_output_valid") is False)
    stats.promotion_eligible = (
        stats.promotion_eligible and row.get("promotion_eligible") is not False
    )

    latency = row.get("latency_ms")
    if isinstance(latency, int | float):
        stats.latencies.append(float(latency))

    scores = {dimension: float(row["scores"].get(dimension, 0)) for dimension in SCORE_DIMENSIONS}
    stats.quality_scores.append(_weighted_pct(scores, manifest, row["question_id"]))
    stats.grounding_scores.append(sum(scores[d] for d in GROUNDING_DIMENSIONS) / 4 * 100)


def _summary_row(
    candidate: str,
    rows: Sequence[dict[str, Any]],
    stats: CandidateStats,
) -> dict[str, Any]:
    return {
        "candidate": candidate,
        "hardware_tier": _first_value(rows, "hardware_tier", "unspecified"),
        "quality_score": round(statistics.fmean(stats.quality_scores), 2),
        "grounding_score": round(statistics.fmean(stats.grounding_scores), 2),
        "wrong_paper_count": stats.wrong_paper_count,
        "empty_answer_count": stats.empty_count,
        "hidden_reasoning_leak_count": stats.hidden_count,
        "p95_latency_ms": _p95(stats.latencies) if stats.latencies else "not_runnable:no_latency",
        "vram_peak_mb": _first_value(rows, "vram_peak_mb", "not_runnable:no_vram"),
        "decision": _candidate_decision(stats),
    }


def _candidate_decision(stats: CandidateStats) -> str:
    if any(
        [
            stats.hidden_count > 0,
            stats.empty_count > 1,
            stats.wrong_paper_count > 2,
            stats.unanswerable_fabrication_count > 0,
            stats.structured_fail_count > 0,
        ]
    ):
        return "reject"
    if not stats.promotion_eligible:
        return "defer:not_promotion_eligible"
    return "defer"


def _weighted_pct(
    scores: dict[str, float],
    manifest: EvalManifest,
    question_id: str,
) -> float:
    weights = next(q.rubric_weights for q in manifest.questions if q.question_id == question_id)
    numerator = sum(scores[dimension] * weights[dimension] for dimension in SCORE_DIMENSIONS)
    denominator = 2 * sum(weights.values())
    return 0.0 if denominator <= 0 else numerator / denominator * 100


def _p95(values: Sequence[float]) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return round(ordered[index], 2)


def _first_value(rows: Sequence[dict[str, Any]], key: str, default: Any) -> Any:
    for row in rows:
        value = row.get(key)
        if value is not None:
            return value
    return default


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file.

    Parameters
    ----------
    path
        File to hash.

    Returns
    -------
    str
        Lowercase hexadecimal digest.
    """

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_git_commit(repo_root: Path | None = None) -> str:
    """Return the current git commit hash or ``unknown``.

    Parameters
    ----------
    repo_root
        Repository root used for the git command. Defaults to the harness repo.

    Returns
    -------
    str
        Current commit hash when available.
    """

    cwd = repo_root or Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def build_run_metadata(inputs: RunMetadataInputs) -> RunMetadata:
    """Build reproducibility metadata for generated summaries.

    Parameters
    ----------
    inputs
        Input paths and labels for this run.

    Returns
    -------
    RunMetadata
        Metadata to include in CSV and Markdown summaries.
    """

    answer_key_hash = "not_provided"
    if inputs.answer_key_path is not None and inputs.answer_key_path.exists():
        answer_key_hash = sha256_file(inputs.answer_key_path)
    return RunMetadata(
        manifest_sha256=sha256_file(inputs.manifest_path),
        answers_sha256=sha256_file(inputs.answers_path),
        answer_key_sha256=answer_key_hash,
        git_commit=current_git_commit(),
        route_label=inputs.route_label or "unspecified",
        backend_label=inputs.backend_label or "unspecified",
        runtime_inventory=(
            str(inputs.runtime_inventory)
            if inputs.runtime_inventory is not None
            else "not_provided"
        ),
    )


def write_dry_run_report(
    path: Path, manifest: EvalManifest, manifest_path: Path, answers_path: Path
) -> None:
    """Write a non-benchmark dry-run report.

    Parameters
    ----------
    path
        Destination Markdown path.
    manifest
        Manifest used to report fixed-pack coverage.
    manifest_path
        Fixed eval manifest path.
    answers_path
        Fixture answer-row path written by dry-run mode.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Scientific RAG Eval Dry Run",
        "",
        "Dry-run fixture rows validate manifest and parser plumbing only.",
        "They are not benchmark evidence and cannot be aggregated via --answers-jsonl.",
        "",
        f"- Papers: {len(manifest.papers)}",
        f"- Questions: {len(manifest.questions)}",
        f"- Fixture rows: `{answers_path}`",
        f"- Manifest SHA-256: `{sha256_file(manifest_path)}`",
        f"- Fixture SHA-256: `{sha256_file(answers_path)}`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_summary_csv(
    path: Path, summaries: Sequence[dict[str, Any]], metadata: RunMetadata | None = None
) -> None:
    """Write candidate summary rows as CSV.

    Parameters
    ----------
    path
        Destination CSV path.
    summaries
        Candidate summary dictionaries from ``summarize_answers``.
    metadata
        Optional reproducibility metadata to append to each CSV row.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "candidate",
        "hardware_tier",
        "quality_score",
        "grounding_score",
        "wrong_paper_count",
        "empty_answer_count",
        "hidden_reasoning_leak_count",
        "p95_latency_ms",
        "vram_peak_mb",
        "decision",
    ]
    if metadata is not None:
        fieldnames.extend(metadata.as_summary_fields())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in summaries:
            output_row = dict(row)
            if metadata is not None:
                output_row.update(metadata.as_summary_fields())
            writer.writerow(output_row)


def write_markdown_report(
    path: Path,
    manifest: EvalManifest,
    summaries: Sequence[dict[str, Any]],
    answers_path: Path,
    metadata: RunMetadata | None = None,
) -> None:
    """Write a human-readable benchmark summary report.

    Parameters
    ----------
    path
        Destination Markdown path.
    manifest
        Manifest used to report paper and question counts.
    summaries
        Candidate summary dictionaries from ``summarize_answers``.
    answers_path
        Source answer-row path referenced in the report.
    metadata
        Optional reproducibility metadata to include in the report.
    """

    lines = [
        "# Local Model Retrieval Eval Report",
        "",
        "This report is generated from fixed manifest inputs and answer rows.",
        "Dry-run fixture rows are parser tests only and are not promotion evidence.",
        "",
        f"- Papers: {len(manifest.papers)}",
        f"- Questions: {len(manifest.questions)}",
        f"- Answer rows: `{answers_path}`",
    ]
    if metadata is not None:
        lines.extend(
            [
                f"- Manifest SHA-256: `{metadata.manifest_sha256}`",
                f"- Answers SHA-256: `{metadata.answers_sha256}`",
                f"- Answer key SHA-256: `{metadata.answer_key_sha256}`",
                f"- Git commit: `{metadata.git_commit}`",
                f"- Route label: `{metadata.route_label}`",
                f"- Backend label: `{metadata.backend_label}`",
                f"- Runtime inventory: `{metadata.runtime_inventory}`",
            ]
        )
    lines.extend(
        [
            "",
            "| candidate | quality_score | grounding_score | wrong_paper_count | "
            "empty_answer_count | hidden_reasoning_leak_count | p95_latency_ms | decision |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in summaries:
        lines.append(
            "| {candidate} | {quality_score} | {grounding_score} | {wrong_paper_count} | "
            "{empty_answer_count} | {hidden_reasoning_leak_count} | {p95_latency_ms} | "
            "{decision} |".format(**row)
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def load_paper_map(path: Path) -> dict[str, int | str]:
    """Load the local mapping from manifest paper keys to product paper ids.

    Parameters
    ----------
    path
        Ignored JSON file keyed by manifest ``paper_key``.

    Returns
    -------
    dict[str, int | str]
        Paper id mapping used by product RAG capture mode.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalHarnessError(f"invalid paper map JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise EvalHarnessError("paper map must be a JSON object keyed by paper_key")
    paper_map: dict[str, int | str] = {}
    for paper_key, paper_id in raw.items():
        if not isinstance(paper_key, str) or not paper_key:
            raise EvalHarnessError("paper map keys must be non-empty strings")
        if not isinstance(paper_id, int | str) or not str(paper_id):
            raise EvalHarnessError(f"paper map value for {paper_key!r} must be an id")
        paper_map[paper_key] = paper_id
    return paper_map


def capture_product_rag_answers(
    manifest: EvalManifest,
    out_dir: Path,
    config: CaptureConfig,
) -> Path:
    """Capture raw product RAG answers without judging or aggregation.

    Parameters
    ----------
    manifest
        Fixed question pack to run against the product API.
    out_dir
        Directory receiving ``raw_answers.jsonl``.
    config
        Runtime capture settings and fixed-pack scope confirmation.

    Returns
    -------
    Path
        Path to the raw capture JSONL file.
    """

    _validate_capture_manifest_scope(manifest, config)
    out_dir.mkdir(parents=True, exist_ok=True)
    answers_path = out_dir / "raw_answers.jsonl"
    headers = _capture_headers(api_key=config.api_key, auth_cookie_file=config.auth_cookie_file)

    with answers_path.open("w", encoding="utf-8") as fh:
        for question in manifest.questions:
            row = _capture_question_row(question, config=config, headers=headers)
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return answers_path


def _validate_capture_manifest_scope(manifest: EvalManifest, config: CaptureConfig) -> None:
    if config.fixed_pack_library_confirmed:
        return
    library_wide_question = next((q for q in manifest.questions if q.scope != "single_paper"), None)
    if library_wide_question is not None:
        raise EvalHarnessError(
            f"{library_wide_question.question_id}: library-wide capture requires "
            "--fixed-pack-library-confirmed"
        )


def _capture_headers(*, api_key: str | None, auth_cookie_file: Path | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    if auth_cookie_file:
        headers["Cookie"] = _read_cookie_header(auth_cookie_file)
    return headers


def _read_cookie_header(path: Path) -> str:
    jar = MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except (LoadError, OSError):
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise EvalHarnessError(f"auth cookie file is empty: {path}")
        return raw
    cookies = [f"{cookie.name}={cookie.value}" for cookie in jar]
    if not cookies:
        raise EvalHarnessError(f"auth cookie file has no cookies: {path}")
    return "; ".join(cookies)


def _capture_question_row(
    question: EvalQuestion,
    *,
    config: CaptureConfig,
    headers: dict[str, str],
) -> dict[str, Any]:
    path, payload = _product_rag_request(
        question,
        config.paper_map,
        config.max_chunks,
        config.max_papers,
        config.decompose,
    )
    started = time.perf_counter()
    status, body = _post_json(config.api_base.rstrip("/") + path, payload, headers, config.timeout)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "candidate": config.candidate,
        "question_id": question.question_id,
        "scope": question.scope,
        "required_papers": question.required_papers,
        "answer": str(body.get("answer", "")) if isinstance(body, dict) else "",
        "sources": body.get("sources", []) if isinstance(body, dict) else [],
        "http_status": status,
        "latency_ms": latency_ms,
        "backend_metadata": _backend_metadata(body),
        "retrieval_scope": _capture_retrieval_scope(question, config),
        "fixed_pack_library_confirmed": config.fixed_pack_library_confirmed,
        "scores": None,
        "promotion_eligible": False,
        "run_note": "capture_only_needs_judging",
        "ts_ms": int(time.time() * 1000),
    }


def _capture_retrieval_scope(question: EvalQuestion, config: CaptureConfig) -> str:
    if question.scope == "single_paper":
        return "single_paper_endpoint"
    if not config.fixed_pack_library_confirmed:
        raise EvalHarnessError(
            f"{question.question_id}: library-wide capture requires --fixed-pack-library-confirmed"
        )
    return "fixed_pack_isolated_library"


def _product_rag_request(
    question: EvalQuestion,
    paper_map: dict[str, int | str],
    max_chunks: int,
    max_papers: int,
    decompose: bool,
) -> tuple[str, dict[str, Any]]:
    if question.scope == "single_paper":
        paper_key = question.required_papers[0]
        if paper_key not in paper_map:
            raise EvalHarnessError(f"{question.question_id}: paper map missing {paper_key!r}")
        return (
            f"/api/papers/{paper_map[paper_key]}/ask",
            {"question": question.question, "max_chunks": max_chunks},
        )
    return (
        "/api/ask",
        {
            "question": question.question,
            "max_chunks": max_chunks,
            "max_papers": max_papers,
            "decompose": decompose,
            "paper_ids": _fixed_pack_paper_ids(paper_map),
        },
    )


def _fixed_pack_paper_ids(paper_map: dict[str, int | str]) -> list[int]:
    """Return local fixed-pack paper ids for explicit cross-paper scoping."""
    paper_ids: list[int] = []
    for paper_key, raw_id in sorted(paper_map.items()):
        try:
            paper_ids.append(int(raw_id))
        except (TypeError, ValueError) as exc:
            raise EvalHarnessError(f"paper map value for {paper_key!r} must be an int") from exc
    return paper_ids


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = _decode_json_body(exc.read())
        if isinstance(body, dict):
            return int(exc.code), body
        return int(exc.code), {"error": str(exc)}
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EvalHarnessError(f"product RAG capture failed: {exc}") from exc


def _decode_json_body(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _backend_metadata(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    return {
        key: body[key]
        for key in ("confidence", "verified_fraction", "model", "route", "backend")
        if key in body
    }


def call_chat_endpoint(api_base: str, model: str, question: EvalQuestion, timeout: float) -> str:
    """Call an OpenAI-compatible chat endpoint for a single question.

    Parameters
    ----------
    api_base
        Base URL exposing ``/v1/chat/completions``.
    model
        Model name sent in the chat-completions payload.
    question
        Fixed benchmark question to ask.
    timeout
        Request timeout in seconds.

    Returns
    -------
    str
        Visible assistant content from the endpoint response.
    """

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": question.question,
                }
            ],
        }
    ).encode("utf-8")
    req = request.Request(
        api_base.rstrip("/") + "/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EvalHarnessError(f"endpoint call failed for {model}: {exc}") from exc
    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise EvalHarnessError("endpoint response did not match chat-completions shape") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for dry-run, capture, or aggregation mode.

    Parameters
    ----------
    argv
        Optional argument list, primarily supplied by tests.

    Returns
    -------
    argparse.Namespace
        Parsed CLI arguments.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl"),
    )
    parser.add_argument("--answer-key", type=Path, default=ANSWER_KEY_DEFAULT)
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/perf/llm-retrieval-eval"))
    parser.add_argument("--candidate", action="append", default=[])
    parser.add_argument("--answers-jsonl", type=Path)
    parser.add_argument("--raw-gate-jsonl", type=Path)
    parser.add_argument("--raw-gate-output", type=Path)
    parser.add_argument("--vram-csv", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--api-base")
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--paper-map-json", type=Path)
    parser.add_argument("--auth-cookie-file", type=Path)
    parser.add_argument("--api-key")
    parser.add_argument("--max-chunks", type=int, default=8)
    parser.add_argument("--max-papers", type=int, default=10)
    parser.add_argument("--decompose", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--route-label")
    parser.add_argument("--backend-label")
    parser.add_argument("--runtime-inventory", type=Path)
    parser.add_argument(
        "--fixed-pack-library-confirmed",
        action="store_true",
        help="Confirm the authenticated library contains only the fixed benchmark paper pack.",
    )
    return parser.parse_args(argv)


def _validate_mode_selection(args: argparse.Namespace) -> None:
    modes = [
        bool(args.dry_run),
        bool(args.answers_jsonl),
        bool(args.capture_only),
        bool(args.raw_gate_jsonl),
    ]
    if sum(modes) != 1:
        raise EvalHarnessError(
            "choose exactly one mode: --dry-run, --answers-jsonl, --capture-only, "
            "or --raw-gate-jsonl"
        )
    if args.api_base and not args.capture_only:
        raise EvalHarnessError("--api-base is only valid with --capture-only")


def _run_dry_mode(args: argparse.Namespace, manifest: EvalManifest) -> int:
    candidates = args.candidate or ["dry-run-fixture"]
    answers_path = write_dry_run_answers(manifest, args.out_dir, candidates)
    write_dry_run_report(args.out_dir / "dry_run_report.md", manifest, args.manifest, answers_path)
    return 0


def _run_capture_mode(args: argparse.Namespace, manifest: EvalManifest) -> int:
    if not args.api_base:
        raise EvalHarnessError("--capture-only requires --api-base")
    if not args.candidate or len(args.candidate) != 1:
        raise EvalHarnessError("--capture-only requires exactly one --candidate")
    if not args.paper_map_json:
        raise EvalHarnessError("--capture-only requires --paper-map-json")
    if not (args.auth_cookie_file or args.api_key):
        raise EvalHarnessError("--capture-only requires --auth-cookie-file or --api-key")
    capture_product_rag_answers(
        manifest,
        args.out_dir,
        CaptureConfig(
            api_base=args.api_base,
            candidate=args.candidate[0],
            paper_map=load_paper_map(args.paper_map_json),
            api_key=args.api_key,
            auth_cookie_file=args.auth_cookie_file,
            timeout=args.timeout,
            max_chunks=args.max_chunks,
            max_papers=args.max_papers,
            decompose=args.decompose,
            fixed_pack_library_confirmed=args.fixed_pack_library_confirmed,
        ),
    )
    return 0


def _run_raw_gate_mode(args: argparse.Namespace, manifest: EvalManifest) -> int:
    paper_map = load_paper_map(args.paper_map_json) if args.paper_map_json else None
    result = summarize_raw_capture_gate(
        manifest,
        load_raw_capture_rows(args.raw_gate_jsonl),
        paper_map=paper_map,
        vram_csv=args.vram_csv,
    )
    output_path = args.raw_gate_output or args.out_dir / "raw_gate.json"
    write_raw_capture_gate(output_path, result)
    return 0


def _run_aggregation_mode(args: argparse.Namespace, manifest: EvalManifest) -> int:
    answer_rows = load_answer_rows(args.answers_jsonl)
    summaries = summarize_answers(manifest, answer_rows)
    metadata = build_run_metadata(
        RunMetadataInputs(
            manifest_path=args.manifest,
            answers_path=args.answers_jsonl,
            answer_key_path=args.answer_key,
            route_label=args.route_label,
            backend_label=args.backend_label,
            runtime_inventory=args.runtime_inventory,
        )
    )
    write_summary_csv(args.out_dir / "summary.csv", summaries, metadata)
    write_markdown_report(
        args.out_dir / "report.md", manifest, summaries, args.answers_jsonl, metadata
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one benchmark harness mode.

    Parameters
    ----------
    argv
        Optional argument list, primarily supplied by tests.

    Returns
    -------
    int
        Process-style exit code.
    """

    args = parse_args(argv)
    manifest = load_manifest(args.manifest)
    _validate_mode_selection(args)
    if args.dry_run:
        return _run_dry_mode(args, manifest)
    if args.capture_only:
        return _run_capture_mode(args, manifest)
    if args.raw_gate_jsonl:
        return _run_raw_gate_mode(args, manifest)
    return _run_aggregation_mode(args, manifest)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalHarnessError as exc:
        print(f"llm_retrieval_eval: {exc}")
        raise SystemExit(2) from exc
