"""Multi-day simulation: replay days of evaluation ticks the way a host would.

At every tick the host: applies scheduled user events, runs the gate, asks a
generator for an opportunity report, runs decide.py, persists any
preference_update (as a real host must), and, on SPEAK, delivers the message,
logs it in delivery_log and schedules the simulated user's reaction (reply,
ignore or dismiss). With "history_limit", only the last N history entries are
kept, like a host that sends a truncated history. The resulting send log is then checked against
invariants computed independently of the gate: caps, gaps, quiet hours,
snoozes, backoff, dismissal cooldowns, and topic and message repetition.

Offline, the generator is a stub that proposes the two best fresh entries from
the simulation's candidate pool plus the most recently sent one as a deliberate
repetition probe, which the policy must reject. With a decider command, every
gate-passing tick calls the real model instead (paid; not run by default).

Simulation file format: see tests/simulations/*.json and README.md.
"""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
SIMULATIONS = ROOT / "tests" / "simulations"

import decide  # noqa: E402  (sys.path set by the importer: run_tests.py / test_skill.py)
import gate  # noqa: E402
import validate  # noqa: E402


def load_simulations(only: list) -> list:
    sims = []
    for path in sorted(SIMULATIONS.glob("*.json")):
        sim = json.loads(path.read_text(encoding="utf-8"))
        if not only or any(sim["id"].startswith(o) for o in only):
            sims.append(sim)
    return sims


def _dt(value: str, tz) -> datetime:
    return gate._parse_dt(value, tz)


def _ticks(schedule: dict, tz) -> list:
    t, end = _dt(schedule["from"], tz), _dt(schedule["to"], tz)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(minutes=schedule["every_minutes"])
    return out


def stub_generator(ctx: dict, pool: list, last_sent: dict, now: datetime, tz) -> dict:
    """Two best fresh pool entries plus the most recently sent one as a repetition probe.

    "Fresh" means not sent in the last 14 days, as a generator following SKILL.md would do.
    The probe is deliberately stale: decide.py has to reject it.
    """
    cooldown = timedelta(days=gate.RECENT_DAYS)

    def available(p):
        start = _dt(p["available_from"], tz) if p.get("available_from") else None
        end = _dt(p["available_until"], tz) if p.get("available_until") else None
        return (start is None or start <= now) and (end is None or now <= end)

    pool = [p for p in pool if available(p)]
    fresh = [p for p in pool if p["key"] not in last_sent or now - last_sent[p["key"]] >= cooldown]
    fresh.sort(key=lambda p: decide.base_score(p["scores"]), reverse=True)
    chosen = fresh[:2]
    recent = sorted((p for p in pool if p["key"] in last_sent), key=lambda p: last_sent[p["key"]], reverse=True)
    if recent and recent[0] not in chosen:
        chosen.append(recent[0])
    cands = []
    for n, p in enumerate(chosen, 1):
        c = {k: copy.deepcopy(p[k]) for k in ("kind", "topic", "message", "rationale", "evidence", "scores")}
        c["id"] = f"c{n}"
        c["_key"] = p["key"]
        cands.append(c)
    return {"boundary": None, "candidates": cands}


def _call_decider(cmd: str, ctx: dict, timeout: int) -> dict:
    proc = subprocess.run(cmd, shell=True, input=json.dumps({"context": ctx}, ensure_ascii=False),
                          capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    decision, _ = validate.extract_json(proc.stdout) if proc.returncode == 0 else (None, None)
    return decision if isinstance(decision, dict) else {"decision": "WAIT", "reason": "decider failed", "candidates": []}


def run(sim: dict, mode: Optional[str] = None, decider: Optional[str] = None, timeout: int = 300) -> dict:
    """Replay one simulation; returns the send log and per-rule block counts."""
    ctx = copy.deepcopy(sim["start"])
    if mode:
        ctx["preferences"]["mode"] = mode
    ctx.setdefault("history", [])
    ctx.setdefault("delivery_log", [])
    tz = gate.ZoneInfo(ctx["timezone"])
    pool = {p["key"]: p for p in sim["pool"]}
    events = [(_dt(e["at"], tz), "user_message", e) for e in sim.get("user_messages", [])]
    events += [(_dt(e["at"], tz), "host", e) for e in sim.get("host_actions", [])]
    sends, blocked, last_sent, n_user, user_times, updates = [], {}, {}, 0, [], []
    limit = sim.get("history_limit")

    def persist(update, at):
        if update:
            ctx["preferences"].update(update)
            updates.append((at, dict(update)))

    for now in _ticks(sim["schedule"], tz):
        events.sort(key=lambda e: e[0])
        while events and events[0][0] <= now:
            at, kind, e = events.pop(0)
            if kind == "host":
                ctx["preferences"].update(e.get("preferences", {}))
            elif kind == "dismiss":
                e["log"]["dismissed_at"] = at.isoformat()
            else:  # a spontaneous user message, or a reply to a proactive message
                n_user += 1
                ctx["history"].append({"id": f"u{n_user}", "role": "user", "timestamp": at.isoformat(), "text": e["text"]})
                user_times.append(at)
                if kind == "reply":
                    e["log"]["replied_at"] = at.isoformat()
        ctx["now"] = now.isoformat()
        if limit:
            ctx["history"] = ctx["history"][-limit:]

        g = gate.evaluate(ctx)
        persist(g.get("preference_update"), now)
        if g["gate"] == "BLOCK":
            blocked[g["blocked_by"][0]] = blocked.get(g["blocked_by"][0], 0) + 1
            continue
        if decider:
            decision = _call_decider(decider, ctx, timeout)
        else:
            report = stub_generator(ctx, sim["pool"], last_sent, now, tz)
            keys = {c["id"]: c.pop("_key") for c in report["candidates"]}
            decision = decide.decide(ctx, report)
        persist(decision.get("preference_update"), now)
        if decision.get("decision") != "SPEAK":
            continue

        chosen = next((c for c in decision.get("candidates", []) if c.get("id") == decision.get("selected")), {})
        key = keys.get(decision["selected"]) if not decider else None
        if key is None:  # live mode: match the send to the closest pool entry for the user's reaction
            key = next((k for k, p in pool.items() if validate.same_topic(p["topic"], chosen.get("topic"))), None)
        pid = f"p{len(sends) + 1}"
        entry = {"id": f"d{len(sends) + 1}", "message_id": pid, "sent_at": now.isoformat(),
                 "topic": chosen.get("topic"), "text": decision["message"]}
        ctx["delivery_log"].append(entry)
        ctx["history"].append({"id": pid, "role": "assistant", "timestamp": now.isoformat(),
                               "text": decision["message"], "proactive": True})
        sends.append({"at": now, "key": key, "topic": chosen.get("topic"), "message": decision["message"],
                      "score": decision.get("confidence"), "prefs": copy.deepcopy(ctx["preferences"]), "log": entry})
        if key:
            last_sent[key] = now
        reaction = (pool.get(key) or {}).get("reaction") or sim.get("default_reaction") or {"type": "ignore"}
        if reaction["type"] in ("reply", "dismiss"):
            at = now + timedelta(minutes=reaction.get("after_minutes", 20))
            events.append((at, reaction["type"], {"text": reaction.get("text", "ok"), "log": entry}))
    return {"sends": sends, "blocked": blocked, "tz": tz, "user_times": user_times, "preference_updates": updates}


# ---------------------------------------------------------------- independent invariants


def invariant_failures(result: dict) -> list:
    """Re-derive the boundaries from the send log alone; any violation is a bug in gate or policy."""
    fails, sends = [], result["sends"]
    per_day: dict = {}
    for i, s in enumerate(sends):
        t, prefs = s["at"], s["prefs"]
        preset = gate.MODES.get(prefs.get("mode") or gate.DEFAULT_MODE, gate.MODES[gate.FALLBACK_MODE])
        cap = prefs.get("max_per_day", preset["max_per_day"])
        gap = prefs.get("min_gap_minutes", preset["min_gap_minutes"])
        q = prefs.get("quiet_hours", gate.DEFAULTS["quiet_hours"])
        label = f"send {i + 1} at {t:%a %d %b %H:%M}"
        if q:
            start, end, minute = gate._parse_hhmm(q["start"]), gate._parse_hhmm(q["end"]), t.hour * 60 + t.minute
            if start != end and gate._in_window(minute, start, end):
                fails.append(f"{label}: inside quiet hours")
        if prefs.get("snooze_until") and t < gate._parse_dt(prefs["snooze_until"], result["tz"]):
            fails.append(f"{label}: while snoozed")
        per_day[t.date()] = per_day.get(t.date(), 0) + 1
        if per_day[t.date()] > cap:
            fails.append(f"{label}: exceeds {cap}/day")
        prev = sends[:i]
        if prev and (t - prev[-1]["at"]) < timedelta(minutes=gap):
            fails.append(f"{label}: only {(t - prev[-1]['at']).seconds // 60} min after the previous send (min {gap})")
        streak = 0
        for p in reversed(prev):  # answered = the user wrote anything after it, before this send
            if any(p["at"] < u <= t for u in result["user_times"]):
                break
            streak += 1
        if streak >= gate.DEFAULTS["max_unanswered"]:
            fails.append(f"{label}: {streak} earlier messages unanswered")
        for p in prev:
            d = p["log"].get("dismissed_at")
            if d and timedelta(0) <= t - gate._parse_dt(d, result["tz"]) < timedelta(hours=gate.DEFAULTS["dismissal_cooldown_hours"]):
                fails.append(f"{label}: within the dismissal cooldown")
            if t - p["at"] < timedelta(hours=gate.DEFAULTS["topic_cooldown_hours"]) and validate.same_topic(p["topic"], s["topic"]):
                fails.append(f"{label}: repeats topic '{p['topic']}' from {p['at']:%a %H:%M}")
            if t - p["at"] < timedelta(days=14) and p["message"] == s["message"]:
                fails.append(f"{label}: identical message sent {p['at']:%a %H:%M}")
    return fails


def expectation_failures(sim: dict, results: dict) -> list:
    """Simulation-specific expectations, per mode and across modes."""
    fails = []
    for mode, res in results.items():
        exp = dict(sim.get("expect", {}))
        exp.update(sim.get("expect_by_mode", {}).get(mode, {}))
        sends = res["sends"]
        tag = f"[{mode}] " if len(results) > 1 else ""
        if "min_total" in exp and len(sends) < exp["min_total"]:
            fails.append(f"{tag}only {len(sends)} messages; expected at least {exp['min_total']} (too passive)")
        if "max_total" in exp and len(sends) > exp["max_total"]:
            fails.append(f"{tag}{len(sends)} messages; expected at most {exp['max_total']} (too pushy)")
        topics = {s["key"] or s["topic"] for s in sends}
        if len(topics) < exp.get("min_distinct_topics", 0):
            fails.append(f"{tag}only {len(topics)} distinct topics; expected {exp['min_distinct_topics']}")
        days = {s["at"].date() for s in sends}
        if len(days) < exp.get("min_active_days", 0):
            fails.append(f"{tag}initiated on only {len(days)} days; expected {exp['min_active_days']}")
        for a, b in exp.get("silent_between", []):
            lo, hi = _dt(a, res["tz"]), _dt(b, res["tz"])
            hits = [s for s in sends if lo <= s["at"] < hi]
            if hits:
                fails.append(f"{tag}sent during a silent window {a}..{b}: {hits[0]['at']:%a %d %b %H:%M}")
        if len(res["preference_updates"]) < exp.get("preference_updates_min", 0):
            fails.append(f"{tag}expected the host to persist at least {exp['preference_updates_min']} preference update(s)")
        for a in exp.get("speaks_after", []):
            if not any(s["at"] >= _dt(a, res["tz"]) for s in sends):
                fails.append(f"{tag}never resumed after {a}")
    if sim.get("expect_monotonic") and len(results) > 1:
        counts = [len(results[m]["sends"]) for m in results]
        if any(b <= a for a, b in zip(counts, counts[1:])):
            fails.append(f"message counts should increase strictly across {list(results)}: {counts}")
    return fails


def evaluate(sim: dict, decider: Optional[str] = None, timeout: int = 300) -> tuple:
    """Run every mode of a simulation; return (results_by_mode, failures)."""
    modes = sim.get("modes") or [sim["start"]["preferences"].get("mode", gate.DEFAULT_MODE)]
    results = {mode: run(sim, mode, decider, timeout) for mode in modes}
    failures = []
    for mode, res in results.items():
        failures += [(f"[{mode}] " if len(modes) > 1 else "") + f for f in invariant_failures(res)]
    failures += expectation_failures(sim, results)
    return results, failures
