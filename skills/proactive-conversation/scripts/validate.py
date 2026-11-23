#!/usr/bin/env python3
"""Validate a proactive-conversation decision against the output contract.

Checks three things, deterministically:
  1. Contract  - required fields, types, SPEAK/WAIT consistency, confidence range.
  2. Grounding - every evidence id exists in the context; SPEAK needs evidence.
  3. Style     - no generic openers, no pressure/guilt, length and tone limits.

Errors make the decision invalid (a host should fall back to WAIT).
Warnings are advisory.

Usage:
    python3 validate.py decision.json [--context context.json]
    echo '{"decision": "WAIT", ...}' | python3 validate.py -

Exit code 0 = valid, 1 = invalid, 2 = unreadable input. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Optional

REQUIRED = ("decision", "reason", "message", "confidence", "evidence")
MIN_SPEAK_CONFIDENCE = 0.6
MAX_MESSAGE_CHARS = 320
MAX_REASON_CHARS = 400

# Generic openers: they signal "speaking to keep activity up" rather than a real reason.
GENERIC_PATTERNS = [
    (r"\bjust (wanted to )?check(ing)? in\b", "just checking in"),
    (r"\bhow are you( doing)?\s*[?!.]", "how are you?"),
    (r"\bhow('?s| is) it going\b", "how's it going"),
    (r"\bhow('?s| is| was) your (day|week|weekend)\b", "how was your day/week"),
    (r"\bhope (you'?re|you are) (doing )?(well|good|great|ok(ay)?)\b", "hope you're well"),
    (r"\bhope all is well\b", "hope all is well"),
    (r"\blong time no (see|talk|chat)\b", "long time no talk"),
    (r"\b(been )?thinking (about|of) you\b", "thinking of you"),
    (r"\bwhat'?s up\b", "what's up"),
    (r"\b(what'?s|anything) new\b", "what's new"),
    (r"\bwhat are you up to\b", "what are you up to"),
    (r"^\s*(hey|hi|hello)( there)?\s*[!.]", "bare greeting opener"),
    (r"\bi'?m (always )?here (for you|if you need)\b", "I'm here for you"),
    (r"\bmiss(ed)? (talking to |chatting with )?you\b", "missed you"),
]

# Pressure / guilt: proactive messages must be easy to ignore.
PRESSURE_PATTERNS = [
    (r"\bhaven'?t (heard|replied|responded)\b", "haven't heard back"),
    (r"\bwhy (haven'?t|didn'?t) you\b", "why didn't you"),
    (r"\b(reply|respond|answer) (asap|soon|quickly)\b", "reply soon"),
    (r"\bplease (reply|respond|answer)\b", "please reply"),
    (r"\bare you ignoring\b", "are you ignoring me"),
    (r"\bi'?ve been waiting\b", "I've been waiting"),
    (r"\bdon'?t forget to (reply|respond|answer)\b", "don't forget to reply"),
]

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")


def extract_json(text: str) -> tuple:
    """Return (obj, note). Accepts raw JSON, a ```json fenced block, or the first {...} object."""
    text = text.strip()
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1)), "decision was wrapped in a code fence"
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1]), "decision was surrounded by extra text"
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None, "no JSON object found"


def context_ids(ctx: Optional[dict]) -> Optional[set]:
    if not isinstance(ctx, dict):
        return None
    ids = set()
    for key in ("history", "events", "memories"):
        for item in ctx.get(key) or []:
            if isinstance(item, dict) and "id" in item:
                ids.add(str(item["id"]))
    return ids


def _user_uses_emoji(ctx: Optional[dict]) -> bool:
    if not isinstance(ctx, dict):
        return False
    return any(
        isinstance(m, dict) and m.get("role") == "user" and EMOJI_RE.search(str(m.get("text", "")))
        for m in ctx.get("history") or []
    )


def validate(decision: Any, ctx: Optional[dict] = None, min_speak_confidence: float = MIN_SPEAK_CONFIDENCE) -> dict:
    errors: list = []
    warnings: list = []

    if not isinstance(decision, dict):
        return {"valid": False, "errors": ["decision must be a JSON object"], "warnings": []}

    for key in REQUIRED:
        if key not in decision:
            errors.append(f"missing field '{key}'")
    extra = set(decision) - set(REQUIRED)
    if extra:
        warnings.append(f"unknown fields ignored: {sorted(extra)}")

    kind = decision.get("decision")
    if kind not in ("SPEAK", "WAIT"):
        errors.append("'decision' must be \"SPEAK\" or \"WAIT\"")

    reason = decision.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("'reason' must be a non-empty string")
    elif len(reason) > MAX_REASON_CHARS:
        errors.append(f"'reason' is {len(reason)} chars; keep it brief (<= {MAX_REASON_CHARS})")

    conf = decision.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        errors.append("'confidence' must be a number between 0 and 1")
        conf = None

    evidence = decision.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        errors.append("'evidence' must be a list of id strings (may be empty for WAIT)")
        evidence = []
    known = context_ids(ctx)
    if known is not None:
        unknown = [e for e in evidence if e not in known]
        if unknown:
            errors.append(f"evidence ids not found in context (possible fabrication): {unknown}")

    message = decision.get("message")
    if kind == "WAIT":
        if message is not None:
            errors.append("'message' must be null when decision is WAIT")
    elif kind == "SPEAK":
        if not evidence:
            errors.append("SPEAK requires at least one evidence id grounding the message in the context")
        if conf is not None and conf < min_speak_confidence:
            errors.append(f"SPEAK with confidence {conf} is below the threshold {min_speak_confidence}; decide WAIT instead")
        if not isinstance(message, str) or not message.strip():
            errors.append("'message' must be a non-empty string when decision is SPEAK")
        else:
            errors.extend(style_errors(message))
            warnings.extend(style_warnings(message, ctx))

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def style_errors(message: str) -> list:
    errs = []
    low = message.lower()
    for pattern, label in GENERIC_PATTERNS:
        if re.search(pattern, low):
            errs.append(f"generic opener: '{label}' - reference the specific topic instead")
    for pattern, label in PRESSURE_PATTERNS:
        if re.search(pattern, low):
            errs.append(f"pressure/guilt phrasing: '{label}' - proactive messages must be easy to ignore")
    if len(message) > MAX_MESSAGE_CHARS:
        errs.append(f"message is {len(message)} chars (max {MAX_MESSAGE_CHARS}); keep it to 1-2 short sentences")
    if message.count("!") > 1:
        errs.append("more than one '!' - too enthusiastic for an unprompted message")
    return errs


def style_warnings(message: str, ctx: Optional[dict]) -> list:
    warns = []
    if message.count("?") > 2:
        warns.append("more than two questions; ask at most one thing")
    if EMOJI_RE.search(message) and not _user_uses_emoji(ctx):
        warns.append("emoji used although the user does not use them")
    if re.search(r"\b[A-Z]{4,}\b", message):
        warns.append("all-caps word; may read as shouting")
    return warns


def _load(path: str) -> str:
    return sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a proactive-conversation decision.")
    parser.add_argument("decision", help="decision JSON file, or '-' for stdin")
    parser.add_argument("--context", help="context JSON file, enables evidence-id grounding checks")
    parser.add_argument("--min-speak-confidence", type=float, default=MIN_SPEAK_CONFIDENCE)
    args = parser.parse_args(argv)
    try:
        decision, note = extract_json(_load(args.decision))
        ctx = json.loads(_load(args.context)) if args.context else None
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [f"cannot read input: {exc}"], "warnings": []}))
        return 2
    if isinstance(ctx, dict) and "now" not in ctx:
        for key in ("context", "input"):  # accept a request or test-scenario wrapper too
            if isinstance(ctx.get(key), dict):
                ctx = ctx[key]
                break
    result = validate(decision, ctx, args.min_speak_confidence)
    if note:
        result["warnings"].insert(0, note)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
