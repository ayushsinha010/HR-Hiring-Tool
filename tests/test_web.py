import json
import re
import unittest
from unittest import mock

from werkzeug.security import generate_password_hash

from . import _testenv as env

import app as hr_app
import db
import scorer


def breakdown():
    return [
        {"name": "Core skills match", "weight": 50, "score": 9, "evidence": "6 yrs Django"},
        {"name": "Tools and tech stack", "weight": 30, "score": 7, "evidence": "Postgres, Docker"},
        {"name": "Communication", "weight": 20, "score": 6, "evidence": "Clear write-up"},
    ]


class WebBase(unittest.TestCase):
    def setUp(self):
        env.fresh_db()
        self.client = hr_app.app.test_client()
        self.login(self.client, "admin", env.ADMIN_PASSWORD)

    @staticmethod
    def login(client, username, password):
        r = client.post("/login", data={"username": username, "password": password})
        assert r.status_code == 302, r.status_code

    def seed(self):
        self.p1 = env.add_position("Alpha Role", created_at="2026-09-01 12:00:00")
        self.p2 = env.add_position("Beta Role", created_at="2026-09-20 12:00:00")
        self.a = env.add_candidate(self.p1, "Ann Alpha", created_at="2026-09-03 12:00:00", score=8.0, breakdown=scorer.normalise_breakdown(breakdown()))
        self.b = env.add_candidate(self.p1, "Bo Alpha", created_at="2026-09-04 12:00:00", score=5.5)       # older score: no breakdown
        self.c = env.add_candidate(self.p1, "Cy Alpha", created_at="2026-09-05 12:00:00")                   # unscored
        self.d = env.add_candidate(self.p2, "Di Beta", created_at="2026-09-21 12:00:00")


class DashboardTests(WebBase):
    def test_timeline_is_the_last_section_and_tasks_cards_are_gone(self):
        self.seed()
        html = self.client.get("/").get_data(as_text=True)
        self.assertEqual(html.count('id="timeline-section"'), 1)
        self.assertLess(html.index('id="funnel-table"'), html.index('id="timeline-section"'))
        self.assertLess(html.index('id="position-cards-grid"'), html.index('id="timeline-section"'))
        end = html.index("</section>", html.index('id="timeline-section"'))
        rest = html[end:html.index('id="calendar-modal"')]
        for token in ("chart-card", "kpi-card", "position-card", "tracker-table", "<table"):
            self.assertNotIn(token, rest)                                  # nothing from the dashboard comes after it
        self.assertIn("Hiring Timeline and Buffer Time", html)
        for gone in ("Open Tasks", "Completed Tasks", "New task", "task-add-form", "add_task_route", "/tasks/"):
            self.assertNotIn(gone, html)
        # the remaining dashboard cards are still there (Time to Fill needs a filled position, so it is not asserted)
        for kept in ("Positions by Status", "Activity Heatmap", 'id="funnel-table"', 'id="position-cards-grid"'):
            self.assertIn(kept, html)
        self.assertIn("Age of Open Positions", html)
        self.assertIn('id="timeline-candidates-table"', html)
        self.assertIn('id="timeline-positions-table"', html)

    def test_position_screen_has_the_scoped_section(self):
        self.seed()
        html = self.client.get(f"/?position={self.p1}").get_data(as_text=True)
        self.assertEqual(html.count('id="timeline-section"'), 1)
        self.assertIn("Position summary", html)
        self.assertIn("Ann Alpha", html[html.index('id="timeline-section"'):])
        section = html[html.index('id="timeline-section"'):]
        self.assertNotIn("Di Beta", section)                               # the other position's candidate is not in it
        self.assertNotIn("Beta Role", section)
        table = section[section.index('id="timeline-candidates-table"'):]
        head = table[:table.index("</thead>")]
        self.assertNotIn("Position</th>", head)                            # redundant column hidden on a position's screen
        self.assertIn("Candidate</th>", head)
        self.assertIn("avg-days-stage-chart", section)
        self.assertIn("timeline.js", html)

    def test_overall_table_keeps_the_position_column(self):
        self.seed()
        html = self.client.get("/").get_data(as_text=True)
        table = html[html.index('id="timeline-candidates-table"'):]
        self.assertIn("<th>Position</th>", table[:table.index("</thead>")])

    def test_section_also_shows_when_a_position_has_no_cvs_yet(self):
        env.add_position("Brand New", created_at="2026-10-01 12:00:00")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Hiring Timeline and Buffer Time", html)
        self.assertIn("Brand New", html)

    def test_tasks_backend_is_untouched(self):
        env.fresh_db()
        urls = {r.rule for r in hr_app.app.url_map.iter_rules()}
        self.assertTrue({"/tasks/add", "/tasks/<int:task_id>/complete", "/tasks/<int:task_id>/delete"} <= urls)
        r = self.client.post("/tasks/add", data={"title": "Keep me"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(db.list_open_tasks(1)[0]["title"], "Keep me")      # table and rows still work
        self.assertNotIn("Keep me", self.client.get("/").get_data(as_text=True))

    def test_non_admin_dashboard_is_scoped(self):
        self.seed()
        uid = db.create_user("viewer", "Viewer", generate_password_hash("pw-viewer-1"))
        db.assign_user_to_position(self.p2, uid, "Reviewer", 1)
        other = hr_app.app.test_client()
        self.login(other, "viewer", "pw-viewer-1")
        html = other.get("/").get_data(as_text=True)
        section = html[html.index('id="timeline-section"'):]
        self.assertIn("Di Beta", section)
        self.assertNotIn("Ann Alpha", section)
        self.assertEqual(other.get(f"/?position={self.p1}").status_code, 403)


class SettingsTests(WebBase):
    def test_targets_card_shows_defaults_and_saves(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Hiring Timeline Targets", html)
        self.assertRegex(html, r'name="target_stage_age_days"[^>]*value="5"')
        r = self.client.post("/settings", data={"form_name": "timeline_targets", "target_stage_age_days": "9",
                                               "target_feedback_pending_days": "4", "target_decision_pending_days": "6",
                                               "target_position_open_days": "45"})
        self.assertIn("Hiring timeline targets saved.", r.get_data(as_text=True))
        self.assertEqual(db.get_timeline_targets(), {"stage_age_days": 9, "feedback_pending_days": 4,
                                                     "decision_pending_days": 6, "position_open_days": 45})

    def test_bad_target_is_rejected_with_a_message(self):
        r = self.client.post("/settings", data={"form_name": "timeline_targets", "target_stage_age_days": "x",
                                               "target_feedback_pending_days": "2", "target_decision_pending_days": "3",
                                               "target_position_open_days": "30"})
        self.assertIn("must be a whole number of days", r.get_data(as_text=True))
        self.assertEqual(db.get_timeline_targets()["stage_age_days"], 5)


class CandidatePageTests(WebBase):
    def test_schedule_then_feedback_through_the_forms(self):
        self.seed()
        r = self.client.post(f"/candidates/{self.a}/interviews/schedule", data={
            "round_key": "l1", "interview_date": "2026-10-05", "interview_time": "10:30", "interviewers": "Jordan, Priya",
            "mode": "Teams", "link": "https://teams.example/x", "record_flag": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("#interviews", r.headers["Location"])
        page = self.client.get(f"/positions/{self.p1}/resumes?candidate={self.a}").get_data(as_text=True)
        self.assertIn("L1 Interview", page)
        self.assertIn("Jordan, Priya", page)
        self.assertIn("Yes, to be recorded", page)
        self.assertIn("05 Oct 2026, 10:30", page)
        self.assertEqual(db.get_candidate(self.a)["l1_scheduled"], 1)         # stage reflected
        iid = db.list_candidate_interviews(self.a)[0]["id"]
        for who, rec in (("Jordan", "Pass"), ("Priya", "Hold")):
            r = self.client.post(f"/candidates/{self.a}/interviews/{iid}/feedback",
                                 data={"panelist": who, "recommendation": rec, "rating": "4", "comments": f"{who} says ok"})
            self.assertEqual(r.status_code, 302)
        page = self.client.get(f"/positions/{self.p1}/resumes?candidate={self.a}").get_data(as_text=True)
        self.assertIn("Jordan says ok", page)
        self.assertIn("Priya says ok", page)
        self.assertEqual(len(db.list_candidate_interviews(self.a)[0]["feedback"]), 2)

    def test_errors_are_shown_not_swallowed(self):
        self.seed()
        r = self.client.post(f"/candidates/{self.a}/interviews/schedule", data={"round_key": "l1", "interview_date": "nonsense", "interview_time": "10:00"})
        self.assertIn("timeline_error=", r.headers["Location"])
        r = self.client.post(f"/candidates/{self.a}/interviews/schedule", data={"round_key": "zz", "interview_date": "2026-10-05", "interview_time": "10:00"})
        self.assertIn("timeline_error=", r.headers["Location"])
        iid = db.schedule_interview(self.a, "l1", scheduled_for="2026-10-05 10:00")
        r = self.client.post(f"/candidates/{self.a}/interviews/{iid}/feedback", data={"panelist": "G", "recommendation": "Maybe"})
        self.assertIn("timeline_error=", r.headers["Location"])
        self.assertEqual(self.client.post(f"/candidates/{self.b}/interviews/{iid}/feedback", data={"panelist": "G", "recommendation": "Pass"}).status_code, 404)

    def test_send_link_flows_also_record_the_interview(self):
        self.seed()
        r = self.client.post(f"/candidates/{self.a}/send-teams-link", json={
            "round": "l2", "date": "20-10-2026", "time": "3:00 PM IST", "teams_link": "https://teams.example/y", "note": "", "record": True})
        self.assertEqual(r.status_code, 200)                                  # (no mailbox configured, so the message says so)
        iv = db.list_candidate_interviews(self.a)[0]
        self.assertEqual((iv["round_key"], iv["scheduled_for"], iv["mode"], iv["record_flag"]), ("l2", "2026-10-20 15:00", "Teams", 1))
        self.assertEqual(db.get_candidate(self.a)["l2_scheduled"], 1)
        # dynamic-rounds position
        pid = env.add_position("Dyn", dynamic=True)
        cid = env.add_candidate(pid, "Dana")
        rid = db.get_interview_rounds(pid)[0]["id"]
        r = self.client.post(f"/candidates/{cid}/rounds/{rid}/send-link", json={"date": "2026-11-02", "time": "11:00", "teams_link": "https://t/z", "note": ""})
        self.assertEqual(r.status_code, 200)
        iv = db.list_candidate_interviews(cid)[0]
        self.assertEqual((iv["round_key"], iv["scheduled_for"], iv["record_flag"]), (f"r{rid}", "2026-11-02 11:00", 0))

    def test_candidate_page_shows_timeline_and_estimate_marks(self):
        self.seed()
        page = self.client.get(f"/positions/{self.p1}/resumes?candidate={self.a}").get_data(as_text=True)
        for text in ("Timeline and Buffer Time", "CV fed in on", "Days in stage", "Interviews and Panel Feedback", "Schedule an interview", "Stage history"):
            self.assertIn(text, page)


class ScoreBreakdownEndpointTests(WebBase):
    def test_with_breakdown(self):
        self.seed()
        r = self.client.get(f"/candidates/{self.a}/score-breakdown.json")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["has_breakdown"] and d["scored"])
        self.assertEqual((d["score"], d["bucket"]), (8.0, "green"))
        self.assertEqual([p["name"] for p in d["parameters"]], ["Core skills match", "Tools and tech stack", "Communication"])
        self.assertEqual(set(d["parameters"][0]), {"name", "weight", "score", "evidence"})
        self.assertEqual(sum(p["weight"] for p in d["parameters"]), 100)
        for key in ("summary", "positives", "negatives", "scored_at", "candidate_name"):
            self.assertIn(key, d)
        self.assertEqual(d["positives"], ["plus"])

    def test_older_score_without_breakdown_still_returns_the_rest(self):
        self.seed()
        d = self.client.get(f"/candidates/{self.b}/score-breakdown.json").get_json()
        self.assertFalse(d["has_breakdown"])
        self.assertTrue(d["scored"])
        self.assertEqual((d["score"], d["bucket"], d["parameters"]), (5.5, "yellow", []))
        self.assertEqual((d["summary"], d["positives"], d["negatives"]), ("summary Bo Alpha", ["plus"], ["minus"]))

    def test_unscored_and_unknown(self):
        self.seed()
        d = self.client.get(f"/candidates/{self.c}/score-breakdown.json").get_json()
        self.assertEqual((d["scored"], d["has_breakdown"], d["score"], d["bucket"]), (False, False, None, "unscored"))
        self.assertEqual(self.client.get("/candidates/999999/score-breakdown.json").status_code, 404)

    def test_corrupt_stored_json_degrades_to_no_breakdown(self):
        self.seed()
        conn = db.get_conn()
        conn.execute("UPDATE candidates SET score_breakdown_json = '{not json' WHERE id = ?", (self.a,))
        conn.commit()
        conn.close()
        d = self.client.get(f"/candidates/{self.a}/score-breakdown.json").get_json()
        self.assertFalse(d["has_breakdown"])
        self.assertEqual(d["score"], 8.0)

    def test_login_required(self):
        self.seed()
        anon = hr_app.app.test_client()
        r = anon.get(f"/candidates/{self.a}/score-breakdown.json")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_per_hiring_access_control(self):
        self.seed()
        uid = db.create_user("rev", "Reviewer", generate_password_hash("pw-rev-1"))
        rev = hr_app.app.test_client()
        self.login(rev, "rev", "pw-rev-1")
        self.assertEqual(rev.get(f"/candidates/{self.a}/score-breakdown.json").status_code, 403)    # not assigned
        db.assign_user_to_position(self.p1, uid, "Tech", 1)
        self.assertEqual(rev.get(f"/candidates/{self.a}/score-breakdown.json").status_code, 200)
        self.assertEqual(rev.get(f"/candidates/{self.d}/score-breakdown.json").status_code, 403)    # a different position
        self.assertEqual(rev.post(f"/candidates/{self.d}/interviews/schedule", data={"round_key": "l1"}).status_code, 403)


class ResumesPageTests(WebBase):
    def test_default_order_labels_and_clickable_scores(self):
        self.seed()
        html = self.client.get(f"/positions/{self.p1}/resumes").get_data(as_text=True)
        names = re.findall(r'<span class="candidate-name"[^>]*>([^<]+)</span>', html)
        self.assertEqual(names, ["Ann Alpha", "Bo Alpha", "Cy Alpha"])          # 8.0, 5.5, unscored last
        self.assertEqual(len(re.findall(r'data-score-breakdown="\d+"', html)), 2)   # scored ones only
        self.assertIn('title="Click for score breakdown"', html)
        self.assertIn("Not scored yet", html)
        self.assertIn("score_breakdown.js", html)

    def test_scoring_without_an_api_key_degrades_gracefully(self):
        self.seed()
        self.assertEqual(db.get_llm_settings()["llm_api_key"], "")            # owner has not entered one (and none is needed here)
        r = self.client.post(f"/positions/{self.p1}/score-selected", json={"candidate_ids": [self.c]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("No LLM API key set", r.get_json()["error"])
        self.assertIn("Settings", r.get_json()["error"])
        html = self.client.get(f"/positions/{self.p1}/resumes").get_data(as_text=True)    # listing still fine
        self.assertIn("Not scored yet", html)
        self.assertIsNone(db.get_candidate(self.c)["score"])

    def test_candidates_json_follows_the_default_order(self):
        self.seed()
        scores = [c["score"] for c in self.client.get(f"/positions/{self.p1}/candidates.json").get_json()]
        self.assertEqual(scores, [8.0, 5.5, None])


if __name__ == "__main__":
    unittest.main()
