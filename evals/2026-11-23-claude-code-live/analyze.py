#!/usr/bin/env python3
"""Summarise a live evaluation run: decisions, candidates, scores, style signals, usage.

    python3 evals/2026-11-23-claude-code-live/analyze.py > evals/2026-11-23-claude-code-live/summary.md
Also writes summary.json next to this file. Standard library only.
"""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "skills" / "proactive-conversation" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import decide  # noqa: E402
import gate  # noqa: E402
import run_tests  # noqa: E402
import validate  # noqa: E402

rows = []
for path in sorted((HERE / "decisions").glob("*.json")):
    sid = path.stem
    scenario = json.loads((ROOT / "tests" / "scenarios" / f"{sid}.json").read_text())
    decision = json.loads(path.read_text())
    raw_path = HERE / "raw" / f"{sid}.claude.json"
    envelope = json.loads(raw_path.read_text()) if raw_path.exists() else {}
    g = gate.evaluate(scenario["input"])
    gen_fail, gen_warn = run_tests.grade_generation(scenario, decision)
    dec_fail, dec_warn = run_tests.grade_decision(scenario, decision, g, check_policy=True)
    cands = []
    for c in decision.get("candidates", []):
        msg = c.get("message") or ""
        cands.append({
            "id": c.get("id"), "kind": c.get("kind"), "topic": c.get("topic"), "message": msg,
            "scores": c.get("scores"), "base": round(decide.base_score(c["scores"]), 3) if c.get("scores") else None,
            "score": c.get("score"), "status": c.get("status"), "note": c.get("note"),
            "chars": len(msg), "questions": msg.count("?"), "exclaims": msg.count("!"),
            "opening": " ".join(msg.split()[:3]),
        })
    rows.append({
        "id": sid, "expected": scenario["expect"]["decision"], "decision": decision["decision"],
        "confidence": decision["confidence"], "selected": decision.get("selected"), "message": decision.get("message"),
        "boundary": decision.get("boundary"), "reason": decision["reason"], "candidates": cands,
        "generation_failures": gen_fail, "decision_failures": dec_fail, "warnings": gen_warn + dec_warn,
        "bar": g["signals"].get("speak_bar"), "mode": g["signals"].get("mode"),
        "penalty": decide.penalties(g["signals"]),
        "turns": envelope.get("num_turns"), "seconds": round((envelope.get("duration_ms") or 0) / 1000, 1),
        "cost_usd": envelope.get("total_cost_usd"), "models": list((envelope.get("modelUsage") or {}).keys()),
    })

(HERE / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")

out = []
out.append(f"# Live run summary ({len(rows)} scenarios)\n")
out.append("| Scenario | Expected | Decision | Conf | Selected | Gen | Dec | Turns | s | $ |")
out.append("|---|---|---|---|---|---|---|---|---|---|")
for r in rows:
    sel = next((c for c in r["candidates"] if c["id"] == r["selected"]), None)
    out.append(f"| {r['id']} | {'/'.join(r['expected'])} | {r['decision']} | {r['confidence']:.2f} | "
               f"{(sel['kind'] + ': ' + sel['topic']) if sel else ('boundary: ' + r['boundary']['type'] if r['boundary'] else '-')} | "
               f"{'ok' if not r['generation_failures'] else 'FAIL'} | {'ok' if not r['decision_failures'] else 'FAIL'} | "
               f"{r['turns']} | {r['seconds']} | {r['cost_usd'] if r['cost_usd'] is None else round(r['cost_usd'], 3)} |")
out.append("")
total = sum(r["cost_usd"] or 0 for r in rows)
out.append(f"Total cost: ${total:.2f}; models: {sorted({m for r in rows for m in r['models']})}\n")
for r in rows:
    out.append(f"## {r['id']} → {r['decision']} ({r['confidence']:.2f}, {r['mode']} bar {r['bar']}, penalties {r['penalty']})")
    if r["boundary"]:
        out.append(f"- boundary: `{r['boundary']}`")
    for c in r["candidates"]:
        s = c["scores"] or {}
        sc = "/".join(str(s.get(k, "?")) for k in validate.SCORE_KEYS)
        out.append(f"- **{c['id']}** {c['kind']} · {c['topic']} · scores {sc} · policy {c['score']} · {c['status']}"
                   f"{' · ' + c['note'] if c['note'] else ''}\n  > {c['message']}")
    for f in r["generation_failures"] + r["decision_failures"]:
        out.append(f"- ✗ {f}")
    for w in r["warnings"]:
        out.append(f"- ! {w}")
    out.append("")

kinds = {}
for r in rows:
    for c in r["candidates"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
openings = {}
for r in rows:
    for c in r["candidates"]:
        first = re.sub(r"[^a-zäöüß' ]", "", c["opening"].lower()).split(" ")[0] if c["opening"] else ""
        openings[first] = openings.get(first, 0) + 1
out.append("## Aggregates")
out.append(f"- candidates per scenario: {[len(r['candidates']) for r in rows]}")
out.append(f"- kinds: {dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}")
out.append(f"- first words of candidate messages: {dict(sorted(openings.items(), key=lambda kv: -kv[1]))}")
out.append(f"- SPEAK: {sum(r['decision'] == 'SPEAK' for r in rows)}, WAIT: {sum(r['decision'] == 'WAIT' for r in rows)}")
print("\n".join(out))
