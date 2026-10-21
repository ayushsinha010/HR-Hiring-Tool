"""Bulk candidate import from a spreadsheet (.csv/.xlsx) - the common "Import from
Spreadsheet" flow of commercial ATS tools. This is additive to, not a replacement
for, the normal resume-upload/watched-folder flow: it lets HR bulk-add
candidates from plain contact data (e.g. a job-fair or referral list) with no
resume file required per person.
"""
import csv
import io

import openpyxl

# Header names are matched case-insensitively against these aliases; anything
# else in the sheet is ignored, so extra columns (Resume File, Sl No, ...) in a
# sheet exported from elsewhere don't break the import.
_COLUMN_ALIASES = {
    "name": {"name", "candidate name", "full name", "candidate"},
    "email": {"email", "email address", "e-mail"},
    "phone": {"phone", "phone number", "mobile", "mobile number", "contact", "contact number"},
    "experience": {"experience", "exp", "total experience", "years of experience"},
    "company": {"current company", "company", "employer"},
    "role": {"current role", "role", "designation", "title", "current title"},
    "location": {"location", "city", "current location"},
}


def _map_headers(header_row):
    mapping = {}
    for idx, raw in enumerate(header_row):
        key = (raw or "").strip().lower()
        for field, aliases in _COLUMN_ALIASES.items():
            if key in aliases and field not in mapping:
                mapping[field] = idx
    return mapping


def parse_rows(file_storage, ext):
    """Returns a list of dicts (keys: name/email/phone/experience/company/role/
    location, each an empty string if that column wasn't present) from an
    uploaded .csv or .xlsx file. The first non-empty row is always treated as
    the header row; blank rows are skipped."""
    if ext == ".csv":
        text = file_storage.read().decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    else:
        wb = openpyxl.load_workbook(file_storage, data_only=True)
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]

    rows = [r for r in rows if any(str(c).strip() for c in r if c is not None)]
    if not rows:
        return []

    header, data_rows = rows[0], rows[1:]
    mapping = _map_headers(header)

    def get(row, field):
        idx = mapping.get(field)
        if idx is None or idx >= len(row) or row[idx] is None:
            return ""
        return str(row[idx]).strip()

    return [
        {field: get(row, field) for field in _COLUMN_ALIASES}
        for row in data_rows
    ]
