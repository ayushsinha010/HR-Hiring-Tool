import json
import sqlite3
import unittest
from datetime import date, datetime

from . import _testenv as env

import db
import timeline

TODAY = date(2026, 10, 10)
NOW = datetime(2026, 10, 10, 12, 0, 0)


def build(position_ids=None):
    return timeline.build_timeline(position_ids, today=TODAY, now=NOW)


class StageHistoryTests(unittest.TestCase):
    def setUp(self):
        env.fresh_db()
        self.pid = env.add_position("Legacy Role")
        self.cid = env.add_candidate(self.pid, "Asha")

    def history(self, cid=None):
        return db.list_candidate_stage_history(cid or self.cid)

    def test_new_candidate_gets_a_recorded_sourced_row_at_cv_added_time(self):
        rows = self.history()
        self.assertEqual([r["stage_key"] for r in rows], ["sourced"])
        self.assertEqual(rows[0]["source"], "recorded")
        self.assertEqual(rows[0]["entered_at"], "2026-09-02 12:00:00")

    def test_every_stage_change_is_appended_with_a_timestamp_and_actor(self):
        db.update_stage(self.cid, "reached", True, actor_user_id=1)
        db.update_stage(self.cid, "l1_scheduled", True, actor_user_id=1)
        db.update_stage(self.cid, "l1_attended", True, actor_user_id=1)
        rows = self.history()
        self.assertEqual([r["stage_key"] for r in rows], ["sourced", "reached", "l1_scheduled", "l1_attended"])
        self.assertEqual([r["from_stage"] for r in rows], [None, "Sourced", "Reached", "L1 Scheduled"])
        self.assertTrue(all(r["entered_at"] for r in rows))
        self.assertEqual({r["actor_user_id"] for r in rows[1:]}, {1})
        self.assertTrue(all(r["source"] == "recorded" for r in rows))

    def test_flag_change_that_does_not_change_the_current_stage_adds_no_row(self):
        db.update_stage(self.cid, "l1_attended", True)            # now at L1 Attended
        before = len(self.history())
        db.update_stage(self.cid, "reached", True)                # earlier box: stage is still L1 Attended
        self.assertEqual(len(self.history()), before)

    def test_moving_back_is_recorded_too(self):
        db.update_stage(self.cid, "reached", True)
        db.update_stage(self.cid, "reached", False)
        self.assertEqual([r["stage_key"] for r in self.history()], ["sourced", "reached", "sourced"])

    def test_existing_stage_events_table_is_still_written(self):
        db.update_stage(self.cid, "reached", True)
        conn = db.get_conn()
        n = conn.execute("SELECT COUNT(*) n FROM stage_events WHERE candidate_id = ?", (self.cid,)).fetchone()["n"]
        conn.close()
        self.assertEqual(n, 1)

    def test_dynamic_round_changes_are_recorded(self):
        pid = env.add_position("Dynamic Role", dynamic=True)
        cid = env.add_candidate(pid, "Rohan")
        rid = db.get_interview_rounds(pid)[0]["id"]
        db.set_candidate_round_status(cid, rid, "scheduled", actor_user_id=1)
        db.set_candidate_round_status(cid, rid, "attended", actor_user_id=1)
        keys = [r["stage_key"] for r in self.history(cid)]
        self.assertEqual(keys, ["sourced", f"r{rid}_scheduled", f"r{rid}_attended"])
        self.assertEqual(self.history(cid)[-1]["stage_label"], "Tech Round Attended")

    def test_backfill_marks_rows_and_never_invents_dates(self):
        # a candidate created BEFORE history existed: flags set, a real stage_events timestamp, no history rows
        cid = env.add_candidate(self.pid, "Chitra")
        no_ts = env.add_candidate(self.pid, "Dora")           # flags but NO timestamp anywhere
        conn = db.get_conn()
        conn.execute("DELETE FROM candidate_stage_history WHERE candidate_id = ?", (cid,))
        conn.execute("UPDATE candidates SET reached = 1, l1_scheduled = 1 WHERE id = ?", (cid,))
        conn.execute("INSERT INTO stage_events (candidate_id, field, value, changed_at) VALUES (?, 'l1_scheduled', 1, '2026-09-20 08:00:00')", (cid,))
        conn.execute("DELETE FROM candidate_stage_history WHERE candidate_id = ?", (no_ts,))
        conn.execute("UPDATE candidates SET reached = 1 WHERE id = ?", (no_ts,))
        conn.commit()
        conn.close()
        self.assertEqual(db.backfill_stage_history(), 2)   # Asha already has history; only these two
        real = self.history(cid)
        self.assertEqual([r["source"] for r in real], ["backfilled", "backfilled"])
        self.assertEqual(real[-1]["stage_key"], "l1_scheduled")
        self.assertEqual(real[-1]["entered_at"], "2026-09-20 08:00:00")      # the real recorded timestamp
        self.assertEqual(real[-1]["basis"], "stage_events")
        proxy = self.history(no_ts)[-1]
        self.assertEqual((proxy["source"], proxy["basis"]), ("backfilled", "cv_added_proxy"))   # flagged estimate
        self.assertEqual(proxy["entered_at"], "2026-09-02 12:00:00")          # = CV-added date, nothing made up

    def test_backfill_is_idempotent(self):
        conn = db.get_conn()
        conn.execute("DELETE FROM candidate_stage_history")
        conn.commit()
        conn.close()
        first = db.backfill_stage_history()
        n = len(self.history())
        self.assertEqual(db.backfill_stage_history(), 0)
        self.assertEqual(len(self.history()), n)
        self.assertGreaterEqual(first, 1)
        db.init_db()  # the app's own start-up path, run again
        self.assertEqual(len(self.history()), n)


class AgingTests(unittest.TestCase):
    """Elapsed-day numbers, calendar days, '-' for missing dates, over-target flags."""

    def setUp(self):
        env.fresh_db()
        self.pid = env.add_position("Aging Role", created_at="2026-09-01 12:00:00")

    def row(self, cid):
        return next(r for r in build()["candidates"] if r["candidate_id"] == cid)

    def test_days_since_cv_and_in_stage_are_calendar_days(self):
        cid = env.add_candidate(self.pid, "Ela", created_at="2026-10-01 12:00:00")
        r = self.row(cid)
        self.assertEqual(r["cv_fed_on"], "2026-10-01")
        self.assertEqual(r["days_since_cv"], 9)
        self.assertEqual(r["days_in_stage"], 9)
        self.assertFalse(r["entered_is_estimate"])
        # calendar days: late evening to just after midnight is one day, not "0 full days"
        self.assertEqual(timeline.days_between(date(2026, 10, 9), date(2026, 10, 10)), 1)

    def test_position_numbers(self):
        env.add_candidate(self.pid, "Fay", created_at="2026-09-04 12:00:00")
        env.add_candidate(self.pid, "Gus", created_at="2026-09-09 12:00:00")
        p = build()["positions"][0]
        self.assertEqual(p["jd_uploaded_on"], "2026-09-01")
        self.assertEqual(p["first_cv_on"], "2026-09-04")
        self.assertEqual(p["days_jd_to_first_cv"], 3)
        self.assertEqual(p["cvs_received"], 2)
        self.assertEqual(p["open_days"], 39)
        self.assertTrue(p["open_over_target"])          # 39 > 30

    def test_missing_dates_show_as_none_never_invented(self):
        pid = env.add_position("Empty Role", created_at="2026-10-05 12:00:00")
        p = next(x for x in build()["positions"] if x["position_id"] == pid)
        self.assertIsNone(p["first_cv_on"])
        self.assertIsNone(p["days_jd_to_first_cv"])
        self.assertEqual(p["cvs_received"], 0)
        cid = env.add_candidate(self.pid, "Hal", created_at="2026-10-08 12:00:00")
        r = self.row(cid)
        for key in ("days_cv_to_schedule", "days_interview_to_feedback", "days_feedback_to_decision", "first_feedback_on"):
            self.assertIsNone(r[key], key)
        self.assertEqual(r["next_interview"], "-")
        self.assertEqual(r["feedback_status"], "-")
        self.assertEqual(timeline.fmt_days(None), "-")
        self.assertEqual(timeline.fmt_date(None), "-")
        self.assertEqual(timeline.days_between(None, TODAY), None)

    def test_stage_age_over_target_uses_the_configurable_target(self):
        cid = env.add_candidate(self.pid, "Ira", created_at="2026-10-02 12:00:00")   # 8 days in Sourced
        self.assertTrue(self.row(cid)["over_target"])
        db.set_timeline_targets({"stage_age_days": 10})
        self.assertFalse(self.row(cid)["over_target"])
        db.set_timeline_targets({"stage_age_days": 7})
        self.assertTrue(self.row(cid)["over_target"])

    def test_default_targets(self):
        self.assertEqual(db.get_timeline_targets(), {"stage_age_days": 5, "feedback_pending_days": 2,
                                                     "decision_pending_days": 3, "position_open_days": 30})

    def test_targets_validation(self):
        with self.assertRaises(ValueError):
            db.set_timeline_targets({"stage_age_days": "abc"})
        with self.assertRaises(ValueError):
            db.set_timeline_targets({"stage_age_days": 0})
        with self.assertRaises(ValueError):
            db.set_timeline_targets({"feedback_pending_days": 9999})
        self.assertEqual(db.get_timeline_targets()["stage_age_days"], 5)     # nothing half-saved
        db.set_timeline_targets({"stage_age_days": 8, "feedback_pending_days": 3})
        self.assertEqual(db.get_timeline_targets()["stage_age_days"], 8)
        self.assertEqual(db.get_timeline_targets()["feedback_pending_days"], 3)

    def test_terminal_stages_are_never_flagged_for_staying_put(self):
        cid = env.add_candidate(self.pid, "Jay", created_at="2026-09-02 12:00:00")
        db.update_stage(cid, "rejected", True)
        env.set_history_time(cid, "rejected", "2026-09-05 12:00:00")
        r = self.row(cid)
        self.assertEqual(r["phase"], "Rejected")
        self.assertFalse(r["over_target"])

    def test_estimated_stage_dates_are_marked_and_left_out_of_averages(self):
        cid = env.add_candidate(self.pid, "Kia", created_at="2026-09-20 12:00:00")
        conn = db.get_conn()
        conn.execute("DELETE FROM candidate_stage_history WHERE candidate_id = ?", (cid,))
        conn.execute("UPDATE candidates SET reached = 1 WHERE id = ?", (cid,))
        conn.commit()
        conn.close()
        db.backfill_stage_history()
        data = build()
        r = next(c for c in data["candidates"] if c["candidate_id"] == cid)
        self.assertTrue(r["entered_is_estimate"])
        self.assertEqual(r["stage_label"], "Reached")
        self.assertEqual(r["entered_stage_on"], "2026-09-20")                 # the CV-added date stands in
        self.assertIn("~", " ".join(r["over_target_reasons"]))                # reason says it is approximate
        phases = {x["phase"] for x in data["avg_days_per_stage"]}
        self.assertNotIn("Reached", phases)                                   # an estimated stay is not averaged

    def test_average_days_per_stage(self):
        a = env.add_candidate(self.pid, "Lea", created_at="2026-09-10 12:00:00")
        b = env.add_candidate(self.pid, "Max", created_at="2026-09-10 12:00:00")
        for cid, reached_on in ((a, "2026-09-12 12:00:00"), (b, "2026-09-16 12:00:00")):
            db.update_stage(cid, "reached", True)
            env.set_history_time(cid, "reached", reached_on)
        avg = {x["phase"]: x for x in build()["avg_days_per_stage"]}
        self.assertEqual(avg["Sourced"]["avg_days"], 4.0)                     # 2 and 6 days
        self.assertEqual(avg["Sourced"]["count"], 2)
        self.assertEqual(avg["Reached"]["avg_days"], 26.0)                    # open stays count up to today: 28 and 24 days
        self.assertEqual(build()["positions"][0]["avg_days_per_stage"]["Sourced"], 4.0)

    def test_candidates_sorted_most_delayed_first(self):
        old = env.add_candidate(self.pid, "Zed old", created_at="2026-09-05 12:00:00")
        mid = env.add_candidate(self.pid, "Amy mid", created_at="2026-09-25 12:00:00")
        new = env.add_candidate(self.pid, "Bob new", created_at="2026-10-09 12:00:00")
        order = [c["candidate_id"] for c in build()["candidates"]]
        self.assertEqual(order, [old, mid, new])
        self.assertFalse(build()["candidates"][2]["over_target"])


class InterviewAndFeedbackTests(unittest.TestCase):
    def setUp(self):
        env.fresh_db()
        self.pid = env.add_position("Interview Role", created_at="2026-09-01 12:00:00")
        self.cid = env.add_candidate(self.pid, "Nila", created_at="2026-09-03 12:00:00")

    def row(self):
        return next(r for r in build()["candidates"] if r["candidate_id"] == self.cid)

    def test_schedule_records_everything_and_moves_the_stage(self):
        iid = db.schedule_interview(self.cid, "l1", scheduled_for="2026-10-14 10:30", interviewers="Jordan, Priya",
                                    mode="Teams", link="https://teams.example/x", record_flag=True, scheduled_by=1)
        row = db.get_interview(iid)
        self.assertEqual((row["round_key"], row["round_label"]), ("l1", "L1 Interview"))
        self.assertEqual(row["scheduled_for"], "2026-10-14 10:30")
        self.assertEqual(row["interviewers"], "Jordan, Priya")
        self.assertEqual((row["mode"], row["link"], row["record_flag"]), ("Teams", "https://teams.example/x", 1))
        self.assertTrue(row["created_at"])                                   # when it was scheduled
        self.assertEqual(db.get_candidate(self.cid)["l1_scheduled"], 1)       # the stage reflects it
        self.assertEqual(db.list_candidate_stage_history(self.cid)[-1]["stage_key"], "l1_scheduled")
        self.assertIn("L1 Interview", self.row()["next_interview"])

    def test_dynamic_round_schedule(self):
        pid = env.add_position("Dyn", dynamic=True)
        cid = env.add_candidate(pid, "Omar")
        rid = db.get_interview_rounds(pid)[1]["id"]
        iid = db.schedule_interview(cid, f"r{rid}", scheduled_for="2026-10-20 09:00", scheduled_by=1)
        self.assertEqual(db.get_interview(iid)["round_label"], "HR Round")
        self.assertTrue(db.get_candidate_round_statuses(cid)[rid]["scheduled"])
        with self.assertRaises(ValueError):
            db.schedule_interview(cid, "r999999", scheduled_for="2026-10-20 09:00")
        with self.assertRaises(ValueError):
            db.schedule_interview(self.cid, "l9", scheduled_for="2026-10-20 09:00")

    def test_resending_the_same_slot_does_not_duplicate_but_a_new_time_is_a_reschedule(self):
        a = db.schedule_interview(self.cid, "l1", scheduled_for="2026-10-14 10:30", scheduled_by=1)
        b = db.schedule_interview(self.cid, "l1", scheduled_for="2026-10-14 10:30", link="https://x", scheduled_by=1)
        self.assertEqual(a, b)
        self.assertEqual(db.get_interview(a)["link"], "https://x")
        c = db.schedule_interview(self.cid, "l1", scheduled_for="2026-10-16 11:00", scheduled_by=1)
        self.assertNotEqual(a, c)
        listed = db.list_candidate_interviews(self.cid)
        self.assertEqual([i["is_current"] for i in listed], [True, False])
        self.assertEqual(self.row()["interviews_scheduled"], 1)               # only the live booking counts

    def test_feedback_save_and_list_multiple_panelists(self):
        iid = db.schedule_interview(self.cid, "l1", scheduled_for="2026-10-05 10:00", interviewers="A, B", scheduled_by=1)
        db.add_interview_feedback(iid, "Jordan", "pass", 4, "Strong.", saved_by=1)
        db.add_interview_feedback(iid, "Priya", "Hold", None, "", saved_by=1)
        fb = db.list_candidate_interviews(self.cid)[0]["feedback"]
        self.assertEqual([(f["panelist"], f["recommendation"], f["rating"]) for f in fb], [("Jordan", "Pass", 4), ("Priya", "Hold", None)])
        self.assertTrue(all(f["created_at"] for f in fb))
        for bad in (dict(panelist="", recommendation="Pass"), dict(panelist="X", recommendation="Maybe"),
                    dict(panelist="X", recommendation="Pass", rating=9), dict(panelist="X", recommendation="Pass", rating="abc")):
            with self.assertRaises(ValueError):
                db.add_interview_feedback(iid, **bad)
        with self.assertRaises(ValueError):
            db.add_interview_feedback(424242, "X", "Pass")
        self.assertEqual(len(db.list_candidate_interviews(self.cid)[0]["feedback"]), 2)

    def test_feedback_pending_and_saved_counts_and_days(self):
        iid = db.schedule_interview(self.cid, "l1", scheduled_for="2026-10-05 10:00", scheduled_by=1)   # 5 days before TODAY
        r = self.row()
        self.assertEqual((r["interviews_scheduled"], r["interviews_done"], r["feedback_pending"], r["feedback_saved"]), (1, 1, 1, 0))
        self.assertEqual(r["feedback_status"], "Pending 5d")
        self.assertTrue(any("Feedback pending 5d" in x for x in r["over_target_reasons"]))     # > target 2d
        fid = db.add_interview_feedback(iid, "Jordan", "Pass", 5, "Great", saved_by=1)
        env.set_time("interview_feedback", fid, "created_at", "2026-10-06 12:00:00")
        r = self.row()
        self.assertEqual((r["feedback_pending"], r["feedback_saved"]), (0, 1))
        self.assertEqual(r["feedback_status"], "Saved (1)")
        self.assertEqual(r["days_interview_to_feedback"], 1)
        self.assertEqual(r["first_feedback_on"], "2026-10-06")

    def test_upcoming_interview_is_not_pending(self):
        db.schedule_interview(self.cid, "l1", scheduled_for="2026-10-20 10:00", scheduled_by=1)
        r = self.row()
        self.assertEqual((r["interviews_done"], r["feedback_pending"]), (0, 0))
        self.assertEqual(r["feedback_status"], "Awaiting interview")
        self.assertFalse(any("Feedback pending" in x for x in r["over_target_reasons"]))

    def test_days_cv_to_schedule_and_feedback_to_decision(self):
        iid = db.schedule_interview(self.cid, "l1", scheduled_for="2026-10-05 10:00", scheduled_by=1)
        env.set_time("interview_schedule", iid, "created_at", "2026-09-08 12:00:00")      # scheduled 5 days after the CV
        fid = db.add_interview_feedback(iid, "Jordan", "Pass", 4, "ok", saved_by=1)
        env.set_time("interview_feedback", fid, "created_at", "2026-10-06 12:00:00")
        db.update_stage(self.cid, "offer_made", True)
        env.set_history_time(self.cid, "offer_made", "2026-10-09 12:00:00")             # decision 3 days after feedback
        r = self.row()
        self.assertEqual(r["days_cv_to_schedule"], 5)
        self.assertEqual(r["days_feedback_to_decision"], 3)
        self.assertEqual(r["phase"], "Offer Made")

    def test_decision_pending_flag(self):
        iid = db.schedule_interview(self.cid, "l1", scheduled_for="2026-09-25 10:00", scheduled_by=1)
        fid = db.add_interview_feedback(iid, "Jordan", "Pass", 4, "ok", saved_by=1)
        env.set_time("interview_feedback", fid, "created_at", "2026-09-26 12:00:00")
        db.update_stage(self.cid, "l1_attended", True)
        r = self.row()
        self.assertEqual(r["decision_pending_days"], 14)
        self.assertTrue(any("No decision 14d after feedback" in x for x in r["over_target_reasons"]))

    def test_rounds_ticked_in_the_tracker_without_details_still_count(self):
        db.update_stage(self.cid, "l1_attended", True)
        r = self.row()
        self.assertEqual((r["interviews_scheduled"], r["interviews_done"], r["feedback_pending"]), (1, 1, 1))
        self.assertEqual(r["feedback_status"], "Pending")                     # no date known, so no day count
        self.assertEqual(r["interviews"][0]["id"], None)

    def test_parse_interview_datetime(self):
        p = timeline.parse_interview_datetime
        self.assertEqual(p("20-08-2026", "3:00 PM IST"), "2026-08-20 15:00")
        self.assertEqual(p("2026-10-14", "10:30"), "2026-10-14 10:30")
        self.assertEqual(p("14 Oct 2026", "9 am"), "2026-10-14 09:00")
        self.assertEqual(p("2026-10-14", ""), "2026-10-14")
        self.assertEqual(p("2026-10-14", "evening"), "2026-10-14")
        self.assertIsNone(p("not a date", "10:00"))
        self.assertIsNone(p("", "10:00"))


class DashboardSectionTests(unittest.TestCase):
    def setUp(self):
        env.fresh_db()
        self.p1 = env.add_position("Alpha", created_at="2026-09-01 12:00:00")
        self.p2 = env.add_position("Beta", created_at="2026-09-20 12:00:00")
        self.a = env.add_candidate(self.p1, "Ann", created_at="2026-09-03 12:00:00")
        self.b = env.add_candidate(self.p1, "Bo", created_at="2026-09-04 12:00:00")
        self.c = env.add_candidate(self.p2, "Cy", created_at="2026-09-21 12:00:00")
        db.update_stage(self.a, "reached", True)
        iid = db.schedule_interview(self.a, "l1", scheduled_for="2026-10-05 10:00", scheduled_by=1)
        db.update_stage(self.a, "l1_attended", True)
        db.add_interview_feedback(iid, "Jordan", "Pass", 4, "ok", saved_by=1)
        db.schedule_interview(self.b, "l1", scheduled_for="2026-10-04 10:00", scheduled_by=1)

    def test_section_data_has_every_column_the_spec_lists(self):
        data = build()
        pos = next(p for p in data["positions"] if p["position_id"] == self.p1)
        for key in ("jd_uploaded_on", "first_cv_on", "cvs_received", "stage_counts", "interviews_scheduled", "interviews_done",
                    "feedback_saved", "feedback_pending", "open_days", "avg_days_per_stage", "items_over_target"):
            self.assertIn(key, pos)
        self.assertEqual((pos["cvs_received"], pos["interviews_scheduled"], pos["interviews_done"]), (2, 2, 2))
        self.assertEqual((pos["feedback_saved"], pos["feedback_pending"]), (1, 1))
        self.assertEqual(pos["stage_counts"]["Interviewed"], 1)
        self.assertEqual(sum(pos["stage_counts"].values()), 2)
        cand = data["candidates"][0]
        for key in ("name", "position_title", "cv_fed_on", "stage_label", "entered_stage_on", "days_in_stage", "next_interview",
                    "feedback_status", "over_target", "days_since_cv", "days_cv_to_schedule", "days_interview_to_feedback",
                    "days_feedback_to_decision"):
            self.assertIn(key, cand)

    def test_position_scope_equals_the_overall_numbers_for_that_position(self):
        overall = build()
        scoped = build([self.p1])
        self.assertEqual(scoped["totals"]["positions"], 1)
        self.assertEqual([p["position_id"] for p in scoped["positions"]], [self.p1])
        o = next(p for p in overall["positions"] if p["position_id"] == self.p1)
        self.assertEqual(scoped["positions"][0], o)                           # identical row, same functions
        self.assertEqual({c["candidate_id"] for c in scoped["candidates"]}, {self.a, self.b})
        self.assertEqual([c for c in overall["candidates"] if c["position_id"] == self.p1], scoped["candidates"])
        self.assertEqual(build([])["positions"], [])                          # a user with no assignments sees nothing

    def test_avg_days_graph_data_is_scoped(self):
        scoped = build([self.p2])
        self.assertEqual(sum(x["count"] for x in scoped["avg_days_per_stage"]), 1)
        self.assertGreater(sum(x["count"] for x in build()["avg_days_per_stage"]), 1)


if __name__ == "__main__":
    unittest.main()
