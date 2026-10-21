"""Tests for the HR Hiring Agent (hiring timeline, interview scheduling and panel feedback, report PDFs,
score breakdown, default resume order).

Run from the project folder:   python -m unittest discover -s tests -t . -v

Every test runs against a throw-away data folder (HR_AGENT_DATA_DIR is pointed at a fresh temp dir the
moment this package is imported), never against data/.
"""
from . import _testenv  # noqa: F401  (sets HR_AGENT_DATA_DIR before db.py is imported anywhere)
