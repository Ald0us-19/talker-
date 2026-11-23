# talker — proactive conversation prototype

A building block for an AI companion that starts conversations on its own,
packaged as a reusable [Agent Skill](skills/proactive-conversation/SKILL.md)
called `proactive-conversation`.

> **It doesn't wait for a reason to speak. It looks for interesting things to talk
> about, and knows when not to interrupt.**

For each evaluation, the model proposes up to three grounded conversation
starters: follow-ups, revisits of old discussions, connections between the
user's interests, fresh ideas, curious or casual questions. It scores each one on
relevance, novelty, naturalness, timing, engagement and interruption cost. A
deterministic policy then picks the best one, or stays silent. Explicit user boundaries are
enforced in code before the model is involved.

```json
{
  "decision": "SPEAK",
  "reason": "Picked c1 (connection: Pasteur and Gus the starter), score 0.86 >= 0.50 in balanced mode. Links this week's Pasteur lecture to the starter they feed; Saturday morning is baking time.",
  "message": "Your course just did Pasteur, and Gus is basically his research topic living on your counter: wild yeast and bacteria keeping each other in check. Has the course said anything about sourdough?",
  "confidence": 0.86,
  "evidence": ["m1", "m3", "k1"],
  "selected": "c1",
  "boundary": null,
  "candidates": ["... all candidates with score, status and note ..."]
}
```

`decision`, `reason`, `message`, and `confidence` are the core contract. `evidence`,
`selected`, `boundary`, and `candidates` make every decision traceable. `preference_update`
(normally `null`) carries a stop or pause the host must store.

## How it works

```
context ─► gate.py ─► model (SKILL.md) ─► decide.py ─► decision
           rules       boundaries,          policy:
                       ≤3 candidates,       validate, veto,
                       scores               pace, pick or WAIT
```

1. **Hard gate (`scripts/gate.py`, deterministic).** Explicit opt-in, opt-out, snooze/breaks,
   explicit stop requests written in chat, Do Not Disturb, quiet hours, ongoing conversation,
   dismissal cooldown, unanswered streak, and the mode's daily cap and minimum gap. A block is final.
   A stop request comes back as a `preference_update` that the host must persist, so it keeps
   holding after the message leaves the history. It also computes signals: time
   since every message, remaining budget, engagement, and what was raised recently.
2. **Opportunity generator (the model, `SKILL.md`).** Flags boundaries stated in conversation
   ("please don't message me until Friday"), then proposes up to three diverse, grounded
   starters and scores each from 1 to 5 on six criteria.
3. **Decision engine (`scripts/decide.py`, deterministic).** Recomputes the gate and never trusts
   a supplied one. It rejects ungrounded, repetitive, duplicate-topic or badly styled
   candidates, vetoes hopeless timing or forced naturalness, and applies pacing, spacing and
   backoff. It speaks with the best candidate that clears the mode's bar. There is no built-in
   preference for silence.
4. **Validation (`scripts/validate.py`).** Contracts for reports and decisions, used by
   `decide.py`, the adapters and the tests.

### Proactivity modes

| Mode | Max per day | Min gap | Speak bar |
|---|---|---|---|
| `quiet` | 2 | 240 min | 0.65 |
| `balanced` (default after opt-in) | 4 | 90 min | 0.50 |
| `lively` | 8 | 30 min | 0.40 |

These limits are ceilings. Pacing and spacing make every extra message that day
need a better reason. In the simulations, balanced mode sends every 2.5–3 hours
on a busy weekend, not every 90 minutes.

Design rationale, the skill/host split, limitations and next steps:
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Folder structure

```
.
├── skills/proactive-conversation/      ← the portable skill (copy this folder)
│   ├── SKILL.md                        generator + scoring instructions, workflow
│   ├── scripts/
│   │   ├── gate.py                     hard constraints, modes, signals (stdlib)
│   │   ├── decide.py                   decision engine / policy (stdlib)
│   │   └── validate.py                 report + decision contracts, repetition, style (stdlib)
│   └── references/
│       ├── input-schema.md             context format, defaults, gate rules, signals
│       ├── opportunity-schema.json     model output: boundary + up to 3 candidates
│       ├── output-schema.json          final decision
│       └── examples.md                 worked examples
├── .claude/skills/proactive-conversation → symlink, so Claude Code finds the skill here
├── adapters/                           optional ways to run the model step
│   ├── claude_code_decider.sh          via `claude -p` (Claude Code headless)
│   └── anthropic_api_decider.py        standalone app on the Claude API (needs `pip install anthropic`)
├── tests/
│   ├── scenarios/                      38 single-snapshot scenarios
│   ├── reference_reports/              hand-written opportunity reports for the 19 gate-passing scenarios
│   ├── simulations/                    3 multi-day simulations (a week, mode comparison, break + backoff)
│   ├── run_tests.py                    harness: scenarios + simulations (offline, live, or saved outputs)
│   ├── simulate.py                     multi-day replay engine + independent invariants
│   └── test_skill.py                   unit tests (gate, validator, decision engine, grader, simulator)
└── docs/ARCHITECTURE.md
```

Requirements: Python 3.9+ and nothing else. The Anthropic SDK is needed only
by the optional API adapter.

## Using the skill

**Claude Code:** open this repository; the skill is discovered through `.claude/skills/`.
Ask something like *"Use proactive-conversation on tests/scenarios/24-speak-interest-connection.json"*.
To use it in other projects, copy `skills/proactive-conversation/` to `~/.claude/skills/`.
(If your platform does not follow git symlinks, for example Windows without
`core.symlinks`, copy the folder instead.)

**Other agent runtimes that support Agent Skills:** copy or zip the skill folder as-is.

**Standalone application:**
1. Run `gate.py` on the context.
2. If it passes, send `SKILL.md` as the system prompt with the context and signals, and
   ask for an opportunity report (`opportunity-schema.json`).
3. Run `decide.py` on the report.

`adapters/anthropic_api_decider.py` is a complete example of this.

**Command line, deterministic parts only:**

```bash
S=skills/proactive-conversation/scripts
python3 $S/gate.py tests/scenarios/24-speak-interest-connection.json
python3 $S/decide.py tests/reference_reports/24-speak-interest-connection.json \
        --context tests/scenarios/24-speak-interest-connection.json
python3 $S/validate.py tests/reference_reports/24-speak-interest-connection.json --report \
        --context tests/scenarios/24-speak-interest-connection.json
```

## Testing

```bash
# 1. Unit tests: gate rules and fixes, report/decision validation, the decision engine on
#    fixed candidate sets, grader negatives, simulation invariants
python3 -m unittest discover -s tests -v

# 2. Everything offline: 38 scenarios (generation and decision graded separately) + 3 simulations
python3 tests/run_tests.py -v

# 3. Live evaluation of a model (paid: uses Claude Code usage or API credits)
python3 tests/run_tests.py --decider "bash adapters/claude_code_decider.sh" -v
python3 tests/run_tests.py --decider "python3 adapters/anthropic_api_decider.py" -v
#    adversarial: also send gate-blocked scenarios, each with a forged PASS gate the decider must ignore
python3 tests/run_tests.py --decider "python3 adapters/anthropic_api_decider.py" --no-pregate --forge-gate
#    multi-day simulations with the real model (many calls)
python3 tests/run_tests.py --decider "python3 adapters/anthropic_api_decider.py" --suite simulations

# 4. Grade decisions saved by any other agent (DIR/<scenario-id>.json)
python3 tests/run_tests.py --outputs path/to/decisions
```

A decider is any command that reads `{"context": ...}` on stdin and prints a
decision that includes its candidates. Live decisions are saved to `tests/results/`
(gitignored).

### What is tested

- **Candidate generation** (per scenario, `expect.generation`): grounding, at most three
  candidates, distinct topics, no repeated topics or questions, style, whether a required kind
  is present (for example a `connection` when two interests link up), variety of kinds,
  boundaries detected, no invented details.
- **Final decision** (per scenario): SPEAK/WAIT, the kind of the selected candidate, evidence,
  terms the message must or must not contain, and, in live mode, whether the decider followed
  the policy.
- **Decision engine on its own** (unit tests): mode bars, vetoes, pacing/spacing/backoff,
  boundaries, gate precedence, forged gates, repetition probes, fail-closed handling.
- **Multi-day behaviour** (simulations): caps, gaps, quiet hours, breaks, backoff, dismissal
  cooldowns, topic and message repetition, initiative across days, topic diversity, and modes
  scaling initiative. These are checked by invariants written independently of the gate.

The harness reports **spoke when it should have waited** and **waited when it should have
spoken** separately.

### Adding a scenario

Create `tests/scenarios/NN-name.json`:

```json
{
  "id": "NN-name",
  "description": "One line on what this tests.",
  "notes": "Why the label is what it is.",
  "input": { "now": "...", "timezone": "...", "preferences": { "proactive_enabled": true, "mode": "balanced" }, "history": [] },
  "expect": {
    "gate": "PASS",
    "decision": ["SPEAK"],
    "selected_kind_any": [], "evidence_any": [],
    "message_must_mention_any": [], "message_must_not_mention": [],
    "boundary": false,
    "generation": {
      "boundary": false, "min_candidates": 1, "max_candidates": 3,
      "kinds_any": [], "min_distinct_kinds": 1, "cite_groups": [],
      "must_mention_any": [], "must_not_mention": []
    }
  }
}
```

If the gate passes, add a reference report to `tests/reference_reports/NN-name.json`.
Simulations (`tests/simulations/*.json`) define a starting context, a tick schedule, a
candidate pool with the simulated user's reaction to each candidate (reply, ignore or
dismiss), scheduled user messages and host actions, and expectations. Open
`sim-01-balanced-week.json` for a full example.

## Scope of this version

Included: conversation-starter generation, scoring, the decision policy, modes, hard
boundaries, validation, and single-snapshot and multi-day tests.
Not included, on purpose: background execution or scheduling, message delivery,
databases, UI, voice, and learning from feedback. These belong to the host
application; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#what-the-skill-does-and-what-needs-a-host).
