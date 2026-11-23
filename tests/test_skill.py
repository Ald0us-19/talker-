"""Unit tests for the deterministic parts: gate rules, validator, and grader.

Run:  python3 -m unittest discover -s tests -v
"""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "proactive-conversation" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import gate  # noqa: E402
import run_tests  # noqa: E402
import validate  # noqa: E402

BASE = {
    "now": "2026-11-24T18:40:00+01:00",
    "timezone": "Europe/Berlin",
    "preferences": {"proactive_enabled": True, "quiet_hours": {"start": "22:00", "end": "08:00"}},
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


class GateTests(unittest.TestCase):
    def test_base_passes(self):
        result = gate.evaluate(ctx())
        self.assertEqual(result["gate"], "PASS", result)
        self.assertIn("m1", result["signals"]["times"])

    def test_overnight_quiet_hours_boundaries(self):
        q = {"quiet_hours": {"start": "22:00", "end": "07:30"}}
        cases = {"21:59": "PASS", "22:00": "BLOCK", "02:00": "BLOCK", "07:29": "BLOCK", "07:30": "PASS"}
        for hhmm, expected in cases.items():
            day = "2026-11-25" if hhmm < "12:00" else "2026-11-24"
            result = gate.evaluate(ctx(now=f"{day}T{hhmm}:00+01:00", prefs=q))
            self.assertEqual(result["gate"], expected, hhmm)

    def test_daytime_quiet_window(self):
        q = {"quiet_hours": {"start": "13:00", "end": "14:00"}}
        self.assertEqual(gate.evaluate(ctx(now="2026-11-24T13:30:00+01:00", prefs=q))["blocked_by"], ["quiet_hours"])

    def test_quiet_hours_null_means_none_and_default_applies_when_absent(self):
        late = "2026-11-24T23:30:00+01:00"
        self.assertEqual(gate.evaluate(ctx(now=late, prefs={"quiet_hours": None}))["gate"], "PASS")
        c = ctx(now=late)
        del c["preferences"]["quiet_hours"]
        result = gate.evaluate(c)
        self.assertEqual(result["blocked_by"], ["quiet_hours"])
        self.assertTrue(any("default" in w for w in result["warnings"]))

    def test_timezone_conversion_uses_local_clock(self):
        # 21:30 UTC is 22:30 in Berlin -> inside quiet hours.
        result = gate.evaluate(ctx(now="2026-11-24T21:30:00Z"))
        self.assertEqual(result["blocked_by"], ["quiet_hours"])

    def test_invalid_inputs_fail_closed(self):
        for bad in [ctx(now="yesterday"), ctx(timezone="Mars/Olympus"), "not an object", ctx(now=None)]:
            result = gate.evaluate(bad)
            self.assertEqual(result["gate"], "BLOCK")
            self.assertEqual(result["blocked_by"][0], "invalid_input")
            self.assertEqual(result["decision"]["decision"], "WAIT")

    def test_opt_in_required(self):
        c = ctx()
        del c["preferences"]["proactive_enabled"]
        self.assertEqual(gate.evaluate(c)["blocked_by"][0], "not_opted_in")
        self.assertEqual(gate.evaluate(ctx(prefs={"proactive_enabled": "yes"}))["blocked_by"][0], "disabled")

    def test_unanswered_streak_counts_only_after_last_user_message(self):
        history = BASE["history"] + [
            proactive("p1", "2026-11-23T10:00:00+01:00"),
            {"id": "u1", "role": "user", "timestamp": "2026-11-23T11:00:00+01:00", "text": "ok"},
            proactive("p2", "2026-11-23T18:00:00+01:00"),
        ]
        result = gate.evaluate(ctx(history=history))
        self.assertEqual(result["signals"]["unanswered_proactive_streak"], 1)
        self.assertEqual(result["gate"], "PASS")
        history.append(proactive("p3", "2026-11-24T09:00:00+01:00"))
        self.assertIn("unanswered_limit", gate.evaluate(ctx(history=history))["blocked_by"])

    def test_dismissal_cooldown_expires(self):
        # Dismissed 10h40m before 'now'.
        history = BASE["history"] + [proactive("p1", "2026-11-24T08:00:00+01:00", feedback="dismissed")]
        self.assertIn("recent_dismissal", gate.evaluate(ctx(history=history))["blocked_by"])
        relaxed = gate.evaluate(ctx(history=history, prefs={"dismissal_cooldown_hours": 6}))
        self.assertNotIn("recent_dismissal", relaxed["blocked_by"])

    def test_all_violations_reported_in_priority_order(self):
        result = gate.evaluate(ctx(now="2026-11-24T23:30:00+01:00", prefs={"proactive_enabled": False}))
        self.assertEqual(result["blocked_by"], ["disabled", "quiet_hours"])

    def test_bad_timestamps_warn_and_are_ignored(self):
        history = BASE["history"] + [
            {"id": "x", "role": "user", "timestamp": "soon", "text": "?"},
            {"id": "f", "role": "user", "timestamp": "2027-01-01T00:00:00+01:00", "text": "future"},
        ]
        result = gate.evaluate(ctx(history=history))
        self.assertEqual(result["gate"], "PASS")
        self.assertEqual(len(result["warnings"]), 2)

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


GOOD_SPEAK = {
    "decision": "SPEAK",
    "reason": "User promised to report back after the interview.",
    "message": "How did the interview go?",
    "confidence": 0.8,
    "evidence": ["m2"],
}
GOOD_WAIT = {"decision": "WAIT", "reason": "Nothing open.", "message": None, "confidence": 0.9, "evidence": []}


def with_(base, **changes):
    d = dict(base)
    d.update(changes)
    return d


class ValidatorTests(unittest.TestCase):
    def assertInvalid(self, decision, fragment, context=None):
        result = validate.validate(decision, context or BASE)
        self.assertFalse(result["valid"], decision)
        self.assertTrue(any(fragment in e for e in result["errors"]), result["errors"])

    def test_good_decisions_pass(self):
        self.assertTrue(validate.validate(GOOD_SPEAK, BASE)["valid"])
        self.assertTrue(validate.validate(GOOD_WAIT, BASE)["valid"])

    def test_contract_errors(self):
        self.assertInvalid(with_(GOOD_WAIT, decision="MAYBE"), "SPEAK")
        self.assertInvalid(with_(GOOD_WAIT, message="hi"), "null")
        self.assertInvalid(with_(GOOD_WAIT, confidence=1.5), "between 0 and 1")
        self.assertInvalid({k: v for k, v in GOOD_WAIT.items() if k != "reason"}, "missing field 'reason'")
        self.assertInvalid(with_(GOOD_SPEAK, message=""), "non-empty")

    def test_grounding_errors(self):
        self.assertInvalid(with_(GOOD_SPEAK, evidence=[]), "requires at least one evidence")
        self.assertInvalid(with_(GOOD_SPEAK, evidence=["m99"]), "not found in context")

    def test_low_confidence_speak_rejected(self):
        self.assertInvalid(with_(GOOD_SPEAK, confidence=0.55), "below the threshold")

    def test_style_errors(self):
        for msg, fragment in [
            ("Hey there! How did the interview go?", "bare greeting"),
            ("Just checking in about the interview.", "just checking in"),
            ("Hope you're doing well. How was the interview?", "hope you're well"),
            ("You haven't replied about the interview.", "haven't heard back"),
            ("How did the interview go?! So excited!", "more than one '!'"),
            ("x" * 400, "max 320"),
        ]:
            self.assertInvalid(with_(GOOD_SPEAK, message=msg), fragment)

    def test_specific_question_is_not_flagged_as_generic(self):
        msg = "How are you feeling after the interview? Happy to debrief if useful."
        self.assertTrue(validate.validate(with_(GOOD_SPEAK, message=msg), BASE)["valid"])

    def test_emoji_warning_depends_on_user_style(self):
        speak = with_(GOOD_SPEAK, message="How did the interview go? 🙂")
        self.assertTrue(validate.validate(speak, BASE)["warnings"])
        emoji_user = ctx(history=BASE["history"] + [
            {"id": "m3", "role": "user", "timestamp": "2026-11-22T20:17:00+01:00", "text": "wish me luck 🙂"}])
        self.assertFalse(validate.validate(speak, emoji_user)["warnings"])

    def test_extract_json_variants(self):
        raw = json.dumps(GOOD_WAIT)
        for text in [raw, f"```json\n{raw}\n```", f"Here you go:\n{raw}\nDone.", '{"a": "brace } in \\" string", "b": 1}']:
            obj, _ = validate.extract_json(text)
            self.assertIsInstance(obj, dict, text)
        self.assertIsNone(validate.extract_json("no json here")[0])


class GraderNegativeTests(unittest.TestCase):
    """The harness must reject plausible-looking but wrong decisions."""

    @classmethod
    def setUpClass(cls):
        cls.scenarios = {s["id"]: s for s in run_tests.load_scenarios([])}

    def fails(self, scenario_id, decision):
        sc = self.scenarios[scenario_id]
        failures, _ = run_tests.grade(sc, decision, gate.evaluate(sc["input"]))
        return failures

    def test_assumed_outcome_is_caught(self):
        bad = with_(GOOD_SPEAK, message="Congrats on the Lindqvist interview, knew you'd nail it.", evidence=["m1"])
        self.assertTrue(any("congrat" in f for f in self.fails("01-speak-interview-followup", bad)))

    def test_invented_detail_is_caught(self):
        bad = with_(GOOD_SPEAK, message="How was your sister's wedding yesterday?", evidence=["m1"], confidence=0.7)
        self.assertTrue(any("wedding" in f for f in self.fails("23-either-vague-sister", bad)))

    def test_speaking_through_a_hard_rule_is_caught(self):
        bad = with_(GOOD_SPEAK, message="How did the Lindqvist interview go?", evidence=["m1"])
        failures = self.fails("07-gate-quiet-hours", bad)
        self.assertTrue(any("hard rule" in f for f in failures))

    def test_speaking_when_user_asked_for_space_is_caught(self):
        bad = with_(GOOD_SPEAK, message="How did the root canal go?", evidence=["m1"])
        self.assertTrue(any("expected WAIT" in f for f in self.fails("18-wait-asked-for-space", bad)))

    def test_wrong_language_is_caught(self):
        bad = with_(GOOD_SPEAK, message="How did the move go yesterday?", evidence=["m1"])
        self.assertTrue(self.fails("05-speak-german-move", bad))

    def test_waiting_when_reminder_was_requested_is_caught(self):
        self.assertTrue(any("expected SPEAK" in f for f in self.fails("02-speak-requested-reminder", GOOD_WAIT)))


if __name__ == "__main__":
    unittest.main()
