import io
import json
import os
import secrets
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import config  # noqa: F401  (loads .env before any setting is read)
import candidate_import
import capture
import db
import resume_parser
import scorer
import tracker_export
import report_generator
import branded_report
import font_installer
import emailer
import offer_letter
import report_data
import timeline

APP_DIR = os.path.dirname(os.path.abspath(__file__))

ALLOWED_JD_EXT = {".docx", ".pdf", ".txt"}
ALLOWED_RESUME_EXT = {".docx", ".pdf", ".txt"}
ALLOWED_IMPORT_EXT = {".csv", ".xlsx"}


def _load_secret_key():
    """Session-signing key: HR_AGENT_SECRET_KEY if set, otherwise a random key generated on
    first run and kept in the (git-ignored) data folder so logins survive restarts."""
    env_key = os.environ.get("HR_AGENT_SECRET_KEY")
    if env_key:
        return env_key
    key_path = os.path.join(db.DATA_DIR, "secret_key")
    try:
        with open(key_path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    generated = secrets.token_hex(32)
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(generated)
    except OSError:
        pass  # read-only data folder: fall back to a per-process key
    return generated


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB, covers large interview recordings
# Static files (style.css etc.) change constantly during active development - without
# this, browsers can keep serving a stale cached copy after an edit and the UI looks
# "broken" even though the server is up to date. Safe to remove once this settles down.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

db.init_db()

# in-memory scoring progress, keyed by position_id (fine for a single-process deployment)
_score_progress = {}


@app.before_request
def require_login():
    if request.endpoint in ("login", "static") or request.path.startswith("/static/"):
        return None
    if request.path.startswith("/offer/approve/") or request.path.startswith("/offer/respond/"):
        return None
    if not session.get("user_id"):
        return redirect(url_for("login"))
    return None


# Feature 1 (per-hiring access control): endpoints an assigned-but-non-admin
# user may never reach, regardless of any position_id/candidate_id in the URL -
# admin-only management pages.
_ADMIN_ONLY_ENDPOINTS = {
    "users_view",
    "delete_user_route",
    "audit_log_view",
    "assign_position",
    "unassign_position",
}


@app.before_request
def enforce_position_scope():
    """Runs after require_login (which already redirected/blocked anyone not
    logged in, and skipped login/static/offer-token routes). Admins bypass all
    scoping. A non-admin user is only allowed onto admin-only management
    endpoints, or onto a position_id/candidate_id-scoped endpoint they're
    actually assigned to - this is what makes 'assigned user sees only their
    own position(s), not the whole candidate database' actually enforced
    everywhere those ids appear in a URL, rather than page-by-page."""
    if request.endpoint in (None, "login", "static") or request.path.startswith("/static/"):
        return None
    if not session.get("user_id"):
        return None  # not logged in - require_login already handled this path

    user = db.get_user_by_id(session["user_id"])
    if not user or db.is_admin_user(user):
        return None

    if request.endpoint in _ADMIN_ONLY_ENDPOINTS:
        abort(403)

    view_args = request.view_args or {}
    position_id = view_args.get("position_id")
    candidate_id = view_args.get("candidate_id")
    if position_id is None and candidate_id is not None:
        candidate = db.get_candidate(candidate_id)
        position_id = candidate["position_id"] if candidate else None
    if position_id is not None and not db.is_user_assigned_to_position(user["id"], position_id):
        abort(403)
    return None


@app.context_processor
def inject_current_user():
    user_id = session.get("user_id")
    current_user = db.get_user_by_id(user_id) if user_id else None
    notifications = db.list_notifications(user_id) if user_id else None
    unread_count = db.unread_notification_count(user_id) if user_id else 0
    return {"current_user": current_user, "notifications": notifications, "unread_notification_count": unread_count}


def _score_highlight_style(score):
    """Continuous red->yellow->green highlight for a 0-10 fit score, used to
    color the candidate name in the resumes list - a finer-grained signal
    than the 3-bucket (green/yellow/red) avatar color. Hue slides from red
    (0) to green (120) with the score; lightness darkens as the score rises
    so a 10 reads as a deep green and a 9 a visibly lighter one rather than
    just a different hue."""
    if score is None:
        return ""
    score = max(0.0, min(10.0, float(score)))
    hue = round(score / 10 * 120)
    lightness = round(82 - score * 4)
    text_color = "#ffffff" if lightness < 55 else "#4a3200"
    return f"background-color: hsl({hue}, 68%, {lightness}%); color: {text_color};"


app.jinja_env.globals["score_highlight_style"] = _score_highlight_style
# Hiring-timeline formatting (dates shown as "06 Oct 2026", missing values as "-")
app.jinja_env.filters["short_date"] = timeline.fmt_date
app.jinja_env.filters["stored_dt"] = timeline.fmt_stored_datetime
app.jinja_env.filters["when_text"] = timeline.fmt_when
app.jinja_env.globals["fmt_days"] = timeline.fmt_days


def _require_admin():
    """Aborts 403 unless the logged-in user is an admin - used by the handful
    of routes (Users, Audit Log, assign/unassign) that are always admin-only,
    on top of the general position-scoping in enforce_position_scope()."""
    user = db.get_user_by_id(session["user_id"])
    if not db.is_admin_user(user):
        abort(403)
    return user


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("home"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    user = db.get_user_by_id(session["user_id"])
    llm_settings = db.get_llm_settings()
    error = None
    message = None

    if request.method == "POST":
        form_name = request.form.get("form_name")

        if form_name == "llm":
            db.update_llm_settings(
                provider=request.form.get("llm_provider"),
                api_key=request.form.get("llm_api_key", "").strip(),
                model=request.form.get("llm_model", "").strip(),
            )
            message = "LLM settings saved."
            llm_settings = db.get_llm_settings()

        elif form_name == "profile":
            display_name = request.form.get("display_name", "").strip()
            if display_name:
                db.update_user_profile(
                    user["id"], display_name,
                    request.form.get("designation", ""), request.form.get("department", ""),
                )
                message = "Profile updated."
                user = db.get_user_by_id(user["id"])
            else:
                error = "Display name can't be empty."

        elif form_name == "password":
            current = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            if not check_password_hash(user["password_hash"], current):
                error = "Current password is incorrect."
            elif len(new_password) < 6:
                error = "New password must be at least 6 characters."
            elif new_password != confirm:
                error = "New password and confirmation don't match."
            else:
                db.update_user_password(user["id"], generate_password_hash(new_password))
                message = "Password changed."

        elif form_name == "email_account":
            db.update_user_email_account(
                user["id"],
                request.form.get("email_address", "").strip(),
                request.form.get("email_app_password", "").strip(),
                request.form.get("email_smtp_host", "").strip() or "smtp.office365.com",
                request.form.get("email_smtp_port", type=int) or 587,
            )
            message = "Email account saved."
            user = db.get_user_by_id(user["id"])

        elif form_name == "onboarding_checklist":
            items = request.form.get("onboarding_items", "").splitlines()
            db.update_onboarding_checklist_items(items)
            message = "Onboarding checklist template saved."

        elif form_name == "timeline_targets":
            try:
                db.set_timeline_targets({
                    name: request.form.get(f"target_{name}", "")
                    for name in db.TIMELINE_TARGET_DEFAULTS
                })
                message = "Hiring timeline targets saved."
            except ValueError as exc:
                error = str(exc)

    return render_template(
        "settings.html",
        user=user,
        llm_settings=llm_settings,
        onboarding_items=db.get_onboarding_checklist_items(),
        timeline_targets=db.get_timeline_targets(),
        timeline_target_labels=db.TIMELINE_TARGET_LABELS,
        timeline_target_defaults=db.TIMELINE_TARGET_DEFAULTS,
        error=error,
        message=message,
        report_template_installed=font_installer.is_installed(),
        has_custom_offer_template=offer_letter.has_custom_template(),
        offer_template_tokens=offer_letter.TEMPLATE_TOKENS,
        email_template_kinds=emailer.EMAIL_TEMPLATE_KINDS,
        email_template_labels=emailer.EMAIL_TEMPLATE_LABELS,
        email_template_tokens=emailer.EMAIL_TEMPLATE_TOKENS,
        email_template_filenames=emailer.EMAIL_TEMPLATE_FILENAMES,
        email_template_dir=emailer.EMAIL_TEMPLATE_DIR,
    )


@app.route("/settings/install-report-template", methods=["POST"])
def install_report_template():
    ok, msg = font_installer.install()
    return jsonify({"ok": ok, "message": msg})


@app.route("/settings/offer-template/upload", methods=["POST"])
def upload_offer_template():
    file = request.files.get("offer_template_file")
    if not file or not file.filename:
        return redirect(url_for("settings"))
    if os.path.splitext(file.filename)[1].lower() != ".docx":
        return redirect(url_for("settings"))
    offer_letter.save_template(file.read())
    return redirect(url_for("settings"))


@app.route("/settings/offer-template/delete", methods=["POST"])
def delete_offer_template():
    offer_letter.delete_template()
    return redirect(url_for("settings"))


# ---- Feature 1: user accounts & per-hiring assignment --------------------------

@app.route("/settings/users", methods=["GET", "POST"])
def users_view():
    """Admin-only. Create additional HR/interviewer user accounts - a simple
    role field is kept on the user record (is_admin) but access itself is
    driven by position_assignments below, not a fixed role enum, since the
    owner explicitly wants who's assigned to change freely over time."""
    _require_admin()
    error = None
    message = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        display_name = request.form.get("display_name", "").strip() or username
        password = request.form.get("password", "")
        if not username or len(password) < 6:
            error = "Give a username and a password of at least 6 characters."
        else:
            try:
                user_id = db.create_user(username, display_name, generate_password_hash(password))
                db.log_audit(session["user_id"], "user_created", f"created user account '{username}'")
                message = f"User '{username}' created."
            except sqlite3.IntegrityError:
                error = f"A user named '{username}' already exists."
    return render_template(
        "users.html",
        users=db.list_users(),
        error=error,
        message=message,
    )


@app.route("/settings/users/<int:user_id>/delete", methods=["POST"])
def delete_user_route(user_id):
    _require_admin()
    if user_id == session["user_id"]:
        return redirect(url_for("users_view"))
    target = db.get_user_by_id(user_id)
    if not target:
        abort(404)
    remaining_admins = [u for u in db.list_users() if u["is_admin"] and u["id"] != user_id]
    if target["is_admin"] and not remaining_admins:
        return redirect(url_for("users_view"))  # never delete the last admin
    db.delete_user(user_id)
    db.log_audit(session["user_id"], "user_deleted", f"deleted user account '{target['username']}'")
    return redirect(url_for("users_view"))


@app.route("/positions/<int:position_id>/assign", methods=["POST"])
def assign_position(position_id):
    """Admin-only. Assigning is a plain upsert (same user again just edits
    their role label) and unassigning below is a plain delete - reassignable/
    removable at any time during the hiring process, never soft-locked."""
    _require_admin()
    position = db.get_position(position_id)
    if not position:
        abort(404)
    assigned_user_id = request.form.get("assigned_user_id", type=int)
    role_label = request.form.get("role_label", "").strip()
    if not assigned_user_id:
        return redirect(url_for("home", position=position_id))
    db.assign_user_to_position(position_id, assigned_user_id, role_label, assigned_by=session["user_id"])
    return redirect(url_for("home", position=position_id, assignment_saved=1))


@app.route("/positions/<int:position_id>/unassign/<int:assignment_id>", methods=["POST"])
def unassign_position(position_id, assignment_id):
    _require_admin()
    db.unassign_user_from_position(assignment_id, removed_by=session["user_id"])
    return redirect(url_for("home", position=position_id, assignment_saved=1))


@app.route("/candidates/<int:candidate_id>/rating", methods=["POST"])
def submit_candidate_rating(candidate_id):
    """Any user with access to this candidate's position (admin, or the
    assigned interviewer) can submit a rating + written remarks for an
    interview round - e.g. Jordan, assigned as technical interviewer, rating
    and leaving remarks on his own round, visible to HR/admin afterward."""
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    rating = request.form.get("rating", type=int)
    round_label = request.form.get("round_label", "").strip()
    remarks = request.form.get("remarks", "").strip()
    if rating is None or not (1 <= rating <= 5):
        return _redirect_to_resumes(candidate, rating_error="Give a rating from 1 to 5.")
    db.add_candidate_rating(candidate_id, candidate["position_id"], session["user_id"], round_label, rating, remarks)
    return _redirect_to_resumes(candidate)


# ---- Hiring timeline: interview scheduling, panel feedback, score breakdown ------

def _back_to_candidate(candidate, anchor="interviews", **extra):
    return redirect(
        url_for(
            "resumes",
            position_id=candidate["position_id"],
            candidate=candidate["id"],
            q=request.form.get("q", ""),
            _anchor=anchor,
            **{"from": request.form.get("from", "")},
            **extra,
        )
    )


@app.route("/candidates/<int:candidate_id>/interviews/schedule", methods=["POST"])
def schedule_candidate_interview(candidate_id):
    """Records an interview being scheduled (round, date/time, panel, mode/link, whether
    it is to be recorded) and ticks that round's 'Scheduled' stage so the candidate's
    stage reflects it. The same record is also written by the existing 'send interview
    link' windows."""
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    date_text = request.form.get("interview_date", "").strip()
    time_text = request.form.get("interview_time", "").strip()
    scheduled_for = timeline.parse_interview_datetime(date_text, time_text)
    if not date_text or not scheduled_for:
        return _back_to_candidate(candidate, timeline_error="Pick a valid interview date (and time).")
    try:
        db.schedule_interview(
            candidate_id,
            request.form.get("round_key", ""),
            scheduled_for=scheduled_for,
            scheduled_for_text=f"{date_text} {time_text}".strip(),
            interviewers=request.form.get("interviewers", ""),
            mode=request.form.get("mode", "") or "Teams",
            link=request.form.get("link", ""),
            record_flag=request.form.get("record_flag") == "1",
            notes=request.form.get("notes", ""),
            scheduled_by=session["user_id"],
        )
    except ValueError as exc:
        return _back_to_candidate(candidate, timeline_error=str(exc))
    return _back_to_candidate(candidate, timeline_saved="scheduled")


@app.route("/candidates/<int:candidate_id>/interviews/<int:interview_id>/feedback", methods=["POST"])
def save_interview_feedback(candidate_id, interview_id):
    """Saves one panel member's feedback (Pass / Hold / Reject, optional 1-5 rating,
    comments) on a scheduled interview. Several panel members can each add theirs."""
    candidate = db.get_candidate(candidate_id)
    interview = db.get_interview(interview_id)
    if not candidate or not interview or interview["candidate_id"] != candidate_id:
        abort(404)
    try:
        db.add_interview_feedback(
            interview_id,
            request.form.get("panelist", ""),
            request.form.get("recommendation", ""),
            rating=request.form.get("rating", ""),
            comments=request.form.get("comments", ""),
            saved_by=session["user_id"],
        )
    except ValueError as exc:
        return _back_to_candidate(candidate, timeline_error=str(exc))
    return _back_to_candidate(candidate, timeline_saved="feedback")


def _record_schedule_from_invite(candidate, round_key, interview_date, interview_time, teams_link, record_flag):
    """The 'send interview link' windows are where interviews actually get scheduled today:
    keep a structured record of each one (and tick that round's Scheduled stage). A problem
    here must never block the invite itself, so errors are swallowed."""
    try:
        db.schedule_interview(
            candidate["id"], round_key,
            scheduled_for=timeline.parse_interview_datetime(interview_date, interview_time),
            scheduled_for_text=f"{interview_date} {interview_time}".strip(),
            mode="Teams", link=teams_link, record_flag=record_flag, scheduled_by=session["user_id"],
        )
    except Exception:  # noqa: BLE001
        pass


@app.route("/candidates/<int:candidate_id>/score-breakdown.json")
def candidate_score_breakdown(candidate_id):
    """How a resume's overall score was made up: the parameters it was scored on (name,
    weight %, score /10, one-line evidence) plus the summary, strengths and gaps. A
    candidate scored before breakdowns existed has none (has_breakdown = false) but still
    returns its overall score, summary, strengths and gaps. Login required, and a
    non-admin only reaches candidates of positions they are assigned to (the same
    per-hiring access control as every other candidate endpoint)."""
    c = db.get_candidate(candidate_id)
    if not c:
        abort(404)
    try:
        parameters = json.loads(c["score_breakdown_json"]) if c["score_breakdown_json"] else []
    except ValueError:
        parameters = []
    if not isinstance(parameters, list):
        parameters = []
    return jsonify(
        {
            "candidate_id": c["id"],
            "candidate_name": c["candidate_name"] or c["resume_filename"],
            "scored": c["score"] is not None,
            "score": c["score"],
            "bucket": c["bucket"],
            "has_breakdown": bool(parameters),
            "parameters": parameters,
            "summary": c["fit_summary"] or "",
            "positives": json.loads(c["positives_json"]) if c["positives_json"] else [],
            "negatives": json.loads(c["negatives_json"]) if c["negatives_json"] else [],
            "scored_at": c["scored_at"],
        }
    )


# ---- Feature 2: audit log -------------------------------------------------------

@app.route("/audit-log")
def audit_log_view():
    _require_admin()
    user_id = request.args.get("user_id", type=int)
    action = request.args.get("action") or None
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None
    logs = db.list_audit_log(user_id=user_id, action=action, date_from=date_from, date_to=date_to)

    days = []
    day_lookup = {}
    for log in logs:
        try:
            created = datetime.strptime(log["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            created = None
        log_date = created.date() if created else None
        today = date.today()
        if log_date == today:
            day_label = "Today"
        elif log_date == today - timedelta(days=1):
            day_label = "Yesterday"
        elif log_date:
            day_label = created.strftime("%A, %d-%m-%Y")
        else:
            day_label = "Unknown date"
        log["time_display"] = created.strftime("%I:%M %p").lstrip("0") if created else ""
        log["action_label"] = db.AUDIT_ACTION_LABELS.get(log["action"], log["action"].replace("_", " ").title())

        if log_date not in day_lookup:
            day_lookup[log_date] = {"label": day_label, "logs": []}
            days.append(day_lookup[log_date])
        day_lookup[log_date]["logs"].append(log)

    return render_template(
        "audit_log.html",
        days=days,
        all_users=db.distinct_audit_actors(),
        all_actions=db.distinct_audit_actions(),
        action_labels=db.AUDIT_ACTION_LABELS,
        filter_user_id=user_id,
        filter_action=action,
        filter_date_from=date_from or "",
        filter_date_to=date_to or "",
    )


# ---- Feature 3: interview & assessment question recommendations ---------------
# Deliberately NOT the existing assessment_questions/assessment_answers/
# assessment_results feature (a formal, candidate-answered, scored
# assessment) - this is a plain pick-and-choose reference list generated for
# the interviewer, never shown to the candidate, never scored, never emailed.

@app.route("/positions/<int:position_id>/interview-questions/generate", methods=["POST"])
def position_interview_questions_generate(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    try:
        result = scorer.generate_interview_question_recommendations(position["jd_text"] or "")
    except scorer.MissingApiKeyError as exc:
        return redirect(url_for("home", position=position_id, reco_error=str(exc)))
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        return redirect(url_for("home", position=position_id, reco_error=f"AI generation failed: {exc}"))
    questions = [{"category": "interview", "text": q} for q in result["interview_questions"]]
    questions += [{"category": "assessment", "text": q} for q in result["assessment_questions"]]
    db.save_interview_recommendations(position_id, None, session["user_id"], questions)
    return redirect(url_for("home", position=position_id))


@app.route("/candidates/<int:candidate_id>/interview-questions/generate", methods=["POST"])
def candidate_interview_questions_generate(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    position = db.get_position(candidate["position_id"])
    name = candidate["candidate_name"] or candidate["resume_filename"]
    try:
        result = scorer.generate_interview_question_recommendations(
            position["jd_text"] or "", resume_text=candidate["resume_text"] or "", candidate_name=name
        )
    except scorer.MissingApiKeyError as exc:
        return _redirect_to_resumes(candidate, reco_error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return _redirect_to_resumes(candidate, reco_error=f"AI generation failed: {exc}")
    questions = [{"category": "interview", "text": q} for q in result["interview_questions"]]
    questions += [{"category": "assessment", "text": q} for q in result["assessment_questions"]]
    db.save_interview_recommendations(candidate["position_id"], candidate_id, session["user_id"], questions)
    return _redirect_to_resumes(candidate)


def _build_activity_heatmap(days=364, position_ids=None):
    """The activity heatmap grid - built in report_data.py so the dashboard and the
    Generate Report PDFs draw the exact same numbers."""
    return report_data.build_activity_heatmap(days=days, position_ids=position_ids)


@app.route("/")
def home():
    user = db.get_user_by_id(session["user_id"])
    is_admin = db.is_admin_user(user)
    # Feature 1 (scoped access): a non-admin assigned user's dashboard/pipeline
    # list only ever includes the position(s) they're assigned to - None means
    # "no restriction" for admins, an empty/short list scopes everyone else.
    allowed_ids = None if is_admin else db.assigned_position_ids_for_user(user["id"])

    positions = db.list_positions(position_ids=allowed_ids)
    selected_id = request.args.get("position", type=int)
    selected = db.get_position(selected_id) if selected_id else None
    if selected_id and not selected:
        abort(404)
    # enforce_position_scope() only inspects URL PATH segments
    # (position_id/candidate_id), so it can't see this position - it's a
    # query string (?position=<id>) on the bare "/" route. Check access here
    # explicitly, or a non-admin could view any position by just changing
    # the query string.
    if selected and not is_admin and selected["id"] not in (allowed_ids or []):
        abort(403)

    overall = None
    funnel_rows = None
    counts = None
    selected_funnel = None
    avg_days_to_hire = None
    stakeholders = None
    activity_heatmap = None
    assignments = None
    assignable_users = None
    interview_recos = None
    days_to_hire_by_position = None
    offer_acceptance = None
    timeline_data = None
    interview_rounds = None
    if selected:
        # the same section as the overall dashboard's last one, scoped to this position
        timeline_data = timeline.build_timeline([selected["id"]])
        selected_funnel = db.funnel_for_position(selected["id"])
        stakeholders = db.get_stakeholders(selected["id"])
        assignments = db.list_position_assignments(selected["id"])
        assignable_users = db.list_users() if is_admin else None
        interview_recos = db.list_position_interview_recommendations(selected["id"])
        if selected["uses_dynamic_rounds"]:
            interview_rounds = db.get_interview_rounds(selected["id"])
    else:
        # Same builder the Generate Overall Report PDF uses, so the two always agree.
        dash = report_data.overall_dashboard_data(position_ids=allowed_ids)
        overall = dash["overall"]
        funnel_rows = dash["funnel_rows"]
        counts = dash["counts"]
        avg_days_to_hire = dash["avg_days_to_hire"]
        activity_heatmap = dash["activity_heatmap"]
        days_to_hire_by_position = dash["days_to_hire_by_position"]
        offer_acceptance = dash["offer_acceptance"]
        timeline_data = dash["timeline"]


    return render_template(
        "home.html",
        positions=positions,
        selected=selected,
        selected_funnel=selected_funnel,
        overall=overall,
        funnel_rows=funnel_rows,
        counts=counts,
        avg_days_to_hire=avg_days_to_hire,
        stakeholders=stakeholders,
        stakeholders_saved=request.args.get("stakeholders_saved", type=int),
        activity_heatmap=activity_heatmap,
        stage_groups=db.stage_groups(),
        status_fields=db.STATUS_FIELDS,
        statuses=db.STATUSES,
        status_labels=db.STATUS_LABELS,
        is_admin=is_admin,
        assignments=assignments,
        assignable_users=assignable_users,
        interview_recos=interview_recos,
        interview_rounds=interview_rounds,
        reco_error=request.args.get("reco_error"),
        assignment_saved=request.args.get("assignment_saved", type=int),
        days_to_hire_by_position=days_to_hire_by_position,
        offer_acceptance=offer_acceptance,
        timeline_data=timeline_data,
    )


# NOTE: the personal to-do routes below (and db.add_task/list_*_tasks, the `tasks` table and its rows) are
# kept as they were, but the dashboard no longer shows the Open Tasks / Completed Tasks cards, so nothing in
# the UI links to them any more.
@app.route("/tasks/add", methods=["POST"])
def add_task_route():
    """Personal to-do list (a common ATS dashboard feature). v1 is
    self-assign only: whoever creates a task is its assignee, no delegation UI."""
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("home"))
    due_date = request.form.get("due_date", "")
    position_id = request.form.get("position_id", type=int)
    user_id = session["user_id"]
    db.add_task(title, due_date, user_id, user_id, position_id=position_id)
    return redirect(url_for("home"))


@app.route("/tasks/<int:task_id>/complete", methods=["POST"])
def complete_task_route(task_id):
    task = db.get_task(task_id)
    if not task:
        abort(404)
    if task["assigned_to_user_id"] != session["user_id"] and not db.is_admin_user(db.get_user_by_id(session["user_id"])):
        abort(403)
    db.complete_task(task_id)
    return redirect(url_for("home"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task_route(task_id):
    task = db.get_task(task_id)
    if not task:
        abort(404)
    if task["assigned_to_user_id"] != session["user_id"] and not db.is_admin_user(db.get_user_by_id(session["user_id"])):
        abort(403)
    db.delete_task(task_id)
    return redirect(url_for("home"))


@app.route("/positions/<int:position_id>/rounds/<int:round_id>/assign", methods=["POST"])
def assign_round_interviewer_route(position_id, round_id):
    round_row = db.get_interview_round(round_id)
    if not round_row or round_row["position_id"] != position_id:
        abort(404)
    user_id = request.form.get("user_id", type=int)
    if user_id:
        db.assign_round_interviewer(round_id, user_id)
    return redirect(url_for("home", position=position_id))


@app.route("/notifications/<int:notification_id>/read", methods=["POST"])
def mark_notification_read_route(notification_id):
    db.mark_notification_read(notification_id, session["user_id"])
    return redirect(request.referrer or url_for("home"))


@app.route("/notifications/mark-all-read", methods=["POST"])
def mark_all_notifications_read_route():
    db.mark_all_notifications_read(session["user_id"])
    return redirect(request.referrer or url_for("home"))


@app.route("/candidates/<int:candidate_id>/rounds/<int:round_id>/toggle", methods=["POST"])
def toggle_candidate_round_status(candidate_id, round_id):
    candidate = db.get_candidate(candidate_id)
    round_row = db.get_interview_round(round_id)
    if not candidate or not round_row or round_row["position_id"] != candidate["position_id"]:
        abort(404)
    field = request.form.get("field") or (request.get_json(silent=True) or {}).get("field")
    if field not in ("scheduled", "attended", "cleared"):
        abort(400)
    db.set_candidate_round_status(candidate_id, round_id, field, actor_user_id=session["user_id"])
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect(url_for(
        "resumes", position_id=candidate["position_id"], candidate=candidate_id,
        q=request.form.get("q", ""), **{"from": request.form.get("from", "")},
    ))


@app.route("/positions/<int:position_id>/status", methods=["POST"])
def set_position_status(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    status = request.form.get("status") or None
    try:
        db.set_manual_status(position_id, status)
    except ValueError:
        abort(400)
    return redirect(url_for("home", position=position_id))


@app.route("/positions/new", methods=["GET", "POST"])
def new_position():
    if request.method == "GET":
        return render_template("position_new.html", error=None)

    title = request.form.get("title", "").strip()
    jd_mode = request.form.get("jd_mode", "upload")
    jd_file = request.files.get("jd_file")
    jd_text_draft = request.form.get("jd_text_draft", "").strip()
    resume_files = [f for f in request.files.getlist("resume_files") if f.filename]

    using_ai_jd = jd_mode == "ai" and jd_text_draft

    if not title or not resume_files or (not using_ai_jd and (not jd_file or not jd_file.filename)):
        return render_template(
            "position_new.html",
            error="Please provide a title, a job description (uploaded or AI-drafted), and at least one resume.",
        )

    if not using_ai_jd:
        jd_ext = os.path.splitext(jd_file.filename)[1].lower()
        if jd_ext not in ALLOWED_JD_EXT:
            return render_template(
                "position_new.html", error="JD must be a .docx, .pdf, or .txt file."
            )
        jd_filename = jd_file.filename
    else:
        jd_ext = ".txt"
        jd_filename = f"{secure_filename(title) or 'jd'} - AI Drafted JD.txt"

    position_id = db.create_position(title, jd_filename, "")
    db.set_position_dates(
        position_id,
        request.form.get("start_date", "").strip(),
        request.form.get("target_end_date", "").strip(),
    )
    round_labels = [r for r in request.form.getlist("round_label") if r.strip()]
    db.set_uses_dynamic_rounds(position_id, True)
    db.create_interview_rounds(position_id, round_labels)
    pos_dir = db.position_dir(position_id)

    jd_path = os.path.join(pos_dir, "jd_source" + jd_ext)
    if using_ai_jd:
        with open(jd_path, "w", encoding="utf-8") as f:
            f.write(jd_text_draft)
        jd_text = jd_text_draft
    else:
        jd_file.save(jd_path)
        jd_text = resume_parser.extract_text(jd_path)

    conn = db.get_conn()
    conn.execute("UPDATE positions SET jd_text = ? WHERE id = ?", (jd_text, position_id))
    conn.commit()
    conn.close()

    _save_resumes(position_id, resume_files)

    return redirect(url_for("home", position=position_id))


def _ingest_one_resume(position_id, resumes_dir, used_names, orig_filename, write_fn, watched_source_path=None):
    """Shared single-file ingestion: dedupe the filename against what's already on
    disk, write it via write_fn(dest_path), parse it, and create the candidate row.
    write_fn is either a Werkzeug FileStorage's .save (manual upload) or a plain
    filesystem copy (folder watcher) - the rest of the pipeline doesn't care which."""
    ext = os.path.splitext(orig_filename)[1].lower()
    if ext not in ALLOWED_RESUME_EXT:
        return None
    base_name = secure_filename(os.path.basename(orig_filename))
    name = base_name
    counter = 1
    while name in used_names:
        stem, ext2 = os.path.splitext(base_name)
        name = f"{stem}_{counter}{ext2}"
        counter += 1
    used_names.add(name)

    resume_path = os.path.join(resumes_dir, name)
    write_fn(resume_path)
    resume_text = resume_parser.extract_text(resume_path)
    candidate_name = resume_parser.guess_candidate_name(orig_filename, resume_text)
    return db.add_candidate(
        position_id, orig_filename, resume_path, candidate_name, resume_text,
        watched_source_path=watched_source_path,
    )


def _save_resumes(position_id, resume_files):
    """Parses and stores each uploaded resume as a new candidate on this position -
    shared by position creation and by adding more resumes to an already-open one,
    so a position never has to be "done" collecting candidates just because its
    initial batch was scored already."""
    pos_dir = db.position_dir(position_id)
    resumes_dir = os.path.join(pos_dir, "resumes")
    os.makedirs(resumes_dir, exist_ok=True)
    used_names = set(os.listdir(resumes_dir)) if os.path.isdir(resumes_dir) else set()
    added = 0
    for f in resume_files:
        candidate_id = _ingest_one_resume(position_id, resumes_dir, used_names, f.filename, f.save)
        if candidate_id:
            added += 1
    return added


def ingest_watched_file(position_id, resumes_dir, used_names, src_path):
    """Same ingestion pipeline as a manual upload, but copying from a real path on
    disk (the linked folder) instead of a Werkzeug FileStorage. Used by folder_watch.py."""
    return _ingest_one_resume(
        position_id, resumes_dir, used_names, os.path.basename(src_path),
        lambda dest: shutil.copy2(src_path, dest),
        watched_source_path=src_path,
    )


@app.route("/positions/<int:position_id>/resumes/add", methods=["POST"])
def add_resumes(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    resume_files = [f for f in request.files.getlist("resume_files") if f.filename]
    added = _save_resumes(position_id, resume_files)
    return redirect(url_for("resumes", position_id=position_id, added=added))


@app.route("/positions/<int:position_id>/browse-and-link-folder", methods=["POST"])
def browse_and_link_folder(position_id):
    """The single "+ Add Resumes" button: pops a native Windows folder-picker
    dialog on the server (works because the server and the browser are the same
    machine here - a browser file input can never hand back a real filesystem
    path, which is what the background watcher in folder_watch.py needs). The
    chosen folder becomes this position's linked/watched folder, and every
    resume already in it is ingested immediately rather than waiting for the
    next poll."""
    position = db.get_position(position_id)
    if not position:
        abort(404)

    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title="Select a folder of resumes to watch")
    root.destroy()

    if not folder:
        return redirect(url_for("resumes", position_id=position_id))

    folder = os.path.normpath(folder)
    db.set_watch_folder(position_id, folder)

    pos_dir = db.position_dir(position_id)
    resumes_dir = os.path.join(pos_dir, "resumes")
    os.makedirs(resumes_dir, exist_ok=True)
    used_names = set(os.listdir(resumes_dir)) if os.path.isdir(resumes_dir) else set()
    added = 0
    for name in sorted(os.listdir(folder)):
        src = os.path.join(folder, name)
        if os.path.isfile(src) and os.path.splitext(name)[1].lower() in ALLOWED_RESUME_EXT:
            if ingest_watched_file(position_id, resumes_dir, used_names, src):
                added += 1

    return redirect(url_for("resumes", position_id=position_id, added=added))


@app.route("/positions/<int:position_id>/candidates/import", methods=["POST"])
def import_candidates_route(position_id):
    """Bulk candidate import from a .csv/.xlsx (a common ATS feature).
    Additive to the normal resume-upload/watched-
    folder flow, not a replacement for it - for adding candidates from plain
    contact data with no resume file per person."""
    position = db.get_position(position_id)
    if not position:
        abort(404)
    file = request.files.get("import_file")
    if not file or not file.filename:
        return redirect(url_for("resumes", position_id=position_id, import_error="No file selected."))
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMPORT_EXT:
        return redirect(url_for("resumes", position_id=position_id, import_error="File must be .csv or .xlsx."))

    rows = candidate_import.parse_rows(file, ext)
    added = skipped_dupe = skipped_no_name = 0
    for row in rows:
        if not row["name"]:
            skipped_no_name += 1
            continue
        if db.add_imported_candidate(position_id, row) == "added":
            added += 1
        else:
            skipped_dupe += 1

    return redirect(url_for(
        "resumes", position_id=position_id,
        imported=added, import_skipped_dupe=skipped_dupe, import_skipped_no_name=skipped_no_name,
    ))


@app.route("/positions/<int:position_id>/watch-folder/unlink", methods=["POST"])
def unlink_watch_folder(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    db.set_watch_folder(position_id, None)
    return redirect(url_for("resumes", position_id=position_id))


@app.route("/candidates/<int:candidate_id>/stop-tracking", methods=["POST"])
def stop_tracking_candidate(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    db.stop_tracking_watched_candidate(candidate_id)
    return redirect(url_for("resumes", position_id=candidate["position_id"], candidate=candidate_id))


@app.route("/positions/<int:position_id>/resumes")
def resumes(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    query = request.args.get("q", "").strip()
    origin = request.args.get("from", "")
    candidates = db.list_candidates(position_id, query=query or None)
    selected_id = request.args.get("candidate", type=int)
    selected = db.get_candidate(selected_id) if selected_id else None
    if selected and selected["position_id"] != position_id:
        selected = None
    onboarding_items = db.get_onboarding_items(selected["id"]) if selected and selected["joined"] else None
    selected_notes = db.get_candidate_notes(selected["id"]) if selected else None
    selected_scorecards = db.get_candidate_scorecards(selected["id"]) if selected else None
    for sc in selected_scorecards or []:
        sc["positives"] = json.loads(sc["positives_json"]) if sc["positives_json"] else []
        sc["negatives"] = json.loads(sc["negatives_json"]) if sc["negatives_json"] else []
    resume_file_missing = bool(selected and not os.path.exists(selected["resume_path"]))
    selected_ratings = db.list_candidate_ratings(selected["id"]) if selected else None
    selected_interview_recos = db.list_candidate_interview_recommendations(selected["id"]) if selected else None
    interview_rounds = None
    candidate_round_statuses = None
    if selected and position["uses_dynamic_rounds"]:
        interview_rounds = db.get_interview_rounds(position_id)
        candidate_round_statuses = db.get_candidate_round_statuses(selected["id"])
    selected_timeline = timeline.candidate_timeline(selected["id"]) if selected else None
    selected_interviews = db.list_candidate_interviews(selected["id"]) if selected else None
    return render_template(
        "resumes.html",
        selected_timeline=selected_timeline,
        selected_interviews=selected_interviews,
        interview_round_options=db.interview_round_options(position) if selected else None,
        feedback_recommendations=db.FEEDBACK_RECOMMENDATIONS,
        interview_modes=db.INTERVIEW_MODES,
        timeline_error=request.args.get("timeline_error"),
        timeline_saved=request.args.get("timeline_saved"),
        timeline_targets=db.get_timeline_targets(),
        position=position,
        candidates=candidates,
        selected=selected,
        query=query,
        origin=origin,
        onboarding_items=onboarding_items,
        selected_notes=selected_notes,
        selected_scorecards=selected_scorecards,
        selected_ratings=selected_ratings,
        selected_interview_recos=selected_interview_recos,
        interview_rounds=interview_rounds,
        candidate_round_statuses=candidate_round_statuses,
        resume_file_missing=resume_file_missing,
        offer_status_labels=db.OFFER_STATUS_LABELS,
        added=request.args.get("added", type=int),
        imported=request.args.get("imported", type=int),
        import_skipped_dupe=request.args.get("import_skipped_dupe", type=int),
        import_skipped_no_name=request.args.get("import_skipped_no_name", type=int),
        import_error=request.args.get("import_error"),
        scorecard_error=request.args.get("scorecard_error"),
        rating_error=request.args.get("rating_error"),
        reco_error=request.args.get("reco_error"),
    )


def _jd_file_path(position):
    ext = os.path.splitext(position["jd_filename"] or "")[1].lower() or ".docx"
    return os.path.join(db.position_dir(position["id"]), "jd_source" + ext)


def _save_stakeholders_from_form(position_id, form):
    """Reads stakeholder_<level>_name/_email pairs off a submitted form and
    upserts or clears each round's assignment - shared by the Add Position
    wizard and the position page's own edit form, since who runs a given
    round can change after the position is already open."""
    for level in db.STAKEHOLDER_LEVELS:
        name = form.get(f"stakeholder_{level}_name", "").strip()
        email = form.get(f"stakeholder_{level}_email", "").strip()
        if name and email:
            db.set_stakeholder(position_id, level, name, email)
        elif not name and not email:
            db.delete_stakeholder(position_id, level)


@app.route("/positions/<int:position_id>/stakeholders", methods=["POST"])
def update_stakeholders(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    _save_stakeholders_from_form(position_id, request.form)
    return redirect(url_for("home", position=position_id, stakeholders_saved=1))


@app.route("/positions/<int:position_id>/chatbot/ask", methods=["POST"])
def chatbot_ask(position_id):
    """Feature 4 (sourcing/screening chatbot): recruiter-facing Q&A grounded in
    this position's actual JD text and candidate list/scores already in the DB -
    no external lookups. history/messages live only in the browser tab (a JS
    array), nothing is persisted server-side beyond the current page session."""
    position = db.get_position(position_id)
    if not position:
        abort(404)
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    history = payload.get("history") or []
    if not question:
        return jsonify({"error": "Type a question first."}), 400

    candidates = db.list_candidates(position_id)
    lines = []
    for c in candidates:
        name = c["candidate_name"] or c["resume_filename"]
        score = f"{c['score']:.1f}/10" if c["score"] is not None else "not scored"
        summary = (c["fit_summary"] or "").strip()
        lines.append(f"- {name}: {score}, bucket={c['bucket']}" + (f" — {summary}" if summary else ""))
    candidates_context = "\n".join(lines) if lines else "(No candidates added to this position yet.)"

    try:
        answer = scorer.chatbot_answer(position["jd_text"] or "", candidates_context, history, question)
    except Exception as exc:  # noqa: BLE001 - surfaced to the AJAX caller
        return _llm_ajax_error(exc)
    return jsonify({"answer": answer})


@app.route("/positions/<int:position_id>/assessment")
def assessment_page(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    questions = db.get_assessment_questions(position_id)
    candidates = db.list_candidates(position_id)
    return render_template(
        "assessment.html",
        position=position,
        questions=questions,
        candidates=candidates,
    )


@app.route("/positions/<int:position_id>/assessment/generate", methods=["POST"])
def assessment_generate(position_id):
    """Drafts N screening questions from the JD - returned for the recruiter to
    review/edit, NOT saved yet. Only assessment_save persists a question set,
    so a bad/half-finished draft never becomes what candidates are actually
    asked."""
    position = db.get_position(position_id)
    if not position:
        abort(404)
    payload = request.get_json(silent=True) or {}
    n = payload.get("n") or 5
    try:
        n = max(1, min(int(n), 10))
    except (TypeError, ValueError):
        n = 5
    try:
        questions = scorer.generate_assessment_questions(position["jd_text"] or "", n=n)
    except Exception as exc:  # noqa: BLE001 - surfaced to the AJAX caller
        return _llm_ajax_error(exc)
    return jsonify({"questions": questions})


@app.route("/positions/<int:position_id>/assessment/save", methods=["POST"])
def assessment_save(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    questions = [q for q in request.form.getlist("question_text") if q.strip()]
    if questions:
        db.replace_assessment_questions(position_id, questions)
    return redirect(url_for("assessment_page", position_id=position_id))


@app.route("/candidates/<int:candidate_id>/assessment", methods=["GET", "POST"])
def candidate_assessment(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    position = db.get_position(candidate["position_id"])
    questions = db.get_assessment_questions(position["id"])

    if request.method == "POST" and questions:
        for q in questions:
            answer = request.form.get(f"answer_{q['id']}", "")
            db.save_assessment_answer(candidate_id, q["id"], answer)
        return redirect(url_for("candidate_assessment", candidate_id=candidate_id, saved=1))

    answers = db.get_assessment_answers(candidate_id)
    result = db.get_assessment_result(candidate_id)
    if result:
        result["positives"] = json.loads(result["positives_json"]) if result["positives_json"] else []
        result["negatives"] = json.loads(result["negatives_json"]) if result["negatives_json"] else []
    error = request.args.get("error")
    if error == "no_answers":
        error = "Answer at least one question before scoring."
    return render_template(
        "candidate_assessment.html",
        candidate=candidate,
        position=position,
        questions=questions,
        answers=answers,
        result=result,
        error=error,
        saved=request.args.get("saved", type=int),
        scored=request.args.get("scored", type=int),
    )


@app.route("/candidates/<int:candidate_id>/assessment/score", methods=["POST"])
def candidate_assessment_score(candidate_id):
    """Scores the candidate's saved answers with the LLM, in the same shape as
    resume scoring (score/summary/positives/negatives), and saves the result
    immediately - matching the existing "Score" convention elsewhere in this
    app (an evaluation, not a draft that needs a separate confirm step)."""
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    position = db.get_position(candidate["position_id"])
    questions = db.get_assessment_questions(position["id"])
    answers = db.get_assessment_answers(candidate_id)
    if not questions or not any((answers.get(q["id"]) or "").strip() for q in questions):
        return redirect(url_for("candidate_assessment", candidate_id=candidate_id, error="no_answers"))

    qa_pairs = [(q["question_text"], answers.get(q["id"], "")) for q in questions]
    try:
        result = scorer.score_assessment_answers(position["jd_text"] or "", qa_pairs)
    except scorer.MissingApiKeyError as exc:
        return redirect(url_for("candidate_assessment", candidate_id=candidate_id, error=str(exc)))
    except Exception as exc:  # noqa: BLE001
        return redirect(url_for("candidate_assessment", candidate_id=candidate_id, error=f"AI scoring failed: {exc}"))

    db.save_assessment_result(
        candidate_id, position["id"], result["score"], result["summary"], result["positives"], result["negatives"]
    )
    return redirect(url_for("candidate_assessment", candidate_id=candidate_id, scored=1))


@app.route("/candidates/<int:candidate_id>/scorecard/generate", methods=["POST"])
def candidate_scorecard_generate(candidate_id):
    """Feature 5 (LLM-assisted interview scorecards): turns this candidate's
    existing Quick Capture notes into a structured scorecard summary. Generated
    and saved immediately (same convention as resume/assessment scoring), and
    kept as an append-only log like candidate_notes so regenerating never loses
    an earlier scorecard."""
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    notes = db.get_candidate_notes(candidate_id)
    if not notes:
        return _redirect_to_resumes(candidate)

    position = db.get_position(candidate["position_id"])
    notes_text = "\n".join(f"[{n['created_at']}] {n['text']}" for n in notes)
    name = candidate["candidate_name"] or candidate["resume_filename"]
    try:
        result = scorer.generate_scorecard_from_notes(position["jd_text"] or "", name, notes_text)
    except scorer.MissingApiKeyError as exc:
        return _redirect_to_resumes(candidate, scorecard_error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced to the recruiter, not swallowed
        return _redirect_to_resumes(candidate, scorecard_error=f"AI scorecard generation failed: {exc}")

    db.save_candidate_scorecard(
        candidate_id, result["score"], result["summary"], result["positives"], result["negatives"], len(notes)
    )
    return _redirect_to_resumes(candidate)


EMAIL_KINDS = ("jd_and_link", "jd_only", "link_only")


def _llm_ajax_error(exc):
    if isinstance(exc, scorer.MissingApiKeyError):
        return jsonify({"error": str(exc)}), 400
    return jsonify({"error": f"AI request failed: {exc}"}), 500


@app.route("/ai/draft-jd", methods=["POST"])
def ai_draft_jd():
    """Feature 1 (AI-drafted job descriptions): drafts JD text from a role title
    + a few bullet points, for the Add Position wizard to pre-fill into an
    editable textarea - never submitted as the real JD without the recruiter
    reviewing/editing it first."""
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    bullets = (payload.get("bullets") or "").strip()
    if not title:
        return jsonify({"error": "Give a role title first."}), 400
    try:
        jd_text = scorer.draft_job_description(title, bullets)
    except Exception as exc:  # noqa: BLE001 - surfaced to the AJAX caller
        return _llm_ajax_error(exc)
    return jsonify({"jd_text": jd_text})


@app.route("/ai/draft-email-note", methods=["POST"])
def ai_draft_email_note():
    """Feature 2 (AI-drafted emails): drafts a short personal note the recruiter
    can review/edit and optionally include in an outgoing JD/interview-link
    email. Does not draft or change the fixed template wording itself,
    and never sends anything - just pre-fills a textarea."""
    payload = request.get_json(silent=True) or {}
    candidate_name = (payload.get("candidate_name") or "the candidate").strip()
    role_name = (payload.get("role_name") or "this role").strip()
    context_label = (payload.get("context") or "a follow-up email").strip()
    try:
        note = scorer.draft_email_note(candidate_name, role_name, context_label)
    except Exception as exc:  # noqa: BLE001 - surfaced to the AJAX caller
        return _llm_ajax_error(exc)
    return jsonify({"note": note})


@app.route("/email-preview")
def email_preview():
    """Live preview for the compose forms - always reads the current .docx
    template file, so editing that file is immediately reflected here too,
    not just at actual send time."""
    kind = request.args.get("kind")
    if kind not in emailer.EMAIL_TEMPLATE_KINDS:
        abort(400)
    user = db.get_user_by_id(session["user_id"])
    role_name = request.args.get("role_name", "")
    candidate_name = request.args.get("candidate_name") or "Candidate Name"
    date = request.args.get("date", "").strip() or "[Date]"
    time_ = request.args.get("time", "").strip() or "[Time]"
    teams_link = request.args.get("teams_link", "").strip() or "[Teams Meeting Link]"
    custom_note = request.args.get("custom_note", "").strip()

    subject = emailer.render_candidate_email_subject(kind, role_name)
    body, template_warning = emailer.render_candidate_email(
        kind,
        candidate_name=candidate_name,
        role_name=role_name,
        sender_name=user["display_name"] or user["username"],
        sender_email=user["email_address"] or "",
        date=date,
        time=time_,
        teams_link=teams_link,
        custom_note=custom_note or None,
    )
    return jsonify({"subject": subject, "body": body, "template_warning": template_warning})


@app.route("/positions/<int:position_id>/send-jd-email", methods=["GET", "POST"])
def send_jd_email(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    user = db.get_user_by_id(session["user_id"])

    if request.method == "GET":
        candidate_ids = [int(x) for x in request.args.get("candidates", "").split(",") if x.strip().isdigit()]
        candidates = [c for c in db.get_candidates_by_ids(candidate_ids) if c["position_id"] == position_id]
        if not candidates:
            abort(400)
        return render_template(
            "send_jd_email.html",
            position=position,
            candidates=candidates,
            has_email_account=bool(user["email_address"] and user["email_app_password"]),
            results=None,
            error=None,
            form=None,
        )

    candidate_ids = [int(x) for x in request.form.getlist("candidate_id")]
    candidates = [c for c in db.get_candidates_by_ids(candidate_ids) if c["position_id"] == position_id]

    call_answered = request.form.get("call_answered") == "yes"
    send_option = request.form.get("send_option", "jd_and_link")
    interview_date = request.form.get("interview_date", "").strip()
    interview_time = request.form.get("interview_time", "").strip()
    teams_link = request.form.get("teams_link", "").strip()
    personal_note = request.form.get("personal_note", "").strip()

    kind = "no_response" if not call_answered else (send_option if send_option in EMAIL_KINDS else "jd_and_link")
    needs_link = kind in ("link_only", "jd_and_link")

    if needs_link and not (interview_date and interview_time and teams_link):
        return render_template(
            "send_jd_email.html",
            position=position,
            candidates=candidates,
            has_email_account=bool(user["email_address"] and user["email_app_password"]),
            results=None,
            error="Please provide the interview date, time, and Teams link.",
            form=request.form,
        )

    needs_jd = kind in ("no_response", "jd_only", "jd_and_link")
    jd_path = _jd_file_path(position)
    attachment = None
    if needs_jd and os.path.exists(jd_path):
        with open(jd_path, "rb") as f:
            attachment = (position["jd_filename"], f.read(), os.path.splitext(jd_path)[1].lstrip("."))

    subject = emailer.render_candidate_email_subject(kind, position["title"])
    sender_name = user["display_name"] or user["username"]
    sender_email = user["email_address"] or ""

    results = []
    for c in candidates:
        to_address = request.form.get(f"email_{c['id']}", "").strip() or c["email"]
        name = c["candidate_name"] or c["resume_filename"]
        if not to_address:
            results.append({"candidate": name, "status": "skipped", "detail": "No email address"})
            db.log_email(c["id"], kind, None, None, subject, user["email_address"], "skipped", "No email address")
            continue
        try:
            body, template_warning = emailer.render_candidate_email(
                kind,
                candidate_name=name,
                role_name=position["title"],
                sender_name=sender_name,
                sender_email=sender_email,
                date=interview_date,
                time=interview_time,
                teams_link=teams_link,
                custom_note=personal_note or None,
            )
            emailer.send_email(user, to_address, subject, body, attachments=[attachment] if attachment else None)
            detail = to_address if not template_warning else f"{to_address} ({template_warning})"
            results.append({"candidate": name, "status": "sent", "detail": detail})
            db.log_email(c["id"], kind, None, to_address, subject, user["email_address"], "sent")
        except emailer.NoEmailAccountError as exc:
            results.append({"candidate": name, "status": "failed", "detail": str(exc)})
            db.log_email(c["id"], kind, None, to_address, subject, user["email_address"], "failed", str(exc))
        except emailer.SendError as exc:
            results.append({"candidate": name, "status": "failed", "detail": str(exc)})
            db.log_email(c["id"], kind, None, to_address, subject, user["email_address"], "failed", str(exc))

    return render_template(
        "send_jd_email.html",
        position=position,
        candidates=candidates,
        has_email_account=bool(user["email_address"] and user["email_app_password"]),
        results=results,
        error=None,
        form=None,
    )


@app.route("/dashboard.json")
def dashboard_json():
    """Powers the dashboard's 6s auto-refresh - must apply the same
    non-admin scoping as home()'s initial render, or the live refresh would
    silently replace a scoped view with company-wide numbers a few seconds
    after page load."""
    user = db.get_user_by_id(session["user_id"])
    allowed_ids = None if db.is_admin_user(user) else db.assigned_position_ids_for_user(user["id"])
    funnel_rows = db.funnel_by_position(position_ids=allowed_ids)
    return jsonify(
        {
            "overall": db.overall_kpis(position_ids=allowed_ids),
            "funnel_rows": funnel_rows,
            "counts": db.status_counts(funnel_rows),
        }
    )


@app.route("/positions/<int:position_id>/candidates.json")
def candidates_json(position_id):
    candidates = db.list_candidates(position_id)
    return jsonify(
        [
            {
                "id": c["id"],
                "bucket": c["bucket"],
                "score": c["score"],
                "candidate_name": c["candidate_name"],
            }
            for c in candidates
        ]
    )


@app.route("/candidates/<int:candidate_id>/detail.json")
def candidate_detail(candidate_id):
    c = db.get_candidate(candidate_id)
    if not c:
        abort(404)
    return jsonify(
        {
            "id": c["id"],
            "candidate_name": c["candidate_name"],
            "score": c["score"],
            "bucket": c["bucket"],
            "fit_summary": c["fit_summary"],
            "positives": json.loads(c["positives_json"]) if c["positives_json"] else [],
            "negatives": json.loads(c["negatives_json"]) if c["negatives_json"] else [],
            "human_review": c["human_review"] or "",
        }
    )


@app.route("/candidates/<int:candidate_id>/human-review", methods=["POST"])
def save_human_review_route(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    payload = request.get_json(silent=True) or {}
    db.save_human_review(candidate_id, payload.get("review", ""))
    return jsonify({"ok": True})


@app.route("/capture/parse", methods=["POST"])
def capture_parse():
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Type something first."}), 400
    result = capture.parse_capture(text, db.all_candidates_for_matching())
    return jsonify(result)


@app.route("/capture/save", methods=["POST"])
def capture_save():
    payload = request.get_json(silent=True) or {}
    candidate_id = payload.get("candidate_id")
    text = (payload.get("text") or "").strip()
    if not candidate_id or not text:
        # Nothing meaningful was typed/selected - not worth an Input Logs
        # entry (there's no candidate or text to show against it).
        return jsonify({"error": "Missing candidate or text."}), 400
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        db.log_capture(
            session["user_id"],
            raw_text=text,
            matched_label=None,
            action_summary="Tried to save this, but the matched candidate no longer exists.",
            status="error",
        )
        return jsonify({"error": "That candidate no longer exists."}), 404
    db.add_candidate_note(candidate_id, text, noted_at=payload.get("parsed_date"), actor_user_id=session["user_id"])

    applied_field = None
    stage_field = payload.get("apply_stage_field")
    if stage_field and stage_field in db.ALL_STAGE_COLUMNS:
        db.update_stage(candidate_id, stage_field, True, actor_user_id=session["user_id"])
        applied_field = stage_field

    action_summary = "Added note"
    if applied_field:
        action_summary += f", marked {capture.stage_field_label(applied_field)}"
    db.log_capture(
        session["user_id"],
        raw_text=text,
        matched_label=candidate["candidate_name"],
        candidate_id=candidate_id,
        action_summary=action_summary,
        status="saved",
    )

    return jsonify({"ok": True, "candidate_name": candidate["candidate_name"], "applied_field": applied_field})


@app.route("/input-logs")
def input_logs_view():
    logs = db.list_capture_logs(session["user_id"])
    today = date.today()
    yesterday = today - timedelta(days=1)
    days = []
    day_lookup = {}
    for log in logs:
        try:
            created = datetime.strptime(log["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            created = None
        log_date = created.date() if created else None
        if log_date == today:
            day_label = "Today"
        elif log_date == yesterday:
            day_label = "Yesterday"
        elif log_date:
            day_label = log_date.strftime("%B %d, %Y")
        else:
            day_label = "Unknown date"
        log["time_display"] = created.strftime("%I:%M %p").lstrip("0") if created else ""

        if log_date not in day_lookup:
            day_lookup[log_date] = {"label": day_label, "logs": []}
            days.append(day_lookup[log_date])
        day_lookup[log_date]["logs"].append(log)

    return render_template("input_logs.html", days=days)


@app.route("/resumes/<int:candidate_id>/file")
def resume_file(candidate_id):
    c = db.get_candidate(candidate_id)
    if not c or not os.path.exists(c["resume_path"]):
        abort(404)
    return send_file(c["resume_path"], download_name=c["resume_filename"])


@app.route("/positions/<int:position_id>/candidates/delete", methods=["POST"])
def delete_candidates_route(position_id):
    payload = request.get_json(silent=True) or {}
    candidate_ids = [int(cid) for cid in payload.get("candidate_ids", [])]
    if not candidate_ids:
        return jsonify({"error": "No candidates selected."}), 400

    candidates = [c for c in db.get_candidates_by_ids(candidate_ids) if c["position_id"] == position_id]
    for c in candidates:
        if os.path.exists(c["resume_path"]):
            os.remove(c["resume_path"])
    db.delete_candidates([c["id"] for c in candidates])
    return jsonify({"ok": True, "deleted": len(candidates)})


def _redirect_to_resumes(candidate, **extra):
    return redirect(
        url_for(
            "resumes",
            position_id=candidate["position_id"],
            candidate=candidate["id"],
            q=request.form.get("q", ""),
            **{"from": request.form.get("from", "")},
            **extra,
        )
    )


@app.route("/candidates/<int:candidate_id>/email", methods=["POST"])
def update_candidate_email_route(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    db.update_candidate_email(candidate_id, request.form.get("email", ""))
    return _redirect_to_resumes(candidate)


@app.route("/candidates/<int:candidate_id>/interview-media", methods=["POST"])
def upload_interview_media(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)

    db.save_interview_media(
        candidate_id,
        interview_video_url=request.form.get("interview_video_url", "").strip(),
        interview_transcript_url=request.form.get("interview_transcript_url", "").strip(),
    )

    return _redirect_to_resumes(candidate)


@app.route("/candidates/<int:candidate_id>/interview-video/delete", methods=["POST"])
def delete_interview_video(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    if candidate["interview_video_path"] and os.path.exists(candidate["interview_video_path"]):
        os.remove(candidate["interview_video_path"])
    db.save_interview_media(
        candidate_id, interview_video_filename=None, interview_video_path=None
    )
    return _redirect_to_resumes(candidate)


@app.route("/candidates/<int:candidate_id>/interview-video/file")
def interview_video_file(candidate_id):
    c = db.get_candidate(candidate_id)
    if not c or not c["interview_video_path"] or not os.path.exists(c["interview_video_path"]):
        abort(404)
    return send_file(c["interview_video_path"], download_name=c["interview_video_filename"])


@app.route("/candidates/<int:candidate_id>/interview-transcript/file")
def interview_transcript_file(candidate_id):
    c = db.get_candidate(candidate_id)
    if not c or not c["interview_transcript_path"] or not os.path.exists(c["interview_transcript_path"]):
        abort(404)
    return send_file(c["interview_transcript_path"], download_name=c["interview_transcript_filename"])


@app.route("/candidates/<int:candidate_id>/goodfit-media", methods=["POST"])
def upload_goodfit_media(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)

    db.save_goodfit_media(
        candidate_id,
        goodfit_video_url=request.form.get("goodfit_video_url", "").strip(),
        goodfit_transcript_url=request.form.get("goodfit_transcript_url", "").strip(),
    )

    return _redirect_to_resumes(candidate)


@app.route("/candidates/<int:candidate_id>/goodfit-video/delete", methods=["POST"])
def delete_goodfit_video(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    if candidate["goodfit_video_path"] and os.path.exists(candidate["goodfit_video_path"]):
        os.remove(candidate["goodfit_video_path"])
    db.save_goodfit_media(candidate_id, goodfit_video_filename=None, goodfit_video_path=None)
    return _redirect_to_resumes(candidate)


@app.route("/candidates/<int:candidate_id>/goodfit-video/file")
def goodfit_video_file(candidate_id):
    c = db.get_candidate(candidate_id)
    if not c or not c["goodfit_video_path"] or not os.path.exists(c["goodfit_video_path"]):
        abort(404)
    return send_file(c["goodfit_video_path"], download_name=c["goodfit_video_filename"])


@app.route("/candidates/<int:candidate_id>/goodfit-transcript/file")
def goodfit_transcript_file(candidate_id):
    c = db.get_candidate(candidate_id)
    if not c or not c["goodfit_transcript_path"] or not os.path.exists(c["goodfit_transcript_path"]):
        abort(404)
    return send_file(c["goodfit_transcript_path"], download_name=c["goodfit_transcript_filename"])


def _start_scoring(position_id, target, args_without_progress):
    existing = _score_progress.get(position_id)
    if existing and existing.get("done", 0) < existing.get("total", 0):
        return None, "Scoring is already running for this position."

    if not db.get_llm_settings()["llm_api_key"]:
        return None, "No LLM API key set. Add one under Settings before scoring."

    progress = {"total": 0, "done": 0, "error": None}
    _score_progress[position_id] = progress
    thread = threading.Thread(target=target, args=(*args_without_progress, progress), daemon=True)
    thread.start()
    return progress, None


@app.route("/positions/<int:position_id>/score-all", methods=["POST"])
def score_all(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    _, error = _start_scoring(position_id, scorer.score_all_for_position, (position_id,))
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"started": True})


@app.route("/positions/<int:position_id>/score-selected", methods=["POST"])
def score_selected(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    payload = request.get_json(silent=True) or {}
    candidate_ids = [int(cid) for cid in payload.get("candidate_ids", [])]
    if not candidate_ids:
        return jsonify({"error": "No candidates selected."}), 400

    progress, error = _start_scoring(
        position_id, scorer.score_selected_candidates, (position_id, candidate_ids)
    )
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"started": True})


@app.route("/positions/<int:position_id>/score-progress.json")
def score_progress(position_id):
    progress = _score_progress.get(position_id, {"total": 0, "done": 0, "error": None})
    return jsonify(progress)


@app.route("/positions/<int:position_id>/tracker")
def tracker(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    candidates = db.list_candidates(position_id)
    kpis = db.position_kpis(position_id)
    stage_map = db.stage_entered_at_map(position_id)
    timing = {c["id"]: db.candidate_timing(c, stage_map) for c in candidates}
    timeline = {c["id"]: db.candidate_stage_timeline(c, stage_map) for c in candidates}
    funnel_row = db.funnel_for_position(position_id)
    interview_rounds = None
    round_statuses = None
    round_cleared_counts = None
    if position["uses_dynamic_rounds"]:
        interview_rounds = db.get_interview_rounds(position_id)
        round_statuses = db.position_round_statuses(position_id)
        round_cleared_counts = {
            r["id"]: sum(1 for cand in round_statuses.values() if cand.get(r["id"], {}).get("cleared"))
            for r in interview_rounds
        }
    return render_template(
        "tracker.html",
        position=position,
        candidates=candidates,
        kpis=kpis,
        stage_groups=db.stage_groups(),
        status_fields=db.STATUS_FIELDS,
        timing=timing,
        timeline=timeline,
        position_timing=db.position_timing(position_id),
        avg_days_to_hire=db.avg_days_to_hire(position_id),
        deadline=funnel_row["deadline"],
        stakeholders=db.get_stakeholders(position_id),
        interview_rounds=interview_rounds,
        round_statuses=round_statuses,
        round_cleared_counts=round_cleared_counts,
    )


def _calendar_month_args():
    today = date.today()
    year = request.args.get("year", type=int) or today.year
    month = request.args.get("month", type=int) or today.month
    return year, month


@app.route("/positions/<int:position_id>/calendar/month.json")
def position_calendar_month(position_id):
    year, month = _calendar_month_args()
    return jsonify({"active_dates": db.calendar_active_dates(position_id, year, month)})


@app.route("/positions/<int:position_id>/calendar/day.json")
def position_calendar_day(position_id):
    day = request.args.get("date", "")
    return jsonify(db.calendar_day_summary(position_id, day))


def _current_user_allowed_position_ids():
    """None for an admin (unrestricted); a (possibly empty) list of position
    ids for a non-admin assigned user."""
    user = db.get_user_by_id(session["user_id"])
    if db.is_admin_user(user):
        return None
    return db.assigned_position_ids_for_user(user["id"])


@app.route("/calendar/month.json")
def overall_calendar_month():
    year, month = _calendar_month_args()
    return jsonify({"active_dates": db.calendar_active_dates(_current_user_allowed_position_ids(), year, month)})


@app.route("/calendar/day.json")
def overall_calendar_day():
    day = request.args.get("date", "")
    return jsonify(db.calendar_day_summary(_current_user_allowed_position_ids(), day))


@app.route("/positions/<int:position_id>/tracker/export")
def tracker_export_download(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    path = tracker_export.build_tracker(position_id)
    safe_title = secure_filename(position["title"]) or "tracker"
    return send_file(path, download_name=f"{safe_title}_tracker.xlsx", as_attachment=True)


@app.route("/positions/<int:position_id>/tracker/export-to-desktop", methods=["POST"])
def tracker_export_to_desktop(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    try:
        path = tracker_export.export_to_desktop(position_id)
    except OSError as exc:
        return jsonify({"error": f"Couldn't write to Desktop: {exc}"}), 500
    return jsonify({"ok": True, "path": str(path), "filename": path.name})


@app.route("/positions/<int:position_id>/report.pdf")
def position_report_pdf(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)

    if branded_report.is_available() and font_installer.is_installed():
        try:
            pdf_bytes, title = branded_report.generate_position_report(position_id)
            buffer = io.BytesIO(pdf_bytes)
        except Exception:
            buffer, title = report_generator.generate_position_report(position_id)
    else:
        buffer, title = report_generator.generate_position_report(position_id)

    safe_title = secure_filename(title) or "position"
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"Hiring Report - {safe_title}.pdf",
    )


@app.route("/reports/overall.pdf")
def overall_report_pdf():
    # Same scoping as the dashboard itself: a non-admin only gets their assigned positions.
    scope = {"position_ids": _current_user_allowed_position_ids()}
    if branded_report.is_available() and font_installer.is_installed():
        try:
            buffer = io.BytesIO(branded_report.generate_overall_report(**scope))
        except Exception:
            buffer = report_generator.generate_overall_report(**scope)
    else:
        buffer = report_generator.generate_overall_report(**scope)

    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="Hiring Report - Overall.pdf",
    )


@app.route("/positions/<int:position_id>/delete", methods=["POST"])
def delete_position_route(position_id):
    position = db.get_position(position_id)
    if not position:
        abort(404)
    db.delete_position(position_id)
    _score_progress.pop(position_id, None)
    pos_dir = os.path.join(db.DATA_DIR, "positions", str(position_id))
    shutil.rmtree(pos_dir, ignore_errors=True)
    return redirect(url_for("home"))


@app.route("/candidates/<int:candidate_id>/stage", methods=["POST"])
def update_candidate_stage(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    payload = request.get_json(silent=True) or {}
    field = payload.get("field")
    value = bool(payload.get("value"))
    try:
        db.update_stage(candidate_id, field, value, actor_user_id=session["user_id"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "kpis": db.position_kpis(candidate["position_id"])})


def _notify_stakeholder(user, position, candidate_id, candidate_name, round_key, interview_date, interview_time, teams_link):
    """Best-effort email to whoever is assigned to conduct this round, if anyone
    is - a failure here (or no stakeholder assigned) never blocks the candidate
    invite itself. Returns a short status string to fold into the UI message."""
    stakeholder = db.get_stakeholders(position["id"]).get(round_key)
    if not stakeholder:
        return None
    round_label = round_key.upper()
    subject = f"{round_label} Interview Scheduled — {candidate_name} ({position['title']})"
    body = emailer.render_stakeholder_notification_email(
        stakeholder["name"], candidate_name, position["title"], round_label,
        interview_date, interview_time, teams_link,
    )
    try:
        emailer.send_email(user, stakeholder["email"], subject, body)
        db.log_email(candidate_id, "stakeholder_notification", round_key, stakeholder["email"], subject, user["email_address"], "sent")
        return f"Notified {stakeholder['name']} ({stakeholder['email']})."
    except (emailer.NoEmailAccountError, emailer.SendError) as exc:
        db.log_email(candidate_id, "stakeholder_notification", round_key, stakeholder["email"], subject, user["email_address"], "failed", str(exc))
        return f"Could not notify stakeholder {stakeholder['name']}: {exc}"


@app.route("/candidates/<int:candidate_id>/send-teams-link", methods=["POST"])
def send_teams_link(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    payload = request.get_json(silent=True) or {}
    round_key = payload.get("round")
    teams_link = (payload.get("teams_link") or "").strip()
    interview_date = (payload.get("date") or "").strip()
    interview_time = (payload.get("time") or "").strip()
    personal_note = (payload.get("note") or "").strip()

    if round_key not in ("l1", "l2", "l3") or not teams_link or not interview_date or not interview_time:
        return jsonify({"ok": False, "message": "Missing round, date, time, or link."}), 400

    db.set_candidate_teams_link(candidate_id, round_key, teams_link)
    _record_schedule_from_invite(candidate, round_key, interview_date, interview_time, teams_link, bool(payload.get("record")))

    position = db.get_position(candidate["position_id"])
    user = db.get_user_by_id(session["user_id"])
    name = candidate["candidate_name"] or candidate["resume_filename"]
    subject = emailer.render_candidate_email_subject("link_only", position["title"])

    stakeholder_note = _notify_stakeholder(user, position, candidate_id, name, round_key, interview_date, interview_time, teams_link)

    if not candidate["email"]:
        db.log_email(candidate_id, "link_only", round_key, None, subject, user["email_address"], "skipped", "No email address")
        message = "Link saved, but this candidate has no email address on file."
        if stakeholder_note:
            message += f" {stakeholder_note}"
        return jsonify({"ok": False, "message": message})

    try:
        body, template_warning = emailer.render_candidate_email(
            "link_only",
            candidate_name=name,
            role_name=position["title"],
            sender_name=user["display_name"] or user["username"],
            sender_email=user["email_address"] or "",
            date=interview_date,
            time=interview_time,
            teams_link=teams_link,
            custom_note=personal_note or None,
        )
        emailer.send_email(user, candidate["email"], subject, body)
        db.log_email(candidate_id, "link_only", round_key, candidate["email"], subject, user["email_address"], "sent")
        message = f"Sent to {candidate['email']}."
        if template_warning:
            message += f" ({template_warning})"
        if stakeholder_note:
            message += f" {stakeholder_note}"
        return jsonify({"ok": True, "message": message})
    except (emailer.NoEmailAccountError, emailer.SendError) as exc:
        db.log_email(candidate_id, "link_only", round_key, candidate["email"], subject, user["email_address"], "failed", str(exc))
        message = f"Link saved, but sending failed: {exc}"
        if stakeholder_note:
            message += f" {stakeholder_note}"
        return jsonify({"ok": False, "message": message})


@app.route("/candidates/<int:candidate_id>/rounds/<int:round_id>/send-link", methods=["POST"])
def send_round_teams_link(candidate_id, round_id):
    """Dynamic-rounds equivalent of send_teams_link() above - same candidate
    email + stakeholder-notification flow, keyed by round_id instead of a
    fixed l1/l2/l3 string."""
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    round_row = db.get_interview_round(round_id)
    if not round_row or round_row["position_id"] != candidate["position_id"]:
        abort(404)
    payload = request.get_json(silent=True) or {}
    teams_link = (payload.get("teams_link") or "").strip()
    interview_date = (payload.get("date") or "").strip()
    interview_time = (payload.get("time") or "").strip()
    personal_note = (payload.get("note") or "").strip()

    if not teams_link or not interview_date or not interview_time:
        return jsonify({"ok": False, "message": "Missing date, time, or link."}), 400

    db.set_candidate_round_teams_link(candidate_id, round_id, teams_link)
    _record_schedule_from_invite(candidate, f"r{round_id}", interview_date, interview_time, teams_link, bool(payload.get("record")))

    position = db.get_position(candidate["position_id"])
    user = db.get_user_by_id(session["user_id"])
    name = candidate["candidate_name"] or candidate["resume_filename"]
    round_label = round_row["round_label"]
    subject = emailer.render_candidate_email_subject("link_only", position["title"])

    stakeholder_note = None
    if round_row["assigned_user_id"]:
        assignee = db.get_user_by_id(round_row["assigned_user_id"])
        if assignee:
            notif_subject = f"{round_label} Interview Scheduled — {name} ({position['title']})"
            notif_body = emailer.render_stakeholder_notification_email(
                assignee["display_name"] or assignee["username"], name, position["title"], round_label,
                interview_date, interview_time, teams_link,
            )
            try:
                emailer.send_email(user, assignee["email_address"], notif_subject, notif_body)
                db.log_email(candidate_id, "stakeholder_notification", round_label, assignee["email_address"], notif_subject, user["email_address"], "sent")
                stakeholder_note = f"Notified {assignee['display_name'] or assignee['username']} ({assignee['email_address']})."
            except (emailer.NoEmailAccountError, emailer.SendError) as exc:
                db.log_email(candidate_id, "stakeholder_notification", round_label, assignee["email_address"], notif_subject, user["email_address"], "failed", str(exc))
                stakeholder_note = f"Could not notify {assignee['display_name'] or assignee['username']}: {exc}"

    if not candidate["email"]:
        db.log_email(candidate_id, "link_only", round_label, None, subject, user["email_address"], "skipped", "No email address")
        message = "Link saved, but this candidate has no email address on file."
        if stakeholder_note:
            message += f" {stakeholder_note}"
        return jsonify({"ok": False, "message": message})

    try:
        body, template_warning = emailer.render_candidate_email(
            "link_only",
            candidate_name=name,
            role_name=position["title"],
            sender_name=user["display_name"] or user["username"],
            sender_email=user["email_address"] or "",
            date=interview_date,
            time=interview_time,
            teams_link=teams_link,
            custom_note=personal_note or None,
        )
        emailer.send_email(user, candidate["email"], subject, body)
        db.log_email(candidate_id, "link_only", round_label, candidate["email"], subject, user["email_address"], "sent")
        message = f"Sent to {candidate['email']}."
        if template_warning:
            message += f" ({template_warning})"
        if stakeholder_note:
            message += f" {stakeholder_note}"
        return jsonify({"ok": True, "message": message})
    except (emailer.NoEmailAccountError, emailer.SendError) as exc:
        db.log_email(candidate_id, "link_only", round_label, candidate["email"], subject, user["email_address"], "failed", str(exc))
        message = f"Link saved, but sending failed: {exc}"
        if stakeholder_note:
            message += f" {stakeholder_note}"
        return jsonify({"ok": False, "message": message})


@app.route("/candidates/<int:candidate_id>/offer", methods=["GET", "POST"])
def offer_letter_route(candidate_id):
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        abort(404)
    position = db.get_position(candidate["position_id"])
    user = db.get_user_by_id(session["user_id"])
    error = None
    message = None

    if request.method == "POST":
        action = request.form.get("action")
        db.set_offer_fields(
            candidate_id,
            request.form.get("designation", "").strip(),
            request.form.get("ctc", "").strip(),
            request.form.get("joining_date", "").strip(),
            request.form.get("notes", "").strip(),
            request.form.get("approver_email", "").strip(),
            expiry_date=request.form.get("expiry_date", "").strip(),
            employment_type=request.form.get("employment_type", "").strip(),
        )
        candidate = db.get_candidate(candidate_id)
        name = candidate["candidate_name"] or candidate["resume_filename"]

        if action == "send_for_approval":
            if not candidate["offer_approver_email"]:
                error = "Add the approver's email address first."
            elif not candidate["offer_designation"] or not candidate["offer_ctc"] or not candidate["offer_joining_date"]:
                error = "Fill in designation, CTC, and joining date first."
            else:
                pdf_bytes, template_warning = offer_letter.generate_offer_letter(candidate, position)
                pdf_dir = db.interview_media_dir(candidate["position_id"], candidate_id)
                pdf_path = os.path.join(pdf_dir, "offer_letter.pdf")
                with open(pdf_path, "wb") as f:
                    f.write(pdf_bytes)
                actor = user["email_address"] or user["username"]
                token = db.send_offer_for_approval(candidate_id, pdf_path, actor=actor)
                approval_url = url_for("offer_approve", token=token, _external=True)
                try:
                    body = emailer.render_offer_approval_request_body(
                        name, position["title"], candidate["offer_designation"], candidate["offer_ctc"],
                        candidate["offer_joining_date"], approval_url,
                    )
                    emailer.send_email(user, candidate["offer_approver_email"], f"Offer Approval Needed — {name}", body)
                    message = f"Sent for approval to {candidate['offer_approver_email']}."
                except (emailer.NoEmailAccountError, emailer.SendError) as exc:
                    message = f"Marked as pending approval, but the email failed to send: {exc}. Approval link: {approval_url}"
                if template_warning:
                    error = template_warning
                candidate = db.get_candidate(candidate_id)

        elif action == "send_to_candidate":
            if candidate["offer_status"] != "approved":
                error = "This offer hasn't been approved yet."
            elif not candidate["email"]:
                error = "This candidate has no email address on file."
            else:
                actor = user["email_address"] or user["username"]
                token = db.mark_offer_sent_to_candidate(candidate_id, actor=actor)
                respond_url = url_for("offer_respond", token=token, _external=True)
                attachment = None
                if candidate["offer_pdf_path"] and os.path.exists(candidate["offer_pdf_path"]):
                    with open(candidate["offer_pdf_path"], "rb") as f:
                        attachment = ("Offer Letter.pdf", f.read(), "pdf")
                custom_message = (
                    request.form.get("candidate_message", "").strip()
                    or "Congratulations! Please find your offer letter attached."
                )
                try:
                    body = emailer.render_offer_to_candidate_body(
                        name, position["title"], custom_message, respond_url,
                        expiry_date=candidate["offer_expiry_date"],
                    )
                    emailer.send_email(
                        user, candidate["email"], f"Your Offer Letter — {position['title']}", body,
                        attachments=[attachment] if attachment else None,
                    )
                    message = f"Offer sent to {candidate['email']}."
                except (emailer.NoEmailAccountError, emailer.SendError) as exc:
                    error = f"Marked as sent, but the email failed to send: {exc}"
                candidate = db.get_candidate(candidate_id)
        else:
            message = "Offer details saved."

    offer_expired = bool(
        candidate["offer_status"] == "sent"
        and candidate["offer_expiry_date"]
        and candidate["offer_expiry_date"] < date.today().isoformat()
    )

    return render_template(
        "offer_letter.html",
        candidate=candidate,
        position=position,
        offer_status_labels=db.OFFER_STATUS_LABELS,
        offer_log=db.offer_log_for_candidate(candidate_id),
        offer_expired=offer_expired,
        error=error,
        message=message,
        origin=request.args.get("from", ""),
        query=request.args.get("q", ""),
    )


@app.route("/offer/approve/<token>", methods=["GET", "POST"])
def offer_approve(token):
    candidate = db.get_candidate_by_offer_token(token)
    if not candidate:
        return render_template("offer_approve.html", candidate=None, position=None, handled=False)
    position = db.get_position(candidate["position_id"])

    if request.method == "POST" and candidate["offer_status"] == "pending_approval":
        action = request.form.get("action")
        actor = candidate["offer_approver_email"]
        if action == "approve":
            db.approve_offer(candidate["id"], actor=actor)
        elif action == "reject":
            db.reject_offer(candidate["id"], request.form.get("reason", "").strip(), actor=actor)
        candidate = db.get_candidate(candidate["id"])

    return render_template(
        "offer_approve.html",
        candidate=candidate,
        position=position,
        handled=candidate["offer_status"] != "pending_approval",
        offer_status_labels=db.OFFER_STATUS_LABELS,
    )


@app.route("/offer/approve/<token>/pdf")
def offer_approve_pdf(token):
    candidate = db.get_candidate_by_offer_token(token)
    if not candidate or not candidate["offer_pdf_path"] or not os.path.exists(candidate["offer_pdf_path"]):
        abort(404)
    return send_file(candidate["offer_pdf_path"], download_name="Offer Letter.pdf")


@app.route("/offer/respond/<token>", methods=["GET", "POST"])
def offer_respond(token):
    candidate = db.get_candidate_by_candidate_token(token)
    if not candidate:
        return render_template("offer_respond.html", candidate=None, position=None, handled=False)
    position = db.get_position(candidate["position_id"])

    if request.method == "POST" and candidate["offer_status"] == "sent":
        action = request.form.get("action")
        if action in ("accept", "decline"):
            db.record_candidate_offer_response(candidate["id"], "accepted" if action == "accept" else "declined")
        candidate = db.get_candidate(candidate["id"])

    return render_template(
        "offer_respond.html",
        candidate=candidate,
        position=position,
        handled=candidate["offer_status"] != "sent",
        offer_status_labels=db.OFFER_STATUS_LABELS,
    )


@app.route("/offer/respond/<token>/pdf")
def offer_respond_pdf(token):
    candidate = db.get_candidate_by_candidate_token(token)
    if not candidate or not candidate["offer_pdf_path"] or not os.path.exists(candidate["offer_pdf_path"]):
        abort(404)
    return send_file(candidate["offer_pdf_path"], download_name="Offer Letter.pdf")


@app.route("/onboarding-items/<int:item_id>/toggle", methods=["POST"])
def toggle_onboarding_item_route(item_id):
    payload = request.get_json(silent=True) or {}
    db.toggle_onboarding_item(item_id, bool(payload.get("completed")))
    return jsonify({"ok": True})


@app.errorhandler(404)
def not_found(_error):
    if request.path.endswith(".json") or request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify({"error": "Not found"}), 404
    return (
        render_template(
            "error.html",
            title="Page not found",
            message="That page doesn't exist anymore - it may have been deleted, or the link is out of date.",
        ),
        404,
    )


@app.errorhandler(500)
def server_error(_error):
    return (
        render_template(
            "error.html",
            title="Something went wrong",
            message="An unexpected error occurred. Try again, and if it keeps happening, check the server log.",
        ),
        500,
    )


if __name__ == "__main__":
    import folder_watch
    import sys

    folder_watch.start(sys.modules[__name__])
    host = os.environ.get("HR_AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("HR_AGENT_PORT", "5000"))
    # Flask's debugger/reloader is on by default for local use, but is never enabled when the
    # app is bound to anything other than the loopback address.
    debug = os.environ.get("HR_AGENT_DEBUG", "1") == "1" and host in ("127.0.0.1", "localhost")
    app.run(host=host, port=port, debug=debug)
