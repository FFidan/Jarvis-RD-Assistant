"""Tests for scripts/perf/llm_retrieval_eval.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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


def test_manifest_has_required_scientific_rag_coverage() -> None:
    """Verify the fixed manifest keeps minimum scientific RAG coverage."""
    mod = _load_module()

    manifest = mod.load_manifest(_MANIFEST)

    assert len(manifest.papers) == 10
    assert len(manifest.questions) >= 24
    assert sum(1 for q in manifest.questions if q.scope == "cross_paper") >= 4
    assert sum(1 for q in manifest.questions if q.scope == "unanswerable") >= 3


def test_dry_run_writes_answers_summary_and_report(tmp_path: Path) -> None:
    """Verify dry-run mode writes fixture answers, summaries, and reports.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for generated artifacts.
    """
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


def test_manifest_refuses_too_few_papers(tmp_path: Path) -> None:
    """Verify manifest validation rejects undersized paper packs.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for the invalid manifest.
    """
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
        "citations": [{"paper_key": "p1_attention", "evidence": "section anchor"}],
        "scores": {dimension: 2 for dimension in mod.SCORE_DIMENSIONS},
        "judge_reviewed": True,
        "judge_type": "executor",
        "latency_ms": 123,
        "vram_peak_mb": 2048,
        "retrieval_scope": "fixed_pack_isolated_library",
        "fixed_pack_library_confirmed": True,
        "structured_output_valid": True,
        "wrong_paper_central_claim": False,
        "unanswerable_fabricated_positive_claim": False,
        "promotion_eligible": True,
    }


def test_summary_requires_exact_question_coverage_per_candidate() -> None:
    """Verify aggregation rejects duplicate rows and missing questions."""
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


def test_summary_rejects_unknown_question_ids() -> None:
    """Verify aggregation rejects rows for questions outside the manifest."""
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


def test_summary_accepts_one_answer_for_each_manifest_question() -> None:
    """Verify exact one-row-per-question coverage can be summarized."""
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, q.question_id) for q in manifest.questions]

    summaries = mod.summarize_answers(manifest, rows)

    assert summaries[0]["candidate"] == "candidate-a"
    assert summaries[0]["decision"] == "defer"


def test_product_rag_request_builds_single_and_cross_payloads() -> None:
    """Verify capture payloads match single-paper and library-wide routes."""
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    paper_map = {"p1_attention": 101, "p3_lora": 303}
    single = next(q for q in manifest.questions if q.question_id == "q01_attention_mechanism")
    cross = next(q for q in manifest.questions if q.question_id == "q11_cross_attention_lora")

    single_path, single_payload = mod._product_rag_request(
        single, paper_map, max_chunks=6, max_papers=9, decompose=True
    )
    cross_path, cross_payload = mod._product_rag_request(
        cross, paper_map, max_chunks=7, max_papers=8, decompose=False
    )

    assert single_path == "/api/papers/101/ask"
    assert single_payload == {"question": single.question, "max_chunks": 6}
    assert cross_path == "/api/ask"
    assert cross_payload == {
        "question": cross.question,
        "max_chunks": 7,
        "max_papers": 8,
        "decompose": False,
    }


def test_capture_only_writes_raw_rows_without_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify capture mode writes raw, non-promotable rows only.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for capture artifacts.
    monkeypatch
        Pytest fixture used to replace network I/O with a local stub.
    """
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    paper_map_path = tmp_path / "paper-map.json"
    paper_map = {paper.paper_key: index for index, paper in enumerate(manifest.papers.values(), 1)}
    paper_map_path.write_text(json.dumps(paper_map), encoding="utf-8")
    calls = []

    def fake_post_json(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        return 200, {
            "answer": "Captured answer",
            "sources": [{"paper_id": 1, "page": 2}],
            "confidence": "medium",
            "verified_fraction": 0.75,
        }

    monkeypatch.setattr(mod, "_post_json", fake_post_json)

    exit_code = mod.main(
        [
            "--manifest",
            str(_MANIFEST),
            "--capture-only",
            "--api-base",
            "http://jarvis.local",
            "--api-key",
            "test-key",
            "--candidate",
            "current-smart-local",
            "--paper-map-json",
            str(paper_map_path),
            "--fixed-pack-library-confirmed",
            "--out-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert calls[0][0].endswith("/api/papers/1/ask")
    assert calls[0][2]["X-API-Key"] == "test-key"
    rows = [json.loads(line) for line in (tmp_path / "raw_answers.jsonl").read_text().splitlines()]
    assert len(rows) == len(manifest.questions)
    assert rows[0]["scores"] is None
    assert rows[0]["promotion_eligible"] is False
    assert rows[0]["fixed_pack_library_confirmed"] is True
    assert rows[0]["backend_metadata"] == {"confidence": "medium", "verified_fraction": 0.75}
    assert not (tmp_path / "summary.csv").exists()


def test_capture_only_requires_fixed_pack_confirmation_for_library_wide_questions(
    tmp_path: Path,
) -> None:
    """Verify library-wide capture fails closed without fixed-pack confirmation.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for the paper map.
    """
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    paper_map_path = tmp_path / "paper-map.json"
    paper_map = {paper.paper_key: index for index, paper in enumerate(manifest.papers.values(), 1)}
    paper_map_path.write_text(json.dumps(paper_map), encoding="utf-8")

    try:
        mod.main(
            [
                "--manifest",
                str(_MANIFEST),
                "--capture-only",
                "--api-base",
                "http://jarvis.local",
                "--api-key",
                "test-key",
                "--candidate",
                "current-smart-local",
                "--paper-map-json",
                str(paper_map_path),
                "--out-dir",
                str(tmp_path),
            ]
        )
    except mod.EvalHarnessError as exc:
        assert "fixed-pack-library-confirmed" in str(exc)
    else:
        raise AssertionError("library-wide capture must require fixed-pack library confirmation")


def test_capture_rows_cannot_be_summarized_as_evidence(tmp_path: Path) -> None:
    """Verify raw capture rows cannot be aggregated as judged evidence.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for raw capture rows.
    """
    mod = _load_module()
    capture_path = tmp_path / "raw_answers.jsonl"
    capture_path.write_text(
        json.dumps(
            {
                "candidate": "current-smart-local",
                "question_id": "q01_attention_mechanism",
                "answer": "Captured answer",
                "scores": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        mod.load_answer_rows(capture_path)
    except mod.EvalHarnessError as exc:
        assert "missing scores object" in str(exc)
    else:
        raise AssertionError("capture-only rows with scores:null must not summarize")


def test_answer_rows_reject_model_self_judged_scores(tmp_path: Path) -> None:
    """Verify model-self judging is rejected before aggregation.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for judged answer rows.
    """
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, q.question_id) for q in manifest.questions]
    rows[0]["judge_type"] = "model_self"
    answers_path = tmp_path / "answers.jsonl"
    answers_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    try:
        mod.load_answer_rows(answers_path)
    except mod.EvalHarnessError as exc:
        assert "judge_type" in str(exc)
    else:
        raise AssertionError("model-self judged rows must be rejected")


def test_answer_rows_require_complete_judged_metadata(tmp_path: Path) -> None:
    """Verify judged rows must carry all hard-fail metadata.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for judged answer rows.
    """
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, q.question_id) for q in manifest.questions]
    del rows[0]["wrong_paper_central_claim"]
    answers_path = tmp_path / "answers.jsonl"
    answers_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    try:
        mod.load_answer_rows(answers_path)
    except mod.EvalHarnessError as exc:
        assert "wrong_paper_central_claim" in str(exc)
    else:
        raise AssertionError("incomplete judged rows must be rejected")


def test_answer_rows_require_non_empty_evidence(tmp_path: Path) -> None:
    """Verify judged rows need non-empty citation or source evidence.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for judged answer rows.
    """
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, q.question_id) for q in manifest.questions]
    rows[0]["citations"] = []
    answers_path = tmp_path / "answers.jsonl"
    answers_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    try:
        mod.load_answer_rows(answers_path)
    except mod.EvalHarnessError as exc:
        assert "non-empty citations or sources" in str(exc)
    else:
        raise AssertionError("judged rows without evidence must be rejected")


def test_answer_rows_require_numeric_vram(tmp_path: Path) -> None:
    """Verify judged rows need numeric VRAM measurements.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for judged answer rows.
    """
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, q.question_id) for q in manifest.questions]
    rows[0]["vram_peak_mb"] = None
    answers_path = tmp_path / "answers.jsonl"
    answers_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    try:
        mod.load_answer_rows(answers_path)
    except mod.EvalHarnessError as exc:
        assert "vram_peak_mb" in str(exc)
    else:
        raise AssertionError("judged rows without numeric VRAM must be rejected")


def test_summary_requires_fixed_pack_scope_for_library_wide_rows(tmp_path: Path) -> None:
    """Verify library-wide judged rows preserve fixed-pack scope markers.

    Parameters
    ----------
    tmp_path
        Pytest fixture included for signature parity with row-validation tests.
    """
    mod = _load_module()
    manifest = mod.load_manifest(_MANIFEST)
    rows = [_valid_answer_row(mod, q.question_id) for q in manifest.questions]
    cross_index = next(
        index
        for index, question in enumerate(manifest.questions)
        if question.scope == "cross_paper"
    )
    del rows[cross_index]["retrieval_scope"]

    try:
        mod.summarize_answers(manifest, rows)
    except mod.EvalHarnessError as exc:
        assert "fixed-pack retrieval_scope" in str(exc)
    else:
        raise AssertionError("library-wide rows must preserve fixed-pack retrieval scope")


def test_main_rejects_ambiguous_modes(tmp_path: Path) -> None:
    """Verify the CLI rejects mutually ambiguous execution modes.

    Parameters
    ----------
    tmp_path
        Pytest-managed temporary directory for command output.
    """
    mod = _load_module()

    try:
        mod.main(
            [
                "--manifest",
                str(_MANIFEST),
                "--dry-run",
                "--capture-only",
                "--api-base",
                "http://jarvis.local",
                "--out-dir",
                str(tmp_path),
            ]
        )
    except mod.EvalHarnessError as exc:
        assert "choose exactly one mode" in str(exc)
    else:
        raise AssertionError("ambiguous mode selection should fail")
