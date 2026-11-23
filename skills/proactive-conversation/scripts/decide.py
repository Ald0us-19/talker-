#!/usr/bin/env python3
"""Decision engine for the proactive-conversation skill.

Turns the model's opportunity report (up to three scored conversation starters)
into the final SPEAK/WAIT decision, with a fixed, inspectable policy:

  1. Recompute the hard gate from the context. A gate result supplied by the
     caller is never trusted. BLOCK -> WAIT.
  2. A boundary flagged by the generator (break request, focus time) -> WAIT.
     A requested break also returns a preference_update for the host to persist
     (pause until the user writes again), so it does not depend on the model
     noticing the request again next time.
  3. Reject candidates that fail validation: ungrounded, repetitive, same topic
     as another candidate, or bad style.
  4. Veto candidates with hopeless timing, forced naturalness, or maximal
     interruption cost.
  5. Score the rest: weighted criteria minus interruption cost, minus pacing
     (share of today's budget already used), spacing (the last proactive
     message was recent compared with an even spread over the waking day) and
     backoff (unanswered streak).
  6. SPEAK with the best candidate if it clears the mode's bar, otherwise WAIT.

Silence is not preferred by default: any candidate that clears the bar is sent.
The mode's limits are ceilings, not targets: pacing and spacing make each extra
message need a better reason, so messages spread out instead of bunching at the
minimum gap.

Usage:
    python3 decide.py report.json --context context.json
    cat report.json | python3 decide.py - --context context.json

Library:
    from decide import decide
    decision = decide(context_dict, report_dict)
Standard library only.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402
import validate  # noqa: E402

# Policy knobs. Criteria are 1-5; each is mapped to 0-1 before weighting.
WEIGHTS = {"relevance": 0.25, "novelty": 0.20, "naturalness": 0.20, "timing": 0.15, "engagement": 0.20}
COST_WEIGHT = 0.40  # interruption_cost 5 would subtract 0.40
PACING_WEIGHT = 0.30  # x share of today's budget already used
SPACING_WEIGHT = 0.70  # x how far inside the natural spacing (waking minutes / max per day) we are
BACKOFF_PER_UNANSWERED = 0.10  # per proactive message in a row without a reply
VETOES = [
    ("timing", lambda v: v <= 1, "timing is wrong right now"),
    ("naturalness", lambda v: v <= 2, "would feel forced or artificial"),
    ("interruption_cost", lambda v: v >= 5, "too disruptive at this moment"),
]
CANDIDATE_FIELDS = ("id", "kind", "topic", "message", "rationale", "evidence", "scores")


def base_score(scores: dict) -> float:
    value = sum(w * (scores[k] - 1) / 4 for k, w in WEIGHTS.items())
    return value - COST_WEIGHT * (scores["interruption_cost"] - 1) / 4


def penalties(signals: dict) -> dict:
    """Deterministic adjustments that depend on recent behaviour, not on the candidate."""
    limit = (signals.get("limits") or {}).get("max_per_day") or 0
    out = {"pacing": 0.0, "spacing": 0.0, "backoff": 0.0}
    if limit:
        out["pacing"] = PACING_WEIGHT * signals.get("proactive_sent_today", 0) / limit
        natural_gap = signals.get("waking_minutes", 1440) / limit
        since = signals.get("minutes_since_last_proactive")
        if since is not None and natural_gap > 0:
            out["spacing"] = SPACING_WEIGHT * max(0.0, 1 - since / natural_gap)
    out["backoff"] = BACKOFF_PER_UNANSWERED * signals.get("unanswered_proactive_streak", 0)
    return {k: round(v, 3) for k, v in out.items()}


def _decision(kind: str, reason: str, confidence: float, *, message=None, evidence=None,
              selected=None, boundary=None, candidates=None, preference_update=None) -> dict:
    reason = reason if len(reason) <= validate.MAX_REASON_CHARS else reason[: validate.MAX_REASON_CHARS - 1] + "…"
    return {
        "decision": kind,
        "reason": reason,
        "message": message,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "evidence": evidence or [],
        "selected": selected,
        "boundary": boundary,
        "candidates": candidates or [],
        "preference_update": preference_update,
    }


def decide(ctx: Any, report: Any) -> dict:
    """Final decision for one context snapshot and one opportunity report. Never raises."""
    g = gate.evaluate(ctx)  # always recomputed; never taken from the caller
    if g["gate"] == "BLOCK":
        d = g["decision"]
        return _decision("WAIT", d["reason"], d["confidence"], evidence=d["evidence"],
                         preference_update=d["preference_update"])
    signals = g["signals"]
    mode, bar = signals["mode"], signals["speak_bar"]

    if not isinstance(report, dict):
        return _decision("WAIT", "Fail-closed: the opportunity report is not a JSON object.", 1.0)
    check = validate.validate_report(report, ctx, signals)

    boundary = report.get("boundary")
    if boundary is not None:
        if validate.check_boundary(boundary, validate.context_ids(ctx)):
            boundary = {"type": "other", "note": "malformed boundary flag", "evidence": []}
        note = str(boundary.get("note", "")).strip().rstrip(".")
        update = None
        if boundary.get("type") == "requested_break":
            update = {"pause_until_user_writes": signals["now"], "updated_at": signals["now"]}
            note += ". Paused until the user writes again; the host must persist preference_update"
        return _decision(
            "WAIT",
            f"Boundary ({boundary.get('type')}): {note}.",
            0.95,
            evidence=[e for e in boundary.get("evidence", []) if isinstance(e, str)],
            boundary=boundary,
            preference_update=update,
        )

    if not isinstance(report.get("candidates"), list):
        return _decision("WAIT", "Fail-closed: the opportunity report has no candidates list.", 1.0)

    adjust = penalties(signals)
    penalty = sum(adjust.values())
    scored = []
    for n, raw in enumerate(report["candidates"][: validate.MAX_CANDIDATES]):
        c = {k: copy.deepcopy(raw.get(k)) for k in CANDIDATE_FIELDS} if isinstance(raw, dict) else {"id": f"#{n}"}
        cid = str(c.get("id") or f"#{n}")
        errors = check["candidate_errors"].get(cid)
        if errors:
            c.update(score=0.0, status="rejected", note=errors[0])
        else:
            veto = next((why for key, test, why in VETOES if test(c["scores"][key])), None)
            s = round(base_score(c["scores"]) - penalty, 3)
            if veto:
                c.update(score=0.0, status="rejected", note=veto)
            else:
                c.update(score=s, status="eligible" if s >= bar else "below_bar",
                         note=None if s >= bar else f"score {s:.2f} below the {mode} bar {bar:.2f}")
        scored.append(c)

    eligible = [c for c in scored if c["status"] == "eligible"]
    if eligible:
        best = max(eligible, key=lambda c: c["score"])  # max() keeps the earliest on ties
        best["status"], best["note"] = "selected", None
        applied = ", ".join(f"{k} -{v:.2f}" for k, v in adjust.items() if v)
        reason = (f"Picked {best['id']} ({best['kind']}: {best['topic']}), score {best['score']:.2f}"
                  f"{f' (after {applied})' if applied else ''} >= {bar:.2f} in {mode} mode. {best['rationale']}")
        return _decision("SPEAK", reason, best["score"], message=best["message"],
                         evidence=list(best["evidence"]), selected=best["id"], candidates=scored)

    if not scored:
        return _decision("WAIT", "No grounded conversation starter was found in the context.", 0.9)
    ranked = sorted(scored, key=lambda c: c["score"], reverse=True)
    top = ranked[0]
    rejected = [f"{c['id']} ({c['note']})" for c in scored if c["status"] == "rejected"]
    if top["status"] == "below_bar":
        reason = f"No candidate cleared the {mode} bar ({bar:.2f}); best was {top['id']} '{top.get('topic')}' at {top['score']:.2f}."
        confidence = 1 - top["score"]
    else:
        reason = "Every candidate was rejected."
        confidence = 0.8
    if rejected:
        reason += " Rejected: " + "; ".join(rejected)
    return _decision("WAIT", reason, max(0.5, min(0.95, confidence)), candidates=scored)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Turn an opportunity report into a SPEAK/WAIT decision.")
    parser.add_argument("report", help="opportunity report JSON (or a decision containing candidates); '-' for stdin")
    parser.add_argument("--context", required=True, help="context JSON (bare, or wrapped as {\"context\": ...})")
    parser.add_argument("--compact", action="store_true", help="single-line JSON output")
    args = parser.parse_args(argv)
    try:
        raw_report = sys.stdin.read() if args.report == "-" else open(args.report, encoding="utf-8").read()
        report, note = validate.extract_json(raw_report)
        ctx_obj = json.loads(open(args.context, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read input: {exc}"}), file=sys.stderr)
        return 2
    if isinstance(ctx_obj, dict) and "gate" in ctx_obj:
        print("note: ignoring the supplied 'gate'; the gate is always recomputed.", file=sys.stderr)
    if note:
        print(f"note: {note}", file=sys.stderr)
    decision = decide(gate.unwrap(ctx_obj), report)
    print(json.dumps(decision, ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
