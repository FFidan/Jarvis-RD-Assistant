"""Tests for scripts/perf/llm_retrieval_eval.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "perf" / "llm_retrieval_eval.py"
_MANIFEST = _REPO_ROOT / "docs" / "perf" / "eval_sets" / "2026-07-03-scientific-rag-eval.jsonl"


def _load_module():
    spec = importlib.util.spec_from_file_location("llm_retrieval_eval", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("llm_retrieval_eval", module)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_manifest_has_required_scientific_rag_coverage():
    mod = _load_module()

    manifest = mod.load_manifest(_MANIFEST)

    assert len(manifest.papers) == 10
    assert len(manifest.questions) >= 24
    assert sum(1 for q in manifest.questions if q.scope == "cross_paper") >= 4
    assert sum(1 for q in manifest.questions if q.scope == "unanswerable") >= 3


def test_dry_run_writes_answers_summary_and_report(tmp_path):
    mod = _load_module()

    exit_code = mod.main(
        [
            "--manifest",
            str(_MANIFEST),
            "--dry-run",
            "--candidate",
            "dry-run-fixture",
            "--out-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert (tmp_path / "answers.jsonl").exists()
    assert (tmp_path / "summary.csv").exists()
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "dry-run-fixture" in report
    assert "not promotion evidence" in report


def test_manifest_refuses_too_few_papers(tmp_path):
    mod = _load_module()
    bad_manifest = tmp_path / "bad.jsonl"
    bad_manifest.write_text(
        '{"type":"paper","paper_key":"p1","identifier":"x","title":"x",'
        '"expected_sections":["s"],"known_terms":["t"],"forbidden_confusions":["f"]}\n',
        encoding="utf-8",
    )

    try:
        mod.load_manifest(bad_manifest)
    except mod.EvalHarnessError as exc:
        assert "need at least" in str(exc)
    else:
        raise AssertionError("manifest with one paper should fail")


def _valid_answer_row(mod, question_id: str, candidate: str = "candidate-a") -> dict:
    return {
        "candidate": candidate,
        "question_id": question_id,
        "answer": "Grounded answer",
        "scores": {dimension: 2 for dimension in mod.SCORE_DIMENSIONS},
        "structured_output_valid": True,
        "promotion_eligible": True,
    }


def test_summary_requires_exact_question_coverage_per_candidate():
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, manifest.questions[0].question_id) for _ in manifest.questions]

    try:
        mod.summarize_answers(manifest, rows)
    except mod.EvalHarnessError as exc:
        message = str(exc)
        assert "duplicate" in message
        assert "missing" in message
    else:
        raise AssertionError("duplicate question rows should not summarize as full coverage")


def test_summary_rejects_unknown_question_ids():
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, q.question_id) for q in manifest.questions]
    rows[0] = _valid_answer_row(mod, "q999_not_in_manifest")

    try:
        mod.summarize_answers(manifest, rows)
    except mod.EvalHarnessError as exc:
        message = str(exc)
        assert "unknown" in message
        assert "missing" in message
    else:
        raise AssertionError("unknown question ids should fail fixed-pack coverage")


def test_summary_accepts_one_answer_for_each_manifest_question():
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, q.question_id) for q in manifest.questions]

    summaries = mod.summarize_answers(manifest, rows)

    assert summaries[0]["candidate"] == "candidate-a"
    assert summaries[0]["decision"] == "defer"
