#!/usr/bin/env python3
"""Deterministic hard-constraint gate for the proactive-conversation skill.

Answers one question with plain rules, no AI: is the assistant even *allowed*
to initiate a conversation right now? If not, it returns a final WAIT decision.
If yes, it returns PASS plus derived time signals (local time, "how long ago"
for every message and event) so the judgment step never has to do date math.

Usage:
    python3 gate.py context.json        # or "-" to read stdin
    python3 gate.py context.json --compact

Library use:
    from gate import evaluate
    result = evaluate(context_dict)

Standard library only (Python 3.9+). See references/input-schema.md for the
context format and the defaults applied below.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

DEFAULTS = {
    "quiet_hours": {"start": "22:00", "end": "08:00"},
    "max_per_day": 2,
    "min_gap_minutes": 240,
    "max_unanswered": 2,
    "dismissal_cooldown_hours": 24,
    "active_window_minutes": 15,
}

# Order matters: the first rule that fires is reported as the main reason.
RULE_ORDER = [
    "invalid_input",
    "not_opted_in",
    "disabled",
    "snoozed",
    "do_not_disturb",
    "quiet_hours",
    "conversation_active",
    "recent_dismissal",
    "unanswered_limit",
    "daily_limit",
    "min_gap",
]


# ---------------------------------------------------------------- helpers


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 string (naive or with offset). None if invalid."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"  # fromisoformat() < 3.11 rejects "Z"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_dt(value: Any, tz: tzinfo) -> Optional[datetime]:
    """Parse into an aware datetime expressed in `tz`; naive values are taken as `tz` wall time."""
    dt = _parse_iso(value)
    if dt is None:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def _parse_hhmm(value: Any) -> Optional[int]:
    """'22:30' -> minutes after midnight, or None."""
    if not isinstance(value, str):
        return None
    try:
        hours, minutes = value.strip().split(":")
        h, m = int(hours), int(minutes)
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _span(minutes: int) -> str:
    minutes = abs(minutes)
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        h, m = divmod(minutes, 60)
        return f"{h}h {m:02d}m"
    if minutes < 14 * 1440:
        d, rest = divmod(minutes, 1440)
        return f"{d}d {rest // 60}h"
    return f"{round(minutes / 1440)} days"


def _describe(dt: datetime, now: datetime) -> str:
    """Human, unambiguous relative time, e.g. 'yesterday, Mon 23 Nov 14:00 (28h 40m ago)'."""
    day_diff = (dt.date() - now.date()).days
    label = {0: "today", -1: "yesterday", 1: "tomorrow"}.get(day_diff)
    stamp = dt.strftime("%a %d %b %H:%M")
    when = f"{label}, {stamp}" if label else stamp
    delta = _minutes(now - dt)
    rel = f"{_span(delta)} ago" if delta >= 0 else f"in {_span(delta)}"
    return f"{when} ({rel})"


def _in_window(minute_of_day: int, start: int, end: int) -> bool:
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end  # overnight window


def _minutes_until(minute_of_day: int, target: int) -> int:
    return (target - minute_of_day) % 1440


def _int_pref(prefs: dict, key: str, warnings: list) -> int:
    value = prefs.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        warnings.append(f"preferences.{key} is invalid ({value!r}); using default {DEFAULTS[key]}.")
        return DEFAULTS[key]
    return int(value)


def _wait(reason: str) -> dict:
    return {"decision": "WAIT", "reason": reason, "message": None, "confidence": 1.0, "evidence": []}


def _result(blocks: list, signals: dict, warnings: list) -> dict:
    blocks.sort(key=lambda b: RULE_ORDER.index(b[0]))
    if blocks:
        rule, reason = blocks[0]
        return {
            "gate": "BLOCK",
            "blocked_by": [b[0] for b in blocks],
            "decision": _wait(f"Hard rule '{rule}': {reason}"),
            "signals": signals,
            "warnings": warnings,
        }
    return {"gate": "PASS", "blocked_by": [], "decision": None, "signals": signals, "warnings": warnings}


# ---------------------------------------------------------------- main logic


def evaluate(ctx: Any) -> dict:
    """Apply hard constraints to a context dict. Never raises on bad input."""
    warnings: list = []
    blocks: list = []  # (rule, human reason)
    signals: dict = {}

    if not isinstance(ctx, dict):
        return _result([("invalid_input", "context must be a JSON object.")], signals, warnings)

    # --- time base -------------------------------------------------------
    tz_name = ctx.get("timezone")
    tz: Optional[tzinfo] = None
    if tz_name:
        if ZoneInfo is None:
            warnings.append("zoneinfo unavailable; ignoring timezone.")
        else:
            try:
                tz = ZoneInfo(str(tz_name).strip())
            except (ZoneInfoNotFoundError, ValueError, OSError):
                return _result(
                    [("invalid_input", f"unknown timezone {tz_name!r}; cannot evaluate timing safely.")],
                    signals,
                    warnings,
                )

    raw_now = _parse_iso(ctx.get("now"))
    if raw_now is None:
        return _result(
            [("invalid_input", "'now' is missing or not ISO 8601; timing cannot be evaluated, so stay silent.")],
            signals,
            warnings,
        )
    if tz is None:
        if raw_now.tzinfo is not None:
            tz = raw_now.tzinfo  # fixed offset taken from 'now'
        else:
            tz = timezone.utc
            warnings.append("No timezone and no UTC offset on 'now'; treating all times as the user's local clock.")
    now = raw_now.replace(tzinfo=tz) if raw_now.tzinfo is None else raw_now.astimezone(tz)
    minute_of_day = now.hour * 60 + now.minute
    signals["local_time"] = now.strftime("%a %d %b %Y %H:%M") + (f" ({tz_name})" if tz_name else "")

    # --- preferences -----------------------------------------------------
    prefs = ctx.get("preferences")
    if not isinstance(prefs, dict):
        prefs = {}
    enabled = prefs.get("proactive_enabled")
    if enabled is None:
        blocks.append(("not_opted_in", "the user has not opted in to proactive messages (preferences.proactive_enabled is missing)."))
    elif enabled is not True:
        blocks.append(("disabled", "the user has turned proactive messages off."))

    snooze = prefs.get("snooze_until")
    if snooze is not None:
        snooze_dt = _parse_dt(snooze, tz)
        if snooze_dt is None:
            warnings.append("preferences.snooze_until is not ISO 8601; ignored.")
        elif now < snooze_dt:
            blocks.append(("snoozed", f"proactive messages are snoozed until {_describe(snooze_dt, now)}."))

    status = ctx.get("user_status") if isinstance(ctx.get("user_status"), dict) else {}
    if status.get("do_not_disturb") is True:
        blocks.append(("do_not_disturb", "the user's device or app reports Do Not Disturb."))

    # Quiet hours: absent -> conservative default; explicit null -> none.
    if "quiet_hours" in prefs:
        quiet = prefs["quiet_hours"]
    else:
        quiet = DEFAULTS["quiet_hours"]
        warnings.append("preferences.quiet_hours not set; using default 22:00-08:00.")
    signals["quiet_hours"] = None
    signals["minutes_until_quiet_hours"] = None
    if quiet is not None:
        start = _parse_hhmm(quiet.get("start")) if isinstance(quiet, dict) else None
        end = _parse_hhmm(quiet.get("end")) if isinstance(quiet, dict) else None
        if start is None or end is None:
            quiet = DEFAULTS["quiet_hours"]
            start, end = _parse_hhmm(quiet["start"]), _parse_hhmm(quiet["end"])
            warnings.append("preferences.quiet_hours is malformed; using default 22:00-08:00.")
        if start == end:
            warnings.append("quiet_hours start equals end; treated as no quiet hours.")
        else:
            label = f"{quiet['start']}-{quiet['end']}"
            signals["quiet_hours"] = label
            if _in_window(minute_of_day, start, end):
                blocks.append(("quiet_hours", f"quiet hours ({label}) are in effect; local time is {now.strftime('%H:%M')}."))
            else:
                signals["minutes_until_quiet_hours"] = _minutes_until(minute_of_day, start)

    max_per_day = _int_pref(prefs, "max_per_day", warnings)
    min_gap = _int_pref(prefs, "min_gap_minutes", warnings)
    max_unanswered = _int_pref(prefs, "max_unanswered", warnings)
    cooldown_h = _int_pref(prefs, "dismissal_cooldown_hours", warnings)
    active_window = _int_pref(prefs, "active_window_minutes", warnings)
    signals["limits"] = {
        "max_per_day": max_per_day,
        "min_gap_minutes": min_gap,
        "max_unanswered": max_unanswered,
        "dismissal_cooldown_hours": cooldown_h,
        "active_window_minutes": active_window,
    }

    # --- history ---------------------------------------------------------
    history = ctx.get("history") if isinstance(ctx.get("history"), list) else []
    if "history" in ctx and not isinstance(ctx.get("history"), list):
        warnings.append("history is not a list; ignored.")
    times: dict = {}
    msgs = []
    for i, msg in enumerate(history):
        if not isinstance(msg, dict):
            warnings.append(f"history[{i}] is not an object; ignored.")
            continue
        mid = str(msg.get("id", f"#{i}"))
        ts = _parse_dt(msg.get("timestamp"), tz)
        if ts is None:
            warnings.append(f"message {mid} has no valid timestamp; excluded from timing rules.")
            continue
        if ts > now + timedelta(minutes=1):
            warnings.append(f"message {mid} is timestamped in the future; excluded from timing rules.")
            continue
        times[mid] = _describe(ts, now)
        msgs.append((ts, mid, msg))
    msgs.sort(key=lambda m: m[0])

    user_msgs = [m for m in msgs if m[2].get("role") == "user"]
    proactive = [m for m in msgs if m[2].get("role") == "assistant" and m[2].get("proactive") is True]
    last_any = msgs[-1][0] if msgs else None
    last_user = user_msgs[-1][0] if user_msgs else None

    signals["message_count"] = len(msgs)
    signals["minutes_since_last_message"] = _minutes(now - last_any) if last_any else None
    signals["minutes_since_last_user_message"] = _minutes(now - last_user) if last_user else None

    if last_any is not None and _minutes(now - last_any) < active_window:
        blocks.append(
            ("conversation_active", f"the last message was {_minutes(now - last_any)} min ago; this is an ongoing conversation, not an initiation.")
        )

    recent_dismissals = [
        m for m in proactive if m[2].get("feedback") == "dismissed" and now - m[0] < timedelta(hours=cooldown_h)
    ]
    if recent_dismissals:
        blocks.append(
            ("recent_dismissal", f"the user dismissed a proactive message {_span(_minutes(now - recent_dismissals[-1][0]))} ago (cooldown {cooldown_h}h).")
        )
    signals["dismissals_last_7_days"] = sum(
        1 for m in proactive if m[2].get("feedback") == "dismissed" and now - m[0] < timedelta(days=7)
    )

    unanswered = [m for m in proactive if last_user is None or m[0] > last_user]
    signals["unanswered_proactive_streak"] = len(unanswered)
    if max_unanswered and len(unanswered) >= max_unanswered:
        blocks.append(
            ("unanswered_limit", f"{len(unanswered)} proactive messages in a row went unanswered; wait until the user writes again.")
        )

    today = [m for m in proactive if m[0].date() == now.date()]
    signals["proactive_sent_today"] = len(today)
    if len(today) >= max_per_day:
        blocks.append(("daily_limit", f"{len(today)} proactive message(s) already sent today (limit {max_per_day})."))

    last_pro = proactive[-1][0] if proactive else None
    signals["minutes_since_last_proactive"] = _minutes(now - last_pro) if last_pro else None
    if last_pro is not None and _minutes(now - last_pro) < min_gap:
        blocks.append(
            ("min_gap", f"the last proactive message was {_span(_minutes(now - last_pro))} ago (minimum gap {min_gap} min).")
        )

    # --- events & memories: timing annotations only ---------------------
    events = ctx.get("events") if isinstance(ctx.get("events"), list) else []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("id", f"event#{i}"))
        start_dt = _parse_dt(ev.get("start"), tz)
        end_dt = _parse_dt(ev.get("end"), tz)
        if start_dt is None:
            times[eid] = "time unknown"
            continue
        desc = "starts " + _describe(start_dt, now)
        if end_dt is not None and start_dt <= now < end_dt:
            desc = "ongoing since " + _describe(start_dt, now)
        elif end_dt is not None and now >= end_dt:
            desc = "ended " + _describe(end_dt, now)
        elif now >= start_dt:
            desc = "started " + _describe(start_dt, now)
        times[eid] = desc

    memories = ctx.get("memories") if isinstance(ctx.get("memories"), list) else []
    for i, mem in enumerate(memories):
        if isinstance(mem, dict) and mem.get("recorded_at"):
            rec = _parse_dt(mem.get("recorded_at"), tz)
            if rec is not None:
                times[str(mem.get("id", f"memory#{i}"))] = "recorded " + _describe(rec, now)

    signals["times"] = times
    return _result(blocks, signals, warnings)


def unwrap(obj: Any) -> Any:
    """Accept a bare context, a {"context": ...} request, or a test scenario {"input": ...}."""
    if isinstance(obj, dict) and "now" not in obj:
        for key in ("context", "input"):
            if isinstance(obj.get(key), dict):
                return obj[key]
    return obj


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Hard-constraint gate for proactive messages.")
    parser.add_argument("context", help="context JSON (bare, or wrapped as {\"context\": ...}); '-' for stdin")
    parser.add_argument("--compact", action="store_true", help="single-line JSON output")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.context == "-" else open(args.context, encoding="utf-8").read()
        ctx = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read context: {exc}"}), file=sys.stderr)
        return 2
    result = evaluate(unwrap(ctx))
    print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
