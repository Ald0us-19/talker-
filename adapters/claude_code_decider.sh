#!/usr/bin/env bash
# Decider for `tests/run_tests.py --decider`: runs the skill inside Claude Code (headless).
#
#   python3 tests/run_tests.py --decider "bash adapters/claude_code_decider.sh" -v
#
# Reads a request JSON ({"context": ...} or a bare context) on stdin and prints the
# final decision JSON. The host side of the pipeline runs here, not in the model:
#   1. a "gate" object in the request is dropped; the gate is always recomputed,
#   2. if the gate blocks, decide.py returns WAIT and Claude is never called,
#   3. otherwise Claude gets the context plus the gate result the adapter computed
#      (signals: times, recent proactive topics, engagement) and produces only the
#      opportunity report (candidates + scores); it runs no scripts,
#   4. decide.py makes the final pick.
# Requires the `claude` CLI, logged in; every model call uses your Claude Code usage.
# Claude may only read files.
#
# Optional: PROACTIVE_RAW_DIR=<dir> saves Claude's full JSON result envelope (raw report,
# model, cost, turns) as <dir>/<PROACTIVE_REQUEST_ID>.claude.json; run_tests.py sets the id.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scripts="$repo/skills/proactive-conversation/scripts"
req_dir="$repo/tests/results/.requests"
mkdir -p "$req_dir"
ctx_file="$(mktemp "$req_dir/context-XXXXXX.json")"
request_file="$(mktemp "$req_dir/request-XXXXXX.json")"
gate_file="$(mktemp "$req_dir/gate-XXXXXX.json")"
report_file="$(mktemp "$req_dir/report-XXXXXX.json")"
envelope_file="$(mktemp "$req_dir/envelope-XXXXXX.json")"
trap 'rm -f "$ctx_file" "$request_file" "$gate_file" "$report_file" "$envelope_file"' EXIT

# 1. Normalise to a bare context. Any caller-supplied gate result is discarded.
python3 -c '
import json, sys
sys.path.insert(0, sys.argv[1])
import gate
json.dump(gate.unwrap(json.load(sys.stdin)), open(sys.argv[2], "w"), ensure_ascii=False)
' "$scripts" "$ctx_file"

# 2. Hard gate: when blocked, the decision is final and no model is called.
python3 "$scripts/gate.py" "$ctx_file" --compact > "$gate_file"
verdict="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["gate"])' "$gate_file")"
if [ "$verdict" != "PASS" ]; then
  echo '{"boundary": null, "candidates": []}' | python3 "$scripts/decide.py" - --context "$ctx_file"
  exit 0
fi

# 3. The model: boundaries, up to three candidates, scores. It gets the gate result computed
#    above (trusted: it was computed here), so it needs no scripts and never does date math.
python3 -c '
import json, sys
json.dump({"context": json.load(open(sys.argv[1])), "gate": json.load(open(sys.argv[2]))},
          open(sys.argv[3], "w"), ensure_ascii=False, indent=1)
' "$ctx_file" "$gate_file" "$request_file"
rel="${request_file#"$repo"/}"
cd "$repo"
claude -p "Use the proactive-conversation skill (skills/proactive-conversation/SKILL.md) for the request in $rel. The host already ran the gate: its result, with all signals, is under \"gate\" in that file, so use those signals and do not run any scripts. Do steps 2-4 only: check boundaries, generate up to three conversation starters and score them. The host runs decide.py on your answer. Reply with only the opportunity report JSON object ({\"boundary\": ..., \"candidates\": [...]}), no other text." \
  --allowedTools "Read" \
  --output-format json > "$envelope_file"

python3 -c '
import json, pathlib, sys
envelope_path, report_path, raw_dir, request_id = sys.argv[1:5]
raw = open(envelope_path, encoding="utf-8").read()
try:
    envelope = json.loads(raw)
    result = envelope.get("result", raw) if isinstance(envelope, dict) else raw
except json.JSONDecodeError:
    result = raw  # not a JSON envelope: treat the whole output as the report text
open(report_path, "w", encoding="utf-8").write(result if isinstance(result, str) else json.dumps(result))
if raw_dir:
    out = pathlib.Path(raw_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{request_id}.claude.json").write_text(raw, encoding="utf-8")
' "$envelope_file" "$report_file" "${PROACTIVE_RAW_DIR:-}" "${PROACTIVE_REQUEST_ID:-request}"

# 4. The policy makes the final pick (fails closed on unusable output).
python3 "$scripts/decide.py" "$report_file" --context "$ctx_file"
