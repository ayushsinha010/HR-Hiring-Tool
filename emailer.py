import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import COMPANY_NAME, COMPANY_WEBSITE


class NoEmailAccountError(RuntimeError):
    pass


class SendError(RuntimeError):
    pass


def _get_sender_account(user):
    if not user.get("email_address") or not user.get("email_app_password"):
        raise NoEmailAccountError(
            "No email account connected. Add your Outlook address and app password under Settings first."
        )
    return {
        "address": user["email_address"],
        "password": user["email_app_password"],
        "host": user["email_smtp_host"] or "smtp.office365.com",
        "port": user["email_smtp_port"] or 587,
    }


def send_email(user, to_address, subject, body_text, attachments=None):
    """Sends one email via the given user's connected mailbox (SMTP/STARTTLS).
    attachments: list of (filename, bytes, mimetype_subtype) tuples. Raises
    NoEmailAccountError / SendError on failure - callers decide how to log/surface it."""
    account = _get_sender_account(user)

    msg = MIMEMultipart()
    msg["From"] = account["address"]
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    for filename, data, subtype in attachments or []:
        part = MIMEApplication(data, _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    try:
        with smtplib.SMTP(account["host"], account["port"], timeout=30) as server:
            server.starttls()
            server.login(account["address"], account["password"])
            server.sendmail(account["address"], [to_address], msg.as_string())
    except NoEmailAccountError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to caller for logging, not swallowed
        raise SendError(str(exc)) from exc


# Built-in fallback wording, used only if the live template file (below) is
# missing or fails to parse - mirroring the default .docx templates in email_templates/.

EMAIL_SUBJECT_TEMPLATES = {
    "no_response": "{role_name} — Opportunity at {company} (Missed Call Follow-Up)",
    "jd_only": "{role_name} — Job Description from {company}",
    "link_only": "Interview Invite — {role_name} at {company}",
    "jd_and_link": "{role_name} — Job Description & Interview Details from {company}",
}


def _render_builtin_subject(kind, role_name):
    return EMAIL_SUBJECT_TEMPLATES[kind].format(role_name=role_name, company=COMPANY_NAME)


def render_no_response_email(candidate_name, role_name, sender_name, sender_email):
    """"No Response to Call" template - tried calling, couldn't connect, JD attached."""
    return (
        f"Hi {candidate_name},\n\n"
        f"We tried reaching you earlier regarding an opening for the {role_name} position with {COMPANY_NAME}, "
        "but couldn't connect.\n\n"
        "We came across your profile and believe your background could be a strong fit. Please find the detailed "
        "Job Description attached for your reference.\n\n"
        f"You can learn more about us at {COMPANY_WEBSITE}.\n\n"
        "Please reply to this email if you'd like to move forward, and we'll share the next steps.\n\n"
        "Looking forward to hearing from you.\n\n"
        "Warm regards,\n\n"
        f"{sender_name}\n\n"
        f"{COMPANY_NAME}\n\n"
        f"{sender_email}"
    )


def render_jd_only_email(candidate_name, role_name, sender_name, sender_email):
    """"JD Only" template - just the Job Description, no interview details yet."""
    return (
        f"Hi {candidate_name},\n\n"
        "Thank you for taking the time to speak with us today. As discussed, please find the detailed Job "
        f"Description for the {role_name} position attached for your reference.\n\n"
        f"You can learn more about us at {COMPANY_WEBSITE}.\n\n"
        "Happy to answer any questions you may have.\n\n"
        "Looking forward to hearing from you.\n\n"
        "Warm regards,\n\n"
        f"{sender_name}\n\n"
        f"{COMPANY_NAME}\n\n"
        f"{sender_email}"
    )


def render_interview_invite_email(candidate_name, role_name, date, time, teams_link, sender_name, sender_email):
    """"Interview Invite" template - date/time/Teams link only, no JD attached."""
    return (
        f"Hi {candidate_name},\n\n"
        f"We're pleased to invite you for an interview for the {role_name} position with {COMPANY_NAME}.\n\n"
        "Interview Details:\n\n"
        f"Date: {date}\n\n"
        f"Time: {time}\n\n"
        "Mode: Microsoft Teams\n\n"
        f"Meeting Link: {teams_link}\n\n"
        "Please join a few minutes early to avoid any last-minute technical issues.\n\n"
        "Looking forward to speaking with you.\n\n"
        "Warm regards,\n\n"
        f"{sender_name}\n\n"
        f"{COMPANY_NAME}\n\n"
        f"{sender_email}"
    )


def render_jd_and_interview_email(candidate_name, role_name, date, time, teams_link, sender_name, sender_email):
    """"JD and Interview Invite" template - Job Description plus date/time/Teams link, in one email."""
    return (
        f"Hi {candidate_name},\n\n"
        "Thank you for taking the time to speak with us today. As discussed, please find the detailed Job "
        f"Description for the {role_name} position attached for your reference.\n\n"
        "We're pleased to invite you for an interview:\n\n"
        f"Date: {date}\n\n"
        f"Time: {time}\n\n"
        "Mode: Microsoft Teams\n\n"
        f"Meeting Link: {teams_link}\n\n"
        "Please join a few minutes early to avoid any last-minute technical issues.\n\n"
        f"You can also learn more about us at {COMPANY_WEBSITE}.\n\n"
        "Looking forward to speaking with you.\n\n"
        "Warm regards,\n\n"
        f"{sender_name}\n\n"
        f"{COMPANY_NAME}\n\n"
        f"{sender_email}"
    )


def _render_builtin_email(kind, candidate_name, role_name, sender_name, sender_email, date=None, time=None, teams_link=None):
    if kind == "no_response":
        return render_no_response_email(candidate_name, role_name, sender_name, sender_email)
    if kind == "jd_only":
        return render_jd_only_email(candidate_name, role_name, sender_name, sender_email)
    if kind == "link_only":
        return render_interview_invite_email(candidate_name, role_name, date, time, teams_link, sender_name, sender_email)
    if kind == "jd_and_link":
        return render_jd_and_interview_email(candidate_name, role_name, date, time, teams_link, sender_name, sender_email)
    raise ValueError(f"Unknown email kind: {kind}")


# ---- Live templates, read straight from the .docx files HR edits directly -
# no upload step: the portal fetches the current wording from disk every time
# it composes an email, so editing the Word file is the whole workflow.

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
EMAIL_TEMPLATE_DIR = os.environ.get("HR_AGENT_EMAIL_TEMPLATE_DIR") or os.path.join(PROJECT_DIR, "email_templates")

EMAIL_TEMPLATE_KINDS = ("no_response", "jd_only", "link_only", "jd_and_link")
EMAIL_TEMPLATE_LABELS = {
    "no_response": "No Response to Call",
    "jd_only": "JD Only",
    "link_only": "Interview Invite",
    "jd_and_link": "JD and Interview Invite",
}
EMAIL_TEMPLATE_FILENAMES = {
    "no_response": "Email Template - No Response to Call.docx",
    "jd_only": "Email Template - JD Only.docx",
    "link_only": "Email Template - Interview Invite.docx",
    "jd_and_link": "Email Template - JD and Interview Invite.docx",
}
EMAIL_TEMPLATE_TOKENS = [
    ("{{ candidate_name }}", "Candidate's full name"),
    ("{{ role_name }}", "Position/role title (from the hiring pipeline)"),
    ("{{ sender_name }}", "Your display name"),
    ("{{ sender_email }}", "Your email address"),
    ("{{ date }}", "Interview date as entered by HR (blank for JD Only / No Response)"),
    ("{{ time }}", "Interview time as entered by HR (blank for JD Only / No Response)"),
    ("{{ teams_link }}", "Teams meeting link as entered by HR (blank for JD Only / No Response)"),
]


def email_template_path(kind):
    return os.path.join(EMAIL_TEMPLATE_DIR, EMAIL_TEMPLATE_FILENAMES[kind])


def _substitute_tokens(text, context):
    for key, value in context.items():
        text = text.replace("{{ " + key + " }}", value).replace("{{" + key + "}}", value)
    return text


def _parse_email_template_file(path):
    """Reads the HR-facing .docx (title / description / "Subject Line" heading
    + value / "Email Body" heading + body) and pulls out just the subject and
    body text, tokens intact. Raises if the file is missing or doesn't have a
    recognizable "Email Body" section."""
    import docx as docx_lib

    paragraphs = [p.text for p in docx_lib.Document(path).paragraphs]

    subject = None
    for i, text in enumerate(paragraphs):
        if text.strip() == "Subject Line":
            subject = next((t for t in paragraphs[i + 1:] if t.strip()), None)
            break

    body_start = next((i + 1 for i, t in enumerate(paragraphs) if t.strip() == "Email Body"), None)
    if body_start is None:
        raise ValueError('template has no "Email Body" section')
    # Word paragraphs get their visual gap from paragraph spacing, not literal
    # blank lines, so a plain "\n" join would read as one dense block. Drop any
    # blank paragraphs and re-insert the gap explicitly with "\n\n".
    body_lines = [t for t in paragraphs[body_start:] if t.strip()]
    if not body_lines:
        raise ValueError("Email Body section is empty")

    return subject, "\n\n".join(body_lines)


def render_candidate_email_subject(kind, role_name):
    try:
        subject, _ = _parse_email_template_file(email_template_path(kind))
        if subject:
            return _substitute_tokens(subject, {"role_name": role_name})
    except Exception:
        pass
    return _render_builtin_subject(kind, role_name)


def render_candidate_email(kind, candidate_name, role_name, sender_name, sender_email, date=None, time=None, teams_link=None, custom_note=None):
    """Returns (body, template_warning). Reads the live wording straight from
    the kind's .docx file in EMAIL_TEMPLATE_DIR every time - HR edits that file
    directly, no upload/versioning inside the app. Falls back to the built-in
    wording if the file is missing or malformed, so a typo in the Word doc
    never blocks sending - just surfaces a warning.

    custom_note: an optional short, recruiter-reviewed personal note (can be
    AI-drafted) appended near the top of the body, ahead of the fixed
    template wording - the official template text itself is never rewritten."""
    context = {
        "candidate_name": candidate_name,
        "role_name": role_name,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "date": date or "",
        "time": time or "",
        "teams_link": teams_link or "",
    }
    try:
        _, body_template = _parse_email_template_file(email_template_path(kind))
        body = _substitute_tokens(body_template, context)
        warning = None
    except Exception as exc:
        body = _render_builtin_email(kind, candidate_name, role_name, sender_name, sender_email, date, time, teams_link)
        warning = (
            f"Couldn't read the {EMAIL_TEMPLATE_LABELS[kind]} template file ({exc}) - used the built-in "
            f"wording instead. Check {EMAIL_TEMPLATE_FILENAMES[kind]} in the Email Templates folder."
        )

    note = (custom_note or "").strip()
    if note:
        greeting = f"Hi {candidate_name},"
        if body.startswith(greeting):
            body = f"{greeting}\n\n{note}\n\n" + body[len(greeting):].lstrip("\n")
        else:
            body = f"{note}\n\n{body}"
    return body, warning


def render_stakeholder_notification_email(stakeholder_name, candidate_name, role_name, round_label, date, time, teams_link):
    """Internal heads-up to whoever is conducting a round - separate from the
    candidate's own invite, since the stakeholder needs to know they're on the
    hook to actually show up and interview, not just that a link was sent."""
    return (
        f"Hi {stakeholder_name},\n\n"
        f"You're assigned to conduct the {round_label} interview for {candidate_name}, "
        f"who is being considered for the {role_name} position.\n\n"
        "Interview Details:\n\n"
        f"Candidate: {candidate_name}\n\n"
        f"Date: {date}\n\n"
        f"Time: {time}\n\n"
        "Mode: Microsoft Teams\n\n"
        f"Meeting Link: {teams_link}\n\n"
        "Best regards,\n"
        f"{COMPANY_NAME} HR Team"
    )


def render_offer_approval_request_body(candidate_name, position_title, designation, ctc, joining_date, approval_url):
    return (
        f"Hi,\n\n"
        f"An offer for {candidate_name} ({position_title}) is ready for your approval.\n\n"
        f"Designation: {designation}\n"
        f"CTC: {ctc}\n"
        f"Joining Date: {joining_date}\n\n"
        f"Please review the offer letter and approve or reject it here:\n{approval_url}\n\n"
        "Best regards,\n"
        f"{COMPANY_NAME} HR Team"
    )


def render_offer_to_candidate_body(candidate_name, position_title, custom_message, respond_url, expiry_date=None):
    expiry_line = f"Please respond by {expiry_date}, after which this offer may no longer be valid.\n\n" if expiry_date else ""
    return (
        f"Dear {candidate_name},\n\n"
        f"{custom_message}\n\n"
        f"Please find your offer letter for the {position_title} role attached. "
        f"You can view the offer and let us know your decision here:\n{respond_url}\n\n"
        f"{expiry_line}"
        "Best regards,\n"
        f"{COMPANY_NAME} HR Team"
    )
