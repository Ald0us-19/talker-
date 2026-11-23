# Input context schema

One JSON object per decision. Only `now` and `preferences.proactive_enabled` are
required. Everything else is optional; missing data is treated as uncertainty,
and unreadable values are interpreted in the stricter direction.

```json
{
  "now": "2026-11-28T10:30:00+01:00",
  "timezone": "Europe/Berlin",
  "preferences": {
    "proactive_enabled": true,
    "mode": "balanced",
    "quiet_hours": { "start": "22:00", "end": "08:00" },
    "snooze_until": null
  },
  "user_status": { "do_not_disturb": false },
  "profile": {
    "interests": [ { "id": "i1", "topic": "sourdough baking", "note": "bakes on weekends" } ]
  },
  "memories": [
    { "id": "k1", "text": "Their sourdough starter is called Gus.", "recorded_at": "2026-10-12T11:00:00+02:00" }
  ],
  "history": [
    { "id": "m1", "role": "user", "timestamp": "2026-11-24T21:10:00+01:00", "text": "Course tonight was all about Pasteur." },
    { "id": "p1", "role": "assistant", "timestamp": "2026-11-25T18:00:00+01:00", "text": "...", "proactive": true }
  ],
  "delivery_log": [
    { "id": "d1", "message_id": "p1", "sent_at": "2026-11-25T18:00:00+01:00", "topic": "jazz kissa bars",
      "text": "...", "replied_at": "2026-11-25T18:20:00+01:00", "dismissed_at": null }
  ],
  "events": [
    { "id": "e1", "title": "Dentist", "start": "2026-11-30T16:00:00+01:00", "end": "2026-11-30T16:45:00+01:00", "source": "calendar" }
  ]
}
```

## Fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `now` | ISO 8601 string | **required** | Current time. Missing or invalid → gate blocks (`invalid_input`). |
| `timezone` | IANA name | offset of `now` | User's local zone for quiet hours and "today". Unknown name → `invalid_input`. |
| `preferences.proactive_enabled` | bool | **required** | Explicit opt-in. Missing → `not_opted_in`; anything but `true` → `disabled`. |
| `preferences.mode` | `quiet` / `balanced` / `lively` | `balanced` | Proactivity preset (table below). Unknown value → `quiet`, with a warning. |
| `preferences.quiet_hours` | `{start, end}` "HH:MM" or `null` | `22:00`–`08:00` | Window may cross midnight. `null` = no quiet hours. |
| `preferences.snooze_until` | ISO 8601 or `null` | `null` | A pause or break the user asked for. Unreadable value → treated as snoozed. |
| `preferences.max_per_day` | int | from mode | User's own cap; overrides the mode preset. `0` blocks all. |
| `preferences.min_gap_minutes` | int | from mode | User's own minimum gap; overrides the mode preset. |
| `preferences.max_unanswered` | int | `2` | Proactive messages in a row without any user message before the gate waits for the user. `0` disables. |
| `preferences.dismissal_cooldown_hours` | int | `24` | Block after a dismissal, counted from `dismissed_at`. `0` disables. |
| `preferences.active_window_minutes` | int | `15` | A message newer than this means an ongoing conversation; initiation does not apply. |
| `preferences.topic_cooldown_hours` | int | `48` | A topic raised proactively within this window cannot be raised again. |
| `user_status.do_not_disturb` | bool | `false` | Host signal (OS focus mode, app setting). |
| `history[]` | list | `[]` | `id`, `role` (`user`/`assistant`), `timestamp`, `text`. Proactive assistant messages carry `"proactive": true`. |
| `delivery_log[]` | list | `[]` | **The host's authoritative record of proactive messages:** `message_id`, `sent_at`, `topic`, `text`, `replied_at`, `dismissed_at`. Keep it even when history is truncated. |
| `profile.interests[]` | list | `[]` | `id`, `topic`, optional `note`. Citable as evidence. |
| `memories[]` | list | `[]` | Stored notes: `id`, `text`, optional `recorded_at`. Citable as evidence. |
| `events[]` | list | `[]` | Facts from the host (calendar, reminders): `id`, `title`, `start`, optional `end`. |

Naive timestamps (no offset) are interpreted in `timezone`. Every `id` must be
unique across `history`, `events`, `memories`, and `profile.interests`.

## Modes

| Mode | `max_per_day` | `min_gap_minutes` | Speak bar (decide.py) |
|---|---|---|---|
| `quiet` | 2 | 240 | 0.65 |
| `balanced` (default) | 4 | 90 | 0.50 |
| `lively` | 8 | 30 | 0.40 |

These are ceilings, not targets: `decide.py` also applies pacing and spacing
penalties, so messages spread across the waking day.

## How the proactive history is read

- Proactive messages are the union of `delivery_log` entries and `history` entries
  with `"proactive": true`, deduplicated by message id; the log's send time wins.
  A truncated history therefore cannot hide sends. Without a log, the gate warns
  that limits may undercount.
- An unreadable send time counts as *now* (it blocks the gap and counts for today).
- Dismissal cooldowns run from `dismissed_at`. If a message is marked dismissed without
  `dismissed_at`, the send time is used and a warning is added. A malformed `dismissed_at` counts as *now*.
- A proactive message counts as answered if it has a valid `replied_at`, or if any
  user message in `history` came after it.

## Gate rules (in priority order)

| Rule id | Blocks when |
|---|---|
| `invalid_input` | `now` missing/invalid, unknown timezone, context not an object |
| `not_opted_in` | `proactive_enabled` missing |
| `disabled` | `proactive_enabled` is not `true` |
| `snoozed` | `now` < `snooze_until`, or `snooze_until` unreadable |
| `do_not_disturb` | `user_status.do_not_disturb` is `true` |
| `quiet_hours` | local time inside the quiet window |
| `conversation_active` | last message newer than `active_window_minutes` |
| `recent_dismissal` | a dismissal within `dismissal_cooldown_hours` |
| `unanswered_limit` | trailing unanswered proactive messages ≥ `max_unanswered` |
| `daily_limit` | proactive messages today ≥ `max_per_day` |
| `min_gap` | last proactive message newer than `min_gap_minutes` |

All matching rules are listed in `blocked_by`; the first one is the stated reason.

## Gate signals (on PASS)

`local_time`, `quiet_hours`, `minutes_until_quiet_hours`, `waking_minutes`, `mode`,
`speak_bar`, `limits`, `message_count`, `minutes_since_last_message`,
`minutes_since_last_user_message`, `rate_limit_source`, `dismissals_last_7_days`,
`unanswered_proactive_streak`, `proactive_sent_today`, `remaining_today`,
`minutes_since_last_proactive`, `engagement` (counts of replied / unanswered / dismissed
over 14 days), `recent_proactive` (up to 20 recent sends with `when`, `hours_ago`, `topic`,
`text`, `outcome`), and `times` (a human-readable "when" for every message, event and memory).
