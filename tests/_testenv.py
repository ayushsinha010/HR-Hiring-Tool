"""Test environment: a temp data folder, set BEFORE db.py is imported (db reads HR_AGENT_DATA_DIR at import)."""
import atexit
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="hr_agent_tests_")
os.environ["HR_AGENT_DATA_DIR"] = TMP
# A known first-admin login for the throw-away database (the real app generates a random one on first run).
ADMIN_PASSWORD = "unit-test-password"
os.environ["HR_AGENT_ADMIN_PASSWORD"] = ADMIN_PASSWORD
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402

assert os.path.abspath(db.DATA_DIR) == os.path.abspath(TMP), "tests must never use the real data folder"
assert os.path.abspath(db.DATA_DIR) != os.path.join(ROOT, "data")


def fresh_db():
    """Empty database (default 'admin' user only) for one test."""
    import gc

    gc.collect()  # a failed earlier test can keep a connection alive in its traceback
    if os.path.exists(db.DB_PATH):
        try:
            os.remove(db.DB_PATH)
        except PermissionError:           # Windows: file still held - empty every table instead
            conn = sqlite3.connect(db.DB_PATH)
            conn.execute("PRAGMA foreign_keys = OFF")
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
                conn.execute(f"DELETE FROM {name}")
            conn.execute("DELETE FROM sqlite_sequence")
            conn.commit()
            conn.close()
    shutil.rmtree(os.path.join(db.DATA_DIR, "positions"), ignore_errors=True)
    db.init_db()


def add_position(title="Test Role", dynamic=False, created_at="2026-09-01 12:00:00", rounds=("Tech Round", "HR Round")):
    pid = db.create_position(title, "jd.txt", "Python Django APIs")
    conn = db.get_conn()
    conn.execute("UPDATE positions SET created_at = ? WHERE id = ?", (created_at, pid))
    conn.commit()
    conn.close()
    if dynamic:
        db.set_uses_dynamic_rounds(pid, True)
        db.create_interview_rounds(pid, list(rounds))
    return pid


def add_candidate(position_id, name, created_at="2026-09-02 12:00:00", score=None, breakdown=None):
    cid = db.add_candidate(position_id, f"{name}.txt", "", name, f"resume of {name}")
    conn = db.get_conn()
    conn.execute("UPDATE candidates SET created_at = ? WHERE id = ?", (created_at, cid))
    conn.execute("UPDATE candidate_stage_history SET entered_at = ? WHERE candidate_id = ? AND stage_key = 'sourced'", (created_at, cid))
    conn.commit()
    conn.close()
    if score is not None:
        db.save_score(cid, score, f"summary {name}", ["plus"], ["minus"], breakdown=breakdown)
    return cid


def set_history_time(candidate_id, stage_key, when):
    """Pins a history row's timestamp (UTC string) so aging numbers are deterministic."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE candidate_stage_history SET entered_at = ? WHERE candidate_id = ? AND stage_key = ? AND source = 'recorded'",
        (when, candidate_id, stage_key),
    )
    conn.commit()
    conn.close()


def set_time(table, row_id, column, when):
    conn = db.get_conn()
    conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (when, row_id))
    conn.commit()
    conn.close()
