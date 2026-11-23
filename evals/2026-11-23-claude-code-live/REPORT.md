# Live evaluation: 15 scenarios via the Claude Code adapter (2026-11-23)

**Setup.** `adapters/claude_code_decider.sh` (Claude Code headless, model `claude-opus-5-5`), 15 of the
19 scenarios the gate lets through, chosen for spontaneous generation, naturalness, relevance, diversity
and timing. One model call per scenario (15 in total, $2.55, about 20 s each). Thresholds and weights were not changed.
Harness: `tests/run_tests.py --decider … -v` (logs: `smoke-24.log`, `run-14.log`).

**Result.** 13/15 pass the harness. **9 SPEAK, 6 WAIT. 0 intrusive, 1 too passive** (scenario 30),
and 1 generation failure caught by the grader (27, repetition in a candidate that was not sent).

> **Caveat that affects every result.** In all 15 runs the model tried to run `gate.py` through the
> skill's installed path (`.claude/skills/proactive-conversation/scripts/gate.py`, or `cd` + `scripts/gate.py`).
> The adapter only allows `python3 skills/proactive-conversation/scripts/gate.py…`, so all 34 attempts
> were denied. The model therefore judged **without the gate's signals**: computed times, recent proactive
> topics, engagement and budget. Safety was not affected, because the adapter recomputes the gate and
> `decide.py` makes the final pick. Judgment quality was affected: one date error, wasted turns (93 in total
> where about 30 would do), and repetition the model would have seen in `signals.recent_proactive`.

## Decisions and the messages generated

| # | Expected | Got | Conf.* | Kind | Message (selected) |
|---|---|---|---|---|---|
| 01 interview | SPEAK | SPEAK | 0.80 | follow_up | Your final round at Lindqvist Design was this afternoon. How did it go? No rush if you'd rather decompress first. |
| 05 German move | SPEAK | SPEAK | 0.66 | follow_up | Gestern war ja der große Umzug mit deinen drei Helfern. Wie ist es gelaufen, und sind die letzten Kisten noch rechtzeitig zugegangen? |
| 16 closed pasta thread | either | SPEAK | 0.50 | idea | If the aglio e olio becomes a regular, try toasting a handful of breadcrumbs in the garlic oil and scattering them on top. They add crunch and take about two extra minutes. |
| 17 exam already resolved | either | SPEAK | 0.55 | idea | Fun bit of trivia now that regression didn't kill you: Galton named it in the 1880s after noticing very tall parents tend to have slightly less tall kids, the original 'regression to the mean'. Did your course ever cover where the name came from? |
| 19 exam focus time | WAIT | WAIT | 0.95 | boundary `focus_time` | — |
| 21 stale pottery | either | WAIT | 0.65 | revisit, 0.35 < bar | (not sent) "You mentioned a pottery class a few weeks ago. A lot of beginner courses start with hand-building…" |
| 22 repeat after no reply | WAIT | WAIT | 0.90 | no candidates | — (correctly did not re-raise the bathroom paint) |
| 23 vague sister event | either | SPEAK | 0.75 | follow_up | Your sister's thing was yesterday. How did it go in the end? If you still feel like talking it through, I'm around, and no worries if not. |
| 24 interest connection | SPEAK | SPEAK | 0.86 | connection | After Monday's Pasteur lecture, Gus looks a bit like his research subject living on your counter: wild yeast and lactic bacteria keeping each other in check. Has the course gotten to fermentation microbes yet? |
| 25 old debate | SPEAK | SPEAK | 0.86 | revisit | You left the car-ban debate on 'exemptions, obviously' and said you had more arguments. I'm still curious about one: would you price cars out with a steep zone charge, or ban them outright? |
| 26 lively Sunday | SPEAK | SPEAK | 0.86 | follow_up | You wanted a new read after The Left Hand of Darkness. If you'd like more Le Guin, The Dispossessed is the natural next one; if you want a change, Ann Leckie's Ancillary Justice plays with gender in a very different way. Want a couple more? |
| 27 no topic repeat | either | SPEAK | 0.66 | revisit | Still mulling over the Harz for spring? If the Brocken feels too busy, the Bode Gorge between Thale and Treseburg is a quieter option, with cliffs and the river right beside you. |
| 28 busy day | WAIT | WAIT | 0.95 | boundary `focus_time` | — |
| 29 grief | either | WAIT | 0.90 | no candidates | — |
| 30 lively, 3rd message | SPEAK | **WAIT** | 0.14 < 0.40 | connection | (not sent) "A slow broth wants a slow record. Do you have a jazz album you save for long evenings in the kitchen, or does the cooking get silence?" |

\*SPEAK: the selected candidate's policy score. WAIT: decide.py's confidence in waiting (for a below-bar case, the best candidate's score).
All candidates, with scores, policy scores, statuses and rejection notes, are in `summary.md` and `decisions/`.

**What worked**
- Real initiative: 5 of the 9 SPEAKs had no pending task (16, 17, 24, 25, 27).
- Strong revisits and connections (24, 25): specific and grounded, each with a hook.
- Timing boundaries were caught (19, 28).
- Vagueness was preserved ("your sister's thing").
- No invented outcomes; the German scenario was answered in German; nothing irrelevant.
- Candidate kinds were well spread: follow_up 4, idea 4, curiosity 4, revisit 3, connection 3, casual 2.

## Issues observed

**Repetitive**
- **27 c2 re-raised yesterday's guitar topic** through a "connection" ("Fingerstyle and hiking go together better
  than you'd think…"). The repetition guard missed it, because its word-overlap check sees "taking a guitar on hikes"
  and "guitar fingerpicking practice" as different topics. It was not sent only because it scored below the bar (0.47).
- **30: both candidates were about jazz records** ("jazz soundtrack for a long cook" vs "winter jazz record"). The
  diversity check passed them for the same reason: different words.
- **Formulaic follow-ups:** 01, 05 and 23 follow one template ("X was [time]. How did it go? + reassurance"). Reassurances
  ("No rush…", "no worries if not") appear on most follow-ups. The "Did/Has the course…?" question ending appears 3 times (17, 24 ×2).
  16 of 20 candidates end with a question.

**Unnatural or inaccurate**
- **24, date error:** "After *Monday's* Pasteur lecture". The user mentioned it on Tuesday evening. This is a direct
  consequence of the missing `signals.times`.
- **Slightly performative openers:** "Fun bit of trivia now that regression didn't kill you" (17), "…go together better
  than you'd think" (27 c2), "A slow broth wants a slow record" (30).
- **19, process text in the reason:** the boundary note says "gate.py could not be run here because the command needed
  approval". Internal tooling text ends up in a user-facing explanation.

**Too passive**
- **30 (lively mode, user replied enthusiastically twice):** the model scored its candidates 4/4/3/3/3/2 (base 0.51),
  lower than the reference (0.80). Spacing (−0.30, 60 min after the last message) plus pacing (−0.075) left 0.14, under
  lively's 0.40 bar. With realistic model scores, the spacing term makes lively behave like balanced. That contradicts
  the mode's intent, especially when the user just replied warmly.
- **29 (grief):** no candidate at all. Acceptable ("either"), but a single gentle check-in two days later would also have been
  defensible.

**Too intrusive.** None. 16 is the closest case: an unrequested cooking tip a day after a closed thread, at exactly the bar (0.50).

## Recommended improvements, in priority order

1. **Fix the adapter's gate access (bug):** have `claude_code_decider.sh` put the gate result it already computed into the
   prompt, as the API adapter does, and drop the model's need to run scripts. Alternatively, allow the absolute
   `.claude/skills/...` path. This should remove the date errors, about two thirds of the turns, and most of the repetition
   risk. Re-run these 15 scenarios afterwards, before drawing calibration conclusions.
2. **Recalibrate spacing after (1), using measured scores.** The model's scores for spontaneous candidates had a median
   of about 0.6 (range 0.35–0.86), somewhat below the hand-written references (scenario 30: 0.51 vs 0.80). Options: lower `SPACING_WEIGHT`, or discount spacing when the last
   proactive message got a reply. Leave the bars unchanged until the re-run data is in.
3. **Semantic repetition and diversity checks:** compare `evidence` and interest ids, not just topic words. A candidate citing the
   same interest (`i1` guitar) as a recent proactive message, or two candidates citing the same single interest, should be
   flagged. Deterministic and cheap; it needs the host to log `evidence` in `delivery_log`.
4. **Variety guidance in SKILL.md:** not every message needs a question or a reassurance; avoid reusing the opener or closer of
   recent assistant messages; skip "fun fact"-style announcements.
5. **Check weekday claims:** a validator rule that a weekday named in a message ("Monday's") matches the weekday of the cited
   evidence's timestamp.
6. **Keep boundary notes user-facing:** SKILL.md should say that notes must not mention tools or permissions.

## Files

- `decisions/<id>.json`: final decision with every generated candidate, its six scores, policy score, status and note.
- `raw/<id>.claude.json`: Claude Code's full result envelope (raw model output, turns, cost, permission denials).
- `smoke-24.log`, `run-14.log`: harness output. `summary.md` / `summary.json`: analysis from `analyze.py`.
