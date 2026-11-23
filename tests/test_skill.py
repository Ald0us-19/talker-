"""Unit tests for the deterministic parts: gate, validator, decision engine, grader, simulator.

Candidate generation (model output) and decision-making (decide.py) are tested
separately: the validator/grader tests judge reports, the DecideTests feed fixed,
hand-built candidate sets to the policy.

Run:  python3 -m unittest discover -s tests -v
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "proactive-conversation" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import decide  # noqa: E402
import gate  # noqa: E402
import run_tests  # noqa: E402
import simulate  # noqa: E402
import validate  # noqa: E402

BASE = {
    "now": "2026-11-24T18:40:00+01:00",
    "timezone": "Europe/Berlin",
    "preferences": {"proactive_enabled": True, "quiet_hours": {"start": "22:00", "end": "08:00"}},
    "profile": {"interests": [{"id": "i1", "topic": "sourdough"}, {"id": "i2", "topic": "jazz records"}]},
    "history": [
        {"id": "m1", "role": "user", "timestamp": "2026-11-22T20:10:00+01:00", "text": "Interview Tuesday 2pm."},
        {"id": "m2", "role": "user", "timestamp": "2026-11-22T20:16:00+01:00", "text": "I'll tell you how it goes."},
    ],
}


def ctx(**changes):
    c = copy.deepcopy(BASE)
    for key, value in changes.items():
        if key == "prefs":
            c["preferences"].update(value)
        else:
            c[key] = value
    return c


def proactive(mid, ts, **extra):
    return {"id": mid, "role": "assistant", "timestamp": ts, "text": "...", "proactive": True, **extra}


def log(i, sent, **extra):
    return {"id": f"d{i}", "message_id": f"x{i}", "sent_at": sent, **extra}


def cand(cid="c1", kind="follow_up", topic="interview", message="How did the interview go?",
         evidence=("m1",), scores=(5, 4, 5, 5, 4, 1), **extra):
    keys = ("relevance", "novelty", "naturalness", "timing", "engagement", "interruption_cost")
    return {"id": cid, "kind": kind, "topic": topic, "message": message, "rationale": "Test candidate.",
            "evidence": list(evidence), "scores": dict(zip(keys, scores)), **extra}


def report(*cands, boundary=None):
    return {"boundary": boundary, "candidates": list(cands)}


# ---------------------------------------------------------------- gate


class GateTests(unittest.TestCase):
    def test_base_passes_with_balanced_default(self):
        result = gate.evaluate(ctx())
        self.assertEqual(result["gate"], "PASS", result)
        self.assertEqual(result["signals"]["mode"], "balanced")
        self.assertEqual(result["signals"]["limits"]["max_per_day"], 4)
        self.assertEqual(result["signals"]["limits"]["min_gap_minutes"], 90)
        self.assertIn("m1", result["signals"]["times"])

    def test_overnight_quiet_hours_boundaries(self):
        q = {"quiet_hours": {"start": "22:00", "end": "07:30"}}
        cases = {"21:59": "PASS", "22:00": "BLOCK", "02:00": "BLOCK", "07:29": "BLOCK", "07:30": "PASS"}
        for hhmm, expected in cases.items():
            day = "2026-11-25" if hhmm < "12:00" else "2026-11-24"
            result = gate.evaluate(ctx(now=f"{day}T{hhmm}:00+01:00", prefs=q))
            self.assertEqual(result["gate"], expected, hhmm)

    def test_quiet_hours_null_means_none_and_default_applies_when_absent(self):
        late = "2026-11-24T23:30:00+01:00"
        self.assertEqual(gate.evaluate(ctx(now=late, prefs={"quiet_hours": None}))["gate"], "PASS")
        c = ctx(now=late)
        del c["preferences"]["quiet_hours"]
        result = gate.evaluate(c)
        self.assertEqual(result["blocked_by"], ["quiet_hours"])
        self.assertTrue(any("default" in w for w in result["warnings"]))
        self.assertEqual(gate.evaluate(ctx())["signals"]["waking_minutes"], 14 * 60)

    def test_timezone_conversion_uses_local_clock(self):
        self.assertEqual(gate.evaluate(ctx(now="2026-11-24T21:30:00Z"))["blocked_by"], ["quiet_hours"])

    def test_invalid_inputs_fail_closed(self):
        for bad in [ctx(now="yesterday"), ctx(timezone="Mars/Olympus"), "not an object", ctx(now=None)]:
            result = gate.evaluate(bad)
            self.assertEqual(result["blocked_by"][0], "invalid_input")
            self.assertEqual(result["decision"]["decision"], "WAIT")

    def test_block_decision_follows_the_output_contract(self):
        blocked = ctx(now="2026-11-24T23:30:00+01:00")
        decision = gate.evaluate(blocked)["decision"]
        self.assertTrue(validate.validate(decision, blocked)["valid"], validate.validate(decision, blocked))
        self.assertEqual(decision, decide.decide(blocked, report()))

    def test_opt_in_required(self):
        c = ctx()
        del c["preferences"]["proactive_enabled"]
        self.assertEqual(gate.evaluate(c)["blocked_by"][0], "not_opted_in")
        self.assertEqual(gate.evaluate(ctx(prefs={"proactive_enabled": "yes"}))["blocked_by"][0], "disabled")

    def test_mode_presets_and_overrides(self):
        for mode, (cap, gap) in {"quiet": (2, 240), "balanced": (4, 90), "lively": (8, 30)}.items():
            limits = gate.evaluate(ctx(prefs={"mode": mode}))["signals"]["limits"]
            self.assertEqual((limits["max_per_day"], limits["min_gap_minutes"]), (cap, gap), mode)
        override = gate.evaluate(ctx(prefs={"mode": "lively", "max_per_day": 3}))["signals"]["limits"]
        self.assertEqual((override["max_per_day"], override["min_gap_minutes"]), (3, 30))

    def test_unknown_mode_falls_back_to_quiet(self):
        result = gate.evaluate(ctx(prefs={"mode": "LOUD"}))
        self.assertEqual(result["signals"]["mode"], "quiet")
        self.assertTrue(any("not quiet/balanced/lively" in w for w in result["warnings"]))

    def test_mode_changes_min_gap(self):
        history = BASE["history"] + [proactive("p1", "2026-11-24T18:00:00+01:00"),
                                     {"id": "u1", "role": "user", "timestamp": "2026-11-24T18:05:00+01:00", "text": "ha"}]
        self.assertEqual(gate.evaluate(ctx(history=history, prefs={"mode": "lively"}))["gate"], "PASS")
        self.assertIn("min_gap", gate.evaluate(ctx(history=history, prefs={"mode": "balanced"}))["blocked_by"])

    def test_fix_malformed_snooze_blocks(self):
        for bad in ["next tuesday", 12, "", {"until": "friday"}]:
            result = gate.evaluate(ctx(prefs={"snooze_until": bad}))
            self.assertEqual(result["blocked_by"][0], "snoozed", bad)
        self.assertEqual(gate.evaluate(ctx(prefs={"snooze_until": "2026-11-24T09:00:00+01:00"}))["gate"], "PASS")

    def test_fix_dismissal_cooldown_runs_from_dismissed_at(self):
        late = [log(1, "2026-11-23T10:00:00+01:00", dismissed_at="2026-11-24T16:00:00+01:00")]
        self.assertIn("recent_dismissal", gate.evaluate(ctx(delivery_log=late))["blocked_by"])
        old = [log(1, "2026-11-23T10:00:00+01:00", dismissed_at="2026-11-23T10:05:00+01:00")]
        self.assertNotIn("recent_dismissal", gate.evaluate(ctx(delivery_log=old))["blocked_by"])

    def test_fix_malformed_dismissed_at_counts_as_now(self):
        bad = [log(1, "2026-11-20T10:00:00+01:00", dismissed_at="sometime")]
        result = gate.evaluate(ctx(delivery_log=bad))
        self.assertIn("recent_dismissal", result["blocked_by"])
        self.assertTrue(any("dismissed_at is malformed" in w for w in result["warnings"]))

    def test_missing_dismissed_at_falls_back_to_send_time_with_warning(self):
        history = BASE["history"] + [proactive("p1", "2026-11-24T08:00:00+01:00", feedback="dismissed")]
        result = gate.evaluate(ctx(history=history))
        self.assertIn("recent_dismissal", result["blocked_by"])
        self.assertTrue(any("without dismissed_at" in w for w in result["warnings"]))

    def test_fix_delivery_log_counts_sends_missing_from_history(self):
        sends = [log(i, f"2026-11-24T{h}:00:00+01:00", replied_at=f"2026-11-24T{h}:10:00+01:00")
                 for i, h in enumerate(["08", "10", "12", "15"], 1)]
        result = gate.evaluate(ctx(delivery_log=sends))
        self.assertEqual(result["signals"]["proactive_sent_today"], 4)
        self.assertIn("daily_limit", result["blocked_by"])
        self.assertEqual(result["signals"]["rate_limit_source"], "delivery_log")

    def test_log_and_history_are_deduplicated(self):
        history = BASE["history"] + [proactive("x1", "2026-11-24T10:00:00+01:00")]
        sends = [log(1, "2026-11-24T10:00:00+01:00", replied_at="2026-11-24T10:05:00+01:00")]
        result = gate.evaluate(ctx(history=history, delivery_log=sends))
        self.assertEqual(result["signals"]["proactive_sent_today"], 1)
        self.assertEqual(result["signals"]["rate_limit_source"], "delivery_log+history")

    def test_fix_unreadable_send_time_counts_as_now(self):
        history = BASE["history"] + [proactive("p1", "garbage")]
        result = gate.evaluate(ctx(history=history))
        self.assertIn("min_gap", result["blocked_by"])
        self.assertEqual(result["signals"]["proactive_sent_today"], 1)

    def test_no_delivery_log_warns(self):
        self.assertTrue(any("No delivery_log" in w for w in gate.evaluate(ctx())["warnings"]))

    def test_unanswered_streak_uses_replies_and_user_messages(self):
        sends = [log(1, "2026-11-23T10:00:00+01:00", replied_at="2026-11-23T10:30:00+01:00"),
                 log(2, "2026-11-23T18:00:00+01:00"), log(3, "2026-11-24T09:00:00+01:00")]
        self.assertIn("unanswered_limit", gate.evaluate(ctx(delivery_log=sends))["blocked_by"])
        history = BASE["history"] + [{"id": "u1", "role": "user", "timestamp": "2026-11-24T08:00:00+01:00", "text": "morning"}]
        result = gate.evaluate(ctx(delivery_log=sends, history=history))
        self.assertEqual(result["signals"]["unanswered_proactive_streak"], 1)

    def test_recent_proactive_and_engagement_signals(self):
        sends = [log(1, "2026-11-22T10:00:00+01:00", topic="jazz", text="Jazz thought", replied_at="2026-11-22T10:10:00+01:00"),
                 log(2, "2026-11-23T10:00:00+01:00", topic="bread", text="Bread thought", dismissed_at="2026-11-23T10:01:00+01:00")]
        sig = gate.evaluate(ctx(delivery_log=sends))["signals"]
        self.assertEqual([r["topic"] for r in sig["recent_proactive"]], ["bread", "jazz"])
        self.assertEqual([r["outcome"] for r in sig["recent_proactive"]], ["dismissed", "replied"])
        self.assertEqual(sig["engagement"], {"proactive_last_14_days": 2, "replied": 1, "unanswered": 0, "dismissed": 1})

    def test_bad_timestamps_warn_and_are_ignored(self):
        history = BASE["history"] + [
            {"id": "x", "role": "user", "timestamp": "soon", "text": "?"},
            {"id": "f", "role": "user", "timestamp": "2027-01-01T00:00:00+01:00", "text": "future"},
        ]
        result = gate.evaluate(ctx(history=history))
        self.assertEqual(result["gate"], "PASS")
        self.assertTrue(any("message x has no valid timestamp" in w for w in result["warnings"]))
        self.assertTrue(any("message f is timestamped in the future" in w for w in result["warnings"]))

    def test_event_status_labels(self):
        events = [
            {"id": "e1", "title": "a", "start": "2026-11-24T20:00:00+01:00"},
            {"id": "e2", "title": "b", "start": "2026-11-24T18:00:00+01:00", "end": "2026-11-24T19:00:00+01:00"},
            {"id": "e3", "title": "c", "start": "2026-11-24T14:00:00+01:00", "end": "2026-11-24T15:00:00+01:00"},
        ]
        times = gate.evaluate(ctx(events=events))["signals"]["times"]
        self.assertTrue(times["e1"].startswith("starts today"))
        self.assertTrue(times["e2"].startswith("ongoing"))
        self.assertTrue(times["e3"].startswith("ended today"))


# ---------------------------------------------------------------- opportunity reports (generation output)


class ReportValidationTests(unittest.TestCase):
    def errors(self, rep, context=None):
        return validate.validate_report(rep, context or ctx())

    def test_good_report_is_valid(self):
        rep = report(cand(), cand("c2", "connection", "jazz and bread", "Bread and jazz both reward patience: long proofs, long solos.", ["i1", "i2"]))
        result = self.errors(rep)
        self.assertTrue(result["valid"], result)

    def test_at_most_three_candidates(self):
        rep = report(*[cand(f"c{i}", topic=t) for i, t in enumerate(["alpha", "bravo", "charlie", "delta"], 1)])
        self.assertTrue(any("at most 3" in e for e in self.errors(rep)["errors"]))

    def test_grounding_and_schema(self):
        cases = [
            (cand(evidence=["m99"]), "not found in context"),
            (cand(evidence=[]), "at least one context id"),
            (cand(kind="chitchat"), "'kind' must be one of"),
            (cand(scores=(6, 4, 5, 5, 4, 1)), "scores.relevance"),
            (cand(message=""), "'message' must be a non-empty string"),
        ]
        for c, fragment in cases:
            errs = self.errors(report(c))["candidate_errors"].get("c1", [])
            self.assertTrue(any(fragment in e for e in errs), (fragment, errs))

    def test_topic_diversity(self):
        rep = report(cand(topic="jazz records"), cand("c2", "idea", "jazz records again", "Different message about records entirely.", ["i2"]))
        self.assertTrue(any("same topic as c1" in e for e in self.errors(rep)["candidate_errors"]["c2"]))

    def test_all_same_kind_warns(self):
        rep = report(*[cand(f"c{i}", "idea", t, f"Idea number {t} with its own words.", ["i1"]) for i, t in
                       enumerate(["alpha", "bravo", "charlie"], 1)])
        self.assertTrue(any("same kind" in w for w in self.errors(rep)["warnings"]))

    def test_repetition_topic_cooldown(self):
        c = ctx(delivery_log=[log(1, "2026-11-24T08:00:00+01:00", topic="jazz records", text="A jazz thought.",
                                  replied_at="2026-11-24T08:10:00+01:00")])
        errs = self.errors(report(cand(topic="jazz records", message="Another jazz idea for you.")), c)["candidate_errors"]["c1"]
        self.assertTrue(any("topic cooldown" in e for e in errs))

    def test_repetition_topic_without_reply_stays_off_limits_for_days(self):
        # Sent 4 days ago; the user has not written since (any later user message counts as an answer).
        c = ctx(history=[{"id": "m1", "role": "user", "timestamp": "2026-11-18T20:00:00+01:00", "text": "Repainting soon."}],
                delivery_log=[log(1, "2026-11-20T18:00:00+01:00", topic="bathroom paint", text="Bathroom colour?")])
        errs = self.errors(report(cand(topic="bathroom paint", message="Did the bathroom paint plan move?")), c)["candidate_errors"]["c1"]
        self.assertTrue(any("got no reply" in e for e in errs))

    def test_repetition_near_duplicate_message_and_repeated_question(self):
        c = ctx(history=BASE["history"] + [{"id": "a1", "role": "assistant", "timestamp": "2026-11-22T20:17:00+01:00",
                                             "text": "Which jazz record would you save from a fire?"}])
        errs = self.errors(report(cand(topic="desert island records", message="Which jazz record would you save from a fire?")), c)
        self.assertTrue(any("repeats earlier assistant message a1" in e for e in errs["candidate_errors"]["c1"]))

    def test_style_errors(self):
        for msg, fragment in [
            ("Hey there! How did the interview go?", "bare greeting"),
            ("Just checking in about the interview.", "just checking in"),
            ("Hope you're doing well. How was the interview?", "hope you're well"),
            ("You haven't replied about the interview.", "haven't heard back"),
            ("How did the interview go?! So excited!", "more than one '!'"),
            ("I'm so excited to hear about the interview.", "artificial enthusiasm"),
            ("x" * 400, "max 320"),
        ]:
            errs = self.errors(report(cand(message=msg)))["candidate_errors"].get("c1", [])
            self.assertTrue(any(fragment in e for e in errs), (msg, errs))

    def test_specific_question_is_not_flagged_as_generic(self):
        msg = "How are you feeling after the interview? Happy to debrief if useful."
        self.assertTrue(self.errors(report(cand(message=msg)))["valid"])

    def test_emoji_warning_depends_on_user_style(self):
        rep = report(cand(message="How did the interview go? 🙂"))
        self.assertTrue(self.errors(rep)["warnings"])
        emoji_user = ctx(history=BASE["history"] + [
            {"id": "m3", "role": "user", "timestamp": "2026-11-22T20:17:00+01:00", "text": "wish me luck 🙂"}])
        self.assertFalse(self.errors(rep, emoji_user)["warnings"])

    def test_boundary_format(self):
        self.assertTrue(self.errors(report(boundary={"type": "requested_break", "note": "asked for a break", "evidence": ["m2"]}))["valid"])
        self.assertFalse(self.errors(report(boundary={"type": "nap", "note": "", "evidence": []}))["valid"])

    def test_extract_json_variants(self):
        raw = json.dumps(report())
        for text in [raw, f"```json\n{raw}\n```", f"Here you go:\n{raw}\nDone.", '{"a": "brace } in \\" string", "b": 1}']:
            obj, _ = validate.extract_json(text)
            self.assertIsInstance(obj, dict, text)
        self.assertIsNone(validate.extract_json("no json here")[0])


# ---------------------------------------------------------------- decision engine (fixed candidates)


class DecideTests(unittest.TestCase):
    def test_picks_best_eligible_candidate(self):
        d = decide.decide(ctx(), report(cand("c1", scores=(4, 3, 4, 3, 3, 2)),
                                        cand("c2", "connection", "jazz and bread", "Bread and jazz both reward patience: long proofs, long solos.",
                                             ["i1", "i2"], (5, 5, 4, 4, 4, 1))))
        self.assertEqual((d["decision"], d["selected"]), ("SPEAK", "c2"))
        self.assertEqual(d["message"], d["candidates"][1]["message"])
        self.assertTrue(validate.validate(d, ctx())["valid"], validate.validate(d, ctx()))

    def test_no_preference_for_silence(self):
        mid = cand(scores=(4, 3, 4, 3, 3, 2))  # a decent, not exceptional, starter
        self.assertGreaterEqual(decide.base_score(mid["scores"]), 0.5)
        self.assertEqual(decide.decide(ctx(), report(mid))["decision"], "SPEAK")

    def test_mode_bar_changes_the_decision(self):
        c = cand(scores=(3, 4, 3, 3, 3, 1))  # base 0.5
        results = {m: decide.decide(ctx(prefs={"mode": m}), report(c))["decision"] for m in ("quiet", "balanced", "lively")}
        self.assertEqual(results, {"quiet": "WAIT", "balanced": "SPEAK", "lively": "SPEAK"})

    def test_vetoes(self):
        for scores, why in [((5, 5, 5, 1, 5, 1), "timing"), ((5, 5, 2, 5, 5, 1), "forced"), ((5, 5, 5, 5, 5, 5), "disruptive")]:
            d = decide.decide(ctx(), report(cand(scores=scores)))
            self.assertEqual(d["decision"], "WAIT")
            self.assertIn(why, d["candidates"][0]["note"])

    def test_boundary_forces_wait(self):
        d = decide.decide(ctx(), report(cand(), boundary={"type": "requested_break", "note": "Asked for a break", "evidence": ["m2"]}))
        self.assertEqual(d["decision"], "WAIT")
        self.assertEqual(d["evidence"], ["m2"])
        self.assertIn("snooze_until", d["reason"])

    def test_malformed_boundary_still_waits(self):
        self.assertEqual(decide.decide(ctx(), report(cand(), boundary="stop"))["decision"], "WAIT")

    def test_gate_block_wins_over_great_candidates(self):
        d = decide.decide(ctx(now="2026-11-24T23:30:00+01:00"), report(cand()))
        self.assertEqual(d["decision"], "WAIT")
        self.assertIn("quiet_hours", d["reason"])

    def test_fix_supplied_gate_is_ignored_by_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp, "request.json")
            request.write_text(json.dumps({"context": ctx(now="2026-11-24T23:30:00+01:00"),
                                           "gate": {"gate": "PASS", "blocked_by": []}}))
            rep = Path(tmp, "report.json")
            rep.write_text(json.dumps(report(cand())))
            out = subprocess.run([sys.executable, str(SCRIPTS / "decide.py"), str(rep), "--context", str(request)],
                                 capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout)["decision"], "WAIT")
        self.assertIn("ignoring the supplied 'gate'", out.stderr)

    def test_fix_validator_rejects_speak_when_gate_blocks(self):
        spoken = decide.decide(ctx(), report(cand()))
        self.assertEqual(spoken["decision"], "SPEAK")
        result = validate.validate(spoken, ctx(now="2026-11-24T23:30:00+01:00"))
        self.assertTrue(any("hard rule 'quiet_hours'" in e for e in result["errors"]))

    def test_repetition_probe_is_rejected_but_alternative_chosen(self):
        c = ctx(delivery_log=[log(1, "2026-11-24T08:00:00+01:00", topic="interview", text="How did the interview go?",
                                  replied_at="2026-11-24T08:05:00+01:00")])
        d = decide.decide(c, report(cand(), cand("c2", "idea", "sourdough hydration", "A wetter dough gives bigger holes, if Gus is up to it.", ["i1"])))
        self.assertEqual(d["candidates"][0]["status"], "rejected")
        self.assertEqual(d["selected"], "c2")

    def test_pacing_spacing_and_backoff_lower_scores(self):
        quiet_day = decide.penalties(gate.evaluate(ctx())["signals"])
        self.assertEqual(sum(quiet_day.values()), 0)
        busy = ctx(prefs={"mode": "lively"}, delivery_log=[
            log(1, "2026-11-24T16:00:00+01:00", replied_at="2026-11-24T16:05:00+01:00"),
            log(2, "2026-11-24T18:00:00+01:00")])
        adjust = decide.penalties(gate.evaluate(busy)["signals"])
        self.assertGreater(adjust["pacing"], 0)
        self.assertGreater(adjust["spacing"], 0)
        self.assertGreater(adjust["backoff"], 0)

    def test_invalid_report_fails_closed(self):
        for bad in [None, "text", {"candidates": "c1"}, []]:
            d = decide.decide(ctx(), bad)
            self.assertEqual(d["decision"], "WAIT")
            self.assertEqual(d["confidence"], 1.0)

    def test_empty_report_waits(self):
        self.assertEqual(decide.decide(ctx(), report())["decision"], "WAIT")

    def test_only_first_three_candidates_considered(self):
        many = [cand(f"c{i}", "idea", t, f"Idea about {t} with distinct wording number {i}.", ["i1"]) for i, t in
                enumerate(["alpha", "bravo", "charlie", "delta"], 1)]
        self.assertEqual(len(decide.decide(ctx(), report(*many))["candidates"]), 3)


# ---------------------------------------------------------------- final decision contract


class DecisionValidatorTests(unittest.TestCase):
    def setUp(self):
        self.spoken = decide.decide(ctx(), report(cand()))

    def assertInvalid(self, decision, fragment):
        result = validate.validate(decision, ctx())
        self.assertFalse(result["valid"], decision)
        self.assertTrue(any(fragment in e for e in result["errors"]), result["errors"])

    def test_good_decisions_pass(self):
        self.assertTrue(validate.validate(self.spoken, ctx())["valid"])
        self.assertTrue(validate.validate(decide.decide(ctx(), report()), ctx())["valid"])

    def test_contract_errors(self):
        self.assertInvalid({**self.spoken, "decision": "MAYBE"}, "SPEAK")
        self.assertInvalid({**self.spoken, "message": "Something else entirely?"}, "selected candidate's message")
        self.assertInvalid({**self.spoken, "selected": "c9"}, "'selected' must name one of the candidates")
        self.assertInvalid({**self.spoken, "confidence": 0.3}, "below the mode's bar")
        self.assertInvalid({**self.spoken, "evidence": ["m99"]}, "not found in context")
        wait = decide.decide(ctx(), report())
        self.assertInvalid({**wait, "message": "hi"}, "null")
        self.assertInvalid({k: v for k, v in wait.items() if k != "candidates"}, "missing field 'candidates'")


# ---------------------------------------------------------------- grader: generation and decision graded separately


class GraderNegativeTests(unittest.TestCase):
    """The harness must reject plausible-looking but wrong answers, at the right stage."""

    @classmethod
    def setUpClass(cls):
        cls.scenarios = {s["id"]: s for s in run_tests.load_scenarios([])}

    def gen_fails(self, sid, rep):
        return run_tests.grade_generation(self.scenarios[sid], rep)[0]

    def dec_fails(self, sid, rep):
        sc = self.scenarios[sid]
        d = decide.decide(sc["input"], rep)
        return run_tests.grade_decision(sc, d, gate.evaluate(sc["input"]), check_policy=True)[0]

    def test_assumed_outcome_is_caught_in_both_stages(self):
        rep = report(cand(topic="Lindqvist", message="Congrats on the Lindqvist interview, knew you'd nail it.", evidence=["m1"]))
        self.assertTrue(any("congrat" in f for f in self.gen_fails("01-speak-interview-followup", rep)))
        self.assertTrue(any("congrat" in f for f in self.dec_fails("01-speak-interview-followup", rep)))

    def test_invented_detail_is_caught(self):
        rep = report(cand(topic="sister", message="How was your sister's wedding yesterday?", evidence=["m1"]))
        self.assertTrue(any("wedding" in f for f in self.gen_fails("23-either-vague-sister", rep)))

    def test_missing_connection_is_a_generation_failure(self):
        rep = report(cand(topic="weekend bake", kind="casual", message="Bread on the menu this weekend?", evidence=["k1"]))
        fails = self.gen_fails("24-speak-interest-connection", rep)
        self.assertTrue(any("kind ['connection']" in f for f in fails))
        self.assertTrue(any("at least 2" in f for f in fails))

    def test_repeated_topic_is_caught_in_generation_and_never_sent(self):
        rep = report(cand(topic="guitar practice", message="Any progress on the guitar this week?", evidence=["m1"]))
        self.assertTrue(self.gen_fails("27-either-no-topic-repeat", rep))
        self.assertEqual(decide.decide(self.scenarios["27-either-no-topic-repeat"]["input"], rep)["decision"], "WAIT")

    def test_hobby_chatter_during_grief_is_caught(self):
        rep = report(cand(topic="spring ride", kind="casual", message="Fancy planning a route for the 100 km ride?", evidence=["k1"],
                          scores=(4, 4, 4, 4, 4, 1)))
        self.assertTrue(any("km" in f or "ride" in f for f in self.gen_fails("29-wait-grief-no-hobby-chat", rep)))
        self.assertTrue(any("ride" in f for f in self.dec_fails("29-wait-grief-no-hobby-chat", rep)))

    def test_missed_boundary_is_caught(self):
        rep = report(cand(topic="root canal", message="How did the root canal go?", evidence=["m1"]))
        self.assertTrue(any("boundary" in f for f in self.gen_fails("18-wait-asked-for-space", rep)))
        self.assertTrue(any("expected WAIT" in f for f in self.dec_fails("18-wait-asked-for-space", rep)))

    def test_wrong_language_is_caught(self):
        rep = report(cand(topic="Umzug", message="How did the move go yesterday?", evidence=["m1"]))
        self.assertTrue(self.gen_fails("05-speak-german-move", rep))

    def test_waiting_when_reminder_was_requested_is_caught(self):
        self.assertTrue(any("expected SPEAK" in f for f in self.dec_fails("02-speak-requested-reminder", report())))

    def test_decision_not_following_policy_is_caught(self):
        sc = self.scenarios["16-either-closed-thread"]
        d = decide.decide(sc["input"], json.loads((run_tests.REFERENCE / f"{sc['id']}.json").read_text()))
        self.assertEqual(d["decision"], "WAIT")
        best = d["candidates"][0]
        forged = {**d, "decision": "SPEAK", "selected": best["id"], "message": best["message"],
                  "evidence": best["evidence"], "confidence": 0.9}
        fails = run_tests.grade_decision(sc, forged, gate.evaluate(sc["input"]), check_policy=True)[0]
        self.assertTrue(any("does not follow the policy" in f for f in fails))


# ---------------------------------------------------------------- multi-day behaviour


class SimulationTests(unittest.TestCase):
    def test_all_simulations_pass(self):
        for sim in simulate.load_simulations([]):
            _, failures = simulate.evaluate(sim)
            self.assertEqual(failures, [], sim["id"])

    def test_invariants_catch_violations(self):
        sim = simulate.load_simulations(["sim-02"])[0]
        res = simulate.run(sim, "balanced")
        first = res["sends"][0]
        bad = dict(first, at=first["at"] + timedelta(minutes=10))  # same topic and message, 10 minutes later
        res["sends"].insert(1, bad)
        fails = simulate.invariant_failures(res)
        self.assertTrue(any("min 90" in f for f in fails), fails)
        self.assertTrue(any("repeats topic" in f for f in fails), fails)
        self.assertTrue(any("identical message" in f for f in fails), fails)

    def test_modes_scale_initiative(self):
        results, _ = simulate.evaluate(simulate.load_simulations(["sim-02"])[0])
        counts = {m: len(r["sends"]) for m, r in results.items()}
        self.assertLess(counts["quiet"], counts["balanced"])
        self.assertLess(counts["balanced"], counts["lively"])


if __name__ == "__main__":
    unittest.main()
