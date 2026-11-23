#!/usr/bin/env python3
"""Validate opportunity reports and final decisions for the proactive-conversation skill.

Two contracts, both checked deterministically:

  Opportunity report (output of the model's generator step):
    {"boundary": null | {...}, "candidates": [up to 3 scored conversation starters]}
    - grounding: every candidate cites evidence ids that exist in the context
    - diversity: no two candidates cover the same topic
    - repetition: no topic raised recently, no near-duplicate of an earlier message
    - style: no generic openers, pressure, artificial enthusiasm; length and tone limits

  Decision (output of decide.py):
    {"decision", "reason", "message", "confidence", "evidence", "selected", "boundary", "candidates",
     "preference_update"}
    - SPEAK only when the recomputed gate passes (a caller-supplied gate is never trusted)
    - the message is the selected candidate's, its score clears the mode's bar,
      and it passes the grounding, repetition and style checks

Errors make an item invalid (decide.py rejects that candidate; a host falls back
to WAIT). Warnings are advisory.

Usage:
    python3 validate.py decision.json --context context.json
    python3 validate.py report.json --report --context context.json
    echo '{"decision": "WAIT", ...}' | python3 validate.py - --context context.json

Exit code 0 = valid, 1 = invalid, 2 = unreadable input. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402

REQUIRED = ("decision", "reason", "message", "confidence", "evidence", "selected", "boundary", "candidates",
            "preference_update")
UPDATE_KEYS = ("proactive_enabled", "snooze_until", "pause_until_user_writes", "updated_at")
KINDS = ("follow_up", "revisit", "connection", "idea", "curiosity", "casual")
SCORE_KEYS = ("relevance", "novelty", "naturalness", "timing", "engagement", "interruption_cost")
BOUNDARY_TYPES = ("requested_break", "focus_time", "other")
MAX_CANDIDATES = 3
MAX_MESSAGE_CHARS = 320
MAX_REASON_CHARS = 400
SAME_TOPIC = 0.5  # token overlap at which two topics count as the same
NEAR_DUPLICATE = 0.6  # token overlap at which two messages count as the same
NO_REPLY_TOPIC_DAYS = 7  # a topic whose last mention got no reply stays off-limits this long

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

# Artificial enthusiasm: performed excitement reads as fake in an unprompted message.
ENTHUSIASM_PATTERNS = [
    (r"\bso excited\b", "so excited"),
    (r"\bcan'?t wait to hear\b", "can't wait to hear"),
    (r"\bsuper (fun|exciting|cool)\b", "super fun"),
    (r"\b(amazing|awesome|incredible)!", "exclaimed superlative"),
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

STOPWORDS = set(
    """a an the and or but if so of to in on at by for with about from into over after before
    is are was were be been being am do does did have has had i you he she it we they me my your our
    their this that these those what which who how when where why can could would should will just
    really maybe some any more most very also still yet again ever not no yes there here than then
    der die das und oder aber ein eine ist sind war wie was wer mit von zu im in am auf für ich du er
    sie es wir ihr dich dir mich mir noch schon""".split()
)


# ---------------------------------------------------------------- text utilities


def tokens(text: Any) -> set:
    """Content-word stems (first 6 letters) used for topic and duplicate checks."""
    if not isinstance(text, str):
        return set()
    words = re.findall(r"[a-zà-ÿß0-9]+", text.lower())
    return {w[:6] for w in words if len(w) >= 3 and w not in STOPWORDS}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def containment(a: set, b: set) -> float:
    """Share of a's tokens that also appear in b."""
    return len(a & b) / len(a) if a and b else 0.0


def same_topic(a: Any, b: Any) -> bool:
    return jaccard(tokens(a), tokens(b)) >= SAME_TOPIC


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
            return json.loads(fence.group(1)), "JSON was wrapped in a code fence"
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
                        return json.loads(text[start : i + 1]), "JSON was surrounded by extra text"
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None, "no JSON object found"


def context_ids(ctx: Optional[dict]) -> Optional[set]:
    if not isinstance(ctx, dict):
        return None
    ids = set()
    profile = ctx.get("profile") if isinstance(ctx.get("profile"), dict) else {}
    for items in (ctx.get("history"), ctx.get("events"), ctx.get("memories"), profile.get("interests")):
        for item in items if isinstance(items, list) else []:
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


def _assistant_texts(ctx: Optional[dict]) -> list:
    if not isinstance(ctx, dict):
        return []
    return [
        (str(m.get("id")), m["text"])
        for m in ctx.get("history") or []
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("text"), str)
    ]


# ---------------------------------------------------------------- style


def style_errors(message: str) -> list:
    errs = []
    low = message.lower()
    for pattern, label in GENERIC_PATTERNS:
        if re.search(pattern, low):
            errs.append(f"generic opener: '{label}' - reference the specific topic instead")
    for pattern, label in ENTHUSIASM_PATTERNS:
        if re.search(pattern, low):
            errs.append(f"artificial enthusiasm: '{label}'")
    for pattern, label in PRESSURE_PATTERNS:
        if re.search(pattern, low):
            errs.append(f"pressure/guilt phrasing: '{label}' - proactive messages must be easy to ignore")
    if len(message) > MAX_MESSAGE_CHARS:
        errs.append(f"message is {len(message)} chars (max {MAX_MESSAGE_CHARS}); keep it to 1-2 short sentences")
    if message.count("!") > 1:
        errs.append("more than one '!' - too enthusiastic for an unprompted message")
    if message.count("?") > 2:
        errs.append("more than two questions; ask one thing")
    return errs


def style_warnings(message: str, ctx: Optional[dict]) -> list:
    warns = []
    if EMOJI_RE.search(message) and not _user_uses_emoji(ctx):
        warns.append("emoji used although the user does not use them")
    if re.search(r"\b[A-Z]{4,}\b", message):
        warns.append("all-caps word; may read as shouting")
    return warns


# ---------------------------------------------------------------- repetition


def repetition_issue(topic: Any, message: Any, signals: dict, ctx: Optional[dict]) -> Optional[str]:
    """Why this topic/message would repeat something recent, or None."""
    cooldown_h = (signals.get("limits") or {}).get("topic_cooldown_hours", gate.DEFAULTS["topic_cooldown_hours"])
    t_topic, t_msg = tokens(topic), tokens(message)
    for rec in signals.get("recent_proactive") or []:
        prev_text = tokens(rec.get("text"))
        if rec.get("topic"):
            same = jaccard(t_topic, tokens(rec["topic"])) >= SAME_TOPIC
        else:
            same = containment(t_topic, prev_text) >= SAME_TOPIC
        hours = rec.get("hours_ago") or 0
        if same and hours < cooldown_h:
            return f"topic was already raised {hours:.0f}h ago ({rec.get('id')}); topic cooldown is {cooldown_h}h"
        if same and rec.get("outcome") in ("unanswered", "dismissed") and hours < NO_REPLY_TOPIC_DAYS * 24:
            return f"topic was raised {hours / 24:.1f} days ago ({rec.get('id')}) and got no reply"
        if jaccard(t_msg, prev_text) >= NEAR_DUPLICATE:
            return f"near-duplicate of proactive message {rec.get('id')}"
    for mid, text in _assistant_texts(ctx):
        if jaccard(t_msg, tokens(text)) >= NEAR_DUPLICATE:
            return f"repeats earlier assistant message {mid}"
    return None


# ---------------------------------------------------------------- opportunity report


def check_candidate(c: Any, ids: Optional[set], signals: dict, ctx: Optional[dict]) -> tuple:
    """Return (errors, warnings) for one candidate."""
    if not isinstance(c, dict):
        return ["candidate must be an object"], []
    errors, warnings = [], []
    for key in ("id", "kind", "topic", "message", "rationale"):
        if not isinstance(c.get(key), str) or not c.get(key, "").strip():
            errors.append(f"'{key}' must be a non-empty string")
    if c.get("kind") not in KINDS:
        errors.append(f"'kind' must be one of {list(KINDS)}")
    evidence = c.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) for e in evidence):
        errors.append("'evidence' must list at least one context id")
    elif ids is not None:
        unknown = [e for e in evidence if e not in ids]
        if unknown:
            errors.append(f"evidence ids not found in context (possible fabrication): {unknown}")
    scores = c.get("scores")
    if not isinstance(scores, dict):
        errors.append("'scores' must be an object")
    else:
        for key in SCORE_KEYS:
            v = scores.get(key)
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
                errors.append(f"scores.{key} must be an integer 1-5")
    message = c.get("message")
    if isinstance(message, str) and message.strip():
        errors.extend(style_errors(message))
        warnings.extend(style_warnings(message, ctx))
        issue = repetition_issue(c.get("topic"), message, signals, ctx)
        if issue:
            errors.append(f"repetition: {issue}")
    return errors, warnings


def check_boundary(boundary: Any, ids: Optional[set]) -> list:
    if boundary is None:
        return []
    if not isinstance(boundary, dict):
        return ["'boundary' must be null or an object"]
    errs = []
    if boundary.get("type") not in BOUNDARY_TYPES:
        errs.append(f"boundary.type must be one of {list(BOUNDARY_TYPES)}")
    if not isinstance(boundary.get("note"), str) or not boundary["note"].strip():
        errs.append("boundary.note must explain the boundary")
    ev = boundary.get("evidence")
    if not isinstance(ev, list) or not ev or not all(isinstance(e, str) for e in ev):
        errs.append("boundary.evidence must cite the message(s) that set it")
    elif ids is not None and any(e not in ids for e in ev):
        errs.append(f"boundary evidence ids not found in context: {[e for e in ev if e not in ids]}")
    return errs


def validate_report(report: Any, ctx: Optional[dict] = None, signals: Optional[dict] = None) -> dict:
    """Check an opportunity report. candidate_errors maps candidate id -> errors."""
    result = {"valid": True, "errors": [], "candidate_errors": {}, "warnings": []}
    if not isinstance(report, dict):
        result.update(valid=False, errors=["report must be a JSON object"])
        return result
    if signals is None:
        signals = gate.evaluate(ctx)["signals"] if isinstance(ctx, dict) else {}
    ids = context_ids(ctx)
    result["errors"].extend(check_boundary(report.get("boundary"), ids))
    cands = report.get("candidates")
    if not isinstance(cands, list):
        result["errors"].append("'candidates' must be a list (may be empty)")
        cands = []
    if len(cands) > MAX_CANDIDATES:
        result["errors"].append(f"{len(cands)} candidates; generate at most {MAX_CANDIDATES}")
    seen_ids: set = set()
    accepted_topics: list = []
    for n, c in enumerate(cands[:MAX_CANDIDATES]):
        cid = str(c.get("id")) if isinstance(c, dict) and c.get("id") else f"#{n}"
        errors, warnings = check_candidate(c, ids, signals, ctx)
        if cid in seen_ids:
            errors.append(f"duplicate candidate id {cid}")
        seen_ids.add(cid)
        if isinstance(c, dict):
            for other_id, other_topic in accepted_topics:
                if same_topic(c.get("topic"), other_topic):
                    errors.append(f"same topic as {other_id}; candidates must cover different topics")
                    break
            accepted_topics.append((cid, c.get("topic")))
        if errors:
            result["candidate_errors"][cid] = errors
        result["warnings"].extend(f"{cid}: {w}" for w in warnings)
    kinds = {c.get("kind") for c in cands if isinstance(c, dict)}
    if len(cands) == MAX_CANDIDATES and len(kinds) == 1:
        result["warnings"].append("all candidates are the same kind; mix casual, intellectual and curious starters")
    result["valid"] = not result["errors"] and not result["candidate_errors"]
    return result


# ---------------------------------------------------------------- final decision


def validate(decision: Any, ctx: Optional[dict] = None, min_speak_confidence: Optional[float] = None) -> dict:
    """Check a final decision. With ctx, the gate is recomputed and evidence ids are checked."""
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
    ids = context_ids(ctx)
    if ids is not None:
        unknown = [e for e in evidence if e not in ids]
        if unknown:
            errors.append(f"evidence ids not found in context (possible fabrication): {unknown}")

    update = decision.get("preference_update")
    if update is not None:
        if not isinstance(update, dict) or not update:
            errors.append("'preference_update' must be null or a non-empty object")
        else:
            unknown_keys = set(update) - set(UPDATE_KEYS)
            if unknown_keys:
                errors.append(f"preference_update may only set {list(UPDATE_KEYS)}; got {sorted(unknown_keys)}")
            if "proactive_enabled" in update and update["proactive_enabled"] is not False:
                errors.append("preference_update may only switch proactive messages off, never on")
            for key in ("snooze_until", "pause_until_user_writes", "updated_at"):
                if key in update and gate._parse_iso(update[key]) is None:
                    errors.append(f"preference_update.{key} must be an ISO 8601 time")
        if kind == "SPEAK":
            errors.append("SPEAK cannot carry a preference_update (a stop or pause means WAIT)")

    cands = decision.get("candidates")
    if not isinstance(cands, list):
        errors.append("'candidates' must be a list")
        cands = []
    selected = decision.get("selected")
    message = decision.get("message")

    if kind == "WAIT":
        if message is not None:
            errors.append("'message' must be null when decision is WAIT")
        if selected is not None:
            errors.append("'selected' must be null when decision is WAIT")
    elif kind == "SPEAK":
        gate_result = gate.evaluate(ctx) if isinstance(ctx, dict) else None
        if gate_result and gate_result["gate"] == "BLOCK":
            errors.append(f"SPEAK although hard rule '{gate_result['blocked_by'][0]}' blocks")
        if decision.get("boundary") is not None:
            errors.append("SPEAK although a boundary was flagged")
        chosen = next((c for c in cands if isinstance(c, dict) and c.get("id") == selected), None)
        if chosen is None:
            errors.append("'selected' must name one of the candidates")
        elif chosen.get("message") != message:
            errors.append("'message' must be the selected candidate's message")
        if not evidence:
            errors.append("SPEAK requires at least one evidence id grounding the message")
        bar = min_speak_confidence
        if bar is None:
            signals = gate_result["signals"] if gate_result else {}
            bar = signals.get("speak_bar", gate.MODES[gate.DEFAULT_MODE]["speak_bar"])
        if conf is not None and conf < bar:
            errors.append(f"SPEAK with confidence {conf} is below the mode's bar {bar}")
        if not isinstance(message, str) or not message.strip():
            errors.append("'message' must be a non-empty string when decision is SPEAK")
        else:
            errors.extend(style_errors(message))
            warnings.extend(style_warnings(message, ctx))
            if gate_result:
                issue = repetition_issue(chosen.get("topic") if chosen else None, message, gate_result["signals"], ctx)
                if issue:
                    errors.append(f"repetition: {issue}")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _load(path: str) -> str:
    return sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a proactive-conversation decision or opportunity report.")
    parser.add_argument("item", help="decision or report JSON file, or '-' for stdin")
    parser.add_argument("--context", help="context JSON file; enables gate, grounding and repetition checks")
    parser.add_argument("--report", action="store_true", help="validate an opportunity report instead of a decision")
    parser.add_argument("--min-speak-confidence", type=float, default=None, help="override the mode's speak bar")
    args = parser.parse_args(argv)
    try:
        item, note = extract_json(_load(args.item))
        ctx = gate.unwrap(json.loads(_load(args.context))) if args.context else None
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [f"cannot read input: {exc}"], "warnings": []}))
        return 2
    result = validate_report(item, ctx) if args.report else validate(item, ctx, args.min_speak_confidence)
    if note:
        result["warnings"].insert(0, note)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
