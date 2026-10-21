"""One source of truth for what the OVERALL dashboard shows.

The dashboard page (app.home) and both "Generate Report" PDF builders
(report_generator.py = built-in reportlab style, branded_report.py = optional Word
template renderer) all take their numbers from overall_dashboard_data(), so the PDF cannot
drift away from the dashboard: every table and graph on the dashboard is a key in
the dict below, and the last one is the "Hiring Timeline and Buffer Time" section.
"""
from datetime import date, timedelta

import db
import timeline


def build_activity_heatmap(days=364, position_ids=None):
    """Weeks-as-columns, Mon-Sun-as-rows grid (GitHub contribution graph
    layout) built server-side so the template just loops over it - counts
    are bucketed into 5 color levels by quartiles of the non-zero days.
    position_ids scopes this to a non-admin assigned user's own position(s)."""
    counts = db.daily_activity_counts(days=days, position_ids=position_ids)
    today = date.today()
    start = today - timedelta(days=days - 1)
    start -= timedelta(days=start.weekday())  # back up to that week's Monday

    non_zero = sorted(v for v in counts.values() if v > 0)

    def level_for(n):
        if n == 0 or not non_zero:
            return 0
        idx = int(len(non_zero) * 0.25), int(len(non_zero) * 0.5), int(len(non_zero) * 0.75)
        q1 = non_zero[min(idx[0], len(non_zero) - 1)]
        q2 = non_zero[min(idx[1], len(non_zero) - 1)]
        q3 = non_zero[min(idx[2], len(non_zero) - 1)]
        if n <= q1:
            return 1
        if n <= q2:
            return 2
        if n <= q3:
            return 3
        return 4

    weeks = []
    week = []
    d = start
    month_labels = []
    last_month = None
    week_index = 0
    while d <= today:
        iso = d.isoformat()
        n = counts.get(iso, 0)
        week.append({"date": iso, "count": n, "level": level_for(n), "in_range": d >= today - timedelta(days=days - 1)})
        if d.weekday() == 0 and d.day <= 7 and d.month != last_month:
            month_labels.append({"week": week_index, "label": d.strftime("%b")})
            last_month = d.month
        if d.weekday() == 6:
            weeks.append(week)
            week = []
            week_index += 1
        d += timedelta(days=1)
    if week:
        weeks.append(week)

    return {"weeks": weeks, "month_labels": month_labels, "total": sum(counts.values())}


def overall_dashboard_data(position_ids=None, today=None):
    """Everything on the overall dashboard, in one dict. position_ids scopes it to a
    non-admin's assigned positions (None = every position)."""
    funnel_rows = db.funnel_by_position(position_ids=position_ids)
    return {
        "overall": db.overall_kpis(position_ids=position_ids),
        "funnel_rows": funnel_rows,
        "counts": db.status_counts(funnel_rows),
        "avg_days_to_hire": db.avg_days_to_hire(position_ids=position_ids),
        "activity_heatmap": build_activity_heatmap(position_ids=position_ids),
        "days_to_hire_by_position": db.days_to_hire_by_position(position_ids=position_ids),
        "offer_acceptance": db.offer_acceptance_stats(position_ids=position_ids),
        "stage_groups": db.stage_groups(),
        "status_fields": db.STATUS_FIELDS,
        "status_labels": db.STATUS_LABELS,
        # the LAST section of the dashboard (and of the PDF): Hiring Timeline and Buffer Time
        "timeline": timeline.build_timeline(position_ids, today=today),
    }
