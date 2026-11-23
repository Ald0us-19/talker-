# Worked examples

Each example shows the context, the opportunity report the model produces, and
what `decide.py` makes of it. The full suite (35 scenarios, 3 multi-day
simulations) lives in the repository's `tests/`.

## 1. SPEAK without any task: a connection between two interests

Saturday 10:30, balanced mode. There's no open question and no reminder. On Tuesday
the user mentioned that their history-of-science course covered Pasteur, then went off to feed
Gus, their sourdough starter (`m1`, `m3`, memory `k1`).

```json
{
  "boundary": null,
  "candidates": [
    { "id": "c1", "kind": "connection", "topic": "Pasteur and Gus the starter",
      "message": "Your course just did Pasteur, and Gus is basically his research topic living on your counter: wild yeast and bacteria keeping each other in check. Has the course said anything about sourdough?",
      "rationale": "Links this week's Pasteur lecture to the starter they feed; Saturday morning is baking time.",
      "evidence": ["m1", "m3", "k1"],
      "scores": { "relevance": 5, "novelty": 5, "naturalness": 4, "timing": 4, "engagement": 4, "interruption_cost": 1 } },
    { "id": "c2", "kind": "curiosity", "topic": "scientists from the course",
      "message": "Of everyone your science-history course has covered so far, who would you actually want to have dinner with?",
      "rationale": "A playful question about their course; less specific than c1.",
      "evidence": ["m1", "i2"],
      "scores": { "relevance": 4, "novelty": 4, "naturalness": 4, "timing": 3, "engagement": 3, "interruption_cost": 1 } },
    { "id": "c3", "kind": "casual", "topic": "weekend bake",
      "message": "Saturday morning, so I'm guessing Gus is getting fed. Bread on the menu this weekend?",
      "rationale": "Light remark matching their weekend baking habit.",
      "evidence": ["k1", "i1"],
      "scores": { "relevance": 4, "novelty": 2, "naturalness": 4, "timing": 5, "engagement": 3, "interruption_cost": 1 } }
  ]
}
```

`decide.py` scores them 0.86, 0.66 and 0.64. All three clear the balanced bar (0.50), and it picks c1:

```json
{ "decision": "SPEAK", "selected": "c1", "confidence": 0.86,
  "reason": "Picked c1 (connection: Pasteur and Gus the starter), score 0.86 >= 0.50 in balanced mode. Links this week's Pasteur lecture to the starter they feed; Saturday morning is baking time.",
  "message": "Your course just did Pasteur, and Gus is basically his research topic living on your counter: ...",
  "evidence": ["m1", "m3", "k1"], "boundary": null, "candidates": ["... the three, with score and status ..."] }
```

## 2. Revisiting an old discussion

Three weeks ago: "Hot take: city centres should ban private cars completely… We
should continue this sometime." A generic "Remember our car debate?" would be weak.
A good revisit brings a fresh angle:

> Still got those extra car-free city centre arguments? One to push against: Oslo mostly removed central parking instead of banning cars outright. Ban or squeeze?

`revisit`, scores 5/4/5/4/5/1, policy score 0.91 → SPEAK.

## 3. The same idea, different modes

A light idea after a closed thread ("If aglio e olio worked that well, cacio e pepe
is the same 15-minute trick…") scores 0.45. That is below balanced's bar (0.50), so the
answer is WAIT; in lively mode (bar 0.40) it would be sent. The candidate is the same; the
user's chosen mode decides.

## 4. Repetition is caught twice

Yesterday's proactive message was about guitar practice (`delivery_log` topic
"guitar fingerpicking practice"). SKILL.md tells the generator not to propose guitar again. If it
does anyway, `validate.py` rejects the candidate ("topic was already raised 24h ago;
topic cooldown is 48h") and `decide.py` moves on to the next one, a question about the user's
Harz hiking plan.

## 5. WAIT: boundaries and bad moments

- **Break request** (`m3`: "please don't message me until I reach out"): the report has
  `boundary: {"type": "requested_break", ...}` and no candidates. Result: WAIT with
  confidence 0.95, and the reason tells the host to set `snooze_until`.
- **Busy day** ("Back-to-back meetings until 6 today"): `focus_time` boundary. The
  interesting board-game topic waits for another day.
- **Grief:** no hobby chatter. A very gentle "I'm around" note scores interruption cost 4,
  policy score 0.21 → WAIT.

## 6. Hard rules come first

At 23:20 with quiet hours 22:00–07:30, the gate blocks before any generation happens:

```json
{ "decision": "WAIT", "reason": "Hard rule 'quiet_hours': quiet hours (22:00-07:30) are in effect; local time is 23:20.",
  "message": null, "confidence": 1.0, "evidence": [], "selected": null, "boundary": null, "candidates": [] }
```

A `"gate": {"gate": "PASS"}` object sent with the request changes nothing: it is ignored.
