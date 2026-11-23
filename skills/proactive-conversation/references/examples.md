# Worked examples

Each example shows the input, what the gate says, and the decision. The full
suite (23 scenarios) lives in the repository's `tests/scenarios/`.

## 1. SPEAK: the user promised to report back

Input (Tuesday 24 Nov, 18:40, Berlin):

```json
{
  "now": "2026-11-24T18:40:00+01:00",
  "timezone": "Europe/Berlin",
  "preferences": { "proactive_enabled": true, "quiet_hours": { "start": "22:00", "end": "08:00" } },
  "history": [
    { "id": "m1", "role": "user", "timestamp": "2026-11-22T20:10:00+01:00",
      "text": "Tuesday's the big day: final-round interview at Lindqvist Design, 2pm. Honestly kind of terrified." },
    { "id": "m2", "role": "assistant", "timestamp": "2026-11-22T20:11:00+01:00",
      "text": "That's a big step. If you want, we can do a practice run of your portfolio walkthrough before then." },
    { "id": "m3", "role": "user", "timestamp": "2026-11-22T20:16:00+01:00",
      "text": "Maybe Monday. Either way I'll tell you how it goes." }
  ]
}
```

Gate: `PASS`; `times.m1 = "Sun 22 Nov 20:10 (46h 30m ago)"`, no proactive messages sent, 200 min until quiet hours.

Reasoning: open loop ("I'll tell you how it goes") tied to an event that ended
about 4.5 hours ago; outcome unknown → ask, don't assume.

```json
{
  "decision": "SPEAK",
  "reason": "The user said they'd report back on Tuesday's 2pm final-round interview at Lindqvist Design; it ended about 4.5 hours ago and nothing has been said since.",
  "message": "How did the final round at Lindqvist go? Happy to hear about it whenever you feel like it.",
  "confidence": 0.82,
  "evidence": ["m1", "m3"]
}
```

Rejected alternatives: "Congrats on the job!" (invents an outcome),
"Hey! Just checking in" (generic), "You haven't told me how it went…" (pressure).

## 2. WAIT by hard rule: quiet hours

Same history, but `now` is `23:20` and quiet hours are `22:00–07:30`. The gate
blocks before any judgment happens, and its decision is returned verbatim:

```json
{
  "decision": "WAIT",
  "reason": "Hard rule 'quiet_hours': quiet hours (22:00-07:30) are in effect; local time is 23:20.",
  "message": null,
  "confidence": 1.0,
  "evidence": []
}
```

## 3. WAIT by judgment: the user asked for space

The gate passes (preferences were never updated), and there is a tempting
follow-up: a dentist appointment ended an hour ago. But the day before, the
user wrote (`m3`): "I'm going offline for a few days, please don't message me
until I reach out."

```json
{
  "decision": "WAIT",
  "reason": "The user asked not to be messaged until they reach out, which outranks the dentist follow-up. The host should pause proactive messages for this user.",
  "message": null,
  "confidence": 0.97,
  "evidence": ["m3"]
}
```

## 4. Grounding under vagueness

User: "My sister's thing is on Saturday and I'm dreading it a bit. Long story."
then "Maybe after. Not tonight." It is now Sunday afternoon. SPEAK and WAIT are
both defensible; what matters is that a SPEAK message keeps the event vague:

```json
{
  "decision": "SPEAK",
  "reason": "The user was dreading their sister's event on Saturday and said 'maybe after' to talking it through; it is now the day after. Details are unknown, so keep it open.",
  "message": "Your sister's thing was yesterday. If you feel like talking it through now that it's over, I'm happy to listen; no pressure either way.",
  "confidence": 0.66,
  "evidence": ["m1", "m3"]
}
```

"How was your sister's wedding?" would be a fabrication and fails the test suite.
