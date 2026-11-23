#!/usr/bin/env python3
"""Standalone decider: the proactive-conversation skill on the Claude API, no agent runtime.

Shows how a host application uses the skill without Claude Code:
  1. run the deterministic gate locally. A "gate" in the request is ignored, never
     trusted; when the gate blocks there is no API call at all,
  2. send SKILL.md as the system prompt plus the context and the locally computed
     signals, and ask only for the opportunity report (structured outputs enforce
     its schema),
  3. let decide.py make the final pick (validation, vetoes, pacing, mode bar),
  4. with --fail-closed, any error or invalid result becomes WAIT.

Optional dependency, needed only by this adapter:  pip install anthropic
Credentials: ANTHROPIC_API_KEY, or an `ant auth login` profile.

Reads a request ({"context": ...} or a bare context) on stdin and prints one
decision JSON object (including the scored candidates):

  python3 tests/run_tests.py --decider "python3 adapters/anthropic_api_decider.py"
  python3 adapters/anthropic_api_decider.py --fail-closed < context.json

Environment: PROACTIVE_MODEL (default claude-opus-5-5), PROACTIVE_EFFORT (default medium).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import anthropic

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "proactive-conversation"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import decide  # noqa: E402
import gate  # noqa: E402
import validate  # noqa: E402

MODEL = os.environ.get("PROACTIVE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("PROACTIVE_EFFORT", "medium")
SCHEMA = json.loads((SKILL_DIR / "references" / "opportunity-schema.json").read_text(encoding="utf-8"))

RUNTIME_NOTE = """

## Runtime note
You have no tools here. The host already ran scripts/gate.py (its result is in the
request under "gate") and will run scripts/decide.py on your answer. Do steps 2-4
only: boundaries, up to three candidates, scores. Return only the opportunity report."""


class DeciderError(RuntimeError):
    pass


def system_prompt() -> str:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]  # drop the YAML frontmatter
    return text.strip() + RUNTIME_NOTE


def generate_report(ctx: dict, gate_result: dict) -> dict:
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system_prompt(),
        messages=[{
            "role": "user",
            "content": "Find conversation starters for this user right now.\n\n"
            + json.dumps({"context": ctx, "gate": gate_result}, ensure_ascii=False, indent=2),
        }],
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        # If the primary model declines, the API retries on a fallback model within the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise DeciderError("the model declined this request")
    if response.stop_reason == "max_tokens":
        raise DeciderError("the response was cut off at max_tokens")
    text = next((block.text for block in response.content if block.type == "text"), "")
    report, _ = validate.extract_json(text)
    if report is None:
        raise DeciderError(f"no JSON in model output: {text[:200]!r}")
    return report


def fail_closed(why: str) -> dict:
    return {"decision": "WAIT", "reason": f"Fail-closed fallback: {why}"[: validate.MAX_REASON_CHARS], "message": None,
            "confidence": 1.0, "evidence": [], "selected": None, "boundary": None, "candidates": []}


def main() -> int:
    parser = argparse.ArgumentParser(description="Proactive-conversation decision via the Claude API.")
    parser.add_argument("--fail-closed", action="store_true",
                        help="production mode: print a WAIT decision instead of failing or emitting invalid output")
    args = parser.parse_args()
    request = json.load(sys.stdin)
    if isinstance(request, dict) and "gate" in request:
        print("note: ignoring the supplied 'gate'; it is recomputed locally.", file=sys.stderr)
    ctx = gate.unwrap(request)

    gate_result = gate.evaluate(ctx)  # never taken from the request
    if gate_result["gate"] == "BLOCK":
        decision = decide.decide(ctx, {"boundary": None, "candidates": []})
    else:
        try:
            decision = decide.decide(ctx, generate_report(ctx, gate_result))
        except (DeciderError, anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
            if not args.fail_closed:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            decision = fail_closed(str(exc))

    if args.fail_closed:
        check = validate.validate(decision, ctx)
        if not check["valid"]:
            decision = fail_closed("invalid decision: " + "; ".join(check["errors"]))
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
