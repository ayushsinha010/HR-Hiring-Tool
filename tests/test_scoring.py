import json
import unittest
from unittest import mock

from . import _testenv as env

import db
import scorer


def good_breakdown():
    return [
        {"name": "Core skills match", "weight": 40, "score": 9, "evidence": "6 yrs Django"},
        {"name": "Relevant experience", "weight": 30, "score": 8, "evidence": "Led API projects"},
        {"name": "Tools and tech stack", "weight": 20, "score": 7, "evidence": "Postgres, Docker"},
        {"name": "Communication", "weight": 10, "score": 6, "evidence": "Clear write-up"},
    ]


class BreakdownValidationTests(unittest.TestCase):
    def test_clean_input_passes_through(self):
        out = scorer.normalise_breakdown(good_breakdown())
        self.assertEqual([r["weight"] for r in out], [40, 30, 20, 10])
        self.assertEqual([r["score"] for r in out], [9.0, 8.0, 7.0, 6.0])
        self.assertEqual(out[0]["evidence"], "6 yrs Django")

    def test_weights_are_rescaled_to_exactly_100(self):
        raw = [{"name": n, "weight": w, "score": 5, "evidence": ""} for n, w in (("a", 1), ("b", 1), ("c", 1))]
        out = scorer.normalise_breakdown(raw)
        self.assertEqual(sum(r["weight"] for r in out), 100)
        self.assertEqual(sorted(r["weight"] for r in out), [33, 33, 34])
        raw = [{"name": n, "weight": w, "score": 5} for n, w in (("a", 50), ("b", 50), ("c", 50), ("d", 50), ("e", 50))]   # sums to 250
        out = scorer.normalise_breakdown(raw)
        self.assertEqual([r["weight"] for r in out], [20] * 5)

    def test_scores_are_clamped_and_rounded(self):
        raw = good_breakdown()
        raw[0]["score"] = 14
        raw[1]["score"] = -3
        raw[2]["score"] = "7.26"
        out = scorer.normalise_breakdown(raw)
        self.assertEqual([r["score"] for r in out], [10.0, 0.0, 7.3, 6.0])

    def test_empty_duplicate_and_unscored_rows_are_dropped(self):
        raw = good_breakdown() + [
            {"name": "", "weight": 10, "score": 5, "evidence": "x"},                  # no name
            {"name": "Core skills match", "weight": 10, "score": 5, "evidence": "x"},  # duplicate
            {"name": "No score", "weight": 10, "evidence": "x"},                        # no score
            {"name": "Bad score", "weight": 10, "score": "n/a"},
            "not a dict", None,
        ]
        out = scorer.normalise_breakdown(raw)
        self.assertEqual([r["name"] for r in out], ["Core skills match", "Relevant experience", "Tools and tech stack", "Communication"])
        self.assertEqual(sum(r["weight"] for r in out), 100)

    def test_missing_or_unusable_weights_fall_back_to_an_even_split(self):
        raw = [{"name": n, "score": s} for n, s in (("a", 4), ("b", 6), ("c", 8), ("d", 10))]
        out = scorer.normalise_breakdown(raw)
        self.assertEqual([r["weight"] for r in out], [25, 25, 25, 25])

    def test_zero_percent_rows_are_dropped_and_the_rest_rescaled(self):
        raw = good_breakdown() + [{"name": "Tiny", "weight": 0.2, "score": 10, "evidence": ""}]
        out = scorer.normalise_breakdown(raw)
        self.assertNotIn("Tiny", [r["name"] for r in out])
        self.assertEqual(sum(r["weight"] for r in out), 100)

    def test_too_few_rows_or_wrong_type_is_invalid(self):
        self.assertIsNone(scorer.normalise_breakdown(good_breakdown()[:2]))
        self.assertIsNone(scorer.normalise_breakdown(None))
        self.assertIsNone(scorer.normalise_breakdown("garbage"))
        self.assertIsNone(scorer.normalise_breakdown({"name": "x"}))
        self.assertEqual(len(scorer.normalise_breakdown(json.dumps(good_breakdown()))), 4)   # a JSON string is accepted

    def test_at_most_eight_parameters(self):
        raw = [{"name": f"p{i}", "weight": 10, "score": 5} for i in range(12)]
        self.assertEqual(len(scorer.normalise_breakdown(raw)), 8)

    def test_weighted_score_is_the_weighted_average_rounded_to_one_decimal(self):
        out = scorer.normalise_breakdown(good_breakdown())
        # 0.4*9 + 0.3*8 + 0.2*7 + 0.1*6 = 8.0
        self.assertEqual(scorer.weighted_score(out), 8.0)
        raw = [{"name": "a", "weight": 1, "score": 7.5}, {"name": "b", "weight": 1, "score": 7}, {"name": "c", "weight": 1, "score": 7}]
        self.assertEqual(scorer.weighted_score(scorer.normalise_breakdown(raw)), 7.2)         # 7.1666 -> 7.2
        raw = [{"name": "a", "weight": 50, "score": 7.5}, {"name": "b", "weight": 25, "score": 7.5}, {"name": "c", "weight": 25, "score": 7}]
        self.assertEqual(scorer.weighted_score(scorer.normalise_breakdown(raw)), 7.4)         # 7.375 -> 7.4 (half up)


class ScoreCandidateTests(unittest.TestCase):
    """The model call is mocked - the owner has not entered an API key, and none is read or written here."""

    def model_output(self, **over):
        data = {"score": 3.0, "fit_summary": "Good fit.", "positives": ["Django"], "negatives": ["No cloud"], "breakdown": good_breakdown()}
        data.update(over)
        return data

    def test_overall_score_is_computed_in_code_from_the_breakdown(self):
        with mock.patch.object(scorer, "_score_via_openrouter", return_value=self.model_output()) as m:
            result = scorer.score_candidate("jd", "resume")
        self.assertEqual(result["score"], 8.0)                  # not the model's own 3.0
        self.assertEqual(len(result["breakdown"]), 4)
        self.assertEqual(m.call_count, 1)

    def test_both_providers_use_the_same_schema_and_result(self):
        env.fresh_db()
        db.update_llm_settings(provider="anthropic")
        with mock.patch.object(scorer, "_score_via_anthropic", return_value=self.model_output()) as a, \
                mock.patch.object(scorer, "_score_via_openrouter") as o:
            result = scorer.score_candidate("jd", "resume")
        self.assertEqual((a.call_count, o.call_count), (1, 0))
        self.assertEqual(result["score"], 8.0)
        db.update_llm_settings(provider="openrouter")
        props = scorer.ANTHROPIC_TOOL["input_schema"]["properties"]
        self.assertIn("breakdown", props)
        self.assertEqual(set(props["breakdown"]["items"]["required"]), {"name", "weight", "score", "evidence"})
        self.assertEqual(scorer.OPENAI_STYLE_TOOL["function"]["parameters"], scorer.ANTHROPIC_TOOL["input_schema"])

    def test_missing_or_invalid_breakdown_falls_back_to_the_models_overall_score(self):
        for bad in (None, [], [{"name": "only one", "weight": 100, "score": 5}], "nope"):
            out = self.model_output(score=6.34)
            out["breakdown"] = bad
            with mock.patch.object(scorer, "_score_via_openrouter", return_value=out):
                result = scorer.score_candidate("jd", "resume")
            self.assertEqual(result["score"], 6.3)
            self.assertIsNone(result["breakdown"])
        out = self.model_output(score=42)
        del out["breakdown"]
        with mock.patch.object(scorer, "_score_via_openrouter", return_value=out):
            self.assertEqual(scorer.score_candidate("jd", "resume")["score"], 10.0)   # clamped to 0-10

    def test_bucket_logic_is_unchanged(self):
        self.assertEqual([db.bucket_for_score(s) for s in (None, 6.0, 6.1, 4.0, 3.9)], ["unscored", "yellow", "green", "yellow", "red"])

    def test_rescoring_selected_candidates_creates_the_breakdown_and_replaces_an_old_score(self):
        env.fresh_db()
        pid = env.add_position("Role")
        old = env.add_candidate(pid, "Old Scored", score=5.5)               # scored before breakdowns existed
        self.assertIsNone(db.get_candidate(old)["score_breakdown_json"])
        with mock.patch.object(scorer, "_score_via_openrouter", return_value=self.model_output()):
            scorer.score_selected_candidates(pid, [old])                    # the "Score selected" action
        c = db.get_candidate(old)
        self.assertEqual(c["score"], 8.0)
        self.assertEqual(c["bucket"], "green")
        self.assertEqual(json.loads(c["score_breakdown_json"])[0]["name"], "Core skills match")
        # re-scoring again with an unusable breakdown clears the stale one (it would no longer add up)
        with mock.patch.object(scorer, "_score_via_openrouter", return_value=self.model_output(breakdown=None, score=4.4)):
            scorer.score_selected_candidates(pid, [old])
        c = db.get_candidate(old)
        self.assertEqual((c["score"], c["bucket"]), (4.4, "yellow"))
        self.assertIsNone(c["score_breakdown_json"])

    def test_save_and_load_breakdown_roundtrip(self):
        env.fresh_db()
        pid = env.add_position("Role")
        cid = env.add_candidate(pid, "Zed")
        db.save_score(cid, 8.0, "s", ["p"], ["n"], breakdown=scorer.normalise_breakdown(good_breakdown()))
        c = db.get_candidate(cid)
        self.assertEqual(json.loads(c["score_breakdown_json"]), scorer.normalise_breakdown(good_breakdown()))
        db.save_score(cid, 7.0, "s", ["p"], ["n"])                           # no breakdown argument: still works
        self.assertIsNone(db.get_candidate(cid)["score_breakdown_json"])


class DefaultResumeOrderTests(unittest.TestCase):
    def setUp(self):
        env.fresh_db()
        self.pid = env.add_position("Order Role")

    def names(self, **kw):
        return [c["candidate_name"] for c in db.list_candidates(self.pid, **kw)]

    def test_highest_score_first_unscored_last(self):
        env.add_candidate(self.pid, "Low", score=3.2)
        env.add_candidate(self.pid, "NoScore1")
        env.add_candidate(self.pid, "High", score=9.1)
        env.add_candidate(self.pid, "Mid", score=6.5)
        self.assertEqual(self.names(), ["High", "Mid", "Low", "NoScore1"])

    def test_numeric_not_text_ordering(self):
        env.add_candidate(self.pid, "ten", score=10.0)
        env.add_candidate(self.pid, "nine", score=9.0)
        env.add_candidate(self.pid, "two", score=2.0)
        self.assertEqual(self.names(), ["ten", "nine", "two"])

    def test_ties_by_most_recent_cv_then_name(self):
        env.add_candidate(self.pid, "Bravo", created_at="2026-09-10 12:00:00", score=7.0)
        env.add_candidate(self.pid, "alpha", created_at="2026-09-10 12:00:00", score=7.0)
        env.add_candidate(self.pid, "Newest", created_at="2026-09-20 12:00:00", score=7.0)
        env.add_candidate(self.pid, "Oldest", created_at="2026-09-01 12:00:00", score=7.0)
        self.assertEqual(self.names(), ["Newest", "alpha", "Bravo", "Oldest"])          # name tie-break is case-insensitive

    def test_unscored_ties_also_newest_first_and_search_keeps_the_order(self):
        env.add_candidate(self.pid, "Older", created_at="2026-09-01 12:00:00")
        env.add_candidate(self.pid, "Newer", created_at="2026-09-09 12:00:00")
        env.add_candidate(self.pid, "Scored", score=1.0)
        self.assertEqual(self.names(), ["Scored", "Newer", "Older"])
        self.assertEqual(self.names(query="er"), ["Newer", "Older"])


if __name__ == "__main__":
    unittest.main()
