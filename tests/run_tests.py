#!/usr/bin/env python3
"""Scenario harness for the proactive-conversation skill.

Modes
-----
  (default)          Offline and deterministic. Checks the gate on every scenario
                     and grades the hand-written reference decisions in
                     tests/reference_outputs/. Needs no model.
  --decider CMD      Live evaluation. For each scenario the gate passes, pipes a
                     request {"context": ..., "gate": ...} to CMD on stdin and
                     grades the JSON decision CMD prints. Gate-blocked scenarios
                     use the gate's own WAIT (no model call), exactly as a host would.
  --no-pregate       With --decider: send every scenario as {"context": ...} only,
                     so the model must run/respect the gate itself (end-to-end test).
  --outputs DIR      Grade previously saved decisions DIR/<scenario-id>.json.

Examples
--------
  python3 tests/run_tests.py
  python3 tests/run_tests.py --decider "bash adapters/claude_code_decider.sh" -v
  python3 tests/run_tests.py --decider "python3 adapters/anthropic_api_decider.py" --only 01-speak-interview-followup

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
REFERENCE = ROOT / "tests" / "reference_outputs"
sys.path.insert(0, str(SKILL_SCRIPTS))

import gate as gate_mod  # noqa: E402
import validate as validate_mod  # noqa: E402


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


def grade(scenario: dict, decision, gate_result: dict) -> tuple:
    """Return (failures, warnings) for one decision."""
    expect = scenario["expect"]
    ctx = scenario["input"]
    report = validate_mod.validate(decision, ctx)
    failures = [f"contract: {e}" for e in report["errors"]]
    warnings = list(report["warnings"])
    if not isinstance(decision, dict):
        return failures, warnings

    kind = decision.get("decision")
    acceptable = expect["decision"]
    if kind not in acceptable:
        failures.append(f"decision {kind}, expected {'/'.join(acceptable)}")
    if gate_result["gate"] == "BLOCK" and kind != "WAIT":
        failures.append("spoke although a hard rule blocked it")

    if kind == "SPEAK":
        message = (decision.get("message") or "").lower()
        evidence = set(decision.get("evidence") or [])
        wanted = expect.get("evidence_any")
        if wanted and not evidence & set(wanted):
            failures.append(f"evidence {sorted(evidence)} cites none of {wanted}")
        must_any = expect.get("message_must_mention_any")
        if must_any and not any(term.lower() in message for term in must_any):
            failures.append(f"message mentions none of {must_any}")
        for term in expect.get("message_must_not_mention", []):
            if term.lower() in message:
                failures.append(f"message contains forbidden/ungrounded term '{term}'")
    elif kind == "WAIT":
        must_any = expect.get("reason_must_mention_any")
        reason = (decision.get("reason") or "").lower()
        if must_any and not any(term.lower() in reason for term in must_any):
            failures.append(f"reason mentions none of {must_any} (right decision, wrong reasoning?)")
    return failures, warnings


def call_decider(cmd: str, payload: dict, timeout: int):
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, shell=True, input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        )
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decider", help="shell command: reads a request JSON on stdin, prints a decision JSON")
    parser.add_argument("--outputs", type=Path, help="grade saved decisions from this directory")
    parser.add_argument("--no-pregate", action="store_true", help="with --decider: do not run the gate for the model")
    parser.add_argument("--only", action="append", default=[], help="scenario id prefix (repeatable)")
    parser.add_argument("--save", type=Path, help="with --decider: directory for raw decisions (default tests/results/<time>)")
    parser.add_argument("--timeout", type=int, default=300, help="seconds per decider call")
    parser.add_argument("-v", "--verbose", action="store_true", help="print reasons, messages and warnings")
    args = parser.parse_args(argv)

    if args.decider and args.outputs:
        parser.error("use either --decider or --outputs")
    mode = "live" if args.decider else ("outputs" if args.outputs else "reference")
    save_dir = None
    if mode == "live":
        save_dir = args.save or ROOT / "tests" / "results" / time.strftime("%Y%m%d-%H%M%S")
        save_dir.mkdir(parents=True, exist_ok=True)

    scenarios = load_scenarios(args.only)
    if not scenarios:
        print("no scenarios matched")
        return 1

    gate_ok = 0
    passed = 0
    false_speak = missed_speak = 0
    print(f"mode: {mode}   scenarios: {len(scenarios)}\n")
    print(f"{'scenario':34} {'gate':20} {'decision':9} {'conf':5}  result")
    print("-" * 84)
    for sc in scenarios:
        g = gate_mod.evaluate(sc["input"])
        label = gate_label(g)
        gate_pass = label == sc["expect"]["gate"]
        gate_ok += gate_pass
        gate_failures = [] if gate_pass else [f"gate gave {label}, expected {sc['expect']['gate']}"]
        timing = ""

        decision, source_note = None, None
        if mode == "reference":
            if g["gate"] == "BLOCK":
                decision = g["decision"]
            else:
                ref = REFERENCE / f"{sc['id']}.json"
                if ref.exists():
                    decision = json.loads(ref.read_text(encoding="utf-8"))
                else:
                    source_note = "no reference output"
        elif mode == "outputs":
            path = args.outputs / f"{sc['id']}.json"
            if path.exists():
                decision, source_note = validate_mod.extract_json(path.read_text(encoding="utf-8"))
            elif g["gate"] == "BLOCK":
                decision = g["decision"]
            else:
                source_note = "no saved output"
        else:
            if g["gate"] == "BLOCK" and not args.no_pregate:
                decision = g["decision"]
            else:
                payload = {"context": sc["input"]} if args.no_pregate else {"context": sc["input"], "gate": g}
                decision, source_note, elapsed = call_decider(args.decider, payload, args.timeout)
                if decision is not None:
                    (save_dir / f"{sc['id']}.json").write_text(
                        json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                timing = f"({elapsed:.1f}s)"

        if decision is None:
            failures, warnings = [source_note or "no decision"], []
        else:
            failures, warnings = grade(sc, decision, g)
            if source_note:
                warnings.insert(0, source_note)
        failures = gate_failures + failures

        kind = decision.get("decision", "?") if isinstance(decision, dict) else "-"
        conf = decision.get("confidence") if isinstance(decision, dict) else None
        expected = sc["expect"]["decision"]
        if kind == "SPEAK" and "SPEAK" not in expected:
            false_speak += 1
        if kind == "WAIT" and expected == ["SPEAK"]:
            missed_speak += 1
        ok = not failures
        passed += ok
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "-"
        print(f"{sc['id']:34} {label:20} {kind:9} {conf_s:5}  {'PASS' if ok else 'FAIL'} {timing}".rstrip())
        for f in failures:
            print(f"    x {f}")
        if args.verbose and isinstance(decision, dict):
            print(f"    reason : {decision.get('reason')}")
            if decision.get("message"):
                print(f"    message: {decision.get('message')}")
            for w in warnings:
                print(f"    ! {w}")

    total = len(scenarios)
    print("-" * 84)
    print(f"gate rules correct : {gate_ok}/{total}")
    print(f"scenarios passed   : {passed}/{total}")
    print(f"spoke when it should have waited : {false_speak}")
    print(f"waited when it should have spoken: {missed_speak}")
    if save_dir:
        print(f"decisions saved to : {save_dir.relative_to(ROOT) if save_dir.is_relative_to(ROOT) else save_dir}")
    return 0 if passed == total and gate_ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
