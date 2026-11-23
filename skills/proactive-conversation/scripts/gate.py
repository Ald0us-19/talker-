#!/usr/bin/env python3
"""Deterministic hard-constraint gate for the proactive-conversation skill.

Answers one question with plain rules, no AI: is the assistant even *allowed*
to initiate a conversation right now? If not, it returns a final WAIT decision.
If yes, it returns PASS plus derived signals so the judgment step never has to
do date math: local time, "how long ago" for every message and event, the
proactivity mode and its limits, remaining budget, engagement with earlier
proactive messages, and what was recently raised (to avoid repetition).

Rate limits and cooldowns are computed from the host's delivery_log (the
authoritative record of sent proactive messages) merged with history. Anything
unreadable is interpreted in the stricter direction (fail closed).

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
import re
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

# Proactivity modes. Limits are ceilings, not targets; speak_bar is the minimum
# candidate score decide.py needs before it speaks in that mode.
DEFAULT_MODE = "balanced"
FALLBACK_MODE = "quiet"  # used when the stored mode is unreadable (fail closed)
MODES = {
    "quiet": {"max_per_day": 2, "min_gap_minutes": 240, "speak_bar": 0.65},
    "balanced": {"max_per_day": 4, "min_gap_minutes": 90, "speak_bar": 0.50},
    "lively": {"max_per_day": 8, "min_gap_minutes": 30, "speak_bar": 0.40},
}

DEFAULTS = {
    "quiet_hours": {"start": "22:00", "end": "08:00"},
    "max_unanswered": 2,
    "dismissal_cooldown_hours": 24,
    "active_window_minutes": 15,
    "topic_cooldown_hours": 48,
}
RECENT_DAYS = 14  # how far back recent proactive messages are summarised
RECENT_MAX = 20  # ...and at most this many of them

# Explicit requests to stop proactive contact, matched deterministically in user
# messages newer than preferences.updated_at. A match blocks immediately and the
# gate returns a preference_update for the host to persist, so the stop keeps
# holding after the message leaves the history. Paraphrases are left to the
# model's boundary flag (a second line of defence, also persisted).
STOP_PATTERNS = [
    r"\b(?:please\s+)?(?:don'?t|do\s+not|stop|quit)\s+(?:messag\w*|text\w*|ping\w*|contact\w*|notify\w*|bother\w*|writ\w*\s+to)\s+me\b",
    r"\b(?:stop|quit|no\s+more)\s+(?:the\s+|your\s+)?(?:proactive\s+)?(?:messages|notifications|check-?ins|texts|reminders)\b",
    r"\b(?:turn|switch|shut)\s+off\s+(?:the\s+|your\s+)?(?:proactive\s+)?(?:messages|notifications|check-?ins)\b",
    r"\bpause\s+(?:the\s+|your\s+)?(?:messages|notifications|check-?ins)\b",
    r"\bleave\s+me\s+alone\b",
    r"\b(?:schreib|schick|meld)\w*\s+(?:mir|dich)\s+(?:bitte\s+)?(?:nicht|keine|nichts)\b",
    r"\bkeine\s+nachrichten\s+mehr\b",
    r"\blass\s+mich\s+in\s+ruhe\b",
]
NEGATIONS = re.compile(r"(?:don'?t|do not|never|nicht)\s+$")
UNTIL_USER = re.compile(
    r"\buntil\s+i\s+(?:reach\s+out|message\s+you|text\s+you|write(?:\s+to\s+you)?|get\s+back(?:\s+to\s+you)?|say\s+so"
    r"|(?:am|'m)\s+(?:back|ready)|come\s+back|contact\s+you)\b|\bbis\s+ich\s+(?:mich\s+(?:wieder\s+)?melde|wieder\s+schreibe)\b")
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}
UNTIL_WEEKDAY = re.compile(r"\b(?:until|till|til|bis)\s+(?:next\s+)?(" + "|".join(WEEKDAYS) + r")\b")
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "a few": 3, "a couple of": 2, "a couple": 2}
FOR_PERIOD = re.compile(r"\bfor\s+(a\s+couple\s+of|a\s+couple|a\s+few|an?|one|two|three|four|five|six|seven|\d+)\s+(day|days|week|weeks)\b")
USER_RELEASE_MINUTES = 30  # a user message this long after an "until I reach out" request ends it

# Order matters: the first rule that fires is reported as the main reason.
RULE_ORDER = [
    "invalid_input",
    "not_opted_in",
    "disabled",
    "stop_requested",
    "paused_until_user_writes",
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


def _int_pref(prefs: dict, key: str, default: int, warnings: list) -> int:
    value = prefs.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        warnings.append(f"preferences.{key} is invalid ({value!r}); using {default}.")
        return default
    return int(value)


def resolve_mode(prefs: dict, warnings: list) -> str:
    """Mode name from preferences: missing -> balanced, unreadable -> quiet."""
    mode = prefs.get("mode")
    if mode is None:
        return DEFAULT_MODE
    if isinstance(mode, str) and mode.strip().lower() in MODES:
        return mode.strip().lower()
    warnings.append(f"preferences.mode {mode!r} is not quiet/balanced/lively; using '{FALLBACK_MODE}'.")
    return FALLBACK_MODE


def _proactive_records(history: list, log: list, tz: tzinfo, now: datetime, warnings: list) -> list:
    """Every proactive message the host knows about, from delivery_log and history.

    delivery_log is the authoritative, append-only record kept by the host; history
    entries marked "proactive": true are merged in (deduplicated by message id) so
    a truncated history cannot hide sends, and a missing log still works. Entries
    with unreadable send times count as sent *now*, which makes the limits stricter,
    never looser.
    """
    records: dict = {}

    def add(key: str, entry: dict, sent_raw: Any, source: str) -> None:
        rec = records.get(key)
        if rec is None:  # first source wins for the send time; the log is read first
            sent = _parse_dt(sent_raw, tz)
            if sent is None:
                warnings.append(f"proactive message {key} has no valid send time; counted as sent now.")
                sent = now
            rec = records[key] = {"key": key, "sent_at": sent, "topic": None, "text": None,
                                  "replied_at": None, "dismissed_at": None, "sources": []}
        sent = rec["sent_at"]
        rec["sources"].append(source)
        rec["topic"] = rec["topic"] or (entry.get("topic") if isinstance(entry.get("topic"), str) else None)
        rec["text"] = rec["text"] or (entry.get("text") if isinstance(entry.get("text"), str) else None)

        replied = entry.get("replied_at")
        if replied is not None and rec["replied_at"] is None:
            rec["replied_at"] = _parse_dt(replied, tz)
            if rec["replied_at"] is None:
                warnings.append(f"proactive message {key}: replied_at is malformed; treated as unanswered.")

        dismissed_raw = entry.get("dismissed_at")
        if dismissed_raw is not None or entry.get("feedback") == "dismissed":
            if dismissed_raw is None:
                when = sent
                warnings.append(f"proactive message {key} was dismissed without dismissed_at; using its send time.")
            else:
                when = _parse_dt(dismissed_raw, tz)
                if when is None:
                    warnings.append(f"proactive message {key}: dismissed_at is malformed; treated as dismissed now.")
                    when = now
            if rec["dismissed_at"] is None or when > rec["dismissed_at"]:
                rec["dismissed_at"] = when

    for i, entry in enumerate(log):
        if not isinstance(entry, dict):
            warnings.append(f"delivery_log[{i}] is not an object; ignored.")
            continue
        key = str(entry.get("message_id") or entry.get("id") or f"log#{i}")
        add(key, entry, entry.get("sent_at"), "delivery_log")
    for i, msg in enumerate(history):
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("proactive") is True:
            add(str(msg.get("id", f"#{i}")), msg, msg.get("timestamp"), "history")
    return sorted(records.values(), key=lambda r: r["sent_at"])


def _wait(reason: str, preference_update: Optional[dict] = None, evidence: Optional[list] = None) -> dict:
    """A final decision in the full output contract (see references/output-schema.json)."""
    return {"decision": "WAIT", "reason": reason, "message": None, "confidence": 1.0, "evidence": evidence or [],
            "selected": None, "boundary": None, "candidates": [], "preference_update": preference_update}


def _result(blocks: list, signals: dict, warnings: list, stop: Optional[dict] = None) -> dict:
    blocks.sort(key=lambda b: RULE_ORDER.index(b[0]))
    update = stop["preference_update"] if stop else None
    if blocks:
        rule, reason = blocks[0]
        evidence = [stop["message_id"]] if stop else []
        return {
            "gate": "BLOCK",
            "blocked_by": [b[0] for b in blocks],
            "decision": _wait(f"Hard rule '{rule}': {reason}", update, evidence),
            "preference_update": update,
            "signals": signals,
            "warnings": warnings,
        }
    return {"gate": "PASS", "blocked_by": [], "decision": None, "preference_update": None,
            "signals": signals, "warnings": warnings}


def find_stop_request(text: Any) -> bool:
    """True if the text explicitly asks the assistant to stop (or pause) proactive contact."""
    if not isinstance(text, str):
        return False
    low = text.lower().replace("\u2019", "'")
    for pattern in STOP_PATTERNS:
        for m in re.finditer(pattern, low):
            if not NEGATIONS.search(low[max(0, m.start() - 12) : m.start()]):
                return True
    return False


def stop_duration(text: str, said_at: datetime, resume_minute: int) -> tuple:
    """How long a stop request lasts: ("user_writes", None) | ("until", datetime) | ("indefinite", None)."""
    low = text.lower().replace("\u2019", "'")

    def resume_on(day) -> datetime:
        return datetime(day.year, day.month, day.day, resume_minute // 60, resume_minute % 60, tzinfo=said_at.tzinfo)

    if UNTIL_USER.search(low):
        return "user_writes", None
    m = UNTIL_WEEKDAY.search(low)
    if m:
        ahead = (WEEKDAYS[m.group(1)] - said_at.weekday()) % 7 or 7
        return "until", resume_on(said_at.date() + timedelta(days=ahead))
    if re.search(r"\b(?:until|till|bis)\s+(?:tomorrow|morgen)\b", low):
        return "until", resume_on(said_at.date() + timedelta(days=1))
    if re.search(r"\b(?:until|till)\s+next\s+week\b|\b(?:this|the|over\s+the)\s+weekend\b|\bam\s+wochenende\b", low):
        return "until", resume_on(said_at.date() + timedelta(days=(7 - said_at.weekday()) or 7))
    m = FOR_PERIOD.search(low)
    if m:
        word = re.sub(r"\s+", " ", m.group(1))
        n = int(word) if word.isdigit() else NUMBERS.get(word, 1)
        days = n * (7 if m.group(2).startswith("week") else 1)
        return "until", resume_on(said_at.date() + timedelta(days=days))
    if re.search(r"\b(?:today|tonight|heute)\b", low):
        return "until", resume_on(said_at.date() + timedelta(days=1))
    return "indefinite", None


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
    signals["now"] = now.isoformat()

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
            # The user asked for a pause; an unreadable end time must not end it.
            blocks.append(("snoozed", f"preferences.snooze_until is set but unreadable ({snooze!r}); treated as snoozed until the host fixes it."))
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
    signals["waking_minutes"] = 1440
    resume_minute = 8 * 60  # when a time-bound stop request ends, if there are no quiet hours
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
            signals["waking_minutes"] = 1440 - (end - start) % 1440
            resume_minute = end
            if _in_window(minute_of_day, start, end):
                blocks.append(("quiet_hours", f"quiet hours ({label}) are in effect; local time is {now.strftime('%H:%M')}."))
            else:
                signals["minutes_until_quiet_hours"] = _minutes_until(minute_of_day, start)

    mode = resolve_mode(prefs, warnings)
    preset = MODES[mode]
    # Explicit numeric preferences are the user's own limits and override the preset.
    max_per_day = _int_pref(prefs, "max_per_day", preset["max_per_day"], warnings)
    min_gap = _int_pref(prefs, "min_gap_minutes", preset["min_gap_minutes"], warnings)
    max_unanswered = _int_pref(prefs, "max_unanswered", DEFAULTS["max_unanswered"], warnings)
    cooldown_h = _int_pref(prefs, "dismissal_cooldown_hours", DEFAULTS["dismissal_cooldown_hours"], warnings)
    active_window = _int_pref(prefs, "active_window_minutes", DEFAULTS["active_window_minutes"], warnings)
    topic_cooldown_h = _int_pref(prefs, "topic_cooldown_hours", DEFAULTS["topic_cooldown_hours"], warnings)
    signals["mode"] = mode
    signals["speak_bar"] = preset["speak_bar"]
    signals["limits"] = {
        "max_per_day": max_per_day,
        "min_gap_minutes": min_gap,
        "max_unanswered": max_unanswered,
        "dismissal_cooldown_hours": cooldown_h,
        "active_window_minutes": active_window,
        "topic_cooldown_hours": topic_cooldown_h,
    }

    # --- history ---------------------------------------------------------
    history = ctx.get("history") if isinstance(ctx.get("history"), list) else []
    if "history" in ctx and not isinstance(ctx.get("history"), list):
        warnings.append("history is not a list; ignored.")
    log = ctx.get("delivery_log") if isinstance(ctx.get("delivery_log"), list) else []
    if "delivery_log" in ctx and not isinstance(ctx.get("delivery_log"), list):
        warnings.append("delivery_log is not a list; ignored.")
    times: dict = {}
    msgs = []
    for i, msg in enumerate(history):
        if not isinstance(msg, dict):
            warnings.append(f"history[{i}] is not an object; ignored.")
            continue
        mid = str(msg.get("id", f"#{i}"))
        ts = _parse_dt(msg.get("timestamp"), tz)
        if ts is None:
            warnings.append(f"message {mid} has no valid timestamp; excluded from the timeline.")
            continue
        if ts > now + timedelta(minutes=1):
            warnings.append(f"message {mid} is timestamped in the future; excluded from the timeline.")
            continue
        times[mid] = _describe(ts, now)
        msgs.append((ts, mid, msg))
    msgs.sort(key=lambda m: m[0])

    user_msgs = [m for m in msgs if m[2].get("role") == "user"]
    last_any = msgs[-1][0] if msgs else None
    last_user = user_msgs[-1][0] if user_msgs else None

    signals["message_count"] = len(msgs)
    signals["minutes_since_last_message"] = _minutes(now - last_any) if last_any else None
    signals["minutes_since_last_user_message"] = _minutes(now - last_user) if last_user else None

    if last_any is not None and _minutes(now - last_any) < active_window:
        blocks.append(
            ("conversation_active", f"the last message was {_minutes(now - last_any)} min ago; this is an ongoing conversation, not an initiation.")
        )

    # --- explicit stop requests and the persisted pause ----------------
    def user_wrote_after(t: datetime) -> bool:
        return any(m[0] >= t + timedelta(minutes=USER_RELEASE_MINUTES) for m in user_msgs)

    stop = None
    updated_raw = prefs.get("updated_at")
    updated_at = _parse_dt(updated_raw, tz) if updated_raw is not None else None
    if updated_raw is not None and updated_at is None:
        warnings.append("preferences.updated_at is unreadable; scanning all history for stop requests.")
    for ts, mid, msg in reversed(user_msgs):
        if updated_at is not None and ts <= updated_at:
            break
        if not find_stop_request(msg.get("text")):
            continue
        kind, until = stop_duration(msg["text"], ts, resume_minute)
        if kind == "user_writes" and user_wrote_after(ts):
            break  # the user has already reached out again
        if kind == "until" and now >= until:
            break  # the requested pause is over
        update = {"updated_at": now.isoformat()}
        if kind == "user_writes":
            update["pause_until_user_writes"] = ts.isoformat()
            lasts = "until the user writes again"
        elif kind == "until":
            update["snooze_until"] = until.isoformat()
            lasts = f"until {_describe(until, now)}"
        else:
            update["proactive_enabled"] = False
            lasts = "until the user turns proactive messages back on"
        stop = {"message_id": mid, "preference_update": update}
        blocks.append(("stop_requested", f"the user asked to stop proactive messages in {mid}; paused {lasts}. "
                                         "The host must persist preference_update."))
        break

    pause_raw = prefs.get("pause_until_user_writes")
    if pause_raw is not None:
        since = _parse_dt(pause_raw, tz)
        if since is None:
            blocks.append(("paused_until_user_writes", f"preferences.pause_until_user_writes is unreadable ({pause_raw!r}); treated as paused."))
        elif not user_wrote_after(since):
            blocks.append(("paused_until_user_writes", f"the user asked for no proactive messages until they write again (asked {_describe(since, now)})."))

    # --- proactive history: delivery_log (authoritative) + history ------
    proactive = _proactive_records(history, log, tz, now, warnings)
    if not log:
        warnings.append("No delivery_log: rate limits are counted from history only and undercount if history is truncated.")
    signals["rate_limit_source"] = "+".join(s for s in ("delivery_log", "history") if any(s in r["sources"] for r in proactive)) or "none"

    def answered(rec: dict) -> bool:
        if rec["replied_at"] is not None and rec["replied_at"] <= now:
            return True
        return last_user is not None and last_user > rec["sent_at"]

    def outcome(rec: dict) -> str:
        if rec["dismissed_at"] is not None:
            return "dismissed"
        return "replied" if answered(rec) else "unanswered"

    dismissed = [r for r in proactive if r["dismissed_at"] is not None]
    recent_dismissals = [r for r in dismissed if now - r["dismissed_at"] < timedelta(hours=cooldown_h)]
    if cooldown_h and recent_dismissals:
        latest = max(r["dismissed_at"] for r in recent_dismissals)
        blocks.append(
            ("recent_dismissal", f"the user dismissed a proactive message {_span(_minutes(now - latest))} ago (cooldown {cooldown_h}h).")
        )
    signals["dismissals_last_7_days"] = sum(1 for r in dismissed if now - r["dismissed_at"] < timedelta(days=7))

    streak = 0
    for rec in reversed(proactive):
        if answered(rec):
            break
        streak += 1
    signals["unanswered_proactive_streak"] = streak
    if max_unanswered and streak >= max_unanswered:
        blocks.append(
            ("unanswered_limit", f"{streak} proactive messages in a row went unanswered; wait until the user writes again.")
        )

    today = [r for r in proactive if r["sent_at"].date() == now.date()]
    signals["proactive_sent_today"] = len(today)
    signals["remaining_today"] = max(0, max_per_day - len(today))
    if len(today) >= max_per_day:
        blocks.append(("daily_limit", f"{len(today)} proactive message(s) already sent today (limit {max_per_day} in {mode} mode)."))

    last_pro = proactive[-1]["sent_at"] if proactive else None
    signals["minutes_since_last_proactive"] = _minutes(now - last_pro) if last_pro else None
    if last_pro is not None and _minutes(now - last_pro) < min_gap:
        blocks.append(
            ("min_gap", f"the last proactive message was {_span(_minutes(now - last_pro))} ago (minimum gap {min_gap} min in {mode} mode).")
        )

    window = [r for r in proactive if now - r["sent_at"] < timedelta(days=RECENT_DAYS)]
    signals["engagement"] = {
        "proactive_last_14_days": len(window),
        "replied": sum(1 for r in window if outcome(r) == "replied"),
        "unanswered": sum(1 for r in window if outcome(r) == "unanswered"),
        "dismissed": sum(1 for r in window if outcome(r) == "dismissed"),
    }
    signals["recent_proactive"] = [
        {
            "id": r["key"],
            "when": _describe(r["sent_at"], now),
            "hours_ago": round((now - r["sent_at"]).total_seconds() / 3600, 1),
            "topic": r["topic"],
            "text": (r["text"] or "")[:200] or None,
            "outcome": outcome(r),
        }
        for r in reversed(window[-RECENT_MAX:])
    ]

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
    return _result(blocks, signals, warnings, stop)


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
