# Input context schema

One JSON object per decision. Only `now` and `preferences.proactive_enabled` are
required; everything else is optional and missing data is treated as uncertainty.

```json
{
  "now": "2026-11-24T18:40:00+01:00",
  "timezone": "Europe/Berlin",
  "preferences": {
    "proactive_enabled": true,
    "quiet_hours": { "start": "22:00", "end": "08:00" },
    "max_per_day": 2,
    "min_gap_minutes": 240,
    "max_unanswered": 2,
    "dismissal_cooldown_hours": 24,
    "active_window_minutes": 15,
    "snooze_until": null
  },
  "user_status": { "do_not_disturb": false },
  "history": [
    { "id": "m1", "role": "user", "timestamp": "2026-11-22T20:10:00+01:00", "text": "..." },
    { "id": "m2", "role": "assistant", "timestamp": "2026-11-22T20:11:00+01:00", "text": "..." },
    { "id": "m9", "role": "assistant", "timestamp": "2026-11-23T10:00:00+01:00", "text": "...",
      "proactive": true, "feedback": "dismissed" }
  ],
  "events": [
    { "id": "e1", "title": "Dentist", "start": "2026-11-25T16:00:00+01:00", "end": "2026-11-25T16:45:00+01:00", "source": "calendar" }
  ],
  "memories": [
    { "id": "k1", "text": "Prefers short messages.", "recorded_at": "2026-10-02T09:00:00+02:00" }
  ]
}
```

## Fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `now` | ISO 8601 string | **required** | Current time. Missing or invalid → gate blocks (`invalid_input`). |
| `timezone` | IANA name | offset of `now` | User's local zone for quiet hours and "today". Unknown name → `invalid_input`. |
| `preferences.proactive_enabled` | bool | **required** | Opt-in. Missing → `not_opted_in`; `false` → `disabled`. |
| `preferences.quiet_hours` | `{start, end}` "HH:MM" or `null` | `22:00`–`08:00` | Window may cross midnight. `null` = no quiet hours. |
| `preferences.max_per_day` | int | `2` | Proactive messages per local calendar day. `0` blocks all. |
| `preferences.min_gap_minutes` | int | `240` | Minimum time between proactive messages. `0` disables. |
| `preferences.max_unanswered` | int | `2` | Consecutive proactive messages with no user reply before the gate blocks until the user writes. `0` disables. |
| `preferences.dismissal_cooldown_hours` | int | `24` | Block after the user dismissed a proactive message. `0` disables. |
| `preferences.active_window_minutes` | int | `15` | If any message is newer than this, the conversation is ongoing; initiation does not apply. `0` disables. |
| `preferences.snooze_until` | ISO 8601 or `null` | `null` | Temporary pause chosen by the user. |
| `user_status.do_not_disturb` | bool | `false` | Host signal (OS focus mode, app setting). |
| `history[]` | list | `[]` | `id`, `role` (`user`/`assistant`), `timestamp`, `text`. Assistant messages the AI sent unprompted carry `"proactive": true`; the host may add `"feedback": "dismissed"`. |
| `events[]` | list | `[]` | Facts from the host (calendar, reminders): `id`, `title`, `start`, optional `end`, `source`, `notes`. |
| `memories[]` | list | `[]` | Stored notes from earlier sessions: `id`, `text`, optional `recorded_at`. |

Naive timestamps (no offset) are interpreted in `timezone`. Every `id` must be
unique across `history`, `events`, and `memories`, because decisions cite them
as `evidence`.

## Gate rules (in priority order)

| Rule id | Blocks when |
|---|---|
| `invalid_input` | `now` missing/invalid, unknown timezone, context not an object |
| `not_opted_in` | `proactive_enabled` missing |
| `disabled` | `proactive_enabled` is not `true` |
| `snoozed` | `now` < `snooze_until` |
| `do_not_disturb` | `user_status.do_not_disturb` is `true` |
| `quiet_hours` | local time inside the quiet window |
| `conversation_active` | last message newer than `active_window_minutes` |
| `recent_dismissal` | a proactive message was dismissed within `dismissal_cooldown_hours` |
| `unanswered_limit` | proactive messages since the last user message ≥ `max_unanswered` |
| `daily_limit` | proactive messages today ≥ `max_per_day` |
| `min_gap` | last proactive message newer than `min_gap_minutes` |

All matching rules are listed in `blocked_by`; the first one is the stated reason.

## Gate output

```json
{
  "gate": "PASS",
  "blocked_by": [],
  "decision": null,
  "signals": {
    "local_time": "Tue 24 Nov 2026 18:40 (Europe/Berlin)",
    "quiet_hours": "22:00-08:00",
    "minutes_until_quiet_hours": 200,
    "limits": { "max_per_day": 2, "min_gap_minutes": 240, "...": "..." },
    "message_count": 3,
    "minutes_since_last_message": 2785,
    "minutes_since_last_user_message": 2785,
    "dismissals_last_7_days": 0,
    "unanswered_proactive_streak": 0,
    "proactive_sent_today": 0,
    "minutes_since_last_proactive": null,
    "times": { "m1": "Sun 22 Nov 20:10 (46h 30m ago)", "e1": "starts tomorrow, Wed 25 Nov 16:00 (in 21h 20m)" }
  },
  "warnings": []
}
```

On `BLOCK`, `decision` holds the final WAIT decision (`confidence: 1.0`) to return verbatim.
