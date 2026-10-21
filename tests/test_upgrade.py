"""The live database is upgraded by the app's own init on start. These tests make sure that is additive,
idempotent and leaves every existing row alone - on a synthetic old-schema database."""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from . import _testenv as env

ROOT = env.ROOT
NEW_TABLES = ("candidate_stage_history", "interview_schedule", "interview_feedback")


def run_init(data_dir, times=1):
    code = "import db\n" + "db.init_db()\n" * times + "print(db.backfill_stage_history())"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "HR_AGENT_DATA_DIR": data_dir},
                       capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1]


def table_dump(conn, table, exclude=()):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] not in exclude]
    return [tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1")]


class UpgradeTests(unittest.TestCase):
    def make_legacy_db(self, folder):
        """A database as it was before this feature: current schema minus the new tables/column."""
        subprocess.run([sys.executable, "-c", "import db; db.init_db()"], cwd=ROOT, env={**os.environ, "HR_AGENT_DATA_DIR": folder}, check=True, capture_output=True)
        path = os.path.join(folder, "hr.db")
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO positions (title, jd_filename, jd_text, created_at, uses_dynamic_rounds) VALUES ('Old Role', 'jd.txt', 'x', '2026-08-01 10:00:00', 0)")
        conn.execute("INSERT INTO candidates (position_id, resume_filename, resume_path, candidate_name, created_at, bucket, reached, l1_scheduled) VALUES (1, 'a.txt', '', 'Old A', '2026-08-02 10:00:00', 'unscored', 1, 1)")
        conn.execute("INSERT INTO candidates (position_id, resume_filename, resume_path, candidate_name, created_at, bucket) VALUES (1, 'b.txt', '', 'Old B', '2026-08-03 10:00:00', 'unscored')")
        conn.execute("INSERT INTO stage_events (candidate_id, field, value, changed_at) VALUES (1, 'l1_scheduled', 1, '2026-08-05 09:00:00')")
        conn.execute("UPDATE candidates SET score = 7.5, fit_summary = 'ok', bucket = 'green' WHERE id = 1")
        for t in NEW_TABLES:
            conn.execute(f"DROP TABLE {t}")
        conn.execute("ALTER TABLE candidates DROP COLUMN score_breakdown_json")
        conn.commit()
        conn.close()
        return path

    def test_synthetic_legacy_database_is_upgraded_safely_and_idempotently(self):
        folder = tempfile.mkdtemp(prefix="hr_upgrade_")
        self.addCleanup(shutil.rmtree, folder, True)
        path = self.make_legacy_db(folder)
        before = sqlite3.connect(path)
        snap = {t: table_dump(before, t) for t in ("positions", "candidates", "stage_events", "users", "settings")}
        before.close()

        backfilled = run_init(folder)
        self.assertEqual(backfilled, "0")                      # init already backfilled; a second explicit call has nothing left
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(NEW_TABLES) <= tables)
        self.assertIn("score_breakdown_json", [r[1] for r in conn.execute("PRAGMA table_info(candidates)")])
        for t, rows in snap.items():                           # every pre-existing row is exactly as it was
            self.assertEqual(table_dump(conn, t, exclude=("score_breakdown_json",)), rows, t)
        hist = conn.execute("SELECT candidate_id, stage_key, entered_at, source, basis FROM candidate_stage_history ORDER BY id").fetchall()
        self.assertEqual(hist, [
            (1, "sourced", "2026-08-02 10:00:00", "backfilled", "cv_added"),
            (1, "l1_scheduled", "2026-08-05 09:00:00", "backfilled", "stage_events"),   # a real recorded timestamp, not invented
            (2, "sourced", "2026-08-03 10:00:00", "backfilled", "cv_added"),
        ])
        conn.close()

        run_init(folder, times=3)                              # idempotent: more starts change nothing
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM candidate_stage_history").fetchone()[0], 3)
        for t, rows in snap.items():
            self.assertEqual(table_dump(conn, t, exclude=("score_breakdown_json",)), rows, t)
        conn.close()


if __name__ == "__main__":
    unittest.main()
