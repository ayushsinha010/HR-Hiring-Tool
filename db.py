import sqlite3
import os
import json
import itertools
import re
import secrets

import config  # noqa: F401  (loads .env before the settings below are read)

DATA_DIR = os.environ.get("HR_AGENT_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "hr.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    jd_filename TEXT,
    jd_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status_changed_at TEXT,
    start_date TEXT,
    target_end_date TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    resume_filename TEXT NOT NULL,
    resume_path TEXT NOT NULL,
    candidate_name TEXT,
    resume_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    score REAL,
    fit_summary TEXT,
    positives_json TEXT,
    negatives_json TEXT,
    bucket TEXT NOT NULL DEFAULT 'unscored',
    scored_at TEXT,
    reached INTEGER NOT NULL DEFAULT 0,
    l1_scheduled INTEGER NOT NULL DEFAULT 0,
    l1_attended INTEGER NOT NULL DEFAULT 0,
    l1_cleared INTEGER NOT NULL DEFAULT 0,
    l2_scheduled INTEGER NOT NULL DEFAULT 0,
    l2_attended INTEGER NOT NULL DEFAULT 0,
    l2_cleared INTEGER NOT NULL DEFAULT 0,
    l3_scheduled INTEGER NOT NULL DEFAULT 0,
    l3_attended INTEGER NOT NULL DEFAULT 0,
    l3_cleared INTEGER NOT NULL DEFAULT 0,
    offer_made INTEGER NOT NULL DEFAULT 0,
    joined INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    on_hold INTEGER NOT NULL DEFAULT 0,
    interview_video_filename TEXT,
    interview_video_path TEXT,
    interview_video_url TEXT,
    interview_transcript_filename TEXT,
    interview_transcript_path TEXT,
    interview_transcript_text TEXT,
    interview_transcript_url TEXT,
    goodfit_video_filename TEXT,
    goodfit_video_path TEXT,
    goodfit_video_url TEXT,
    goodfit_transcript_filename TEXT,
    goodfit_transcript_path TEXT,
    goodfit_transcript_text TEXT,
    goodfit_transcript_url TEXT,
    email TEXT,
    l1_teams_link TEXT,
    l2_teams_link TEXT,
    l3_teams_link TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    email_address TEXT,
    email_app_password TEXT,
    email_smtp_host TEXT DEFAULT 'smtp.office365.com',
    email_smtp_port INTEGER DEFAULT 587
);

CREATE TABLE IF NOT EXISTS email_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    email_type TEXT NOT NULL,
    round TEXT,
    recipient TEXT,
    subject TEXT,
    sent_by TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS stage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    field TEXT NOT NULL,
    value INTEGER NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS offer_approval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    action TEXT NOT NULL,
    actor TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS onboarding_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    item_key TEXT NOT NULL,
    label TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stakeholders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    level TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(position_id, level)
);

CREATE TABLE IF NOT EXISTS candidate_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    text TEXT NOT NULL,
    noted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assessment_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    question_text TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assessment_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    question_id INTEGER NOT NULL REFERENCES assessment_questions(id),
    answer_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(candidate_id, question_id)
);

CREATE TABLE IF NOT EXISTS assessment_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    position_id INTEGER NOT NULL REFERENCES positions(id),
    score REAL,
    summary TEXT,
    positives_json TEXT,
    negatives_json TEXT,
    scored_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidate_scorecards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    score REAL,
    summary TEXT,
    positives_json TEXT,
    negatives_json TEXT,
    notes_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS capture_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    raw_text TEXT NOT NULL,
    matched_label TEXT,
    candidate_id INTEGER REFERENCES candidates(id),
    action_summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'saved',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---- Per-hiring access control & assignment ----------------------------------
-- Deliberately NOT a fixed role enum: role_label is free text (e.g. "Technical
-- Interviewer", "HR Screener") since the owner explicitly wants the set of
-- labels to be changeable/addable over time without a schema change. Access
-- itself is governed by the existence of a row here (position-level), not by
-- the label text - reassigning/unassigning is a normal delete/update, never a
-- soft-lock.
CREATE TABLE IF NOT EXISTS position_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    assigned_user_id INTEGER NOT NULL REFERENCES users(id),
    role_label TEXT,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(position_id, assigned_user_id)
);

-- Append-only, like candidate_notes/candidate_scorecards - an interviewer's
-- rating/remarks for one interview round on one candidate. Multiple rows per
-- candidate are expected (different rounds, or a rating revised over time).
CREATE TABLE IF NOT EXISTS candidate_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    position_id INTEGER NOT NULL REFERENCES positions(id),
    rated_by_user_id INTEGER NOT NULL REFERENCES users(id),
    round_label TEXT,
    rating INTEGER,
    remarks TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---- Audit log ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    actor_label TEXT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    candidate_id INTEGER REFERENCES candidates(id),
    position_id INTEGER REFERENCES positions(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---- Interview & assessment question recommendations (not a scored assessment) -
-- candidate_id NULL = a position-level (JD-only) batch of suggestions; non-null =
-- generated with a specific candidate's resume text also considered. Append-only
-- so past recommendation batches are never lost when a new one is generated.
CREATE TABLE IF NOT EXISTS interview_question_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    candidate_id INTEGER REFERENCES candidates(id),
    generated_by_user_id INTEGER REFERENCES users(id),
    questions_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Personal to-do list (common "Open Tasks"/"Completed" dashboard widgets). v1 is deliberately
-- self-assign only (assigned_to_user_id is always the creator) - no
-- delegation UI yet, same "don't build for hypothetical needs" discipline as
-- the rest of this app.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    due_date TEXT,
    position_id INTEGER REFERENCES positions(id),
    assigned_to_user_id INTEGER NOT NULL REFERENCES users(id),
    created_by INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

-- Dynamic interview rounds (Feature added 2026-09-03, per explicit request).
-- Applies only to positions created from now on (positions.uses_dynamic_rounds
-- flag) - existing positions keep their fixed L1/L2/L3 columns untouched, by
-- deliberate decision, since real candidate history is already tracked
-- against those columns and shouldn't be migrated/risked.
CREATE TABLE IF NOT EXISTS interview_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    round_number INTEGER NOT NULL,
    round_label TEXT NOT NULL,
    assigned_user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per candidate per round of a dynamic-rounds position - the dynamic
-- equivalent of the legacy l1_scheduled/l1_attended/l1_cleared columns, just
-- keyed by round_id instead of a fixed column name so it works for any
-- number of rounds.
CREATE TABLE IF NOT EXISTS candidate_round_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    round_id INTEGER NOT NULL REFERENCES interview_rounds(id),
    scheduled INTEGER NOT NULL DEFAULT 0,
    attended INTEGER NOT NULL DEFAULT 0,
    cleared INTEGER NOT NULL DEFAULT 0,
    scheduled_at TEXT,
    attended_at TEXT,
    cleared_at TEXT,
    UNIQUE(candidate_id, round_id)
);

-- In-app notifications - currently only fired when someone is assigned to
-- conduct an interview round, but kept generic (title/message/position_id)
-- so future notification types don't need a schema change.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    position_id INTEGER REFERENCES positions(id),
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---- Hiring timeline, stage history, interview scheduling & panel feedback ----
-- (see timeline.py for the aging maths.)
-- All three tables are NEW and additive: no existing table/column is altered, and
-- the existing stage_events table (raw checkbox flips, used by the heatmap, the
-- calendar and days-to-hire) is left exactly as it was.
--
-- Stage-level, append-only history: one row each time a candidate's CURRENT stage
-- (Sourced / Reached / <round> Scheduled / ... / Offer Made / Joined / Rejected)
-- changes. source = 'recorded' for rows written live by the app, 'backfilled' for
-- the one-time rows created at upgrade for candidates that already existed - for
-- those, basis says which existing timestamp was used ('cv_added' = the CV's own
-- added date, 'stage_events' / 'round_status' = a real recorded stage timestamp,
-- 'cv_added_proxy' = NO stage timestamp existed, so the CV-added date stands in
-- and the UI shows it as an estimate). Dates are never invented.
CREATE TABLE IF NOT EXISTS candidate_stage_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    position_id INTEGER REFERENCES positions(id) ON DELETE CASCADE,
    from_stage TEXT,
    stage_key TEXT NOT NULL,
    stage_label TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'recorded',
    basis TEXT,
    actor_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stage_history_candidate ON candidate_stage_history(candidate_id, entered_at);
CREATE INDEX IF NOT EXISTS idx_stage_history_position ON candidate_stage_history(position_id);

-- One row per "interview scheduled" action (append-only: rescheduling adds a new
-- row, the latest row for a candidate+round is the live one). round_key is
-- 'l1'/'l2'/'l3' on the legacy fixed-round positions and 'r<interview_rounds.id>'
-- on dynamic-rounds positions. scheduled_for is ISO 'YYYY-MM-DD HH:MM' (or just
-- 'YYYY-MM-DD') when the date/time could be read, else NULL with the raw text kept
-- in scheduled_for_text. created_at = when it was scheduled in the app.
CREATE TABLE IF NOT EXISTS interview_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    position_id INTEGER REFERENCES positions(id) ON DELETE CASCADE,
    round_key TEXT NOT NULL,
    round_id INTEGER,
    round_label TEXT NOT NULL,
    scheduled_for TEXT,
    scheduled_for_text TEXT,
    interviewers TEXT,
    mode TEXT,
    link TEXT,
    record_flag INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    scheduled_by INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_interview_schedule_candidate ON interview_schedule(candidate_id);

-- Panel feedback on one scheduled interview. Several rows per interview (one per
-- panel member). recommendation is Pass / Hold / Reject; rating (1-5) is optional.
CREATE TABLE IF NOT EXISTS interview_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interview_schedule(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    position_id INTEGER REFERENCES positions(id) ON DELETE CASCADE,
    panelist TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    rating INTEGER,
    comments TEXT,
    saved_by INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_interview_feedback_interview ON interview_feedback(interview_id);
"""

DEFAULT_LLM_SETTINGS = {
    "llm_provider": "openrouter",
    "llm_api_key": "",
    "llm_model": "openai/gpt-5.4",
    "llm_base_url": "https://openrouter.ai/api/v1",
}

DEFAULT_ONBOARDING_ITEMS = [
    "Laptop issued",
    "ID card issued",
    "Email & system access provisioned",
    "Buddy assigned",
    "Day-1 orientation completed",
]

OFFER_STATUSES = ("draft", "pending_approval", "needs_edit", "approved", "rejected", "sent", "accepted", "declined")
OFFER_STATUS_LABELS = {
    "draft": "Draft",
    "pending_approval": "Pending Approval",
    "needs_edit": "Needs Edit",
    "approved": "Approved",
    "rejected": "Rejected",
    "sent": "Sent to Candidate",
    "accepted": "Accepted",
    "declined": "Declined",
}

# Ordered pipeline checklist shown on the tracker dashboard - (db column, short label, group)
STAGE_FIELDS = [
    ("reached", "Reached", "Sourcing"),
    ("l1_scheduled", "Scheduled", "L1 Interview"),
    ("l1_attended", "Attended", "L1 Interview"),
    ("l1_cleared", "Cleared", "L1 Interview"),
    ("l2_scheduled", "Scheduled", "L2 Interview"),
    ("l2_attended", "Attended", "L2 Interview"),
    ("l2_cleared", "Cleared", "L2 Interview"),
    ("l3_scheduled", "Scheduled", "L3 Interview"),
    ("l3_attended", "Attended", "L3 Interview"),
    ("l3_cleared", "Cleared", "L3 Interview"),
    ("offer_made", "Offer Made", "Offer"),
    ("joined", "Joined", "Outcome"),
]
STATUS_FIELDS = [
    ("rejected", "Rejected"),
    ("on_hold", "On Hold"),
]
ALL_STAGE_COLUMNS = [f[0] for f in STAGE_FIELDS] + [f[0] for f in STATUS_FIELDS]

_ROUND_FIELD_RE = re.compile(r"^l([123])_(scheduled|attended|cleared)$")
_PLAIN_STAGE_LABELS = {f: label for f, label, _g in STAGE_FIELDS}
_PLAIN_STAGE_LABELS.update({f: label for f, label in STATUS_FIELDS})


def _stage_field_label(field):
    """Human label for a stage field, e.g. 'l2_scheduled' -> 'L2 Interview -
    Scheduled', used to build readable audit log entries. Same formatting
    convention as capture.stage_field_label(), kept as a separate small copy
    here rather than importing capture.py (which itself imports db, so
    importing it back would be circular)."""
    m = _ROUND_FIELD_RE.match(field)
    if m:
        sub_label = {"scheduled": "Scheduled", "attended": "Attended", "cleared": "Cleared"}[m.group(2)]
        return f"L{m.group(1)} Interview - {sub_label}"
    return _PLAIN_STAGE_LABELS.get(field, field)


def stage_groups():
    return [(group, list(items)) for group, items in itertools.groupby(STAGE_FIELDS, key=lambda f: f[2])]


def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_add_column(conn, "positions", "manual_status", "TEXT")
    _migrate_add_column(conn, "positions", "status_changed_at", "TEXT")
    _migrate_add_column(conn, "positions", "start_date", "TEXT")
    _migrate_add_column(conn, "positions", "target_end_date", "TEXT")
    _migrate_add_column(conn, "positions", "watch_folder", "TEXT")
    _migrate_add_column(conn, "candidates", "watched_source_path", "TEXT")
    _migrate_add_column(conn, "candidates", "source_missing", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "candidates", "imported_no_resume", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "candidates", "offer_expiry_date", "TEXT")
    _migrate_add_column(conn, "candidates", "offer_employment_type", "TEXT")
    _migrate_add_column(conn, "users", "designation", "TEXT")
    _migrate_add_column(conn, "users", "department", "TEXT")
    _migrate_add_column(conn, "positions", "uses_dynamic_rounds", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "candidate_round_status", "teams_link", "TEXT")
    # Per-parameter score breakdown (JSON list of {name, weight, score, evidence}); NULL for
    # anything scored before it existed - those keep working, the popup just says so.
    _migrate_add_column(conn, "candidates", "score_breakdown_json", "TEXT")
    for old, new in [
        ("gd_video_filename", "goodfit_video_filename"),
        ("gd_video_path", "goodfit_video_path"),
        ("gd_transcript_filename", "goodfit_transcript_filename"),
        ("gd_transcript_path", "goodfit_transcript_path"),
        ("gd_transcript_text", "goodfit_transcript_text"),
    ]:
        _migrate_rename_column(conn, "candidates", old, new)
    for column in (
        "interview_video_filename",
        "interview_video_path",
        "interview_video_url",
        "interview_transcript_filename",
        "interview_transcript_path",
        "interview_transcript_text",
        "interview_transcript_url",
        "created_at",
        "goodfit_video_filename",
        "goodfit_video_path",
        "goodfit_video_url",
        "goodfit_transcript_filename",
        "goodfit_transcript_path",
        "goodfit_transcript_text",
        "goodfit_transcript_url",
        "email",
        "l1_teams_link",
        "l2_teams_link",
        "l3_teams_link",
    ):
        _migrate_add_column(conn, "candidates", column, "TEXT")
    _migrate_add_column(conn, "users", "email_address", "TEXT")
    _migrate_add_column(conn, "users", "email_app_password", "TEXT")
    _migrate_add_column(conn, "users", "email_smtp_host", "TEXT")
    _migrate_add_column(conn, "users", "email_smtp_port", "INTEGER")
    _migrate_add_column(conn, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
    for column in (
        "offer_status",
        "offer_designation",
        "offer_ctc",
        "offer_joining_date",
        "offer_notes",
        "offer_approver_email",
        "offer_approval_token",
        "offer_candidate_token",
        "offer_rejection_reason",
        "offer_sent_for_approval_at",
        "offer_approved_at",
        "offer_sent_to_candidate_at",
        "offer_responded_at",
        "offer_pdf_path",
        "human_review",
    ):
        _migrate_add_column(conn, "candidates", column, "TEXT")
    # Backfill candidates.created_at for rows added before this column existed -
    # their position's created_at is the closest honest proxy we have (better
    # than "now", which would hide their real age).
    conn.execute(
        """UPDATE candidates SET created_at = (
               SELECT positions.created_at FROM positions WHERE positions.id = candidates.position_id
           ) WHERE created_at IS NULL"""
    )
    conn.commit()
    conn.close()
    ensure_default_user()
    _ensure_an_admin_exists()
    _cleanup_candidate_names_once()
    _backfill_candidate_emails()
    _backfill_stage_history_safely()


def _backfill_stage_history_safely():
    """Runs the one-time (idempotent) stage-history backfill on every start. A
    failure here must never stop the app from starting - the dashboard copes
    with candidates that have no history rows yet - so it is reported, not raised."""
    try:
        backfill_stage_history()
    except Exception as exc:  # noqa: BLE001
        import sys

        print(f"[db] stage-history backfill skipped: {exc}", file=sys.stderr)


def _ensure_an_admin_exists():
    """Multi-user support (position assignments/scoped access) was added after
    this app already had a single 'admin' user with no admin/non-admin
    distinction. Backfill: mark the literal 'admin' account as admin if
    present, and if - for any reason - no user is flagged admin at all (e.g. a
    renamed seed account), promote the earliest-created user rather than
    leaving nobody able to manage the app."""
    conn = get_conn()
    conn.execute("UPDATE users SET is_admin = 1 WHERE username = 'admin'")
    any_admin = conn.execute("SELECT COUNT(*) as n FROM users WHERE is_admin = 1").fetchone()["n"]
    if not any_admin:
        first = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if first:
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (first["id"],))
    conn.commit()
    conn.close()


def _cleanup_candidate_names_once():
    """One-time pass: re-derive candidate_name from filename+resume_text with the
    current (better) parsing rules, for candidates uploaded before that logic
    existed - e.g. 'Portal_ASHARMA[6y_0m]' -> 'ASHARMA'. Guarded by a settings
    flag so it only runs once, in case a manual rename feature is added later."""
    conn = get_conn()
    done = conn.execute("SELECT value FROM settings WHERE key = 'candidate_names_cleaned_v1'").fetchone()
    if done:
        conn.close()
        return

    import resume_parser

    rows = conn.execute("SELECT id, resume_filename, resume_text FROM candidates").fetchall()
    for row in rows:
        new_name = resume_parser.guess_candidate_name(row["resume_filename"], row["resume_text"] or "")
        conn.execute("UPDATE candidates SET candidate_name = ? WHERE id = ?", (new_name, row["id"]))
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('candidate_names_cleaned_v1', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    conn.commit()
    conn.close()


def _backfill_candidate_emails():
    """Fills in candidates.email from resume_text for anyone missing it - safe to
    run every startup since it only ever fills NULLs, never overwrites."""
    import resume_parser

    conn = get_conn()
    rows = conn.execute("SELECT id, resume_text FROM candidates WHERE email IS NULL").fetchall()
    for row in rows:
        found = resume_parser.extract_email(row["resume_text"] or "")
        if found:
            conn.execute("UPDATE candidates SET email = ? WHERE id = ?", (found, row["id"]))
    conn.commit()
    conn.close()


def _migrate_add_column(conn, table, column, col_type):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()


def _migrate_rename_column(conn, table, old, new):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if old in existing and new not in existing:
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        conn.commit()


def ensure_default_user():
    """Seed a login if the users table is empty - migrates users.json once if present (legacy format
    {"username": {"password_hash": "..."}}), else creates the first admin user 'admin'.

    The first admin's password is taken from the HR_AGENT_ADMIN_PASSWORD environment variable; if that
    is not set, a random one is generated and printed once to the console. Change it afterwards under
    Settings -> Change Password."""
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
    if count > 0:
        conn.close()
        return

    users_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
    seeded = False
    if os.path.exists(users_json_path):
        with open(users_json_path, "r", encoding="utf-8") as f:
            legacy_users = json.load(f)
        for username, data in legacy_users.items():
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
                (username, username.capitalize(), data["password_hash"]),
            )
            seeded = True

    if not seeded:
        from werkzeug.security import generate_password_hash

        admin_password = os.environ.get("HR_AGENT_ADMIN_PASSWORD")
        generated = not admin_password
        if generated:
            admin_password = secrets.token_urlsafe(9)
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
            ("admin", "Admin", generate_password_hash(admin_password)),
        )
        if generated:
            print(
                "\n[first run] Created the admin user 'admin' with a generated password: "
                f"{admin_password}\n"
                "           Note it down now (it is not stored in plain text), then change it under "
                "Settings -> Change Password.\n"
                "           To choose your own, set HR_AGENT_ADMIN_PASSWORD before the first start.\n",
                flush=True,
            )

    conn.commit()
    conn.close()


def get_user_by_username(username):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_user_password(user_id, password_hash):
    conn = get_conn()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    conn.commit()
    conn.close()


def update_user_display_name(user_id, display_name):
    conn = get_conn()
    conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
    conn.commit()
    conn.close()


def update_user_profile(user_id, display_name, designation, department):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET display_name = ?, designation = ?, department = ? WHERE id = ?",
        (display_name, (designation or "").strip() or None, (department or "").strip() or None, user_id),
    )
    conn.commit()
    conn.close()


def get_llm_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    stored = {r["key"]: r["value"] for r in rows}
    return {**DEFAULT_LLM_SETTINGS, **stored}


def update_llm_settings(provider=None, api_key=None, model=None):
    updates = {}
    if provider is not None:
        updates["llm_provider"] = provider
    if api_key:  # blank means "keep the existing key" - never overwrite with empty
        updates["llm_api_key"] = api_key
    if model is not None:
        updates["llm_model"] = model
    if not updates:
        return
    conn = get_conn()
    for key, value in updates.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()
    conn.close()


def position_dir(position_id):
    d = os.path.join(DATA_DIR, "positions", str(position_id))
    os.makedirs(os.path.join(d, "resumes"), exist_ok=True)
    return d


def interview_media_dir(position_id, candidate_id):
    d = os.path.join(DATA_DIR, "positions", str(position_id), "interviews", str(candidate_id))
    os.makedirs(d, exist_ok=True)
    return d


def save_interview_media(candidate_id, **fields):
    allowed = {
        "interview_video_filename",
        "interview_video_path",
        "interview_video_url",
        "interview_transcript_filename",
        "interview_transcript_path",
        "interview_transcript_text",
        "interview_transcript_url",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    conn = get_conn()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE candidates SET {set_clause} WHERE id = ?", (*updates.values(), candidate_id))
    conn.commit()
    conn.close()


def save_goodfit_media(candidate_id, **fields):
    allowed = {
        "goodfit_video_filename",
        "goodfit_video_path",
        "goodfit_video_url",
        "goodfit_transcript_filename",
        "goodfit_transcript_path",
        "goodfit_transcript_text",
        "goodfit_transcript_url",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    conn = get_conn()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE candidates SET {set_clause} WHERE id = ?", (*updates.values(), candidate_id))
    conn.commit()
    conn.close()


def update_candidate_email(candidate_id, email):
    conn = get_conn()
    conn.execute("UPDATE candidates SET email = ? WHERE id = ?", (email.strip() or None, candidate_id))
    conn.commit()
    conn.close()


def set_candidate_teams_link(candidate_id, round_key, link):
    if round_key not in ("l1", "l2", "l3"):
        raise ValueError(f"Unknown round: {round_key}")
    conn = get_conn()
    conn.execute(f"UPDATE candidates SET {round_key}_teams_link = ? WHERE id = ?", (link, candidate_id))
    conn.commit()
    conn.close()


def save_human_review(candidate_id, review_text):
    conn = get_conn()
    conn.execute("UPDATE candidates SET human_review = ? WHERE id = ?", (review_text.strip() or None, candidate_id))
    conn.commit()
    conn.close()


# A position can have at most one stakeholder per interview round - who
# actually conducts it isn't fixed to a role (HR/tech lead/CEO all just
# fill in whichever level they're doing), so this is freeform per position
# and editable any time, not just when the position is created.
STAKEHOLDER_LEVELS = ("l1", "l2", "l3")


def get_stakeholders(position_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT level, name, email FROM stakeholders WHERE position_id = ?", (position_id,)
    ).fetchall()
    conn.close()
    return {row["level"]: {"name": row["name"], "email": row["email"]} for row in rows}


def set_stakeholder(position_id, level, name, email):
    if level not in STAKEHOLDER_LEVELS:
        raise ValueError(f"Unknown level: {level}")
    conn = get_conn()
    conn.execute(
        """INSERT INTO stakeholders (position_id, level, name, email) VALUES (?, ?, ?, ?)
           ON CONFLICT(position_id, level) DO UPDATE SET name = excluded.name, email = excluded.email""",
        (position_id, level, name.strip(), email.strip()),
    )
    conn.commit()
    conn.close()


def delete_stakeholder(position_id, level):
    conn = get_conn()
    conn.execute("DELETE FROM stakeholders WHERE position_id = ? AND level = ?", (position_id, level))
    conn.commit()
    conn.close()


def update_user_email_account(user_id, email_address, email_app_password, email_smtp_host, email_smtp_port):
    conn = get_conn()
    if email_app_password:
        conn.execute(
            """UPDATE users SET email_address = ?, email_app_password = ?,
                   email_smtp_host = ?, email_smtp_port = ? WHERE id = ?""",
            (email_address, email_app_password, email_smtp_host, email_smtp_port, user_id),
        )
    else:
        # blank password field means "keep the existing one" - never overwrite with empty
        conn.execute(
            "UPDATE users SET email_address = ?, email_smtp_host = ?, email_smtp_port = ? WHERE id = ?",
            (email_address, email_smtp_host, email_smtp_port, user_id),
        )
    conn.commit()
    conn.close()


def log_email(candidate_id, email_type, round_key, recipient, subject, sent_by, status, error_message=None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO email_log (candidate_id, email_type, round, recipient, subject, sent_by, status, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (candidate_id, email_type, round_key, recipient, subject, sent_by, status, error_message),
    )
    conn.commit()
    conn.close()


def email_log_for_candidate(candidate_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM email_log WHERE candidate_id = ? ORDER BY sent_at DESC", (candidate_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_onboarding_checklist_items():
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = 'onboarding_checklist_items'").fetchone()
    conn.close()
    if row and row["value"]:
        return json.loads(row["value"])
    return list(DEFAULT_ONBOARDING_ITEMS)


def update_onboarding_checklist_items(items):
    items = [i.strip() for i in items if i.strip()]
    if not items:
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('onboarding_checklist_items', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(items),),
    )
    conn.commit()
    conn.close()


def ensure_onboarding_items(candidate_id):
    """Seeds a candidate's checklist from the current settings template the first
    time it's viewed - later template edits don't retroactively change checklists
    that already exist, since that would silently alter someone's in-progress list."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT COUNT(*) as n FROM onboarding_items WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()["n"]
    if existing == 0:
        for i, label in enumerate(get_onboarding_checklist_items()):
            conn.execute(
                "INSERT INTO onboarding_items (candidate_id, item_key, label, sort_order) VALUES (?, ?, ?, ?)",
                (candidate_id, f"item_{i}", label, i),
            )
        conn.commit()
    conn.close()


def get_onboarding_items(candidate_id):
    ensure_onboarding_items(candidate_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM onboarding_items WHERE candidate_id = ? ORDER BY sort_order", (candidate_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def toggle_onboarding_item(item_id, completed):
    conn = get_conn()
    if completed:
        conn.execute(
            "UPDATE onboarding_items SET completed = 1, completed_at = datetime('now') WHERE id = ?", (item_id,)
        )
    else:
        conn.execute("UPDATE onboarding_items SET completed = 0, completed_at = NULL WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def onboarding_progress(candidate_id):
    items = get_onboarding_items(candidate_id)
    done = sum(1 for i in items if i["completed"])
    return done, len(items)


def generate_offer_token():
    import secrets

    return secrets.token_urlsafe(32)


def set_offer_fields(candidate_id, designation, ctc, joining_date, notes, approver_email,
                      expiry_date=None, employment_type=None):
    conn = get_conn()
    row = conn.execute("SELECT offer_status FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    status = row["offer_status"] if row and row["offer_status"] else "draft"
    conn.execute(
        """UPDATE candidates SET offer_designation = ?, offer_ctc = ?, offer_joining_date = ?,
               offer_notes = ?, offer_approver_email = ?, offer_status = ?,
               offer_expiry_date = ?, offer_employment_type = ? WHERE id = ?""",
        (designation, ctc, joining_date, notes, approver_email, status,
         expiry_date or None, employment_type or None, candidate_id),
    )
    conn.commit()
    conn.close()


def log_offer_action(candidate_id, action, actor=None, note=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO offer_approval_log (candidate_id, action, actor, note) VALUES (?, ?, ?, ?)",
        (candidate_id, action, actor, note),
    )
    row = conn.execute("SELECT position_id, candidate_name FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    conn.commit()
    conn.close()
    if row:
        name = row["candidate_name"] or f"candidate #{candidate_id}"
        actor_label = actor or "the system"
        conn2 = get_conn()
        conn2.execute(
            """INSERT INTO audit_log (user_id, actor_label, action, detail, candidate_id, position_id)
               VALUES (NULL, ?, 'offer_action', ?, ?, ?)""",
            (actor_label, f"offer {action.replace('_', ' ')} for {name}", candidate_id, row["position_id"]),
        )
        conn2.commit()
        conn2.close()


def offer_log_for_candidate(candidate_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM offer_approval_log WHERE candidate_id = ? ORDER BY created_at DESC", (candidate_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def send_offer_for_approval(candidate_id, pdf_path, actor=None):
    token = generate_offer_token()
    conn = get_conn()
    conn.execute(
        """UPDATE candidates SET offer_status = 'pending_approval', offer_approval_token = ?,
               offer_pdf_path = ?, offer_sent_for_approval_at = datetime('now'),
               offer_rejection_reason = NULL WHERE id = ?""",
        (token, pdf_path, candidate_id),
    )
    conn.commit()
    conn.close()
    log_offer_action(candidate_id, "sent_for_approval", actor)
    return token


def get_candidate_by_offer_token(token):
    conn = get_conn()
    row = conn.execute("SELECT * FROM candidates WHERE offer_approval_token = ?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


def approve_offer(candidate_id, actor=None, note=None):
    conn = get_conn()
    conn.execute(
        "UPDATE candidates SET offer_status = 'approved', offer_approved_at = datetime('now') WHERE id = ?",
        (candidate_id,),
    )
    conn.commit()
    conn.close()
    log_offer_action(candidate_id, "approved", actor, note)


def reject_offer(candidate_id, reason, actor=None):
    conn = get_conn()
    conn.execute(
        "UPDATE candidates SET offer_status = 'needs_edit', offer_rejection_reason = ? WHERE id = ?",
        (reason, candidate_id),
    )
    conn.commit()
    conn.close()
    log_offer_action(candidate_id, "rejected", actor, reason)


def mark_offer_sent_to_candidate(candidate_id, actor=None):
    token = generate_offer_token()
    conn = get_conn()
    conn.execute(
        """UPDATE candidates SET offer_status = 'sent', offer_candidate_token = ?,
               offer_sent_to_candidate_at = datetime('now') WHERE id = ?""",
        (token, candidate_id),
    )
    conn.commit()
    conn.close()
    log_offer_action(candidate_id, "sent_to_candidate", actor)
    return token


def get_candidate_by_candidate_token(token):
    conn = get_conn()
    row = conn.execute("SELECT * FROM candidates WHERE offer_candidate_token = ?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


def record_candidate_offer_response(candidate_id, response):
    if response not in ("accepted", "declined"):
        raise ValueError(f"Unknown response: {response}")
    conn = get_conn()
    conn.execute(
        "UPDATE candidates SET offer_status = ?, offer_responded_at = datetime('now') WHERE id = ?",
        (response, candidate_id),
    )
    conn.commit()
    conn.close()
    log_offer_action(candidate_id, f"candidate_{response}", "candidate")


def bucket_for_score(score):
    if score is None:
        return "unscored"
    if score > 6:
        return "green"
    if score >= 4:
        return "yellow"
    return "red"


def create_position(title, jd_filename, jd_text):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO positions (title, jd_filename, jd_text) VALUES (?, ?, ?)",
        (title, jd_filename, jd_text),
    )
    conn.commit()
    position_id = cur.lastrowid
    conn.close()
    return position_id


def list_positions(position_ids=None):
    """position_ids: if given, restricts to just those positions - used to scope
    a non-admin assigned user's view down to their own assignment(s) rather
    than the whole candidate database. None (the default) means unrestricted,
    preserving prior behavior for admins."""
    conn = get_conn()
    if position_ids is not None:
        if not position_ids:
            conn.close()
            return []
        placeholders = ", ".join("?" for _ in position_ids)
        positions = conn.execute(
            f"SELECT * FROM positions WHERE id IN ({placeholders}) ORDER BY created_at DESC", position_ids
        ).fetchall()
    else:
        positions = conn.execute(
            "SELECT * FROM positions ORDER BY created_at DESC"
        ).fetchall()
    result = []
    for p in positions:
        counts = conn.execute(
            "SELECT bucket, COUNT(*) as n FROM candidates WHERE position_id = ? GROUP BY bucket",
            (p["id"],),
        ).fetchall()
        bucket_counts = {"green": 0, "yellow": 0, "red": 0, "unscored": 0}
        for c in counts:
            bucket_counts[c["bucket"]] = c["n"]
        total = sum(bucket_counts.values())

        progress = conn.execute(
            "SELECT SUM(reached) as reached, SUM(joined) as joined FROM candidates WHERE position_id = ?",
            (p["id"],),
        ).fetchone()
        computed = auto_status({"total": total, "reached": progress["reached"] or 0, "joined": progress["joined"] or 0})
        status = p["manual_status"] if p["manual_status"] in STATUSES else computed

        from datetime import datetime as _dt

        created = _parse_dt(p["created_at"]) or _dt.now()
        days_open = (_dt.now() - created).days

        result.append(
            {
                **dict(p),
                "bucket_counts": bucket_counts,
                "total": total,
                "hiring_status": status,
                "days_open": days_open,
                "deadline": deadline_status(dict(p), status == "closed"),
            }
        )
    conn.close()
    return result


def get_position(position_id):
    conn = get_conn()
    p = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    conn.close()
    return dict(p) if p else None


def add_candidate(position_id, resume_filename, resume_path, candidate_name, resume_text, watched_source_path=None):
    import resume_parser

    email = resume_parser.extract_email(resume_text or "")
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO candidates
           (position_id, resume_filename, resume_path, candidate_name, resume_text, bucket, created_at, email, watched_source_path)
           VALUES (?, ?, ?, ?, ?, 'unscored', datetime('now'), ?, ?)""",
        (position_id, resume_filename, resume_path, candidate_name, resume_text, email, watched_source_path),
    )
    candidate_id = cur.lastrowid
    _record_sourced_history(conn, candidate_id)
    conn.commit()
    conn.close()
    return candidate_id


def add_imported_candidate(position_id, row):
    """Adds one candidate from a bulk spreadsheet import (Feature: candidate
    spreadsheet import) - no resume file, just the contact/experience fields
    typed into the sheet. Deduped against existing candidates on this position
    by email (case-insensitive) first, falling back to an exact case-insensitive
    name match when no email was given - the usual "dedupe by
    email" import behavior. Returns 'added' or 'duplicate'; the caller is
    responsible for skipping nameless rows before calling this."""
    conn = get_conn()
    email = (row.get("email") or "").strip()
    name = (row.get("name") or "").strip()
    existing = None
    if email:
        existing = conn.execute(
            "SELECT id FROM candidates WHERE position_id = ? AND lower(email) = lower(?)",
            (position_id, email),
        ).fetchone()
    if not existing and name:
        existing = conn.execute(
            "SELECT id FROM candidates WHERE position_id = ? AND lower(candidate_name) = lower(?)",
            (position_id, name),
        ).fetchone()
    if existing:
        conn.close()
        return "duplicate"

    field_labels = [
        ("name", "Name"), ("phone", "Phone"), ("email", "Email"),
        ("experience", "Experience"), ("company", "Current Company"),
        ("role", "Current Role"), ("location", "Location"),
    ]
    lines = ["[Imported from spreadsheet - no resume file attached]", ""]
    for key, label in field_labels:
        value = (row.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    resume_text = "\n".join(lines)

    cur = conn.execute(
        """INSERT INTO candidates
           (position_id, resume_filename, resume_path, candidate_name, resume_text,
            bucket, created_at, email, imported_no_resume)
           VALUES (?, ?, ?, ?, ?, 'unscored', datetime('now'), ?, 1)""",
        (position_id, "(Imported - no resume file)", "", name, resume_text, email or None),
    )
    _record_sourced_history(conn, cur.lastrowid)
    conn.commit()
    conn.close()
    return "added"


def set_watch_folder(position_id, folder_path):
    conn = get_conn()
    conn.execute("UPDATE positions SET watch_folder = ? WHERE id = ?", (folder_path, position_id))
    conn.commit()
    conn.close()


def positions_with_watch_folder():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, watch_folder FROM positions WHERE watch_folder IS NOT NULL AND watch_folder != ''"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_watched_candidates(position_id):
    """Candidates on this position that came from a linked folder (i.e. have a
    watched_source_path) - used by the folder watcher to diff against the folder's
    current contents. Manually-uploaded candidates are never touched by the watcher."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, watched_source_path, source_missing FROM candidates "
        "WHERE position_id = ? AND watched_source_path IS NOT NULL",
        (position_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_source_missing(candidate_id, missing):
    conn = get_conn()
    conn.execute(
        "UPDATE candidates SET source_missing = ? WHERE id = ?", (1 if missing else 0, candidate_id)
    )
    conn.commit()
    conn.close()


def stop_tracking_watched_candidate(candidate_id):
    """Detach a candidate from its linked-folder source (keeps the candidate,
    just stops the watcher from ever flagging/tracking it again)."""
    conn = get_conn()
    conn.execute(
        "UPDATE candidates SET watched_source_path = NULL, source_missing = 0 WHERE id = ?",
        (candidate_id,),
    )
    conn.commit()
    conn.close()


# Default order of every resume/candidate list in the app (Resumes page, tracker,
# tracker export, reports, chatbot context): highest AI/JD score first, candidates
# with no score yet at the END, ties broken by the most recently added CV and then
# by name. Every list goes through list_candidates(), so this is the one place the
# default order lives; any manual column sorting/filtering a page offers is applied
# on top of it, client-side, and is unaffected.
CANDIDATE_DEFAULT_ORDER = "(score IS NULL), score DESC, created_at DESC, candidate_name COLLATE NOCASE, id"


def list_candidates(position_id, query=None):
    conn = get_conn()
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        needle = f"%{escaped}%"
        rows = conn.execute(
            f"""SELECT * FROM candidates
               WHERE position_id = ?
                 AND (candidate_name LIKE ? ESCAPE '\\' OR resume_text LIKE ? ESCAPE '\\')
               ORDER BY {CANDIDATE_DEFAULT_ORDER}""",
            (position_id, needle, needle),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM candidates WHERE position_id = ? ORDER BY {CANDIDATE_DEFAULT_ORDER}",
            (position_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_candidates_by_ids(candidate_ids):
    if not candidate_ids:
        return []
    conn = get_conn()
    placeholders = ", ".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"SELECT * FROM candidates WHERE id IN ({placeholders})", candidate_ids
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_candidates(candidate_ids):
    if not candidate_ids:
        return
    conn = get_conn()
    placeholders = ", ".join("?" for _ in candidate_ids)
    conn.execute(f"DELETE FROM candidates WHERE id IN ({placeholders})", candidate_ids)
    conn.commit()
    conn.close()


def _fk_children(conn, parent_table):
    """Every (child_table, child_column, column_is_required) that has a foreign key
    pointing at parent_table. Read from the live schema, so tables added later are
    covered without editing this function."""
    found = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        required = {r[1]: bool(r[3]) for r in conn.execute(f"PRAGMA table_info({table})")}
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            if fk[2] == parent_table:
                found.append((table, fk[3], required.get(fk[3], False)))
    return found


def delete_position(position_id):
    """Delete a position, its candidates and everything that hangs off them.

    The old version only deleted the candidates and the position, so any other
    row pointing at them (email log, interview rounds, round status, ratings,
    notes ...) made SQLite refuse with 'FOREIGN KEY constraint failed'.
    Rows that cannot exist without the candidate/position are deleted; rows
    that merely mention them (audit log, notifications, capture log) keep their
    history and just lose the link. All of it happens in one transaction, so a
    failure leaves the data exactly as it was."""
    conn = get_conn()
    try:
        candidate_ids = [r[0] for r in conn.execute(
            "SELECT id FROM candidates WHERE position_id = ?", (position_id,))]
        def clear(child, column, required, ids):
            marks = ", ".join("?" for _ in ids)
            if required or child == "tasks":     # a position's tasks are meaningless without it
                conn.execute(f"DELETE FROM {child} WHERE {column} IN ({marks})", ids)
            else:
                conn.execute(f"UPDATE {child} SET {column} = NULL WHERE {column} IN ({marks})", ids)

        # 1. rows that point at this position's candidates
        if candidate_ids:
            for child, column, required in _fk_children(conn, "candidates"):
                if child != "candidates":
                    clear(child, column, required, candidate_ids)
        # 2. rows that point at the position itself (rounds, stakeholders, tasks ...)
        for child, column, required in _fk_children(conn, "positions"):
            if child != "candidates":
                clear(child, column, required, [position_id])
        # 3. the candidates, then the position
        conn.execute("DELETE FROM candidates WHERE position_id = ?", (position_id,))
        conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def daily_activity_counts(days=365, position_ids=None):
    """Combined per-day count of hiring activity - stage changes, Quick
    Capture notes, emails sent, and new candidates added - across the whole
    system (or, if position_ids is given, scoped to just those positions'
    candidates - a non-admin assigned user's heatmap)."""
    if position_ids is not None and not position_ids:
        return {}
    conn = get_conn()
    if position_ids is not None:
        placeholders = ", ".join("?" for _ in position_ids)
        pos_filter = f"AND c.position_id IN ({placeholders})"
        params = (
            list(position_ids) * 3
            + list(position_ids)
            + [f"-{days} days"]
        )
        rows = conn.execute(
            f"""
            SELECT date(d) AS day, COUNT(*) AS n FROM (
                SELECT se.changed_at AS d FROM stage_events se JOIN candidates c ON c.id = se.candidate_id WHERE 1=1 {pos_filter}
                UNION ALL
                SELECT cn.created_at AS d FROM candidate_notes cn JOIN candidates c ON c.id = cn.candidate_id WHERE 1=1 {pos_filter}
                UNION ALL
                SELECT el.sent_at AS d FROM email_log el JOIN candidates c ON c.id = el.candidate_id WHERE 1=1 {pos_filter}
                UNION ALL
                SELECT c.created_at AS d FROM candidates c WHERE 1=1 {pos_filter}
            )
            WHERE d >= date('now', ?)
            GROUP BY day
            """,
            params,
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT date(d) AS day, COUNT(*) AS n FROM (
                SELECT changed_at AS d FROM stage_events
                UNION ALL
                SELECT created_at AS d FROM candidate_notes
                UNION ALL
                SELECT sent_at AS d FROM email_log
                UNION ALL
                SELECT created_at AS d FROM candidates
            )
            WHERE d >= date('now', ?)
            GROUP BY day
            """,
            (f"-{days} days",),
        ).fetchall()
    conn.close()
    return {r["day"]: r["n"] for r in rows}


def get_candidate(candidate_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---- Quick Capture: free-text notes filed against a candidate ----------------

def all_candidates_for_matching():
    """Every named candidate across all positions, for fuzzy-matching a Quick
    Capture sentence against - deliberately not scoped to one position since
    the capture box is opened globally, from any page."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT candidates.id, candidates.candidate_name, positions.title AS position_title
           FROM candidates JOIN positions ON positions.id = candidates.position_id
           WHERE candidates.candidate_name IS NOT NULL AND candidates.candidate_name != ''"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_candidate_note(candidate_id, text, noted_at=None, actor_user_id=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO candidate_notes (candidate_id, text, noted_at) VALUES (?, ?, ?)",
        (candidate_id, text, noted_at),
    )
    row = conn.execute("SELECT position_id, candidate_name FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    conn.commit()
    conn.close()
    if row:
        name = row["candidate_name"] or f"candidate #{candidate_id}"
        _log_audit_internal(
            actor_user_id, "note_added", f"added a note for {name}",
            candidate_id=candidate_id, position_id=row["position_id"],
        )


def get_candidate_notes(candidate_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM candidate_notes WHERE candidate_id = ? ORDER BY created_at DESC",
        (candidate_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Input Logs: a personal, read-only record of everything typed into
# Quick Capture and what it did - including attempts that didn't go through,
# so "whatever I did today" is genuinely complete, not just successful saves.

def log_capture(user_id, raw_text, matched_label=None, action_summary="", status="saved", candidate_id=None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO capture_logs (user_id, raw_text, matched_label, candidate_id, action_summary, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, raw_text, matched_label, candidate_id, action_summary, status),
    )
    conn.commit()
    conn.close()


def list_capture_logs(user_id):
    """Newest-first, left-joined to candidates so the template can link a
    matched entry straight to that candidate's page - candidates.position_id
    is needed for that URL and isn't on capture_logs itself. The join stays
    left so a log entry still shows (just without a clickable chip) if the
    candidate was since deleted."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT capture_logs.*, candidates.position_id AS candidate_position_id
           FROM capture_logs
           LEFT JOIN candidates ON candidates.id = capture_logs.candidate_id
           WHERE capture_logs.user_id = ?
           ORDER BY capture_logs.created_at DESC""",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def unscored_candidates(position_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM candidates WHERE position_id = ? AND bucket = 'unscored'",
        (position_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_stage(candidate_id, field, value, actor_user_id=None):
    if field not in ALL_STAGE_COLUMNS:
        raise ValueError(f"Unknown stage field: {field}")
    conn = get_conn()
    stage_before = _stage_before_change(conn, candidate_id)
    conn.execute(
        f"UPDATE candidates SET {field} = ? WHERE id = ?",
        (1 if value else 0, candidate_id),
    )
    conn.execute(
        "INSERT INTO stage_events (candidate_id, field, value, changed_at) VALUES (?, ?, ?, datetime('now'))",
        (candidate_id, field, 1 if value else 0),
    )
    _record_stage_change(conn, candidate_id, stage_before, actor_user_id)
    row = conn.execute("SELECT position_id, candidate_name FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    conn.commit()
    conn.close()
    if row:
        label = _stage_field_label(field)
        name = row["candidate_name"] or f"candidate #{candidate_id}"
        verb = "set" if value else "cleared"
        _log_audit_internal(
            actor_user_id, "stage_change",
            f"{verb} {label} for {name}",
            candidate_id=candidate_id, position_id=row["position_id"],
        )


def position_kpis(position_id):
    conn = get_conn()
    row = conn.execute(
        """SELECT
               COUNT(*) as total,
               SUM(reached) as reached,
               SUM(l1_scheduled) as l1_scheduled,
               SUM(l1_attended) as l1_attended,
               SUM(l1_cleared) as l1_cleared,
               SUM(l2_scheduled) as l2_scheduled,
               SUM(l2_attended) as l2_attended,
               SUM(l2_cleared) as l2_cleared,
               SUM(l3_scheduled) as l3_scheduled,
               SUM(l3_attended) as l3_attended,
               SUM(l3_cleared) as l3_cleared,
               SUM(offer_made) as offer_made,
               SUM(joined) as joined,
               SUM(rejected) as rejected,
               SUM(on_hold) as on_hold
           FROM candidates WHERE position_id = ?""",
        (position_id,),
    ).fetchone()
    conn.close()
    kpis = dict(row)
    return {k: (v or 0) for k, v in kpis.items()}


_SUM_COLUMNS = [f[0] for f in STAGE_FIELDS] + [f[0] for f in STATUS_FIELDS]


STATUSES = ("active", "closed", "upcoming")
STATUS_LABELS = {"active": "Ongoing", "closed": "Closed", "upcoming": "Upcoming"}


def auto_status(row):
    """active / closed / upcoming, auto-derived from a funnel row's counts."""
    if row["total"] == 0:
        return "upcoming"
    if row["joined"] > 0:
        return "closed"
    if row["reached"] > 0:
        return "active"
    return "upcoming"


def _finalize_funnel_row(d, manual_status=None):
    d["selection_rate"] = round(d["joined"] / d["total"] * 100, 1) if d["total"] else 0.0
    d["auto_status"] = auto_status(d)
    d["status"] = manual_status if manual_status in STATUSES else d["auto_status"]
    d["is_manual_status"] = manual_status in STATUSES
    return d


def status_counts(funnel_rows):
    counts = {"active": 0, "closed": 0, "upcoming": 0}
    for row in funnel_rows:
        counts[row["status"]] += 1
    counts["total_positions"] = len(funnel_rows)
    return counts


def set_manual_status(position_id, status):
    if status is not None and status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    conn = get_conn()
    conn.execute(
        "UPDATE positions SET manual_status = ?, status_changed_at = datetime('now') WHERE id = ?",
        (status, position_id),
    )
    conn.commit()
    conn.close()


def set_position_dates(position_id, start_date, target_end_date):
    conn = get_conn()
    conn.execute(
        "UPDATE positions SET start_date = ?, target_end_date = ? WHERE id = ?",
        (start_date or None, target_end_date or None, position_id),
    )
    conn.commit()
    conn.close()


def set_uses_dynamic_rounds(position_id, value):
    conn = get_conn()
    conn.execute(
        "UPDATE positions SET uses_dynamic_rounds = ? WHERE id = ?", (1 if value else 0, position_id)
    )
    conn.commit()
    conn.close()


def deadline_status(position, is_closed):
    """None if there's nothing to warn about, else {'level': 'danger'|'warning', 'message': ...}.
    'danger' = past the target end date and still not filled. 'warning' = 3 days or fewer left."""
    target = position.get("target_end_date")
    if not target or is_closed:
        return None
    from datetime import date as _date

    target_date = _date.fromisoformat(target[:10])
    days_left = (target_date - _date.today()).days
    if days_left < 0:
        return {"level": "danger", "message": f"Deadline crossed by {-days_left} day{'s' if days_left != -1 else ''}"}
    if days_left <= 3:
        return {"level": "warning", "message": f"{days_left} day{'s' if days_left != 1 else ''} left to hire"}
    return None


def funnel_by_position(position_ids=None):
    """Per-position funnel breakdown, same shape as the CEO Dashboard's 'Full
    Funnel by Role' table. position_ids scopes this to a subset (a non-admin
    assigned user's positions) - None (default) is unrestricted, for admins."""
    if position_ids is not None and not position_ids:
        return []
    conn = get_conn()
    sum_cols = ", ".join(f"SUM(c.{col}) as {col}" for col in _SUM_COLUMNS)
    where = ""
    params = []
    if position_ids is not None:
        placeholders = ", ".join("?" for _ in position_ids)
        where = f"WHERE p.id IN ({placeholders})"
        params = list(position_ids)
    rows = conn.execute(
        f"""SELECT p.id as position_id, p.title as title, p.manual_status as manual_status,
                   p.target_end_date as target_end_date,
                   COUNT(c.id) as total, {sum_cols}
            FROM positions p
            LEFT JOIN candidates c ON c.position_id = p.id
            {where}
            GROUP BY p.id
            ORDER BY p.created_at DESC""",
        params,
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        manual_status = d.pop("manual_status")
        target_end_date = d.pop("target_end_date")
        for col in _SUM_COLUMNS:
            d[col] = d[col] or 0
        row = _finalize_funnel_row(d, manual_status)
        row.update(position_timing(d["position_id"]))
        row["deadline"] = deadline_status({"target_end_date": target_end_date}, row["status"] == "closed")
        result.append(row)
    return result


def funnel_for_position(position_id):
    """Same shape as one funnel_by_position() row, scoped to a single position (for the JD panel snapshot)."""
    position = get_position(position_id)
    d = position_kpis(position_id)
    d["position_id"] = position_id
    row = _finalize_funnel_row(d, position["manual_status"])
    row.update(position_timing(position_id))
    row["deadline"] = deadline_status(position, row["status"] == "closed")
    return row


def overall_kpis(position_ids=None):
    """Aggregate funnel counts across every position (or, if position_ids is
    given, just those - scoping a non-admin assigned user's dashboard down to
    their own assignment(s))."""
    if position_ids is not None and not position_ids:
        zeroed = {col: 0 for col in _SUM_COLUMNS}
        zeroed.update({"total": 0, "awaiting_l2": 0, "awaiting_l3": 0, "awaiting_offer": 0, "selection_rate": 0.0, "offer_rate": 0.0})
        return zeroed
    conn = get_conn()
    sum_cols = ", ".join(f"SUM({col}) as {col}" for col in _SUM_COLUMNS)
    where = ""
    params = []
    if position_ids is not None:
        placeholders = ", ".join("?" for _ in position_ids)
        where = f"WHERE position_id IN ({placeholders})"
        params = list(position_ids)
    totals_row = conn.execute(f"SELECT COUNT(*) as total, {sum_cols} FROM candidates {where}", params).fetchone()
    awaiting_row = conn.execute(
        f"""SELECT
               SUM(CASE WHEN l1_cleared = 1 AND l2_scheduled = 0 THEN 1 ELSE 0 END) as awaiting_l2,
               SUM(CASE WHEN l2_cleared = 1 AND l3_scheduled = 0 THEN 1 ELSE 0 END) as awaiting_l3,
               SUM(CASE WHEN l3_cleared = 1 AND offer_made = 0 THEN 1 ELSE 0 END) as awaiting_offer
           FROM candidates {where}""",
        params,
    ).fetchone()
    conn.close()

    result = {k: (v or 0) for k, v in dict(totals_row).items()}
    result.update({k: (v or 0) for k, v in dict(awaiting_row).items()})
    total = result["total"]
    result["selection_rate"] = round(result["joined"] / total * 100, 1) if total else 0.0
    result["offer_rate"] = round(result["offer_made"] / total * 100, 1) if total else 0.0
    return result


def current_stage(c):
    """(label, field) for the most-advanced true stage - field is None for
    'Sourced' (no boolean column represents it; created_at is the anchor date)."""
    if c["rejected"]:
        return "Rejected", "rejected"
    if c["joined"]:
        return "Joined", "joined"
    if c["offer_made"]:
        return "Offer Made", "offer_made"
    for cleared, attended, scheduled, level in (
        (c["l3_cleared"], c["l3_attended"], c["l3_scheduled"], "L3"),
        (c["l2_cleared"], c["l2_attended"], c["l2_scheduled"], "L2"),
        (c["l1_cleared"], c["l1_attended"], c["l1_scheduled"], "L1"),
    ):
        if cleared:
            return f"Cleared {level}", f"{level.lower()}_cleared"
        if attended:
            return f"{level} Attended", f"{level.lower()}_attended"
        if scheduled:
            return f"{level} Scheduled", f"{level.lower()}_scheduled"
    if c["reached"]:
        return "Reached", "reached"
    return "Sourced", None


def current_stage_label(c):
    """Single human-readable stage, most-advanced-first, for report tables."""
    return current_stage(c)[0]


def _parse_dt(value):
    if not value:
        return None
    from datetime import datetime as _dt

    return _dt.fromisoformat(value)


def stage_entered_at_map(position_id):
    """{(candidate_id, field): last timestamp the field was set to 1} for a whole position -
    one query instead of one per candidate."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT se.candidate_id, se.field, MAX(se.changed_at) as changed_at
           FROM stage_events se
           JOIN candidates c ON c.id = se.candidate_id
           WHERE c.position_id = ? AND se.value = 1
           GROUP BY se.candidate_id, se.field""",
        (position_id,),
    ).fetchall()
    conn.close()
    return {(r["candidate_id"], r["field"]): r["changed_at"] for r in rows}


def candidate_timing(c, stage_map=None):
    """days_since_sourced, current stage label, and days_in_stage (+ days_to_hire if joined)."""
    from datetime import datetime as _dt

    now = _dt.now()
    created = _parse_dt(c["created_at"]) or now
    days_since_sourced = (now - created).days

    label, field = current_stage(c)
    stage_entered = None
    if field is not None:
        if stage_map is not None:
            stage_entered = _parse_dt(stage_map.get((c["id"], field)))
        else:
            stage_entered = _parse_dt(get_stage_entered_at(c["id"], field))
    stage_started = stage_entered or created
    days_in_stage = (now - stage_started).days

    days_to_hire = None
    if c["joined"]:
        joined_at = None
        if field == "joined":
            joined_at = stage_entered
        elif stage_map is not None:
            joined_at = _parse_dt(stage_map.get((c["id"], "joined")))
        else:
            joined_at = _parse_dt(get_stage_entered_at(c["id"], "joined"))
        if joined_at:
            days_to_hire = (joined_at - created).days

    return {
        "days_since_sourced": days_since_sourced,
        "stage_label": label,
        "days_in_stage": days_in_stage,
        "days_to_hire": days_to_hire,
    }


def get_stage_entered_at(candidate_id, field):
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(changed_at) as changed_at FROM stage_events WHERE candidate_id = ? AND field = ? AND value = 1",
        (candidate_id, field),
    ).fetchone()
    conn.close()
    return row["changed_at"] if row else None


def position_timing(position_id):
    """days_open since the position was created, and days_to_fill once closed."""
    from datetime import datetime as _dt

    position = get_position(position_id)
    now = _dt.now()
    created = _parse_dt(position["created_at"]) or now
    days_open = (now - created).days

    days_to_fill = None
    conn = get_conn()
    progress = conn.execute(
        "SELECT COUNT(*) as total, SUM(reached) as reached, SUM(joined) as joined FROM candidates WHERE position_id = ?",
        (position_id,),
    ).fetchone()
    conn.close()
    computed_status = auto_status(
        {"total": progress["total"], "reached": progress["reached"] or 0, "joined": progress["joined"] or 0}
    )
    status = position["manual_status"] if position["manual_status"] in STATUSES else computed_status
    if status == "closed":
        closed_at = _parse_dt(position["status_changed_at"])
        if not closed_at:
            conn = get_conn()
            r = conn.execute(
                """SELECT MIN(se.changed_at) as changed_at FROM stage_events se
                   JOIN candidates c ON c.id = se.candidate_id
                   WHERE c.position_id = ? AND se.field = 'joined' AND se.value = 1""",
                (position_id,),
            ).fetchone()
            conn.close()
            closed_at = _parse_dt(r["changed_at"]) if r else None
        if closed_at:
            days_to_fill = (closed_at - created).days

    return {"days_open": days_open, "days_to_fill": days_to_fill}


def candidate_stage_timeline(c, stage_map):
    """Per-STAGE_FIELD day count, in column order:
       - done: frozen day count between the previous stage's timestamp and this one
       - pending: live day count from the previous stage's timestamp to now (the one
         column the candidate is currently sitting at)
       - stopped: same as pending but frozen at the rejected/on_hold timestamp -
         once a candidate is rejected or put on hold, no column keeps counting
       - future: not reached yet - blank
    """
    now = _dt_now()
    created = _parse_dt(c["created_at"]) or now

    terminal_time = None
    if c["rejected"]:
        terminal_time = _parse_dt(stage_map.get((c["id"], "rejected"))) or now
    elif c["on_hold"]:
        terminal_time = _parse_dt(stage_map.get((c["id"], "on_hold"))) or now

    prev_time = created
    result = {}
    active_assigned = False
    for field, _label, _group in STAGE_FIELDS:
        if c[field]:
            checked_at = _parse_dt(stage_map.get((c["id"], field))) or prev_time
            days = max((checked_at - prev_time).days, 0)
            result[field] = {"state": "done", "days": days}
            prev_time = checked_at
        elif not active_assigned:
            active_assigned = True
            end_time = terminal_time or now
            days = max((end_time - prev_time).days, 0)
            result[field] = {"state": "stopped" if terminal_time else "pending", "days": days}
        else:
            result[field] = {"state": "future", "days": None}
    return result


def _dt_now():
    from datetime import datetime as _dt

    return _dt.now()


def avg_days_to_hire(position_id=None, position_ids=None):
    """Average days from sourced to joined. Scoped to one position (position_id)
    or a list of positions (position_ids, e.g. a non-admin's assignments), else
    company-wide. None if nobody's joined (or, with an empty position_ids list,
    trivially None - nothing to average)."""
    if position_ids is not None and not position_ids:
        return None
    conn = get_conn()
    if position_id:
        rows = conn.execute("SELECT * FROM candidates WHERE position_id = ? AND joined = 1", (position_id,)).fetchall()
    elif position_ids is not None:
        placeholders = ", ".join("?" for _ in position_ids)
        rows = conn.execute(
            f"SELECT * FROM candidates WHERE position_id IN ({placeholders}) AND joined = 1", position_ids
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM candidates WHERE joined = 1").fetchall()
    conn.close()
    days_list = []
    for row in rows:
        c = dict(row)
        timing = candidate_timing(c)
        if timing["days_to_hire"] is not None:
            days_list.append(timing["days_to_hire"])
    return round(sum(days_list) / len(days_list), 1) if days_list else None


def days_to_hire_by_position(position_ids=None):
    """Per-position average days-to-hire (sourced -> joined), same underlying
    calculation as avg_days_to_hire() but broken down by role instead of one
    company-wide number - the "Time-to-hire" graph, a common recruiting
    dashboard widget. Only
    positions with at least one joined candidate are included, sorted worst
    (slowest) first since that's the more actionable ordering for a dashboard."""
    if position_ids is not None and not position_ids:
        return []
    conn = get_conn()
    where = "WHERE joined = 1"
    params = []
    if position_ids is not None:
        placeholders = ", ".join("?" for _ in position_ids)
        where += f" AND position_id IN ({placeholders})"
        params = list(position_ids)
    rows = conn.execute(f"SELECT * FROM candidates {where}", params).fetchall()
    conn.close()

    by_position = {}
    for row in rows:
        c = dict(row)
        timing = candidate_timing(c)
        if timing["days_to_hire"] is None:
            continue
        by_position.setdefault(c["position_id"], []).append(timing["days_to_hire"])

    result = []
    for pos_id, days_list in by_position.items():
        position = get_position(pos_id)
        result.append({
            "position_id": pos_id,
            "title": position["title"] if position else f"Position #{pos_id}",
            "avg_days": round(sum(days_list) / len(days_list), 1),
        })
    result.sort(key=lambda r: r["avg_days"], reverse=True)
    return result


def offer_acceptance_stats(position_ids=None):
    """Accepted vs. declined offer counts + acceptance rate - the "Offer
    Acceptance Rate" dashboard widget. Offers still pending a candidate response (sent/approved/
    etc.) are excluded from the rate on purpose - only a settled outcome counts,
    same reasoning as offer_rate elsewhere only counting made offers."""
    if position_ids is not None and not position_ids:
        return {"accepted": 0, "declined": 0, "rate": None}
    conn = get_conn()
    where = "WHERE offer_status IN ('accepted', 'declined')"
    params = []
    if position_ids is not None:
        placeholders = ", ".join("?" for _ in position_ids)
        where += f" AND position_id IN ({placeholders})"
        params = list(position_ids)
    row = conn.execute(
        f"""SELECT
               SUM(CASE WHEN offer_status = 'accepted' THEN 1 ELSE 0 END) as accepted,
               SUM(CASE WHEN offer_status = 'declined' THEN 1 ELSE 0 END) as declined
            FROM candidates {where}""",
        params,
    ).fetchone()
    conn.close()
    accepted = row["accepted"] or 0
    declined = row["declined"] or 0
    total = accepted + declined
    rate = round(accepted / total * 100, 1) if total else None
    return {"accepted": accepted, "declined": declined, "rate": rate}


# ---- Personal tasks (Open Tasks / Completed Tasks dashboard widgets) ----------

def list_open_tasks(user_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.*, p.title as position_title
           FROM tasks t LEFT JOIN positions p ON p.id = t.position_id
           WHERE t.assigned_to_user_id = ? AND t.status = 'open'
           ORDER BY (t.due_date IS NULL), t.due_date, t.created_at""",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_completed_tasks(user_id, limit=10):
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.*, p.title as position_title
           FROM tasks t LEFT JOIN positions p ON p.id = t.position_id
           WHERE t.assigned_to_user_id = ? AND t.status = 'completed'
           ORDER BY t.completed_at DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_task(title, due_date, assigned_to_user_id, created_by, position_id=None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO tasks (title, due_date, position_id, assigned_to_user_id, created_by)
           VALUES (?, ?, ?, ?, ?)""",
        (title.strip(), (due_date or "").strip() or None, position_id, assigned_to_user_id, created_by),
    )
    conn.commit()
    conn.close()


def get_task(task_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def complete_task(task_id):
    conn = get_conn()
    conn.execute(
        "UPDATE tasks SET status = 'completed', completed_at = datetime('now') WHERE id = ?", (task_id,)
    )
    conn.commit()
    conn.close()


def delete_task(task_id):
    conn = get_conn()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()


# ---- Notifications ---------------------------------------------------------

def create_notification(user_id, title, message, position_id=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO notifications (user_id, title, message, position_id) VALUES (?, ?, ?, ?)",
        (user_id, title, message, position_id),
    )
    conn.commit()
    conn.close()


def list_notifications(user_id, limit=20):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def unread_notification_count(user_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as n FROM notifications WHERE user_id = ? AND is_read = 0", (user_id,)
    ).fetchone()
    conn.close()
    return row["n"]


def mark_notification_read(notification_id, user_id):
    conn = get_conn()
    conn.execute(
        "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (notification_id, user_id)
    )
    conn.commit()
    conn.close()


def mark_all_notifications_read(user_id):
    conn = get_conn()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (user_id,))
    conn.commit()
    conn.close()


# ---- Dynamic interview rounds (Feature added 2026-09-03) -------------------
# Applies only to positions with uses_dynamic_rounds = 1 (new positions going
# forward) - see the SCHEMA comment above for why existing positions are
# deliberately left on the legacy fixed L1/L2/L3 columns.

def create_interview_rounds(position_id, round_labels):
    """round_labels is an ordered list of free-text round names, e.g.
    ['Technical Screen', 'Manager Round', 'HR Round'] - any length, any
    wording, not fixed to L1/L2/L3. Interviewer assignment happens later,
    from the position page, once Assigned Team members actually exist."""
    conn = get_conn()
    for i, label in enumerate(round_labels, start=1):
        label = label.strip()
        if not label:
            continue
        conn.execute(
            "INSERT INTO interview_rounds (position_id, round_number, round_label) VALUES (?, ?, ?)",
            (position_id, i, label),
        )
    conn.commit()
    conn.close()


def get_interview_rounds(position_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT ir.*, u.username AS assigned_username, u.display_name AS assigned_display_name
           FROM interview_rounds ir LEFT JOIN users u ON u.id = ir.assigned_user_id
           WHERE ir.position_id = ? ORDER BY ir.round_number""",
        (position_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_interview_round(round_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM interview_rounds WHERE id = ?", (round_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def assign_round_interviewer(round_id, user_id):
    """Sets who conducts this round and fires the in-app notification that
    was the whole point of this feature - the assignee finds out immediately,
    without HR having to separately message them."""
    conn = get_conn()
    conn.execute("UPDATE interview_rounds SET assigned_user_id = ? WHERE id = ?", (user_id, round_id))
    conn.commit()
    conn.close()

    round_row = get_interview_round(round_id)
    position = get_position(round_row["position_id"]) if round_row else None
    if round_row and position:
        create_notification(
            user_id,
            "Interview round assigned",
            f"You've been assigned to conduct {round_row['round_label']} for {position['title']}.",
            position_id=position["id"],
        )


def get_candidate_round_statuses(candidate_id):
    """Dict keyed by round_id -> status row, for however many rounds this
    candidate's position actually has."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM candidate_round_status WHERE candidate_id = ?", (candidate_id,)
    ).fetchall()
    conn.close()
    return {r["round_id"]: dict(r) for r in rows}


def set_candidate_round_status(candidate_id, round_id, field, actor_user_id=None):
    """Toggles one boolean field (scheduled/attended/cleared) for this
    candidate+round, upserting the row if it doesn't exist yet - mirrors the
    legacy toggle_activity_status()-style idiom used elsewhere in this app.
    A change of the candidate's current stage is also written to
    candidate_stage_history (with who made it, when known)."""
    if field not in ("scheduled", "attended", "cleared"):
        raise ValueError(f"Unknown field: {field}")
    conn = get_conn()
    stage_before = _stage_before_change(conn, candidate_id)
    row = conn.execute(
        "SELECT * FROM candidate_round_status WHERE candidate_id = ? AND round_id = ?",
        (candidate_id, round_id),
    ).fetchone()
    if row is None:
        conn.execute(
            f"INSERT INTO candidate_round_status (candidate_id, round_id, {field}, {field}_at) "
            f"VALUES (?, ?, 1, datetime('now'))",
            (candidate_id, round_id),
        )
    else:
        new_value = 0 if row[field] else 1
        timestamp = "datetime('now')" if new_value else "NULL"
        conn.execute(
            f"UPDATE candidate_round_status SET {field} = ?, {field}_at = {timestamp} "
            f"WHERE candidate_id = ? AND round_id = ?",
            (new_value, candidate_id, round_id),
        )
    _record_stage_change(conn, candidate_id, stage_before, actor_user_id)
    conn.commit()
    conn.close()


def set_candidate_round_teams_link(candidate_id, round_id, link):
    """Upserts just the Teams link for this candidate+round, independent of
    the scheduled/attended/cleared flags - mirrors set_candidate_teams_link()
    used by the legacy fixed L1/L2/L3 tracker."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM candidate_round_status WHERE candidate_id = ? AND round_id = ?",
        (candidate_id, round_id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO candidate_round_status (candidate_id, round_id, teams_link) VALUES (?, ?, ?)",
            (candidate_id, round_id, link),
        )
    else:
        conn.execute(
            "UPDATE candidate_round_status SET teams_link = ? WHERE candidate_id = ? AND round_id = ?",
            (link, candidate_id, round_id),
        )
    conn.commit()
    conn.close()


def position_round_statuses(position_id):
    """All candidates' round statuses for a dynamic-rounds position in one
    query, keyed candidate_id -> round_id -> status row - the dynamic-rounds
    equivalent of the per-candidate timeline used by the fixed tracker."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT crs.* FROM candidate_round_status crs
           JOIN candidates c ON c.id = crs.candidate_id
           WHERE c.position_id = ?""",
        (position_id,),
    ).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["candidate_id"], {})[r["round_id"]] = dict(r)
    return out


CALENDAR_MILESTONES = ["sourced", "reached", "l1_cleared", "l2_cleared", "l3_cleared", "offer_made", "joined", "rejected", "on_hold"]
CALENDAR_MILESTONE_LABELS = {
    "sourced": "Sourced",
    "reached": "Reached",
    "l1_cleared": "L1 Cleared",
    "l2_cleared": "L2 Cleared",
    "l3_cleared": "L3 Cleared",
    "offer_made": "Offer Made",
    "joined": "Joined",
    "rejected": "Rejected",
    "on_hold": "On Hold",
}


def _position_scope_clause(position_id, column, params_out):
    """Shared helper for the calendar queries below: position_id can be None (no
    restriction), a single int (one position's calendar page), or a list (a
    non-admin's assigned positions, scoping the overall/company-wide calendar
    view down to just what they're allowed to see). Appends any needed params
    to params_out and returns the SQL clause to splice in."""
    if position_id is None:
        return ""
    if isinstance(position_id, (list, tuple, set)):
        ids = list(position_id)
        if not ids:
            params_out.append(-1)  # no positions allowed - clause that matches nothing
            return f"AND {column} = ?"
        placeholders = ", ".join("?" for _ in ids)
        params_out.extend(ids)
        return f"AND {column} IN ({placeholders})"
    params_out.append(position_id)
    return f"AND {column} = ?"


def calendar_active_dates(position_id, year, month):
    """Dates within one month that have any activity - used to mark the calendar
    grid. position_id: None, a single id, or a list (see _position_scope_clause)."""
    conn = get_conn()
    prefix = f"{year:04d}-{month:02d}"
    params1 = [prefix + "%"]
    where = _position_scope_clause(position_id, "c.position_id", params1)
    events = conn.execute(
        f"""SELECT DISTINCT date(se.changed_at) as d FROM stage_events se
            JOIN candidates c ON c.id = se.candidate_id
            WHERE date(se.changed_at) LIKE ? {where}""",
        params1,
    ).fetchall()
    params2 = [prefix + "%"]
    where2 = _position_scope_clause(position_id, "position_id", params2)
    sourced = conn.execute(
        f"SELECT DISTINCT date(created_at) as d FROM candidates WHERE date(created_at) LIKE ? {where2}", params2
    ).fetchall()
    conn.close()
    return sorted({r["d"] for r in events} | {r["d"] for r in sourced})


def calendar_day_summary(position_id, date_str):
    """Net counts per milestone for one calendar day. Each candidate counts once per
    milestone regardless of how many times a box was ticked/unticked that day - only
    the LAST event of the day for that candidate+field decides whether it counts.
    position_id: None, a single id, or a list (see _position_scope_clause)."""
    conn = get_conn()
    params1 = [date_str]
    where = _position_scope_clause(position_id, "c.position_id", params1)
    rows = conn.execute(
        f"""SELECT se.candidate_id, se.field, se.value, se.changed_at
            FROM stage_events se JOIN candidates c ON c.id = se.candidate_id
            WHERE date(se.changed_at) = ? {where}
            ORDER BY se.changed_at""",
        params1,
    ).fetchall()
    params2 = [date_str]
    where2 = _position_scope_clause(position_id, "position_id", params2)
    sourced_count = conn.execute(
        f"SELECT COUNT(*) as n FROM candidates WHERE date(created_at) = ? {where2}", params2
    ).fetchone()["n"]
    conn.close()

    last_value = {}
    for r in rows:
        last_value[(r["candidate_id"], r["field"])] = r["value"]

    counts = {m: 0 for m in CALENDAR_MILESTONES}
    counts["sourced"] = sourced_count
    for (_cand_id, field), value in last_value.items():
        if field in counts and value == 1:
            counts[field] += 1
    return counts


def save_score(candidate_id, score, fit_summary, positives, negatives, breakdown=None):
    """Stores a scoring result. breakdown (optional) is the validated list of
    {name, weight, score, evidence} the overall score was computed from; it is
    saved with the score so the popup always adds up. Re-scoring without a breakdown
    clears any older one - it would no longer match the new overall score."""
    bucket = bucket_for_score(score)
    conn = get_conn()
    conn.execute(
        """UPDATE candidates
           SET score = ?, fit_summary = ?, positives_json = ?, negatives_json = ?,
               bucket = ?, scored_at = datetime('now'), score_breakdown_json = ?
           WHERE id = ?""",
        (score, fit_summary, json.dumps(positives), json.dumps(negatives), bucket,
         json.dumps(breakdown) if breakdown else None, candidate_id),
    )
    conn.commit()
    conn.close()


# ---- Screening assessments (AI-generated JD questions + recorded answers) ----

def get_assessment_questions(position_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM assessment_questions WHERE position_id = ? ORDER BY sort_order", (position_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def replace_assessment_questions(position_id, questions):
    """Replaces this position's question set with a new (recruiter-reviewed)
    list. Also clears any answers/results tied to the old questions, since
    they'd otherwise dangle against questions that no longer exist - this is
    a v1 tradeoff (regenerating starts the assessment over for this position),
    called out to the recruiter in the UI before they confirm."""
    conn = get_conn()
    old_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assessment_questions WHERE position_id = ?", (position_id,)
    ).fetchall()]
    if old_ids:
        placeholders = ", ".join("?" for _ in old_ids)
        conn.execute(f"DELETE FROM assessment_answers WHERE question_id IN ({placeholders})", old_ids)
    conn.execute("DELETE FROM assessment_questions WHERE position_id = ?", (position_id,))
    conn.execute("DELETE FROM assessment_results WHERE position_id = ?", (position_id,))
    for i, q in enumerate(questions):
        q = q.strip()
        if q:
            conn.execute(
                "INSERT INTO assessment_questions (position_id, question_text, sort_order) VALUES (?, ?, ?)",
                (position_id, q, i),
            )
    conn.commit()
    conn.close()


def save_assessment_answer(candidate_id, question_id, answer_text):
    conn = get_conn()
    conn.execute(
        """INSERT INTO assessment_answers (candidate_id, question_id, answer_text) VALUES (?, ?, ?)
           ON CONFLICT(candidate_id, question_id) DO UPDATE SET answer_text = excluded.answer_text,
               created_at = datetime('now')""",
        (candidate_id, question_id, answer_text.strip()),
    )
    conn.commit()
    conn.close()


def get_assessment_answers(candidate_id):
    """{question_id: answer_text} for this candidate."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT question_id, answer_text FROM assessment_answers WHERE candidate_id = ?", (candidate_id,)
    ).fetchall()
    conn.close()
    return {r["question_id"]: r["answer_text"] for r in rows}


def save_assessment_result(candidate_id, position_id, score, summary, positives, negatives):
    conn = get_conn()
    conn.execute(
        """INSERT INTO assessment_results (candidate_id, position_id, score, summary, positives_json, negatives_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (candidate_id, position_id, score, summary, json.dumps(positives), json.dumps(negatives)),
    )
    conn.commit()
    conn.close()


def get_assessment_result(candidate_id):
    """Most recent scoring result for this candidate's assessment, or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM assessment_results WHERE candidate_id = ? ORDER BY scored_at DESC LIMIT 1", (candidate_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ---- LLM-assisted interview scorecards (from Quick Capture notes) ------------

def save_candidate_scorecard(candidate_id, score, summary, positives, negatives, notes_count):
    conn = get_conn()
    conn.execute(
        """INSERT INTO candidate_scorecards (candidate_id, score, summary, positives_json, negatives_json, notes_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (candidate_id, score, summary, json.dumps(positives), json.dumps(negatives), notes_count),
    )
    conn.commit()
    conn.close()


def get_candidate_scorecards(candidate_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM candidate_scorecards WHERE candidate_id = ? ORDER BY created_at DESC", (candidate_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Users & admin ------------------------------------------------------------

def list_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY (is_admin) DESC, username").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_user(username, display_name, password_hash, is_admin=0):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, is_admin) VALUES (?, ?, ?, ?)",
        (username, display_name, password_hash, 1 if is_admin else 0),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id


def delete_user(user_id):
    """Removes a user account and anything assigned/attributed to it that would
    otherwise dangle - their position assignments (access grants, meaningless
    once the account is gone). Ratings/notes/audit rows they created are kept
    (append-only history), just no longer joinable to a live user row."""
    conn = get_conn()
    conn.execute("DELETE FROM position_assignments WHERE assigned_user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def is_admin_user(user):
    return bool(user and user.get("is_admin"))


# ---- Position assignments (Feature 1: per-hiring access control) --------------

def list_position_assignments(position_id):
    """Every user assigned to this position, newest-first, with display names
    resolved for both the assignee and whoever made the assignment."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT pa.*, u.username AS assigned_username, u.display_name AS assigned_display_name,
                  u.email_address AS assigned_email,
                  ub.username AS assigned_by_username, ub.display_name AS assigned_by_display_name
           FROM position_assignments pa
           JOIN users u ON u.id = pa.assigned_user_id
           LEFT JOIN users ub ON ub.id = pa.assigned_by
           WHERE pa.position_id = ?
           ORDER BY pa.assigned_at DESC""",
        (position_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def assigned_position_ids_for_user(user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT position_id FROM position_assignments WHERE assigned_user_id = ?", (user_id,)
    ).fetchall()
    conn.close()
    return [r["position_id"] for r in rows]


def is_user_assigned_to_position(user_id, position_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM position_assignments WHERE assigned_user_id = ? AND position_id = ?",
        (user_id, position_id),
    ).fetchone()
    conn.close()
    return row is not None


def assign_user_to_position(position_id, assigned_user_id, role_label, assigned_by):
    """Upsert - assigning the same user to the same position again just updates
    their role label/assigned-by rather than creating a duplicate row. This is
    also how a role label gets edited later (re-submit the assign form)."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO position_assignments (position_id, assigned_user_id, role_label, assigned_by, assigned_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(position_id, assigned_user_id) DO UPDATE SET
               role_label = excluded.role_label, assigned_by = excluded.assigned_by, assigned_at = excluded.assigned_at""",
        (position_id, assigned_user_id, (role_label or "").strip() or None, assigned_by),
    )
    conn.commit()
    conn.close()

    position = get_position(position_id)
    assigned_user = get_user_by_id(assigned_user_id)
    who = assigned_user["display_name"] or assigned_user["username"] if assigned_user else f"user #{assigned_user_id}"
    role_bit = f" as {role_label.strip()}" if role_label and role_label.strip() else ""
    _log_audit_internal(
        assigned_by, "assignment_changed",
        f"assigned {who}{role_bit} to {position['title'] if position else 'a position'}",
        position_id=position_id,
    )


def unassign_user_from_position(assignment_id, removed_by):
    conn = get_conn()
    row = conn.execute("SELECT * FROM position_assignments WHERE id = ?", (assignment_id,)).fetchone()
    if not row:
        conn.close()
        return
    conn.execute("DELETE FROM position_assignments WHERE id = ?", (assignment_id,))
    conn.commit()
    conn.close()

    position = get_position(row["position_id"])
    assigned_user = get_user_by_id(row["assigned_user_id"])
    who = assigned_user["display_name"] or assigned_user["username"] if assigned_user else f"user #{row['assigned_user_id']}"
    _log_audit_internal(
        removed_by, "assignment_changed",
        f"unassigned {who} from {position['title'] if position else 'a position'}",
        position_id=row["position_id"],
    )


# ---- Candidate interview ratings & remarks (Feature 1) -------------------------

def add_candidate_rating(candidate_id, position_id, rated_by_user_id, round_label, rating, remarks):
    conn = get_conn()
    conn.execute(
        """INSERT INTO candidate_ratings (candidate_id, position_id, rated_by_user_id, round_label, rating, remarks)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (candidate_id, position_id, rated_by_user_id, (round_label or "").strip() or None, rating, (remarks or "").strip() or None),
    )
    conn.commit()
    conn.close()

    candidate = get_candidate(candidate_id)
    rater = get_user_by_id(rated_by_user_id)
    who = rater["display_name"] or rater["username"] if rater else f"user #{rated_by_user_id}"
    name = candidate["candidate_name"] or f"candidate #{candidate_id}" if candidate else f"candidate #{candidate_id}"
    round_bit = f" ({round_label.strip()})" if round_label and round_label.strip() else ""
    _log_audit_internal(
        rated_by_user_id, "rating_submitted",
        f"{who} rated {name}{round_bit}: {rating}/5",
        candidate_id=candidate_id, position_id=position_id,
    )


def list_candidate_ratings(candidate_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT cr.*, u.username, u.display_name
           FROM candidate_ratings cr JOIN users u ON u.id = cr.rated_by_user_id
           WHERE cr.candidate_id = ? ORDER BY cr.created_at DESC""",
        (candidate_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Audit log (Feature 2) -----------------------------------------------------

def _log_audit_internal(user_id, action, detail, candidate_id=None, position_id=None):
    actor_label = None
    if user_id:
        user = get_user_by_id(user_id)
        if user:
            actor_label = user["display_name"] or user["username"]
    conn = get_conn()
    conn.execute(
        """INSERT INTO audit_log (user_id, actor_label, action, detail, candidate_id, position_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, actor_label, action, detail, candidate_id, position_id),
    )
    conn.commit()
    conn.close()


def log_audit(user_id, action, detail, candidate_id=None, position_id=None):
    """Public entry point for app.py call sites that don't go through one of
    db.py's own mutating functions above (e.g. offer actions, user creation)."""
    _log_audit_internal(user_id, action, detail, candidate_id=candidate_id, position_id=position_id)


AUDIT_ACTION_LABELS = {
    "stage_change": "Stage updated",
    "note_added": "Note added",
    "rating_submitted": "Rating submitted",
    "assignment_changed": "Assignment changed",
    "offer_action": "Offer action",
    "user_created": "User created",
    "user_deleted": "User deleted",
    "interview_scheduled": "Interview scheduled",
    "feedback_saved": "Panel feedback saved",
}


def list_audit_log(user_id=None, action=None, date_from=None, date_to=None):
    conn = get_conn()
    clauses = []
    params = []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if date_from:
        clauses.append("date(created_at) >= date(?)")
        params.append(date_from)
    if date_to:
        clauses.append("date(created_at) <= date(?)")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY created_at DESC LIMIT 1000", params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def distinct_audit_actors():
    """Users who have at least one audit_log row - for the filter dropdown, so
    it doesn't list every user account regardless of whether they've done
    anything yet."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT u.id, u.username, u.display_name FROM audit_log a
           JOIN users u ON u.id = a.user_id
           ORDER BY u.username"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def distinct_audit_actions():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()
    conn.close()
    return [r["action"] for r in rows]


# ---- Interview & assessment question recommendations (Feature 3) --------------
# Deliberately separate from assessment_questions/assessment_answers/
# assessment_results above - those are a formal, candidate-answered, scored
# assessment. This is just a plain suggestions list for the interviewer to look
# at and pick from, never shown to or answered by the candidate, never scored.

def save_interview_recommendations(position_id, candidate_id, generated_by_user_id, questions):
    conn = get_conn()
    conn.execute(
        """INSERT INTO interview_question_recommendations
           (position_id, candidate_id, generated_by_user_id, questions_json)
           VALUES (?, ?, ?, ?)""",
        (position_id, candidate_id, generated_by_user_id, json.dumps(questions)),
    )
    conn.commit()
    conn.close()


def _load_recommendation_rows(rows):
    result = []
    for r in rows:
        d = dict(r)
        d["questions"] = json.loads(d["questions_json"]) if d["questions_json"] else []
        result.append(d)
    return result


def list_position_interview_recommendations(position_id):
    """Position-level batches only (candidate_id IS NULL) - JD-only recommendations."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, u.username, u.display_name FROM interview_question_recommendations r
           LEFT JOIN users u ON u.id = r.generated_by_user_id
           WHERE r.position_id = ? AND r.candidate_id IS NULL
           ORDER BY r.created_at DESC""",
        (position_id,),
    ).fetchall()
    conn.close()
    return _load_recommendation_rows(rows)


def list_candidate_interview_recommendations(candidate_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, u.username, u.display_name FROM interview_question_recommendations r
           LEFT JOIN users u ON u.id = r.generated_by_user_id
           WHERE r.candidate_id = ?
           ORDER BY r.created_at DESC""",
        (candidate_id,),
    ).fetchall()
    conn.close()
    return _load_recommendation_rows(rows)


# ---- Hiring timeline: stage history, interview scheduling, panel feedback -------
# (see timeline.py.) The aging maths and the
# dashboard/report tables live in timeline.py; this block is the storage layer.

# Coarse stage groups used for the dashboard's per-position counts and for "average
# days per stage" - the same vocabulary for legacy L1/L2/L3 positions and for
# dynamic-rounds positions with their own round names.
STAGE_PHASES = (
    "Sourced", "Reached", "Interview Scheduled", "Interviewed", "Round Cleared",
    "Offer Made", "On Hold", "Joined", "Rejected",
)
# Reaching any of these stages is a hiring DECISION (used for "feedback -> decision").
DECISION_STAGE_KEYS = ("offer_made", "joined", "rejected")
FEEDBACK_RECOMMENDATIONS = ("Pass", "Hold", "Reject")
INTERVIEW_MODES = ("Teams", "In person", "Phone", "Other")

# Configurable targets (days), stored in the existing key/value settings table as
# timeline_target_<name>. Editable under Settings -> Hiring Timeline Targets.
TIMELINE_TARGET_DEFAULTS = {
    "stage_age_days": 5,
    "feedback_pending_days": 2,
    "decision_pending_days": 3,
    "position_open_days": 30,
}
TIMELINE_TARGET_LABELS = {
    "stage_age_days": "Candidate sitting in one stage for more than (days)",
    "feedback_pending_days": "Panel feedback still pending after the interview for more than (days)",
    "decision_pending_days": "No decision after the first feedback was saved for more than (days)",
    "position_open_days": "Position still open since the JD was uploaded for more than (days)",
}


def get_timeline_targets():
    """{name: days} - the saved targets, falling back to the defaults for any that
    were never saved (or hold something unusable)."""
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'timeline_target_%'").fetchall()
    conn.close()
    result = dict(TIMELINE_TARGET_DEFAULTS)
    for r in rows:
        name = r["key"][len("timeline_target_"):]
        if name in result:
            try:
                value = int(str(r["value"]).strip())
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 365:
                result[name] = value
    return result


def set_timeline_targets(values):
    """Saves any of the targets in `values` ({name: days}); every value must be a
    whole number of days from 1 to 365. Raises ValueError (nothing saved) otherwise."""
    cleaned = {}
    for name, raw in values.items():
        if name not in TIMELINE_TARGET_DEFAULTS:
            continue
        try:
            days = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"'{TIMELINE_TARGET_LABELS[name]}' must be a whole number of days.")
        if not 1 <= days <= 365:
            raise ValueError(f"'{TIMELINE_TARGET_LABELS[name]}' must be between 1 and 365 days.")
        cleaned[name] = days
    conn = get_conn()
    for name, days in cleaned.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"timeline_target_{name}", str(days)),
        )
    conn.commit()
    conn.close()


def timeline_stage(c, rounds=None, round_statuses=None):
    """(stage_key, stage_label, phase) for a candidate row dict - the single
    "where is this CV right now" answer used by stage history and the dashboard.

    rounds is None for the legacy fixed L1/L2/L3 positions (read from the
    candidates' own l1_/l2_/l3_ flags); for a dynamic-rounds position pass its
    interview_rounds rows and this candidate's candidate_round_status rows
    ({round_id: row}). Precedence: Rejected, Joined, On Hold, Offer Made, then the
    furthest interview round with any flag set, then Reached, else Sourced."""
    if c["rejected"]:
        return "rejected", "Rejected", "Rejected"
    if c["joined"]:
        return "joined", "Joined", "Joined"
    if c["on_hold"]:
        return "on_hold", "On Hold", "On Hold"
    if c["offer_made"]:
        return "offer_made", "Offer Made", "Offer Made"
    if rounds is not None:
        statuses = round_statuses or {}
        for r in sorted(rounds, key=lambda r: r["round_number"], reverse=True):
            st = statuses.get(r["id"]) or {}
            if st.get("cleared"):
                return f"r{r['id']}_cleared", f"Cleared {r['round_label']}", "Round Cleared"
            if st.get("attended"):
                return f"r{r['id']}_attended", f"{r['round_label']} Attended", "Interviewed"
            if st.get("scheduled"):
                return f"r{r['id']}_scheduled", f"{r['round_label']} Scheduled", "Interview Scheduled"
    else:
        for level in ("l3", "l2", "l1"):
            tag = level.upper()
            if c[f"{level}_cleared"]:
                return f"{level}_cleared", f"Cleared {tag}", "Round Cleared"
            if c[f"{level}_attended"]:
                return f"{level}_attended", f"{tag} Attended", "Interviewed"
            if c[f"{level}_scheduled"]:
                return f"{level}_scheduled", f"{tag} Scheduled", "Interview Scheduled"
    if c["reached"]:
        return "reached", "Reached", "Reached"
    return "sourced", "Sourced", "Sourced"


def _stage_for_row(conn, c):
    """timeline_stage() for a candidate row dict, loading that position's dynamic
    rounds / this candidate's round statuses when it has them."""
    rounds = statuses = None
    pos = conn.execute("SELECT uses_dynamic_rounds FROM positions WHERE id = ?", (c["position_id"],)).fetchone()
    if pos and pos["uses_dynamic_rounds"]:
        rounds = [
            dict(r) for r in conn.execute(
                "SELECT id, round_number, round_label FROM interview_rounds WHERE position_id = ? ORDER BY round_number",
                (c["position_id"],),
            ).fetchall()
        ]
        statuses = {
            r["round_id"]: dict(r) for r in conn.execute(
                "SELECT * FROM candidate_round_status WHERE candidate_id = ?", (c["id"],)
            ).fetchall()
        }
    key, label, phase = timeline_stage(c, rounds, statuses)
    return {"key": key, "label": label, "phase": phase}


def _stage_snapshot(conn, candidate_id):
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    return _stage_for_row(conn, dict(row)) if row else None


def _record_sourced_history(conn, candidate_id):
    """Origin row for a brand-new candidate: it entered 'Sourced' the moment its
    CV was fed in (candidates.created_at). A real, live record, not a backfill."""
    row = conn.execute("SELECT position_id, created_at FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not row or not row["created_at"]:
        return
    conn.execute(
        """INSERT INTO candidate_stage_history
           (candidate_id, position_id, from_stage, stage_key, stage_label, entered_at, source)
           VALUES (?, ?, NULL, 'sourced', 'Sourced', ?, 'recorded')""",
        (candidate_id, row["position_id"], row["created_at"]),
    )


def _backfill_candidate_history(conn, c):
    """Writes the history rows for ONE candidate that has none yet, from the best
    timestamps that already exist - never inventing one. Rows are marked
    source='backfilled'; basis records which existing timestamp each one came from.
    Returns True if rows were written."""
    created = c["created_at"]
    if not created:
        pos = conn.execute("SELECT created_at FROM positions WHERE id = ?", (c["position_id"],)).fetchone()
        created = pos["created_at"] if pos else None
    if not created:
        return False  # nothing to anchor a date on - leave it without history rather than guess
    snap = _stage_for_row(conn, c)
    conn.execute(
        """INSERT INTO candidate_stage_history
           (candidate_id, position_id, from_stage, stage_key, stage_label, entered_at, source, basis)
           VALUES (?, ?, NULL, 'sourced', 'Sourced', ?, 'backfilled', 'cv_added')""",
        (c["id"], c["position_id"], created),
    )
    if snap["key"] == "sourced":
        return True
    ts = basis = None
    m = re.match(r"^r(\d+)_(scheduled|attended|cleared)$", snap["key"])
    if m:
        r = conn.execute(
            f"SELECT {m.group(2)}_at AS ts FROM candidate_round_status WHERE candidate_id = ? AND round_id = ?",
            (c["id"], int(m.group(1))),
        ).fetchone()
        if r and r["ts"]:
            ts, basis = r["ts"], "round_status"
    elif snap["key"] in ALL_STAGE_COLUMNS:
        r = conn.execute(
            "SELECT MAX(changed_at) AS ts FROM stage_events WHERE candidate_id = ? AND field = ? AND value = 1",
            (c["id"], snap["key"]),
        ).fetchone()
        if r and r["ts"]:
            ts, basis = r["ts"], "stage_events"
    if not ts:
        ts, basis = created, "cv_added_proxy"
    conn.execute(
        """INSERT INTO candidate_stage_history
           (candidate_id, position_id, from_stage, stage_key, stage_label, entered_at, source, basis)
           VALUES (?, ?, 'Sourced', ?, ?, ?, 'backfilled', ?)""",
        (c["id"], c["position_id"], snap["key"], snap["label"], ts, basis),
    )
    return True


def backfill_stage_history():
    """Idempotent upgrade step (runs on every start): every candidate that has NO
    stage-history rows yet gets backfilled rows (see _backfill_candidate_history).
    Candidates that already have history are never touched. Returns how many
    candidates were backfilled."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM candidates c
           WHERE NOT EXISTS (SELECT 1 FROM candidate_stage_history h WHERE h.candidate_id = c.id)"""
    ).fetchall()
    done = 0
    for r in rows:
        if _backfill_candidate_history(conn, dict(r)):
            done += 1
    conn.commit()
    conn.close()
    return done


def _stage_before_change(conn, candidate_id):
    """Call right BEFORE changing a candidate's stage flags: makes sure the
    candidate's history has started (a candidate that somehow has none gets its
    pre-change state backfilled first) and returns the pre-change stage."""
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not row:
        return None
    has_history = conn.execute(
        "SELECT 1 FROM candidate_stage_history WHERE candidate_id = ? LIMIT 1", (candidate_id,)
    ).fetchone()
    if not has_history:
        _backfill_candidate_history(conn, dict(row))
    return _stage_for_row(conn, dict(row))


def _record_stage_change(conn, candidate_id, stage_before, actor_user_id=None):
    """Call right AFTER a stage flag changed (same transaction): if the candidate's
    current stage differs from stage_before, append a history row stamped now."""
    after = _stage_snapshot(conn, candidate_id)
    if not after or (stage_before and stage_before["key"] == after["key"]):
        return
    pos = conn.execute("SELECT position_id FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    conn.execute(
        """INSERT INTO candidate_stage_history
           (candidate_id, position_id, from_stage, stage_key, stage_label, entered_at, source, actor_user_id)
           VALUES (?, ?, ?, ?, ?, datetime('now'), 'recorded', ?)""",
        (candidate_id, pos["position_id"], stage_before["label"] if stage_before else None,
         after["key"], after["label"], actor_user_id),
    )


def list_candidate_stage_history(candidate_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM candidate_stage_history WHERE candidate_id = ? ORDER BY entered_at, id", (candidate_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _resolve_round(position, round_key):
    """(round_id, round_label) for a schedule's round_key on this position, or
    ValueError. Legacy positions: 'l1'/'l2'/'l3'. Dynamic-rounds positions: 'r<id>'."""
    key = (round_key or "").strip().lower()
    if position["uses_dynamic_rounds"]:
        m = re.match(r"^r(\d+)$", key)
        rnd = get_interview_round(int(m.group(1))) if m else None
        if not rnd or rnd["position_id"] != position["id"]:
            raise ValueError(f"Unknown interview round: {round_key}")
        return rnd["id"], rnd["round_label"]
    if key not in ("l1", "l2", "l3"):
        raise ValueError(f"Unknown interview round: {round_key}")
    return None, f"{key.upper()} Interview"


def interview_round_options(position):
    """[(round_key, label)] a candidate of this position can be scheduled for."""
    if position["uses_dynamic_rounds"]:
        return [(f"r{r['id']}", r["round_label"]) for r in get_interview_rounds(position["id"])]
    return [(k, f"{k.upper()} Interview") for k in ("l1", "l2", "l3")]


def _default_panel(position, round_key, round_id):
    """The panel member(s) already on record for this round (legacy stakeholder /
    dynamic-round assignee), used when none were typed in."""
    if position["uses_dynamic_rounds"]:
        rnd = get_interview_round(round_id) if round_id else None
        if rnd and rnd["assigned_user_id"]:
            u = get_user_by_id(rnd["assigned_user_id"])
            if u:
                return u["display_name"] or u["username"]
        return None
    s = get_stakeholders(position["id"]).get(round_key)
    return s["name"] if s else None


def schedule_interview(candidate_id, round_key, scheduled_for=None, scheduled_for_text=None, interviewers=None,
                       mode=None, link=None, record_flag=False, notes=None, scheduled_by=None, mark_stage=True):
    """Records that an interview was scheduled (who/when/where/recorded?) plus the
    timestamp it was scheduled, and - unless mark_stage is False - ticks that
    round's existing 'Scheduled' stage so the candidate's stage reflects it.
    Returns the new interview id. Raises ValueError on an unknown candidate/round."""
    candidate = get_candidate(candidate_id)
    if not candidate:
        raise ValueError("Unknown candidate.")
    position = get_position(candidate["position_id"])
    round_id, round_label = _resolve_round(position, round_key)
    key = round_key.strip().lower()
    if mode and mode not in INTERVIEW_MODES:
        mode = "Other"
    panel = (interviewers or "").strip() or _default_panel(position, key, round_id)
    when = (scheduled_for or "").strip() or None
    when_text = (scheduled_for_text or "").strip() or None
    conn = get_conn()
    # Re-sending the same invite (same round, same date/time) is not a reschedule: update
    # that row instead of adding a second one, so panel feedback already saved on it
    # stays attached to the live interview.
    latest = conn.execute(
        "SELECT * FROM interview_schedule WHERE candidate_id = ? AND round_key = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (candidate_id, key),
    ).fetchone()
    same_slot = bool(latest) and (
        (when and latest["scheduled_for"] == when) or (not when and not latest["scheduled_for"] and when_text and latest["scheduled_for_text"] == when_text)
    )
    if same_slot:
        interview_id = latest["id"]
        conn.execute(
            """UPDATE interview_schedule SET interviewers = COALESCE(?, interviewers), mode = COALESCE(?, mode),
                      link = COALESCE(?, link), record_flag = ?, notes = COALESCE(?, notes)
               WHERE id = ?""",
            (panel, (mode or "").strip() or None, (link or "").strip() or None, 1 if record_flag else 0,
             (notes or "").strip() or None, interview_id),
        )
    else:
        cur = conn.execute(
            """INSERT INTO interview_schedule
               (candidate_id, position_id, round_key, round_id, round_label, scheduled_for, scheduled_for_text,
                interviewers, mode, link, record_flag, notes, scheduled_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (candidate_id, candidate["position_id"], key, round_id, round_label, when, when_text,
             panel, (mode or "").strip() or None, (link or "").strip() or None,
             1 if record_flag else 0, (notes or "").strip() or None, scheduled_by),
        )
        interview_id = cur.lastrowid
    conn.commit()
    conn.close()

    if mark_stage:
        if position["uses_dynamic_rounds"]:
            if not get_candidate_round_statuses(candidate_id).get(round_id, {}).get("scheduled"):
                set_candidate_round_status(candidate_id, round_id, "scheduled", actor_user_id=scheduled_by)
        elif not candidate[f"{key}_scheduled"]:
            update_stage(candidate_id, f"{key}_scheduled", True, actor_user_id=scheduled_by)

    if not same_slot:
        name = candidate["candidate_name"] or f"candidate #{candidate_id}"
        _log_audit_internal(
            scheduled_by, "interview_scheduled",
            f"scheduled {round_label} for {name} ({when or when_text or 'no date given'})",
            candidate_id=candidate_id, position_id=candidate["position_id"],
        )
    return interview_id


def get_interview(interview_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM interview_schedule WHERE id = ?", (interview_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_interview_feedback(interview_id, panelist, recommendation, rating=None, comments=None, saved_by=None):
    """Saves one panel member's feedback on a scheduled interview (several per
    interview are normal). recommendation: Pass / Hold / Reject (any case);
    rating: optional 1-5. Raises ValueError for anything unusable."""
    interview = get_interview(interview_id)
    if not interview:
        raise ValueError("Unknown interview.")
    panelist = (panelist or "").strip()
    if not panelist:
        raise ValueError("Enter the panel member's name.")
    rec = next((r for r in FEEDBACK_RECOMMENDATIONS if r.lower() == (recommendation or "").strip().lower()), None)
    if not rec:
        raise ValueError("Choose Pass, Hold or Reject.")
    if rating in ("", None):
        rating = None
    else:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            raise ValueError("Rating must be a number from 1 to 5.")
        if not 1 <= rating <= 5:
            raise ValueError("Rating must be a number from 1 to 5.")
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO interview_feedback
           (interview_id, candidate_id, position_id, panelist, recommendation, rating, comments, saved_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (interview_id, interview["candidate_id"], interview["position_id"], panelist, rec, rating,
         (comments or "").strip() or None, saved_by),
    )
    conn.commit()
    feedback_id = cur.lastrowid
    conn.close()
    candidate = get_candidate(interview["candidate_id"])
    name = (candidate["candidate_name"] if candidate else None) or f"candidate #{interview['candidate_id']}"
    _log_audit_internal(
        saved_by, "feedback_saved", f"{panelist} gave {rec} on {interview['round_label']} for {name}",
        candidate_id=interview["candidate_id"], position_id=interview["position_id"],
    )
    return feedback_id


def list_candidate_interviews(candidate_id):
    """Every interview scheduled for this candidate, newest first, each with its
    panel feedback rows (oldest first) under 'feedback'. is_current marks the latest
    schedule per round - an older row for the same round was rescheduled."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM interview_schedule WHERE candidate_id = ? ORDER BY created_at DESC, id DESC", (candidate_id,)
    ).fetchall()
    feedback = conn.execute(
        "SELECT * FROM interview_feedback WHERE candidate_id = ? ORDER BY created_at, id", (candidate_id,)
    ).fetchall()
    conn.close()
    by_interview = {}
    for f in feedback:
        by_interview.setdefault(f["interview_id"], []).append(dict(f))
    seen_rounds = set()
    result = []
    for r in rows:
        d = dict(r)
        d["feedback"] = by_interview.get(d["id"], [])
        d["is_current"] = d["round_key"] not in seen_rounds
        seen_rounds.add(d["round_key"])
        result.append(d)
    return result


def timeline_raw_data(position_ids=None):
    """Everything the hiring-timeline dashboard needs, in a handful of queries
    (no per-candidate round trips): positions (newest first, same order as the
    rest of the dashboard), their candidates (stage columns only - no resume
    text), stage history, interview schedule rows, panel feedback, and the
    dynamic-rounds rounds/status rows. position_ids scopes it to a subset (a
    non-admin's assigned positions); None = every position."""
    empty = {"positions": [], "candidates": [], "history": [], "schedules": [], "feedback": [],
             "rounds": [], "round_statuses": []}
    if position_ids is not None and not position_ids:
        return empty
    conn = get_conn()
    if position_ids is not None:
        marks = ", ".join("?" for _ in position_ids)
        positions = conn.execute(
            f"SELECT * FROM positions WHERE id IN ({marks}) ORDER BY created_at DESC, id DESC", list(position_ids)
        ).fetchall()
    else:
        positions = conn.execute("SELECT * FROM positions ORDER BY created_at DESC, id DESC").fetchall()
    ids = [p["id"] for p in positions]
    if not ids:
        conn.close()
        return empty
    marks = ", ".join("?" for _ in ids)
    stage_cols = ", ".join(ALL_STAGE_COLUMNS)
    candidates = conn.execute(
        f"""SELECT id, position_id, candidate_name, resume_filename, created_at, score, bucket, {stage_cols}
            FROM candidates WHERE position_id IN ({marks}) ORDER BY {CANDIDATE_DEFAULT_ORDER}""", ids
    ).fetchall()
    cand_filter = f"candidate_id IN (SELECT id FROM candidates WHERE position_id IN ({marks}))"
    history = conn.execute(
        f"SELECT * FROM candidate_stage_history WHERE {cand_filter} ORDER BY entered_at, id", ids
    ).fetchall()
    schedules = conn.execute(
        f"SELECT * FROM interview_schedule WHERE {cand_filter} ORDER BY created_at, id", ids
    ).fetchall()
    feedback = conn.execute(
        f"SELECT * FROM interview_feedback WHERE {cand_filter} ORDER BY created_at, id", ids
    ).fetchall()
    rounds = conn.execute(
        f"SELECT * FROM interview_rounds WHERE position_id IN ({marks}) ORDER BY position_id, round_number", ids
    ).fetchall()
    round_statuses = conn.execute(
        f"SELECT * FROM candidate_round_status WHERE {cand_filter}", ids
    ).fetchall()
    conn.close()
    return {
        "positions": [dict(r) for r in positions], "candidates": [dict(r) for r in candidates],
        "history": [dict(r) for r in history], "schedules": [dict(r) for r in schedules],
        "feedback": [dict(r) for r in feedback], "rounds": [dict(r) for r in rounds],
        "round_statuses": [dict(r) for r in round_statuses],
    }
