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
import json
import math
import statistics
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
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
HIDDEN_REASONING_MARKERS = ("<think>", "</think>", "harmony analysis", "scratchpad")


class EvalHarnessError(RuntimeError):
    """Raised for manifest, answer, or execution errors."""


@dataclass(frozen=True)
class EvalPaper:
    paper_key: str
    identifier: str
    title: str
    expected_sections: list[str]
    known_terms: list[str]
    forbidden_confusions: list[str]


@dataclass(frozen=True)
class EvalQuestion:
    question_id: str
    question: str
    scope: str
    required_papers: list[str]
    expected_evidence: list[str]
    scientific_traps: list[str]
    rubric_weights: dict[str, float]


@dataclass(frozen=True)
class EvalManifest:
    papers: dict[str, EvalPaper]
    questions: list[EvalQuestion]


@dataclass
class CandidateStats:
    empty_count: int = 0
    hidden_count: int = 0
    wrong_paper_count: int = 0
    unanswerable_fabrication_count: int = 0
    structured_fail_count: int = 0
    promotion_eligible: bool = True
    latencies: list[float] = field(default_factory=list)
    quality_scores: list[float] = field(default_factory=list)
    grounding_scores: list[float] = field(default_factory=list)


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


def load_answer_rows(path: Path) -> list[dict[str, Any]]:
    rows = _read_jsonl(path)
    for row in rows:
        candidate = row.get("candidate")
        question_id = row.get("question_id")
        if not isinstance(candidate, str) or not candidate:
            raise EvalHarnessError("answer row missing non-empty candidate")
        if not isinstance(question_id, str) or not question_id:
            raise EvalHarnessError("answer row missing non-empty question_id")
        scores = row.get("scores")
        if not isinstance(scores, dict):
            raise EvalHarnessError(f"{candidate}/{question_id}: missing scores object")
        for dimension in SCORE_DIMENSIONS:
            score = scores.get(dimension)
            if not isinstance(score, int | float) or score < 0 or score > 2:
                raise EvalHarnessError(
                    f"{candidate}/{question_id}: score {dimension!r} must be 0..2"
                )
    return rows


def summarize_answers(
    manifest: EvalManifest,
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_candidate[row["candidate"]].append(row)

    manifest_question_ids = {question.question_id for question in manifest.questions}
    summaries: list[dict[str, Any]] = []
    for candidate, candidate_rows in sorted(by_candidate.items()):
        _validate_candidate_question_coverage(candidate, candidate_rows, manifest_question_ids)
        stats = _collect_candidate_stats(manifest, candidate_rows)
        summaries.append(_summary_row(candidate, candidate_rows, stats))
    return summaries


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
        return "not_runnable:dry_run_fixture"
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


def write_summary_csv(path: Path, summaries: Sequence[dict[str, Any]]) -> None:
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
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in summaries:
            writer.writerow(row)


def write_markdown_report(
    path: Path,
    manifest: EvalManifest,
    summaries: Sequence[dict[str, Any]],
    answers_path: Path,
) -> None:
    lines = [
        "# Local Model Retrieval Eval Report",
        "",
        "This report is generated from fixed manifest inputs and answer rows.",
        "Dry-run fixture rows are parser tests only and are not promotion evidence.",
        "",
        f"- Papers: {len(manifest.papers)}",
        f"- Questions: {len(manifest.questions)}",
        f"- Answer rows: `{answers_path}`",
        "",
        "| candidate | quality_score | grounding_score | wrong_paper_count | "
        "empty_answer_count | hidden_reasoning_leak_count | p95_latency_ms | decision |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in summaries:
        lines.append(
            "| {candidate} | {quality_score} | {grounding_score} | {wrong_paper_count} | "
            "{empty_answer_count} | {hidden_reasoning_leak_count} | {p95_latency_ms} | "
            "{decision} |".format(**row)
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def call_chat_endpoint(api_base: str, model: str, question: EvalQuestion, timeout: float) -> str:
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl"),
    )
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/perf/llm-retrieval-eval"))
    parser.add_argument("--candidate", action="append", default=[])
    parser.add_argument("--answers-jsonl", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--api-base")
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = load_manifest(args.manifest)
    candidates = args.candidate or ["dry-run-fixture"]

    if args.dry_run:
        answers_path = write_dry_run_answers(manifest, args.out_dir, candidates)
    elif args.answers_jsonl:
        answers_path = args.answers_jsonl
    elif args.api_base:
        raise EvalHarnessError(
            "endpoint capture is intentionally not auto-scored; capture answer rows with a "
            "judge-reviewed scores object, then rerun with --answers-jsonl"
        )
    else:
        raise EvalHarnessError("provide --dry-run, --answers-jsonl, or --api-base")

    answer_rows = load_answer_rows(answers_path)
    summaries = summarize_answers(manifest, answer_rows)
    write_summary_csv(args.out_dir / "summary.csv", summaries)
    write_markdown_report(args.out_dir / "report.md", manifest, summaries, answers_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalHarnessError as exc:
        print(f"llm_retrieval_eval: {exc}")
        raise SystemExit(2) from exc
