import io
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import db
from config import COMPANY_NAME

PURPLE = colors.HexColor("#662984")
GREY = colors.HexColor("#797973")
TEXT_BODY = colors.HexColor("#333333")
BORDER = colors.HexColor("#E3E0D8")

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logo.png")
TEMPLATE_PATH = os.path.join(db.DATA_DIR, "offer_letter_template.docx")

TEMPLATE_TOKENS = [
    ("{{ candidate_name }}", "Candidate's full name"),
    ("{{ designation }}", "Job title being offered"),
    ("{{ position }}", "Position/role title (from the hiring pipeline)"),
    ("{{ ctc }}", "Annual CTC as entered by HR"),
    ("{{ joining_date }}", "Proposed joining date as entered by HR"),
    ("{{ notes }}", "Any additional notes HR typed in"),
    ("{{ date }}", "Today's date"),
]

SOFFICE_CANDIDATES = [
    "soffice",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]

TITLE_STYLE = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=PURPLE, spaceAfter=4)
DATE_STYLE = ParagraphStyle("Date", fontName="Helvetica", fontSize=9, leading=12, textColor=GREY, spaceAfter=18)
BODY_STYLE = ParagraphStyle("Body", fontName="Helvetica", fontSize=10.5, leading=16, textColor=TEXT_BODY, spaceAfter=12)
SIGN_STYLE = ParagraphStyle("Sign", fontName="Helvetica-Bold", fontSize=10.5, leading=15, textColor=TEXT_BODY, spaceBefore=24)


def _logo_flowable(max_width=3.5 * cm):
    if not os.path.exists(LOGO_PATH):
        return None
    reader = ImageReader(LOGO_PATH)
    w, h = reader.getSize()
    height = max_width * (h / w)
    return Image(LOGO_PATH, width=max_width, height=height)


def has_custom_template():
    return os.path.exists(TEMPLATE_PATH)


def save_template(file_bytes):
    os.makedirs(db.DATA_DIR, exist_ok=True)
    with open(TEMPLATE_PATH, "wb") as f:
        f.write(file_bytes)


def delete_template():
    if os.path.exists(TEMPLATE_PATH):
        os.remove(TEMPLATE_PATH)


def _find_soffice():
    for candidate in SOFFICE_CANDIDATES:
        if shutil.which(candidate) or os.path.exists(candidate):
            return candidate
    raise RuntimeError("LibreOffice (soffice) not found - can't convert the uploaded template to PDF.")


def _template_context(candidate, position):
    return {
        "candidate_name": candidate["candidate_name"] or candidate["resume_filename"],
        "designation": candidate.get("offer_designation") or "",
        "position": position["title"],
        "ctc": candidate.get("offer_ctc") or "",
        "joining_date": candidate.get("offer_joining_date") or "",
        "notes": candidate.get("offer_notes") or "",
        "date": datetime.now().strftime("%d %B %Y"),
        "expiry_date": candidate.get("offer_expiry_date") or "",
        "employment_type": candidate.get("offer_employment_type") or "",
    }


def _generate_from_template(candidate, position):
    from docxtpl import DocxTemplate

    soffice = _find_soffice()
    tmp_dir = tempfile.mkdtemp(prefix="offer_letter_")
    try:
        doc = DocxTemplate(TEMPLATE_PATH)
        doc.render(_template_context(candidate, position))
        filled_docx = os.path.join(tmp_dir, "offer_letter.docx")
        doc.save(filled_docx)

        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp_dir, filled_docx],
            capture_output=True,
            timeout=60,
        )
        pdf_path = os.path.join(tmp_dir, "offer_letter.pdf")
        if not os.path.exists(pdf_path):
            stderr = (result.stderr or b"").decode(errors="replace")
            raise RuntimeError(f"LibreOffice did not produce a PDF from the offer template: {stderr}")
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def generate_offer_letter(candidate, position):
    """Uses the HR-uploaded .docx letterhead template (Settings -> Offer Letter
    Template) if one exists, filling {{ tokens }} and converting via LibreOffice.
    Falls back to a generic placeholder letter if no template is uploaded, or
    if filling/converting the template fails for any reason - offer generation
    should never hard-fail just because a template has a typo'd token or LibreOffice
    hiccups. Returns (pdf_bytes, template_warning) - template_warning is set when
    the fallback was used because the uploaded template itself failed, so the
    caller can tell HR their letter used a generic placeholder instead."""
    if has_custom_template():
        try:
            return _generate_from_template(candidate, position), None
        except Exception as exc:
            return _generate_placeholder_letter(candidate, position), (
                f"Your uploaded offer template failed to render ({exc}) - used the generic placeholder "
                f"letter instead. Check the template's {{{{ tokens }}}} under Settings."
            )
    return _generate_placeholder_letter(candidate, position), None


def _generate_placeholder_letter(candidate, position):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, topMargin=1.8 * cm, bottomMargin=1.8 * cm, leftMargin=2 * cm, rightMargin=2 * cm
    )
    story = []

    logo = _logo_flowable()
    if logo:
        story.append(logo)
        story.append(Spacer(1, 10))

    story.append(Paragraph("Offer of Employment", TITLE_STYLE))
    story.append(Paragraph(datetime.now().strftime("%d %B %Y"), DATE_STYLE))

    name = candidate["candidate_name"] or candidate["resume_filename"]
    designation = candidate.get("offer_designation") or "[Designation]"
    ctc = candidate.get("offer_ctc") or "[CTC]"
    joining_date = candidate.get("offer_joining_date") or "[Joining Date]"
    notes = candidate.get("offer_notes") or ""
    employment_type = candidate.get("offer_employment_type") or ""
    expiry_date = candidate.get("offer_expiry_date") or ""

    story.append(Paragraph(f"Dear {name},", BODY_STYLE))
    story.append(
        Paragraph(
            f"We are pleased to offer you the position of <b>{designation}</b> at {COMPANY_NAME}, "
            f"for the role of <b>{position['title']}</b>. This letter confirms the terms of your "
            f"employment, subject to the conditions outlined below and any accompanying policy "
            f"documents.",
            BODY_STYLE,
        )
    )

    detail_rows = [
        ["Designation", designation],
        ["Position", position["title"]],
        ["Annual CTC", ctc],
        ["Proposed Joining Date", joining_date],
    ]
    if employment_type:
        detail_rows.append(["Employment Type", employment_type])
    if expiry_date:
        detail_rows.append(["Offer Valid Until", expiry_date])
    details = Table(detail_rows, colWidths=[5 * cm, 9 * cm])
    details.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("TEXTCOLOR", (0, 0), (-1, -1), TEXT_BODY),
                ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(Spacer(1, 6))
    story.append(details)
    story.append(Spacer(1, 12))

    if notes:
        story.append(Paragraph(notes, BODY_STYLE))

    story.append(
        Paragraph(
            "This offer is contingent upon successful completion of any pending background or "
            "reference checks. Please confirm your acceptance of this offer at your earliest convenience.",
            BODY_STYLE,
        )
    )
    story.append(Paragraph("We look forward to welcoming you to the team.", BODY_STYLE))

    story.append(Paragraph(f"For {COMPANY_NAME},", SIGN_STYLE))
    story.append(Paragraph("HR Team", BODY_STYLE))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()
