#!/usr/bin/env python3
"""Standalone decider: the proactive-conversation skill on the Claude API, no agent runtime.

Shows how a host application can use the skill without Claude Code:
  1. run the deterministic gate locally (no API call at all when it blocks),
  2. send SKILL.md as the system prompt plus the context and the gate signals,
  3. constrain the reply to the decision schema with structured outputs,
  4. validate locally; with --fail-closed, any error or invalid output becomes WAIT.

Optional dependency, needed only by this adapter:  pip install anthropic
Credentials: ANTHROPIC_API_KEY, or an `ant auth login` profile.

Reads a request ({"context": ..., "gate": ...} or a bare context) on stdin and
prints one decision JSON object:

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

import gate  # noqa: E402
import validate  # noqa: E402

MODEL = os.environ.get("PROACTIVE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("PROACTIVE_EFFORT", "medium")
SCHEMA = json.loads((SKILL_DIR / "references" / "output-schema.json").read_text(encoding="utf-8"))

RUNTIME_NOTE = """

## Runtime note
You have no tools here. The host already ran scripts/gate.py and put its result
in the request under "gate"; the host also validates your answer. Return only
the decision object."""


class DeciderError(RuntimeError):
    pass


def system_prompt() -> str:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]  # drop the YAML frontmatter
    return text.strip() + RUNTIME_NOTE


def decide(request: dict) -> dict:
    ctx = request["context"] if isinstance(request.get("context"), dict) else request
    gate_result = request.get("gate") or gate.evaluate(ctx)
    if gate_result["gate"] == "BLOCK":
        return gate_result["decision"]  # hard rule: no model call

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system_prompt(),
        messages=[{
            "role": "user",
            "content": "Decide whether to initiate a conversation now.\n\n"
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
    decision, _ = validate.extract_json(text)
    if decision is None:
        raise DeciderError(f"no JSON in model output: {text[:200]!r}")
    return decision


def fallback_wait(why: str) -> dict:
    return {"decision": "WAIT", "reason": f"Fail-closed fallback: {why}", "message": None,
            "confidence": 1.0, "evidence": []}


def main() -> int:
    parser = argparse.ArgumentParser(description="Proactive-conversation decision via the Claude API.")
    parser.add_argument("--fail-closed", action="store_true",
                        help="production mode: print a WAIT decision instead of failing or emitting invalid output")
    args = parser.parse_args()
    request = json.load(sys.stdin)
    ctx = request["context"] if isinstance(request.get("context"), dict) else request

    try:
        decision = decide(request)
    except (DeciderError, anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
        if not args.fail_closed:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        decision = fallback_wait(str(exc))

    if args.fail_closed:
        report = validate.validate(decision, ctx)
        if not report["valid"]:
            decision = fallback_wait("invalid model output: " + "; ".join(report["errors"]))
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
