"""Hiring timeline and buffer time.

Turns what the app already records - when the JD was uploaded (positions.created_at),
when each CV was fed in (candidates.created_at), the stage history, the scheduled
interviews and the panel feedback - into elapsed-time numbers ("buffer time") and
over-target flags, for:

  * the dashboard's last section "Hiring Timeline and Buffer Time"
    (one row per position + a candidate-level table, most delayed first),
  * the candidate page,
  * both Generate Report PDFs.

Rules that hold everywhere in this module:
  * Days are CALENDAR days (date minus date, local calendar), shown as "-" when
    either date is missing. A date is never invented.
  * Timestamps stored by SQLite's datetime('now') are UTC; they are converted to the
    machine's local calendar day before any day arithmetic. Interview date/time is
    what the recruiter typed (local), so it is used as-is.
  * A stage-entry date that was only ESTIMATED by the one-time backfill (no real
    stage timestamp existed, the CV-added date stands in) is flagged as an estimate
    wherever it is shown and is left out of the "average days per stage".

compute_timeline() is pure (give it the raw rows and a `today`), which is what the
tests drive; build_timeline() reads the database and calls it.
"""
import re
from datetime import date, datetime, timezone

import db

DASH = "-"

# Stages where waiting is expected / finished: they are never flagged for "sitting in a
# stage too long", and Joined/Rejected are left out of the average-days-per-stage.
STAGE_AGE_EXEMPT_PHASES = ("Joined", "Rejected", "On Hold")
AVERAGE_EXEMPT_PHASES = ("Joined", "Rejected")
DECIDED_PHASES = ("Offer Made", "Joined", "Rejected")


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

def _parse_stored(value):
    """datetime from a stored 'YYYY-MM-DD HH:MM:SS' (UTC) string, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


def local_date(value):
    """The local calendar date of a stored (UTC) timestamp string, or None."""
    dt = _parse_stored(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()


def local_datetime(value):
    dt = _parse_stored(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().replace(tzinfo=None)


def days_between(start, end):
    """Whole calendar days from start to end (both dates), or None if either is missing."""
    if start is None or end is None:
        return None
    return (end - start).days


def fmt_date(value):
    """'06 Oct 2026' for a date / ISO string, or '-'."""
    if value in (None, ""):
        return DASH
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return DASH
    return value.strftime("%d %b %Y")


def fmt_days(value):
    return DASH if value is None else str(value)


def fmt_stored_datetime(value):
    """'06 Oct 2026, 16:10' (local time) for a stored UTC timestamp, or '-'."""
    dt = local_datetime(value)
    return dt.strftime("%d %b %Y, %H:%M") if dt else DASH


def _scheduled_parts(scheduled_for):
    """(date, datetime-or-None) from an interview's scheduled_for ('YYYY-MM-DD HH:MM'
    or 'YYYY-MM-DD'); (None, None) when it was never captured."""
    if not scheduled_for:
        return None, None
    text = str(scheduled_for).strip()
    try:
        d = date.fromisoformat(text[:10])
    except ValueError:
        return None, None
    dt = None
    if len(text) > 10:
        try:
            dt = datetime.fromisoformat(text.replace("T", " "))
        except ValueError:
            dt = None
    return d, dt


def fmt_when(scheduled_for, text=None):
    """Display text for an interview's date/time."""
    d, dt = _scheduled_parts(scheduled_for)
    if dt:
        return dt.strftime("%d %b %Y, %H:%M")
    if d:
        return d.strftime("%d %b %Y")
    return (text or "").strip() or DASH


# ---- reading the free-text date/time typed into the "send interview link" window ----

_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y",
    "%d %b, %Y", "%d %B, %Y", "%b %d, %Y", "%B %d, %Y", "%d-%m-%y", "%d/%m/%y",
)
_TIME_FORMATS = ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M", "%H.%M", "%I.%M %p")
_TZ_WORDS = re.compile(r"\b(IST|UTC|GMT|PST|PDT|EST|EDT|CST|CDT|BST|CET|SGT)\b", re.I)


def parse_interview_datetime(date_text, time_text=None):
    """ISO 'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD' when no usable time) from what a
    recruiter typed, e.g. '20-08-2026' + '3:00 PM IST'. None when the date cannot
    be read - the caller then keeps the raw text instead of guessing."""
    date_text = (date_text or "").strip()
    if not date_text:
        return None
    parsed = None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(date_text, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            import parsedatetime

            result, status = parsedatetime.Calendar().parseDT(date_text)
            if status in (1, 3):
                parsed = result.date()
        except Exception:  # noqa: BLE001 - a missing/odd library just means "unreadable"
            parsed = None
    if parsed is None:
        return None
    clock = _TZ_WORDS.sub("", (time_text or "")).strip()
    for fmt in _TIME_FORMATS:
        try:
            t = datetime.strptime(clock, fmt).time()
            return f"{parsed.isoformat()} {t.strftime('%H:%M')}"
        except ValueError:
            continue
    return parsed.isoformat()


# ---------------------------------------------------------------------------
# stage helpers
# ---------------------------------------------------------------------------

def phase_for_key(stage_key):
    """The coarse stage group for a stage_key stored in candidate_stage_history."""
    if stage_key in ("sourced", "reached", "offer_made", "on_hold", "joined", "rejected"):
        return {"sourced": "Sourced", "reached": "Reached", "offer_made": "Offer Made",
                "on_hold": "On Hold", "joined": "Joined", "rejected": "Rejected"}[stage_key]
    if stage_key.endswith("_scheduled"):
        return "Interview Scheduled"
    if stage_key.endswith("_attended"):
        return "Interviewed"
    if stage_key.endswith("_cleared"):
        return "Round Cleared"
    return stage_key


def is_estimate(history_row):
    """True for a backfilled stage-entry whose date is only the CV-added date standing in."""
    return bool(history_row) and history_row.get("source") == "backfilled" and history_row.get("basis") == "cv_added_proxy"


def _group(rows, key):
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


# ---------------------------------------------------------------------------
# the computation
# ---------------------------------------------------------------------------

def _candidate_interviews(schedules, feedback_by_interview, c, rounds_status, rounds, now, today):
    """Per-interview facts for one candidate, one entry per round that has an interview:
    a round counts if an interview was recorded for it (only the LATEST record of a round
    counts - an earlier one was rescheduled) OR the tracker shows it ticked
    Scheduled/Attended/Cleared (older candidates were only ever ticked, never recorded;
    those have no date and no id, so no panel feedback can be attached until the details
    are recorded)."""
    entries = {}
    if rounds is None:  # legacy fixed L1/L2/L3 positions
        for level in ("l1", "l2", "l3"):
            if any(c.get(f"{level}_{f}") for f in ("scheduled", "attended", "cleared")):
                entries[level] = {
                    "round_label": f"{level.upper()} Interview", "attended": bool(c.get(f"{level}_attended") or c.get(f"{level}_cleared")),
                }
    else:
        for r in rounds:
            st = rounds_status.get(r["id"]) or {}
            if any(st.get(f) for f in ("scheduled", "attended", "cleared")):
                entries[f"r{r['id']}"] = {"round_label": r["round_label"], "attended": bool(st.get("attended") or st.get("cleared"))}

    latest = {}
    for sch in schedules:  # ascending by created_at, so the last one per round wins
        latest[sch["round_key"]] = sch
    for key, sch in latest.items():
        entry = entries.setdefault(key, {"round_label": sch["round_label"], "attended": False})
        entry["schedule"] = sch

    out = []
    for key, entry in entries.items():
        sch = entry.get("schedule")
        d, dt = _scheduled_parts(sch["scheduled_for"]) if sch else (None, None)
        if dt is not None:
            past = dt <= now
        elif d is not None:
            past = d < today
        else:
            past = False
        done = entry["attended"] or past
        fb = feedback_by_interview.get(sch["id"], []) if sch else []
        fb_dates = sorted(x for x in (local_date(f["created_at"]) for f in fb) if x)
        out.append({
            "id": sch["id"] if sch else None, "round_key": key, "round_label": entry["round_label"],
            "when_date": d, "when_dt": dt,
            "when_text": fmt_when(sch["scheduled_for"], sch.get("scheduled_for_text")) if sch else DASH,
            "scheduled_on": local_date(sch["created_at"]) if sch else None,
            "done": done, "feedback_count": len(fb), "first_feedback_on": fb_dates[0] if fb_dates else None,
            "pending_days": days_between(d, today) if (done and not fb) else None,
            "pending": done and not fb,
        })
    out.sort(key=lambda i: (i["when_dt"] or datetime.combine(i["when_date"] or date.max, datetime.min.time())))
    return out


def compute_timeline(raw, targets, today, now=None, position_meta=None):
    """Pure computation - see the module docstring. `raw` is db.timeline_raw_data()'s
    shape, `targets` the {name: days} thresholds, `today` a date, `now` a naive local
    datetime (defaults to today at noon), `position_meta` {position_id: {'status',
    'days_to_fill'}} from the funnel (optional)."""
    now = now or datetime.combine(today, datetime.min.time()).replace(hour=12)
    position_meta = position_meta or {}
    history_by_c = _group(raw["history"], "candidate_id")
    schedules_by_c = _group(raw["schedules"], "candidate_id")
    feedback_by_c = _group(raw["feedback"], "candidate_id")
    feedback_by_interview = _group(raw["feedback"], "interview_id")
    rounds_by_pos = _group(raw["rounds"], "position_id")
    statuses_by_c = {}
    for r in raw["round_statuses"]:
        statuses_by_c.setdefault(r["candidate_id"], {})[r["round_id"]] = r
    positions = {p["id"]: p for p in raw["positions"]}

    stays_by_phase = {}          # overall: phase -> [days]
    stays_by_pos_phase = {}      # (position_id, phase) -> [days]
    candidates_out = []

    for c in raw["candidates"]:
        pos = positions.get(c["position_id"])
        if pos is None:
            continue
        rounds = rounds_by_pos.get(pos["id"], []) if pos.get("uses_dynamic_rounds") else None
        statuses = statuses_by_c.get(c["id"], {})
        key, label, phase = db.timeline_stage(c, rounds, statuses)
        cv_date = local_date(c["created_at"])
        hist = history_by_c.get(c["id"], [])

        entered_row = next((h for h in reversed(hist) if h["stage_key"] == key), None)
        if key == "sourced":
            entered_date, entered_est = cv_date, False
        elif entered_row is not None:
            entered_date, entered_est = local_date(entered_row["entered_at"]), is_estimate(entered_row)
        else:
            entered_date, entered_est = cv_date, True   # no history row for this stage: CV date stands in
        days_in_stage = days_between(entered_date, today)

        # ---- interviews & feedback -------------------------------------------------
        interviews = _candidate_interviews(schedules_by_c.get(c["id"], []), feedback_by_interview, c, statuses, rounds, now, today)
        upcoming = [i for i in interviews if not i["done"]]
        pending = [i for i in interviews if i["pending"]]
        feedback_rows = feedback_by_c.get(c["id"], [])
        fb_dates = sorted(x for x in (local_date(f["created_at"]) for f in feedback_rows) if x)
        first_feedback_on = fb_dates[0] if fb_dates else None

        if upcoming:
            nxt = upcoming[0]
            next_interview = f"{nxt['round_label']} - {nxt['when_text']}" if nxt["when_text"] != DASH else f"{nxt['round_label']} (date not recorded)"
        else:
            next_interview = DASH

        pending_days_list = [i["pending_days"] for i in pending if i["pending_days"] is not None]
        max_pending_days = max(pending_days_list) if pending_days_list else None
        if not interviews:
            feedback_status, feedback_state = DASH, "none"
        elif pending:
            feedback_status = f"Pending {max_pending_days}d" if max_pending_days is not None else "Pending"
            feedback_state = "pending"
        elif feedback_rows:
            feedback_status, feedback_state = f"Saved ({len(feedback_rows)})", "saved"
        else:
            feedback_status, feedback_state = "Awaiting interview", "upcoming"

        # ---- the elapsed-time numbers ---------------------------------------------
        sched_dates = [local_date(s["created_at"]) for s in schedules_by_c.get(c["id"], []) if local_date(s["created_at"])]
        sched_dates += [local_date(h["entered_at"]) for h in hist
                        if h["stage_key"].endswith("_scheduled") and not is_estimate(h) and local_date(h["entered_at"])]
        first_scheduled_on = min(sched_dates) if sched_dates else None
        days_cv_to_schedule = days_between(cv_date, first_scheduled_on)

        fb_gap = None
        for i in interviews:
            if i["first_feedback_on"] and i["when_date"]:
                gap = days_between(i["when_date"], i["first_feedback_on"])
                if gap is not None and gap >= 0:
                    fb_gap = gap if fb_gap is None else min(fb_gap, gap)
        days_interview_to_feedback = fb_gap

        decision_gap = None
        if first_feedback_on:
            for h in hist:
                if h["stage_key"] in db.DECISION_STAGE_KEYS and not is_estimate(h):
                    dd = local_date(h["entered_at"])
                    if dd and dd >= first_feedback_on:
                        decision_gap = (dd - first_feedback_on).days
                        break
        days_feedback_to_decision = decision_gap
        decision_pending_days = None
        if first_feedback_on and phase not in DECIDED_PHASES and phase != "On Hold":
            decision_pending_days = days_between(first_feedback_on, today)

        # ---- over-target ------------------------------------------------------------
        reasons, excess = [], 0
        if phase not in STAGE_AGE_EXEMPT_PHASES and days_in_stage is not None and days_in_stage > targets["stage_age_days"]:
            reasons.append(f"In stage '{label}' {'~' if entered_est else ''}{days_in_stage}d (target {targets['stage_age_days']}d)")
            excess += days_in_stage - targets["stage_age_days"]
        if max_pending_days is not None and max_pending_days > targets["feedback_pending_days"]:
            reasons.append(f"Feedback pending {max_pending_days}d (target {targets['feedback_pending_days']}d)")
            excess += max_pending_days - targets["feedback_pending_days"]
        if decision_pending_days is not None and decision_pending_days > targets["decision_pending_days"]:
            reasons.append(f"No decision {decision_pending_days}d after feedback (target {targets['decision_pending_days']}d)")
            excess += decision_pending_days - targets["decision_pending_days"]
        delay = max(
            (days_in_stage if (phase not in STAGE_AGE_EXEMPT_PHASES and days_in_stage is not None) else -1),
            max_pending_days if max_pending_days is not None else -1,
            decision_pending_days if decision_pending_days is not None else -1,
        )

        # ---- completed/ongoing stays, for the average days per stage ---------------
        for idx, h in enumerate(hist):
            start = local_date(h["entered_at"])
            ph = phase_for_key(h["stage_key"])
            if ph in AVERAGE_EXEMPT_PHASES or start is None or is_estimate(h):
                continue
            if idx + 1 < len(hist):
                nxt_h = hist[idx + 1]
                if is_estimate(nxt_h):
                    continue
                end = local_date(nxt_h["entered_at"])
            else:
                end = today
            if end is None:
                continue
            d = max((end - start).days, 0)
            stays_by_phase.setdefault(ph, []).append(d)
            stays_by_pos_phase.setdefault((pos["id"], ph), []).append(d)

        history_display = [{
            "stage_label": h["stage_label"], "entered_at": h["entered_at"], "source": h["source"],
            "basis": h["basis"], "estimate": is_estimate(h),
        } for h in hist]

        candidates_out.append({
            "candidate_id": c["id"], "position_id": pos["id"], "position_title": pos["title"],
            "name": c["candidate_name"] or c["resume_filename"], "score": c["score"], "bucket": c["bucket"],
            "cv_fed_on": cv_date.isoformat() if cv_date else None,
            "days_since_cv": days_between(cv_date, today),
            "stage_key": key, "stage_label": label, "phase": phase,
            "entered_stage_on": entered_date.isoformat() if entered_date else None,
            "entered_is_estimate": entered_est,
            "days_in_stage": days_in_stage,
            "days_cv_to_schedule": days_cv_to_schedule,
            "next_interview": next_interview,
            "interviews": interviews,
            "interviews_scheduled": len(interviews),
            "interviews_done": sum(1 for i in interviews if i["done"]),
            "feedback_saved": sum(1 for i in interviews if i["feedback_count"]),
            "feedback_pending": len(pending),
            "feedback_rows": len(feedback_rows),
            "feedback_status": feedback_status, "feedback_state": feedback_state,
            "first_feedback_on": first_feedback_on.isoformat() if first_feedback_on else None,
            "days_interview_to_feedback": days_interview_to_feedback,
            "days_feedback_to_decision": days_feedback_to_decision,
            "decision_pending_days": decision_pending_days,
            "over_target": bool(reasons), "over_target_reasons": reasons, "excess_days": excess,
            "delay_days": delay,
            "history": history_display,
        })

    candidates_out.sort(key=lambda r: (not r["over_target"], -r["excess_days"], -r["delay_days"], r["name"].lower()))

    # ---- one row per position ---------------------------------------------------------
    positions_out = []
    for p in raw["positions"]:
        mine = [r for r in candidates_out if r["position_id"] == p["id"]]
        jd_date = local_date(p["created_at"])
        cv_dates = sorted(d for d in (date.fromisoformat(r["cv_fed_on"]) for r in mine if r["cv_fed_on"]))
        first_cv = cv_dates[0] if cv_dates else None
        meta = position_meta.get(p["id"], {})
        closed = meta.get("status") == "closed"
        open_days = days_between(jd_date, today)
        open_over = (not closed) and open_days is not None and open_days > targets["position_open_days"]
        stage_counts = {ph: 0 for ph in db.STAGE_PHASES}
        for r in mine:
            stage_counts[r["phase"]] += 1
        avg_stage = {}
        for ph in db.STAGE_PHASES:
            vals = stays_by_pos_phase.get((p["id"], ph))
            if vals:
                avg_stage[ph] = round(sum(vals) / len(vals), 1)
        over_candidates = sum(1 for r in mine if r["over_target"])
        positions_out.append({
            "position_id": p["id"], "title": p["title"], "status": meta.get("status"),
            "jd_uploaded_on": jd_date.isoformat() if jd_date else None,
            "first_cv_on": first_cv.isoformat() if first_cv else None,
            "days_jd_to_first_cv": days_between(jd_date, first_cv),
            "cvs_received": len(mine),
            "stage_counts": stage_counts,
            "interviews_scheduled": sum(r["interviews_scheduled"] for r in mine),
            "interviews_done": sum(r["interviews_done"] for r in mine),
            "feedback_saved": sum(r["feedback_saved"] for r in mine),
            "feedback_pending": sum(r["feedback_pending"] for r in mine),
            "open_days": open_days, "days_to_fill": meta.get("days_to_fill"), "filled": closed,
            "open_over_target": open_over,
            "avg_days_per_stage": avg_stage,
            "items_over_target": over_candidates + (1 if open_over else 0),
            "candidates_over_target": over_candidates,
        })

    chart = []
    for ph in db.STAGE_PHASES:
        vals = stays_by_phase.get(ph)
        if vals:
            chart.append({"phase": ph, "avg_days": round(sum(vals) / len(vals), 1), "count": len(vals)})

    return {
        "today": today.isoformat(),
        "targets": dict(targets),
        "positions": positions_out,
        "candidates": candidates_out,
        "avg_days_per_stage": chart,
        "totals": {
            "positions": len(positions_out), "candidates": len(candidates_out),
            "candidates_over_target": sum(1 for r in candidates_out if r["over_target"]),
            "items_over_target": sum(p["items_over_target"] for p in positions_out),
        },
    }


def build_timeline(position_ids=None, today=None, now=None):
    """Reads the database and returns compute_timeline()'s result. position_ids
    scopes it (None = every position); today/now are for tests, default to the
    machine's local date/time."""
    now = now or datetime.now()
    today = today or now.date()
    raw = db.timeline_raw_data(position_ids)
    meta = {
        r["position_id"]: {"status": r["status"], "days_to_fill": r["days_to_fill"]}
        for r in db.funnel_by_position(position_ids)
    }
    return compute_timeline(raw, db.get_timeline_targets(), today, now, meta)


def candidate_timeline(candidate_id, today=None, now=None):
    """The timeline row for one candidate (for the candidate page), or None."""
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        return None
    data = build_timeline([candidate["position_id"]], today=today, now=now)
    return next((r for r in data["candidates"] if r["candidate_id"] == candidate_id), None)
