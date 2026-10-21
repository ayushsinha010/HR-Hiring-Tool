# HR Hiring Agent

![HR Hiring Agent](static/logo.png)

HR Hiring Agent is a small recruitment tracker that runs on your own machine. A recruiter creates a
position from a job description, adds résumés, optionally lets an LLM score each résumé against the
job description, and then follows every candidate through interview rounds, offer and onboarding
from one hiring tracker. It is a single Flask app with a SQLite database, meant for a small HR team
or for evaluating the idea; it is not a hosted service.

## Features

- **Positions**: create a position from an uploaded job description (`.docx`, `.pdf`, `.txt`) or from
  an LLM-drafted one; ongoing / closed / upcoming status; per-position interview rounds (the classic
  L1/L2/L3, or custom round names).
- **Résumés**: upload `.docx` / `.pdf` / `.txt` files, link a local folder that is watched for new
  files, or bulk-import candidates from a `.csv` / `.xlsx` sheet. Candidate name and e-mail are
  extracted from the résumé text; duplicates are detected.
- **AI scoring (optional)**: score résumés 0-10 against the job description with a score breakdown,
  strengths and concerns, shown as green / yellow / red buckets. Also optional: screening assessment
  questions, interview-question suggestions, an LLM-drafted job description, short drafted e-mail notes,
  a per-position Q&A chatbot and interview scorecards. All of these need an API key (see Configuration).
- **Hiring tracker**: stage checkboxes per candidate, stage history with timestamps, notes, an
  Excel export of the tracker, and a calendar of activity.
- **Interviews**: schedule interviews, assign interviewers, collect panel feedback (recommendation,
  rating, comments) per round, attach interview recordings / transcripts.
- **Quick capture**: type a sentence such as "Noah selected for L2, schedule tomorrow at 3pm"; the
  app fuzzy-matches the candidate (rapidfuzz), parses the date (parsedatetime) and asks you to
  confirm before saving.
- **Dashboard and reports**: pipeline funnel, time to fill, time to hire, offer acceptance, activity
  heatmap and a "Hiring Timeline and Buffer Time" section that flags candidates / positions waiting
  longer than configurable targets. Overall and per-position PDF reports (reportlab).
- **E-mail**: JD and interview-invite e-mails sent through each user's own SMTP account, based on
  editable `.docx` templates in `email_templates/` (with built-in fallback wording).
- **Offers**: offer letter PDF (built-in placeholder letter, or your own `.docx` letterhead template
  filled through LibreOffice), approval link for an approver, response link for the candidate.
- **Onboarding checklist**, personal notifications, an audit log and an input log.
- **Users and access**: password login, an admin role, and per-position assignment so a non-admin
  user only sees the positions they are assigned to.

## Tech stack

- Python 3 with Flask (dependencies are not pinned in `requirements.txt`; the versions used for
  verification are listed below)
- SQLite (standard library `sqlite3`), no ORM
- Jinja2 templates, plain JavaScript, Chart.js 4 (loaded from the jsDelivr CDN) and the Open Sans web
  font (Google Fonts), so the dashboard charts and font need an internet connection
- python-docx, docxtpl, openpyxl, PyMuPDF (`pymupdf`), reportlab, rapidfuzz, parsedatetime, httpx,
  anthropic, waitress

Verified with: Python 3.14.7, Flask 3.1.3, python-docx 1.2.0, openpyxl 3.1.5, PyMuPDF 1.28.2,
reportlab 5.0.0, docxtpl 0.20.2, rapidfuzz 3.14.5, parsedatetime 2.6, anthropic 0.122.0.

## Project structure

```
app.py                 Flask app: routes, login, access rules
db.py                  SQLite schema, migrations on start, all queries
scorer.py              LLM calls (OpenRouter or Anthropic): scoring, drafts, assessments
capture.py             Quick-capture sentence parsing and fuzzy candidate matching
timeline.py            Hiring timeline / buffer-time calculations
report_data.py         Numbers shared by the dashboard and the PDF reports
report_generator.py    Built-in PDF reports (reportlab)
branded_report.py      Optional template-based PDF reports (needs an external renderer, see Notes)
emailer.py             SMTP sending and e-mail template loading
offer_letter.py        Offer letter PDF (placeholder letter or uploaded .docx template)
resume_parser.py, candidate_import.py, folder_watch.py, tracker_export.py, font_installer.py
config.py              .env loader, COMPANY_NAME / COMPANY_WEBSITE
email_templates/       Editable .docx e-mail templates
static/, templates/    CSS, JavaScript, logo and Jinja2 pages
scripts/               backup.ps1, watchdog.ps1, make_email_templates.py, reset_password.py
tests/                 unittest suite (runs on throw-away data)
data/                  Created/used at run time (git-ignored; only .gitkeep is committed)
```

## Prerequisites

- Python 3.10 or newer (the original notes say 3.10+; verified here on 3.14.7). Check with
  `python --version`.
- Nothing else is required for the core app.
- Optional:
  - an API key from [OpenRouter](https://openrouter.ai) or Anthropic for the AI features;
  - an SMTP mailbox (the default host is `smtp.office365.com`, port 587) to actually send e-mails;
  - LibreOffice (`soffice`) to use an uploaded `.docx` offer-letter template;
  - Windows with PowerShell for the "Install Report Template" font button and the "export to Desktop"
    button. The core app is plain Python and was developed on Windows; it has not been run on
    macOS or Linux as part of this write-up (not verified).

## Setup and run

Windows PowerShell:

```powershell
git clone <your-repo-url> HR-Hiring-Agent
cd HR-Hiring-Agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# optional: choose the first admin password yourself (otherwise one is generated and printed)
$env:HR_AGENT_ADMIN_PASSWORD = "<choose a password>"
python app.py
```

macOS / Linux:

```bash
git clone <your-repo-url> HR-Hiring-Agent
cd HR-Hiring-Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# optional: choose the first admin password yourself (otherwise one is generated and printed)
export HR_AGENT_ADMIN_PASSWORD="<choose a password>"
python app.py
```

Open <http://127.0.0.1:5000>. On the very first start the app creates `data/` with an empty
database and one user, `admin`:

- if `HR_AGENT_ADMIN_PASSWORD` was set, that is the password;
- otherwise a random password is generated and printed once in the console
  (`[first run] Created the admin user 'admin' ...`). Note it down; it is not stored in plain text.

Sign in, then change the password under **Settings -> Change Password**. If the password is lost,
stop the app and run `python scripts/reset_password.py` (it asks for a new one).

After signing in you see an empty dashboard. Use **+ Add Position** to create the first position.
There is no seeded demo data.

To use another port, set `HR_AGENT_PORT` (for example `HR_AGENT_PORT=5201`). For a longer-running
setup you can use waitress instead of the Flask development server:

```
python -m waitress --host=127.0.0.1 --port=5000 app:app
```

Under waitress the folder-watch background thread is not started (it is only started by
`python app.py`).

## Configuration

Variables can be exported in your shell or put in a `.env` file in the project folder (copy
`.env.example`; real shell variables win over the file). All are optional.

| Variable | Default | Purpose |
|---|---|---|
| `HR_AGENT_HOST` | `127.0.0.1` | Address `python app.py` binds to. |
| `HR_AGENT_PORT` | `5000` | Port for `python app.py`. |
| `HR_AGENT_DEBUG` | `1` | Flask debugger/reloader for `python app.py`; it is never enabled when the host is not `127.0.0.1` / `localhost`. Set `0` to turn it off. |
| `HR_AGENT_DATA_DIR` | `./data` | Folder for the database and uploaded files. |
| `HR_AGENT_ADMIN_PASSWORD` | random, printed once | Password of the first `admin` user (only used when the database has no users). |
| `HR_AGENT_SECRET_KEY` | random, saved in `data/secret_key` | Flask session signing key. |
| `COMPANY_NAME` | `Your Company` | Name used in e-mails, the placeholder offer letter, the PDF report footer and LLM prompts. |
| `COMPANY_WEBSITE` | `www.example.com` | Website line in the built-in e-mail wording. |
| `HR_AGENT_EMAIL_TEMPLATE_DIR` | `./email_templates` | Folder holding the e-mail `.docx` templates. |
| `REPORT_TEMPLATE_DIR` | empty | Folder of an external template renderer for `branded_report.py` (not included). |

Set in the web UI (**Settings**), stored in the local database, never in the repository:

- **AI Scoring (LLM)**: provider (OpenRouter or Anthropic), model (default `openai/gpt-5.4` for
  OpenRouter) and API key. No key is shipped; without one, only the AI features are unavailable.
- **Email Account**: your address, an app password and the SMTP host / port. Without it, send
  attempts are logged as failed and nothing crashes.
- Onboarding checklist items, hiring-timeline targets and an optional offer-letter `.docx` template.

## Data and storage

- `data/hr.db` is the SQLite database. The schema is created on start and upgraded additively by
  `db.init_db()`.
- `data/positions/<id>/` holds uploaded job descriptions, résumés and interview files.
- `data/secret_key` is the generated session key; `data/offer_letter_template.docx` appears if you
  upload an offer template.
- Everything under `data/` is git-ignored. To reset the app: stop it and delete the contents of
  `data/` (keep `.gitkeep`); the next start is a clean first run again.
- `scripts/backup.ps1` zips `data/` into `backups/` (and optionally copies it to another folder).
  `scripts/watchdog.ps1` is a Windows health-check/restart script meant for Task Scheduler.
- All content you create (candidates, positions, notes) is yours; the repository contains no real
  people. The tests use made-up names.

## Tests / quality checks

From the project folder:

```
python -m unittest discover -s tests -t . -v
```

The suite runs against a throw-away temporary data folder and a known throw-away admin login, never
`data/`. Last run: 81 tests, 0 failures, 2 skipped (the two template-report tests, which need the
optional external renderer, matplotlib, LibreOffice and Open Sans). It took roughly a minute and a
half on the verification machine. It covers résumé ordering and scoring, the timeline / buffer-time maths, interview scheduling and
feedback, the main web pages and their access rules, PDF report generation, and an additive upgrade
of an old-schema database.

A manual smoke test on port 5201 with an empty data folder (login page, wrong password, login, create
a position with two résumés, dashboard, settings, users, audit log, tracker, XLSX export, both PDF
reports, e-mail preview, logout) returned the expected 200 / 302 / 404 responses.

## Notes and limitations

- **Local use first.** The built-in server is Flask's development server; there is no HTTPS and the
  login is a simple username/password. Do not expose it to the internet. If you share it on a
  network, put it behind a reverse proxy with HTTPS and use waitress; the debugger is disabled for
  any non-loopback host.
- **Secrets in the database.** The LLM API key and each user's SMTP app password are stored as plain
  text in `data/hr.db`, and the database and its backups also hold candidate personal data. Protect
  the `data/` and `backups/` folders accordingly and never commit them.
- Public offer-approval and offer-response links (`/offer/approve/<token>`, `/offer/respond/<token>`)
  work without logging in; the random token in the URL is the only protection.
- Single process, single SQLite file: fine for a small team, not for many concurrent writers. The
  scoring progress is kept in memory.
- "Link folder" opens a native folder picker on the machine running the app (Tkinter), and "export to
  Desktop" writes to that machine's Desktop, so both only make sense when the browser and the server
  are the same computer.
- `branded_report.py` renders template-based PDF reports only when `REPORT_TEMPLATE_DIR` points to an
  external Markdown-to-Word renderer (`scripts/build.py`, `build_doc.py`, `inject_vml.py`,
  `build_toc.py`), LibreOffice and the Open Sans font are available. That renderer is not part of
  this repository; without it the built-in reportlab reports are used. This path was not exercised.
- The legacy `users.json` file (`{"name": {"password_hash": "..."}}`) is still migrated once if it
  sits next to `db.py` and the users table is empty; it is git-ignored and not needed.
- `pip install -r requirements.txt` into a brand-new virtual environment was checked only as a dry run
  (dependency resolution succeeded); the tests ran against packages already installed on the
  verification machine.
- The generic logo in `static/logo.png` is a placeholder; replace it with your own (same file name).

## License

Released under the MIT License: see [LICENSE](LICENSE). Replace it with another license if you prefer.
