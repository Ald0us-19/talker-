# talker — proactive conversation prototype

The first building block of an AI companion that can start conversations on
its own. This prototype solves one problem only:

> **Should the AI initiate a conversation right now, or stay silent?**

It is packaged as a reusable [Agent Skill](skills/proactive-conversation/SKILL.md)
called `proactive-conversation`. Given a JSON snapshot (history, preferences,
current time), it returns:

```json
{
  "decision": "SPEAK",
  "reason": "The user said they'd report back on Tuesday's 2pm interview; it ended ~4.5h ago and nothing has been said since.",
  "message": "How did the final round at Lindqvist go? Happy to hear about it whenever you feel like it.",
  "confidence": 0.82,
  "evidence": ["m1", "m3"]
}
```

`decision`, `reason`, `message`, and `confidence` are the requested contract.
`evidence` lists the ids of the context items that justify the decision. It
makes grounding checkable: a SPEAK decision must cite real context items.

## How it works (short version)

1. **Hard gate (deterministic):** `scripts/gate.py` blocks on opt-out, missing opt-in, snooze,
   Do Not Disturb, quiet hours, an ongoing conversation, recent dismissals, unanswered
   streaks, and rate limits. A block is final. If it passes, it also computes time
   signals ("yesterday 14:00, 28h ago") so the model never does date arithmetic.
2. **Judgment (model):** `SKILL.md` tells the model how to find *grounded* opportunities
   (requests, open loops, time-anchored events, commitments), weigh relevance, usefulness,
   timing, and interruption cost, and default to silence.
3. **Validation (deterministic):** `scripts/validate.py` checks the contract, that evidence
   ids exist, the SPEAK confidence threshold, and bans generic or pressuring phrasing.
   The host treats anything invalid as WAIT.

Design rationale, the split between skill and host, limitations, and next
steps: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Folder structure

```
.
├── skills/proactive-conversation/      ← the portable skill (copy this folder)
│   ├── SKILL.md                        judgment instructions + workflow
│   ├── scripts/
│   │   ├── gate.py                     hard constraints + time signals (stdlib)
│   │   └── validate.py                 output contract, grounding, style lint (stdlib)
│   └── references/
│       ├── input-schema.md             context format, defaults, gate rules
│       ├── output-schema.json          decision JSON Schema
│       └── examples.md                 worked input → output examples
├── .claude/skills/proactive-conversation → symlink, so Claude Code finds the skill here
├── adapters/                           optional ways to run the judgment step
│   ├── claude_code_decider.sh          via `claude -p` (Claude Code headless)
│   └── anthropic_api_decider.py        standalone app on the Claude API (needs `pip install anthropic`)
├── tests/
│   ├── scenarios/                      23 scenarios: 5 SPEAK, 10 hard-rule WAIT, 7 judgment WAIT, 1 either
│   ├── reference_outputs/              hand-written reference decisions for the 13 gate-passing scenarios
│   ├── run_tests.py                    scenario harness (offline, live, or saved outputs)
│   └── test_skill.py                   unit tests: gate edge cases, validator, grader negatives
└── docs/ARCHITECTURE.md
```

Requirements: Python 3.9+ and nothing else. The Anthropic SDK is needed only
by the optional API adapter.

## Using the skill

**Claude Code:** open this repository; the skill is discovered through `.claude/skills/`.
Ask something like *"Use proactive-conversation on tests/scenarios/01-speak-interview-followup.json"*.
To use it in other projects, copy `skills/proactive-conversation/` to `~/.claude/skills/`.
(If your platform does not follow git symlinks, for example Windows without
`core.symlinks`, copy the folder instead.)

**Claude apps / other agent runtimes that support Agent Skills:** zip or copy the
`skills/proactive-conversation/` folder as-is.

**Standalone application:** run the gate yourself, send `SKILL.md` as the system
prompt with the context and gate result, then validate. `adapters/anthropic_api_decider.py`
is a complete example of this, about 120 lines long.

**Command line, deterministic parts only:**

```bash
python3 skills/proactive-conversation/scripts/gate.py tests/scenarios/07-gate-quiet-hours.json
python3 skills/proactive-conversation/scripts/validate.py tests/reference_outputs/01-speak-interview-followup.json \
        --context tests/scenarios/01-speak-interview-followup.json
```

## Testing

```bash
# 1. Unit tests: gate edge cases, validator rules, and checks that the grader rejects bad answers
python3 -m unittest discover -s tests -v

# 2. Scenario suite, offline: gate on all 23 scenarios + grading of the reference decisions
python3 tests/run_tests.py -v

# 3. Live evaluation of a model (uses your Claude Code usage or API credits)
python3 tests/run_tests.py --decider "bash adapters/claude_code_decider.sh" -v
python3 tests/run_tests.py --decider "python3 adapters/anthropic_api_decider.py" -v

#    End-to-end variant: the model must run and obey the gate itself
python3 tests/run_tests.py --decider "bash adapters/claude_code_decider.sh" --no-pregate

# 4. Grade decisions saved by any other agent (DIR/<scenario-id>.json)
python3 tests/run_tests.py --outputs path/to/decisions
```

A decider is any command that reads `{"context": ..., "gate": ...}` on stdin
and prints a decision JSON. Live decisions are saved to `tests/results/` (gitignored).

Each scenario declares the expected gate rule, the acceptable decisions, and,
for SPEAK, which evidence ids should be cited, the terms the message must
mention (topic, language), and the terms it must not contain (assumed outcomes,
invented details). The harness reports two error types separately:
**spoke when it should have waited** (the costly one) and **waited when it
should have spoken**.

### Adding a scenario

Create `tests/scenarios/NN-name.json`:

```json
{
  "id": "NN-name",
  "description": "One line on what this tests.",
  "notes": "Why the label is what it is.",
  "input": { "now": "...", "timezone": "...", "preferences": { "proactive_enabled": true }, "history": [] },
  "expect": {
    "gate": "PASS",
    "decision": ["WAIT"],
    "evidence_any": [],
    "message_must_mention_any": [],
    "message_must_not_mention": [],
    "reason_must_mention_any": []
  }
}
```

If the gate passes, add a reference decision to `tests/reference_outputs/NN-name.json`.

## Scope of this version

Included: the decision, message drafting, hard constraints, validation, and tests.
Not included, on purpose: background execution or scheduling, message delivery,
databases, UI, voice, and learning from feedback. These belong to the host
application; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#what-the-skill-does-and-what-needs-a-host).
