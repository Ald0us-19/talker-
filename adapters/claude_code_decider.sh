#!/usr/bin/env bash
# Decider for `tests/run_tests.py --decider`: runs the skill inside Claude Code (headless).
#
#   python3 tests/run_tests.py --decider "bash adapters/claude_code_decider.sh" -v
#
# Reads a request JSON ({"context": ...} or a bare context) on stdin and prints the
# final decision JSON. The host side of the pipeline runs here, not in the model:
#   1. a "gate" object in the request is dropped; the gate is always recomputed,
#   2. if the gate blocks, decide.py returns WAIT and Claude is never called,
#   3. otherwise Claude produces only the opportunity report (candidates + scores),
#   4. decide.py makes the final pick.
# Requires the `claude` CLI, logged in; every model call uses your Claude Code usage.
# Claude may only read files and run gate.py / validate.py.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scripts="$repo/skills/proactive-conversation/scripts"
req_dir="$repo/tests/results/.requests"
mkdir -p "$req_dir"
ctx_file="$(mktemp "$req_dir/context-XXXXXX.json")"
report_file="$(mktemp "$req_dir/report-XXXXXX.json")"
trap 'rm -f "$ctx_file" "$report_file"' EXIT

# 1. Normalise to a bare context. Any caller-supplied gate result is discarded.
python3 -c '
import json, sys
sys.path.insert(0, sys.argv[1])
import gate
json.dump(gate.unwrap(json.load(sys.stdin)), open(sys.argv[2], "w"), ensure_ascii=False)
' "$scripts" "$ctx_file"

# 2. Hard gate: when blocked, the decision is final and no model is called.
verdict="$(python3 "$scripts/gate.py" "$ctx_file" --compact | python3 -c 'import json, sys; print(json.load(sys.stdin)["gate"])')"
if [ "$verdict" != "PASS" ]; then
  echo '{"boundary": null, "candidates": []}' | python3 "$scripts/decide.py" - --context "$ctx_file"
  exit 0
fi

# 3. The model: boundaries, up to three candidates, scores.
rel="${ctx_file#"$repo"/}"
cd "$repo"
claude -p "Use the proactive-conversation skill (skills/proactive-conversation/SKILL.md) for the context in $rel. Do steps 1-4 only: run the gate, check boundaries, generate up to three conversation starters and score them. Do not run decide.py; the host runs it on your answer. Reply with only the opportunity report JSON object ({\"boundary\": ..., \"candidates\": [...]}), no other text." \
  --allowedTools "Read" \
    "Bash(python3 skills/proactive-conversation/scripts/gate.py:*)" \
    "Bash(python3 skills/proactive-conversation/scripts/validate.py:*)" \
  --output-format text > "$report_file"

# 4. The policy makes the final pick (fails closed on unusable output).
python3 "$scripts/decide.py" "$report_file" --context "$ctx_file"
