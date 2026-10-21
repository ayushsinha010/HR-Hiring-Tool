"""Deployment-wide settings, read from environment variables (optionally via a local .env file).

Importing this module first (db.py and app.py do) loads KEY=VALUE lines from a `.env` file in the
project folder into os.environ, without overriding variables that are already set. See
.env.example for every supported variable.

COMPANY_NAME / COMPANY_WEBSITE appear in candidate emails, the generated offer letter, the PDF
report footer and the LLM prompts that draft recruiter notes. Change them with these variables -
nothing else in the code needs editing.
"""
import os


def _load_dotenv(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

COMPANY_NAME = os.environ.get("COMPANY_NAME", "Your Company").strip() or "Your Company"
COMPANY_WEBSITE = os.environ.get("COMPANY_WEBSITE", "www.example.com").strip() or "www.example.com"
