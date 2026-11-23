#!/usr/bin/env bash
# Decider for `tests/run_tests.py --decider`: runs the skill inside Claude Code (headless).
#
#   python3 tests/run_tests.py --decider "bash adapters/claude_code_decider.sh" -v
#
# Reads a request JSON ({"context": ..., "gate": ...}) on stdin and prints the
# decision JSON. Requires the `claude` CLI, logged in; every call uses your
# Claude Code usage. Claude may only read files and run the two skill scripts.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
req_dir="$repo/tests/results/.requests"
mkdir -p "$req_dir"
req="$(mktemp "$req_dir/request-XXXXXX.json")"
trap 'rm -f "$req"' EXIT
cat > "$req"
rel="${req#"$repo"/}"

cd "$repo"
claude -p "Use the proactive-conversation skill (skills/proactive-conversation/SKILL.md) to decide whether to initiate a conversation for the request in $rel. If the request contains a \"gate\" object, the host already ran the gate; otherwise run the gate on that file yourself. Reply with only the decision JSON object, no other text." \
  --allowedTools "Read" \
    "Bash(python3 skills/proactive-conversation/scripts/gate.py:*)" \
    "Bash(python3 skills/proactive-conversation/scripts/validate.py:*)" \
  --output-format text
