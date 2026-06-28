"""Unit tests for scripts/gen_notice.py — gate, check-notice, and unknown-license coverage."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# Load gen_notice as a module (it lives in scripts/, not a package)
_SCRIPT = Path(__file__).parent.parent / "gen_notice.py"
spec = importlib.util.spec_from_file_location("gen_notice", _SCRIPT)
gen_notice = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(gen_notice)  # type: ignore[union-attr]
main = gen_notice.main


# ---------------------------------------------------------------------------
# is_strong_copyleft
# ---------------------------------------------------------------------------


def test_strong_copyleft_gpl():
    assert gen_notice.is_strong_copyleft("GPL-3.0")


def test_strong_copyleft_agpl():
    assert gen_notice.is_strong_copyleft("AGPL-3.0-only")


def test_strong_copyleft_lgpl_is_not_strong():
    assert not gen_notice.is_strong_copyleft("LGPL-2.1")


def test_strong_copyleft_mit_is_false():
    assert not gen_notice.is_strong_copyleft("MIT")


# ---------------------------------------------------------------------------
# is_unrecognized (NEW)
# ---------------------------------------------------------------------------


def test_unrecognized_unknown():
    assert gen_notice.is_unrecognized("UNKNOWN")


def test_unrecognized_licenseref():
    assert gen_notice.is_unrecognized("LicenseRef-proprietary")


def test_unrecognized_empty():
    assert gen_notice.is_unrecognized("")


def test_unrecognized_mit_is_false():
    assert not gen_notice.is_unrecognized("MIT")


def test_unrecognized_lgpl_is_false():
    assert not gen_notice.is_unrecognized("LGPL-2.1")


# ---------------------------------------------------------------------------
# cmd_gate — strong copyleft and unknown license
# ---------------------------------------------------------------------------


def _py_json(tmp_path: Path, entries: list[dict]) -> str:
    p = tmp_path / "licenses-python.json"
    p.write_text(json.dumps(entries))
    return str(p)


def test_gate_passes_permissive(tmp_path):
    rc = main(
        [
            "gate",
            "--python-json",
            _py_json(tmp_path, [{"Name": "requests", "Version": "2.31", "License": "Apache-2.0"}]),
        ]
    )
    assert rc == 0


def test_gate_fails_gpl(tmp_path):
    rc = main(
        [
            "gate",
            "--python-json",
            _py_json(tmp_path, [{"Name": "bad-pkg", "Version": "1.0", "License": "GPL-3.0"}]),
        ]
    )
    assert rc == 1


def test_gate_fails_unknown_license(tmp_path):
    """An unrecognized license string must block the gate — not silently pass as permissive."""
    rc = main(
        [
            "gate",
            "--python-json",
            _py_json(tmp_path, [{"Name": "mystery", "Version": "0.1", "License": "UNKNOWN"}]),
        ]
    )
    assert rc == 1


def test_gate_fails_licenseref(tmp_path):
    rc = main(
        [
            "gate",
            "--python-json",
            _py_json(
                tmp_path,
                [{"Name": "custom-lib", "Version": "2.0", "License": "LicenseRef-proprietary"}],
            ),
        ]
    )
    assert rc == 1


def test_gate_fails_empty_license(tmp_path):
    rc = main(
        [
            "gate",
            "--python-json",
            _py_json(tmp_path, [{"Name": "no-license", "Version": "1.0", "License": ""}]),
        ]
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# cmd_check_notice — Node JSON coverage (NEW)
# ---------------------------------------------------------------------------


def test_check_notice_passes_when_node_lgpl_attributed(tmp_path):
    python_data: list[dict] = []
    node_data = {"frozendict@2.4.7": {"licenses": "LGPL-2.0"}}
    notice = "NOTICE\n======\n\n  frozendict        2.4.7   — LGPL-2.0\n"
    (tmp_path / "p.json").write_text(json.dumps(python_data))
    (tmp_path / "n.json").write_text(json.dumps(node_data))
    (tmp_path / "NOTICE").write_text(notice)
    rc = main(
        [
            "check-notice",
            "--python-json",
            str(tmp_path / "p.json"),
            "--node-json",
            str(tmp_path / "n.json"),
            "--notice",
            str(tmp_path / "NOTICE"),
        ]
    )
    assert rc == 0


def test_check_notice_fails_when_node_lgpl_missing(tmp_path):
    """check-notice --node-json must fail when a Node LGPL dep is not in NOTICE."""
    python_data: list[dict] = []
    node_data = {"some-lgpl-lib@1.0.0": {"licenses": "LGPL-2.1"}}
    notice = "NOTICE\n======\n"
    (tmp_path / "p.json").write_text(json.dumps(python_data))
    (tmp_path / "n.json").write_text(json.dumps(node_data))
    (tmp_path / "NOTICE").write_text(notice)
    rc = main(
        [
            "check-notice",
            "--python-json",
            str(tmp_path / "p.json"),
            "--node-json",
            str(tmp_path / "n.json"),
            "--notice",
            str(tmp_path / "NOTICE"),
        ]
    )
    assert rc == 1
