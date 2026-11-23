# Live evaluation, run 2: same 15 scenarios, fixed adapter (2026-11-23)

**What changed since run 1.** The Claude Code adapter now hands the model the gate result it computed itself,
with all signals (times, recent proactive topics, engagement), and allows only `Read`. Before, the model's
attempts to run `gate.py` were all denied. Thresholds, weights and scenarios are unchanged. Model: `claude-opus-5-5`.
One call per scenario, 15 in total.

## Run 1 → run 2

| | Run 1 | Run 2 |
|---|---|---|
| Denied tool calls | 34 | **0** |
| Turns / time / cost | 93 / 290 s / $2.55 | **59 / 184 s / $2.21** |
| SPEAK / WAIT | 9 / 6 | 8 / 7 |
| Scenarios fully passing | 13/15 (27 generation, 30 decision) | 13/15 (29 generation, 30 decision) |
| Intrusive sends | 0 | 0 |
| Too passive | 1 (30) | 1 (30) |
| Date errors in messages | 1 ("Monday's lecture", actually Tuesday) | **0** (e.g. "too tired for book ideas on Thursday" is correct) |
| Repeated topic proposed (27, guitar) | yes, not sent | **no** |

14 of 15 decisions were the same in both runs. The one flip, 16 (SPEAK 0.50 → WAIT 0.35), is an "either" case sitting
at the bar. Where both runs spoke, the selected score changed by at most 0.12 (four were identical).

## Decisions and messages (run 2)

| # | Got | Conf. | Message (selected) |
|---|---|---|---|
| 01 interview | SPEAK | 0.80 | Your final round at Lindqvist Design was this afternoon. How did it go? No pressure if you'd rather decompress first. |
| 05 German move | SPEAK | 0.71 | Gestern war ja dein Umzug mit den drei Freunden. Wie ist es gelaufen, und hat es mit den letzten Kisten noch geklappt? |
| 16 closed pasta thread | WAIT | 0.65 | (cacio e pepe idea, 0.35, below bar) |
| 17 exam resolved | SPEAK | 0.55 | Now that regression didn't kill you after all: the name comes from Galton noticing that very tall parents tended to have kids closer to average height. He called it 'regression towards mediocrity', which is a slightly rude name for a method. |
| 19 focus time | WAIT | 0.95 | boundary `focus_time` (note is clean now, with no tooling text) |
| 21 stale pottery | WAIT | 0.65 | (revisit, 0.35, below bar) |
| 22 repeat after no reply | WAIT | 0.90 | no candidates, correctly |
| 23 vague sister event | SPEAK | 0.66 | Your sister's thing was yesterday, the one you were dreading a bit. If you feel like talking it through now that it's over, I'm here. No pressure either way. |
| 24 interest connection | SPEAK | 0.81 | After your Pasteur lecture, Gus looks a bit different: he's the kind of mixed culture of wild yeast and bacteria Pasteur spent years untangling, and Gus has kept his own balance going since well before October. Did the course get into fermentation microbes? |
| 25 old debate | SPEAK | 0.86 | You left our car-free city centre debate with more arguments still to come. Here's a test case: would your ban hold up for everything inside Berlin's Ringbahn, or is that too big an area for exemptions to cover? |
| 26 lively Sunday | SPEAK | 0.86 | You were too tired for book ideas on Thursday, so here are two: The Dispossessed if you want more Le Guin, or Ancillary Justice if the gender ideas in Left Hand stuck with you. Want a few more along those lines? |
| 27 no topic repeat | SPEAK | 0.78 | Still mulling the Harz for spring? If the Brocken feels too busy, the Harzer Hexenstieg is a quieter option: you can walk a section of it through the Bode gorge to Thale instead of doing the whole thing. |
| 28 busy day | WAIT | 0.95 | boundary `focus_time` |
| 29 grief | WAIT | 0.80 | (one cycling candidate, vetoed, not sent; see below) |
| 30 lively, 3rd message | **WAIT** | 0.81 | (jazz record for tonight's cooking, 0.19, below the 0.40 bar) |

Every candidate, with its scores and policy notes, is in `summary.md` and `decisions/`.

## Findings

**Improved by the fix**
- Times are right. Revisits and connections now use the user's own context better: Berlin's Ringbahn in 25, Thursday in 26.
- 27 no longer proposes guitar (the model can see `recent_proactive`).
- 19's boundary note no longer leaks tooling text.
- 36% faster, 13% cheaper.

**Still open**
1. **Too passive in lively mode (30), reproducible.** With signals, the model rated engagement 4 and its best candidate's base
   score was 0.56. Spacing (−0.30, 60 min after the last message) plus pacing (−0.075) still pushed it to 0.19, under the 0.40 bar.
   Two runs agree, so the spacing term is the cause, not missing context.
2. **29: hobby chatter proposed during grief.** "…an easy, unstructured spin this weekend… still counts toward that spring
   100 km…" The model scored it naturalness 2 and interruption cost 4, so the policy vetoed it. The safety net worked, but the
   generator shouldn't propose it at all.
3. **Small invented details.**
   - 26 c2 calls Miso "she"; the cat's sex isn't in the context.
   - 24 says Gus has existed "since well before October"; the memory only shows Gus existed by 12 October.
4. **Formula and phrasing.**
   - Follow-ups still end with a reassurance ("No pressure if…", "No pressure either way").
   - 17 reused run 1's quip ("now that regression didn't kill you").
   - Most candidates still end with a question.

## Recommendations (unchanged thresholds pending your decision)

1. **Spacing:** halve `SPACING_WEIGHT` (0.70 → 0.35), or skip spacing when the last proactive message got a reply. On run 2's
   numbers, 30 would score about 0.34 (still a WAIT) or about 0.49 (a SPEAK). Then re-check sim-02, so lively doesn't drift back
   into bursts.
2. **SKILL.md, sensitive contexts:** when the user is grieving or said they don't want to talk, propose no hobby or activity
   starters at all.
3. **SKILL.md, grounding:** no pronouns or attributes (sex, age, duration) that the context doesn't state.
4. **SKILL.md, variety:** a reassurance and a closing question are optional, not default.
5. **Repetition and diversity by evidence or interest ids** (from run 1); still worth doing, even though run 2 had no repeat.

## Files

`decisions/`, `raw/` (Claude Code envelopes: 0 permission denials), `run.log`, `summary.md` / `summary.json`
(from `../2026-11-23-claude-code-live/analyze.py`).
