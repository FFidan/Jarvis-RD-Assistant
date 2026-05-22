#!/usr/bin/env bash
# Prepare context for Claude-Code subagent judging of a bench bundle.
# Run AFTER the bench produces tier-rankings.json skeleton.
#
# Usage: scripts/perf/judge.sh <bundle_dir> <seed_dir>
# Example: scripts/perf/judge.sh artifacts/perf/vllm-confirmatory-20260522T120000Z shared/seed-papers/

set -euo pipefail
bundle_dir="${1:?usage: $0 <bundle_dir> <seed_dir>}"
seed_dir="${2:?usage: $0 <bundle_dir> <seed_dir>}"

[ -f "${bundle_dir}/tier-rankings.json" ] || { echo "skeleton missing — run the bench first"; exit 1; }
[ -d "${seed_dir}" ] || { echo "seed dir not found: ${seed_dir}"; exit 1; }

mkdir -p "${bundle_dir}/judge-tasks"

python3 - "${bundle_dir}" "${seed_dir}" <<'PY'
import json, os, sys
bundle, seed = sys.argv[1], sys.argv[2]
with open(os.path.join(bundle, "tier-rankings.json")) as f:
    data = json.load(f)
tasks_dir = os.path.join(bundle, "judge-tasks")
# Resolve JUDGE_PROMPT.md relative to bundle's repo, the script's location, or cwd.
# Operators may invoke this from various directories — try the obvious places.
template_path = None
for cand in [
    os.path.join(os.path.dirname(os.path.abspath(bundle)), "scripts", "perf", "JUDGE_PROMPT.md"),
    os.path.join(os.getcwd(), "scripts", "perf", "JUDGE_PROMPT.md"),
    "scripts/perf/JUDGE_PROMPT.md",
]:
    if os.path.exists(cand):
        template_path = cand
        break
if template_path is None:
    sys.exit("JUDGE_PROMPT.md not found (looked next to bundle, in cwd, and scripts/perf/)")
with open(template_path) as f:
    prompt_template = f.read()
for tier, entries in data["tiers"].items():
    for e in entries:
        cell_dir = os.path.join(bundle, "quality", tier + "_" + e["backend"], e["backend"])
        # Task 3 used cand_dir = OUT_DIR/${pair} where pair = ${CAND_TIER}_${CAND_BACKEND}.
        # quality_capture wrote to OUT_DIR/quality/${pair}/${engine}. So a cell's answers
        # live at <bundle>/quality/<tier>_<backend>/<backend>/q*.txt.
        task_path = os.path.join(tasks_dir, f"{tier}__{e['model'].replace('/', '_')}.md")
        with open(task_path, "w") as out:
            out.write(prompt_template
                      .replace("${SEED_DIR}", seed)
                      .replace("${CELL_DIR}", cell_dir)
                      .replace("<tier>", tier)
                      .replace("<model>", e["model"]))
        print(f"wrote: {task_path}")
PY

echo
echo "Judge tasks ready in: ${bundle_dir}/judge-tasks/"
echo "Run each one via Claude Code (dispatch subagents in parallel)."
echo "After scores.md + verdict.txt files exist, run: scripts/perf/aggregate_judge.py ${bundle_dir}"
