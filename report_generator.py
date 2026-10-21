"""Built-in (reportlab) PDF reports - used whenever the optional Word-template path
(branded_report.py) is not available, and as its fallback.

The OVERALL report contains every table and graph the overall dashboard shows, in the
same order, and ends with the dashboard's last section "Hiring Timeline and Buffer
Time" (a by-position table, a graph and a by-candidate table):

  1. KPI cards (positions, active/closed/upcoming, candidates, joined, selection rate, avg days to hire)
  2. Positions by Status (graph) and the overall hiring funnel (graph)
  3. Activity Heatmap (graph)
  4. Time to Fill, Age of Open Positions, Time to Hire by Role, Offer Acceptance Rate (graphs)
  5. One funnel graph per position (the position cards)
  6. Full funnel by position (table, wide, landscape page)
  7. Hiring Timeline and Buffer Time (landscape pages): by-position table, average-days-per-stage
     graph, by-candidate table (most delayed first)

Wide tables sit on landscape pages, cells wrap (Paragraph cells), header rows repeat on every
page and a row is never split across a page break. Numbers come from report_data.py, the same
builder the dashboard page uses.
"""
import io
import os
from datetime import datetime

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape

import db
import report_data
import timeline
from config import COMPANY_NAME

PURPLE = colors.HexColor("#662984")
CYAN = colors.HexColor("#0096AF")
GREEN = colors.HexColor("#96C800")
YELLOW = colors.HexColor("#F3B827")
GREY = colors.HexColor("#797973")
RED = colors.HexColor("#B51729")
CREAM = colors.HexColor("#FAFAED")
TEXT_BODY = colors.HexColor("#333333")
BORDER = colors.HexColor("#E3E0D8")
SUB_HEADER = colors.HexColor("#F2F2F2")
TOTAL_FILL = colors.HexColor("#E8F7F7")

BUCKET_ROW_FILLS = {
    "green": colors.HexColor("#F2FFF2"),
    "yellow": colors.HexColor("#FFF8E8"),
    "red": colors.HexColor("#FCE8E8"),
    "unscored": colors.HexColor("#F2F2F2"),
}
HEAT_COLORS = [colors.HexColor("#F2F2F2"), colors.HexColor("#C6E9C6"), colors.HexColor("#8FD48F"), GREEN, colors.HexColor("#5A7A00")]

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logo.png")

TITLE_STYLE = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=PURPLE, spaceAfter=6)
SUBTITLE_STYLE = ParagraphStyle("Subtitle", fontName="Helvetica", fontSize=10, leading=13, textColor=GREY, spaceAfter=16)
SECTION_STYLE = ParagraphStyle("Section", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=PURPLE, spaceBefore=16, spaceAfter=8, keepWithNext=1)
BIG_SECTION_STYLE = ParagraphStyle("BigSection", parent=SECTION_STYLE, fontSize=16, leading=20, spaceBefore=0, spaceAfter=4, keepWithNext=1)
SUB_SECTION_STYLE = ParagraphStyle("SubSection", parent=SECTION_STYLE, fontSize=11, leading=14, spaceBefore=12, spaceAfter=6, keepWithNext=1)
NOTE_STYLE = ParagraphStyle("Note", fontName="Helvetica", fontSize=8, leading=10.5, textColor=GREY, spaceAfter=6)
BODY_STYLE = ParagraphStyle("Body", fontName="Helvetica", fontSize=9, leading=12, textColor=TEXT_BODY)
CARD_TITLE_STYLE = ParagraphStyle("CardTitle", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=PURPLE)
CARD_META_STYLE = ParagraphStyle("CardMeta", fontName="Helvetica", fontSize=7.5, leading=10, textColor=GREY)

PORTRAIT = A4
LANDSCAPE = landscape(A4)
MARGIN = 1.6 * cm
PORTRAIT_WIDTH = PORTRAIT[0] - 2 * MARGIN
LANDSCAPE_WIDTH = LANDSCAPE[0] - 2 * MARGIN


# ---------------------------------------------------------------------------
# page furniture
# ---------------------------------------------------------------------------

def _logo_flowable(max_width=3.5 * cm):
    if not os.path.exists(LOGO_PATH):
        return None
    reader = ImageReader(LOGO_PATH)
    w, h = reader.getSize()
    height = max_width * (h / w)
    return Image(LOGO_PATH, width=max_width, height=height)


def _header(title, subtitle=None):
    flowables = []
    logo = _logo_flowable()
    if logo:
        flowables.append(logo)
        flowables.append(Spacer(1, 6))
    flowables.append(Paragraph(title, TITLE_STYLE))
    generated = f"Generated {datetime.now().strftime('%d %b %Y, %H:%M')}"
    flowables.append(Paragraph(f"{subtitle + ' &middot; ' if subtitle else ''}{generated}", SUBTITLE_STYLE))
    return flowables


def _draw_footer(canvas, doc):
    """Footer on every page: company line, product name and page number."""
    canvas.saveState()
    width, _height = canvas._pagesize
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, 1.15 * cm, width - MARGIN, 1.15 * cm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(GREY)
    canvas.drawString(MARGIN, 0.7 * cm, f"{COMPANY_NAME}  |  HR Hiring Agent")
    canvas.drawRightString(width - MARGIN, 0.7 * cm, f"Page {doc.page}")
    canvas.restoreState()


def _new_doc(buffer):
    """A document with a portrait and a landscape page template (switch with
    NextPageTemplate('landscape') / ('portrait') right before a PageBreak)."""
    doc = BaseDocTemplate(buffer, pagesize=PORTRAIT, topMargin=MARGIN, bottomMargin=MARGIN,
                          leftMargin=MARGIN, rightMargin=MARGIN)
    portrait_frame = Frame(MARGIN, MARGIN, PORTRAIT[0] - 2 * MARGIN, PORTRAIT[1] - 2 * MARGIN, id="portrait",
                           leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    landscape_frame = Frame(MARGIN, MARGIN, LANDSCAPE[0] - 2 * MARGIN, LANDSCAPE[1] - 2 * MARGIN, id="landscape",
                            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="portrait", frames=[portrait_frame], pagesize=PORTRAIT, onPage=_draw_footer),
        PageTemplate(id="landscape", frames=[landscape_frame], pagesize=LANDSCAPE, onPage=_draw_footer),
    ])
    return doc


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

def _cell_style(size=7, bold=False, align="LEFT", color=TEXT_BODY):
    return ParagraphStyle(
        f"cell-{size}-{bold}-{align}", fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size,
        leading=size + 2, textColor=color, alignment={"LEFT": 0, "CENTER": 1, "RIGHT": 2}[align],
    )


def _p(text, size=7, bold=False, align="LEFT", color=TEXT_BODY):
    """A wrapping table cell."""
    return Paragraph(escape("" if text is None else str(text)), _cell_style(size, bold, align, color))


def _kpi_table(pairs, width=None):
    """pairs: list of (label, value) - rendered as a two-row table, labels on top."""
    header = [Paragraph(escape(str(p[0])), _cell_style(8, True, "CENTER", colors.white)) for p in pairs]
    values = [str(p[1]) for p in pairs]
    col_widths = None
    if width:
        col_widths = [width / len(pairs)] * len(pairs)
    table = Table([header, values], hAlign="LEFT", colWidths=col_widths)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), PURPLE),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 1), (-1, 1), 13),
                ("TEXTCOLOR", (0, 1), (-1, 1), TEXT_BODY),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _data_table(rows, col_widths, header_rows=1, extra_style=None, font=7, zebra=True):
    """A branded table: purple header band that repeats on every page, wrapping cells,
    thin grid, no row ever split across a page. rows[0:header_rows] are the header rows."""
    table = Table(rows, colWidths=col_widths, repeatRows=header_rows, splitByRow=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), PURPLE),
        ("TEXTCOLOR", (0, 0), (-1, header_rows - 1), colors.white),
        ("FONTNAME", (0, 0), (-1, header_rows - 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), font),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    if zebra:
        for r in range(header_rows, len(rows)):
            if (r - header_rows) % 2 == 1:
                style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#FAFAF5")))
    table.setStyle(TableStyle(style + (extra_style or [])))
    return table


def _scaled(widths, total):
    """Scales relative column weights so they add up to `total` points."""
    s = sum(widths)
    return [w * total / s for w in widths]


# ---------------------------------------------------------------------------
# graphs (reportlab vector drawings)
# ---------------------------------------------------------------------------

def _funnel_chart(labels, values, width=460, height=190, bar_color=CYAN):
    drawing = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x = 40
    chart.y = 30
    chart.width = width - 60
    chart.height = height - 60
    chart.data = [values]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 5.5 if width < 300 else 8
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 7 if width < 300 else 8
    chart.bars[0].fillColor = bar_color
    chart.barWidth = 12 if width < 300 else 14
    chart.groupSpacing = 8
    drawing.add(chart)
    return drawing


def _status_pie(counts, width=260, height=160):
    drawing = Drawing(width, height)
    pie = Pie()
    pie.x = 20
    pie.y = 10
    pie.width = 120
    pie.height = 120
    pie.data = [counts["active"], counts["closed"], counts["upcoming"]]
    pie.labels = [f"Active ({counts['active']})", f"Closed ({counts['closed']})", f"Upcoming ({counts['upcoming']})"]
    pie.slices[0].fillColor = GREEN
    pie.slices[1].fillColor = GREY
    pie.slices[2].fillColor = YELLOW
    pie.simpleLabels = 1
    pie.slices.fontName = "Helvetica"
    pie.slices.fontSize = 8
    drawing.add(pie)
    return drawing


def _two_slice_pie(a_label, a_value, b_label, b_value, a_color=GREEN, b_color=RED, width=260, height=150):
    drawing = Drawing(width, height)
    pie = Pie()
    pie.x = 20
    pie.y = 10
    pie.width = 110
    pie.height = 110
    pie.data = [max(a_value, 0), max(b_value, 0)]
    pie.labels = [f"{a_label} ({a_value})", f"{b_label} ({b_value})"]
    pie.slices[0].fillColor = a_color
    pie.slices[1].fillColor = b_color
    pie.simpleLabels = 1
    pie.slices.fontName = "Helvetica"
    pie.slices.fontSize = 8
    drawing.add(pie)
    return drawing


def _short(text, n=30):
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "..."


def _hbar_chart(labels, values, color, width=250, height=None, label_width=95, label_chars=22):
    """Horizontal bars (one per label) - the dashboard's Time to Fill / Age / Time to Hire graphs."""
    n = max(len(values), 1)
    height = height or (34 + 20 * n)
    drawing = Drawing(width, height)
    chart = HorizontalBarChart()
    chart.x = label_width
    chart.y = 18
    chart.width = width - label_width - 28
    chart.height = height - 28
    chart.data = [list(values)]
    chart.categoryAxis.categoryNames = [_short(l, label_chars) for l in labels]
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.boxAnchor = "e"
    chart.categoryAxis.reverseDirection = 1
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 7
    chart.bars[0].fillColor = color
    chart.barWidth = 9
    chart.bars.strokeColor = None
    chart.barLabelFormat = "%s"
    chart.barLabels.fontName = "Helvetica"
    chart.barLabels.fontSize = 7
    chart.barLabels.nudge = 8
    chart.groupSpacing = 6
    drawing.add(chart)
    return drawing


def _heatmap_drawing(heatmap, max_width):
    """GitHub-style activity grid: weeks across, Mon-Sun down, month labels on top."""
    weeks = heatmap["weeks"]
    left, top_pad = 24, 14
    step = min(9.0, (max_width - left - 4) / max(len(weeks), 1))
    cell = step - 1.5
    height = 7 * step + top_pad + 4
    drawing = Drawing(left + len(weeks) * step + 4, height)
    for m in heatmap["month_labels"]:
        drawing.add(String(left + m["week"] * step, height - 9, m["label"], fontSize=6.5, fontName="Helvetica", fillColor=GREY))
    for row, label in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
        drawing.add(String(0, height - top_pad - (row + 1) * step + 1.5, label, fontSize=6, fontName="Helvetica", fillColor=GREY))
    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            drawing.add(Rect(left + wi * step, height - top_pad - (di + 1) * step + 0.75, cell, cell,
                             fillColor=HEAT_COLORS[day["level"]], strokeColor=None))
    return drawing


# ---------------------------------------------------------------------------
# the dashboard sections
# ---------------------------------------------------------------------------

FUNNEL_LABELS = ["Sourced", "Reached", "L1", "L2", "L3", "Offered", "Joined"]
FUNNEL_KEYS = ["total", "reached", "l1_cleared", "l2_cleared", "l3_cleared", "offer_made", "joined"]


def _kpi_story(data):
    counts, overall = data["counts"], data["overall"]
    story = [
        _kpi_table([
            ("Total Positions", counts["total_positions"]), ("Active Hiring", counts["active"]),
            ("Closed Hiring", counts["closed"]), ("Upcoming Hiring", counts["upcoming"]),
        ], width=PORTRAIT_WIDTH),
        Spacer(1, 8),
    ]
    compact = [
        ("Candidates in Funnel", overall["total"]), ("Joined", overall["joined"]),
        ("Overall Selection Rate", f"{overall['selection_rate']}%"),
    ]
    if data["avg_days_to_hire"] is not None:
        compact.append(("Avg Days to Hire", f"{data['avg_days_to_hire']}d"))
    story.append(_kpi_table(compact, width=PORTRAIT_WIDTH))
    return story


def _two_up(left, right):
    """Two graphs side by side (None = leave that half blank)."""
    t = Table([[left or "", right or ""]], colWidths=[PORTRAIT_WIDTH / 2, PORTRAIT_WIDTH / 2], hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _titled(title, content, note=None):
    parts = [Paragraph(title, CARD_TITLE_STYLE)]
    if note:
        parts.append(Paragraph(note, CARD_META_STYLE))
    parts.append(Spacer(1, 3))
    parts.append(content)
    return parts


def _timing_graphs_story(data):
    funnel_rows = data["funnel_rows"]
    filled = [r for r in funnel_rows if r["days_to_fill"] is not None]
    open_rows = [r for r in funnel_rows if r["days_to_fill"] is None]
    half = PORTRAIT_WIDTH / 2 - 8
    story = []
    left = _titled("Time to Fill (days from opening to filled)", _hbar_chart([r["title"] for r in filled], [r["days_to_fill"] for r in filled], CYAN, half)) if filled else None
    right = _titled("Age of Open Positions (days still open)", _hbar_chart([r["title"] for r in open_rows], [r["days_open"] for r in open_rows], YELLOW, half)) if open_rows else None
    if left or right:
        story.append(_two_up(left, right))
        story.append(Spacer(1, 8))
    hire = data["days_to_hire_by_position"]
    offer = data["offer_acceptance"]
    left = _titled("Time to Hire by Role (avg. days sourced to joined)", _hbar_chart([r["title"] for r in hire], [r["avg_days"] for r in hire], PURPLE, half)) if hire else None
    right = None
    if offer and offer.get("rate") is not None:
        right = _titled(
            f"Offer Acceptance Rate ({offer['rate']}%)",
            _two_slice_pie("Accepted", offer["accepted"], "Declined", offer["declined"]),
            note=f"{offer['accepted']} accepted, {offer['declined']} declined",
        )
    if left or right:
        story.append(_two_up(left, right))
    return story


def _position_cards_story(data):
    """One card per position: title, status, selection rate, days open / to fill, deadline
    warning and its own funnel graph - the dashboard's position cards."""
    cards = []
    for row in data["funnel_rows"]:
        days = f"{row['days_to_fill']}d to fill" if row["days_to_fill"] is not None else f"{row['days_open']}d open"
        meta = f"{db.STATUS_LABELS[row['status']]} &middot; {row['selection_rate']}% selection rate &middot; {days}"
        if row.get("deadline"):
            meta += f" &middot; <font color='#B51729'>{escape(row['deadline']['message'])}</font>"
        chart = _funnel_chart(FUNNEL_LABELS, [row[k] for k in FUNNEL_KEYS], width=230, height=120, bar_color=PURPLE)
        cards.append([Paragraph(escape(row["title"]), CARD_TITLE_STYLE), Paragraph(meta, CARD_META_STYLE), chart])
    if not cards:
        return []
    story = []
    rows = []
    for i in range(0, len(cards), 2):
        pair = cards[i:i + 2]
        if len(pair) == 1:
            pair.append("")
        rows.append(pair)
    table = Table(rows, colWidths=[PORTRAIT_WIDTH / 2, PORTRAIT_WIDTH / 2], hAlign="LEFT", splitByRow=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0, colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(table)
    return story


def _funnel_table_story(data):
    """The dashboard's full funnel table: every stage column per position, plus a TOTAL row."""
    stage_groups, status_fields = data["stage_groups"], data["status_fields"]
    overall = data["overall"]
    hdr = lambda t, **kw: _p(t, 6, True, "CENTER", colors.white)  # noqa: E731
    top = [hdr("Position"), hdr("Days"), hdr("In Funnel")]
    sub = [hdr(""), hdr(""), hdr("")]
    style = [("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (1, 1)), ("SPAN", (2, 0), (2, 1))]
    col = 3
    for group, items in stage_groups:
        top.append(hdr(group))
        top.extend([hdr("")] * (len(items) - 1))
        style.append(("SPAN", (col, 0), (col + len(items) - 1, 0)))
        sub.extend(hdr(label) for _f, label, _g in items)
        col += len(items)
    for _f, label in status_fields:
        top.append(hdr(label))
        sub.append(hdr(""))
        style.append(("SPAN", (col, 0), (col, 1)))
        col += 1
    rows = [top, sub]
    for r in data["funnel_rows"]:
        days = f"{r['days_to_fill']}d (filled)" if r["days_to_fill"] is not None else f"{r['days_open']}d"
        rows.append([_p(r["title"], 7, True), _p(days, 7, align="CENTER"), _p(r["total"], 7, align="CENTER")]
                    + [_p(r[f], 7, align="CENTER") for _g, items in stage_groups for f, _l, _x in items]
                    + [_p(r[f], 7, align="CENTER") for f, _l in status_fields])
    rows.append([_p("TOTAL - all positions", 7, True), _p("-", 7, align="CENTER"), _p(overall["total"], 7, True, "CENTER")]
                + [_p(overall[f], 7, True, "CENTER") for _g, items in stage_groups for f, _l, _x in items]
                + [_p(overall[f], 7, True, "CENTER") for f, _l in status_fields])
    n_stage = sum(len(items) for _g, items in stage_groups) + len(status_fields)
    widths = _scaled([4.0, 1.6, 1.4] + [1.35] * (n_stage - len(status_fields)) + [1.3] * len(status_fields), LANDSCAPE_WIDTH)
    style.append(("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), TOTAL_FILL))
    style.append(("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#8A4FA8")))
    return [
        Paragraph("Full Funnel by Position", BIG_SECTION_STYLE),
        Paragraph("Every pipeline stage per position; the last row adds them all up.", NOTE_STYLE),
        _data_table(rows, widths, header_rows=2, extra_style=style, zebra=False),
    ]


# ---- the new LAST section: Hiring Timeline and Buffer Time ---------------------------

PHASE_SHORT = {
    "Sourced": "Sourced", "Reached": "Reached", "Interview Scheduled": "Interview scheduled", "Interviewed": "Inter- viewed",
    "Round Cleared": "Round cleared", "Offer Made": "Offer made", "On Hold": "On hold", "Joined": "Joined", "Rejected": "Rejected",
}


def _timeline_story(tl, width, heading=True, show_position_col=True):
    """The 'Hiring Timeline and Buffer Time' section as flowables for a page `width` points wide
    (landscape): targets line, by-position table, average-days-per-stage graph, by-candidate table
    (most delayed first). Used by the overall report (last section) and the per-position report."""
    tg = tl["targets"]
    story = []
    if heading:
        story.append(Paragraph("Hiring Timeline and Buffer Time", BIG_SECTION_STYLE))
    story.append(Paragraph(
        "When the JD went up, when each CV came in, where every CV is now and how long things have been waiting, in "
        "calendar days. A dash (-) means that date was never recorded; <b>est.</b> means no stage date existed, so the "
        "CV-added date stands in.", NOTE_STYLE))
    story.append(Paragraph(
        f"<b>Over target when:</b> in one stage more than {tg['stage_age_days']}d &middot; feedback pending more than "
        f"{tg['feedback_pending_days']}d &middot; no decision after feedback more than {tg['decision_pending_days']}d &middot; "
        f"position open more than {tg['position_open_days']}d. &nbsp; <font color='#B51729'><b>"
        f"{tl['totals']['candidates_over_target']} of {tl['totals']['candidates']} candidate(s) over target.</b></font>", BODY_STYLE))

    # -- by position --
    story.append(Paragraph("By position", SUB_SECTION_STYLE))
    h = lambda t: _p(t, 5.5, True, "CENTER", colors.white)  # noqa: E731
    phases = list(PHASE_SHORT)
    top = [h("Position"), h("JD and CVs")] + [h("")] * 3 + [h("Candidates in each stage")] + [h("")] * (len(phases) - 1) \
        + [h("Interviews and feedback")] + [h("")] * 3 + [h("Buffer time")] + [h("")] * 2
    sub = [h(""), h("JD uploaded on"), h("First CV on"), h("JD to 1st CV (d)"), h("CVs received")] \
        + [h(PHASE_SHORT[p]) for p in phases] \
        + [h("Sched- uled"), h("Done"), h("Feedback saved"), h("Feedback pending"), h("Position open (d)"), h("Avg days per stage"), h("Over target")]
    rows = [top, sub]
    for p in tl["positions"]:
        avg = "<br/>".join(f"{escape(k)} {v}d" for k, v in p["avg_days_per_stage"].items()) or "-"
        open_val = p["days_to_fill"] if p["filled"] and p["days_to_fill"] is not None else p["open_days"]
        rows.append(
            [_p(p["title"], 6.5, True), _p(timeline.fmt_date(p["jd_uploaded_on"]), 6.5, align="CENTER"),
             _p(timeline.fmt_date(p["first_cv_on"]), 6.5, align="CENTER"), _p(timeline.fmt_days(p["days_jd_to_first_cv"]), 6.5, align="CENTER"),
             _p(p["cvs_received"], 6.5, align="CENTER")]
            + [_p(p["stage_counts"][ph], 6.5, align="CENTER") for ph in phases]
            + [_p(p["interviews_scheduled"], 6.5, align="CENTER"), _p(p["interviews_done"], 6.5, align="CENTER"),
               _p(p["feedback_saved"], 6.5, align="CENTER"), _p(p["feedback_pending"], 6.5, align="CENTER"),
               _p(f"{timeline.fmt_days(open_val)}{' (filled)' if p['filled'] else ''}", 6.5, align="CENTER"),
               Paragraph(avg, _cell_style(6, False, "LEFT")), _p(p["items_over_target"], 6.5, True, "CENTER", RED if p["items_over_target"] else TEXT_BODY)]
        )
    if not tl["positions"]:
        rows.append([_p("No positions yet.", 7)] + [_p("", 7)] * 20)
    widths = _scaled([3.6, 2.3, 2.3, 1.4, 1.4] + [2.1] * len(phases) + [1.7, 1.4, 1.9, 1.9, 1.8, 4.2, 1.5], width)
    style = [("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (4, 0)), ("SPAN", (5, 0), (5 + len(phases) - 1, 0)),
             ("SPAN", (5 + len(phases), 0), (8 + len(phases), 0)), ("SPAN", (9 + len(phases), 0), (11 + len(phases), 0)),
             ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#8A4FA8"))]
    story.append(_data_table(rows, widths, header_rows=2, extra_style=style, font=6.5))

    # -- graph: average days per stage --
    chart_rows = tl["avg_days_per_stage"]
    if chart_rows:
        story.append(Paragraph("Average days per stage", SUB_SECTION_STYLE))
        story.append(Paragraph("How long candidates have spent in each stage (completed stays plus the stage they are in now); "
                               "estimated dates are left out.", NOTE_STYLE))
        story.append(_hbar_chart([f"{r['phase']} (n={r['count']})" for r in chart_rows], [r["avg_days"] for r in chart_rows],
                                 CYAN, width=min(width, 560), label_width=170, label_chars=34))

    # -- by candidate --
    story.append(Paragraph("By candidate (most delayed first)", SUB_SECTION_STYLE))
    cols = ["Candidate", "Position", "CV fed on", "Days since CV", "Current stage", "Entered stage on", "Days in stage",
            "CV to interview scheduled (d)", "Next interview", "Feedback", "Interview to feedback (d)",
            "Feedback to decision (d)", "Over target"]
    keep = [i for i, name in enumerate(cols) if show_position_col or name != "Position"]   # a position's own report hides the Position column
    rows = [[h(cols[i]) for i in keep]]
    extra = []
    for i, c in enumerate(tl["candidates"], start=1):
        est = " est." if c["entered_is_estimate"] else ""
        reasons = "; ".join(c["over_target_reasons"])
        if c["over_target"]:
            flag = Paragraph(f"<font color='#B51729'><b>OVER TARGET</b></font><br/>{escape(reasons)}", _cell_style(6, False, "LEFT"))
        elif c["phase"] in ("Joined", "Rejected", "On Hold"):
            flag = _p("-", 6.5, align="CENTER")
        else:
            flag = _p("On track", 6.5, align="CENTER")
        full_row = [
            _p(c["name"], 6.5, True), _p(c["position_title"], 6.5), _p(timeline.fmt_date(c["cv_fed_on"]), 6.5, align="CENTER"),
            _p(timeline.fmt_days(c["days_since_cv"]), 6.5, align="CENTER"), _p(c["stage_label"], 6.5),
            _p(timeline.fmt_date(c["entered_stage_on"]) + est, 6.5, align="CENTER"),
            _p(timeline.fmt_days(c["days_in_stage"]) + (est if c["days_in_stage"] is not None else ""), 6.5, align="CENTER"),
            _p(timeline.fmt_days(c["days_cv_to_schedule"]), 6.5, align="CENTER"), _p(c["next_interview"], 6.5),
            _p(c["feedback_status"], 6.5, align="CENTER"), _p(timeline.fmt_days(c["days_interview_to_feedback"]), 6.5, align="CENTER"),
            _p(timeline.fmt_days(c["days_feedback_to_decision"]), 6.5, align="CENTER"), flag,
        ]
        rows.append([full_row[i] for i in keep])
        if c["over_target"]:
            extra.append(("BACKGROUND", (0, i), (0, i), colors.HexColor("#FCE8E8")))
    if not tl["candidates"]:
        rows.append([_p("No candidates yet.", 7)] + [_p("", 7)] * (len(keep) - 1))
    widths = _scaled([[4.3, 3.4, 2.1, 1.3, 3.4, 2.3, 1.5, 1.9, 3.4, 2.2, 1.7, 1.7, 4.8][i] for i in keep], width)
    story.append(_data_table(rows, widths, header_rows=1, extra_style=extra, font=6.5))
    return story


# ---------------------------------------------------------------------------
# the two reports
# ---------------------------------------------------------------------------

def generate_position_report(position_id):
    position = db.get_position(position_id)
    candidates = db.list_candidates(position_id)
    kpis = db.funnel_for_position(position_id)
    tl = timeline.build_timeline([position_id])

    buffer = io.BytesIO()
    doc = _new_doc(buffer)
    story = []

    story += _header(f"Hiring Report &mdash; {escape(position['title'])}")
    story.append(
        _kpi_table(
            [
                ("In Funnel", kpis["total"]),
                ("Reached", kpis["reached"]),
                ("L1 Cleared", kpis["l1_cleared"]),
                ("L2 Cleared", kpis["l2_cleared"]),
                ("L3 Cleared", kpis["l3_cleared"]),
                ("Offers", kpis["offer_made"]),
                ("Joined", kpis["joined"]),
                ("Selection Rate", f"{kpis['selection_rate']}%"),
            ],
            width=PORTRAIT_WIDTH,
        )
    )

    story.append(Paragraph("Hiring Funnel", SECTION_STYLE))
    values = [kpis[k] if k != "total" else kpis["total"] for k in FUNNEL_KEYS]
    story.append(_funnel_chart(FUNNEL_LABELS, values))

    story.append(Paragraph("Candidates", SECTION_STYLE))
    head = [_p(t, 8, True, color=colors.white) for t in ("Candidate", "Score", "Screening", "Current Stage")]
    rows = [head]
    for c in candidates:
        score = f"{c['score']:.1f}" if c["score"] is not None else "Not scored yet"
        rows.append([_p(c["candidate_name"] or c["resume_filename"], 8), _p(score, 8), _p(c["bucket"], 8),
                     _p(db.current_stage_label(c), 8)])
    style = []
    for row_idx, c in enumerate(candidates, start=1):
        fill = BUCKET_ROW_FILLS.get(c["bucket"], BUCKET_ROW_FILLS["unscored"])
        style.append(("BACKGROUND", (0, row_idx), (-1, row_idx), fill))
    story.append(_data_table(rows, _scaled([7, 2.4, 3, 4], PORTRAIT_WIDTH), extra_style=style, font=8, zebra=False))

    # this position's timeline rows (landscape pages)
    story.append(NextPageTemplate("landscape"))
    story.append(PageBreak())
    story += _timeline_story(tl, LANDSCAPE_WIDTH, show_position_col=False)

    doc.build(story)
    buffer.seek(0)
    return buffer, position["title"]


def generate_overall_report(position_ids=None):
    """The Overall Hiring Report: every table and graph of the overall dashboard, then the
    Hiring Timeline and Buffer Time section last. position_ids scopes it like the dashboard
    does for a non-admin user."""
    data = report_data.overall_dashboard_data(position_ids=position_ids)
    counts, overall = data["counts"], data["overall"]

    buffer = io.BytesIO()
    doc = _new_doc(buffer)
    story = []

    story += _header("Overall Hiring Report", subtitle=f"{counts['total_positions']} position(s)")
    story += _kpi_story(data)

    left = _titled("Positions by Status", _status_pie(counts))
    right = _titled("Overall Hiring Funnel", _funnel_chart(FUNNEL_LABELS, [overall[k] for k in FUNNEL_KEYS], width=265, height=160, bar_color=PURPLE))
    story.append(Spacer(1, 8))
    story.append(_two_up(left, right))

    heat = data["activity_heatmap"]
    story.append(KeepTogether([
        Paragraph(f"Activity Heatmap <font size='8' color='#797973'>({heat['total']} action{'' if heat['total'] == 1 else 's'} in the last year)</font>", SECTION_STYLE),
        _heatmap_drawing(heat, PORTRAIT_WIDTH),
        Paragraph("Each square is one day; darker means more hiring activity (stage changes, notes, emails, new candidates).", NOTE_STYLE),
    ]))

    story += _timing_graphs_story(data)

    cards = _position_cards_story(data)
    if cards:
        story.append(Paragraph("Positions", SECTION_STYLE))
        story += cards

    # wide tables on landscape pages
    story.append(NextPageTemplate("landscape"))
    story.append(PageBreak())
    story += _funnel_table_story(data)
    story.append(PageBreak())
    story += _timeline_story(data["timeline"], LANDSCAPE_WIDTH)   # the LAST section

    doc.build(story)
    buffer.seek(0)
    return buffer
