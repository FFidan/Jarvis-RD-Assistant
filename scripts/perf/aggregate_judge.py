#!/usr/bin/env python3
"""Aggregate per-cell verdict.txt files into tier-rankings.json + report.md."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path


def aggregate(bundle_dir: Path) -> None:
    rankings_path = bundle_dir / "tier-rankings.json"
    if not rankings_path.exists():
        sys.exit(f"skeleton missing: {rankings_path}")

    with rankings_path.open(encoding="utf-8") as f:
        data = json.load(f)

    # Read every verdict.txt under the bundle
    for verdict_file in bundle_dir.rglob("verdict.txt"):
        try:
            tier, model, score, summary = (
                verdict_file.read_text(encoding="utf-8").strip().split("\t", 3)
            )
        except ValueError as exc:
            sys.stderr.write(f"skipping malformed {verdict_file}: {exc}\n")
            continue
        for entry in data["tiers"].get(tier, []):
            if entry["model"] == model:
                entry["judge_score"] = int(score)
                entry["summary"] = summary

    # Sort each tier by judge_score desc; use throughput_p95_at_c4 as tiebreaker
    # (lower latency = better, so negate; 1e9 = sentinel for missing/null → sorts last)
    for tier, entries in data["tiers"].items():
        entries.sort(
            key=lambda e: (e.get("judge_score") or 0, -(e.get("throughput_p95_at_c4") or 1e9)),
            reverse=True,
        )

    data["judge_run_at"] = date.today().isoformat()
    rankings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Write narrative report
    report = [f"# Tier defaults report — {data['judge_run_at']}\n"]
    for tier, entries in data["tiers"].items():
        report.append(f"\n## Tier `{tier}`\n")
        report.append("| Rank | Model | Backend | Score | sim/native | Summary |")
        report.append("|---|---|---|---|---|---|")
        for i, e in enumerate(entries, 1):
            report.append(
                f"| {i} | `{e['model']}` | {e['backend']} | "
                f"{e.get('judge_score', '—')} | {e.get('sim_or_native', '—')} | "
                f"{e.get('summary', '—').replace('|', chr(92) + '|')} |"
            )
        if entries:
            top = entries[0]
            report.append(f"\n**Recommended default:** `{top['model']}` ({top['backend']})\n")

    # bundle lives at <repo_root>/artifacts/perf/<bundle>/, so .parent×3 = repo root
    report_path = (
        bundle_dir.parent.parent.parent
        / "docs"
        / "perf"
        / f"{data['judge_run_at']}-tier-defaults-report.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report), encoding="utf-8")
    print(f"report → {report_path}")
    print(f"rankings → {rankings_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <bundle_dir>")
    aggregate(Path(sys.argv[1]))
