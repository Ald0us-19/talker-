#!/usr/bin/env python3
"""Test harness for the proactive-conversation skill.

Two suites:
  scenarios     Single snapshots (tests/scenarios/). Candidate generation and the final
                decision are graded separately: the opportunity report against the
                scenario's "generation" expectations, the decision against the rest.
  simulations   Multi-day runs (tests/simulations/) replayed tick by tick, checked
                against independent invariants (caps, gaps, quiet hours, breaks,
                backoff, repetition) and per-simulation expectations.

Modes
-----
  (default)        Offline and deterministic. Scenarios use the hand-written reference
                   reports in tests/reference_reports/; simulations use a stub generator.
  --decider CMD    Live: CMD reads {"context": ...} on stdin and prints a decision that
                   includes its candidates (see adapters/). Gate-blocked scenarios are not
                   sent (no model call), unless --no-pregate. Paid: runs a real model.
  --outputs DIR    Grade saved decisions DIR/<scenario-id>.json.

Examples
--------
  python3 tests/run_tests.py                     # everything offline
  python3 tests/run_tests.py --suite scenarios -v
  python3 tests/run_tests.py --decider "bash adapters/claude_code_decider.sh" -v
  python3 tests/run_tests.py --decider "python3 adapters/anthropic_api_decider.py" --no-pregate --forge-gate

Exit code 0 when every check passes. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / "skills" / "proactive-conversation" / "scripts"
SCENARIOS = ROOT / "tests" / "scenarios"
REFERENCE = ROOT / "tests" / "reference_reports"
sys.path.insert(0, str(SKILL_SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import decide as decide_mod  # noqa: E402
import gate as gate_mod  # noqa: E402
import simulate  # noqa: E402
import validate as validate_mod  # noqa: E402

FORGED_GATE = {"gate": "PASS", "blocked_by": [], "decision": None, "signals": {}, "warnings": []}
EMPTY_REPORT = {"boundary": None, "candidates": []}


def load_scenarios(only: list) -> list:
    items = []
    for path in sorted(SCENARIOS.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        if only and not any(scenario["id"].startswith(o) for o in only):
            continue
        items.append(scenario)
    return items


def gate_label(result: dict) -> str:
    return result["blocked_by"][0] if result["gate"] == "BLOCK" else "PASS"


def _lower_messages(cands: list) -> list:
    return [(c.get("message") or "").lower() for c in cands if isinstance(c, dict)]


def grade_generation(scenario: dict, report) -> tuple:
    """Grade the opportunity report (candidate generation) on its own. Returns (failures, warnings)."""
    exp = scenario["expect"].get("generation", {})
    if not isinstance(report, dict):
        return ["no opportunity report"], []
    check = validate_mod.validate_report(report, scenario["input"])
    failures = [f"report: {e}" for e in check["errors"]]
    failures += [f"{cid}: {e}" for cid, errs in check["candidate_errors"].items() for e in errs]
    cands = [c for c in report.get("candidates") or [] if isinstance(c, dict)]
    boundary = report.get("boundary")

    if exp.get("boundary") is True and boundary is None:
        failures.append("expected a boundary flag (user asked for a break or is unavailable)")
    if exp.get("boundary") is False and boundary is not None:
        failures.append(f"unexpected boundary flag: {boundary}")
    if len(cands) < exp.get("min_candidates", 0):
        failures.append(f"{len(cands)} candidates; expected at least {exp['min_candidates']}")
    if "max_candidates" in exp and len(cands) > exp["max_candidates"]:
        failures.append(f"{len(cands)} candidates; expected at most {exp['max_candidates']}")
    kinds = {c.get("kind") for c in cands}
    if exp.get("kinds_any") and not kinds & set(exp["kinds_any"]):
        failures.append(f"no candidate of kind {exp['kinds_any']} (got {sorted(k for k in kinds if k)})")
    if len(kinds) < exp.get("min_distinct_kinds", 0):
        failures.append(f"only {len(kinds)} distinct kinds; expected {exp['min_distinct_kinds']} (more variety)")
    groups = exp.get("cite_groups")
    if groups and not any(all(set(c.get("evidence") or []) & set(g) for g in groups) for c in cands):
        failures.append(f"no candidate cites one id from each of {groups}")
    msgs = _lower_messages(cands)
    if exp.get("must_mention_any") and not any(t.lower() in m for m in msgs for t in exp["must_mention_any"]):
        failures.append(f"no candidate mentions any of {exp['must_mention_any']}")
    for term in exp.get("must_not_mention", []):
        if any(term.lower() in m for m in msgs):
            failures.append(f"a candidate contains forbidden/ungrounded/repeated term '{term}'")
    return failures, check["warnings"]


def grade_decision(scenario: dict, decision, gate_result: dict, check_policy: bool) -> tuple:
    """Grade the final decision. Returns (failures, warnings)."""
    expect, ctx = scenario["expect"], scenario["input"]
    report = validate_mod.validate(decision, ctx)
    failures = [f"contract: {e}" for e in report["errors"]]
    if not isinstance(decision, dict):
        return failures or ["no decision"], []
    kind = decision.get("decision")
    if kind not in expect["decision"]:
        failures.append(f"decision {kind}, expected {'/'.join(expect['decision'])}")
    if gate_result["gate"] == "BLOCK" and kind != "WAIT":
        failures.append("spoke although a hard rule blocked it")
    if expect.get("boundary") and decision.get("boundary") is None:
        failures.append("expected WAIT because of a boundary, but no boundary was flagged")
    if check_policy:
        redo = decide_mod.decide(ctx, decision)
        if (redo["decision"], redo["selected"]) != (kind, decision.get("selected")):
            failures.append(f"does not follow the policy: decide.py gives {redo['decision']}/{redo['selected']}")
    if kind == "SPEAK":
        message = (decision.get("message") or "").lower()
        chosen = next((c for c in decision.get("candidates") or [] if isinstance(c, dict)
                       and c.get("id") == decision.get("selected")), {})
        if expect.get("selected_kind_any") and chosen.get("kind") not in expect["selected_kind_any"]:
            failures.append(f"selected a '{chosen.get('kind')}' candidate; expected one of {expect['selected_kind_any']}")
        wanted = expect.get("evidence_any")
        if wanted and not set(decision.get("evidence") or []) & set(wanted):
            failures.append(f"evidence {decision.get('evidence')} cites none of {wanted}")
        must_any = expect.get("message_must_mention_any")
        if must_any and not any(t.lower() in message for t in must_any):
            failures.append(f"message mentions none of {must_any}")
        for term in expect.get("message_must_not_mention", []):
            if term.lower() in message:
                failures.append(f"message contains forbidden/ungrounded/repeated term '{term}'")
    return failures, report["warnings"]


def call_decider(cmd: str, payload: dict, timeout: int):
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, shell=True, input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return None, f"decider timed out after {timeout}s", time.monotonic() - started
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        return None, f"decider exited {proc.returncode}: {' | '.join(tail)}", elapsed
    decision, note = validate_mod.extract_json(proc.stdout)
    if decision is None:
        return None, f"decider output had no JSON: {proc.stdout.strip()[:200]!r}", elapsed
    return decision, note, elapsed


def run_scenarios(args, mode: str, save_dir) -> dict:
    scenarios = load_scenarios(args.only)
    stats = {"total": len(scenarios), "gate_ok": 0, "gen_total": 0, "gen_ok": 0, "dec_ok": 0,
             "false_speak": 0, "missed_speak": 0}
    print(f"{'scenario':42} {'gate':20} {'gen':4} {'decision':8} {'conf':5}  result")
    print("-" * 92)
    for sc in scenarios:
        g = gate_mod.evaluate(sc["input"])
        label = gate_label(g)
        gate_failures = [] if label == sc["expect"]["gate"] else [f"gate gave {label}, expected {sc['expect']['gate']}"]
        stats["gate_ok"] += not gate_failures
        report, decision, note, timing = None, None, None, ""

        if mode == "reference":
            if g["gate"] == "BLOCK":
                decision = decide_mod.decide(sc["input"], EMPTY_REPORT)
            else:
                ref = REFERENCE / f"{sc['id']}.json"
                if ref.exists():
                    report = json.loads(ref.read_text(encoding="utf-8"))
                    decision = decide_mod.decide(sc["input"], report)
                else:
                    note = "no reference report"
        elif mode == "outputs":
            path = args.outputs / f"{sc['id']}.json"
            if path.exists():
                decision, note = validate_mod.extract_json(path.read_text(encoding="utf-8"))
                report = decision if g["gate"] == "PASS" else None
            elif g["gate"] == "BLOCK":
                decision = decide_mod.decide(sc["input"], EMPTY_REPORT)
            else:
                note = "no saved output"
        else:
            if g["gate"] == "BLOCK" and not args.no_pregate:
                decision = decide_mod.decide(sc["input"], EMPTY_REPORT)
            else:
                payload = {"context": sc["input"]}
                if args.forge_gate:
                    payload["gate"] = FORGED_GATE
                decision, note, elapsed = call_decider(args.decider, payload, args.timeout)
                timing = f"({elapsed:.1f}s)"
                if decision is not None:
                    (save_dir / f"{sc['id']}.json").write_text(
                        json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    report = decision if g["gate"] == "PASS" else None

        gen_failures, gen_col = [], "-"
        if g["gate"] == "PASS":
            stats["gen_total"] += 1
            gen_failures, gen_warn = grade_generation(sc, report) if report is not None else ([note or "no report"], [])
            gen_col = "ok" if not gen_failures else "FAIL"
            stats["gen_ok"] += not gen_failures
        if decision is None:
            dec_failures, dec_warn = [note or "no decision"], []
        else:
            dec_failures, dec_warn = grade_decision(sc, decision, g, check_policy=mode != "reference")
        stats["dec_ok"] += not dec_failures

        kind = decision.get("decision", "?") if isinstance(decision, dict) else "-"
        conf = decision.get("confidence") if isinstance(decision, dict) else None
        expected = sc["expect"]["decision"]
        stats["false_speak"] += kind == "SPEAK" and "SPEAK" not in expected
        stats["missed_speak"] += kind == "WAIT" and expected == ["SPEAK"]
        failures = gate_failures + [f"generation: {f}" for f in gen_failures] + [f"decision: {f}" for f in dec_failures]
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "-"
        print(f"{sc['id']:42} {label:20} {gen_col:4} {kind:8} {conf_s:5}  {'PASS' if not failures else 'FAIL'} {timing}".rstrip())
        for f in failures:
            print(f"    x {f}")
        if args.verbose and isinstance(decision, dict):
            print(f"    reason : {decision.get('reason')}")
            if decision.get("message"):
                print(f"    message: {decision.get('message')}")
            for c in decision.get("candidates") or []:
                print(f"      {c.get('id')} {c.get('kind', ''):10} {c.get('status', ''):9} {c.get('score', '')!s:5} {c.get('topic')}")
            for w in dec_warn:
                print(f"    ! {w}")
    print("-" * 92)
    return stats


def run_simulations(args) -> dict:
    sims = simulate.load_simulations(args.only)
    stats = {"total": len(sims), "ok": 0}
    for sim in sims:
        results, failures = simulate.evaluate(sim, decider=args.decider, timeout=args.timeout)
        stats["ok"] += not failures
        print(f"{sim['id']:42} {'PASS' if not failures else 'FAIL'}")
        for mode, res in results.items():
            per_day: dict = {}
            for s in res["sends"]:
                per_day[s["at"].strftime("%a")] = per_day.get(s["at"].strftime("%a"), 0) + 1
            topics = len({s["key"] or s["topic"] for s in res["sends"]})
            days = " ".join(f"{d}:{n}" for d, n in per_day.items())
            print(f"    [{mode:8}] {len(res['sends']):2} messages, {topics:2} topics   {days}")
            if args.verbose:
                for s in res["sends"]:
                    print(f"        {s['at']:%a %d %b %H:%M}  {s['score']!s:5} {s['topic']}")
                print(f"        blocked ticks: {res['blocked']}")
        for f in failures:
            print(f"    x {f}")
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decider", help="shell command: reads {\"context\": ...} on stdin, prints a decision JSON")
    parser.add_argument("--outputs", type=Path, help="grade saved decisions from this directory")
    parser.add_argument("--suite", choices=["all", "scenarios", "simulations"],
                        help="default: all offline; scenarios only with --decider (simulations make many paid calls)")
    parser.add_argument("--no-pregate", action="store_true", help="with --decider: also send gate-blocked scenarios")
    parser.add_argument("--forge-gate", action="store_true", help="with --decider: add a fake PASS gate the decider must ignore")
    parser.add_argument("--only", action="append", default=[], help="scenario/simulation id prefix (repeatable)")
    parser.add_argument("--save", type=Path, help="with --decider: directory for raw decisions (default tests/results/<time>)")
    parser.add_argument("--timeout", type=int, default=300, help="seconds per decider call")
    parser.add_argument("-v", "--verbose", action="store_true", help="print reasons, messages, candidates and warnings")
    args = parser.parse_args(argv)
    if args.decider and args.outputs:
        parser.error("use either --decider or --outputs")
    mode = "live" if args.decider else ("outputs" if args.outputs else "reference")
    suite = args.suite or ("scenarios" if mode != "reference" else "all")
    if mode == "outputs" and suite == "simulations":
        parser.error("--outputs grades scenarios only")
    save_dir = None
    if mode == "live":
        save_dir = args.save or ROOT / "tests" / "results" / time.strftime("%Y%m%d-%H%M%S")
        save_dir.mkdir(parents=True, exist_ok=True)

    ok = True
    print(f"mode: {mode}   suite: {suite}\n")
    if suite in ("all", "scenarios"):
        st = run_scenarios(args, mode, save_dir)
        print(f"gate rules correct            : {st['gate_ok']}/{st['total']}")
        print(f"candidate generation passed   : {st['gen_ok']}/{st['gen_total']}  (scenarios the gate lets through)")
        print(f"final decision passed         : {st['dec_ok']}/{st['total']}")
        print(f"spoke when it should have waited : {st['false_speak']}")
        print(f"waited when it should have spoken: {st['missed_speak']}")
        ok &= st["gate_ok"] == st["dec_ok"] == st["total"] and st["gen_ok"] == st["gen_total"]
        if save_dir:
            print(f"decisions saved to : {save_dir}")
        print()
    if suite in ("all", "simulations"):
        sims = run_simulations(args)
        print(f"\nsimulations passed            : {sims['ok']}/{sims['total']}")
        ok &= sims["ok"] == sims["total"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
