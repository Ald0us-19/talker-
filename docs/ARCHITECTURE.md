# Architecture

## The problem

For one snapshot in time, the skill answers:

> Is there something this user would enjoy talking about right now, and is now a good moment to bring it up?

Version 1 answered a narrower question ("is there an unfinished task worth an
interruption?") and defaulted to silence. Version 2 treats initiative as the
goal: the model actively looks for conversation starters, including ones with
no practical purpose. Restraint comes from explicit rules and an inspectable
scoring policy, not from a general bias toward silence.

The skill is still a stateless function, `decide(context) -> decision`. The host
owns all state (history, delivery log, preferences, memories) and passes it in.
Nothing runs in the background.

## Pipeline

```
            host application / scheduler   (not part of the skill)
                         │  context snapshot (JSON)
                         ▼
  ┌──────────────────────────────────────────────┐
  │ 1. scripts/gate.py                  rules    │── BLOCK ──► WAIT, confidence 1.0
  │    opt-in, disabled, snooze/break, DND,      │             (no model call)
  │    quiet hours, active conversation,         │
  │    dismissal cooldown, unanswered streak,    │
  │    mode's daily cap + min gap                │
  │    + signals: times, budget, engagement,     │
  │      recent proactive topics                 │
  └──────────────────────┬───────────────────────┘
                         │ PASS + signals
                         ▼
  ┌──────────────────────────────────────────────┐
  │ 2. SKILL.md                      model       │
  │    boundaries stated in chat?  → flag        │
  │    opportunity generator: ≤3 candidates      │
  │    (follow_up, revisit, connection, idea,    │
  │     curiosity, casual), each grounded        │
  │    scores 1-5: relevance, novelty,           │
  │    naturalness, timing, engagement,          │
  │    interruption cost                         │
  └──────────────────────┬───────────────────────┘
                         │ opportunity report
                         ▼
  ┌──────────────────────────────────────────────┐
  │ 3. scripts/decide.py                policy   │
  │    recompute gate (never trust a supplied    │
  │    one) → boundary? → validate candidates    │
  │    (grounding, repetition, diversity, style) │
  │    → vetoes → score − pacing − spacing −     │
  │    backoff → best ≥ mode bar ? SPEAK : WAIT  │
  └──────────────────────┬───────────────────────┘
                         ▼
            host delivers the message (or not), appends to delivery_log,
            records replies/dismissals, acts on boundaries (snooze_until)
```

`scripts/validate.py` holds the contracts and checks used in step 3, by the
adapters, and by the tests.

## Rules, judgment and policy

| Concern | Mechanism | Why |
|---|---|---|
| Opt-in/opt-out, snooze/breaks, DND, quiet hours | rule (gate) | Consent and explicit boundaries never depend on a model. |
| Daily cap, min gap (per mode), cooldowns, backoff | rule (gate) | Exact, auditable, user-configurable. |
| Explicit stop request in chat ("stop messaging me", "don't message me until Friday") | rule (gate `stop_requested`) → `preference_update` persisted by the host | Consent can't hinge on the model noticing it. Once stored, the preference enforces the stop even after the message leaves the history. |
| Paraphrased break request, or busy/focus time | model flags a `boundary` → policy forces WAIT | Phrasing varies too much for keywords. A `requested_break` is also returned as a `preference_update` (pause until the user writes); focus time is not persisted. |
| What is interesting to say | model (generator) | Needs understanding: open loops, links between interests, fresh angles, tone. |
| How good each starter is right now | model (six 1–5 scores) | Judgment, but broken into criteria that can be inspected and tuned. |
| Which starter wins, and whether any is good enough | policy (`decide.py`) | Deterministic and testable without a model; behaviour can be tuned without editing the prompt. |
| Repetition, grounding, diversity, banned phrasing | rule (validator) | A checkable safety net after the model. |

## Scoring policy

```
base     = Σ weight_k · (score_k − 1)/4      relevance .25, novelty .20, naturalness .20, timing .15, engagement .20
         − 0.40 · (interruption_cost − 1)/4
pacing   = 0.30 · sent_today / max_per_day
spacing  = 0.70 · max(0, 1 − minutes_since_last_proactive / (waking_minutes / max_per_day))
backoff  = 0.10 · unanswered_streak
score    = base − pacing − spacing − backoff
vetoes   : timing 1, naturalness ≤ 2, interruption_cost 5
SPEAK if the best eligible score ≥ the mode's bar (quiet 0.65, balanced 0.50, lively 0.40)
```

A decent starter (scores 4/3/4/3/3/2, base 0.51) is sent in balanced and lively
mode. An excellent one (5/5/4/4/4/1, base 0.86) is sent even in quiet mode.
Pacing and spacing are what make the limits ceilings rather than targets. In the mode-comparison
simulation, with a constant supply of excellent candidates and an eager user, the modes send:

- quiet: 08:00 and 14:30
- balanced: every 2.5–3 hours
- lively: about hourly, tapering off later in the day

## What the skill does, and what needs a host

| Capability | Provided by the skill | Requires the host application |
|---|---|---|
| Hard-constraint evaluation, modes | `gate.py` | Accurate preferences, timezone, `user_status`, and a complete `delivery_log` |
| Finding and scoring conversation starters | `SKILL.md` + a capable model | A model runtime (Claude Code, Claude API, another agent) |
| Final pick | `decide.py` | Treating errors, timeouts and invalid output as WAIT |
| Contracts, grounding, repetition, style | `validate.py` | — |
| Deciding *when* to evaluate | — | A scheduler or event trigger (app opened, event ended, every hour while awake) |
| Sending the message | — | A delivery channel |
| Recording sends, replies, dismissals (`delivery_log`) | — | Storage |
| Persisting every `preference_update`; opt-out/mode/quiet-hours settings | — | Settings UI and persistence; stamping `updated_at`; resuming only on an explicit user action |
| Long-term memory, interest profile | reads `memories[]`, `profile.interests[]` | Memory storage and retrieval |

## Fixes in this version

| Issue | Before | Now |
|---|---|---|
| Caller-supplied gate results | The API adapter used `request["gate"]` when present, so a forged `"PASS"` skipped every rule. SKILL.md told the model to trust it. | The gate is always recomputed: by `decide.py`, both adapters, and `validate.py`, which rejects SPEAK when the recomputed gate blocks. Covered by unit tests and `--forge-gate`. |
| Malformed snooze | An unreadable `snooze_until` was warned about and ignored (fail open). | It counts as snoozed until the host fixes it. |
| Dismissal timestamps | The cooldown ran from the message's *send* time; proactive messages with bad timestamps were dropped, along with their dismissals. | The cooldown runs from `dismissed_at`. A missing value falls back to the send time with a warning; a malformed one counts as now. |
| Stop requests depended on the model | "Please don't message me" was only caught by the model's boundary flag, and only the `reason` text suggested setting `snooze_until`. | The gate detects explicit stop requests deterministically and returns a structured `preference_update` (`proactive_enabled: false`, `snooze_until`, or `pause_until_user_writes`) for the host to persist. Model-flagged breaks are persisted the same way. |
| History-based rate limits | Caps and gaps were counted only from `history` entries marked proactive, so truncated history or bad timestamps undercounted. | `delivery_log` is the authoritative source, merged with history and deduplicated. Unreadable send times count as now. A missing log produces a warning. |

## Design decisions and trade-offs

1. **The model scores, the policy decides.** The model's judgment is split into six visible
   scores, and a fixed formula turns them into a decision. *Trade-off:* the model can't make a
   holistic exception, such as a strong gut feeling, without scoring for it. In return the behaviour
   is testable without a model, consistent across runs, and tunable (weights, bars) without
   changing the prompt.
2. **Up to three candidates, not one.** Generating alternatives before choosing reduces
   tunnel vision on the most obvious follow-up and makes repetition and diversity checkable.
   *Trade-off:* more output tokens per evaluation.
3. **General knowledge is allowed in ideas; facts about the user are not.** "Sourdough is wild
   yeast and bacteria" is fine; "how was your sister's wedding?" is not. *Trade-off:* the model's
   general claims can't be checked against the context. SKILL.md asks for well-established
   facts and forbids presenting news as fact.
4. **Repetition is blocked deterministically.** Topic cooldown (48h), no re-raising a topic whose
   last mention got no reply (7 days), and no near-duplicate messages (14 days), using a cheap
   token-overlap measure. *Trade-off:* it is crude. Paraphrases with different words get through,
   and broad shared words ("weekend") can over-match within the cooldown window.
5. **Fail closed on unreadable data, but not on missing data.** Malformed snooze, dismissal or
   send times are read strictly. A missing `delivery_log` only produces a warning, which keeps
   simple hosts working.
6. **Opt-in stays mandatory.** Balanced mode is the default only after `proactive_enabled: true`.
7. **Stdlib-only Python, JSON in and out.** The skill folder is portable and every piece can be
   called as a subprocess or reimplemented elsewhere.

## Extension points

- **New criterion or weight** → `WEIGHTS` / `VETOES` in `decide.py`, plus the table in SKILL.md.
- **New hard rule** → a check and an entry in `RULE_ORDER` in `gate.py`, plus a scenario.
- **Per-user tuning from feedback** → the host adjusts `mode`, `topic_cooldown_hours` or explicit
  limits from `delivery_log` outcomes; no prompt changes needed.
- **Better repetition detection** → swap `validate.tokens()` / `same_topic()` for embeddings.
- **Memory retrieval** → the host chooses which `memories`/`interests` to include; the skill already
  cites them.

## Limitations

- **No live model evaluation yet.** The reference reports were written by hand following
  SKILL.md, so they show that labels, grader and policy agree, not how a model performs.
  Both adapters were verified end to end against a fake `claude` binary and a local API
  stub, not against a real model.
- **Score calibration is untested with real models.** The weights and bars were chosen by reasoning
  through examples and simulations. A model that inflates or compresses scores will shift behaviour,
  and the bars may need tuning after a live run.
- **Simulations use a stub generator.** The offline multi-day runs test the gate, policy and
  invariants over time, not what a model would generate on day five. The live simulation path
  exists but has not been run.
- **"Answered" is coarse.** Any user message after a proactive message counts as a reply unless
  the host records `replied_at`.
- **Grading is substring-based.** It can't judge originality or warmth, and English-centric style
  rules don't cover other languages.
- **Scenario authorship bias.** The 38 scenarios and 3 simulations were written by the same author
  as the policy; borderline labels are marked "either".
- **Stop detection is phrase-based.** It covers explicit English and German phrasings. Paraphrases
  depend on the model's boundary flag. A topic-limited request ("don't message me about work") is
  treated as a full stop, which errs toward silence.
- **History text is untrusted.** The gate and policy are immune to injected instructions; the
  model is told to treat history as data, but there is no dedicated prompt-injection defence.

## Recommended next steps

1. **Run the live evaluation** (both adapters, several repeats) and compare the score
   distributions with the bars; retune the bars if needed.
2. **Add a quality judge** for originality and naturalness (rubric plus human spot checks), beyond
   string checks.
3. **Run one live simulation** to see whether the generator stays fresh across days.
4. **Build the smallest host:** trigger loop, delivery, `delivery_log`, acting on boundaries.
5. **Learn per user from `delivery_log`**, for example favouring the kinds of starter the user
   replies to (deterministic, outside the prompt).
6. **Upgrade repetition detection** to embeddings once a dependency is acceptable.
